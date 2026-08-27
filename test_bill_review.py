"""Regression test for the 2026-08-05 requirements batch (FakeQB, no
QuickBooks needed), driven by the two real invoices in
Bills/inbox/Bill Review:

  240170  Invoice1015374  2026-05-31 x.pdf  (J+B Engineering — progress
      billing across several KPA-* POs via a CONTRACT/PRIOR/CURRENT/
      TO DATE/REMAINING table, no "PO No." label, HST 429.00 on a
      registration-number line)
  7069744.pdf  (Rimkus — "PO Number: KPA-25-1723, KPA-26-1039,
      KPA-26-1591" on one invoice, C$ amounts)

Covers: per-PO billing-table amounts (each linked PO gets its own
CURRENT amount, never the invoice total), multi-PO detection + additive
linking, mixed match/failure
(one PO number with no open PO), duplicate PO numbers, over-PO-balance
explicit choice (full / cap / cancel), description sanity flags,
partial-line billing (0.00 = not billed), tax never auto-filled,
date = today, and the qbXML that results.
"""
import sys
from datetime import datetime

sys.path.insert(0, r'quickbooks-automation-main\app')
import bill_entry_gui
from bill_entry_gui import BillEntryApp
from qb_client import build_bill_add_qbxml

TODAY = datetime.now().strftime("%Y-%m-%d")
RIMKUS = "Rimkus Consulting Group Canada, Inc."
JB = "J+B Engineering Inc."


def po(txn, vendor, ref, date, lines, memo=""):
    for i, l in enumerate(lines):
        l.setdefault("txn_line_id", f"{txn}-L{i+1}")
        l.setdefault("quantity", 0)
        l.setdefault("received", 0)
        l.setdefault("rate", 0)
        l.setdefault("customer_job", "Acme Corp:Project X")
        l.setdefault("tax_code", "H")
    subtotal = round(sum(l["amount"] for l in lines), 2)
    open_total = round(sum(l["open_amount"] for l in lines), 2)
    return {"txn_id": txn, "vendor": vendor, "ref_number": ref,
            "txn_date": date, "total": subtotal, "subtotal": subtotal,
            "open_total": open_total,
            "billed_total": round(subtotal - open_total, 2),
            "memo": memo, "customer_jobs": ["Acme Corp:Project X"],
            "lines": lines}


# Rimkus invoice: KPA-25-1723 and KPA-26-1591 exist and are open;
# KPA-26-1039 deliberately does NOT (mixed match/failure). The 25-1723
# PO's open balance (995) is less than the invoice pre-tax (6,275.50).
RIMKUS_POS = [
    po("TXN-1723", RIMKUS, "KPA-25-1723", "2025-05-01",
       [{"item": "Design", "desc": "Store design",
         "amount": 10695.0, "open_amount": 995.0}]),
    po("TXN-1591", RIMKUS, "KPA-26-1591", "2026-03-01",
       [{"item": "Structural", "desc": "Structural site assessment",
         "amount": 1575.0, "open_amount": 1575.0,
         "quantity": 1, "rate": 1575.0},
        {"item": "Structural", "desc": "Structural redesign of ACM canopy",
         "amount": 3150.0, "open_amount": 3150.0,
         "quantity": 1, "rate": 3150.0}]),
    # An unrelated pair sharing one PO number — duplicate detection.
    po("TXN-DUP-A", RIMKUS, "KPA-99-7777", "2026-01-01",
       [{"item": "Misc", "desc": "misc a", "amount": 100.0,
         "open_amount": 100.0}]),
    po("TXN-DUP-B", RIMKUS, "KPA-99-7777", "2026-02-01",
       [{"item": "Misc", "desc": "misc b", "amount": 200.0,
         "open_amount": 200.0}]),
]

