"""Headless-ish smoke test: drive BillEntryApp with a FakeQB stub."""
import sys
sys.path.insert(0, r'quickbooks-automation-main\app')
import bill_entry_gui
from bill_entry_gui import BillEntryApp


class FakeQB:
    def vendors(self):
        return ["Bell Canada", "TD Aeroplan Visa", "FedEx", "Telus"]

    def items(self):
        return ["Freight", "Software"]

    def accounts(self):
        return ["3900 Retained Earnings", "5100 Travel", "5200 Office Supplies"]

    def tax_codes(self):
        return ["H", "E"]

    def customers(self):
        return ["Acme Corp", "Acme Corp:Project X"]

    def open_purchase_orders(self, vendor):
        return []

    def host_info(self):
        return "FakeQB (smoke test)"

    def bill_exists(self, vendor, ref):
        return False

    def add_bill(self, *a, **k):
        return True, "fake"

    def close(self):
        pass


def dump(app, label):
    print("=" * 70)
    print(label)
    print("vendor:", repr(app.vendor.get()), "| ref:", repr(app.ref.get()),
          "| date:", repr(app.date.get()))
    print("header memo:", repr(app.memo.get()))
    print("doc_type:", app.parsed["doc_type"], "| rows:", len(app.rows))
    print("parsed_label:", app.parsed_label.cget("text"))
    print("totals_label:", app.totals_label.cget("text"),
          "| color:", app.totals_label.cget("foreground"))
    for r in app.rows[:3] + app.rows[-2:]:
        print(f'  kind={r.kind.get():<8} name={r.name.get()!r:<12} '
              f'amt={r.amount.get():>10} tax={r.tax.get()!r:<4} '
              f'memo={r.memo.get()[:50]!r}')
    names = {r.name.get() for r in app.rows}
    print("all account fields blank:", names == {""})
    negs = [r.amount.get() for r in app.rows if r.amount.get().startswith("-")]
    print("negative (refund) rows:", negs)


app = BillEntryApp(FakeQB())
app.withdraw()

app.open_pdf(r"Bills\inbox\2026-07 TD_AEROPLAN_VISA_BUSINESS_2508_Jul_06-2026.pdf")
app.update()
dump(app, "TD statement")

app.open_pdf(r"quickbooks-automation-main\16.99999.10021.273832725.XXXXX2947.000000.pdf")
app.update()
dump(app, "FedEx bill (regression)")

app.open_pdf(r"Bills\inbox\Sample Bills\INVOICE_23117-B3_from_Thomas A_ Fekete Limited.pdf")
app.update()
dump(app, "Fekete bill (ref/memo auto-fill)")
assert app.ref.get() == "23117-B3", app.ref.get()
assert app.memo.get() == "Project No. 2307116 PO KPA-23-1722", app.memo.get()

app.destroy()
print("=" * 70)
print("SMOKE TEST DONE")
