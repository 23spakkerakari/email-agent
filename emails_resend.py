#!/usr/bin/env python
"""Same outreach workflow as emails.py, but sends through Resend instead of SMTP.

Workflow:
  1. Paste 3-5 emails you've written into tone_samples.md
  2. Describe the outreach in campaign.md
  3. python emails_resend.py generate leads.csv   -> drafts land in outbox/ for review
  4. Edit/delete any drafts you don't like
  5. python emails_resend.py send                 -> sends outbox/ drafts via Resend

Drafting is shared with emails.py; only the send step differs. Needs in .env:
  RESEND_API_KEY   from https://resend.com/api-keys
  RESEND_FROM      an address on a domain you've verified in Resend,
                   e.g. "Pradhi <pradhi@yourdomain.com>"
  RESEND_REPLY_TO  optional, where replies should go (e.g. your Gmail)
"""

import argparse
import os
import sys
import time

import resend
from dotenv import load_dotenv

from emails import OUTBOX, ROOT, cmd_generate, parse_draft, record_sent, screen_drafts


def load_drafts():
    drafts = []
    for path in sorted(OUTBOX.glob("*.md")) if OUTBOX.exists() else []:
        parsed = parse_draft(path)
        if parsed:
            drafts.append((path, *parsed))
        else:
            print(f"warning: {path.name} is malformed (needs to:/subject:/--- header), skipping")
    return drafts


def cmd_send(args):
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_FROM")
    if not api_key or not sender:
        sys.exit(
            "error: set RESEND_API_KEY and RESEND_FROM in .env (see .env.example).\n"
            "RESEND_FROM must use a domain you've verified at https://resend.com/domains"
        )
    reply_to = os.environ.get("RESEND_REPLY_TO") or None
    resend.api_key = api_key

    drafts = load_drafts()
    if not drafts:
        sys.exit("Nothing to send — outbox/ is empty. Run generate first.")

    drafts = screen_drafts(drafts, args)
    if not drafts:
        sys.exit("Nothing left to send after the duplicate cross-check.")

    print(f"About to send {len(drafts)} email(s) from {sender}" + (f" (replies to {reply_to})" if reply_to else "") + ":")
    for _, to, subject, _ in drafts:
        print(f"  {to}  |  {subject}")
    if not args.yes:
        if input("\nType 'send' to confirm: ").strip().lower() != "send":
            sys.exit("Aborted, nothing sent.")

    sent_count = 0
    for path, to, subject, body in drafts:
        params: resend.Emails.SendParams = {
            "from": sender,
            "to": [to],
            "subject": subject,
            "text": body,
        }
        if reply_to:
            params["reply_to"] = reply_to
        try:
            result = resend.Emails.send(params)
        except resend.exceptions.ResendError as e:
            print(f"  FAILED {to}: {e}")
            continue
        record_sent(path)
        sent_count += 1
        print(f"  sent {to}  (id {result.get('id', '?')})")
        # Resend's default rate limit is 2 requests/second.
        time.sleep(args.delay)

    print(f"\n{sent_count}/{len(drafts)} sent. Sent drafts moved to sent\\")


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="draft emails for each lead into outbox/")
    gen.add_argument("leads", help="path to leads CSV (must have an 'Email' column)")
    gen.add_argument("--limit", type=int, help="only draft the first N leads (for testing)")
    gen.add_argument("--overwrite", action="store_true", help="re-draft leads that already have a draft")
    gen.add_argument("--ignore-names", action="store_true", help="cross-check on email only, not on names")
    gen.set_defaults(func=cmd_generate)

    snd = sub.add_parser("send", help="send everything in outbox/ via Resend")
    snd.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    snd.add_argument("--delay", type=float, default=1.0, help="seconds between sends (default 1)")
    snd.add_argument("--ignore-names", action="store_true", help="cross-check on email only, not on names")
    snd.add_argument("--allow-duplicates", action="store_true", help="send even if the recipient was already mailed")
    snd.set_defaults(func=cmd_send)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