JB_POS = [
    po("TXN-2530", JB, "KPA-24-2530", "2024-06-01",
       [{"item": "OTC", "desc": "OTC", "amount": 4850.0,
         "open_amount": 627.50, "quantity": 1, "rate": 627.50}]),
    po("TXN-2190", JB, "KPA-24-2190", "2024-04-01",
       [{"item": "Eng", "desc": "Engineering Design", "amount": 62885.0,
         "open_amount": 11163.75}]),
    po("TXN-1686", JB, "KPA-26-1686", "2026-01-01",
       [{"item": "Boilers", "desc": "Cascade Boilers", "amount": 9800.0,
         "open_amount": 9800.0, "quantity": 1, "rate": 9800.0}]),
    po("TXN-1685", JB, "KPA-26-1685", "2026-01-15",
       [{"item": "Constr", "desc": "Construction", "amount": 31221.0,
         "open_amount": 31221.0, "quantity": 1, "rate": 31221.0}]),
]


class FakeQB:
    def __init__(self):
        self.pos_by_vendor = {RIMKUS: RIMKUS_POS, JB: JB_POS}
        self.added = []

    def vendors(self):
        return ["Bell Canada", RIMKUS, JB, "Telus"]

    def items(self):
        return ["Design", "Eng", "Freight", "Misc", "OTC", "Structural"]

    def accounts(self):
        return ["5100 Travel", "5200 Office Supplies"]

    def tax_codes(self):
        return ["E", "H"]

    def customers(self):
        return ["Acme Corp", "Acme Corp:Project X"]

    def host_info(self):
        return "FakeQB (bill review test)"

    def bill_exists(self, vendor, ref):
        return False

    def open_purchase_orders(self, vendor):
        return [dict(p) for p in self.pos_by_vendor.get(vendor, [])]

    def add_bill(self, vendor, ref, date, lines, memo=""):
        self.added.append({"vendor": vendor, "ref": ref, "date": date,
                           "lines": lines, "memo": memo})
        return True, "fake"

    def close(self):
        pass


# ---- scripted dialogs -------------------------------------------------
mb = bill_entry_gui.messagebox
DIALOGS = {"askyesno": True, "askyesnocancel": None}
CALLS = []


def _fake(name):
    def f(*a, **k):
        CALLS.append((name, a[0] if a else ""))
        return DIALOGS.get(name)
    return f


for name in ("askyesno", "askyesnocancel"):
    setattr(mb, name, _fake(name))
for name in ("showinfo", "showwarning", "showerror"):
    setattr(mb, name, _fake(name))


def status(app):
    return app.status.get("1.0", "end")


def linked_rows(app):
    return [r for r in app.rows if r.link]


def row_for(app, txn_line_id):
    return next(r for r in linked_rows(app)
                if r.link["txn_line_id"] == txn_line_id)


qb = FakeQB()
app = BillEntryApp(qb)
app.withdraw()

# ======================================================================
# Scenario 1 — Rimkus 7069744: three PO numbers on one invoice, two open,
# one missing; additive linking; cap choice; partial lines; entry.
# ======================================================================
app.vendor_defaults = {RIMKUS: {"kind": "Item", "name": "Freight",
                                "tax_code": "H"}}  # tax must NOT apply
app.open_pdf(r"Bills\inbox\Bill Review\7069744.pdf")
app.update()

assert app.vendor.get() == RIMKUS, app.vendor.get()
assert app.ref.get() == "7069744", "layout-mode invoice number"
assert app.date.get() == TODAY, "bill date must be TODAY, not the PDF's"
assert app.memo.get() == ("Project No. 2407121 "
                          "PO KPA-25-1723, KPA-26-1039, KPA-26-1591")
assert app.parsed["pre_tax_total"] == 6275.50
assert app.parsed["po_numbers"] == ["KPA-25-1723", "KPA-26-1039",
                                    "KPA-26-1591"]
# Tax was NOT auto-filled from vendor defaults (name/kind still are).
assert app.rows[0].name.get() == "Freight" and app.rows[0].tax.get() == ""

entries = [app.po_list.get(i) for i in range(app.po_list.size())]
assert "← PO # in PDF" in entries[0] and "← PO # in PDF" in entries[1], entries
assert "DUPLICATE PO #" in entries[2] and "DUPLICATE PO #" in entries[3]
assert app.po_list.curselection() == (0,), "first matched PO preselected"
assert "KPA-26-1039" in status(app) and "no matching open PO" in status(app), \
    "the PO number with no open PO must be flagged"
