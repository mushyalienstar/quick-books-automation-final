"""PO-linking regression test (FakeQB, no QuickBooks needed).

Covers: auto-match of a PO whose number appears in the PDF, linking the
bill to the PO (partial fulfillment — bill amount < PO open balance),
Customer:Job defaulting from the PO line, and the qbXML the builder emits
(LinkToTxn per line, no ItemRef, element order, nothing that closes a PO).
"""
import sys
sys.path.insert(0, r'quickbooks-automation-main\app')
from bill_entry_gui import BillEntryApp
from qb_client import build_bill_add_qbxml, po_link_sizing

# FedEx sample bill: ref 2-738-32725, pre-tax 194.30. The fake PO reuses
# that number so the "PO # in PDF" auto-match path is exercised.
PO = {
    "txn_id": "ABC-123", "vendor": "FedEx", "ref_number": "2-738-32725",
    "txn_date": "2026-07-01",
    "total": 5650.00, "subtotal": 5000.00, "open_total": 3000.00,
    "billed_total": 2000.00, "memo": "Project X materials",
    "customer_jobs": ["Acme Corp:Project X"],
    "lines": [
        {"txn_line_id": "L1", "item": "Freight", "desc": "shipping",
         "quantity": 5, "received": 2, "rate": 1000.0, "amount": 5000.0,
         "open_amount": 3000.0, "customer_job": "Acme Corp:Project X",
         "tax_code": "H"},
    ],
}


class FakeQB:
    def vendors(self):
        return ["Bell Canada", "FedEx", "Telus"]

    def items(self):
        return ["Freight", "Software"]

    def accounts(self):
        return ["5100 Travel", "5200 Office Supplies"]

    def tax_codes(self):
        return ["H", "E"]

    def customers(self):
        return ["Acme Corp", "Acme Corp:Project X"]

    def host_info(self):
        return "FakeQB (PO link test)"

    def bill_exists(self, vendor, ref):
        return False

    def open_purchase_orders(self, vendor):
        return [PO] if vendor == "FedEx" else []

    def add_bill(self, *a, **k):
        return True, "fake"

    def close(self):
        pass


app = BillEntryApp(FakeQB())
app.withdraw()
app.open_pdf(r"quickbooks-automation-main\16.99999.10021.273832725.XXXXX2947.000000.pdf")
app.update()

assert app.open_pos, "vendor's open POs should load with the PDF"
assert app.po_list.curselection() == (0,), \
    "PO whose number appears in the PDF should be preselected"
print("auto-match: PO preselected, info:", app.po_info.cget("text"))
assert "billed so far 2,000.00" in app.po_info.cget("text")
assert "open 3,000.00" in app.po_info.cget("text")

app.link_po()
app.update()
linked_rows = [r for r in app.rows if r.link]
assert len(linked_rows) == 1, f"expected 1 linked row, got {len(linked_rows)}"
assert not any(r.prefilled for r in app.rows), "auto-prefill row must be gone"
r = linked_rows[0]
print(f"linked row: kind={r.kind.get()} name={r.name.get()!r} "
      f"amount={r.amount.get()} tax={r.tax.get()} job={r.customer.get()!r}")
assert r.amount.get() == "194.30", "partial: bill pre-tax, not PO open total"
assert r.customer.get() == "Acme Corp:Project X"
assert r.tax.get() == "H"

lines = [d for d in (row.to_dict() for row in app.rows) if d]
assert lines[0]["link"] == {"txn_id": "ABC-123", "txn_line_id": "L1"}
assert lines[0]["customer"] == "Acme Corp:Project X"

xml = build_bill_add_qbxml("FedEx", "2-738-32725", "2026-07-16", lines,
                           memo="test bill")
print(xml)
assert ("<LinkToTxn><TxnID>ABC-123</TxnID><TxnLineID>L1</TxnLineID>"
        "</LinkToTxn>") in xml
# QuickBooks requires a linked line to carry ONLY the link + one sizing
# override (status 3153 "parameters conflict" otherwise, seen live).
# Item, rate, Customer:Job and tax all flow from the PO line.
assert "<Quantity>0.1943</Quantity>" in xml, \
    "quantity = amount / PO rate (194.30 / 1000)"
for banned in ("<ItemRef>", "<CustomerRef>", "<SalesTaxCodeRef>",
               "<Amount>"):
    assert banned not in xml, f"{banned} conflicts with LinkToTxn"
assert xml.index("<Quantity>") < xml.index("<LinkToTxn>")
# Partial billing must never force the PO shut.
assert "IsManuallyClosed" not in xml

