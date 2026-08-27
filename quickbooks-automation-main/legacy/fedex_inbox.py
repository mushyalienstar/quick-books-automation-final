"""Folder-based runner — runs INSIDE the VM where QuickBooks Desktop lives.

Drop FedEx invoice PDFs into <base>\\inbox (from the host via OneDrive or RDP
drive redirection). Each PDF is parsed, validated against the QuickBooks
lists, and entered as a bill. The PDF then moves to <base>\\done (bill
created) or <base>\\flagged (problems found), with a .report.txt beside it
explaining exactly what happened. Flagged invoices are never partially
entered — fix the flag in QuickBooks and re-drop the PDF into inbox.

Usage (inside the VM, QuickBooks open with the company file):
    python fedex_inbox.py                 # process the inbox once
    python fedex_inbox.py --watch 30      # keep watching, check every 30s
    python fedex_inbox.py --force         # enter bills despite flags
"""
import argparse
import shutil
import time
import traceback
from pathlib import Path

from fedex_bill import QB_AVAILABLE, QuickBooks, process_pdf

DEFAULT_BASE = Path(r"C:\Users\finance.automation\Documents\FedExBills")
SETTLE_SECONDS = 10  # skip files still being copied/synced into the inbox


def process_folder(base: Path, force: bool) -> int:
    inbox, done, flagged = base / "inbox", base / "done", base / "flagged"
    for folder in (inbox, done, flagged):
        folder.mkdir(parents=True, exist_ok=True)

    pdfs = [p for p in sorted(inbox.glob("*.pdf"))
            if time.time() - p.stat().st_mtime > SETTLE_SECONDS]
    if not pdfs:
        return 0

    qb = QuickBooks()
    info = qb.host_info()
    if info:
        print(f"Connected to {info}")
    try:
        for pdf in pdfs:
            print(f"\nProcessing {pdf.name} ...")
            try:
                result = process_pdf(pdf, qb, force=force)
                sent = result["sent"]
                report = "\n".join(
                    [result["preview"], ""]
                    + [f"FLAG: {f}" for f in result["flags"]]
                    + ["", result["message"]])
            except Exception:
                sent = False
                report = f"ERROR while processing {pdf.name}:\n{traceback.format_exc()}"
            dest = done if sent else flagged
            (dest / (pdf.stem + ".report.txt")).write_text(report, encoding="utf-8")
            shutil.move(str(pdf), str(dest / pdf.name))
            print(report)
            print(f"-> moved to {dest / pdf.name}")
    finally:
        qb.close()
    return len(pdfs)


def main():
    parser = argparse.ArgumentParser(
        description="Watch a folder for FedEx PDFs and enter them as QuickBooks bills.")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE,
                        help=f"folder holding inbox/done/flagged (default {DEFAULT_BASE})")
    parser.add_argument("--watch", nargs="?", const=30, type=int, metavar="SECONDS",
                        help="keep running, checking the inbox every N seconds (default 30)")
    parser.add_argument("--force", action="store_true",
                        help="enter bills even when validation flags are raised")
    args = parser.parse_args()

    if not QB_AVAILABLE:
        raise SystemExit("pywin32 is not installed — run `pip install pywin32` "
                         "on the machine where QuickBooks Desktop is installed.")

    if args.watch:
        print(f"Watching {args.base / 'inbox'} every {args.watch}s — Ctrl+C to stop.")
        while True:
            try:
                process_folder(args.base, args.force)
            except Exception as e:
                # QuickBooks closed, company file locked, etc. — report and retry.
                print(f"ERROR: {e}")
            time.sleep(args.watch)
    else:
        count = process_folder(args.base, args.force)
        print(f"\n{count} PDF(s) processed." if count else "Inbox empty — nothing to do.")


if __name__ == "__main__":
    main()