assert "2 open POs' numbers appear" in status(app)
assert "shares the same" in status(app), "duplicate PO warning must be logged"

# Link PO 1 (KPA-25-1723, open 995 < bill 6,275.50): capped at 995, no
# over-receipt question yet — KPA-26-1591 is still matched-but-unlinked.
CALLS.clear()
app.link_po()
app.update()
assert not any(c[0] == "askyesnocancel" for c in CALLS), \
    "no over-PO question while another matched PO is unlinked"
assert len(app.linked_pos) == 1 and len(linked_rows(app)) == 1
assert row_for(app, "TXN-1723-L1").amount.get() == "995.00"

# Link PO 2 (KPA-26-1591): both its lines match the invoice's OWN line
# items ("Structural site assessment: $1,575.00" / "Structural redesign
# of ACM canopy: $3,150.00" — 2026-08-12 fix), so each row carries the
# invoice's stated amount. The leftover 555.50 (= the missing
# KPA-26-1039's charges) is REPORTED for a normal line — never offered
# for dumping onto lines that carry the invoice's own amounts.
app.po_list.selection_clear(0, "end")
app.po_list.selection_set(1)
CALLS.clear()
app.link_po()
app.update()
assert not any(c[0] == "askyesnocancel" for c in CALLS), \
    "leftover must never be dumped onto invoice-matched lines"
assert len(app.linked_pos) == 2 and len(linked_rows(app)) == 3
assert row_for(app, "TXN-1591-L1").amount.get() == "1575.00"
assert row_for(app, "TXN-1591-L2").amount.get() == "3150.00"
assert status(app).count("from the invoice's own line") == 2, \
    "both amounts must come from the invoice's own line items"
assert "add a normal line for it" in status(app), \
    "the 555.50 leftover must be reported, not silently absorbed"
# No description warnings — these PO lines are really on the invoice.
assert all(r.warn_label.cget("text") == "" for r in linked_rows(app))

# Partial-line billing: zero one PO line — it must simply not be billed.
row_for(app, "TXN-1591-L2").amount.set("0.00")
lines = [d for d in (r.to_dict() for r in app.rows) if d]
assert all(l["link"]["txn_line_id"] != "TXN-1591-L2" for l in lines
           if l.get("link")), "a 0.00 linked row must not be billed"
row_for(app, "TXN-1591-L2").amount.set("3150.00")

# The unmatched PO's 555.50 goes on as a normal expense line; user picks
# the tax code by hand (item: tax is always a manual dropdown pick).
extra = app.add_line(apply_defaults=False)
extra.kind.set("Expense")
extra._kind_changed()
extra.name.set("5100 Travel")
extra.amount.set("555.50")
extra.tax.set("H")

DIALOGS["askyesno"] = True   # "Confirm bill"
CALLS.clear()
app.enter_bill()
app.update()
assert qb.added, "bill must be entered"
bill = qb.added[-1]
assert bill["vendor"] == RIMKUS and bill["ref"] == "7069744"
assert bill["date"] == TODAY
sent_linked = [l for l in bill["lines"] if l.get("link")]
assert {l["link"]["txn_id"] for l in sent_linked} == {"TXN-1723", "TXN-1591"}
assert round(sum(l["amount"] for l in bill["lines"]), 2) == 6275.50
assert "fully billed" in status(app), "both POs report as fully billed"

xml = build_bill_add_qbxml(bill["vendor"], bill["ref"], bill["date"],
                           bill["lines"], memo=bill["memo"])
assert xml.count("<LinkToTxn>") == 3
assert "<TxnID>TXN-1723</TxnID>" in xml and "<TxnID>TXN-1591</TxnID>" in xml
assert "<Quantity>1</Quantity>" in xml      # 1575/1575 on the rated line
assert "<Amount>995.00</Amount>" in xml     # rate-less line: Amount override
assert ("<AccountRef><FullName>5100 Travel</FullName></AccountRef>" in xml
        and "<Amount>555.50</Amount>" in xml)
