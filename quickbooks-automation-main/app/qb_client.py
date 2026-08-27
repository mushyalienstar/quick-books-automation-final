"""QuickBooks Desktop client (qbXML over the QBXMLRP2 COM component).

All list data — vendors, items, chart of accounts, sales tax codes — is
queried live from whichever company file is open. Nothing about the
company file (item names, tax codes, provinces, accounts) is hardcoded,
so the same build works against any file.
"""
import re
import shutil
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.sax.saxutils import escape

QB_AVAILABLE = True
try:
    import win32com.client
except ImportError:
    QB_AVAILABLE = False

# Item types that can't go on a bill's Items tab.
NON_BILLABLE_ITEM_TYPES = {
    "ItemSalesTaxRet", "ItemSalesTaxGroupRet", "ItemPaymentRet",
}

# Fallback recognition of a sales-tax item by NAME ("GST (ITC)", "HST on
# purchases", "Sales Tax …"), used ONLY when the live item-type query
# fails — the ItemQuery types above are authoritative whenever available;
# nothing here assumes a specific item exists in the company file.
TAX_ITEM_NAME = re.compile(
    r"^\s*(?:GST|HST|PST|QST|VAT|Sales\s*Tax)\b|\(ITC\)", re.IGNORECASE)


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def po_link_sizing(amount: float, rate: float) -> tuple:
    """(quantity, send_amount) sizing for a PO-linked bill line.

    QuickBooks tracks a PO line's receipt by QUANTITY and recomputes the
    bill line's amount as quantity x rate at 5-decimal quantity
    precision. Each override alone fails a live case (both 2026-08-20):
    Quantity alone drifts cents when no 5dp quantity multiplies back to
    the exact amount (2,500.00 at rate 31,000 → qty 0.08065 → 2,500.15);
    Amount alone posts the right dollars but QuickBooks defaults the
    quantity to 1, misstating the receipt (and closing a qty-1 PO line).
    So: Quantity alone when it multiplies back to the exact cents (the
    live-verified minimal line), Quantity PLUS the exact Amount when it
    can't (QuickBooks keeps the amount and implies the cost), and Amount
    alone only for rate-less lines (no quantity to state).

    The exactness test is DECIMAL arithmetic, and "exact" means the
    product has no sub-cent digits at all — float round() is banker's
    rounding on binary approximations and disagrees with QuickBooks on
    midpoints (live 2026-08-21: 171.25 at rate 1,500 → qty 0.11417 →
    171.255, Python rounded it back to 171.25 so Quantity went alone,
    QuickBooks half-up-rounded the same product to 171.26). When any
    rounding would be needed, the exact Amount rides along instead."""
    if not rate:
        return None, True
    cents = Decimal(str(round(amount, 2)))
    drate = Decimal(str(rate))
    qty = (cents / drate).quantize(Decimal("0.00001"), ROUND_HALF_UP)
    return float(qty), (qty * drate) != cents


def wrap_qbxml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<?qbxml version="13.0"?>\n'
        "<QBXML>\n"
        '    <QBXMLMsgsRq onError="stopOnError">\n'
        f"{body}"
        "    </QBXMLMsgsRq>\n"
        "</QBXML>\n"
    )


