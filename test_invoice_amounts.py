"""Regression test for the 2026-08-12 live-VM bug batch (FakeQB, no
QuickBooks needed): a linked bill line must always carry the amount the
INVOICE states for that line — never the PO line's amount, and never a
silently-capped substitute.

Covers:
  Bug 1 — linking fills the invoice's own per-line amount (matched by
      description), not the PO line's open amount; with NO readable
      invoice amount the line is 0.00 + warning, never the PO's number.
  Bug 2 — an invoice line amount above the PO line's open balance is an
      explicit Yes/No/Cancel choice showing both real numbers — never a
      silent cap.
  Bug 3 — description matching is concept-level: "Bathroom Cleaning"
      matches "Cleaning services in the bathroom" (word order, grammar,
      extra words), both for line matching and for the "⚠ desc?" flag.
Plus: matched lines take their money off the spread first, ambiguous
matches are never guessed, and per-PO billing tables suppress matching.
"""
import sys

sys.path.insert(0, r'quickbooks-automation-main\app')
import bill_entry_gui
from bill_entry_gui import BillEntryApp
from bill_parser import desc_match_ratio, desc_similarity, desc_words

VENDOR = "Sparkle Janitorial Ltd."


# ---- parser-level (Bug 3) ---------------------------------------------

assert desc_similarity("Bathroom Cleaning",
                       "Cleaning services in the bathroom") >= 0.99, \
    "same work in different words must match"
assert desc_similarity("Bathroom Cleaning",
                       "Cleaned all the bathrooms") >= 0.99, \
    "grammar/plural drift must match"
assert desc_similarity("Bathroom Cleaning",
                       "Electrical panel upgrade") == 0.0
assert desc_similarity("", "anything") == 0.0
assert len(desc_words("Shipping")) < 2, \
    "a single word can't identify the work (no line matching)"
assert desc_match_ratio(
    "Bathroom Cleaning",
    "INVOICE 42 … Cleaning services performed in the bathroom … "
    "Total 2,500.00") >= 0.99, "reworded desc must not flag '⚠ desc?'"
print("parser: concept-level description matching OK")


# ---- GUI harness -------------------------------------------------------

def po(txn, ref, lines):
    for i, l in enumerate(lines):
        l.setdefault("txn_line_id", f"{txn}-L{i+1}")
        l.setdefault("quantity", 0)
        l.setdefault("received", 0)
        l.setdefault("rate", 0)
        l.setdefault("customer_job", "Acme Corp:Project X")
        l.setdefault("tax_code", "H")
        l.setdefault("amount", l["open_amount"])
    subtotal = round(sum(l["amount"] for l in lines), 2)
    open_total = round(sum(l["open_amount"] for l in lines), 2)
    return {"txn_id": txn, "vendor": VENDOR, "ref_number": ref,
            "txn_date": "2026-06-01", "total": subtotal,
            "subtotal": subtotal, "open_total": open_total,
            "billed_total": round(subtotal - open_total, 2),
            "memo": "", "customer_jobs": ["Acme Corp:Project X"],
            "lines": lines}


class FakeQB:
    def __init__(self):
        self.pos = []

    def vendors(self):
        return [VENDOR]

    def items(self):
        return ["Cleaning", "Repairs"]

    def accounts(self):
        return ["5100 Maintenance"]

    def tax_codes(self):
        return ["E", "H"]

    def customers(self):
        return ["Acme Corp", "Acme Corp:Project X"]

    def host_info(self):
        return "FakeQB (invoice amounts test)"

    def bill_exists(self, vendor, ref):
        return False

    def open_purchase_orders(self, vendor):
        return [dict(p) for p in self.pos]

    def add_bill(self, *a, **k):
        return True, "fake"

    def close(self):
        pass


mb = bill_entry_gui.messagebox
DIALOGS = {"askyesno": True, "askyesnocancel": None}
CALLS = []  # (name, title, message)


def _fake(name):
    def f(*a, **k):
        CALLS.append((name, a[0] if a else "", a[1] if len(a) > 1 else ""))
        return DIALOGS.get(name)
    return f


for name in ("askyesno", "askyesnocancel", "showinfo", "showwarning",
             "showerror"):
    setattr(mb, name, _fake(name))


def make_parsed(pre_tax, candidates, text=""):
    text = text or ("INVOICE 42 from Sparkle Janitorial Ltd.  "
                    + "  ".join(f'{c["label"]} {c["amount"]:.2f}'
                                for c in candidates)).ljust(120)
    return {"doc_type": "bill", "invoice_number": "INV-42",
            "po_number": "", "po_numbers": [], "po_table_rows": [],
            "po_current_amounts": {}, "project_number": "", "memo": "",
            "invoice_date": "2026-08-01", "total": pre_tax, "taxes": [],
            "tax_total": 0.0, "pre_tax_total": pre_tax,
            "charge_candidates": candidates, "statement": None,
            "text": text, "vendor_text": text}


