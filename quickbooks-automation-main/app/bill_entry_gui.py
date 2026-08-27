"""PDF bill -> QuickBooks vendor bill entry tool (any vendor).

Run inside the VM with QuickBooks Desktop open on the company file:

    python bill_entry_gui.py [optional-pdf-or-folder]

Open a bill PDF; the parser pre-fills vendor, ref number, date and the
detected amounts. Item, Account, Tax Code and Customer:Job are DROPDOWNS
populated live from the open company file — whatever that file actually
has. Nothing is validated against hardcoded names and nothing blocks entry:
anything the parser couldn't figure out you just pick from a dropdown.

Purchase orders: when a bill loads, the vendor's open POs are listed (every
PO whose number appears in the PDF is flagged; the first is preselected).
Linking a PO creates bill lines against the PO's open lines, each filled
with the amount THIS INVOICE bills for it: the invoice's own line item when
one matches the PO line's description, its per-PO billing-table amount, or
the invoice's pre-tax total spread across the lines. The PO's amounts are
only the open ceiling — never the fill — and when no invoice amount is
readable at all the lines are added at 0.00 to be typed from the invoice.
Amounts stay editable for partial fulfillment (the PO stays open for the
remainder), and Customer:Job defaults from the PO so costs land on the
right project.
Linking is additive: an invoice that covers several POs links each in turn,
and one that references a PO with no open match just gets normal lines for
that part. Billing above a PO's open balance is the user's explicit choice
(over-receipt closing the PO vs capping at the open balance) — never a
silent decision. No PO match, or no link, just means a normal bill — never
a rejection.

The bill Date defaults to TODAY (the day the bill is entered), never the
date printed on the PDF. Tax codes are never auto-filled — always picked
from the live QuickBooks list. After entry the source PDF is staged as the
bill's Doc Center attachment.
"""
import json
import os
import re
import shutil
import sys
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from bill_parser import (HISTORY_LABEL, desc_match_ratio, desc_similarity,
                         desc_words, guess_vendor, parse_bill)
from qb_client import QB_AVAILABLE, QuickBooks, po_link_sizing

DEFAULTS_FILE = Path(__file__).with_name("vendor_defaults.json")
SETTINGS_FILE = Path(__file__).with_name("gui_settings.json")
LINE_KINDS = ("Item", "Expense")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(settings: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass  # settings are a convenience, never fatal


def load_vendor_defaults() -> dict:
    try:
        return json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_vendor_defaults(defaults: dict):
    try:
        DEFAULTS_FILE.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
    except Exception:
        pass  # defaults are a convenience, never fatal


class ScrollFrame(ttk.Frame):
    """Vertically scrollable frame — statements can have 60+ lines."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._window, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.inner.bind("<Enter>", lambda e: self.canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self.inner.bind("<Leave>", lambda e: self.canvas.unbind_all(
            "<MouseWheel>"))

    def _on_wheel(self, event):
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")


class LineRow:
    """One bill line: kind + name dropdown + amount + tax dropdown +
    customer:job dropdown + memo. A row linked to a PO line (set_link)
    bills against that line — item and Customer:Job come from the PO, so
    kind/name/customer are locked; the amount (partial OK) and the Tax
    dropdown stay editable on every row, linked or not."""

    def __init__(self, app: "BillEntryApp", parent: ttk.Frame):
        self.app = app
        self.frame = ttk.Frame(parent)
        self.link = None       # {"txn_id","txn_line_id","po_ref","open_amount"}
        self.prefilled = False  # the single auto row created on PDF load

        self.kind = tk.StringVar(value="Item")
        self.kind_box = ttk.Combobox(self.frame, textvariable=self.kind,
                                     values=LINE_KINDS, width=8, state="readonly")
        self.kind_box.bind("<<ComboboxSelected>>", lambda e: self._kind_changed())

        self.name = tk.StringVar()
        self.name_box = ttk.Combobox(self.frame, textvariable=self.name, width=30)
        self.name_box.bind("<KeyRelease>", self._filter_names)

        self.amount = tk.StringVar()
        self.amount_entry = ttk.Entry(self.frame, textvariable=self.amount, width=10,
                                      justify="right")
        self.amount.trace_add("write", lambda *_: app.refresh_totals())

        self.tax = tk.StringVar()
        self.tax_box = ttk.Combobox(self.frame, textvariable=self.tax, width=8)

        self.customer = tk.StringVar()
        self.customer_box = ttk.Combobox(self.frame, textvariable=self.customer,
                                         width=20)
        self.customer_box.bind("<KeyRelease>", self._filter_customers)

        self.memo = tk.StringVar()
        self.memo_entry = ttk.Entry(self.frame, textvariable=self.memo, width=22)

        remove = ttk.Button(self.frame, text="✕", width=3,
                            command=lambda: app.remove_line(self))

        # Soft review flag ("⚠ desc?") — e.g. a linked PO line whose
        # description doesn't seem to appear on this invoice. FIRST
        # column, fixed width, so a narrow window can never push it out
        # of view (live 2026-08-21: as the LAST column it vanished off
        # the right edge on a laptop screen and a wrong-line link was
        # nearly missed).
        self.warn_label = ttk.Label(self.frame, text="", foreground="red",
                                    width=8)

        for col, widget in enumerate((self.warn_label, self.kind_box,
                                      self.name_box, self.amount_entry,
                                      self.tax_box, self.customer_box,
                                      self.memo_entry, remove)):
            widget.grid(row=0, column=col, padx=(0, 6), sticky="w")

        self._kind_changed()
        self.tax_box["values"] = app.tax_codes
        self.customer_box["values"] = app.customers

    def _all_names(self) -> list:
        return self.app.items if self.kind.get() == "Item" else self.app.accounts

    def _kind_changed(self):
        self.name_box["values"] = self._all_names()

    def _filter_names(self, _event=None):
        typed = self.name.get().strip().lower()
        names = self._all_names()
        self.name_box["values"] = (
            [n for n in names if typed in n.lower()] or names if typed else names)

    def _filter_customers(self, _event=None):
        typed = self.customer.get().strip().lower()
        names = self.app.customers
        self.customer_box["values"] = (
            [n for n in names if typed in n.lower()] or names if typed else names)

    def set_link(self, po: dict, po_line: dict):
        """Bill this row against a specific PO line. The amount can be any
        part of the line's open balance — QuickBooks records the partial
        receipt and leaves the rest of the PO open.

        Item, rate and Customer:Job are display-only: QuickBooks requires
        a linked line to inherit them from the PO line (sending them
        alongside LinkToTxn errors or drops the link), so those boxes just
        show what the PO will provide. Tax stays a manual dropdown like on
        every other row: it starts as the PO line's own code (what flows
        through the link anyway) and picking a DIFFERENT code applies it
        to the entered bill right after the add via BillMod — QuickBooks
        rejects a tax code sent together with a PO link (status 3210);
        leaving it unchanged or blank changes nothing and lets the PO
        line's code apply."""
        self.link = {"txn_id": po["txn_id"],
                     "txn_line_id": po_line["txn_line_id"],
                     "po_ref": po["ref_number"],
                     "open_amount": po_line["open_amount"],
                     "item": po_line["item"], "desc": po_line["desc"],
                     "rate": po_line["rate"],
                     "po_tax_code": po_line["tax_code"]}
        self.kind_box["values"] = ("PO",)
        self.kind.set("PO")
        self.name.set(po_line["item"] or po_line["desc"])
        self.tax.set(po_line["tax_code"])
        self.customer.set(po_line["customer_job"])
        for box in (self.kind_box, self.name_box, self.customer_box):
            box.state(["disabled"])

    def to_dict(self):
        """None if the row is blank; raises ValueError with a reason if bad."""
        name, amount = self.name.get().strip(), self.amount.get().strip()
        if not amount and (self.link or not name):
            return None  # a blank linked row just isn't billed this time
        if not name and not self.link:
            raise ValueError("a line has an amount but no Item/Account selected")
        try:
            value = round(float(amount.replace(",", "").replace("$", "")), 2)
        except ValueError:
            raise ValueError(f'line "{name}" has a bad amount: {amount!r}')
        if self.link and not value:
            # 0.00 against a PO line = not billed this time; the PO line
            # stays fully open (partial-line billing on multi-line POs).
            return None
        d = {"kind": "item" if self.link else self.kind.get().lower(),
             "name": name, "amount": value,
             "tax_code": self.tax.get().strip(), "memo": self.memo.get().strip(),
             "customer": self.customer.get().strip()}
        if self.link:
            d["link"] = {"txn_id": self.link["txn_id"],
                         "txn_line_id": self.link["txn_line_id"]}
            d["po_ref"] = self.link["po_ref"]
            d["open_amount"] = self.link["open_amount"]
            # The PO line's own tax code, so the builder can tell a manual
            # override (sent) from the inherited default (flows through
            # the link, nothing sent).
            d["po_tax_code"] = self.link.get("po_tax_code", "")
            # Mirror what the QuickBooks UI sends when receiving against a
            # PO: the PO line's real item plus the quantity this amount
            # buys at the PO rate (name_box may show a desc fallback).
            # po_rate rides along so the builder can size the line
            # penny-perfect (see qb_client.po_link_sizing — an inexact
            # quantity alone posts drifted cents, an amount alone makes
            # QuickBooks default the receipt quantity to 1).
            d["name"] = self.link["item"]
            d["po_rate"] = self.link["rate"]
            qty, _ = po_link_sizing(value, self.link["rate"])
            if qty is not None:
                d["quantity"] = qty
        return d


