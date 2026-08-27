"""PO-panel diagnostics regression test (FakeQB + stubbed qb_client).

Guards the J+B report of 2026-08-18: two open POs (KPA-26-1686/1685)
under "J+B Engineering Inc." must be listed and flagged when the real
J+B billing-table invoice loads — and when QuickBooks does NOT return
them as open, the panel must say WHY (fully received / manually closed /
under a differently-spelled vendor / not in the file at all) instead of
silently showing nothing.
"""
import sys

sys.path.insert(0, r'quickbooks-automation-main\app')
from bill_entry_gui import BillEntryApp
from qb_client import QuickBooks

JB_PDF = r"Bills\inbox\Bill Review\240170  Invoice1015374  2026-05-31 x.pdf"
VENDOR = "J+B Engineering Inc."


def make_po(txn_id, ref, open_amount):
    return {"txn_id": txn_id, "vendor": VENDOR, "ref_number": ref,
            "txn_date": "2026-04-01", "total": open_amount * 1.13,
            "subtotal": open_amount, "open_total": open_amount,
            "billed_total": 0.0, "memo": "", "customer_jobs": ["Job A"],
            "lines": [{"txn_line_id": "L1", "item": "Work", "desc": "Work",
                       "quantity": 1, "received": 0, "rate": open_amount,
                       "amount": open_amount, "open_amount": open_amount,
                       "customer_job": "Job A", "tax_code": ""}]}


class FakeQB:
    def __init__(self, open_pos=(), closed_pos=(), by_ref=()):
        self._open = list(open_pos)
        self.last_closed_pos = list(closed_pos)
        self._by_ref = list(by_ref)
        self.by_ref_queries = []

    def vendors(self):
        return [VENDOR, "J+B Engineering Inc"]  # note: also the no-dot twin

    def items(self):
        return ["Work"]

    def accounts(self):
        return ["5100 Eng"]

    def tax_codes(self):
        return ["H", "E"]

    def customers(self):
        return ["Job A"]

    def host_info(self):
        return "FakeQB (PO diagnostics test)"

    def bill_exists(self, vendor, ref):
        return False

    def open_purchase_orders(self, vendor):
        return [dict(p) for p in self._open] if vendor == VENDOR else []

    def find_pos_by_ref(self, refs):
        self.by_ref_queries.append(list(refs))
        return [p for p in self._by_ref
                if any(p["ref_number"].lower() == r.lower() for r in refs)]

    def add_bill(self, *a, **k):
        return True, "fake"

    def close(self):
        pass


def status(app):
    return app.status.get("1.0", "end")


def po_list(app):
    return [app.po_list.get(i) for i in range(app.po_list.size())]


# ======================================================================
# 1. The J+B report itself: both POs open -> both listed and flagged.
# ======================================================================
qb = FakeQB(open_pos=[make_po("JB-1", "KPA-26-1686", 9800.0),
                      make_po("JB-2", "KPA-26-1685", 31221.0)])
app = BillEntryApp(qb)
app.withdraw()
app.open_pdf(JB_PDF)
app.update()
assert app.vendor.get() == VENDOR, f"vendor guess: {app.vendor.get()!r}"
listed = po_list(app)
assert len(listed) == 2, f"both J+B POs must be listed, got {listed}"
assert all("← PO # in PDF" in line for line in listed), \
    f"both POs' numbers are in the PDF and must be flagged: {listed}"
assert app.po_list.curselection() == (0,), "first matched PO preselected"
app.destroy()
print("J+B invoice + open POs: both listed, flagged, preselected OK")

# ======================================================================
# 2. Same invoice, but QuickBooks says the POs are NOT open any more
#    (fully received — e.g. an earlier full-amount test bill). The panel
#    must say that per PO instead of a bare "no open POs".
# ======================================================================
closed = [{"txn_id": "JB-1", "vendor": VENDOR, "ref_number": "KPA-26-1686",
           "txn_date": "2026-04-01", "total": 11074.0,
           "status": "fully received"},
          {"txn_id": "JB-2", "vendor": VENDOR, "ref_number": "KPA-26-1685",
           "txn_date": "2026-04-01", "total": 35279.73,
           "status": "manually closed"}]
qb = FakeQB(closed_pos=closed)
app = BillEntryApp(qb)
app.withdraw()
app.open_pdf(JB_PDF)
app.update()
assert not po_list(app), "no open POs -> empty list"
s = status(app)
assert "KPA-26-1686 EXISTS for J+B Engineering Inc." in s and \
       "fully received" in s, f"must explain the fully-received PO:\n{s}"
assert "KPA-26-1685 EXISTS for J+B Engineering Inc." in s and \
       "manually closed" in s, f"must explain the manually-closed PO:\n{s}"
app.destroy()
print("closed POs: per-PO 'EXISTS but fully received/closed' notes OK")

