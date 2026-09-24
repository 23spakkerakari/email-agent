#!/usr/bin/env python
"""Draft follow-ups for people who haven't replied.

  python followup.py                     -> 1st follow-up for everyone in sent/
  python followup.py --stage 2           -> 2nd follow-up for everyone in followup1/
  python followup.py --stage 2 --message second.txt   (custom text, use {name} for the name)
  python emails.py send   /  python emails_resend.py send

Each stage reads the previous stage's folder (sent/ -> followup1/ -> followup2/ ...)
and writes <original>_followup<N>.md drafts into outbox/. When a follow-up is sent,
the send command moves that person's whole thread into followup<N>/, so a folder
only ever contains people who have received exactly that many follow-ups and
nobody gets the same follow-up twice.

The follow-up is addressed by the name in the original's greeting ("Hey Jeff,"),
sent as "Re: <original subject>" with the original quoted underneath.
"""

import argparse
import sys
from pathlib import Path

from emails import (
    FOLLOWUP_RE,
    OUTBOX,
    greeting_name,
    parse_draft,
    recipients_in,
    stage_dir,
    thread_files,
)

DEFAULT_MESSAGE = """\
Hey {name},

I hope you're doing well! I wanted to follow up from my request above to see if \
you were able to chat. To clarify, I'm not trying to sell you anything! I'm a college student looking 
to learn more about hiring at companies like yours, and would appreciate any time you have to spare.
Thank you,
Pradhi
"""

def outbox_index():
    """{email: {stems}} and {name: {stems}} for drafts waiting in outbox/."""
    emails, names = {}, {}
    for path in sorted(OUTBOX.glob("*.md")) if OUTBOX.exists() else []:
        parsed = parse_draft(path)
        if not parsed:
            continue
        emails.setdefault(parsed[0].lower(), set()).add(path.stem)
        name = greeting_name(parsed[2])
        if name:
            names.setdefault(name.strip().lower(), set()).add(path.stem)
    return emails, names


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", type=int, default=1, help="which follow-up this is (default 1)")
    parser.add_argument("--message", help="text file with the follow-up body; use {name} for the recipient's name")
    parser.add_argument("--limit", type=int, help="only draft the first N follow-ups")
    parser.add_argument("--overwrite", action="store_true", help="re-draft follow-ups already waiting in outbox/")
    parser.add_argument("--ignore-names", action="store_true", help="cross-check on email only, not on names")
    args = parser.parse_args()
    if args.stage < 1:
        sys.exit("error: --stage must be 1 or higher")

    message = DEFAULT_MESSAGE
    if args.message:
        message = Path(args.message).read_text(encoding="utf-8")
        if "{name}" not in message:
            print("warning: --message has no {name} placeholder, so it won't be personalized")
    elif args.stage > 1:
        print(f"note: using the built-in follow-up text for stage {args.stage}; pass --message to change it\n")

    source = stage_dir(args.stage - 1)
    originals = [p for p in sorted(source.glob("*.md")) if not FOLLOWUP_RE.match(p.stem)] if source.exists() else []
    if not originals:
        sys.exit(f"Nothing in {source.name}/ to follow up on.")
    OUTBOX.mkdir(exist_ok=True)

    # Cross-check: who already received this stage, and who already has a draft
    # sitting in outbox/ (by email and by greeting name).
    done_emails, done_names = recipients_in([stage_dir(args.stage)])
    pending_emails, pending_names = outbox_index()

    drafted = skipped = 0
    for path in originals:
        stem = f"{path.stem}_followup{args.stage}"
        out_path = OUTBOX / f"{stem}.md"
        already_sent = any(f.stem == stem for f in thread_files(path.stem))
        if already_sent or (out_path.exists() and not args.overwrite):
            skipped += 1
            continue
        if args.limit and drafted >= args.limit:
            break

        parsed = parse_draft(path)
        if not parsed:
            print(f"warning: {path.name} is malformed, skipping")
            continue
        to, subject, body = parsed
        name = greeting_name(body)
        if not name:
            print(f"warning: {path.name} has no 'Hey <name>,' greeting, skipping")
            continue

        key, name_key = to.lower(), name.strip().lower()
        if key in done_emails:
            skipped += 1
            continue
        clash = pending_emails.get(key, set()) - {stem}
        if clash:
            print(f"skip {to} — outbox/ already has {sorted(clash)[0]}.md")
            skipped += 1
            continue
        # First-name overlap is too weak to skip on; just flag it.
        if not args.ignore_names and (
            name_key in done_names or pending_names.get(name_key, set()) - {stem}
        ):
            print(f"note: '{name}' also appears elsewhere (different address)")

        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
        out_path.write_text(
            f"to: {to}\n"
            f"subject: {subject}\n"
            f"---\n"
            f"{message.format(name=name).rstrip()}\n\n"
            f"{quoted}\n",
            encoding="utf-8",
        )
        pending_emails.setdefault(key, set()).add(stem)
        pending_names.setdefault(name_key, set()).add(stem)
        drafted += 1
        print(f"{to} — {name}")

    print(f"\n{drafted} stage-{args.stage} follow-up draft(s) written to {OUTBOX}\ ({skipped} already drafted or sent)")
    if drafted:
        print("Review them, then run: python emails.py send   (or emails_resend.py send)")


if __name__ == "__main__":
    main()