assert f"<TxnDate>{TODAY}</TxnDate>" in xml
# Tax defaults must no longer be saved per vendor.
assert "tax_code" not in app.vendor_defaults.get(RIMKUS, {})
print("scenario 1 (Rimkus multi-PO, mixed failure, cap, entry): OK")

# ======================================================================
# Scenario 2 — J+B 1015374: per-PO billing table (CONTRACT / PRIOR /
# CURRENT / TO DATE / REMAINING). Linking a PO takes ITS rows' CURRENT
# amount — 9,800 for KPA-26-1686, 2,500 for KPA-26-1685 — never the
# invoice's 3,300 bottom line. A 0.00-CURRENT PO adds no rows; the
# -9,000 back-out is refused as a linked line (manual negative line
# instead); everything reconciles to the 3,300 pre-tax. The entry-time
# over-edit cap still works on a table-sized line.
# ======================================================================
app.open_pdf(r"Bills\inbox\Bill Review\240170  Invoice1015374  2026-05-31 x.pdf")
app.update()
assert app.vendor.get() == JB
assert app.ref.get() == "1015374"
assert app.parsed["pre_tax_total"] == 3300.0, "HST 429 must be recognized"
assert app.date.get() == TODAY
assert app.parsed["po_current_amounts"] == {
    "KPA-24-2190": -9000.0, "KPA-24-2530": 0.0,
    "KPA-26-1686": 9800.0, "KPA-26-1685": 2500.0}, \
    app.parsed["po_current_amounts"]
assert "per-PO billing table" in status(app)
entries = [app.po_list.get(i) for i in range(app.po_list.size())]
assert all("← PO # in PDF" in e for e in entries), entries

# Link KPA-26-1686: its table amount (9,800.00) — NOT the invoice total.
app.po_list.selection_clear(0, "end")
app.po_list.selection_set(2)
CALLS.clear()
app.link_po()
app.update()
assert not any(c[0] == "askyesnocancel" for c in CALLS)
assert row_for(app, "TXN-1686-L1").amount.get() == "9800.00", \
    "linked PO must get ITS table amount, not the invoice total"
assert "9,800.00 CURRENT for KPA-26-1686" in status(app)

# Link KPA-26-1685: 2,500.00, same rule.
app.po_list.selection_clear(0, "end")
app.po_list.selection_set(3)
app.link_po()
app.update()
assert row_for(app, "TXN-1685-L1").amount.get() == "2500.00"

# KPA-24-2530's CURRENT is blank (= 0.00): linking bills nothing.
rows_before = len(app.rows)
app.po_list.selection_clear(0, "end")
app.po_list.selection_set(0)
app.link_po()
app.update()
assert len(app.rows) == rows_before and len(linked_rows(app)) == 2
assert "0.00 CURRENT for KPA-24-2530" in status(app)

# KPA-24-2190 nets -9,000 (a back-out): no PO line can carry it — the
# tool says so and the user enters it as a manual negative line.
app.po_list.selection_clear(0, "end")
app.po_list.selection_set(1)
app.link_po()
app.update()
assert len(linked_rows(app)) == 2, "negative CURRENT must add no PO rows"
assert "credit/back-out" in status(app)
extra = app.add_line(apply_defaults=False)
extra.kind.set("Expense")
extra._kind_changed()
extra.name.set("5100 Travel")
extra.amount.set("-9000.00")
extra.tax.set("H")
app.refresh_totals()
# str(): cget can hand back a Tcl object that prints as the color name
# but never equals a Python string.
assert str(app.totals_label.cget("foreground")) == "dark green", \
    "9,800 + 2,500 - 9,000 must reconcile to the 3,300 pre-tax"

# Tax stays a manual dropdown on linked rows: override 1686's code to E
# (1685 keeps the PO's own H — inherited, so nothing is sent for it).
assert not row_for(app, "TXN-1686-L1").tax_box.instate(["disabled"]), \
    "tax dropdown must stay editable on a linked row"
row_for(app, "TXN-1686-L1").tax.set("E")

