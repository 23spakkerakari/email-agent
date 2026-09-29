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

## Sending via Resend instead of Gmail

`emails_resend.py` is a drop-in alternative with the same `generate` / `send` commands,
but `send` goes through [Resend](https://resend.com) rather than SMTP. Drafting is shared,
so `outbox/` drafts work with either script.

```powershell
pip install resend
# in .env: RESEND_API_KEY, RESEND_FROM (on a domain verified in Resend), optional RESEND_REPLY_TO
python emails_resend.py generate leads.csv --limit 2
python emails_resend.py send
```

Resend won't send from a Gmail address: `RESEND_FROM` must be on a domain you've verified
at https://resend.com/domains. Set `RESEND_REPLY_TO` to your Gmail so replies still land there.
Sends are spaced 1s apart by default (Resend's rate limit is 2 req/s).

## Follow-ups

`followup.py` drafts a follow-up for everyone who hasn't replied. Each person can get up
to **5** follow-ups after the original. Each stage reads the previous stage's folder and,
once sent, the whole thread moves forward one folder:

```
sent/  --stage 1-->  followup1/  --stage 2-->  followup2/  --stage 3-->  ...  --stage 5-->  followup5/
```

So `sent/` only ever holds people who got just the original, `followup1/` only people
who got exactly one follow-up, and so on. Nobody can be sent the same follow-up twice,
and nobody in `followup5/` is ever drafted again.

```powershell
python followup.py --status                # how many people are at each stage
python followup.py --limit 2               # 1st follow-up, test on 2 first
python followup.py                         # 1st follow-up for everyone in sent/
python emails.py send                      # send from the same Gmail so it threads

python followup.py --stage 2               # 2nd follow-up for everyone in followup1/
python emails.py send
# ... and so on up to --stage 5
```

The text for every stage lives in **`followups.md`**, one `## Follow-up N` section per
stage, with `{name}` where the first name goes. Edit it to change what each follow-up says.
To send something different just once, put the body in a text file and pass `--message that.txt`.

The name comes from the original's greeting line, the subject is `Re: <original>`, and the
latest message in the thread is quoted underneath (so a 3rd follow-up shows the 2nd, which
shows the 1st, like a normal reply chain).

Send follow-ups before running `generate` on a new lead list, since `send` sends
everything in `outbox/`. Duplicate detection in `generate` checks every archive folder,
so you won't accidentally cold-email someone you're already following up with.
