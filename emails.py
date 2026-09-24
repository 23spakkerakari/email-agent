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

# Follow-up drafts are named <original>_followup<N>.md. When one is sent, the
# whole thread (original + every follow-up) moves into followup<N>/, so sent/
# only ever holds people who have received just the original.
FOLLOWUP_RE = re.compile(r"^(.+)_followup(\d+)$")


def stage_dir(n: int) -> Path:
    return SENT if n == 0 else ROOT / f"followup{n}"


def thread_dirs() -> list[Path]:
    """Every folder holding already-sent mail: sent/, followup1/, followup2/, ..."""
    return [d for d in [SENT, *ROOT.glob("followup[0-9]*")] if d.is_dir()]


def thread_files(base: str) -> list[Path]:
    """All files belonging to one recipient's thread, wherever they currently live."""
    found = []
    for d in thread_dirs():
        found.extend(f for f in [d / f"{base}.md", *d.glob(f"{base}_followup*.md")] if f.exists())
    return found


def record_sent(path: Path) -> Path:
    """Move a just-sent draft out of outbox/ into the right archive folder."""
    m = FOLLOWUP_RE.match(path.stem)
    dest = stage_dir(int(m.group(2))) if m else SENT
    dest.mkdir(exist_ok=True)
    if m:
        for f in thread_files(m.group(1)):
            if f.parent != dest:
                f.rename(dest / f.name)
    return path.rename(dest / path.name)

MODEL = "claude-opus-5"