# Over-edit the 1686 line above its open balance, pick "No — cap" at
# entry time: back to 9,800 with the quantity recomputed.
row_for(app, "TXN-1686-L1").amount.set("9900.00")
DIALOGS["askyesnocancel"] = False   # cap
DIALOGS["askyesno"] = True          # confirm bill
CALLS.clear()
app.enter_bill()
app.update()
assert any(c[0] == "askyesnocancel" for c in CALLS), "over-check must ask"
bill = qb.added[-1]
l1686 = next(l for l in bill["lines"]
             if l.get("link", {}).get("txn_id") == "TXN-1686")
assert l1686["amount"] == 9800.0, "capped at the PO line's open balance"
assert l1686["quantity"] == 1.0, "quantity recomputed after capping"
l1685 = next(l for l in bill["lines"]
             if l.get("link", {}).get("txn_id") == "TXN-1685")
assert l1685["amount"] == 2500.0
assert round(sum(l["amount"] for l in bill["lines"]), 2) == 3300.0, \
    "entered bill must reconcile to the invoice's net pre-tax total"

# The manual tax pick (E on the linked 1686 line) rides on the line dict
# for qb_client to apply via BillMod AFTER entry — QuickBooks rejects a
# SalesTaxCodeRef sent next to LinkToTxn (status 3210, whole bill). The
# BillAdd qbXML keeps every linked line minimal: the only SalesTaxCodeRef
# is the manual expense line's H.
assert l1686["tax_code"] == "E" and l1686["po_tax_code"] == "H"
xml = build_bill_add_qbxml(bill["vendor"], bill["ref"], bill["date"],
                           bill["lines"], memo=bill["memo"])
assert "<FullName>E</FullName></SalesTaxCodeRef>" not in xml, \
    "a tax pick must never ride inside a linked ItemLineAdd"
assert xml.count("<SalesTaxCodeRef>") == 1, xml
print("scenario 2 (J+B per-PO billing table amounts, linked-row tax): OK")

# ======================================================================
# Scenario 3 — over-receipt chosen (Yes) and link cancelled (Cancel),
# sized by the TABLE amount: the solo PO has 627.50 open but the invoice
# bills 9,800 CURRENT against it; plus a bogus-description flag.
# ======================================================================
qb.pos_by_vendor[JB] = [
    po("TXN-SOLO", JB, "KPA-26-1686", "2026-01-01",
       [{"item": "OTC", "desc": "Underwater basket weaving",
         "amount": 4850.0, "open_amount": 627.50,
         "quantity": 1, "rate": 627.50}]),
]
app.open_pdf(r"Bills\inbox\Bill Review\240170  Invoice1015374  2026-05-31 x.pdf")
app.update()

DIALOGS["askyesnocancel"] = None    # Cancel — undo the link
app.link_po()
app.update()
assert not app.linked_pos and not linked_rows(app), "cancel must unlink"
assert app.rows and app.rows[0].amount.get() == "3300.00", \
    "prefilled row restored after cancel"

app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = True    # Yes — bill the full amount
app.link_po()
app.update()
r = row_for(app, "TXN-SOLO-L1")
assert r.amount.get() == "9800.00", \
    "over-receipt takes this PO's table amount, not the invoice total"
assert "over-receipt" in status(app)
assert r.warn_label.cget("text") == "⚠ desc?", \
    "bogus PO line description must be flagged"
assert "REVIEW" in status(app)
print("scenario 3 (over-receipt yes/cancel, description flag): OK")

# ======================================================================
# Scenario 4 — duplicate PO numbers: no auto-select, linking asks first.
# ======================================================================
qb.pos_by_vendor[JB] = [
    po("TXN-D1", JB, "KPA-24-2530", "2024-06-01",
       [{"item": "OTC", "desc": "OTC", "amount": 500.0,
         "open_amount": 500.0}]),
    po("TXN-D2", JB, "KPA-24-2530", "2024-07-01",
       [{"item": "OTC", "desc": "OTC", "amount": 900.0,
         "open_amount": 900.0}]),
]
app.open_pdf(r"Bills\inbox\Bill Review\240170  Invoice1015374  2026-05-31 x.pdf")
app.update()
assert app.po_list.curselection() == (), \
    "a duplicated PO number must never be auto-selected"
