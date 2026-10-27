#!/usr/bin/env python3
"""Paper digest: scans arXiv, journals and conferences for papers on your topics
and emails you a ranked digest. Configure everything in config.yaml.

Secrets / env vars:
  SMTP_USER, SMTP_PASS, MAIL_TO (comma-separated for several recipients)  -> required
  SMTP_HOST (default smtp.gmail.com)                                      -> optional
  ANTHROPIC_API_KEY                                                       -> optional AI re-ranking
  DRY_RUN=1                                                               -> print HTML, don't send
"""
import os, re, json, time, smtplib, html, datetime as dt
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import sys
import requests, yaml

sys.stdout.reconfigure(line_buffering=True)  # live log lines in GitHub Actions

ARXIV = "http://export.arxiv.org/api/query"
CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org/works"
NS = {"a": "http://www.w3.org/2005/Atom"}
HERE = os.path.dirname(os.path.abspath(__file__))
UA = {}


def load_cfg():
    with open(os.path.join(HERE, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    UA["User-Agent"] = f"paper-digest/1.0 (mailto:{cfg.get('contact_email', '')})"
    return cfg


def window(cfg):
    end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return end - dt.timedelta(days=cfg["days_back"]), end


def clean(s):
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").split())


# ---------------- arXiv ----------------
def build_query(cfg, start, end):
    cats = " OR ".join(f"cat:{c}" for c in cfg["arxiv_categories"])
    q = f"({cats}) AND submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    req = cfg.get("required_terms") or []
    if req:
        terms = " OR ".join(f'abs:"{t}"' if " " in t else f"abs:{t}" for t in req)
        q = f"({terms}) AND " + q
    return q


def fetch_arxiv(cfg):
    start, end = window(cfg)
    q, papers, idx, page = build_query(cfg, start, end), [], 0, 200
    while True:
        r = requests.get(ARXIV, params={"search_query": q, "start": idx, "max_results": page,
                         "sortBy": "submittedDate", "sortOrder": "descending"}, timeout=60)
        r.raise_for_status()
        entries = ET.fromstring(r.text).findall("a:entry", NS)
        for e in entries:
            papers.append({
                "id": e.find("a:id", NS).text.strip(),
                "title": clean(e.find("a:title", NS).text),
                "abstract": clean(e.find("a:summary", NS).text),
                "authors": [a.find("a:name", NS).text for a in e.findall("a:author", NS)],
                "date": e.find("a:published", NS).text[:10], "venue": "arXiv"})
        if len(entries) < page or idx > cfg.get("arxiv_max", 2000):
            break
        idx += page
        time.sleep(3)
    return papers


# ---------------- Crossref (journals + conferences) ----------------
def _cr_item(it, venue):
    parts = (it.get("published") or it.get("issued") or {}).get("date-parts", [[None]])[0]
    date = "-".join(f"{p:02d}" for p in parts if p) if parts and parts[0] else ""
    return {"id": it.get("URL") or f"https://doi.org/{it['DOI']}", "doi": it["DOI"].lower(),
            "title": clean((it.get("title") or [""])[0]), "abstract": clean(it.get("abstract")),
            "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])],
            "date": date, "venue": venue}


def _cr_get(url, params):
    out, cursor = [], "*"
    while cursor:
        r = requests.get(url, params={**params, "rows": 500, "cursor": cursor}, headers=UA, timeout=60)
        if r.status_code == 404:
            return out
        r.raise_for_status()
        msg = r.json()["message"]
        out += msg.get("items", [])
        cursor = msg.get("next-cursor") if len(msg.get("items", [])) == 500 else None
        time.sleep(1)
    return out


def _filters(cfg, extra=""):
    start, end = window(cfg)
    # newly indexed this week AND published recently (avoids old records re-indexed after edits)
    pub_from = (end - dt.timedelta(days=120)).date()
    f = f"from-index-date:{start.date()},until-index-date:{end.date()},from-pub-date:{pub_from}"
    return f + (f",{extra}" if extra else "")


SELECT = "DOI,title,abstract,author,published,issued,URL,container-title,type"


def fetch_journals(cfg):
    papers = []
    for venue, issns in (cfg.get("journals") or {}).items():
        seen = set()
        for issn in issns:
            try:
                for it in _cr_get(f"{CROSSREF}/journals/{issn}/works",
                                  {"filter": _filters(cfg), "select": SELECT}):
                    if it["DOI"] not in seen and it.get("title"):
                        seen.add(it["DOI"]); papers.append(_cr_item(it, venue))
            except Exception as e:
                print(f"[journal {venue} {issn}] skipped: {e}")
    return papers


def fetch_conferences(cfg):
    papers = []
    for venue, name in (cfg.get("conferences") or {}).items():
        try:
            items = _cr_get(f"{CROSSREF}/works", {"query.container-title": name, "select": SELECT,
                            "filter": _filters(cfg, "type:proceedings-article")})
            key = name.lower()
            for it in items:  # query is fuzzy: keep only real matches of the proceedings title
                ct = " ".join(it.get("container-title") or []).lower()
                if key in ct and it.get("title"):
                    papers.append(_cr_item(it, venue))
        except Exception as e:
            print(f"[conference {venue}] skipped: {e}")
    return papers


def enrich_abstracts(papers):
    """IEEE rarely deposits abstracts in Crossref; pull them from OpenAlex by DOI."""
    todo = [p for p in papers if p.get("doi") and not p["abstract"]]
    by_doi = {p["doi"]: p for p in todo}
    for i in range(0, len(todo), 50):
        dois = "|".join(f"https://doi.org/{p['doi']}" for p in todo[i:i + 50])
        try:
            r = requests.get(OPENALEX, headers=UA, timeout=60, params={
                "filter": f"doi:{dois}", "per-page": 50, "select": "doi,abstract_inverted_index"})
            r.raise_for_status()
            for w in r.json().get("results", []):
                inv = w.get("abstract_inverted_index")
                d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                if inv and d in by_doi:
                    pos = sorted((i, word) for word, idxs in inv.items() for i in idxs)
                    by_doi[d]["abstract"] = " ".join(word for _, word in pos)
        except Exception as e:
            print(f"[openalex] skipped batch: {e}")
        time.sleep(1)
    return papers


# ---------------- filtering & scoring ----------------
def _hits(text, kw):
    flags = 0 if kw.isupper() else re.IGNORECASE
    return len(re.findall(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])", text, flags))


def passes_required(p, cfg):
    req = cfg.get("required_terms") or []
    text = f"{p['title']} {p['abstract']}"
    return not req or any(_hits(text, t) for t in req)


def score(p, cfg):
    """Score against every section; the paper is filed under its best-scoring section."""
    total, tags, best, best_s = 0.0, [], None, 0.0
    for sec in cfg["sections"]:
        sec_s = 0.0
        for kw, w in (sec.get("keywords") or {}).items():
            t = _hits(p["title"], kw) * cfg["title_multiplier"]
            a = min(_hits(p["abstract"], kw), 3)
            if t or a:
                sec_s += w * (t + a)
                if kw not in tags:
                    tags.append(kw)
        total += sec_s
        if sec_s > best_s:
            best, best_s = sec["name"], sec_s
    if p["venue"] != "arXiv":
        total += cfg.get("peer_review_bonus", 1)
    p.update(score=round(total, 1), tags=tags, section=best or cfg["sections"][-1]["name"])
    return p


def dedupe(papers):
    """Same paper on arXiv and in a journal: keep the peer-reviewed entry."""
    key = lambda p: re.sub(r"[^a-z0-9]", "", p["title"].lower())[:80]
    out = {}
    for p in sorted(papers, key=lambda p: p["venue"] == "arXiv"):
        out.setdefault(key(p), p)
    return list(out.values())


def llm_rerank(papers, cfg):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not papers:
        return papers
    items = "\n\n".join(f"[{i}] ({p['venue']}) {p['title']}\n{p['abstract'][:1200]}"
                        for i, p in enumerate(papers))
    prompt = (f"Researcher profile: {cfg['llm']['profile']}\n\n"
              "Rate each paper's relevance to this researcher from 0 to 10 and give a one-line "
              "reason (max 20 words). Respond ONLY with a JSON array: "
              '[{"i": 0, "score": 7, "why": "..."}]\n\n' + items)
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=180, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": cfg["llm"]["model"], "max_tokens": 6000,
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = re.sub(r"```(json)?", "", "".join(b.get("text", "") for b in r.json()["content"])).strip()
        for row in json.loads(text):
            papers[row["i"]]["llm"], papers[row["i"]]["why"] = row["score"], row["why"]
        papers = [p for p in papers if p.get("llm", 0) >= cfg["llm"]["min_relevance"]]
        papers.sort(key=lambda p: (-p["llm"], -p["score"]))
    except Exception as e:
        print(f"LLM re-rank skipped: {e}")
    return papers


# ---------------- email ----------------
def render(papers, cfg):
    def card(p):
        auth = ", ".join(p["authors"][:4]) + (" et al." if len(p["authors"]) > 4 else "")
        why = f"<div style='color:#0a7'>{html.escape(p['why'])}</div>" if p.get("why") else ""
        rel = f" · relevance {p['llm']}/10" if "llm" in p else ""
        badge = ("background:#eee;color:#333" if p["venue"] == "arXiv" else "background:#1a5;color:#fff")
        abs_ = html.escape(p["abstract"][:400]) + "…" if p["abstract"] else "<i>no abstract available</i>"
        return (f"<div style='margin:0 0 18px'>"
                f"<span style='{badge};font-size:11px;padding:1px 6px;border-radius:3px'>{html.escape(p['venue'])}</span> "
                f"<a href='{p['id']}' style='font-weight:600;font-size:15px'>{html.escape(p['title'])}</a>"
                f"<div style='color:#666;font-size:12px'>{html.escape(auth)} · {p['date']} · score {p['score']}{rel}</div>"
                f"{why}<div style='font-size:12px;color:#888'>matched: {html.escape(', '.join(p['tags']))}</div>"
                f"<div style='font-size:13px;margin-top:4px'>{abs_}</div></div>")
    out = "<div style='font-family:sans-serif;max-width:760px'>"
    for sec in cfg["sections"]:
        group = [p for p in papers if p["section"] == sec["name"]]
        if group:
            out += f"<h2>{html.escape(sec['name'])} ({len(group)})</h2>" + "".join(card(p) for p in group)
    return out + "<p style='color:#999;font-size:11px'>Generated by paper-digest. Edit config.yaml to tune topics.</p></div>"


def send(subject, body):
    if os.environ.get("DRY_RUN"):
        print(subject); print(body); return
    user = os.environ.get("SMTP_USER", "").strip()
    pwd = "".join(os.environ.get("SMTP_PASS", "").split())  # app passwords: drop spaces / NBSPs
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").replace(";", ",").split(",") if a.strip()]
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASS", pwd), ("MAIL_TO", to)) if not v]
    if missing:
        raise SystemExit(f"Missing secrets: {', '.join(missing)}")
    print(f"SMTP: user={user[:3]}***@{user.split('@')[-1]}, password length={len(pwd)} (Gmail app password = 16), recipients={len(to)}")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.attach(MIMEText(body, "html"))
    try:  # SSL on 465
        with smtplib.SMTP_SSL(host, 465, timeout=60) as s:
            s.login(user, pwd); s.send_message(msg); return
    except smtplib.SMTPAuthenticationError:
        raise
    except Exception as e:
        print(f"SMTP 465 failed ({e!r}); retrying with STARTTLS on 587")
    with smtplib.SMTP(host, 587, timeout=60) as s:  # fallback: STARTTLS on 587
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(user, pwd); s.send_message(msg)


def main():
    cfg = load_cfg()
    papers = []
    sources = (("arXiv", fetch_arxiv, cfg.get("use_arxiv", True)),
               ("journals", fetch_journals, bool(cfg.get("journals"))),
               ("conferences", fetch_conferences, bool(cfg.get("conferences"))))
    for name, fn, on in sources:
        if not on:
            continue
        try:
            got = fn(cfg); papers += got; print(f"{name}: {len(got)} fetched")
        except Exception as e:
            print(f"{name} failed: {e}")
    papers = enrich_abstracts(papers)
    papers = [score(p, cfg) for p in dedupe(papers) if passes_required(p, cfg)]
    papers = [p for p in papers if p["score"] >= cfg["min_score"]]
    papers.sort(key=lambda p: -p["score"])
    papers = llm_rerank(papers[: cfg["max_papers"] * 2], cfg)[: cfg["max_papers"]]
    print(f"relevant after filtering: {len(papers)}")
    if not papers:
        print("Nothing relevant this period; no email sent."); return
    n_pr = sum(p["venue"] != "arXiv" for p in papers)
    subject = (f"{cfg.get('subject_prefix', 'Paper digest')} {dt.date.today()}: "
               f"{len(papers)} papers ({n_pr} peer-reviewed)")
    send(subject, render(papers, cfg))
    print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