def build_bill_add_qbxml(vendor: str, ref_number: str, txn_date: str,
                         lines: list, memo: str = "") -> str:
    """qbXML BillAddRq from generic line dicts (see QuickBooks.add_bill).
    Element order follows the qbXML 13.0 spec — expense lines before item
    lines; per line Amount, then Memo/Desc, CustomerRef, SalesTaxCodeRef,
    LinkToTxn last.

    A line with "link" ({"txn_id", "txn_line_id"}) is billed against that
    purchase-order line and must carry ONLY the LinkToTxn plus the
    override(s) that size the partial receipt — see po_link_sizing:
    Quantity alone when it multiplies back to the exact cent amount at
    the PO rate, Quantity plus the exact Amount when it can't (Quantity
    alone drifts cents, Amount alone makes QuickBooks default the
    receipt quantity to 1 — both live 2026-08-20), Amount alone for
    rate-less lines. NOTHING else may ride on a linked line — three live data points: extra refs → 3153 "parameters
    conflict" or a silently dropped link (2026-07-31), and SalesTaxCodeRef
    next to LinkToTxn → 3210 'The "LinkToTxn" field has an invalid value'
    rejecting the whole bill (2026-08-07). A manual tax pick on a linked
    line (tax_code differing from the line's "po_tax_code") is therefore
    NOT emitted here — QuickBooks.add_bill applies it with a follow-up
    BillMod once the bill and its link exist (see
    build_bill_tax_mod_qbxml). A quantity below the line's open balance
    is a partial receipt — QuickBooks decreases the open quantity and the
    PO stays open. Nothing here ever closes a PO explicitly."""
    expense_xml, item_xml = "", ""
    for line in lines:
        tax = ""
        if line.get("tax_code"):
            tax = (f"\n                <SalesTaxCodeRef>"
                   f"<FullName>{escape(line['tax_code'])}</FullName>"
                   f"</SalesTaxCodeRef>")
        cust = ""
        if line.get("customer"):
            cust = (f"\n                <CustomerRef>"
                    f"<FullName>{escape(line['customer'])}</FullName>"
                    f"</CustomerRef>")
        if line["kind"] == "expense":
            note = ""
            if line.get("memo"):
                note = f"\n                <Memo>{escape(line['memo'])}</Memo>"
            expense_xml += f"""
            <ExpenseLineAdd>
                <AccountRef><FullName>{escape(line['name'])}</FullName></AccountRef>
                <Amount>{line['amount']:.2f}</Amount>{note}{cust}{tax}
            </ExpenseLineAdd>"""
        elif line.get("link"):
            link = line["link"]
            if "po_rate" in line:
                qty, send_amount = po_link_sizing(line["amount"],
                                                  line["po_rate"])
            else:  # legacy dicts: explicit quantity, else exact amount
                qty = line.get("quantity")
                send_amount = not qty
            override = ""
            if qty:
                override += (f"\n                <Quantity>"
                             f"{qty:g}</Quantity>")
            if send_amount:
                override += (f"\n                <Amount>"
                             f"{line['amount']:.2f}</Amount>")
            # Nothing but the sizing override(s) may accompany LinkToTxn —
            # even SalesTaxCodeRef here makes QuickBooks reject the whole
            # bill (status 3210, live 2026-08-07). A manual tax pick on a
            # linked line is applied by add_bill via BillMod afterwards.
            item_xml += f"""
            <ItemLineAdd>{override}
                <LinkToTxn><TxnID>{escape(link['txn_id'])}</TxnID><TxnLineID>{escape(link['txn_line_id'])}</TxnLineID></LinkToTxn>
            </ItemLineAdd>"""
        else:
            note = ""
            if line.get("memo"):
                note = f"\n                <Desc>{escape(line['memo'])}</Desc>"
            item_xml += f"""
            <ItemLineAdd>
                <ItemRef><FullName>{escape(line['name'])}</FullName></ItemRef>{note}
                <Amount>{line['amount']:.2f}</Amount>{cust}{tax}
            </ItemLineAdd>"""

    memo_xml = f"\n                <Memo>{escape(memo)}</Memo>" if memo else ""
    date_xml = f"\n                <TxnDate>{txn_date}</TxnDate>" if txn_date else ""
    ref_xml = (f"\n                <RefNumber>{escape(ref_number)}</RefNumber>"
               if ref_number else "")
    body = f"""        <BillAddRq>
            <BillAdd>
                <VendorRef><FullName>{escape(vendor)}</FullName></VendorRef>{date_xml}{ref_xml}{memo_xml}{expense_xml}{item_xml}
            </BillAdd>
        </BillAddRq>
"""
    return wrap_qbxml(body)


