import re
from datetime import datetime
import os

QB_AVAILABLE = True
try:
    import win32com.client
except ImportError:
    QB_AVAILABLE = False

from invoice.parser import parse_invoice


def build_invoice_qbxml(data: dict) -> str:
    line_items_xml = ""
    for item in data["line_items"]:
        line_items_xml += f"""
        <InvoiceLineAdd>
            <ItemRef>
                <FullName>{item['description']}</FullName>
            </ItemRef>
            <Desc>{item['details']}</Desc>
            <Quantity>{item['qty']}</Quantity>
            <Rate>{item['unit_cost']}</Rate>
        </InvoiceLineAdd>
        """

    return f"""<?xml version="1.0" encoding="utf-8"?>
<?qbxml version="13.0"?>
<QBXML>
    <QBXMLMsgsRq onError="stopOnError">
        <InvoiceAddRq>
            <InvoiceAdd>
                <CustomerRef>
                    <FullName>{data['client_name']}</FullName>
                </CustomerRef>
                <TxnDate>{data['date_of_issue']}</TxnDate>
                <RefNumber>{data['invoice_number']}</RefNumber>
                {line_items_xml}
            </InvoiceAdd>
        </InvoiceAddRq>
    </QBXMLMsgsRq>
</QBXML>
"""


def send_to_quickbooks(qbxml: str):
    if not QB_AVAILABLE:
        print("pywin32 not installed — skipping QuickBooks connection.")
        return

    qb = win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
    try:
        qb.OpenConnection2("", "QuickBooks Automation App", 1)
        ticket = qb.BeginSession("", 2)
        response = qb.ProcessRequest(ticket, qbxml)
        print(response)
        qb.EndSession(ticket)
    except Exception as e:
        print(f"Could not connect to QuickBooks: {e}")
    finally:
        qb.CloseConnection()


def normalize_invoice_data(raw: dict) -> dict:
    def clean_number(value: str) -> float:
        cleaned = re.sub(r"[^\d.\-]", "", str(value))
        return float(cleaned) if cleaned else 0.0

    def clean_date(value: str) -> str:
        value = value.strip()
        for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return value

    normalized = {
        "client_name": raw["client_name"].strip(),
        "date_of_issue": clean_date(raw["date_of_issue"]),
        "invoice_number": raw["invoice_number"].strip(),
        "line_items": [],
    }

    for item in raw["line_items"]:
        if not item["description"].strip():
            continue
        normalized["line_items"].append({
            "description": item["description"].strip(),
            "details": item["details"].strip(),
            "qty": clean_number(item["qty"]),
            "unit_cost": clean_number(item["unit_cost"]),
        })

    return normalized


if __name__ == "__main__":
    file_path = r"C:\Users\arnav\OneDrive\Documents\Coding\quickbooks-automation\invoices\invoice.pdf"

    raw_data = parse_invoice(file_path)
    clean_data = normalize_invoice_data(raw_data)

    qbxml = build_invoice_qbxml(clean_data)

    with open("generated_invoice.xml", "w", encoding="utf-8") as f:
        f.write(qbxml)

    print(qbxml)
    send_to_quickbooks(qbxml)