# Server-side refusal fallback is only supported on Opus 5 / Fable models;
# Sonnet and older models reject the parameter with a 400.
FALLBACK_KWARGS = (
    {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    if MODEL.startswith(("claude-opus-5", "claude-fable"))
    else {}
)

SYSTEM_INSTRUCTIONS = """\
You ghost-write outreach emails as the sender. Your job is to sound exactly like \
them, not like a marketer or an AI assistant.

Below you are given (1) sample emails the sender actually wrote and (2) a campaign \
brief describing this outreach. Study the samples for sentence length, formality, \
greeting/sign-off habits, punctuation quirks, and vocabulary, and reproduce that \
voice faithfully.

Rules:
- Write ONE email to the lead described in the user message.
- Frame the email around the ask and value proposition earlier in the emails
- Personalize using only the facts provided about the lead. Never invent details \
about them or their company; if there is little to go on, stay brief and generic \
rather than fabricate.
- Max 80 words; keep it short, to the point, and polite. Do not add flourishes the sender \
wouldn't use.
- Specify that you're only trying to gain some advice from the lead, not sell them anything.
- Never include the phrase 'this email reaches me directly' or "reply here"
- Include the phrase "I know your time is valuable, so I'm more than happy to adjust to your schedule"
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
    leads = [r for r in rows if r.get("Email")]
    skipped = len(rows) - len(leads)
    if skipped:
        print(f"warning: skipped {skipped} row(s) with no 'Email' value")
    if not leads:
        sys.exit("error: no usable rows — the CSV needs an 'mail' column")
    return leads


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "lead"


GREETING = re.compile(r"^(?:Hey|Hi|Hello)\s+(.+?),\s*$")


def greeting_name(body: str):
    """The name a draft opens with: 'Hey Jeff,' -> 'Jeff'."""
    for line in body.splitlines():
        if line.strip():
            m = GREETING.match(line.strip())
            return m.group(1) if m else None
    return None


def recipients_in(folders) -> tuple[set[str], set[str]]:
    """(emails, greeting names) of every parseable draft in the given folders."""
    emails, names = set(), set()
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.glob("*.md"):
            parsed = parse_draft(path)
            if not parsed:
                continue
            emails.add(parsed[0].lower())
            name = greeting_name(parsed[2])
            if name:
                names.add(name.strip().lower())
    return emails, names


def lead_names(lead: dict) -> set[str]:
    """Name spellings from a CSV row that could match a draft's greeting."""
    return {
        lead[k].strip().lower()
        for k in ("First name", "Full name", "Name")
        if lead.get(k, "").strip()
    }


def confirm_duplicate(reason: str) -> bool:
    answer = input(f"  {reason} Draft it again anyway? [y/N]: ")
    return answer.strip().lower() in ("y", "yes")


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

    # Cross-check every lead against people already mailed (sent/, followup*/)
    # and against drafts still waiting in outbox/, by email and by greeting name.
    mailed, mailed_names = recipients_in(thread_dirs())
    drafted, drafted_names = recipients_in([OUTBOX])
    seen_this_run: set[str] = set()

    generated = 0
    for i, lead in enumerate(leads, 1):
        out_path = OUTBOX / f"{i:03d}_{slugify(lead['Email'])}.md"
        if out_path.exists() and not args.overwrite:
            print(f"[{i}/{len(leads)}] {lead['Email']} — draft exists, skipping")
            continue

        email = lead["Email"].lower()
        if email in seen_this_run:
            print(f"[{i}/{len(leads)}] {lead['Email']} — duplicate row in this CSV, skipping")
            continue

        if email in mailed:
            clash = f"You already emailed {lead['Email']}."
        elif email in drafted:
            clash = f"{lead['Email']} already has a draft waiting in outbox/."
        else:
            clash = None
        if clash and not confirm_duplicate(clash):
            print(f"[{i}/{len(leads)}] {lead['Email']} — skipped (duplicate)")
            continue

        # Greetings carry first names only, so a name match is a weak signal —
        # worth surfacing, not worth blocking a different address over.
        overlap = set() if args.ignore_names else lead_names(lead) & (mailed_names | drafted_names)
        if overlap:
            print(f"  note: '{sorted(overlap)[0]}' already appears in an existing draft (different address)")
        seen_this_run.add(email)

        lead_desc = "\n".join(f"{k}: {v}" for k, v in lead.items() if v)
        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=4096,
                **FALLBACK_KWARGS,
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
            print(f"[{i}/{len(leads)}] {lead['Email']} — API error, skipping: {e}")
            continue

        if response.stop_reason == "refusal":
            print(f"[{i}/{len(leads)}] {lead['Email']} — model declined, skipping")
            continue

        draft = json.loads(
            next(b.text for b in response.content if b.type == "text")
        )
        out_path.write_text(
            f"to: {lead['Email']}\n"
            f"subject: {draft['subject']}\n"
            f"---\n"
            f"{draft['body'].strip()}\n",
            encoding="utf-8",
        )
        generated += 1
        print(f"[{i}/{len(leads)}] {lead['Email']} — {draft['subject']}")

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


def screen_drafts(drafts, args):
    """Hold back originals aimed at someone already mailed (sent/, followup*/).

    Follow-up drafts are exempt — reaching a prior recipient is their whole job.
    """
    mailed, mailed_names = recipients_in(thread_dirs())
    kept, held, notes = [], [], []
    for entry in drafts:
        path, to, _subject, body = entry
        if FOLLOWUP_RE.match(path.stem):
            kept.append(entry)
            continue
        if to.lower() in mailed:
            held.append((entry, "already emailed"))
            continue
        name = (greeting_name(body) or "").strip().lower()
        if name and not args.ignore_names and name in mailed_names:
            notes.append((to, name))
        kept.append(entry)

    if notes:
        print(f"Cross-check: {len(notes)} draft(s) share a first name with someone already mailed —")
        print("different address, so these are still going out:")
        for to, name in notes[:10]:
            print(f"  {to}  — '{name}'")
        if len(notes) > 10:
            print(f"  ...and {len(notes) - 10} more")
        print()
    if held:
        verb = "sending anyway" if args.allow_duplicates else "holding"
        print(f"Cross-check: {len(held)} draft(s) match someone already mailed ({verb}):")
        for (_, to, subject, _), reason in held:
            print(f"  {to}  |  {subject}  — {reason}")
        print()
        if args.allow_duplicates:
            kept.extend(entry for entry, _ in held)
    return kept


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

    drafts = screen_drafts(drafts, args)
    if not drafts:
        sys.exit("Nothing left to send after the duplicate cross-check.")

    print(f"About to send {len(drafts)} email(s) from {user}:")
    for _, to, subject, _ in drafts:
        print(f"  {to}  |  {subject}")
    if not args.yes:
        if input("\nType 'send' to confirm: ").strip().lower() != "send":
            sys.exit("Aborted, nothing sent.")

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
            record_sent(path)
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
    gen.add_argument("--ignore-names", action="store_true", help="cross-check on email only, not on names")
    gen.set_defaults(func=cmd_generate)

    snd = sub.add_parser("send", help="send everything in outbox/ via SMTP")
    snd.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    snd.add_argument("--delay", type=float, default=3.0, help="seconds between sends (default 3)")
    snd.add_argument("--ignore-names", action="store_true", help="cross-check on email only, not on names")
    snd.add_argument("--allow-duplicates", action="store_true", help="send even if the recipient was already mailed")
    snd.set_defaults(func=cmd_send)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
