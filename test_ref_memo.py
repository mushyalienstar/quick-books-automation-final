"""Before/after check: what Ref No. / Memo the parser produces per inbox PDF.

Run:  python test_ref_memo.py
"""
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "quickbooks-automation-main", "app"))
from bill_parser import parse_bill  # noqa: E402

for path in sorted(glob.glob(os.path.join("Bills", "inbox", "**", "*.pdf"),
                             recursive=True)):
    name = os.path.basename(path)
    try:
        r = parse_bill(path)
    except Exception as e:
        print(f"{name}\n  ERROR: {e}\n")
        continue
    print(name)
    print(f"  doc_type        = {r['doc_type']}")
    print(f"  invoice_number  = {r['invoice_number']!r}")
    print(f"  po_number       = {r['po_number']!r}")
    print(f"  project_number  = {r.get('project_number', '<not implemented>')!r}")
    print(f"  memo            = {r.get('memo', '<not implemented>')!r}")
    print()

# ---- qbXML check (no QuickBooks needed): the generated BillAdd must carry
# the extracted Ref No. and Memo verbatim. Uses the Fekete reference bill.
from bill_parser import build_memo  # noqa: E402
from qb_client import build_bill_add_qbxml  # noqa: E402

r = parse_bill(os.path.join("Bills", "inbox", "Sample Bills",
                            "INVOICE_23117-B3_from_Thomas A_ Fekete Limited.pdf"))
xml = build_bill_add_qbxml(
    "Thomas A. Fekete Limited", r["invoice_number"], r["invoice_date"],
    [{"kind": "item", "name": "Eng. Fees", "amount": r["pre_tax_total"],
      "tax_code": "G"}],
    memo=r["memo"])
assert "<RefNumber>23117-B3</RefNumber>" in xml, xml
assert "<Memo>Project No. 2307116 PO KPA-23-1722</Memo>" in xml, xml
assert r["memo"] == build_memo(r["project_number"], r["po_number"])

# Fallback rules: never a dangling label.
assert build_memo("123", "PO9") == "Project No. 123 PO PO9"
assert build_memo("123", "") == "Project No. 123"
assert build_memo("", "KPA-1") == "PO KPA-1"
assert build_memo("", "") == ""
print("qbXML check: RefNumber + Memo present in BillAdd — OK")

