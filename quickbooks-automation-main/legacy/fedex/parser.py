import re
from datetime import datetime

from pypdf import PdfReader


def _to_float(value: str) -> float:
    return float(value.replace(",", ""))


def _parse_date(value: str) -> str:
    return datetime.strptime(value, "%b %d, %Y").strftime("%Y-%m-%d")


def _clean_detail_text(text: str) -> str:
    # Stitch shipments that span pages back together by stripping page
    # headers and the "continued" markers FedEx inserts at page breaks.
    text = re.sub(r"Invoice Number.*?Page\s*\d+ of \d+", " ", text)
    text = re.sub(r"Tracking ID:\s*\d+\s*Continued on the next page", " ", text)
    text = re.sub(r"FedEx Express Shipper DetailTracking ID:\s*\d+\s*continued", " ", text)
    text = re.sub(r"FedEx Express Shipper Detail", " ", text)
    return text


def _parse_shipment(block: str) -> dict:
    shipment = {}

    m = re.match(r"\s*([A-Za-z]{3} \d{1,2}, \d{4})", block)
    shipment["ship_date"] = _parse_date(m.group(1)) if m else ""

    m = re.search(r"(\d{12})FedEx", block)
    shipment["tracking_id"] = m.group(1) if m else ""

    # The customer reference (Cust. Ref. / Ref.#2 / Ref.#3) holds the project
    # number used to pick the QuickBooks customer:job, e.g. "2510177".
    header = block.split("Tracking ID")[0]
    m = re.search(r"\b(\d{7})\b", header)
    shipment["project_number"] = m.group(1) if m else ""

    m = re.search(r"Cust\. Ref\.:\s*(.*?)Ref\.#3:\s*(.*?)Ref\.#2:\s*(.*?)(?:The Earned Discount|Automation|Fuel Surcharge|$)", block, re.DOTALL)
    if m:
        refs = []
        for part in m.groups():
            part = re.sub(r"[^\x20-\x7e]", "", part).strip()
            if part:
                refs.append(part)
        shipment["customer_reference"] = " / ".join(refs)
    else:
        shipment["customer_reference"] = ""

    # Destination province drives the QuickBooks sales tax code (ON, MB, ...).
    province = ""
    recipient = block.split("Recipient", 1)
    if len(recipient) == 2:
        m = re.search(r"\b([A-Z]{2})\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d\b", recipient[1])
        if m:
            province = m.group(1)
    shipment["province"] = province

    m = re.search(r"Earned Discount\s*(-?[\d,]+\.\d{2})", block)
    shipment["earned_discount"] = _to_float(m.group(1)) if m else 0.0

    m = re.search(r"Subtotal\s*(-?[\d,]+\.\d{2})", block)
    subtotal = _to_float(m.group(1)) if m else 0.0

    # The FedEx per-shipment subtotal already includes the (negative) earned
    # discount; the QuickBooks item line is entered gross of that discount.
    shipment["cost"] = round(subtotal - shipment["earned_discount"], 2)

    m = re.search(r"Canada (GST|HST)(?:\s*\(([A-Z]{2})\))?\s*([\d,]+\.\d{2})", block)
    if m:
        shipment["tax_type"] = m.group(1)
        shipment["tax_amount"] = _to_float(m.group(3))
    else:
        shipment["tax_type"] = ""
        shipment["tax_amount"] = 0.0

    m = re.search(r"Total CAD\$([\d,]+\.\d{2})", block)
    shipment["total"] = _to_float(m.group(1)) if m else 0.0

    return shipment


def parse_fedex_invoice(file_path: str) -> dict:
    reader = PdfReader(file_path)
    pages = [page.extract_text() or "" for page in reader.pages]
    full_text = "\n".join(pages)

    invoice = {}

    m = re.search(r"Invoice Number([\d-]+)", full_text)
    invoice["invoice_number"] = m.group(1) if m else ""

    m = re.search(r"Invoice Date([A-Za-z]{3} \d{1,2}, \d{4})", full_text)
    invoice["invoice_date"] = _parse_date(m.group(1)) if m else ""

    m = re.search(r"Account Number([X\d-]+)", full_text)
    invoice["account_number"] = m.group(1) if m else ""

    m = re.search(r"TOTALCAD \$([\d,]+\.\d{2})", full_text)
    invoice["total"] = _to_float(m.group(1)) if m else 0.0

    detail_text = _clean_detail_text(
        " ".join(p for p in pages if "Shipper Detail" in p)
    )
    blocks = re.split(r"Ship Date:", detail_text)[1:]
    invoice["shipments"] = [_parse_shipment(b) for b in blocks]

    invoice["earned_discount_total"] = round(
        sum(s["earned_discount"] for s in invoice["shipments"]), 2
    )

    return invoice


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\finance.automation\Documents\quickbooks-automation-main\16.99999.10021.273832725.XXXXX2947.000000.pdf"
    )
    print(json.dumps(parse_fedex_invoice(path), indent=2))