def load(app, parsed, pos):
    qb.pos = pos
    app.parsed = parsed
    app.pdf_path = None
    app.linked_pos = []
    for row in list(app.rows):
        app.remove_line(row)
    row = app.add_line(apply_defaults=False)
    row.prefilled = True
    if parsed["pre_tax_total"]:
        row.amount.set(f'{parsed["pre_tax_total"]:.2f}')
    app.vendor.set(VENDOR)
    app.refresh_pos()
    CALLS.clear()


def status(app):
    return app.status.get("1.0", "end")


def linked_rows(app):
    return [r for r in app.rows if r.link]


qb = FakeQB()
app = BillEntryApp(qb)
app.withdraw()

# ======================================================================
# Bug 1 — the invoice bills 2,500 for work the PO line has 5,000 open:
# the bill line must show 2,500 (the invoice's number), leaving 2,500
# open on the PO — never the PO's 5,000.
# ======================================================================
load(app, make_parsed(2500.0, [
        {"label": "Cleaning services in the bathroom", "amount": 2500.0}]),
     [po("TXN-B1", "PO-100",
         [{"item": "Cleaning", "desc": "Bathroom Cleaning",
           "open_amount": 5000.0}])])
app.po_list.selection_set(0)
app.link_po()
app.update()
assert len(linked_rows(app)) == 1
r = linked_rows(app)[0]
assert r.amount.get() == "2500.00", \
    f"must fill the INVOICE amount, got {r.amount.get()}"
assert not any(c[0] == "askyesnocancel" for c in CALLS), \
    "within the open balance — no dialog needed"
assert "2,500.00 stays open on the PO" in status(app), \
    "the remaining open balance must be spelled out"
assert "from the invoice's own line" in status(app)
assert r.warn_label.cget("text") == "", \
    "reworded description must not flag '⚠ desc?'"
print("bug 1: linked line = invoice amount (2,500), PO keeps 2,500 open OK")

# ======================================================================
# Bug 2 — invoice bills 1,575 but the PO line has only 1,500 open:
# explicit Yes/No/Cancel with both real numbers, never a silent cap.
# ======================================================================
OVER_PO = [po("TXN-B2", "PO-200",
              [{"item": "Cleaning", "desc": "Bathroom Cleaning",
                "open_amount": 1500.0}])]
OVER_PARSED = make_parsed(1575.0, [
    {"label": "Cleaning services in the bathroom", "amount": 1575.0}])

# Yes — bill the invoice's full 1,575 (over-receipt).
load(app, OVER_PARSED, OVER_PO)
app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = True
app.link_po()
app.update()
over_calls = [c for c in CALLS if c[0] == "askyesnocancel"]
assert over_calls, "over-balance MUST ask — never a silent decision"
message = over_calls[0][2]
assert "1,575.00" in message and "1,500.00" in message \
    and "75.00" in message, \
    f"dialog must show invoice, open and overage amounts: {message}"
assert linked_rows(app)[0].amount.get() == "1575.00", \
    "Yes keeps the invoice's amount"
assert "over-receipt" in status(app)

# No — cap at 1,500, and SAY so (the 75 left unbilled).
load(app, OVER_PARSED, OVER_PO)
app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = False
app.link_po()
app.update()
assert any(c[0] == "askyesnocancel" for c in CALLS)
assert linked_rows(app)[0].amount.get() == "1500.00"
assert "capped at its open" in status(app) and "75.00" in status(app), \
    "a cap must be the user's logged choice, never silent"

# Cancel — undo the link entirely.
load(app, OVER_PARSED, OVER_PO)
app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = None
app.link_po()
app.update()
assert not app.linked_pos and not linked_rows(app), "cancel must unlink"
print("bug 2: over-balance = explicit choice with real numbers OK")

# ======================================================================
# No readable invoice amount at all (e.g. image-only scan): 0.00 +
# warning — the PO's 5,000 must never be auto-filled.
# ======================================================================
load(app, make_parsed(0.0, [], text="x" * 120),
     [po("TXN-B3", "PO-300",
         [{"item": "Cleaning", "desc": "Bathroom Cleaning",
           "open_amount": 5000.0}])])
app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = None
app.link_po()
app.update()
assert len(linked_rows(app)) == 1
assert linked_rows(app)[0].amount.get() == "0.00", \
    "unknown invoice amount must fill 0.00, NEVER the PO's amount"