def build_bill_tax_mod_qbxml(txn_id: str, edit_sequence: str,
                             expense_line_ids: list,
                             item_line_mods: list) -> str:
    """BillModRq that ONLY sets sales-tax codes on existing bill lines —
    the second step for a manual tax pick on a PO-linked line, which
    QuickBooks refuses to take together with LinkToTxn in the BillAdd.

    QuickBooks Mod semantics: once ANY line mod appears in the request,
    every line to keep must appear too — a line listed with just its
    TxnLineID passes through unchanged, an omitted line is DELETED. So all
    expense lines and all item lines are always listed; item_line_mods is
    [{"txn_line_id": ..., "tax_code": <set when this line changes>}]."""
    lines_xml = ""
    for line_id in expense_line_ids:
        lines_xml += (f"\n                <ExpenseLineMod>"
                      f"<TxnLineID>{escape(line_id)}</TxnLineID>"
                      f"</ExpenseLineMod>")
    for mod in item_line_mods:
        tax = ""
        if mod.get("tax_code"):
            tax = (f"<SalesTaxCodeRef><FullName>{escape(mod['tax_code'])}"
                   f"</FullName></SalesTaxCodeRef>")
        lines_xml += (f"\n                <ItemLineMod>"
                      f"<TxnLineID>{escape(mod['txn_line_id'])}</TxnLineID>"
                      f"{tax}</ItemLineMod>")
    body = f"""        <BillModRq>
            <BillMod>
                <TxnID>{escape(txn_id)}</TxnID>
                <EditSequence>{escape(edit_sequence)}</EditSequence>{lines_xml}
            </BillMod>
        </BillModRq>
"""
    return wrap_qbxml(body)


