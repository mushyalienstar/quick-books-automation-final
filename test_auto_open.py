"""Auto-open-PDF toggle regression test (no QuickBooks needed).

The "Auto-open PDF in viewer" toggle: ON means every PDF loaded into the
tool also opens in the default viewer (cross-referencing the raw invoice
against the extracted numbers); OFF is exactly the old behavior. The
choice persists across sessions via gui_settings.json.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r'quickbooks-automation-main\app')
import bill_entry_gui
from bill_entry_gui import BillEntryApp

PDF = r"Bills\inbox\Bill Review\7069744.pdf"

# Isolate settings from the real gui_settings.json.
tmp = Path(tempfile.mkdtemp())
bill_entry_gui.SETTINGS_FILE = tmp / "gui_settings.json"

opened = []
_real_startfile = getattr(bill_entry_gui.os, "startfile", None)
bill_entry_gui.os.startfile = lambda p: opened.append(str(p))


class FakeQB:
    def vendors(self):
        return ["Rimkus Consulting Group Canada, Inc."]

    def items(self):
        return ["Consulting"]

    def accounts(self):
        return ["5100 Eng"]

    def tax_codes(self):
        return ["H", "E"]

    def customers(self):
        return ["Job A"]

    def host_info(self):
        return "FakeQB (auto-open test)"

    def bill_exists(self, vendor, ref):
        return False

    def open_purchase_orders(self, vendor):
        return []

    def add_bill(self, *a, **k):
        return True, "fake"

    def close(self):
        pass


try:
    # Fresh install: no settings file -> toggle defaults OFF, nothing opens.
    app = BillEntryApp(FakeQB())
    app.withdraw()
    assert not app.auto_open_pdf.get(), "auto-open must default OFF"
    app.open_pdf(PDF)
    app.update()
    assert not opened, "toggle OFF: the PDF must NOT be opened externally"

    # Toggle ON (as the Checkbutton does): opens on every load + persists.
    app.auto_open_pdf.set(True)
    app._save_auto_open()
    app.open_pdf(PDF)
    app.update()
    assert opened == [str(Path(PDF))], f"toggle ON must open the PDF: {opened}"
    assert "Auto-opened 7069744.pdf" in app.status.get("1.0", "end")
    saved = json.loads(bill_entry_gui.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["auto_open_pdf"] is True, "choice must persist to disk"
    app.destroy()

    # Next session: the saved choice comes back on.
    app2 = BillEntryApp(FakeQB())
    app2.withdraw()
    assert app2.auto_open_pdf.get() is True, \
        "a new session must start with the remembered choice"
    opened.clear()
    app2.open_pdf(PDF)
    app2.update()
    assert opened, "remembered ON must auto-open on load"

    # Toggle OFF again: persists too, and loading stops opening.
    app2.auto_open_pdf.set(False)
    app2._save_auto_open()
    opened.clear()
    app2.open_pdf(PDF)
    app2.update()
    assert not opened, "toggle OFF again: no external open"
    saved = json.loads(bill_entry_gui.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["auto_open_pdf"] is False
    app2.destroy()

    # A broken viewer must never block loading the bill (best-effort).
    def boom(_):
        raise OSError("no handler for .pdf")

    bill_entry_gui.os.startfile = boom
    app3 = BillEntryApp(FakeQB())
    app3.withdraw()
    app3.auto_open_pdf.set(True)
    app3.open_pdf(PDF)
    app3.update()
    assert app3.parsed and app3.parsed["invoice_number"] == "7069744", \
        "the bill must still load when the viewer fails"
    assert "Could not auto-open the PDF" in app3.status.get("1.0", "end")
    app3.destroy()
finally:
    if _real_startfile is not None:
        bill_entry_gui.os.startfile = _real_startfile

print("=" * 70)
print("AUTO-OPEN PDF TOGGLE TEST DONE")