assert "added at 0.00, NOT the PO's amount" in status(app)
assert not any(c[0] == "askyesnocancel" for c in CALLS)
print("unknown invoice amount: 0.00 + warning, never the PO's number OK")

# ======================================================================
# Matched lines take their money off the spread first: line B's invoice
# amount can't leak onto earlier unmatched line A.
# ======================================================================
load(app, make_parsed(2500.0, [
        {"label": "Cleaning services in the bathroom", "amount": 2500.0}]),
     [po("TXN-B4", "PO-400",
         [{"item": "Repairs", "desc": "Roofing shingle repair work",
           "open_amount": 1000.0},
          {"item": "Cleaning", "desc": "Bathroom Cleaning",
           "open_amount": 5000.0}])])
app.po_list.selection_set(0)
app.link_po()
app.update()
rows = linked_rows(app)
assert len(rows) == 1, "the unmatched line has no invoice money → skipped"
assert rows[0].link["txn_line_id"] == "TXN-B4-L2"
assert rows[0].amount.get() == "2500.00"
print("precedence: matched invoice amounts never leak onto other lines OK")

# ======================================================================
# Ambiguity — two invoice lines match the description equally well with
# DIFFERENT amounts: never guess; fall back to the spread and say so.
# ======================================================================
load(app, make_parsed(300.0, [
        {"label": "Fuel surcharge June", "amount": 100.0},
        {"label": "Fuel surcharge July", "amount": 200.0}]),
     [po("TXN-B5", "PO-500",
         [{"item": "Cleaning", "desc": "Fuel surcharge extra",
           "open_amount": 500.0}])])
app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = None
app.link_po()
app.update()
assert "not guessing" in status(app)
assert linked_rows(app)[0].amount.get() == "300.00", \
    "ambiguous match falls back to the pre-tax spread"
print("ambiguity: equally-plausible amounts are never guessed OK")

# ======================================================================
# A per-PO billing table suppresses line matching (its CURRENT amount is
# already the invoice's own number; its rows would be bogus candidates).
# ======================================================================
parsed = make_parsed(2500.0, [
    {"label": "Bathroom cleaning contract row", "amount": 9999.0}])
parsed["po_table_rows"] = [{"po_number": "PO-600", "desc": "Bathroom",
                            "current": 2500.0, "amounts": {}}]
parsed["po_current_amounts"] = {"PO-600": 2500.0}
load(app, parsed,
     [po("TXN-B6", "PO-600",
         [{"item": "Cleaning", "desc": "Bathroom Cleaning",
           "open_amount": 5000.0}])])
app.po_list.selection_set(0)
app.link_po()
app.update()
assert linked_rows(app)[0].amount.get() == "2500.00", \
    "table CURRENT wins; the bogus 9,999 candidate must be ignored"
print("per-PO table: line matching suppressed, CURRENT amount used OK")

# ======================================================================
# REAL PDF (live-VM repro 2026-08-12): Rimkus 7069744 prints "Structural
# site assessment: $1,575.00". A PO line with that description and a
# DELIBERATELY different open balance (1,700) must fill 1,575.00 — the
# invoice's number — never 1,700. (The colon after the description used
# to make candidate extraction miss the line entirely, so matching found
# nothing and the spread filled the PO's open amount.)
# ======================================================================
qb.pos = [po("TXN-REAL", "KPA-26-1591",
             [{"item": "Structural", "desc": "Structural site assessment",
               "open_amount": 1700.0, "quantity": 1, "rate": 1700.0}])]
app.open_pdf(r"Bills\inbox\Bill Review\7069744.pdf")
app.update()
cands = {c["label"]: c["amount"] for c in app.parsed["charge_candidates"]}
assert cands.get("Structural site assessment") == 1575.0, \
    f"parser must extract the colon-separated line item: {cands}"
app.vendor.set(VENDOR)
app.refresh_pos()
app.update()
app.po_list.selection_set(0)
CALLS.clear()
DIALOGS["askyesnocancel"] = None
app.link_po()
app.update()
r = linked_rows(app)[0]
assert r.amount.get() == "1575.00", \
    f"REAL PDF: must fill the invoice's 1,575.00, got {r.amount.get()}"
assert not any(c[0] == "askyesnocancel" for c in CALLS), \
    "1,575 is within the 1,700 open balance — no dialog"
assert "125.00 stays open on the PO" in status(app)
assert 'from the invoice\'s own line "Structural site assessment"' \
    in status(app)
print("REAL Rimkus PDF: 1,700-open PO line filled with invoice's 1,575 OK")