class BillEntryApp(tk.Tk):
    def __init__(self, qb):
        super().__init__()
        self.qb = qb
        self.title("Bill Entry — PDF → QuickBooks")
        self.geometry("1280x700")
        self.minsize(1000, 560)

        self.vendors, self.items, self.accounts, self.tax_codes = [], [], [], []
        self.customers = [""]
        self.own_company = []   # company file's own name(s), never a vendor
        self.vendor_defaults = load_vendor_defaults()
        self.settings = load_settings()
        self.pdf_path = None
        self.parsed = None
        self.rows = []
        self.open_pos = []      # open POs for the current vendor
        self.linked_pos = []    # PO dicts this bill is billed against
                                # (one invoice can cover several POs)

        self._build_ui()
        self.refresh_lists()

    # ---------- UI construction ----------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Open PDF…", command=self.open_pdf).pack(side="left")
        ttk.Button(top, text="Refresh QuickBooks lists",
                   command=self.refresh_lists).pack(side="left", padx=8)
        # Cross-reference aid: with this ON, every PDF loaded into the tool
        # also opens in the default PDF viewer/browser, so the real invoice
        # sits next to what the tool extracted. Remembered across sessions.
        self.auto_open_pdf = tk.BooleanVar(
            value=bool(self.settings.get("auto_open_pdf")))
        ttk.Checkbutton(top, text="Auto-open PDF in viewer",
                        variable=self.auto_open_pdf,
                        command=self._save_auto_open).pack(side="left")
        self.conn_label = ttk.Label(top, text="", foreground="gray")
        self.conn_label.pack(side="right")

        head = ttk.LabelFrame(self, text="Bill")
        head.pack(fill="x", **pad)
        self.vendor = tk.StringVar()
        self.ref = tk.StringVar()
        self.date = tk.StringVar()
        self.memo = tk.StringVar()

        ttk.Label(head, text="Vendor").grid(row=0, column=0, sticky="e", **pad)
        self.vendor_box = ttk.Combobox(head, textvariable=self.vendor, width=36)
        self.vendor_box.grid(row=0, column=1, sticky="w", **pad)
        self.vendor_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_pos())
        self.vendor_box.bind("<Return>", lambda e: self.refresh_pos())
        ttk.Label(head, text="Ref No.").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(head, textvariable=self.ref, width=18).grid(
            row=0, column=3, sticky="w", **pad)
        ttk.Label(head, text="Date (YYYY-MM-DD)").grid(row=0, column=4, sticky="e", **pad)
        ttk.Entry(head, textvariable=self.date, width=12).grid(
            row=0, column=5, sticky="w", **pad)
        ttk.Label(head, text="Memo").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(head, textvariable=self.memo, width=60).grid(
            row=1, column=1, columnspan=3, sticky="w", **pad)
        self.parsed_label = ttk.Label(head, text="No PDF loaded.", foreground="gray")
        self.parsed_label.grid(row=1, column=4, columnspan=2, sticky="w", **pad)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, **pad)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        lines_frame = ttk.LabelFrame(body, text="Lines")
        lines_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        header = ttk.Frame(lines_frame)
        header.pack(fill="x", padx=6, pady=(6, 0))
        for text, width in (("⚠", 9), ("Type", 10), ("Item / Account", 33),
                            ("Amount", 11), ("Tax", 10),
                            ("Customer:Job", 23), ("Memo", 22)):
            ttk.Label(header, text=text, width=width,
                      font=("", 9, "bold")).pack(side="left")
        self.lines_scroll = ScrollFrame(lines_frame)
        self.lines_scroll.pack(fill="both", expand=True, padx=6, pady=6)
        self.lines_container = self.lines_scroll.inner
        ttk.Button(lines_frame, text="+ Add line",
                   command=self.add_line).pack(anchor="w", padx=6, pady=(0, 6))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        po_frame = ttk.LabelFrame(right, text="Open purchase orders for this vendor")
        po_frame.pack(fill="x")
        self.po_list = tk.Listbox(po_frame, height=5, exportselection=False)
        self.po_list.pack(fill="x", padx=6, pady=(6, 2))
        self.po_list.bind("<<ListboxSelect>>", self._po_selected)
        self.po_info = ttk.Label(po_frame, text="Pick a vendor, then Find POs.",
                                 foreground="gray", wraplength=420, justify="left")
        self.po_info.pack(fill="x", padx=6)
        po_btns = ttk.Frame(po_frame)
        po_btns.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Button(po_btns, text="Find POs",
                   command=self.refresh_pos).pack(side="left")
        ttk.Button(po_btns, text="Link bill to selected PO",
                   command=self.link_po).pack(side="left", padx=6)
        ttk.Button(po_btns, text="Unlink all",
                   command=self.unlink_po).pack(side="left")

        cand_frame = ttk.LabelFrame(right, text="Amounts detected in PDF (double-click to add as line)")
        cand_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.cand_list = tk.Listbox(cand_frame)
        self.cand_list.pack(fill="both", expand=True, padx=6, pady=6)
        self.cand_list.bind("<Double-Button-1>", self.add_candidate)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.totals_label = ttk.Label(bottom, text="", font=("", 10))
        self.totals_label.pack(side="left")
        self.enter_btn = ttk.Button(bottom, text="Enter Bill in QuickBooks",
                                    command=self.enter_bill)
        self.enter_btn.pack(side="right")
        # Always-visible mismatch summary: the per-row "⚠ desc?" flag can
        # scroll out of sight, this label sits beside the button that
        # commits the bill and cannot.
        self.warn_summary = ttk.Label(bottom, text="", foreground="red")
        self.warn_summary.pack(side="right", padx=(0, 12))

        self.status = tk.Text(self, height=4, state="disabled", wrap="word")
        self.status.pack(fill="x", padx=8, pady=(0, 8))

    # ---------- QuickBooks lists (live, never hardcoded) ----------

    def refresh_lists(self):
        if not self.qb:
            self.conn_label.config(
                text="QuickBooks NOT connected — preview only", foreground="red")
            self.enter_btn.state(["disabled"])
            return
        try:
            self.vendors = self.qb.vendors()
            self.items = self.qb.items()
            self.accounts = self.qb.accounts()
            self.tax_codes = [""] + self.qb.tax_codes()
            self.customers = ([""] + self.qb.customers()
                              if hasattr(self.qb, "customers") else [""])
            # The company file's own name(s): the bill-to party on every
            # invoice — vendor guessing must never pick it.
            self.own_company = (self.qb.company_names()
                                if hasattr(self.qb, "company_names") else [])
        except Exception as e:
            self.log(f"Could not load QuickBooks lists: {e}")
            return
        self.vendor_box["values"] = self.vendors
        for row in self.rows:
            row._kind_changed()
            row.tax_box["values"] = self.tax_codes
            row.customer_box["values"] = self.customers
        product = self.qb.host_info()
        self.conn_label.config(
            text=f"Connected: {product}  ({len(self.vendors)} vendors, "
                 f"{len(self.items)} items, {len(self.accounts)} accounts, "
                 f"{len(self.customers) - 1} customers, "
                 f"{len(self.tax_codes) - 1} tax codes)",
            foreground="dark green")
        self.enter_btn.state(["!disabled"])
        # Say whether the own-company vendor exclusion is armed — a
        # silently empty exclusion is indistinguishable from a working
        # one until a bill guesses the user's own company as the vendor
        # (live 2026-08-21).
        if hasattr(self.qb, "company_names"):
            if self.own_company:
                self.log("Own company (excluded from vendor guessing): "
                         + ", ".join(self.own_company))
            else:
                self.log("⚠ Could not read the company file's own name — "
                         "the own-company vendor exclusion is INACTIVE "
                         "this session. Double-check the Vendor field on "
                         "every bill.")

    # ---------- PDF loading ----------

    def open_pdf(self, path: str = None):
        if not path:
            path = filedialog.askopenfilename(
                title="Open bill PDF", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        self.pdf_path = Path(path)
        try:
            self.parsed = parse_bill(path)
        except Exception as e:
            messagebox.showerror("Parse error", f"Could not read {path}:\n{e}")
            return

        if self.auto_open_pdf.get():
            self._open_pdf_externally()

        p = self.parsed
        self.ref.set(p["invoice_number"])
        # The bill Date is the day it's ENTERED — today — never the date
        # printed on the PDF (that one still shows in the parsed info).
        self.date.set(datetime.now().strftime("%Y-%m-%d"))
        # "Project No. X PO Y" built from the bill's own identifiers; the
        # filename only when the bill carries neither.
        self.memo.set(p["memo"] or self.pdf_path.name)
        # For statements, vendor_text excludes the transaction rows, so a
        # line-item merchant can't be mistaken for the card issuer. The
        # company file's own name is excluded — it's the bill-to on every
        # invoice, never the vendor.
        vendor = guess_vendor(p["vendor_text"], self.vendors,
                              exclude_names=tuple(
                                  getattr(self, "own_company", []) or []))
        self.vendor.set(vendor)
        if not vendor and self.vendors:
            self.log("Vendor: no QuickBooks vendor name (or unique "
                     "email/web domain) found in this PDF — pick the "
                     "vendor from the dropdown.")

        self.cand_list.delete(0, "end")
        for c in p["charge_candidates"]:
            self.cand_list.insert("end", f'{c["amount"]:>10.2f}   {c["label"]}')

        for row in list(self.rows):
            self.remove_line(row)
        self.linked_pos = []

        if p["doc_type"] == "statement":
            self._load_statement(p)
        else:
            # One line pre-filled with the pre-tax amount — the usual case
            # where the whole bill goes to one item/account and the tax code
            # adds tax.
            row = self.add_line()
            row.prefilled = True
            if p["pre_tax_total"]:
                row.amount.set(f'{p["pre_tax_total"]:.2f}')
            taxes = ", ".join(f'{t["label"]} {t["amount"]:.2f}'
                              for t in p["taxes"][:4])
            self.parsed_label.config(
                text=f'PDF total {p["total"]:.2f}  (tax {p["tax_total"]:.2f}'
                     + (f": {taxes}" if taxes else "") + ")",
                foreground="black")
            self.log(f"Loaded {self.pdf_path.name}. Check the pre-filled fields, "
                     f"pick Item/Account and Tax from the dropdowns, then Enter Bill.")
            per_po = p.get("po_current_amounts") or {}
            if per_po:
                table_sum = round(sum(per_po.values()), 2)
                msg = ('This invoice has a per-PO billing table — CURRENT '
                       'amounts: '
                       + ", ".join(f'{n} {a:,.2f}' for n, a in per_po.items())
                       + '. Linking a PO uses ITS amount, not the invoice '
                         'total.')
                if p["pre_tax_total"] and abs(
                        table_sum - p["pre_tax_total"]) > 0.02:
                    msg += (f' NOTE: the table sums to {table_sum:,.2f} but '
                            f'the invoice pre-tax is '
                            f'{p["pre_tax_total"]:,.2f} — double-check the '
                            f'amounts before entering.')
                self.log(msg)

        self.title(f"Bill Entry — {self.pdf_path.name}")
        self.refresh_totals()
        self.refresh_pos()

    def _save_auto_open(self):
        """Persist the auto-open toggle so the next session starts with
        the same choice."""
        self.settings["auto_open_pdf"] = bool(self.auto_open_pdf.get())
        save_settings(self.settings)

    def _open_pdf_externally(self):
        """Show the loaded PDF in the default viewer/browser — the raw
        invoice next to the tool's extracted numbers. Best-effort: a
        viewer problem never blocks loading the bill."""
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(self.pdf_path))  # default Windows handler
            else:
                webbrowser.open(self.pdf_path.resolve().as_uri())
            self.log(f"Auto-opened {self.pdf_path.name} in the default PDF "
                     f"viewer (untick \"Auto-open PDF in viewer\" to stop).")
        except Exception as e:
            self.log(f"Could not auto-open the PDF ({e}) — open "
                     f"{self.pdf_path} manually.")

    def _load_statement(self, p: dict):
        """One line per statement transaction. Item/Account is left blank on
        purpose — pick each from the dropdown; nothing is silently defaulted."""
        s = p["statement"]
        if s["statement_date"]:
            self.ref.set(f'Stmt {s["statement_date"]}')
        skipped = 0
        for t in s["transactions"]:
            if t["is_payment"]:  # paying the card isn't an expense line
                skipped += 1
                continue
            row = self.add_line(apply_defaults=False)
            row.kind.set("Expense")
            row._kind_changed()
            row.amount.set(f'{t["amount"]:.2f}')
            row.memo.set(f'{t["date"] or t["date_raw"]} {t["description"]}')

        target = s["stated_purchases"] or s["charges_total"]
        matches = abs(s["charges_total"] - target) < 0.005
        self.parsed_label.config(
            text=f'Statement: {len(s["transactions"])} transactions, charges '
                 f'{s["charges_total"]:.2f} vs stated {target:.2f} '
                 f'{"✓" if matches else "✗ MISMATCH"}',
            foreground="dark green" if matches else "red")
        self.log(f'Loaded {self.pdf_path.name} as a CREDIT CARD STATEMENT: '
                 f'{len(s["transactions"])} transactions'
                 + (f" ({skipped} card-payment row(s) excluded)" if skipped else "")
                 + ". Vendor should be the card issuer. Each line's Account is "
                   "left blank — pick one per line (or delete lines you don't "
                   "want) before entering.")

    # ---------- purchase orders ----------

    def refresh_pos(self):
        """List the vendor's open POs and flag EVERY one whose PO number
        appears in the loaded PDF (an invoice can bill several POs). Purely
        an aid — linking is always the user's call, and no PO match just
        means a normal bill entry. Also surfaces two soft warnings: open
        POs that share the same PO number (duplicates are never auto-
        selected), and invoice PO numbers with no matching open PO."""
        self.po_list.delete(0, "end")
        self.open_pos = []
        vendor = self.vendor.get().strip()
        # A linked PO belongs to one vendor — changing the vendor unlinks
        # rather than letting a mismatched bill reach QuickBooks.
        if self.linked_pos and any(po.get("vendor", vendor) != vendor
                                   for po in self.linked_pos):
            refs = ", ".join(po["ref_number"] for po in self.linked_pos)
            self.unlink_po(quiet=True)
            self.log(f"Vendor changed — unlinked PO(s) {refs} (they belong "
                     f"to the previous vendor).")
        if not vendor or not self.qb or not hasattr(self.qb,
                                                    "open_purchase_orders"):
            self.po_info.config(text="Pick a vendor, then Find POs.",
                                foreground="gray")
            return
        try:
            self.open_pos = self.qb.open_purchase_orders(vendor)
        except Exception as e:
            self.po_info.config(text=f"PO lookup failed: {e}", foreground="red")
            return

        # PO numbers the parser read off the invoice (may be several:
        # "PO Number: KPA-25-1723, KPA-26-1039, KPA-26-1591").
        parsed = self.parsed or {}
        pdf_po_nos = [p.strip() for p in
                      (parsed.get("po_numbers")
                       or ([parsed["po_number"]] if parsed.get("po_number")
                           else []))
                      if p.strip()]

        if not self.open_pos:
            closed = getattr(self.qb, "last_closed_pos", None) or []
            text = f"No open POs for {vendor} — enter the bill normally."
            if closed:
                text = (f"No OPEN POs for {vendor} — QuickBooks has "
                        f"{len(closed)} PO(s) for this vendor, but every "
                        f"one is fully received or closed. Enter the bill "
                        f"normally.")
            if pdf_po_nos:
                text = (f"This invoice references PO(s) "
                        f"{', '.join(pdf_po_nos)}, but {vendor} has no open "
                        f"POs in QuickBooks — the log below says why. Enter "
                        f"the bill normally, or fix what it reports and "
                        f"Find POs again.")
                self.log("NOTE: " + text)
                self._explain_missing_pos(vendor, pdf_po_nos)
            self.po_info.config(text=text, foreground="gray")
            return

        # Scan the PDF text AND the filename — scanned PDFs have no text
        # layer, but files are often named after the PO ("[KPA-25-1744]…").
        pdf_text = parsed.get("text", "")
        if self.pdf_path:
            pdf_text += "\n" + self.pdf_path.name
        pdf_pos_lower = {p.lower() for p in pdf_po_nos}

        # Two open POs sharing one number is a data problem QuickBooks
        # allows to exist — flag it and never auto-pick between them.
        ref_counts = {}
        for po in self.open_pos:
            ref = (po["ref_number"] or "").strip().lower()
            if ref:
                ref_counts[ref] = ref_counts.get(ref, 0) + 1
        dup_refs = {ref for ref, n in ref_counts.items() if n > 1}

        match_idxs = []
        for i, po in enumerate(self.open_pos):
            ref = (po["ref_number"] or "").strip()
            hit = ""
            # PDFs often glue labels straight onto numbers ("PO No2-738-…"),
            # so only rule out digit/dash neighbours, not any word boundary.
            if ref and (ref.lower() in pdf_pos_lower
                        or (len(ref) >= 3 and pdf_text and re.search(
                            rf"(?<![\d-]){re.escape(ref)}(?![\d-])",
                            pdf_text))):
                hit = "  ← PO # in PDF"
                match_idxs.append(i)
            if ref and ref.lower() in dup_refs:
                hit += "  ⚠ DUPLICATE PO #"
            job = (po["customer_jobs"] or [""])[0]
            self.po_list.insert(
                "end",
                f'PO {ref or "?"}  {po["txn_date"]}  '
                f'open {po["open_total"]:,.2f} of {po["subtotal"]:,.2f}'
                + (f"  {job}" if job else "") + hit)

        if dup_refs:
            self.log("WARNING: more than one open PO shares the same "
                     "number: " + ", ".join(sorted(dup_refs)).upper() +
                     ". Nothing was auto-selected for those — verify by "
                     "date/amount before linking, and consider renumbering "
                     "one of them in QuickBooks.")

        # Invoice PO numbers with no matching open PO (item: mixed
        # match/failure) — flagged, never a blocker.
        open_refs = {(po["ref_number"] or "").strip().lower()
                     for po in self.open_pos}
        missing = [p for p in pdf_po_nos if p.lower() not in open_refs]
        if missing:
            self.log(f"NOTE: this invoice references PO(s) "
                     f"{', '.join(missing)} with no matching open PO for "
                     f"{vendor}. Link the PO(s) that did match; enter the "
                     f"rest as normal lines.")
            self._explain_missing_pos(vendor, missing)

        matched_refs = [self.open_pos[i]["ref_number"] for i in match_idxs]
        sel = next((i for i in match_idxs
                    if (self.open_pos[i]["ref_number"] or "").strip().lower()
                    not in dup_refs), None)
        if sel is not None:
            self.po_list.selection_set(sel)
            self.po_list.see(sel)
            self._po_selected()
            if len(match_idxs) > 1:
                self.log(f'{len(match_idxs)} open POs\' numbers appear in '
                         f'this PDF: {", ".join(matched_refs)}. '
                         f'PO {self.open_pos[sel]["ref_number"]} is selected '
                         f'— click "Link bill to selected PO" for each PO '
                         f'this bill covers (links add up on one bill).')
            else:
                self.log(f'PO {self.open_pos[sel]["ref_number"]}\'s number '
                         f'appears in this PDF — check it and click "Link '
                         f'bill to selected PO" if this bill is against it.')
        elif not match_idxs:
            self.po_info.config(
                text=f"{len(self.open_pos)} open PO(s) for {vendor}. Select "
                     f"one for details; link only if this bill is against it.",
                foreground="black")

    def _explain_missing_pos(self, vendor: str, missing: list):
        """Say WHY each invoice-referenced PO number has no entry in the
        panel: closed/fully received under this vendor, living under a
        differently-spelled vendor, or not in the company file at all.
        Live lookups, one soft NOTE per number, never a blocker."""
        if not missing or not self.qb:
            return
        reasons = {}
        # Same vendor, but not open — open_purchase_orders keeps what its
        # query filtered out, so this needs no extra round trip.
        for po in getattr(self.qb, "last_closed_pos", None) or []:
            ref = (po.get("ref_number") or "").strip()
            status = po.get("status", "closed")
            for m in missing:
                if m.lower() != ref.lower() or m in reasons:
                    continue
                why = (f'PO {ref} EXISTS for {vendor} (dated '
                       f'{po.get("txn_date", "?")}) but is {status} — ')
                if status == "fully received":
                    why += ('bills/item receipts already used up every '
                            'line (a full-amount entry — e.g. an earlier '
                            'TEST bill — does this). Delete that bill, or '
                            'untick the "Clsd" flag on the PO\'s lines in '
                            'QuickBooks, then Find POs again.')
                else:
                    why += ('it was ticked closed in QuickBooks. Untick '
                            '"Closed" on the PO to reopen it, then Find '
                            'POs again.')
                reasons[m] = why
        # Still unexplained: search the number across ALL vendors — an
        # invoice booked under "… Inc." while the PO sits under "… Inc"
        # is exactly the mismatch a vendor-scoped query can't see.
        left = [m for m in missing if m not in reasons]
        checked_all_vendors = False
        if left and hasattr(self.qb, "find_pos_by_ref"):
            try:
                hits = self.qb.find_pos_by_ref(left)
                checked_all_vendors = True
            except Exception as e:
                self.log(f"Could not look up PO number(s) "
                         f"{', '.join(left)} across vendors: {e}")
                hits = []
            for po in hits:
                ref = (po.get("ref_number") or "").strip()
                m = next((x for x in left if x.lower() == ref.lower()), None)
                if not m:
                    continue
                where = (f'PO {ref} exists in QuickBooks under vendor '
                         f'"{po.get("vendor", "?")}" ({po.get("status")}, '
                         f'dated {po.get("txn_date", "?")}, total '
                         f'{po.get("total", 0.0):,.2f})')
                if po.get("vendor") == vendor:
                    where += (' — yet the vendor-scoped PO query did not '
                              'return it (more than 500 POs for this '
                              'vendor?). Check the PO in QuickBooks.')
                else:
                    where += (f' — NOT under "{vendor}" selected here. If '
                              f'that is the same company spelled '
                              f'differently, pick that exact vendor name '
                              f'and Find POs again.')
                if m in reasons:
                    reasons[m] += " Also: " + where + "."
                else:
                    reasons[m] = where + "."
        for m in missing:
            if m in reasons:
                self.log("NOTE: " + reasons[m])
            elif checked_all_vendors:
                self.log(f'NOTE: PO {m}: QuickBooks has no purchase order '
                         f'with this number under ANY vendor, open or '
                         f'closed — the number printed on the invoice may '
                         f'differ from the PO\'s, or the PO was deleted.')

    def _selected_po(self):
        sel = self.po_list.curselection()
        return self.open_pos[sel[0]] if sel and sel[0] < len(self.open_pos) \
            else None

    def _po_selected(self, _event=None):
        po = self._selected_po()
        if not po:
            return
        jobs = ", ".join(po["customer_jobs"]) or "(no Customer:Job on the PO)"
        self.po_info.config(
            text=f'PO {po["ref_number"]}  {po["txn_date"]}   '
                 f'original {po["subtotal"]:,.2f}, '
                 f'billed so far {po["billed_total"]:,.2f}, '
                 f'open {po["open_total"]:,.2f}.   Jobs: {jobs}'
                 + (f'   Memo: {po["memo"]}' if po["memo"] else ""),
            foreground="black")

    def link_po(self):
        """Bill against the selected PO: one editable row per open PO line,
        each filled with the amount THIS INVOICE bills for it. Amount
        sources, in order: the invoice's own line item matched to the PO
        line by description, the invoice's per-PO billing-table amount,
        the bill's not-yet-allocated pre-tax total spread across the
        lines (ones the invoice's text actually mentions first) — and
        0.00 with a warning when none of those is readable.
        The PO's own amounts are only the open ceiling, never the fill
        (partial fulfillment — whatever isn't billed stays open on the
        PO). Customer:Job defaults from the PO lines. Additive: linking
        another PO keeps the rows already linked, so one invoice can bill
        several POs. Billing above a line's or the PO's open balance is
        offered as an explicit choice, never done (or suppressed)
        silently."""
        po = self._selected_po()
        if not po:
            messagebox.showinfo("No PO selected",
                                "Select a purchase order in the list first.")
            return
        if any(p["txn_id"] == po["txn_id"] for p in self.linked_pos):
            messagebox.showinfo(
                "Already linked",
                f'PO {po["ref_number"]} is already linked to this bill.')
            return
        # Same number on two different open POs: make absolutely sure the
        # right one was picked — two bills silently landing on two POs
        # that share a number is exactly what must never happen.
        ref = (po["ref_number"] or "").strip()
        same_ref = [p for p in self.open_pos
                    if p["txn_id"] != po["txn_id"] and ref
                    and (p["ref_number"] or "").strip().lower() == ref.lower()]
        if same_ref and not messagebox.askyesno(
                "Duplicate PO number",
                f'{len(same_ref) + 1} open POs share the number {ref}.\n\n'
                f'Selected: dated {po["txn_date"]}, open '
                f'{po["open_total"]:,.2f} of {po["subtotal"]:,.2f}.\n'
                f'Check the date and amounts against the invoice — is this '
                f'the right PO to link?'):
            return

        self.linked_pos.append(po)
        primary_job = (po["customer_jobs"] or [""])[0]

        # The auto-prefilled row would double-count the amount now covered
        # by the PO rows. (Remove BEFORE measuring what's already allocated.)
        for row in [r for r in self.rows if r.prefilled]:
            self.remove_line(row)

        bill_amt = None
        po_amt = self._po_table_amount(po)
        # A sales-tax ITEM line on the PO (e.g. "GST (ITC)") is not work
        # to bill — tax on a bill comes from the Tax dropdown the user
        # picks per line. Linking skips such lines, and says so.
        tax_lines = [l for l in po["lines"]
                     if l.get("is_tax_line") and l["open_amount"] > 0]
        if tax_lines:
            names = ", ".join(f'"{l["item"] or l["desc"]}" '
                              f'({l["open_amount"]:,.2f})'
                              for l in tax_lines)
            self.log(f'NOTE: PO {po["ref_number"]} carries sales-tax item '
                     f'line(s) — {names} — skipped: tax on a bill comes '
                     f'from each line\'s Tax dropdown, never from a PO '
                     f'line. (The PO\'s open total still includes them.)')
        open_lines = [l for l in po["lines"]
                      if l["open_amount"] > 0 and not l.get("is_tax_line")]
        # The invoice's own line for a PO line (matched by description) is
        # the primary amount source — what the invoice actually bills for
        # that work this time. The PO line's amount is never the fill.
        matches = self._match_invoice_lines(open_lines)
        if po_amt is not None:
            # The invoice's own per-PO billing table (CURRENT column) says
            # how much of it is billed against THIS PO — spread that, not
            # the invoice total (one invoice bills several POs different
            # amounts; the bottom line is just their sum).
            bill_amt = po_amt
            remaining = max(po_amt, 0.0)
        elif (self.parsed and self.parsed["doc_type"] == "bill"
                and self.parsed["pre_tax_total"]):
            bill_amt = self.parsed["pre_tax_total"]
            # Spread only what earlier-linked POs / manual rows haven't
            # already claimed of this bill's pre-tax total.
            already = 0.0
            for r in self.rows:
                try:
                    already += float(r.amount.get()
                                     .replace(",", "").replace("$", ""))
                except ValueError:
                    pass
            remaining = max(round(bill_amt - already, 2), 0.0)
        else:
            remaining = None
        # Matched lines take their invoice amounts off the top, so the
        # spread over the unmatched lines can't hand their money to an
        # earlier PO line.
        if remaining is not None:
            for line in open_lines:
                m = matches.get(id(line))
                if m:
                    remaining = max(round(remaining - m["amount"], 2), 0.0)
        # Spread order: PO lines whose description actually appears in
        # this invoice's text get the unallocated money FIRST — an
        # invoice that quotes one PO line's wording ("Construction
        # Administration - hourly rates") while never mentioning another
        # ("Pre Design Services") is billing the quoted line (live
        # 2026-08-21: blind PO order handed the amount to the wrong
        # line). Image-only scans or all/none-mentioned POs keep plain
        # PO order. The total allocated is order-independent; only which
        # line the money lands on changes.
        spread_order = open_lines
        pdf_text = (self.parsed or {}).get("text", "")
        if remaining is not None and len(pdf_text.strip()) >= 100:
            def rank(line):
                # 0 — the invoice quotes this PO line's own dollar figure
                # ("Hourly rates - upset limit $3,500" names the $3,500
                # line). Strongest cue: live 2026-08-21 word overlap
                # typo-matched "Site Reviews" to the bill-to's "Suite
                # 200" and outranked the truly-billed line. 1 — the
                # line's wording appears in the text. 2 — unmentioned.
                amt = line["amount"]
                forms = {f"{amt:,.2f}"}
                if amt > 0 and amt == int(amt):
                    forms.add(f"{int(amt):,}")
                if amt > 0 and any(
                        re.search(r"(?<![\d,.])" + re.escape(f)
                                  + r"(?![\d.])", pdf_text)
                        for f in forms):
                    return 0
                if desc_match_ratio(line["desc"] or line["item"],
                                    pdf_text) >= 0.5:
                    return 1
                return 2
            ranks = {id(l): rank(l) for l in open_lines}
            if len(set(ranks.values())) > 1:
                spread_order = sorted(open_lines,
                                      key=lambda l: ranks[id(l)])
        # Allocate first (in spread order), then add rows in PO order so
        # the on-screen rows always mirror the PO.
        alloc = {}
        for line in spread_order:
            m = matches.get(id(line))
            if m is not None:
                # The invoice's stated amount for this line — filled even
                # above the line's open balance (the choice dialog below
                # asks; the number itself is never substituted).
                alloc[id(line)] = round(m["amount"], 2)
            elif remaining is None:
                # No readable invoice amount for this line: 0.00, loudly.
                # Auto-filling the PO's amount would bill what the PO says
                # instead of what the invoice says.
                alloc[id(line)] = None
            else:
                amt = round(min(line["open_amount"], remaining), 2)
                remaining = round(remaining - amt, 2)
                alloc[id(line)] = amt
        added_rows = []
        skipped = 0
        unknown = 0
        for line in open_lines:
            m = matches.get(id(line))
            amt = alloc[id(line)]
            if m is None and amt is None:
                amt = 0.0
                unknown += 1
            elif m is None and amt <= 0:
                skipped += 1
                continue
            row = self.add_line(apply_defaults=False)
            row.set_link(po, line)
            row.amount.set(f"{amt:.2f}")
            added_rows.append((row, line))

        # Non-PO rows the user already added default to the PO's job too.
        if primary_job:
            for row in self.rows:
                if not row.link and not row.customer.get().strip():
                    row.customer.set(primary_job)

        self._check_line_descriptions(po, added_rows)

        msg = (f'Linked to PO {po["ref_number"]}: {len(added_rows)} line(s) '
               f'added (PO open balance {po["open_total"]:,.2f} of '
               f'{po["subtotal"]:,.2f}).')
        if len(self.linked_pos) > 1:
            msg += (f' {len(self.linked_pos)} POs are now linked to this '
                    f'bill: '
                    + ", ".join(p["ref_number"] for p in self.linked_pos)
                    + '.')
        if po_amt is not None:
            msg += (f' Amount from this invoice\'s per-PO billing table: '
                    f'{po_amt:,.2f} CURRENT for {po["ref_number"]} — this '
                    f'PO\'s own share, not the invoice total.')
            if po_amt < 0:
                msg += (' Negative = a credit/back-out; a linked PO line '
                        'can\'t carry it — add a normal line with the '
                        'negative amount if it belongs on this bill.')
            elif remaining and remaining > 0.005:
                msg += (f' {remaining:,.2f} of it exceeds what this PO '
                        f'has open — see the choice dialog.')
        elif bill_amt is not None:
            if remaining and remaining > 0.005:
                msg += (f' {remaining:,.2f} of the bill\'s pre-tax '
                        f'{bill_amt:,.2f} is still unallocated — ')
                if self._unlinked_matches():
                    msg += ('link the next matched PO for it, or add a '
                            'normal line.')
                elif matches:
                    msg += ('add a normal line for it — the linked lines '
                            'carry the invoice\'s own per-line amounts, '
                            'so nothing was dumped onto them.')
                else:
                    msg += 'see the choice dialog.'
            else:
                msg += (f' The bill\'s pre-tax {bill_amt:,.2f} is now '
                        f'fully allocated.')
        if bill_amt is not None and skipped:
            msg += f' {skipped} PO line(s) left for a future bill.'
        msg += (' Edit any amount down for a partial bill — the PO stays '
                'open for whatever remains; a line set to 0.00 is simply '
                'not billed this time. On PO lines the amount and Tax are '
                'yours to edit (Tax starts as the PO line\'s code — pick '
                'a different one to override it); item and Customer:Job '
                'flow from the PO itself.')
        self.log(msg)

        # Say exactly where each matched line's number came from: the
        # invoice's amount, the PO line's open amount, and what that
        # leaves — the user always sees both real numbers.
        for row, line in added_rows:
            m = matches.get(id(line))
            if not m:
                continue
            desc = line["desc"] or line["item"]
            left = round(line["open_amount"] - m["amount"], 2)
            if left > 0.005:
                note = f'{left:,.2f} stays open on the PO'
            elif left >= -0.005:
                note = 'exactly its open balance — the line closes'
            else:
                note = f'{-left:,.2f} OVER its open balance'
            self.log(f'PO line "{desc}": filled with {m["amount"]:,.2f} '
                     f'from the invoice\'s own line "{m["label"]}" '
                     f'(PO line open {line["open_amount"]:,.2f} — {note}).')
        if po_amt is None and bill_amt is not None:
            unmatched = [l for _, l in added_rows if id(l) not in matches]
            if unmatched:
                self.log('No invoice line matched by description for '
                         + "; ".join(f'"{l["desc"] or l["item"]}"'
                                     for l in unmatched)
                         + ' — their amounts come from spreading the '
                           'invoice\'s pre-tax total across the PO lines; '
                           'check each against the invoice.')
        if unknown:
            self.log(f'WARNING: no readable invoice amount for {unknown} '
                     f'linked line(s) — they were added at 0.00, NOT the '
                     f'PO\'s amount. Type each amount from the invoice '
                     f'(a line left at 0.00 is simply not billed).')
        self.refresh_totals()

        # The invoice's own amount for a matched line can exceed what that
        # line has open — explicit per-line choice, never silent (and the
        # invoice's number stays on screen either way).
        for row, line in added_rows:
            m = matches.get(id(line))
            if m and m["amount"] > line["open_amount"] + 0.005:
                if not self._offer_line_over_choice(po, row, line, m):
                    return  # cancelled — the PO was unlinked

        # The bill exceeds everything the linked POs have open, and no
        # other matched PO is left to absorb it — the user decides:
        # over-receipt (close the PO with the overage) or cap (leave the
        # overage unbilled). Never a silent decision. When the amount came
        # from the invoice's per-PO table, the overage is by definition
        # THIS PO's (other matched POs have their own table amounts), so
        # the choice is offered right away. Not offered when lines carry
        # the invoice's own per-line amounts: leftover money then belongs
        # to something else, never dumped onto an invoice-matched line.
        if (remaining is not None and remaining > 0.005 and added_rows
                and not matches
                and (po_amt is not None or not self._unlinked_matches())):
            self._offer_over_po_choice(po, added_rows, remaining)

    def _match_invoice_lines(self, lines):
        """Match open PO lines to the invoice's OWN line items (the parsed
        label+amount charge candidates) by description, so a linked row
        gets the amount the invoice actually bills for that work — the PO
        line's amount is only the open ceiling, never the fill.
        Conservative on purpose: descriptions must share at least two
        meaningful words (concept match — word order, grammar and extra
        surrounding words don't matter), an amount is never guessed
        between equally-plausible candidates, and nothing is matched when
        the invoice carries a per-PO billing table (the table already
        states the invoice's own amounts, and its rows would double as
        bogus candidates here). Returns {id(po_line): candidate}."""
        parsed = self.parsed or {}
        if parsed.get("doc_type") != "bill" or parsed.get("po_table_rows"):
            return {}
        pre_tax = parsed.get("pre_tax_total") or 0.0
        candidates = [
            c for c in (parsed.get("charge_candidates") or [])
            if c["amount"] > 0
            # Progress-invoice context ("Store design $10,695.00" original
            # value, "Minus invoiced to date", "Remaining …") is never
            # what THIS invoice bills, however well the description fits.
            and not HISTORY_LABEL.search(c["label"])
            # One line can't bill more than the whole invoice's pre-tax
            # total — bigger numbers are contract values, not charges.
            and (pre_tax <= 0 or c["amount"] <= pre_tax + 0.005)]
        matches, used = {}, set()
        for line in lines:
            desc = line["desc"] or line["item"]
            if len(desc_words(desc)) < 2:
                continue  # one generic word can't identify the work
            scored = [(desc_similarity(desc, c["label"]), i)
                      for i, c in enumerate(candidates) if i not in used]
            best = max((s for s, _ in scored), default=0.0)
            if best < 0.6:
                continue
            top = [candidates[i] for s, i in scored if s == best]
            amounts = {round(c["amount"], 2) for c in top}
            if len(amounts) > 1:
                self.log(f'PO line "{desc}": several invoice lines match '
                         f'its description equally well with different '
                         f'amounts ('
                         + ", ".join(f"{a:,.2f}" for a in sorted(amounts))
                         + ') — not guessing between them; check the '
                           'filled amount against the invoice.')
                continue
            idx = next(i for s, i in scored if s == best)
            used.add(idx)
            matches[id(line)] = candidates[idx]
        return matches

    def _offer_line_over_choice(self, po, row, line, cand) -> bool:
        """Explicit user choice when the invoice's own amount for one PO
        line exceeds what that line has open. The invoice's number is
        already filled in — this decides whether it stays (over-receipt)
        or is capped; never a silent substitution either way. Returns
        False when the user cancels (the PO gets unlinked)."""
        desc = line["desc"] or line["item"]
        over = round(cand["amount"] - line["open_amount"], 2)
        choice = messagebox.askyesnocancel(
            "Invoice amount exceeds the PO line's open balance",
            f'This invoice bills {cand["amount"]:,.2f} for '
            f'"{cand["label"]}", but the matching PO line "{desc}" only '
            f'has {line["open_amount"]:,.2f} open — {over:,.2f} over.\n\n'
            f'Yes — bill the invoice\'s {cand["amount"]:,.2f}: QuickBooks '
            f'records the {over:,.2f} as an over-receipt and closes that '
            f'PO line.\n\n'
            f'No — cap this line at the {line["open_amount"]:,.2f} open '
            f'balance: the {over:,.2f} stays unbilled here (add a normal '
            f'line for it, or bill it later).\n\n'
            f'Cancel — undo linking PO {po["ref_number"]}.')
        if choice is None:
            self._remove_po_link(po)
            self.log(f'Linking PO {po["ref_number"]} cancelled.')
            return False
        if choice:
            self.log(f'Your choice: line "{desc}" billed at the invoice\'s '
                     f'{cand["amount"]:,.2f} — {over:,.2f} above its open '
                     f'{line["open_amount"]:,.2f}; QuickBooks will close '
                     f'it as an over-receipt.')
        else:
            row.amount.set(f'{line["open_amount"]:.2f}')
            self.log(f'Your choice: line "{desc}" capped at its open '
                     f'{line["open_amount"]:,.2f} — the invoice bills '
                     f'{cand["amount"]:,.2f} for it, so {over:,.2f} is '
                     f'left unbilled here.')
        self.refresh_totals()
        return True

    def _po_table_amount(self, po):
        """This PO's own billed amount from the invoice's per-PO billing
        table (its rows' CURRENT column, summed), or None when the invoice
        has no such table or this PO isn't in it — the caller then falls
        back to spreading the invoice's pre-tax total."""
        amounts = (self.parsed or {}).get("po_current_amounts") or {}
        ref = (po["ref_number"] or "").strip().lower()
        if not ref:
            return None
        for number, amount in amounts.items():
            if number.strip().lower() == ref:
                return amount
        return None

    def _unlinked_matches(self) -> bool:
        """Any open PO whose number appears in the PDF but isn't linked yet?
        While one exists, unallocated bill amount likely belongs to IT, so
        no over-receipt question is asked yet."""
        pdf_text = (self.parsed or {}).get("text", "")
        if self.pdf_path:
            pdf_text += "\n" + self.pdf_path.name
        pdf_pos = {p.strip().lower() for p in
                   ((self.parsed or {}).get("po_numbers") or [])}
        linked_ids = {p["txn_id"] for p in self.linked_pos}
        for po in self.open_pos:
            if po["txn_id"] in linked_ids:
                continue
            ref = (po["ref_number"] or "").strip()
            if ref and (ref.lower() in pdf_pos
                        or (len(ref) >= 3 and pdf_text and re.search(
                            rf"(?<![\d-]){re.escape(ref)}(?![\d-])",
                            pdf_text))):
                return True
        return False

    def _offer_over_po_choice(self, po, added_rows, overage: float):
        """Explicit user choice for a bill that exceeds the PO's open
        balance: bill it all (over-receipt closes the PO line) or cap at
        the open balance (remainder billed later some other way)."""
        row, line = added_rows[-1]
        choice = messagebox.askyesnocancel(
            "Bill exceeds the PO's open balance",
            f'This bill has {overage:,.2f} more than PO {po["ref_number"]} '
            f'has open ({po["open_total"]:,.2f}).\n\n'
            f'Yes — bill the FULL amount against this PO: the overage goes '
            f'on line "{line["item"] or line["desc"]}", QuickBooks records '
            f'an over-receipt and closes that PO line.\n\n'
            f'No — CAP at the PO\'s open balance: the {overage:,.2f} stays '
            f'unbilled here (add a normal line for it, or bill it later).\n\n'
            f'Cancel — undo linking this PO.')
        if choice is None:
            self._remove_po_link(po)
            self.log(f'Linking PO {po["ref_number"]} cancelled.')
            return
        if choice:
            try:
                current = float(row.amount.get()
                                .replace(",", "").replace("$", ""))
            except ValueError:
                current = 0.0
            row.amount.set(f"{current + overage:.2f}")
            self.log(f'Your choice: bill the full amount — line '
                     f'"{line["item"] or line["desc"]}" set to '
                     f'{current + overage:,.2f} ({overage:,.2f} above its '
                     f'open balance; QuickBooks will close it as an '
                     f'over-receipt).')
        else:
            self.log(f'Your choice: capped at PO {po["ref_number"]}\'s open '
                     f'balance — {overage:,.2f} left unbilled here (the '
                     f'lines total will show the gap until you add a normal '
                     f'line for it).')
        self.refresh_totals()

    def _check_line_descriptions(self, po, added_rows):
        """Soft sanity check (never a blocker): does each linked PO line's
        description look like something this invoice actually bills? A PO
        line whose words don't appear anywhere in the PDF text gets a red
        "⚠ desc?" flag — usually a sign the amount spread landed on the
        wrong PO line and should be moved."""
        pdf_text = (self.parsed or {}).get("text", "")
        if len(pdf_text.strip()) < 100:
            return  # image-only scan — no text to check against
        suspects = []
        for row, line in added_rows:
            desc = line["desc"] or line["item"]
            if desc and desc_match_ratio(desc, pdf_text) < 0.5:
                row.warn_label.config(text="⚠ desc?")
                suspects.append(desc)
        if suspects:
            self.log(f'REVIEW (PO {po["ref_number"]}): these linked PO '
                     f'line descriptions don\'t obviously appear in this '
                     f'invoice: '
                     + "; ".join(f'"{s}"' for s in suspects) +
                     '. Wording can simply differ — but make sure the '
                     'amounts are on the right PO lines (set a line to '
                     '0.00 to skip it) before entering.')
            self.refresh_totals()  # light the always-visible ⚠ summary

    def _remove_po_link(self, po):
        """Detach one PO: drop its rows, keep any other linked POs."""
        self.linked_pos = [p for p in self.linked_pos
                           if p["txn_id"] != po["txn_id"]]
        for row in [r for r in self.rows
                    if r.link and r.link["txn_id"] == po["txn_id"]]:
            self.remove_line(row)
        if not self.rows:
            row = self.add_line()
            row.prefilled = True
            if (self.parsed and self.parsed["doc_type"] == "bill"
                    and self.parsed["pre_tax_total"]):
                row.amount.set(f'{self.parsed["pre_tax_total"]:.2f}')
        self.refresh_totals()

    def unlink_po(self, quiet: bool = False):
        """Unlink every linked PO and reset to normal entry."""
        if not self.linked_pos:
            return
        refs = ", ".join(p["ref_number"] for p in self.linked_pos)
        self.linked_pos = []
        for row in [r for r in self.rows if r.link]:
            self.remove_line(row)
        if not self.rows:
            row = self.add_line()
            row.prefilled = True
            if (self.parsed and self.parsed["doc_type"] == "bill"
                    and self.parsed["pre_tax_total"]):
                row.amount.set(f'{self.parsed["pre_tax_total"]:.2f}')
        if not quiet:
            self.log(f"Unlinked PO(s) {refs}; lines reset to normal entry.")
        self.refresh_totals()

    # ---------- lines ----------

    def add_line(self, apply_defaults: bool = True) -> LineRow:
        row = LineRow(self, self.lines_container)
        row.frame.pack(fill="x", pady=2)
        self.rows.append(row)
        d = self.vendor_defaults.get(self.vendor.get().strip()) \
            if apply_defaults else None
        if d:  # last-used choice for this vendor, purely a convenience.
            # Tax is deliberately NOT defaulted — the tax code is the
            # user's pick from the live dropdown on every bill.
            if d.get("kind") in LINE_KINDS:
                row.kind.set(d["kind"])
                row._kind_changed()
            if d.get("name") in row._all_names():
                row.name.set(d["name"])
        return row

    def remove_line(self, row: LineRow):
        row.frame.destroy()
        self.rows.remove(row)
        self.refresh_totals()

    def add_candidate(self, _event=None):
        sel = self.cand_list.curselection()
        if not sel or not self.parsed:
            return
        c = self.parsed["charge_candidates"][sel[0]]
        # Reuse the single empty pre-filled row before growing the list.
        row = None
        for r in self.rows:
            if not r.name.get().strip() and not r.memo.get().strip():
                row = r
                break
        row = row or self.add_line()
        row.amount.set(f'{c["amount"]:.2f}')
        row.memo.set(c["label"])
        self.refresh_totals()

    def _flagged_rows(self):
        """Linked rows carrying an active "⚠ desc?" flag AND an amount
        that would actually be billed — entry must confirm these
        explicitly (a 0.00 row isn't billed, so its flag is moot).
        Tk gotcha: cget can return a Tcl object — always str() it."""
        flagged = []
        for row in self.rows:
            if not (row.link and str(row.warn_label.cget("text")).strip()):
                continue
            try:
                v = float(row.amount.get().replace(",", "").replace("$", ""))
            except ValueError:
                continue
            if abs(v) > 0.005:
                flagged.append((row, v))
        return flagged

    def refresh_totals(self):
        # Mismatch summary first — it must track every amount edit (a
        # flagged row set to 0.00 stops being billed, so it un-flags).
        if hasattr(self, "warn_summary"):
            flagged = self._flagged_rows()
            self.warn_summary.config(text=(
                f"⚠ {len(flagged)} linked line(s) flagged 'desc?' — "
                f"review before entering" if flagged else ""))
        amounts = []
        for row in self.rows:
            try:
                amounts.append(
                    float(row.amount.get().replace(",", "").replace("$", "")))
            except ValueError:
                pass
        total = sum(amounts)

        if self.parsed and self.parsed["doc_type"] == "statement":
            s = self.parsed["statement"]
            charges = sum(a for a in amounts if a > 0)
            credits = sum(a for a in amounts if a < 0)
            target = s["stated_purchases"] or s["charges_total"]
            ok = abs(charges - target) < 0.005
            self.totals_label.config(
                text=f'Charges: {charges:.2f} vs statement {target:.2f} '
                     f'{"✓" if ok else "✗ MISMATCH"}   |   '
                     f"credits: {credits:.2f}   |   bill total: {total:.2f}",
                foreground="dark green" if ok else "dark orange")
            return

        text = f"Lines total: {total:.2f}"
        color = "black"
        if self.parsed and self.parsed["total"]:
            target = self.parsed["pre_tax_total"]
            text += (f"   |   PDF pre-tax: {target:.2f}   "
                     f'PDF total (incl. tax): {self.parsed["total"]:.2f}')
            color = "dark green" if abs(total - target) < 0.005 else "dark orange"
        self.totals_label.config(text=text, foreground=color)

    # ---------- entry ----------

    def log(self, message: str):
        self.status.config(state="normal")
        self.status.insert("end", message + "\n")
        self.status.see("end")
        self.status.config(state="disabled")

    def enter_bill(self):
        vendor = self.vendor.get().strip()
        if not vendor:
            messagebox.showwarning("Missing vendor", "Pick a vendor from the dropdown.")
            return
        if self.vendors and vendor not in self.vendors:
            if not messagebox.askyesno(
                    "Vendor not in QuickBooks",
                    f'"{vendor}" is not in the vendor list. QuickBooks will '
                    f"reject the bill unless the vendor exists. Try anyway?"):
                return

        date = self.date.get().strip()
        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                messagebox.showwarning("Bad date",
                                       f'Date "{date}" is not YYYY-MM-DD.')
                return

        try:
            lines = [d for d in (row.to_dict() for row in self.rows) if d]
        except ValueError as e:
            messagebox.showwarning("Check lines", str(e).capitalize() + ".")
            return
        if not lines:
            messagebox.showwarning("No lines", "Add at least one line with an "
                                               "Item/Account and an amount.")
            return

        # Reconciliation: a statement whose extracted charges don't add up
        # to its own stated total means lines were missed or edited away.
        # Soft confirmation only — never a blocker.
        if self.parsed and self.parsed["doc_type"] == "statement":
            s = self.parsed["statement"]
            target = s["stated_purchases"] or s["charges_total"]
            charges = sum(l["amount"] for l in lines if l["amount"] > 0)
            if target and abs(charges - target) > 0.005:
                if not messagebox.askyesno(
                        "Totals don't match the statement",
                        f"The charge lines add up to {charges:.2f}, but the "
                        f"statement says {target:.2f} in purchases/charges "
                        f"(difference {charges - target:+.2f}).\n\n"
                        f"Some transactions may be missing or were removed. "
                        f"Enter the bill anyway?"):
                    return

        # Billing more than a linked PO line has open is legal in QuickBooks
        # (an over-receipt that closes the line) — the user explicitly
        # picks between billing it all and capping at the open balance.
        # Partial amounts pass silently; they simply leave the PO open.
        linked = [l for l in lines if l.get("link")]
        over = [l for l in linked
                if l["amount"] > l.get("open_amount", l["amount"]) + 0.005]
        if over:
            detail = "\n".join(
                f'  {l["name"]}: {l["amount"]:.2f} vs {l["open_amount"]:.2f} open'
                for l in over)
            choice = messagebox.askyesnocancel(
                "Amount exceeds PO open balance",
                f"These lines bill more than the linked PO line has "
                f"open:\n{detail}\n\n"
                f"Yes — bill the FULL amounts: QuickBooks records the "
                f"extra as an over-receipt and closes those PO lines.\n\n"
                f"No — CAP each line at its open balance: the overage "
                f"stays unbilled and the PO stays open for it.\n\n"
                f"Cancel — go back and edit the lines.")
            if choice is None:
                return
            if not choice:
                capped = 0
                for r in self.rows:
                    if not r.link:
                        continue
                    try:
                        v = float(r.amount.get()
                                  .replace(",", "").replace("$", ""))
                    except ValueError:
                        continue
                    if v > r.link["open_amount"] + 0.005:
                        r.amount.set(f'{r.link["open_amount"]:.2f}')
                        capped += 1
                self.log(f"Capped {capped} line(s) at the PO's open "
                         f"balance — the overage stays unbilled and the "
                         f"PO stays open for it.")
                # Rebuild from the capped rows (quantity is derived from
                # the amount, so it must be recomputed too).
                try:
                    lines = [d for d in (row.to_dict() for row in self.rows)
                             if d]
                except ValueError as e:
                    messagebox.showwarning("Check lines",
                                           str(e).capitalize() + ".")
                    return
                linked = [l for l in lines if l.get("link")]
                if not lines:
                    messagebox.showwarning(
                        "No lines", "Nothing left to bill after capping.")
                    return

        # A "⚠ desc?" flag usually means the amount sits on the WRONG PO
        # line, and QuickBooks will receive against exactly the linked
        # line — so entering demands an explicit confirmation that NAMES
        # each flagged PO line (the per-row flag alone proved too easy to
        # click past: live 2026-08-21 a bill consumed "Pre Design
        # Services" instead of "Construction Administration"). Soft by
        # design — wording can legitimately differ from the PO — but the
        # default answer is No.
        flagged = self._flagged_rows()
        if flagged:
            detail = "\n".join(
                f'  • PO {r.link["po_ref"]} line '
                f'"{r.link["desc"] or r.link["item"]}" '
                f'(open {r.link["open_amount"]:,.2f}) ← billing {v:,.2f}'
                for r, v in flagged)
            if not messagebox.askyesno(
                    "⚠ Check the PO line(s) first",
                    f"{len(flagged)} linked line(s) are flagged '⚠ desc?' "
                    f"— the PO line's description doesn't obviously appear "
                    f"in this invoice, which usually means the amount "
                    f"landed on the wrong PO line.\n\n"
                    f"QuickBooks will receive against EXACTLY these PO "
                    f"lines:\n\n{detail}\n\n"
                    f"Yes — these amounts really belong on these PO "
                    f"lines.\n"
                    f"No — go back and fix it (set a wrong line to 0.00 "
                    f"and put its amount on the right PO line).",
                    default=messagebox.NO, icon=messagebox.WARNING):
                self.log("Entry stopped: review the ⚠ flagged line(s) — "
                         "move each amount to the right PO line, or set "
                         "the line to 0.00 to skip it.")
                return

        ref = self.ref.get().strip()
        try:
            if ref and self.qb.bill_exists(vendor, ref):
                if not messagebox.askyesno(
                        "Possible duplicate",
                        f"A bill with ref {ref} already exists for {vendor}. "
                        f"Enter it again anyway?"):
                    return
        except Exception:
            pass  # duplicate check is best-effort, never a blocker

        def fmt(l):
            s = (f'  {l["kind"]:<8} {l["name"]:<28.28} {l["amount"]:>10.2f}  '
                 f'{l["tax_code"]}')
            if l.get("customer"):
                s += f'  ⇒ {l["customer"]}'
            if l.get("link"):
                s += f'  [PO {l["po_ref"]}]'
            return s

        shown = lines[:12]
        summary = "\n".join(fmt(l) for l in shown)
        if len(lines) > len(shown):
            lines_total = sum(l["amount"] for l in lines)
            summary += (f"\n  … and {len(lines) - len(shown)} more lines "
                        f"(total of all {len(lines)} lines: {lines_total:.2f})")
        po_note = ""
        if linked and self.linked_pos:
            po_note = "\nLinked PO(s): " + ", ".join(
                f'{p["ref_number"]} (open {p["open_total"]:,.2f})'
                for p in self.linked_pos) + " — partial billing leaves them open"
        tax_overrides = [l for l in linked
                         if l.get("tax_code")
                         and l["tax_code"] != l.get("po_tax_code", "")]
        if tax_overrides:
            po_note += ("\nTax pick on linked line(s): " + ", ".join(
                f'{l["name"] or l["po_ref"]} → {l["tax_code"]}'
                for l in tax_overrides)
                + " (applied automatically right after entry — the normal "
                  "two-step for PO-linked lines, then verified)")
        if not messagebox.askyesno(
                "Confirm bill",
                f"Vendor: {vendor}\nRef: {ref or '(none)'}   Date: {date or 'today'}"
                f"{po_note}\n\n{summary}\n\nEnter this bill into QuickBooks?"):
            return

        try:
            ok, message = self.qb.add_bill(vendor, ref, date, lines,
                                           memo=self.memo.get().strip())
        except Exception as e:
            ok, message = False, f"QuickBooks error: {e}"
        self.log(message)
        if not ok:
            messagebox.showerror("Not entered", message)
            return

        if linked and self.linked_pos:
            # Trust but verify: ask QuickBooks whether the bill is actually
            # linked to each PO — a dropped LinkToTxn otherwise looks
            # exactly like success.
            recorded = None
            try:
                txn_id = getattr(self.qb, "last_txn_id", "")
                if txn_id and hasattr(self.qb, "bill_linked_txns"):
                    recorded = {t["txn_id"]
                                for t in self.qb.bill_linked_txns(txn_id)}
            except Exception as e:
                self.log(f"Could not verify the PO link(s): {e}")
            unrecorded = []
            for po in self.linked_pos:
                po_ref = po["ref_number"]
                verified = None if recorded is None \
                    else po["txn_id"] in recorded
                if verified is False:
                    unrecorded.append(po_ref)
                    continue
                if verified:
                    self.log(f"QuickBooks confirmed the bill is linked to "
                             f"PO {po_ref}. ✓")
                billed_now = round(sum(
                    l["amount"] for l in linked
                    if l["link"]["txn_id"] == po["txn_id"]), 2)
                po_left = round(po["open_total"] - billed_now, 2)
                if po_left > 0.005:
                    self.log(f'PO {po_ref}: {billed_now:,.2f} billed against '
                             f'it, ~{po_left:,.2f} still open — the PO stays '
                             f'open in QuickBooks.')
                else:
                    self.log(f'PO {po_ref} is now fully billed; QuickBooks '
                             f'will mark it received.')
            if unrecorded:
                warning = (f"The bill was entered, but QuickBooks did NOT "
                           f"record its link to PO(s) "
                           f"{', '.join(unrecorded)} — those POs' open "
                           f"balances are unchanged and no job costing came "
                           f"from them.\n\nThe exact request/response XML "
                           f"was saved as last_qbxml_request.xml / "
                           f"last_qbxml_response.xml next to qb_client.py.")
                self.log("WARNING: " + warning.replace("\n\n", " "))
                messagebox.showwarning("PO link not recorded", warning)

        # Auto-attach the source PDF to the bill (Doc Center staging —
        # best-effort, never blocks; the log says what happened).
        try:
            txn_id = getattr(self.qb, "last_txn_id", "")
            if (self.pdf_path and txn_id
                    and hasattr(self.qb, "attach_file_to_txn")):
                _, attach_msg = self.qb.attach_file_to_txn(
                    str(self.pdf_path), txn_id)
                self.log(attach_msg)
        except Exception as e:
            self.log(f"PDF attach failed: {e} — attach it manually.")

        # Statement lines are per-merchant, so the first line's account says
        # nothing about this vendor (the card issuer); PO-linked lines carry
        # no chosen Item/Account — don't save a default from either. The tax
        # code is deliberately NOT remembered: it's a fresh pick every bill.
        first = next((l for l in lines if not l.get("link")), None)
        if first and not (self.parsed and self.parsed["doc_type"] == "statement"):
            self.vendor_defaults[vendor] = {"kind": first["kind"].capitalize(),
                                            "name": first["name"]}
            save_vendor_defaults(self.vendor_defaults)
        # Severity must match content: a routine entry (including the
        # normal apply-tax-after-entry step) gets a calm info box; only a
        # message carrying a real problem (⚠ / QuickBooks warning) gets
        # the warning dialog and its sound.
        if "⚠" in message or "warning" in message.lower():
            messagebox.showwarning("Entered — check one thing", message)
        else:
            messagebox.showinfo("Entered", message)
        self.linked_pos = []
        self.refresh_pos()  # re-query so open balances reflect this bill
        self._archive_pdf()

    def _archive_pdf(self):
        """If the PDF lives in an inbox/ folder, offer to move it to done/."""
        if not self.pdf_path or self.pdf_path.parent.name.lower() != "inbox":
            return
        done = self.pdf_path.parent.parent / "done"
        if messagebox.askyesno("Move PDF",
                               f"Move {self.pdf_path.name} to {done}?"):
            done.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.pdf_path), str(done / self.pdf_path.name))
            self.log(f"Moved to {done / self.pdf_path.name}")
            self.pdf_path = None


def main():
    qb = None
    if QB_AVAILABLE:
        try:
            qb = QuickBooks()
        except Exception as e:
            print(f"Could not connect to QuickBooks: {e}")
    else:
        print("pywin32 not installed — running in preview mode (no QB entry).")

    app = BillEntryApp(qb)
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if target.is_dir():  # e.g. the OneDrive inbox folder
            pdfs = sorted(target.glob("*.pdf")) or sorted(
                (target / "inbox").glob("*.pdf"))
            if pdfs:
                app.after(200, lambda: app.open_pdf(str(pdfs[0])))
        elif target.exists():
            app.after(200, lambda: app.open_pdf(str(target)))
    try:
        app.mainloop()
    finally:
        if qb:
            qb.close()


if __name__ == "__main__":
    main()
