from pypdf import PdfReader


def extract_form_fields(file_path: str) -> dict:
    reader = PdfReader(file_path)
    fields = reader.get_fields()
    if not fields:
        return {}
    return {name: (field.get("/V", "") or "").strip() for name, field in fields.items()}


def parse_invoice(file_path: str) -> dict:
    raw = extract_form_fields(file_path)

    invoice = {
        "invoice_number": raw.get("Text1", ""),
        "date_of_issue": raw.get("Text2", ""),
        "cleaning_period": raw.get("Text3", ""),
        "company_name": raw.get("Text4", ""),
        "company_address": raw.get("Text5", ""),
        "company_city_state": raw.get("Text6", ""),
        "company_phone": raw.get("Text7", ""),
        "company_email": raw.get("Text8", ""),
        "company_website": raw.get("Text9", ""),
        "client_name": raw.get("Text10", ""),
        "client_street": raw.get("Text11", ""),
        "client_city_state": raw.get("Text12", ""),
        "client_zip": raw.get("Text13", ""),
        "line_items": [
            {
                "description": raw.get("Text14", ""),
                "details": raw.get("Text15", ""),
                "unit_cost": raw.get("Text16", ""),
                "qty": raw.get("Text17", ""),
                "amount": raw.get("Text18", ""),
            },
            {
                "description": raw.get("Text19", ""),
                "details": raw.get("Text20", ""),
                "unit_cost": raw.get("Text21", ""),
                "qty": raw.get("Text22", ""),
                "amount": raw.get("Text23", ""),
            },
            {
                "description": raw.get("Text24", ""),
                "details": raw.get("Text25", ""),
                "unit_cost": raw.get("Text26", ""),
                "qty": raw.get("Text27", ""),
                "amount": raw.get("Text28", ""),
            },
            {
                "description": raw.get("Text29", ""),
                "details": raw.get("Text30", ""),
                "unit_cost": raw.get("Text31", ""),
                "qty": raw.get("Text32", ""),
                "amount": raw.get("Text33", ""),
            },
            {
                "description": raw.get("Text34", ""),
                "details": raw.get("Text35", ""),
                "unit_cost": raw.get("Text36", ""),
                "qty": raw.get("Text37", ""),
                "amount": raw.get("Text38", ""),
            },
            {
                "description": raw.get("Text39", ""),
                "details": raw.get("Text40", ""),
                "unit_cost": raw.get("Text41", ""),
                "qty": raw.get("Text42", ""),
                "amount": raw.get("Text43", ""),
            },
        ],
        "subtotal": raw.get("Text44", ""),
        "discount": raw.get("Text45", ""),
        "tax_rate": raw.get("Text46", ""),
        "tax": raw.get("Text47", ""),
        "grand_total": raw.get("Text48", ""),
        "total": raw.get("Text49", ""),
        "terms": raw.get("Text50", ""),
        "bank_name": raw.get("Text51", ""),
        "account_number": raw.get("Text52", ""),
        "sort_code": raw.get("Text53", ""),
        "footer_note": raw.get("Text54", ""),
    }

    return invoice


if __name__ == "__main__":
    file_path = r"C:\Users\arnav\OneDrive\Documents\Coding\quickbooks-automation\invoices\invoice.pdf"
    result = parse_invoice(file_path)

    print("Invoice #:", result["invoice_number"])
    print("Date:", result["date_of_issue"])
    print("Client:", result["client_name"])
    print("\nLine items:")
    for item in result["line_items"]:
        print(f"  {item['description']:<25} {item['unit_cost']:>6} x {item['qty']:>3} = {item['amount']:>6}")
    print("\nSubtotal:", result["subtotal"])
    print("Discount:", result["discount"])
    print("Tax:", result["tax"], f"({result['tax_rate']})")
    print("Total:", result["total"])