# Tax is the user's manual pick on EVERY line: the dropdown stays
# editable on a linked row and the pick rides on the line dict — but it
# must NEVER appear inside the linked ItemLineAdd itself (SalesTaxCodeRef
# next to LinkToTxn = status 3210 'invalid LinkToTxn value', the WHOLE
# bill rejected — live 2026-08-07). add_bill applies the pick with a
# follow-up BillMod instead (tested below with a stubbed session).
import xml.etree.ElementTree as ET

assert not r.tax_box.instate(["disabled"]), \
    "tax dropdown must stay editable on a linked row"
r.tax.set("E")
lines_e = [d for d in (row.to_dict() for row in app.rows) if d]
assert lines_e[0]["tax_code"] == "E" and lines_e[0]["po_tax_code"] == "H"
xml_e = build_bill_add_qbxml("FedEx", "2-738-32725", "2026-07-16", lines_e)
assert "<SalesTaxCodeRef>" not in xml_e, \
    "a linked line must stay minimal even with a manual tax pick"

# Schema-level validation of the generated request (parsed, not string-
# matched): the linked ItemLineAdd holds exactly the sizing override plus
# LinkToTxn, and LinkToTxn holds exactly TxnID and TxnLineID as child
# elements whose text is the real ids — the SDK structure for linking a
# bill line to a PO line, never a formatted string.
line_adds = ET.fromstring(xml_e).findall(".//BillAdd/ItemLineAdd")
assert len(line_adds) == 1
assert [c.tag for c in line_adds[0]] == ["Quantity", "LinkToTxn"]
link_el = line_adds[0].find("LinkToTxn")
assert [c.tag for c in link_el] == ["TxnID", "TxnLineID"]
assert link_el.findtext("TxnID") == "ABC-123"
assert link_el.findtext("TxnLineID") == "L1"
assert not (link_el.text or "").strip(), \
    "LinkToTxn itself must carry no text, only child elements"
r.tax.set("H")
print("linked-row tax: editable; BillAdd stays minimal; LinkToTxn "
      "structure validated by parse")

# The pick reaches QuickBooks in a second request: add_bill sends the
# minimal BillAdd, then a BillMod listing EVERY line (Mod semantics: an
# omitted line is DELETED) with SalesTaxCodeRef only on the picked one.
# End-to-end through the real add_bill with a stubbed session — no COM.
from qb_client import QuickBooks

ADD_RS = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<BillAddRs statusCode="0" statusMessage="Status OK">
<BillRet><TxnID>BILL-1</TxnID><EditSequence>1754321</EditSequence>
<AmountDue>794.30</AmountDue>
<ExpenseLineRet><TxnLineID>BL-E1</TxnLineID></ExpenseLineRet>
<ItemLineRet><TxnLineID>BL-I1</TxnLineID></ItemLineRet>
<ItemLineRet><TxnLineID>BL-I2</TxnLineID></ItemLineRet>
</BillRet></BillAddRs></QBXMLMsgsRs></QBXML>"""
MOD_RS_OK = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<BillModRs statusCode="0" statusMessage="Status OK">
<BillRet><TxnID>BILL-1</TxnID>
<ExpenseLineRet><TxnLineID>BL-E1</TxnLineID></ExpenseLineRet>
<ItemLineRet><TxnLineID>BL-I1</TxnLineID>
<SalesTaxCodeRef><FullName>E</FullName></SalesTaxCodeRef></ItemLineRet>
<ItemLineRet><TxnLineID>BL-I2</TxnLineID>
<SalesTaxCodeRef><FullName>H</FullName></SalesTaxCodeRef></ItemLineRet>
</BillRet></BillModRs></QBXMLMsgsRs></QBXML>"""
# Mod accepted but no line detail in the response — verification must
# fall back to reading the bill fresh (a third request).
MOD_RS_OK_BARE = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<BillModRs statusCode="0" statusMessage="Status OK">
<BillRet><TxnID>BILL-1</TxnID></BillRet></BillModRs>
</QBXMLMsgsRs></QBXML>"""
QUERY_RS_E = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<BillQueryRs statusCode="0" statusMessage="Status OK">
<BillRet><TxnID>BILL-1</TxnID>
<ItemLineRet><TxnLineID>BL-I1</TxnLineID>
<SalesTaxCodeRef><FullName>E</FullName></SalesTaxCodeRef></ItemLineRet>
<ItemLineRet><TxnLineID>BL-I2</TxnLineID></ItemLineRet>
</BillRet></BillQueryRs></QBXMLMsgsRs></QBXML>"""
# Mod "succeeded" but the line still carries the OLD code — the silent-
# drop case the verification exists to catch.
MOD_RS_DROPPED = MOD_RS_OK.replace(
    "<TxnLineID>BL-I1</TxnLineID>\n<SalesTaxCodeRef><FullName>E</FullName>",
    "<TxnLineID>BL-I1</TxnLineID>\n<SalesTaxCodeRef><FullName>H</FullName>")
