import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

QB_AVAILABLE = True
try:
    import win32com.client
except ImportError:
    QB_AVAILABLE = False

from fedex.parser import parse_fedex_invoice

VENDOR_NAME = "FedEx"
ITEM_NAME = "5230 Courier"
# Account used on the Expenses tab for the FedEx earned discount line.
# Change this to the exact account name you pick in QuickBooks.
DISCOUNT_ACCOUNT = "5230 Courier"

DEFAULT_PDF = (
    r"C:\Users\arnav\OneDrive\Documents\Coding\quickbooks-automation"
    r"\16.99999.10021.273832725.XXXXX2947.000000.pdf"
)


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


def build_bill_qbxml(invoice: dict, job_names: dict) -> str:
    # Discount is entered on the Expenses tab, split per tax code so the
    # GST/HST QuickBooks computes matches the FedEx invoice exactly.
    discount_by_tax = defaultdict(float)
    for s in invoice["shipments"]:
        discount_by_tax[s["province"]] += s["earned_discount"]

    expense_lines = ""
    for tax_code, amount in discount_by_tax.items():
        if round(amount, 2) == 0:
            continue
        expense_lines += f"""
            <ExpenseLineAdd>
                <AccountRef>
                    <FullName>{escape(DISCOUNT_ACCOUNT)}</FullName>
                </AccountRef>
                <Amount>{amount:.2f}</Amount>
                <Memo>FedEx earned discount</Memo>
                <SalesTaxCodeRef>
                    <FullName>{escape(tax_code)}</FullName>
                </SalesTaxCodeRef>
            </ExpenseLineAdd>"""

    item_lines = ""
    for s in invoice["shipments"]:
        job = job_names.get(s["project_number"], "")
        customer_ref = ""
        if job:
            customer_ref = f"""
                <CustomerRef>
                    <FullName>{escape(job)}</FullName>
                </CustomerRef>"""
        item_lines += f"""
            <ItemLineAdd>
                <ItemRef>
                    <FullName>{escape(ITEM_NAME)}</FullName>
                </ItemRef>
                <Cost>{s['cost']:.2f}</Cost>
                <Amount>{s['cost']:.2f}</Amount>
                <SalesTaxCodeRef>
                    <FullName>{escape(s['province'])}</FullName>
                </SalesTaxCodeRef>{customer_ref}
                <BillableStatus>Billable</BillableStatus>
            </ItemLineAdd>"""

    body = f"""        <BillAddRq>
            <BillAdd>
                <VendorRef>
                    <FullName>{escape(VENDOR_NAME)}</FullName>
                </VendorRef>
                <TxnDate>{invoice['invoice_date']}</TxnDate>
                <RefNumber>{escape(invoice['invoice_number'])}</RefNumber>{expense_lines}{item_lines}
            </BillAdd>
        </BillAddRq>
"""
    return wrap_qbxml(body)


class QuickBooks:
    def __init__(self):
        self.qb = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
        self.qb.OpenConnection2("", "FedEx Bill Automation", 1)
        self.ticket = self.qb.BeginSession("", 2)

    def request(self, qbxml: str) -> str:
        return self.qb.ProcessRequest(self.ticket, qbxml)

    def host_info(self) -> str:
        """Product name and supported qbXML versions of the attached QuickBooks."""
        try:
            root = ET.fromstring(self.request(wrap_qbxml("        <HostQueryRq/>\n")))
            ret = root.find(".//HostRet")
            if ret is None:
                return ""
            versions = [el.text for el in ret.findall("SupportedQBXMLVersion")]
            return f"{ret.findtext('ProductName', '')} (qbXML {', '.join(versions)})"
        except Exception:
            return ""

    def close(self):
        self.qb.EndSession(self.ticket)
        self.qb.CloseConnection()


def fetch_ret_names(qb: "QuickBooks", rq_tag: str, extra: str = "") -> set:
    """FullName/Name of every entry a list query returns (items, accounts, ...)."""
    qbxml = wrap_qbxml(f"        <{rq_tag}>\n{extra}        </{rq_tag}>\n")
    root = ET.fromstring(qb.request(qbxml))
    rs = root.find(f".//{rq_tag[:-2]}Rs")
    names = set()
    if rs is None:
        return names
    for ret in rs:
        name = ret.findtext("FullName") or ret.findtext("Name")
        if name:
            names.add(name)
    return names


def bill_exists(qb: "QuickBooks", ref_number: str) -> bool:
    qbxml = wrap_qbxml(
        "        <BillQueryRq>\n"
        f"            <RefNumber>{escape(ref_number)}</RefNumber>\n"
        "        </BillQueryRq>\n"
    )
    root = ET.fromstring(qb.request(qbxml))
    return root.find(".//BillRet") is not None


def match_jobs(invoice: dict, full_names: set) -> tuple:
    """Map each shipment's project number to a QuickBooks customer:job.

    Anything that can't be matched unambiguously becomes a flag; flagged
    invoices are held (not entered) unless --force is given.
    """
    matches, flags = {}, []
    for s in invoice["shipments"]:
        project = s["project_number"]
        label = f"shipment {s['tracking_id']} (ref: {s['customer_reference']})"
        if not project:
            flags.append(f"No 7-digit project number found for {label}.")
            continue
        if project in matches:
            continue
        candidates = [
            name for name in full_names
            if ":" in name and project in name.split(":")[-1]
        ]
        if len(candidates) == 1:
            matches[project] = candidates[0]
        elif candidates:
            flags.append(
                f"Project {project} matches multiple customer:jobs "
                f"{sorted(candidates)} — {label}.")
        else:
            flags.append(
                f"No customer:job in QuickBooks contains project {project} — {label}.")
    return matches, flags


