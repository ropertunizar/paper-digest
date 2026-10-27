# paper-digest

A self-hosted research digest. On a schedule you choose (daily, weekly, monthly), it scans
**arXiv**, **journals** (by ISSN) and **conference proceedings** for papers on *your* topics,
ranks them, and emails you only when something relevant appears.

Runs for free on GitHub Actions: no server, no PC left on.

**How it works**
1. Fetches new papers from arXiv, plus journals and conferences via Crossref. Missing abstracts are filled from OpenAlex.
2. Keeps papers that mention your `required_terms`, and drops duplicates (the journal version wins over the arXiv preprint).
3. Scores each paper with your keyword `sections`, and files it under its best-matching section.
4. Optional: Claude re-ranks the shortlist against your research profile and adds a one-line "why".
5. Sends one HTML email. No matches means no email.

---

## Setup (about 10 minutes, all in the browser)

### 1. Create your copy
On this repo's main page, click the green **Use this template** button (top right, next to **Code**), then **Create a new repository**.
- Owner: you. Name: anything, e.g. `my-paper-digest`.
- Choose **Private** (recommended: your config reveals your research interests, and GitHub pauses schedules in public repos after 60 days without commits).
- Click **Create repository**.

### 2. Make it yours: edit `config.yaml`
Click `config.yaml` in your new repo, then the **pencil icon** (top right of the file view). Change:
- `contact_email`: your email.
- `required_terms`: words that define your field (or `[]` to accept everything).
- `sections`: your topics and keyword weights. Add, remove or rename sections freely.
- `arxiv_categories`, `journals`, `conferences`: your venues.
- `days_back`: must match the schedule (step 5).
- `llm.profile`: only if you use AI re-ranking.

Then click the green **Commit changes…** button (top right), and **Commit changes** in the popup.

### 3. Create an email password for the sender
The digest is sent *from* an email account you own.

**Gmail (simplest):**
1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/twosv
2. Create an App Password: https://myaccount.google.com/apppasswords. Type a name (e.g. `paper-digest`) in the **App name** box and click **Create**.
3. Copy the 16-character password from the popup (it's shown only once).

Tip: a dedicated Gmail account just for sending keeps your main mailbox separate.

**Other providers:** use their SMTP server and an app password if they offer one, and add the `SMTP_HOST` secret below
(e.g. Outlook: `smtp.office365.com`, or your university's SMTP server). The script tries SSL on port 465, then STARTTLS on 587.

### 4. Add your secrets
In your repo: **Settings** tab (top bar, right end) → **Secrets and variables** (left sidebar, under "Security") → **Actions** → green **New repository secret** button.
Add each one separately. Names must match exactly:

| Name | Value | Required |
|---|---|---|
| `SMTP_USER` | the sender email address | yes |
| `SMTP_PASS` | the app password from step 3 | yes |
| `MAIL_TO` | where to receive the digest; several allowed, comma-separated | yes |
| `SMTP_HOST` | SMTP server if not Gmail | no |
| `ANTHROPIC_API_KEY` | key from https://console.anthropic.com, enables AI ranking (a few cents per run) | no |

### 5. Choose the frequency
Edit `.github/workflows/digest.yml` (pencil icon, as in step 2) and change the `cron:` line. Times are UTC:

| Frequency | cron | `days_back` in config.yaml |
|---|---|---|
| Weekly, Monday | `"0 6 * * 1"` (default) | 7 |
| Daily | `"0 6 * * *"` | 1 |
| Monthly, 1st | `"0 6 1 * *"` | 31 |

### 6. Test it
**Actions** tab (top bar) → **paper-digest** (left sidebar) → green **Run workflow** button (top right) → **Run workflow** in the popup.
Click the new run → **digest** (left column) → expand **Run python digest.py** to watch the log. The first email may land in spam.

---

## Tuning
- **Too many papers:** raise `min_score`, lower `max_papers`, or make `required_terms` stricter.
- **Too few:** lower `min_score`, add keywords, or add more categories and venues.
- **Wrong section:** raise that keyword's weight in the right section.
- The email shows each paper's score and matched keywords, so use those to adjust the weights.

## Troubleshooting
| Log says | Fix |
|---|---|
| `Missing secrets: SMTP_PASS` | Secret missing or misspelled (check the exact name in step 4). |
| `SMTPAuthenticationError` | Wrong app password, or 2-Step Verification is off. |
| `Connection unexpectedly closed` | Usually a wrong or empty password; recreate the app password. |
| `[journal X issn] skipped` | Wrong ISSN; check it on the journal's homepage. |
| `Nothing relevant this period` | Normal. Lower `min_score` if it happens every time. |

## Cost
Free. Private repos get 2,000 GitHub Actions minutes per month; a run takes about 5–10 min.
