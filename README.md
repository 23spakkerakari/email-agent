# email_agent

Semi-personalized outreach emails in your own tone of voice, from a spreadsheet of leads.
Claude (`claude-opus-5`) drafts every email; nothing is sent until you review and confirm.

## Setup (once)

```powershell
pip install -r requirements.txt
copy .env.example .env    # fill in ANTHROPIC_API_KEY + Gmail app password
```

For Gmail you need an [app password](https://myaccount.google.com/apppasswords)
(requires 2FA) — your normal password won't work.

Then:

1. **`tone_samples.md`** — paste 3-5 real emails you've written. This is how it learns your voice.
2. **`campaign.md`** — describe what this outreach is about and the ask.

## Per campaign

```powershell
# leads CSV needs an 'email' column; every other column (name, company, notes, ...)
# is fed to the model as personalization context
python emails.py generate leads.csv --limit 2   # test on 2 leads first
python emails.py generate leads.csv             # draft the rest (existing drafts are skipped)
```

Drafts land in `outbox/` as editable `.md` files (`to:` / `subject:` header, then the body).
Edit or delete any you don't like, then:

```powershell
python emails.py send        # shows the full list, asks you to type 'send' to confirm
```

Sent drafts move to `sent/`. Sends are spaced 3s apart (`--delay` to change).

## Notes

- The system prompt (tone samples + campaign brief) is prompt-cached, so per-lead
  cost after the first email is mostly just the output tokens.
- The model is instructed to only personalize from facts in the CSV row — it won't
  invent details about a lead. Richer `notes` columns → better personalization.
- `.env`, `outbox/`, `sent/`, and any real `*.csv` lead lists are gitignored.
