#!/usr/bin/env python
"""Semi-personalized outreach emails in your own tone of voice.

Workflow:
  1. Paste 3-5 emails you've written into tone_samples.md
  2. Describe the outreach in campaign.md
  3. python emails.py generate leads.csv     -> drafts land in outbox/ for review
  4. Edit/delete any drafts you don't like
  5. python emails.py send                   -> sends outbox/ drafts via SMTP
"""

import argparse
import csv
import json
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import anthropic
from dotenv import load_dotenv
import os

ROOT = Path(__file__).parent
OUTBOX = ROOT / "outbox"
SENT = ROOT / "sent"

MODEL = "claude-opus-5"

SYSTEM_INSTRUCTIONS = """\
You ghost-write outreach emails as the sender. Your job is to sound exactly like \
them, not like a marketer or an AI assistant.

Below you are given (1) sample emails the sender actually wrote and (2) a campaign \
brief describing this outreach. Study the samples for sentence length, formality, \
greeting/sign-off habits, punctuation quirks, and vocabulary, and reproduce that \
voice faithfully.

Rules:
- Write ONE email to the lead described in the user message.
- Personalize using only the facts provided about the lead. Never invent details \
about them or their company; if there is little to go on, stay brief and generic \
rather than fabricate.
- Match the samples' length and register. Do not add flourishes the sender \
wouldn't use.
- No placeholder brackets like [Name] — write the finished email, ready to send.
- The subject line should be short and look human-written, not like a newsletter.
"""

EMAIL_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["subject", "body"],
        "additionalProperties": False,
    },
}


def read_required(path: Path, hint: str) -> str:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        sys.exit(f"error: {path.name} is missing or empty. {hint}")
    return path.read_text(encoding="utf-8")


def load_leads(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = [
            {k.strip(): (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(f)
        ]
    leads = [r for r in rows if r.get("email")]
    skipped = len(rows) - len(leads)
    if skipped:
        print(f"warning: skipped {skipped} row(s) with no 'email' value")
    if not leads:
        sys.exit("error: no usable rows — the CSV needs an 'email' column")
    return leads


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "lead"


def cmd_generate(args):
    tone = read_required(
        ROOT / "tone_samples.md",
        "Paste a few emails you've written into it so I can learn your voice.",
    )
    if "(paste an email you wrote here)" in tone:
        sys.exit(
            "error: tone_samples.md still has placeholder text — replace it with "
            "real emails you've written before generating."
        )
    campaign = read_required(
        ROOT / "campaign.md",
        "Describe what this outreach is about: what you're offering and the ask.",
    )
    leads = load_leads(Path(args.leads))
    if args.limit:
        leads = leads[: args.limit]

    OUTBOX.mkdir(exist_ok=True)
    # Identity-linked API keys require the workspace id on every request.
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None
    )

    # Stable system prefix, cached across leads — only the lead info varies.
    system = [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"# Sender's sample emails\n\n{tone}\n\n# Campaign brief\n\n{campaign}",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    generated = 0
    for i, lead in enumerate(leads, 1):
        out_path = OUTBOX / f"{i:03d}_{slugify(lead['email'])}.md"
        if out_path.exists() and not args.overwrite:
            print(f"[{i}/{len(leads)}] {lead['email']} — draft exists, skipping")
            continue

        lead_desc = "\n".join(f"{k}: {v}" for k, v in lead.items() if v)
        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=4096,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                output_config={"format": EMAIL_SCHEMA},
                messages=[
                    {
                        "role": "user",
                        "content": f"Write the outreach email for this lead:\n\n{lead_desc}",
                    }
                ],
            )
        except anthropic.AuthenticationError:
            sys.exit(
                "error: no valid Claude API credentials. Set ANTHROPIC_API_KEY "
                "in .env, or run `ant auth login`."
            )
        except anthropic.APIError as e:
            print(f"[{i}/{len(leads)}] {lead['email']} — API error, skipping: {e}")
            continue

        if response.stop_reason == "refusal":
            print(f"[{i}/{len(leads)}] {lead['email']} — model declined, skipping")
            continue

        draft = json.loads(
            next(b.text for b in response.content if b.type == "text")
        )
        out_path.write_text(
            f"to: {lead['email']}\n"
            f"subject: {draft['subject']}\n"
            f"---\n"
            f"{draft['body'].strip()}\n",
            encoding="utf-8",
        )
        generated += 1
        print(f"[{i}/{len(leads)}] {lead['email']} — {draft['subject']}")

    print(f"\n{generated} draft(s) written to {OUTBOX}\\")
    print("Review/edit them, then run: python emails.py send")


def parse_draft(path: Path):
    text = path.read_text(encoding="utf-8")
    header, sep, body = text.partition("\n---\n")
    fields = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    if not sep or not fields.get("to") or not fields.get("subject"):
        return None
    return fields["to"], fields["subject"], body.strip()


def cmd_send(args):
    load_dotenv(ROOT / ".env")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        sys.exit(
            "error: set SMTP_USER and SMTP_PASSWORD in .env (see .env.example).\n"
            "For Gmail, create an app password: https://myaccount.google.com/apppasswords"
        )
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    from_name = os.environ.get("FROM_NAME", "")

    drafts = []
    for path in sorted(OUTBOX.glob("*.md")) if OUTBOX.exists() else []:
        parsed = parse_draft(path)
        if parsed:
            drafts.append((path, *parsed))
        else:
            print(f"warning: {path.name} is malformed (needs to:/subject:/--- header), skipping")
    if not drafts:
        sys.exit("Nothing to send — outbox/ is empty. Run generate first.")

    print(f"About to send {len(drafts)} email(s) from {user}:")
    for _, to, subject, _ in drafts:
        print(f"  {to}  |  {subject}")
    if not args.yes:
        if input("\nType 'send' to confirm: ").strip().lower() != "send":
            sys.exit("Aborted, nothing sent.")

    SENT.mkdir(exist_ok=True)
    sent_count = 0
    with smtplib.SMTP_SSL(host, port) as smtp:
        smtp.login(user, password)
        for path, to, subject, body in drafts:
            msg = EmailMessage()
            msg["From"] = f"{from_name} <{user}>" if from_name else user
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            try:
                smtp.send_message(msg)
            except smtplib.SMTPException as e:
                print(f"  FAILED {to}: {e}")
                continue
            path.rename(SENT / path.name)
            sent_count += 1
            print(f"  sent {to}")
            time.sleep(args.delay)

    print(f"\n{sent_count}/{len(drafts)} sent. Sent drafts moved to sent\\")


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="draft emails for each lead into outbox/")
    gen.add_argument("leads", help="path to leads CSV (must have an 'email' column)")
    gen.add_argument("--limit", type=int, help="only draft the first N leads (for testing)")
    gen.add_argument("--overwrite", action="store_true", help="re-draft leads that already have a draft")
    gen.set_defaults(func=cmd_generate)

    snd = sub.add_parser("send", help="send everything in outbox/ via SMTP")
    snd.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    snd.add_argument("--delay", type=float, default=3.0, help="seconds between sends (default 3)")
    snd.set_defaults(func=cmd_send)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
