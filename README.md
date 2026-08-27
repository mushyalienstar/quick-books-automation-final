# QuickBooks Automation — Start Here

Enters vendor bill PDFs into QuickBooks Desktop (Canadian edition) with purchase-order
linking, via Intuit's official qbXML interface. A human reviews every bill in a small
desktop app before anything is entered — nothing is committed to QuickBooks unseen.

**New here? Read this document:**
**`quickbooks-automation-main\README.md`** — how to run the tool and how it works.

## Folder map

```
QuickBooks Automation\
│
├── README.md                      ← this file
│
├── Bills\                         ← OneDrive-synced working folders (shared with the VM)
│   ├── inbox\                     ←   drop new bill PDFs here; sample/test bills live
│   │                              ←   in subfolders (Sample Bills, Bill Review, …)
│   ├── done\                      ←   entered bills get moved here
│   └── flagged\                   ←   bills that needed attention
│
├── quickbooks-automation-main\    ← the code
│   ├── README.md                  ←   how to run the tool
│   ├── VM_SETUP.md                ←   one-time VM/QuickBooks setup
│   ├── requirements.txt           ←   Python dependencies (pypdf, pywin32)
│   ├── app\                       ←   THE CURRENT TOOL (5 files)
│   │   ├── bill_entry_gui.py      ←     the review screen (run this)
│   │   ├── bill_parser.py         ←     PDF → extracted fields (vendor-agnostic)
│   │   ├── qb_client.py           ←     talks to QuickBooks (qbXML over COM)
│   │   ├── run_bill_entry.bat     ←     double-click launcher (inside the VM)
│   │   └── vendor_defaults.json   ←     per-vendor last-used Item/Account memory
│   ├── legacy\                    ←   superseded FedEx-only pipeline. NOT used.
│   │                              ←   Kept for reference only — see "Legacy" in
│   │                              ←   quickbooks-automation-main\README.md.
│
└── test_*.py                      ← automated regression tests (9 suites, see below)
```

## Running the tool

The tool must run **inside the finance VM** where QuickBooks Desktop is installed
(the qbXML COM component only exists there). With QuickBooks open on the company file:
double-click `quickbooks-automation-main\app\run_bill_entry.bat`, or see
`quickbooks-automation-main\README.md` for command-line options.

On this machine (no QuickBooks) you can still run the parser and all tests.

## Running the tests

The `test_*.py` files in this folder are the regression suite — every bug found on a
real invoice has a test here so it can't come back. Run them **from this folder**:

```
python test_parse.py            # PDF parsing on the sample bills
python test_gui_smoke.py        # full GUI flow against a fake QuickBooks stub
python test_po_link.py          # PO-linking qbXML generation, penny-exact sizing
python test_bill_review.py      # multi-PO / progress-billing / warning-flag scenarios
python test_invoice_amounts.py  # invoice-line ↔ PO-line amount matching
python test_po_diagnostics.py   # "why is this PO missing?" explanations
python test_ref_memo.py         # ref-number / memo auto-fill
python test_total_vendor.py     # total detection and vendor guessing safeguards
python test_auto_open.py        # auto-open-PDF setting
```

None of them need QuickBooks — they use real sample PDFs from `Bills\inbox\` plus a
stubbed QuickBooks session.

## ⚠ Do not move or rename these

Paths are relative and shared with the VM through OneDrive; the following are
load-bearing exactly where they are:

- **`Bills\`** — the VM's launcher points at `...\QuickBooks Automation\Bills\inbox`.
- **`quickbooks-automation-main\`** and everything inside **`app\`** — the tests and
  the app's own files reference these locations by name.
- **The `test_*.py` files in this folder** — they find the code and the sample PDFs
  by relative path from here.
