#!/usr/bin/env python
"""Draft follow-ups for people who haven't replied, up to MAX_FOLLOWUPS per person.

  python followup.py --status            -> how many people are at each stage
  python followup.py                     -> 1st follow-up for everyone in sent/
  python followup.py --stage 2           -> 2nd follow-up for everyone in followup1/
  python followup.py --stage 5           -> 5th (last) follow-up for everyone in followup4/
  python followup.py --stage 2 --message second.txt   (one-off text, use {name} for the name)
  python emails.py send   /  python emails_resend.py send

The text for each stage lives in followups.md under "## Follow-up N"; edit it there.

Each stage reads the previous stage's folder (sent/ -> followup1/ -> ... -> followup5/)
and writes <original>_followup<N>.md drafts into outbox/. When a follow-up is sent,
the send command moves that person's whole thread into followup<N>/, so a folder
only ever contains people who have received exactly that many follow-ups and
nobody gets the same follow-up twice. Nobody in followup5/ is ever drafted again.

The follow-up is addressed by the name in the original's greeting ("Hey Jeff,"),
sent as "Re: <original subject>" with the latest message in the thread quoted
underneath (which itself quotes the one before, like a normal reply chain).
"""

import argparse
import re
import sys
from pathlib import Path

from emails import (
    FOLLOWUP_RE,
    OUTBOX,
    ROOT,
    greeting_name,
    parse_draft,
    recipients_in,
    stage_dir,
    thread_files,
)

# How many follow-ups one person can receive after the original email.
MAX_FOLLOWUPS = 5

MESSAGES_FILE = ROOT / "followups.md"
STAGE_HEADING = re.compile(r"^##\s*Follow-up\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def load_messages(path: Path = MESSAGES_FILE) -> dict[int, str]:
    """{stage: body} from followups.md, one '## Follow-up N' section per stage."""
    if not path.exists():
        sys.exit(f"error: {path.name} is missing. It holds the text for each follow-up stage.")
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    parts = STAGE_HEADING.split(text)  # [preamble, "1", body1, "2", body2, ...]
    messages = {}
    for num, body in zip(parts[1::2], parts[2::2]):
        if body.strip():
            messages[int(num)] = body.strip()
    return messages


def people_in(folder: Path) -> list[Path]:
    """Original drafts in a stage folder, one per recipient (follow-up files excluded)."""
    if not folder.exists():
        return []
    return [p for p in sorted(folder.glob("*.md")) if not FOLLOWUP_RE.match(p.stem)]


def print_status():
    print("People who haven't replied, by how many follow-ups they've had:\n")
    for n in range(MAX_FOLLOWUPS + 1):
        folder = stage_dir(n)
        count = len(people_in(folder))
        if n == MAX_FOLLOWUPS:
            nxt = "done, no more follow-ups"
        else:
            nxt = f"next: python followup.py --stage {n + 1}"
        print(f"  {folder.name + '/':<12}{count:>5}   {nxt}")

    pending = {}
    for path in sorted(OUTBOX.glob("*.md")) if OUTBOX.exists() else []:
        m = FOLLOWUP_RE.match(path.stem)
        if m:
            pending[int(m.group(2))] = pending.get(int(m.group(2)), 0) + 1
    if pending:
        summary = ", ".join(f"{c} for stage {s}" for s, c in sorted(pending.items()))
        print(f"\nFollow-up drafts waiting in outbox/: {summary}")
    else:
        print("\nNo follow-up drafts waiting in outbox/.")


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
    parser.add_argument("--stage", type=int, default=1,
                        help=f"which follow-up this is, 1-{MAX_FOLLOWUPS} (default 1)")
    parser.add_argument("--message", help="text file with a one-off follow-up body; use {name} for the recipient's name")
    parser.add_argument("--limit", type=int, help="only draft the first N follow-ups")
    parser.add_argument("--overwrite", action="store_true", help="re-draft follow-ups already waiting in outbox/")
    parser.add_argument("--ignore-names", action="store_true", help="cross-check on email only, not on names")
    parser.add_argument("--status", action="store_true", help="show how many people are at each stage and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
        return
    if not 1 <= args.stage <= MAX_FOLLOWUPS:
        sys.exit(f"error: --stage must be between 1 and {MAX_FOLLOWUPS} (each person gets at most {MAX_FOLLOWUPS} follow-ups)")

    if args.message:
        message = Path(args.message).read_text(encoding="utf-8")
    else:
        message = load_messages().get(args.stage)
        if not message:
            sys.exit(
                f"error: {MESSAGES_FILE.name} has no '## Follow-up {args.stage}' section. "
                f"Add one there, or pass --message <file>."
            )
    if "{name}" not in message:
        print("warning: the follow-up text has no {name} placeholder, so it won't be personalized")

    source = stage_dir(args.stage - 1)
    originals = people_in(source)
    if not originals:
        sys.exit(f"Nothing in {source.name}/ to follow up on. Try: python followup.py --status")
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

        # Quote the most recent message in the thread: the previous follow-up if
        # there is one (it already carries the earlier quotes), else the original.
        quote_body = body
        if args.stage > 1:
            prev = source / f"{path.stem}_followup{args.stage - 1}.md"
            prev_parsed = parse_draft(prev) if prev.exists() else None
            if prev_parsed:
                quote_body = prev_parsed[2]
            else:
                print(f"note: {prev.name} not found, quoting the original instead")

        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        quoted = "\n".join(f"> {line}" if line else ">" for line in quote_body.splitlines())
        out_path.write_text(
            f"to: {to}\n"
            f"subject: {subject}\n"
            f"---\n"
            f"{message.replace('{name}', name).rstrip()}\n\n"
            f"{quoted}\n",
            encoding="utf-8",
        )
        pending_emails.setdefault(key, set()).add(stem)
        pending_names.setdefault(name_key, set()).add(stem)
        drafted += 1
        print(f"{to} — {name}")

    print(f"\n{drafted} stage-{args.stage} follow-up draft(s) written to {OUTBOX}\\ ({skipped} already drafted or sent)")
    if drafted:
        print("Review them, then run: python emails.py send   (or emails_resend.py send)")
        if args.stage == MAX_FOLLOWUPS:
            print(f"This is follow-up {MAX_FOLLOWUPS}, the last one: once sent, these people won't be followed up again.")


if __name__ == "__main__":
    main()