# ======================================================================
# 3. The POs live under a differently-spelled vendor: the cross-vendor
#    by-number lookup must name that vendor exactly.
# ======================================================================
by_ref = [{"txn_id": "JB-1", "ref_number": "KPA-26-1686",
           "vendor": "J+B Engineering Inc", "txn_date": "2026-04-01",
           "total": 11074.0, "status": "open"}]
qb = FakeQB(by_ref=by_ref)
app = BillEntryApp(qb)
app.withdraw()
app.open_pdf(JB_PDF)
app.update()
s = status(app)
assert 'under vendor "J+B Engineering Inc"' in s and \
       'NOT under "J+B Engineering Inc."' in s, \
    f"must name the actual vendor spelling:\n{s}"
# Numbers that exist nowhere get the strongest possible statement.
assert "KPA-24-2190: QuickBooks has no purchase order with this number" \
       in s, f"unfindable ref must be called out:\n{s}"
assert qb.by_ref_queries, "cross-vendor lookup must actually run"
app.destroy()
print("vendor-spelling mismatch + not-found refs: named precisely OK")

# ======================================================================
# 4. qb_client parsing: open_purchase_orders keeps the filtered POs on
#    last_closed_pos; find_pos_by_ref queries by RefNumber, no
#    EntityFilter, and reports vendor + status.
# ======================================================================
PO_RS = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<PurchaseOrderQueryRs statusCode="0">
<PurchaseOrderRet><TxnID>T-OPEN</TxnID><TxnDate>2026-04-01</TxnDate>
<RefNumber>KPA-26-1685</RefNumber><TotalAmount>35279.73</TotalAmount>
<VendorRef><FullName>J+B Engineering Inc.</FullName></VendorRef>
<PurchaseOrderLineRet><TxnLineID>L1</TxnLineID>
<ItemRef><FullName>Work</FullName></ItemRef><Desc>Construction</Desc>
<Quantity>1</Quantity><Rate>31221.00</Rate><Amount>31221.00</Amount>
</PurchaseOrderLineRet></PurchaseOrderRet>
<PurchaseOrderRet><TxnID>T-RCVD</TxnID><TxnDate>2026-04-01</TxnDate>
<RefNumber>KPA-26-1686</RefNumber><TotalAmount>11074.00</TotalAmount>
<VendorRef><FullName>J+B Engineering Inc.</FullName></VendorRef>
<IsFullyReceived>true</IsFullyReceived>
<PurchaseOrderLineRet><TxnLineID>L1</TxnLineID>
<Amount>9800.00</Amount></PurchaseOrderLineRet></PurchaseOrderRet>
<PurchaseOrderRet><TxnID>T-CLSD</TxnID><TxnDate>2026-03-01</TxnDate>
<RefNumber>KPA-24-2530</RefNumber><TotalAmount>4850.00</TotalAmount>
<IsManuallyClosed>true</IsManuallyClosed>
<PurchaseOrderLineRet><TxnLineID>L1</TxnLineID>
<Amount>4850.00</Amount></PurchaseOrderLineRet></PurchaseOrderRet>
</PurchaseOrderQueryRs></QBXMLMsgsRs></QBXML>"""
ITEM_RS = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<ItemQueryRs statusCode="0"><ItemServiceRet><Name>Work</Name>
<FullName>Work</FullName></ItemServiceRet></ItemQueryRs>
</QBXMLMsgsRs></QBXML>"""

client = QuickBooks.__new__(QuickBooks)  # no COM session
requests = []


def fake_request(x):
    requests.append(x)
    return ITEM_RS if "ItemQueryRq" in x else PO_RS


client.request = fake_request
pos = client.open_purchase_orders(VENDOR)
assert [p["ref_number"] for p in pos] == ["KPA-26-1685"], \
    "only the truly open PO is 'open'"
assert {(p["ref_number"], p["status"]) for p in client.last_closed_pos} \
    == {("KPA-26-1686", "fully received"), ("KPA-24-2530", "manually closed")}
assert client.last_closed_pos[0]["vendor"] == VENDOR

found = client.find_pos_by_ref(["KPA-26-1686", "", None])
req = requests[-1]
assert "<RefNumber>KPA-26-1686</RefNumber>" in req
assert "EntityFilter" not in req, "by-number lookup spans ALL vendors"
assert {(f["ref_number"], f["vendor"], f["status"]) for f in found} == {
    ("KPA-26-1685", "J+B Engineering Inc.", "open"),
    ("KPA-26-1686", "J+B Engineering Inc.", "fully received"),
    ("KPA-24-2530", "", "manually closed")}
print("qb_client: last_closed_pos + find_pos_by_ref parse/build OK")

print("=" * 70)
print("PO DIAGNOSTICS TEST DONE")