class QuickBooks:
    """One open session against the currently-open company file."""

    def __init__(self, app_name: str = "Bill Entry"):
        if not QB_AVAILABLE:
            raise RuntimeError("pywin32 is not installed — run `pip install "
                               "pywin32` on the machine with QuickBooks Desktop.")
        self.qb = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
        self.qb.OpenConnection2("", app_name, 1)
        self.ticket = self.qb.BeginSession("", 2)
        self.last_txn_id = ""  # TxnID of the most recently added bill

    def request(self, qbxml: str) -> str:
        return self.qb.ProcessRequest(self.ticket, qbxml)

    def close(self):
        try:
            self.qb.EndSession(self.ticket)
        finally:
            self.qb.CloseConnection()

    def host_info(self) -> str:
        try:
            root = ET.fromstring(self.request(wrap_qbxml("        <HostQueryRq/>\n")))
            ret = root.find(".//HostRet")
            if ret is None:
                return ""
            return ret.findtext("ProductName", "")
        except Exception:
            return ""

    def company_names(self) -> list:
        """CompanyName + LegalCompanyName of the OPEN company file, live
        (never hardcoded) and cached for the session. This is the bill-to
        party on every incoming invoice — vendor guessing excludes it so
        the user's own company can never be picked as the vendor, even
        when it also exists as a vendor record. [] on failure (the
        safeguard is then simply unavailable, never a blocker)."""
        cached = getattr(self, "_company_names_cache", None)
        if cached is not None:
            return cached
        names = []
        try:
            root = ET.fromstring(self.request(wrap_qbxml(
                "        <CompanyQueryRq/>\n")))
            ret = root.find(".//CompanyRet")
            for tag in ("CompanyName", "LegalCompanyName"):
                name = (ret.findtext(tag) or "").strip() \
                    if ret is not None else ""
                if name and name not in names:
                    names.append(name)
        except Exception:
            pass
        # Second, independent source: the company FILE's name — .QBW
        # files are conventionally named after the company ("K Paul
        # Architect Inc.QBW"), and GetCurrentCompanyFileName can work
        # when CompanyQueryRq doesn't. Either source alone keeps the
        # own-company exclusion alive (live 2026-08-21: a silently empty
        # exclusion reverted vendor guessing to picking the user's own
        # company from the bill-to block).
        try:
            stem = Path(self.company_file_path()).stem.strip()
        except Exception:
            stem = ""
        if stem and stem not in names:
            names.append(stem)
        self._company_names_cache = names
        return names

    def _query_names(self, rq_tag: str, extra: str = "",
                     skip_ret_tags: set = frozenset()) -> list:
        """FullName/Name of every entry a list query returns."""
        qbxml = wrap_qbxml(f"        <{rq_tag}>\n{extra}        </{rq_tag}>\n")
        root = ET.fromstring(self.request(qbxml))
        rs = root.find(f".//{rq_tag[:-2]}Rs")
        names = []
        if rs is None:
            return names
        for ret in rs:
            if ret.tag in skip_ret_tags:
                continue
            name = ret.findtext("FullName") or ret.findtext("Name")
            if name and name not in names:
                names.append(name)
        return names

    # ---- live lists (the source of truth for the entry form's dropdowns) ----

    def vendors(self) -> list:
        return sorted(self._query_names(
            "VendorQueryRq", "            <ActiveStatus>ActiveOnly</ActiveStatus>\n"),
            key=str.lower)

    def items(self) -> list:
        return sorted(self._query_names(
            "ItemQueryRq", "            <ActiveStatus>ActiveOnly</ActiveStatus>\n",
            skip_ret_tags=NON_BILLABLE_ITEM_TYPES), key=str.lower)

    def accounts(self) -> list:
        return sorted(self._query_names(
            "AccountQueryRq", "            <ActiveStatus>ActiveOnly</ActiveStatus>\n"),
            key=str.lower)

    def tax_codes(self) -> list:
        return sorted(self._query_names(
            "SalesTaxCodeQueryRq",
            "            <ActiveStatus>ActiveOnly</ActiveStatus>\n"), key=str.lower)

    def customers(self) -> list:
        """Customer and Customer:Job FullNames, for per-line job costing."""
        return sorted(self._query_names(
            "CustomerQueryRq", "            <ActiveStatus>ActiveOnly</ActiveStatus>\n"),
            key=str.lower)

    # ---- purchase orders ----

    def _non_billable_item_names(self):
        """FullNames of the company file's non-billable-type items (sales
        tax items, tax groups, payment items) — the PO lines that must
        never become bill lines. Queried live (types, not names) and
        cached for the session; None when the query fails, so callers
        fall back to name recognition."""
        cached = getattr(self, "_non_billable_cache", False)
        if cached is not False:
            return cached
        try:
            root = ET.fromstring(self.request(wrap_qbxml(
                "        <ItemQueryRq>\n        </ItemQueryRq>\n")))
            rs = root.find(".//ItemQueryRs")
            names = set()
            for ret in (rs if rs is not None else []):
                if ret.tag in NON_BILLABLE_ITEM_TYPES:
                    name = ret.findtext("FullName") or ret.findtext("Name")
                    if name:
                        names.add(name)
            self._non_billable_cache = names
        except Exception:
            self._non_billable_cache = None
        return self._non_billable_cache

    def open_purchase_orders(self, vendor: str) -> list:
        """Open purchase orders for a vendor (not fully received, not
        manually closed), newest first, each with per-line open balances.

        Open amounts mirror QuickBooks' native partial-receipt tracking:
        a line's open amount is (Quantity - ReceivedQuantity) x Rate. Lines
        without a quantity are treated as fully open (best effort).
        "subtotal" is the sum of line amounts (pre-tax, comparable to bill
        lines); "total" is the PO's TotalAmount (may include tax).

        The vendor's NOT-open POs (fully received / manually closed) from
        the same query land in self.last_closed_pos — a PO that a bill
        (even a deleted-and-re-entered test one) received in full simply
        stops being open, and "the panel doesn't show my PO" needs the
        tool to say that instead of silently filtering it out."""
        self.last_closed_pos = []
        if not vendor:
            return []
        qbxml = wrap_qbxml(
            "        <PurchaseOrderQueryRq>\n"
            "            <MaxReturned>500</MaxReturned>\n"
            "            <EntityFilter>\n"
            f"                <FullName>{escape(vendor)}</FullName>\n"
            "            </EntityFilter>\n"
            "            <IncludeLineItems>true</IncludeLineItems>\n"
            "        </PurchaseOrderQueryRq>\n")
        root = ET.fromstring(self.request(qbxml))
        non_billable = self._non_billable_item_names()
        pos = []
        for ret in root.findall(".//PurchaseOrderRet"):
            manually_closed = ret.findtext("IsManuallyClosed") == "true"
            if manually_closed or ret.findtext("IsFullyReceived") == "true":
                self.last_closed_pos.append({
                    "txn_id": ret.findtext("TxnID", ""),
                    "vendor": vendor,
                    "ref_number": ret.findtext("RefNumber", ""),
                    "txn_date": ret.findtext("TxnDate", ""),
                    "total": _f(ret.findtext("TotalAmount")),
                    "status": ("manually closed" if manually_closed
                               else "fully received"),
                })
                continue
            lines = []
            for lr in ret.findall("PurchaseOrderLineRet"):
                amount = _f(lr.findtext("Amount"))
                qty = _f(lr.findtext("Quantity"))
                rcvd = _f(lr.findtext("ReceivedQuantity"))
                rate = _f(lr.findtext("Rate"))
                if lr.findtext("IsManuallyClosed") == "true":
                    open_amount = 0.0
                elif qty:
                    per_unit = rate if rate else amount / qty
                    open_amount = round(max(qty - rcvd, 0.0) * per_unit, 2)
                else:
                    open_amount = amount
                item_name = lr.findtext("ItemRef/FullName", "")
                # A sales-tax ITEM line on the PO ("GST (ITC)") is not
                # work to bill — flagged here so linking can skip it (tax
                # on a bill is the Tax column, the user's pick per line).
                if non_billable is not None:
                    is_tax_line = item_name in non_billable
                else:
                    is_tax_line = bool(TAX_ITEM_NAME.search(item_name))
                lines.append({
                    "txn_line_id": lr.findtext("TxnLineID", ""),
                    "item": item_name,
                    "is_tax_line": is_tax_line,
                    "desc": lr.findtext("Desc", ""),
                    "quantity": qty, "received": rcvd, "rate": rate,
                    "amount": amount, "open_amount": open_amount,
                    "customer_job": lr.findtext("CustomerRef/FullName", ""),
                    "tax_code": lr.findtext("SalesTaxCodeRef/FullName", ""),
                })
            subtotal = round(sum(l["amount"] for l in lines), 2)
            open_total = round(sum(l["open_amount"] for l in lines), 2)
            jobs = []
            for l in lines:
                if l["customer_job"] and l["customer_job"] not in jobs:
                    jobs.append(l["customer_job"])
            pos.append({
                "txn_id": ret.findtext("TxnID", ""),
                "vendor": vendor,
                "ref_number": ret.findtext("RefNumber", ""),
                "txn_date": ret.findtext("TxnDate", ""),
                "total": _f(ret.findtext("TotalAmount")),
                "subtotal": subtotal,
                "open_total": open_total,
                "billed_total": round(max(subtotal - open_total, 0.0), 2),
                "memo": ret.findtext("Memo", ""),
                "customer_jobs": jobs,
                "lines": lines,
            })
        pos.sort(key=lambda p: p["txn_date"], reverse=True)
        return pos

    def find_pos_by_ref(self, refs: list) -> list:
        """Locate POs by their printed number ANYWHERE in the company file
        — any vendor, open or not. The diagnostic behind "the invoice
        references a PO the panel doesn't show": it says which vendor the
        PO actually belongs to (down to punctuation — "Inc." vs "Inc" is
        a different vendor to the EntityFilter) and whether it is still
        open. RefNumber is an exact, case-insensitive match."""
        refs = [r.strip() for r in refs if r and r.strip()]
        if not refs:
            return []
        ref_xml = "".join(
            f"            <RefNumber>{escape(r)}</RefNumber>\n" for r in refs)
        qbxml = wrap_qbxml(
            "        <PurchaseOrderQueryRq>\n"
            + ref_xml +
            "        </PurchaseOrderQueryRq>\n")
        root = ET.fromstring(self.request(qbxml))
        found = []
        for ret in root.findall(".//PurchaseOrderRet"):
            manually_closed = ret.findtext("IsManuallyClosed") == "true"
            fully_received = ret.findtext("IsFullyReceived") == "true"
            found.append({
                "txn_id": ret.findtext("TxnID", ""),
                "ref_number": ret.findtext("RefNumber", ""),
                "vendor": ret.findtext("VendorRef/FullName", ""),
                "txn_date": ret.findtext("TxnDate", ""),
                "total": _f(ret.findtext("TotalAmount")),
                "status": ("manually closed" if manually_closed
                           else "fully received" if fully_received
                           else "open"),
            })
        return found

    # ---- bill entry ----

    def bill_exists(self, vendor: str, ref_number: str) -> bool:
        """True if this vendor already has a bill with this ref number."""
        if not ref_number:
            return False
        qbxml = wrap_qbxml(
            "        <BillQueryRq>\n"
            f"            <RefNumber>{escape(ref_number)}</RefNumber>\n"
            "        </BillQueryRq>\n")
        root = ET.fromstring(self.request(qbxml))
        for ret in root.findall(".//BillRet"):
            if not vendor or ret.findtext("VendorRef/FullName") == vendor:
                return True
        return False

    def add_bill(self, vendor: str, ref_number: str, txn_date: str,
                 lines: list, memo: str = "") -> tuple:
        """Enter a vendor bill. Each line is a dict:
            {"kind": "item"|"expense", "name": <Item/Account FullName>,
             "amount": float, "tax_code": str (optional), "memo": str (optional),
             "customer": <Customer:Job FullName> (optional),
             "link": {"txn_id", "txn_line_id"} (optional — bill against that
                     PO line; a partial amount leaves the PO open)}
        Returns (ok, message)."""
        qbxml = build_bill_add_qbxml(vendor, ref_number, txn_date, lines, memo)
        self.last_txn_id = ""
        response = self.request(qbxml)
        # Always keep the last exchange on disk — when QuickBooks accepts a
        # bill but silently drops something (e.g. a PO link), the raw XML is
        # the only way to see what actually happened.
        self._dump("last_qbxml_request.xml", qbxml)
        self._dump("last_qbxml_response.xml", response)
        rs = ET.fromstring(response).find(".//BillAddRs")
        if rs is None:
            return False, f"Unexpected response:\n{response}"
        ret = rs.find("BillRet")
        if ret is not None:
            self.last_txn_id = ret.findtext("TxnID", "")
        status = rs.get("statusCode")
        if status == "0" or ret is not None:
            # A BillRet with a non-zero status is a warning: the bill WAS
            # entered, but QuickBooks changed or dropped something — say so.
            total = ret.findtext("AmountDue", "") if ret is not None else ""
            suffix = f" (bill total {total})" if total else ""
            message = f"Bill added to QuickBooks{suffix}."
            if status != "0":
                message += (f" QuickBooks warning (status {status}): "
                            f"{rs.get('statusMessage')}")
            tax_note = self._apply_linked_tax_overrides(lines, ret)
            if tax_note:
                message += " " + tax_note
            return True, message
        message = (f"QuickBooks rejected the bill "
                   f"(status {status}): {rs.get('statusMessage')}")
        if status == "3176":
            # QuickBooks can't lock a transaction that's open on screen —
            # linking a bill to a PO needs to update that PO.
            message += ("\n\nClose the purchase order (and any other "
                        "transaction windows) in QuickBooks — Window menu → "
                        "Close All — then enter the bill again.")
        return False, message

    def _apply_linked_tax_overrides(self, lines: list, bill_ret) -> str:
        """Second step of a manual tax pick on a PO-linked line: QuickBooks
        rejects the whole bill when SalesTaxCodeRef accompanies LinkToTxn
        (status 3210, live 2026-08-07), so the code is set afterwards with
        a BillMod on the just-added bill. Best-effort — the bill and its
        PO link are already in; returns a note for the entry log ('' when
        no linked line has a tax pick to apply). Runs BEFORE the GUI's
        post-entry link verification, which will also catch a link broken
        by the mod itself."""
        overrides = [l for l in lines if l.get("link") and l.get("tax_code")
                     and l["tax_code"] != l.get("po_tax_code", "")]
        if not overrides or bill_ret is None:
            return ""
        codes = ", ".join(sorted({l["tax_code"] for l in overrides}))
        manual = (f"— set the tax code ({codes}) on the bill's PO line(s) "
                  f"in QuickBooks by hand.")
        try:
            txn_id = bill_ret.findtext("TxnID", "")
            edit_seq = bill_ret.findtext("EditSequence", "")
            item_rets = bill_ret.findall("ItemLineRet")
            # The bill's item lines come back in submission order (expense
            # lines and item lines are separate lists), so request lines
            # map onto returned TxnLineIDs by position.
            item_lines = [l for l in lines if l["kind"] != "expense"]
            if not (txn_id and edit_seq) or len(item_rets) != len(item_lines):
                return (f"⚠ Tax pick on linked line(s) NOT applied (could "
                        f"not match the entered bill's lines back) {manual}")
            expense_ids = [er.findtext("TxnLineID", "")
                           for er in bill_ret.findall("ExpenseLineRet")]
            item_mods = []
            for line, line_ret in zip(item_lines, item_rets):
                mod = {"txn_line_id": line_ret.findtext("TxnLineID", "")}
                if any(line is o for o in overrides):
                    mod["tax_code"] = line["tax_code"]
                item_mods.append(mod)
            qbxml = build_bill_tax_mod_qbxml(txn_id, edit_seq,
                                             expense_ids, item_mods)
            response = self.request(qbxml)
            self._dump("last_qbxml_mod_request.xml", qbxml)
            self._dump("last_qbxml_mod_response.xml", response)
            rs = ET.fromstring(response).find(".//BillModRs")
            mod_status = rs.get("statusCode") if rs is not None else None
            if mod_status == "0":
                # Applying the tax after entry is EXPECTED behavior (the
                # one way QuickBooks takes tax on a PO-linked line), so a
                # clean apply must read as routine success — but only
                # after verifying the pick actually sits on the line, so
                # a silently dropped/defaulted code can't hide behind a
                # calm message.
                expected = {m["txn_line_id"]: m["tax_code"]
                            for m in item_mods if m.get("tax_code")}
                applied = {ilr.findtext("TxnLineID", ""):
                           ilr.findtext("SalesTaxCodeRef/FullName", "")
                           for ilr in rs.findall(".//BillRet/ItemLineRet")}
                if not all(lid in applied for lid in expected):
                    # Mod response came back without line detail —
                    # read the bill fresh instead.
                    applied = self._bill_line_tax_codes(txn_id)
                unreadable = [lid for lid in expected if lid not in applied]
                wrong = {lid: applied[lid] for lid, code in expected.items()
                         if lid in applied and applied[lid] != code}
                if wrong:
                    got = ", ".join(f'"{c or "(none)"}"'
                                    for c in wrong.values())
                    return (f"⚠ Tax code {codes} did NOT stick on "
                            f"{len(wrong)} linked line(s) — QuickBooks "
                            f"reports {got} on the line after the update "
                            f"{manual}")
                if unreadable:
                    return (f"Tax code {codes} applied to {len(overrides)} "
                            f"linked line(s) after entry; QuickBooks "
                            f"accepted it but the line couldn't be read "
                            f"back to double-check — glance at the bill's "
                            f"Tax column once.")
                return (f"Tax code {codes} applied and verified on "
                        f"{len(overrides)} linked line(s). ✓")
            detail = (f"status {mod_status}: {rs.get('statusMessage')}"
                      if rs is not None else "no BillModRs in the response")
            return (f"⚠ Tax pick on linked line(s) NOT applied "
                    f"(BillMod {detail}) — the bill and PO link are in "
                    f"{manual}")
        except Exception as e:
            return (f"⚠ Tax pick on linked line(s) NOT applied ({e}) — "
                    f"the bill and PO link are in {manual}")

    def company_file_path(self) -> str:
        """Full path of the open .QBW, straight from the request processor."""
        try:
            return self.qb.GetCurrentCompanyFileName(self.ticket) or ""
        except Exception:
            return ""

    def attach_file_to_txn(self, file_path: str, txn_id: str) -> tuple:
        """Stage a source document as a Doc Center attachment on a
        transaction. Returns (ok, message).

        qbXML has no attachment request — the Desktop SDK simply doesn't
        expose the Doc Center, so a fully silent attach isn't possible.
        What QuickBooks DOES support: attachment files live under
        "Attach\\<company file name>\\Txn\\<TxnID>" beside the company
        file, and Company → Documents → Repair Attached Document Links
        re-registers whatever it finds there. So the PDF is copied where
        QuickBooks expects it; one Repair Links run (it processes every
        staged file at once, so many bills can be batched) puts it on the
        bill's paper-clip. Best-effort — never blocks the entry."""
        if not (file_path and txn_id):
            return False, "PDF attach skipped (no file or no TxnID)."
        qbw = self.company_file_path()
        if not qbw:
            return False, ("PDF attach skipped — QuickBooks did not report "
                           "the company file path.")
        source = Path(file_path)
        # TxnIDs are hex-and-dash, safe as a folder name.
        target_dir = (Path(qbw).parent / "Attach" / Path(qbw).stem
                      / "Txn" / txn_id)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if not target.exists():
                shutil.copy2(source, target)
            return True, (f"PDF staged as this bill's attachment "
                          f"({target}). Run Company → Documents → Repair "
                          f"Attached Document Links in QuickBooks to finish "
                          f"— one run attaches every staged PDF at once.")
        except Exception as e:
            return False, (f"PDF attach failed ({e}) — attach "
                           f"{source.name} to the bill manually.")

    def _bill_line_tax_codes(self, txn_id: str) -> dict:
        """TxnLineID → sales-tax code FullName for a bill's item lines,
        read fresh from QuickBooks — the verification behind the tax-on-
        linked-line second step. Empty dict on any failure (verification
        is then reported as unavailable, never guessed)."""
        try:
            qbxml = wrap_qbxml(
                "        <BillQueryRq>\n"
                f"            <TxnID>{escape(txn_id)}</TxnID>\n"
                "            <IncludeLineItems>true</IncludeLineItems>\n"
                "        </BillQueryRq>\n")
            root = ET.fromstring(self.request(qbxml))
            return {ilr.findtext("TxnLineID", ""):
                    ilr.findtext("SalesTaxCodeRef/FullName", "")
                    for ilr in root.findall(".//BillRet/ItemLineRet")}
        except Exception:
            return {}

    def bill_linked_txns(self, txn_id: str) -> list:
        """Transactions QuickBooks reports as linked to a bill — the way to
        verify a LinkToTxn actually attached the bill to its PO."""
        if not txn_id:
            return []
        qbxml = wrap_qbxml(
            "        <BillQueryRq>\n"
            f"            <TxnID>{escape(txn_id)}</TxnID>\n"
            "            <IncludeLinkedTxns>true</IncludeLinkedTxns>\n"
            "        </BillQueryRq>\n")
        root = ET.fromstring(self.request(qbxml))
        return [{"txn_id": lt.findtext("TxnID", ""),
                 "txn_type": lt.findtext("TxnType", ""),
                 "ref_number": lt.findtext("RefNumber", "")}
                for lt in root.findall(".//BillRet/LinkedTxn")]

    @staticmethod
    def _dump(filename: str, content: str):
        try:
            Path(__file__).with_name(filename).write_text(content,
                                                          encoding="utf-8")
        except Exception:
            pass  # diagnostics only — never let them break the entry