entries = [app.po_list.get(i) for i in range(app.po_list.size())]
assert all("DUPLICATE PO #" in e for e in entries)
app.po_list.selection_set(0)
DIALOGS["askyesno"] = False         # "is this the right PO?" → No
app.link_po()
assert not app.linked_pos, "declining the duplicate confirm must not link"
DIALOGS["askyesno"] = True
app.link_po()
assert len(app.linked_pos) == 1
print("scenario 4 (duplicate PO number safety): OK")

# ======================================================================
# Scenario 5 — non-PO invoice: vendor with no open POs stays plain.
# ======================================================================
qb.pos_by_vendor[JB] = []
app.open_pdf(r"Bills\inbox\Bill Review\240170  Invoice1015374  2026-05-31 x.pdf")
app.update()
assert not app.linked_pos and app.open_pos == []
# The table's KPA refs now count as referenced POs, so the no-open-PO
# case reports the mismatch instead of a plain "no POs" note.
assert "no open POs" in app.po_info.cget("text")
assert "KPA-26-1686" in app.po_info.cget("text")
assert app.rows and app.rows[0].amount.get() == "3300.00"
DIALOGS["askyesno"] = True
qb.added.clear()
app.rows[0].name.set("Eng")
app.enter_bill()
assert qb.added and not any(l.get("link") for l in qb.added[-1]["lines"]), \
    "a no-PO bill must enter exactly like before PO support"
print("scenario 5 (non-PO invoice unaffected): OK")

app.destroy()

# ======================================================================
# Scenario 6 — PDF attachment staging (qb_client, no COM): the PDF must
# land in Attach\<company file name>\Txn\<TxnID>\ beside the .QBW, where
# QuickBooks' "Repair Attached Document Links" picks it up.
# ======================================================================
import tempfile
from pathlib import Path

from qb_client import QuickBooks

with tempfile.TemporaryDirectory() as tmp:
    qbw = Path(tmp) / "K Paul Architect Inc.QBW"
    qbw.write_bytes(b"fake company file")
    client = QuickBooks.__new__(QuickBooks)   # no COM session
    client.company_file_path = lambda: str(qbw)
    pdf = Path(tmp) / "7069744.pdf"
    pdf.write_bytes(b"%PDF-fake")
    ok, msg = client.attach_file_to_txn(str(pdf), "ABC-123")
    staged = (Path(tmp) / "Attach" / "K Paul Architect Inc" / "Txn"
              / "ABC-123" / "7069744.pdf")
    assert ok, msg
    assert staged.exists() and staged.read_bytes() == b"%PDF-fake"
    assert "Repair Attached Document Links" in msg
    ok2, _ = client.attach_file_to_txn(str(pdf), "ABC-123")
    assert ok2, "re-staging the same file must not fail"
    # Missing pieces fail soft, never raise.
    assert client.attach_file_to_txn("", "ABC-123")[0] is False
    assert client.attach_file_to_txn(str(pdf), "")[0] is False
    client.company_file_path = lambda: ""
    assert client.attach_file_to_txn(str(pdf), "ABC-123")[0] is False
print("scenario 6 (PDF attachment staging): OK")

# ======================================================================
# Scenario 7 — a "⚠ desc?" flag must be impossible to miss or silently
# click past (live 2026-08-21: a flagged bill consumed the wrong PO
# line): the flag sits in COLUMN 0 (a narrow window can't push it off-
# screen), an always-visible summary sits beside the Enter button, and
# entering demands an explicit confirmation NAMING the PO line.
# ======================================================================
qb = FakeQB()
qb.pos_by_vendor[JB] = [
    po("TXN-SOLO", JB, "KPA-26-1686", "2026-01-01",
       [{"item": "OTC", "desc": "Underwater basket weaving",
         "amount": 4850.0, "open_amount": 627.50,
         "quantity": 1, "rate": 627.50}]),
]
app = BillEntryApp(qb)
app.withdraw()
app.open_pdf(r"Bills\inbox\Bill Review\240170  Invoice1015374  2026-05-31 x.pdf")
app.update()
app.po_list.selection_set(0)
DIALOGS["askyesnocancel"] = True    # over-receipt: bill the full amount
app.link_po()
app.update()
r = row_for(app, "TXN-SOLO-L1")
assert str(r.warn_label.cget("text")) == "⚠ desc?"
assert r.warn_label.grid_info()["column"] == 0, \
    "the flag must be the FIRST column — never pushed off a narrow window"