def validate(qb: "QuickBooks", invoice: dict) -> tuple:
    """Check every list reference the bill will use. Returns (job_names, flags)."""
    flags = []

    if ITEM_NAME not in fetch_ret_names(qb, "ItemQueryRq"):
        flags.append(f'Item "{ITEM_NAME}" is not in the QuickBooks Item list.')

    if DISCOUNT_ACCOUNT not in fetch_ret_names(qb, "AccountQueryRq"):
        flags.append(
            f'Account "{DISCOUNT_ACCOUNT}" (discount Expenses-tab line) '
            f"is not in the Chart of Accounts.")

    tax_codes = fetch_ret_names(qb, "SalesTaxCodeQueryRq")
    for province in sorted({s["province"] for s in invoice["shipments"] if s["province"]}):
        if province not in tax_codes:
            flags.append(f'Sales tax code "{province}" is not in the QuickBooks tax code list.')
    for s in invoice["shipments"]:
        if not s["province"]:
            flags.append(f"No destination province parsed for shipment {s['tracking_id']}.")

    customers = fetch_ret_names(
        qb, "CustomerQueryRq", "            <ActiveStatus>All</ActiveStatus>\n")
    job_names, job_flags = match_jobs(invoice, customers)
    flags += job_flags

    if invoice["invoice_number"] and bill_exists(qb, invoice["invoice_number"]):
        flags.append(
            f"A bill with ref number {invoice['invoice_number']} already exists "
            f"in QuickBooks — this invoice looks already entered.")

    return job_names, flags


def check_totals(invoice: dict) -> list:
    items_total = round(sum(s["cost"] for s in invoice["shipments"]), 2)
    computed = round(items_total + invoice["earned_discount_total"]
                     + sum(s["tax_amount"] for s in invoice["shipments"]), 2)
    if computed != invoice["total"]:
        return [f"Parsed lines total {computed:.2f} but the invoice says "
                f"{invoice['total']:.2f} — the parser may have missed a charge."]
    return []


def format_preview(invoice: dict, job_names: dict) -> str:
    lines = [f"Bill: {VENDOR_NAME}  Ref {invoice['invoice_number']}  "
             f"Date {invoice['invoice_date']}  Total CAD {invoice['total']:.2f}",
             "", "Items tab:"]
    for s in invoice["shipments"]:
        job = job_names.get(s["project_number"]) or f"<unmatched: {s['customer_reference']}>"
        lines.append(f"  {ITEM_NAME:<14} {s['cost']:>8.2f}  {s['province']:<3} {job}")
    lines += ["", "Expenses tab:",
              f"  {DISCOUNT_ACCOUNT:<14} {invoice['earned_discount_total']:>8.2f}  (earned discount)"]
    return "\n".join(lines)


def parse_add_response(response: str) -> tuple:
    rs = ET.fromstring(response).find(".//BillAddRs")
    if rs is None:
        return False, f"Unexpected response:\n{response}"
    if rs.get("statusCode") == "0":
        return True, "Bill added to QuickBooks successfully."
    return False, (f"QuickBooks rejected the bill "
                   f"(status {rs.get('statusCode')}): {rs.get('statusMessage')}")


def process_pdf(pdf_path, qb: "QuickBooks" = None, force: bool = False,
                xml_out: str = None) -> dict:
    """Parse one FedEx PDF and, if it validates cleanly, enter it as a bill.

    Returns {"sent", "flags", "preview", "message", "invoice"}. Any flag
    holds the whole bill (nothing partial is entered) unless force=True.
    """
    invoice = parse_fedex_invoice(str(pdf_path))
    result = {"invoice": invoice, "sent": False, "flags": [],
              "preview": "", "message": ""}

    if not invoice["shipments"]:
        result["flags"].append("No shipments found in the PDF.")
        result["message"] = "Bill NOT entered."
        return result

    result["flags"] += check_totals(invoice)

    job_names = {}
    if qb:
        job_names, flags = validate(qb, invoice)
        result["flags"] += flags

    result["preview"] = format_preview(invoice, job_names)
    qbxml = build_bill_qbxml(invoice, job_names)
    if xml_out:
        Path(xml_out).write_text(qbxml, encoding="utf-8")

    if qb is None:
        result["message"] = ("QuickBooks not connected — qbXML generated only, "
                             "list validation and job matching skipped.")
    elif result["flags"] and not force:
        result["message"] = ("Bill NOT entered — fix the flags above in QuickBooks "
                             "or the PDF, then re-drop the file (or use --force).")
    else:
        sent, message = parse_add_response(qb.request(qbxml))
        result["sent"] = sent
        result["message"] = message
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Enter one FedEx invoice PDF as a QuickBooks bill.")
    parser.add_argument("pdf", nargs="?", default=DEFAULT_PDF)
    parser.add_argument("--force", action="store_true",
                        help="enter the bill even when validation flags are raised "
                             "(unmatched shipments get no customer:job)")
    parser.add_argument("--xml", default="generated_fedex_bill.xml",
                        help="where to write the generated qbXML")
    args = parser.parse_args()

    qb = None
    if QB_AVAILABLE:
        try:
            qb = QuickBooks()
            info = qb.host_info()
            if info:
                print(f"Connected to {info}")
        except Exception as e:
            print(f"Could not connect to QuickBooks: {e}")
    else:
        print("pywin32 not installed — generating XML only.")

    try:
        result = process_pdf(args.pdf, qb, force=args.force, xml_out=args.xml)
    finally:
        if qb:
            qb.close()

    print(f"\n{result['preview']}\n")
    for flag in result["flags"]:
        print(f"FLAG: {flag}")
    print(f"\n{result['message']}")
    print(f"qbXML written to {args.xml}")


if __name__ == "__main__":
    main()
