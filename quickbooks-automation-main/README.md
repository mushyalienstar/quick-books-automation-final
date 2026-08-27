# quickbooks-automation

PDF vendor bill → QuickBooks Desktop bill entry, for **any** vendor's PDF.

## Bill entry tool (current)

Run inside the VM with QuickBooks Desktop open on the company file:

```
python app\bill_entry_gui.py                    # then Open PDF…
python app\bill_entry_gui.py path\to\bill.pdf   # open a specific PDF
python app\bill_entry_gui.py path\to\inbox      # open the first PDF in a folder
```

(or double-click `app\run_bill_entry.bat`)

How it works:

- **Vendor-agnostic parsing** (`bill_parser.py`) — best-effort extraction of
  ref number, invoice date, total, tax, and candidate charge lines from any
  bill PDF. Nothing is vendor-specific and parsing never rejects a bill.
- **Live QuickBooks lists** (`qb_client.py`) — Vendor, Item, Account and Tax
  Code are dropdowns populated by querying the *open company file* at runtime
  (VendorQuery / ItemQuery / AccountQuery / SalesTaxCodeQuery). Nothing about
  the company file is hardcoded, so the tool works unchanged whether the file
  has one province's tax codes or all of them.
- **Amounts auto-populate** — the pre-tax total pre-fills the first line, and
  every amount detected in the PDF is listed on the right; double-click one to
  add it as a line. Amounts stay editable.
- **No hard rejection** — anything the parser can't figure out, you pick from
  a dropdown. The only dialogs are confirmations (possible duplicate ref
  number, final "enter this bill?" summary); none of them block you.
- **No reference-number / customer:job matching** — line items are never
  matched to jobs via shipment refs or project codes.
- Last-used Item/Account/Tax per vendor is remembered
  (`vendor_defaults.json`) and pre-selected next time.

Setup inside the VM is unchanged from `VM_SETUP.md`: Python + `pip install
pypdf pywin32`; the `QBXMLRP2` COM component ships with QuickBooks Desktop.
First run pops the QuickBooks access-authorization dialog once.

## Legacy (superseded)

`app/fedex_bill.py`, `app/fedex_inbox.py`, `app/fedex/parser.py` — the old
headless FedEx-only pipeline. It validated against hardcoded names
("5230 Courier", per-province tax codes) and matched shipment reference
numbers to customer:jobs, rejecting any bill that didn't line up. Kept for
reference; use `bill_entry_gui.py` instead.