MOD_RS_FAIL = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<BillModRs statusCode="3200" statusMessage="edit sequence out of date"/>
</QBXMLMsgsRs></QBXML>"""

LINES = [
    {"kind": "expense", "name": "5100 Travel", "amount": 100.0,
     "tax_code": "H", "memo": "", "customer": ""},
    {"kind": "item", "name": "Freight", "amount": 500.0, "tax_code": "E",
     "po_tax_code": "", "memo": "", "customer": "",
     "link": {"txn_id": "ABC-123", "txn_line_id": "L1"}},
    {"kind": "item", "name": "Software", "amount": 194.30, "tax_code": "H",
     "memo": "", "customer": ""},
]

sent = []
client = QuickBooks.__new__(QuickBooks)   # no COM session
client.last_txn_id = ""
client._dump = lambda *a, **k: None
client.request = lambda x: (sent.append(x),
                            ADD_RS if len(sent) == 1 else MOD_RS_OK)[1]
ok, message = client.add_bill("FedEx", "r-1", "2026-08-07", LINES)
assert ok and len(sent) == 2, (ok, len(sent))
# A clean apply is EXPECTED behavior (the one way QuickBooks takes tax on
# a PO-linked line) — the note must read as verified routine success,
# never as a warning.
assert "Tax code E applied and verified on 1 linked line(s)" in message, message
assert "⚠" not in message, message
for ila in ET.fromstring(sent[0]).findall(".//ItemLineAdd"):
    if ila.find("LinkToTxn") is not None:
        assert [c.tag for c in ila] == ["Amount", "LinkToTxn"], \
            "the linked line in the actual request must stay minimal"
mod = ET.fromstring(sent[1]).find(".//BillMod")
assert mod.findtext("TxnID") == "BILL-1"
assert mod.findtext("EditSequence") == "1754321"
assert [e.findtext("TxnLineID") for e in mod.findall("ExpenseLineMod")] \
    == ["BL-E1"]
item_mods = mod.findall("ItemLineMod")
assert [e.findtext("TxnLineID") for e in item_mods] == ["BL-I1", "BL-I2"], \
    "EVERY item line must be listed or QuickBooks deletes the missing ones"
assert item_mods[0].findtext("SalesTaxCodeRef/FullName") == "E"
assert item_mods[1].find("SalesTaxCodeRef") is None, \
    "lines without a pick pass through unchanged"

# A failed BillMod must never fail (or roll back) the entered bill.
sent.clear()
client.request = lambda x: (sent.append(x),
                            ADD_RS if len(sent) == 1 else MOD_RS_FAIL)[1]
ok, message = client.add_bill("FedEx", "r-2", "2026-08-07", LINES)
assert ok, "a failed tax mod must never fail the entered bill"
assert "NOT applied" in message and "3200" in message, message

# Mod response without line detail → the code is verified by reading the
# bill back fresh (BillQuery + IncludeLineItems, a third request).
sent.clear()
client.request = lambda x: (sent.append(x),
                            [ADD_RS, MOD_RS_OK_BARE, QUERY_RS_E]
                            [len(sent) - 1])[1]
ok, message = client.add_bill("FedEx", "r-2b", "2026-08-07", LINES)
assert ok and len(sent) == 3, (ok, len(sent))
assert "<BillQueryRq>" in sent[2] and "IncludeLineItems" in sent[2]
assert "applied and verified" in message and "⚠" not in message, message

# Mod says OK but the line still carries the old code — the verification
# must catch the silently dropped pick and say so LOUDLY.
sent.clear()
client.request = lambda x: (sent.append(x),
                            ADD_RS if len(sent) == 1 else MOD_RS_DROPPED)[1]
ok, message = client.add_bill("FedEx", "r-2c", "2026-08-07", LINES)
assert ok, "a dropped tax pick must never fail the entered bill"
assert "did NOT stick" in message and "⚠" in message, message
assert '"H"' in message, "the note must say what QuickBooks actually kept"

# No tax pick on any linked line → exactly one request, no mod, no note.
sent.clear()
ok, message = client.add_bill("FedEx", "r-3", "2026-08-07",
                              [dict(LINES[0]), dict(LINES[2])])
assert ok and len(sent) == 1 and "Tax code" not in message
print("linked-row tax via BillMod: applied, fail-soft, skipped when unused")

# A rate-less PO line can't be sized by quantity — Amount override instead.
xml2 = build_bill_add_qbxml("FedEx", "r", "", [
    {"kind": "item", "name": "", "amount": 500.0, "tax_code": "",
     "memo": "", "customer": "",
     "link": {"txn_id": "ABC-123", "txn_line_id": "L2"}}])
assert "<Amount>500.00</Amount>" in xml2 and "<Quantity>" not in xml2
assert "<LinkToTxn><TxnID>ABC-123</TxnID><TxnLineID>L2</TxnLineID></LinkToTxn>" in xml2

# Penny-perfect sizing (live bugs 2026-08-20): QuickBooks recomputes a
# linked line's amount as quantity x rate at 5-decimal quantity
# precision, so Quantity ALONE posted 2,500.00 @ rate 31,000 as 2,500.15
# (qty 0.08065); Amount ALONE posted the right dollars but defaulted the
# receipt quantity to 1, misstating how much of the PO line was
# received. When the quantity can't multiply back exactly, BOTH must be
# sent: the fractional Quantity (correct receipt) + the exact Amount.
assert po_link_sizing(194.30, 1000.0) == (0.1943, False)   # exact → qty only
assert po_link_sizing(2500.00, 31000.0) == (0.08065, True)  # + exact Amount
assert po_link_sizing(500.0, 0) == (None, True)             # rate-less → Amount
# Midpoint case (live 2026-08-21): 171.25 @ 1,500 → qty 0.11417 →
# 171.255 — Python float round() said 171.25 ("exact"), QuickBooks
# half-up-rounded to 171.26 and the bill posted a cent high. The test
# must be DECIMAL-exact: any product with sub-cent digits sends Amount.
assert po_link_sizing(171.25, 1500.0) == (0.11417, True)
r.link["rate"] = 31000.0
r.amount.set("2500.00")
d_drift = r.to_dict()
assert d_drift["quantity"] == 0.08065, \
    "the receipt quantity must still be amount / rate, never dropped"
assert d_drift["po_rate"] == 31000.0
xml3 = build_bill_add_qbxml("FedEx", "r", "", [d_drift])
assert "<Quantity>0.08065</Quantity>" in xml3, \
    "the fractional quantity must post (QB would default it to 1)"
assert "<Amount>2500.00</Amount>" in xml3, \
    "the exact dollar amount must post (qty x rate alone drifts to 2500.15)"
assert (xml3.index("<Quantity>") < xml3.index("<Amount>")
        < xml3.index("<LinkToTxn>")), "qbXML order: Quantity, Amount, link"
assert ("<LinkToTxn><TxnID>ABC-123</TxnID><TxnLineID>L1</TxnLineID>"
        "</LinkToTxn>") in xml3
r.link["rate"] = 1000.0
r.amount.set("194.30")
# Sweep: the quantity is always stated for a rated line, and the exact
# Amount rides along in every case where quantity x rate isn't the cent
# amount EXACTLY in decimal arithmetic (sub-cent digits = QuickBooks
# gets to round = drift, whatever the rounding mode).
from decimal import Decimal

for rate in (0.07, 3.0, 1000.0, 1234.56, 1500.0, 31000.0, 99999.99):
    for cents in range(1, 250001, 1937):
        amount = cents / 100.0
        q, send_amount = po_link_sizing(amount, rate)
        assert q is not None, (amount, rate)
        assert send_amount or (Decimal(str(q)) * Decimal(str(rate))
                               == Decimal(str(amount))), (amount, rate, q)
print("penny-perfect sizing: exact quantity alone, else quantity + exact "
      "Amount together")

# Unlink restores normal entry.
app.unlink_po()
app.update()
assert not app.linked_pos
assert not any(r.link for r in app.rows)
assert app.rows and app.rows[0].amount.get() == "194.30"

# Vendor scoping fails safe: changing the vendor unlinks the PO and shows
# only the new vendor's POs (none here) — no cross-vendor leakage.
app.po_list.selection_set(0)
app.link_po()
assert app.linked_pos
app.vendor.set("Bell Canada")
app.refresh_pos()
assert not app.linked_pos, "vendor change must unlink the PO"
assert not any(r.link for r in app.rows)
assert app.open_pos == [], "other vendors must show zero POs"
print("vendor-change fail-safe: PO unlinked, PO list empty")

app.destroy()
print("=" * 70)
print("PO LINK TEST DONE")