# Same real PDF — progress-billing history must never be matched: a PO
# line "Store design" must NOT take the invoice's "Store design
# $10,695.00" ORIGINAL-value line (the invoice bills 995 for that PO, as
# "Remaining to be invoiced"; without a description hit that arrives via
# the spread, capped at the open balance with the explicit dialog).
qb.pos = [po("TXN-HIST", "KPA-25-1723",
             [{"item": "Design", "desc": "Store design",
               "open_amount": 995.0}])]
app.open_pdf(r"Bills\inbox\Bill Review\7069744.pdf")
app.update()
app.vendor.set(VENDOR)
app.refresh_pos()
app.update()
app.po_list.selection_set(0)
CALLS.clear()
DIALOGS["askyesnocancel"] = False   # cap at open — invoice bills 3 POs
app.link_po()
app.update()
r = linked_rows(app)[0]
assert r.amount.get() == "995.00", \
    f"history amounts must never be matched, got {r.amount.get()}"
assert 'PO line "Store design": filled with' not in status(app), \
    "no invoice-line match may be claimed for a history amount"
print("REAL Rimkus PDF: contract/history amounts never matched OK")

# ======================================================================
# A sales-tax ITEM line on the PO ("GST (ITC)") must never become a bill
# line — tax is the user's per-line Tax dropdown pick.
# ======================================================================
tax_po = po("TXN-TAX", "PO-700",
            [{"item": "Cleaning", "desc": "Bathroom Cleaning",
              "open_amount": 5000.0},
             {"item": "GST (ITC)", "desc": "", "open_amount": 242.50}])
tax_po["lines"][1]["is_tax_line"] = True
load(app, make_parsed(2500.0, [
        {"label": "Cleaning services in the bathroom", "amount": 2500.0}]),
     [tax_po])
app.po_list.selection_set(0)
app.link_po()
app.update()
rows = linked_rows(app)
assert len(rows) == 1 and rows[0].link["txn_line_id"] == "TXN-TAX-L1", \
    "the GST (ITC) line must not become a bill line"
assert rows[0].amount.get() == "2500.00"
assert "sales-tax item line" in status(app) and "242.50" in status(app), \
    "skipping the tax line must be said out loud"
print("PO sales-tax item line: skipped with a note, never billed OK")

app.destroy()

# ======================================================================
# qb_client: is_tax_line comes from the LIVE item types (ItemSalesTaxRet
# etc.), with name recognition only as fallback when the query fails.
# ======================================================================
from qb_client import QuickBooks

ITEM_RS = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<ItemQueryRs statusCode="0">
<ItemServiceRet><Name>Structural</Name><FullName>Structural</FullName></ItemServiceRet>
<ItemSalesTaxRet><Name>GST (ITC)</Name></ItemSalesTaxRet>
</ItemQueryRs></QBXMLMsgsRs></QBXML>"""
PO_RS = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<PurchaseOrderQueryRs statusCode="0">
<PurchaseOrderRet><TxnID>PO-1</TxnID><TxnDate>2026-06-01</TxnDate>
<RefNumber>KPA-26-1591</RefNumber><TotalAmount>5092.50</TotalAmount>
<PurchaseOrderLineRet><TxnLineID>L1</TxnLineID>
<ItemRef><FullName>Structural</FullName></ItemRef>
<Desc>Structural site assessment</Desc><Quantity>1</Quantity>
<Rate>4850.00</Rate><Amount>4850.00</Amount></PurchaseOrderLineRet>
<PurchaseOrderLineRet><TxnLineID>L2</TxnLineID>
<ItemRef><FullName>GST (ITC)</FullName></ItemRef>
<Amount>242.50</Amount></PurchaseOrderLineRet>
</PurchaseOrderRet></PurchaseOrderQueryRs></QBXMLMsgsRs></QBXML>"""

client = QuickBooks.__new__(QuickBooks)   # no COM session
client.request = lambda x: ITEM_RS if "ItemQueryRq" in x else PO_RS
pos = client.open_purchase_orders("Rimkus")
assert pos[0]["lines"][0]["is_tax_line"] is False
assert pos[0]["lines"][1]["is_tax_line"] is True, \
    "GST (ITC) must be flagged via its live ItemSalesTaxRet type"

def _failing_items(x):
    if "ItemQueryRq" in x:
        raise RuntimeError("item query unavailable")
    return PO_RS

client2 = QuickBooks.__new__(QuickBooks)
client2.request = _failing_items
pos2 = client2.open_purchase_orders("Rimkus")
assert pos2[0]["lines"][1]["is_tax_line"] is True, \
    "name-pattern fallback must still flag GST (ITC)"
assert pos2[0]["lines"][0]["is_tax_line"] is False
print("qb_client: tax item lines flagged by live type (+fallback) OK")

print("=" * 70)
print("INVOICE AMOUNTS TEST DONE")
