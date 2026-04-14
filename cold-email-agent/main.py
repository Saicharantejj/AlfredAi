"""
main.py
-------
Orchestrates the full cold-email pipeline.
"""

import csv
import sys
import time
from pathlib import Path

import config
import tracker
import reply_listener
from email_generator import generate_email
from sender          import send_with_delay


def load_leads(csv_path: str) -> list:
    path = Path(csv_path)
    if not path.exists():
        print(f"❌  Leads file not found: {csv_path}")
        print("    Create data/leads.csv based on the example in the README.")
        sys.exit(1)

    leads = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            clean = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k is not None}
            if not clean.get("email"):
                continue
            leads.append(clean)

    print(f"📋  Loaded {len(leads)} leads from {csv_path}")
    return leads


def run(csv_path: str = None) -> None:
    csv_path = csv_path or config.LEADS_CSV

    tracker.init_db()

    leads = load_leads(csv_path)
    if not leads:
        print("⚠️  No valid leads found. Exiting.")
        return

    stop_event = reply_listener.start_listener_thread()

    sent_count   = 0
    skip_count   = 0
    failed_count = 0

    for i, lead in enumerate(leads, start=1):
        email_addr = lead["email"]
        print(f"\n[{i}/{len(leads)}] Processing: {lead.get('name', email_addr)} <{email_addr}>")

        if tracker.already_sent(email_addr):
            print(f"  ⏭  Already sent to {email_addr} — skipping.")
            skip_count += 1
            continue

        print("  🤖  Generating personalised email…")
        try:
            generated = generate_email(lead)
        except Exception as exc:
            print(f"  ❌  Generation failed: {exc}")
            failed_count += 1
            continue

        subject = generated["subject"]
        body    = generated["body"]
        print(f"  📝  Subject: {subject}")

        delay = config.DELAY_BETWEEN_EMAILS_SECONDS if i < len(leads) else 0
        ok = send_with_delay(lead, subject, body, delay=delay)
        if ok:
            sent_count += 1
        else:
            failed_count += 1

    print("\n" + "═" * 52)
    print("  CAMPAIGN COMPLETE")
    print(f"  ✅  Sent:    {sent_count}")
    print(f"  ⏭  Skipped: {skip_count}")
    print(f"  ❌  Failed:  {failed_count}")
    print("═" * 52)
    reply_listener._notify_complete(sent_count)

    print(f"\n📊  DB totals: {tracker.summary()}")
    print("\n👂  Keeping reply listener alive. Press Ctrl+C to exit.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n👋  Shutting down…")
        stop_event.set()


if __name__ == "__main__":
    custom_csv = sys.argv[1] if len(sys.argv) > 1 else None
    run(custom_csv)