assert "1 linked line(s) flagged" in str(app.warn_summary.cget("text")), \
    "the beside-the-button summary must light up with the flag"

# Entering with the flag must ask an explicit, line-NAMING confirmation
# (before the generic 'Confirm bill') — No stops the entry.
captured = []


def ask_capture(title, message, **kwargs):
    captured.append((title, message))
    return DIALOGS["askyesno"]


mb.askyesno = ask_capture
DIALOGS["askyesno"] = False
qb.added.clear()
app.enter_bill()
app.update()
assert not qb.added, "answering No must stop the entry"
titles = [t for t, _ in captured]
assert any("Check the PO line" in t for t in titles), titles
assert not any("Confirm bill" in t for t in titles), \
    "the flag question must come BEFORE the generic confirm"
named = next(m for t, m in captured if "Check the PO line" in t)
assert "Underwater basket weaving" in named, "must NAME the PO line"
assert "KPA-26-1686" in named and "627.50" in named and "9,800.00" in named
assert "Entry stopped" in status(app)

# Yes on the naming confirm continues into the normal flow and enters.
captured.clear()
DIALOGS["askyesno"] = True
app.enter_bill()
app.update()
assert qb.added, "explicit Yes must still enter the bill (never a block)"
assert "Check the PO line" in captured[0][0]
assert any("Confirm bill" in t for t, _ in captured[1:])

# A flagged row set to 0.00 isn't billed — summary and confirm clear.
r.amount.set("0.00")
app.update()
assert str(app.warn_summary.cget("text")) == "", \
    "zeroed flagged line must clear the always-visible summary"
assert not app._flagged_rows()
mb.askyesno = _fake("askyesno")
app.destroy()
print("scenario 7 (desc? flag: column 0, summary, naming confirm): OK")

# ======================================================================
# Scenario 8 — spread prefers PO lines the invoice actually mentions
# (live 2026-08-21: GWAL invoice quoted "Construction Administration"
# but blind PO order handed the money to "Pre Design Services"; second
# live case: the invoice quoted the PO line's own dollar figure, "upset
# limit $3,500", which must outrank word overlap — word overlap typo-
# matched "Site Reviews" to the bill-to's "Suite 200"). Line 1 (first in
# PO order) is nowhere in the Rimkus PDF; line 2's own amount (1,575.00)
# is quoted by the invoice. The unallocated money lands on line 2 first.
# ======================================================================
qb = FakeQB()
qb.pos_by_vendor[RIMKUS] = [
    po("TXN-SPRD", RIMKUS, "KPA-25-1723", "2026-01-01",
       [{"item": "Misc", "desc": "Underwater basket weaving",
         "amount": 6000.0, "open_amount": 6000.0},
        {"item": "Design", "desc": "Store design consulting",
         "amount": 1575.0, "open_amount": 1000.0}]),
]
app = BillEntryApp(qb)
app.withdraw()
app.open_pdf(r"Bills\inbox\Bill Review\7069744.pdf")
app.update()
app.po_list.selection_clear(0, "end")
app.po_list.selection_set(0)
DIALOGS["askyesno"] = True
DIALOGS["askyesnocancel"] = True
app.link_po()
app.update()
assert row_for(app, "TXN-SPRD-L2").amount.get() == "1000.00", \
    "the line whose description IS in the invoice must be filled first"
assert row_for(app, "TXN-SPRD-L1").amount.get() == "5275.50", \
    "the unmentioned line gets only the remainder (6275.50 - 1000)"
assert str(row_for(app, "TXN-SPRD-L1").warn_label.cget("text")) \
    == "⚠ desc?", "the unmentioned line stays flagged for review"
assert str(row_for(app, "TXN-SPRD-L2").warn_label.cget("text")) == ""
app.destroy()
print("scenario 8 (spread prefers invoice-mentioned PO lines): OK")

print("=" * 70)
print("BILL REVIEW TEST DONE")
