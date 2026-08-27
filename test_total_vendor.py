"""Regression test for the 2026-08-21 GWAL live bugs (no QuickBooks
needed):

1. Totals: "Engineering Fees subtotal  1.75  171.25" put total HOURS
   where the parser expected dollars — a "total" label tier must never
   match inside "subtotal", the dollar value is the LAST money token of
   a column run, a label detached from its amount (GWAL layout mode)
   falls back to raw-text label-line/amount-next-line, and repeated
   per-section totals (FedEx) never beat the invoice-level one.
2. Vendor: the company file's own name (live CompanyQueryRq) is the
   bill-to on every invoice and must never win vendor guessing; a vendor
   whose name only appears via its email/web domain (logo is an image)
   is found by domain stem = first word or initials — unambiguously or
   not at all.
"""
import sys
sys.path.insert(0, r'quickbooks-automation-main\app')
from bill_parser import _find_total, guess_vendor, parse_bill

# ---- _find_total unit cases ----

# Hours column before the dollar column: last money token of the run.
assert _find_total("Engineering Fees subtotal 1.75 171.25\n"
                   "Invoice total 1.75 193.51") == 193.51
# "subtotal" must never satisfy a "total" tier (1.75 here is HOURS).
assert _find_total("Engineering Fees subtotal 1.75\n"
                   "stuff 22.26 elsewhere\nInvoice total 193.51") == 193.51
assert _find_total("Sub Total 100.00\nTotal 113.00") == 113.00
# Label detached from its amount (GWAL layout mode) — raw-text fallback:
# label alone on a line, amount opening the next.
assert _find_total("Engineering Fees subtotal 1.75",
                   "Invoice subtotal\n171.25\nHST\n22.26\n"
                   "Invoice total\n193.51") == 193.51
# Per-section totals repeated below the invoice-level one (FedEx): the
# payable is the largest hit, not the last.
assert _find_total("Total CAD $211.48\nTotal CAD $105.90\n"
                   "Total CAD $40.44\nTotal CAD $65.14") == 211.48

# ---- the real GWAL invoice, end to end ----

GWAL_PDF = (r"Bills\inbox\Sample Bills 2"
            r"\GWAL, A Division_2026-160_2210 Bank St - M_76289_07-31-2026.pdf")
p = parse_bill(GWAL_PDF)
assert p["total"] == 193.51, p["total"]
assert p["tax_total"] == 22.26, p["tax_total"]
assert p["pre_tax_total"] == 171.25, p["pre_tax_total"]
assert [t["amount"] for t in p["taxes"]] == [22.26]
assert p["invoice_number"] == "76289"
assert p["po_number"] == "KPA-26-1176"
print("GWAL totals: 193.51 total / 22.26 HST / 171.25 pre-tax OK")

# ---- vendor guessing ----

VENDORS = ["Bell Canada", "FedEx", "Goodkey, Weedmark & Associates Ltd",
           "K Paul Architect Inc."]
OWN = ("K Paul Architect Inc.",)

# The own company (bill-to block on every invoice) must never win — even
# though it IS in the vendor list and prominent in the text.
assert guess_vendor(p["vendor_text"], VENDORS) == "K Paul Architect Inc.", \
    "sanity: without the exclusion the own company wins (the live bug)"
got = guess_vendor(p["vendor_text"], VENDORS, exclude_names=OWN)
assert got == "Goodkey, Weedmark & Associates Ltd", got
print("GWAL vendor: own company excluded, GWAL found via gwal.com "
      "acronym OK")

# Domain-stem fallback details (no vendor name in the text at all):
text = "Questions: accounting@gwal.com   www.gwal.com"
assert guess_vendor(text, VENDORS, exclude_names=OWN) \
    == "Goodkey, Weedmark & Associates Ltd"       # initials g-w-a-l
assert guess_vendor("mail from billing@fedex.ca", VENDORS,
                    exclude_names=OWN) == "FedEx"  # first word
# An ambiguous stem (two vendors match) guesses NOTHING — never a coin
# flip; same for freemail domains.
two = VENDORS + ["Goodkey Weedmark Atlantic Ltd"]
assert guess_vendor(text, two, exclude_names=OWN) == ""
assert guess_vendor("from bob@gmail.com", ["Gmail Consulting"],
                    exclude_names=OWN) == ""
# Full-name presence still beats the domain tier, longest match first.
assert guess_vendor("Bell Canada bill, see www.gwal.com",
                    VENDORS, exclude_names=OWN) == "Bell Canada"
# Exclusion tolerates punctuation/suffix variants of the company name.
assert guess_vendor("K Paul Architect Inc is the client",
                    VENDORS, exclude_names=("K Paul Architect Inc.",)) == ""
print("vendor guessing: exclusion variants, domain tier, ambiguity "
      "fail-safe OK")

# ---- the second GWAL invoice (76288, Quebec project) — the live
# 2026-08-21 report: own-company guess resurfaced here. Locally the
# exclusion handles it; the live failure mode is an EMPTY exclusion
# (company_names() failing soft), covered below. ----

GWAL2_PDF = (r"Bills\inbox\Sample Bills 2"
             r"\GWAL, A Division_2026-123_650 Boul Arthur-"
             r"_76288_07-31-2026.pdf")
p2 = parse_bill(GWAL2_PDF)
assert p2["total"] == 2250.06, p2["total"]
# Quebec taxes: "QST No. 1002924494 TQ0001  195.21" carries "No." and a
# TQ-suffixed registration number (live 2026-08-21 — the missed QST
# overstated pre-tax by exactly 195.21 and cascaded into a wrong linked
# amount). Both taxes must be found.
assert sorted(t["amount"] for t in p2["taxes"]) == [97.85, 195.21], \
    p2["taxes"]
assert p2["tax_total"] == 293.06 and p2["pre_tax_total"] == 1957.00
assert p2["invoice_number"] == "76288"
assert p2["po_number"] == "KPA-26-1224"
assert guess_vendor(p2["vendor_text"], VENDORS) == "K Paul Architect Inc.", \
    "sanity: an EMPTY exclusion reproduces the live 76288 symptom"
assert guess_vendor(p2["vendor_text"], VENDORS, exclude_names=OWN) \
    == "Goodkey, Weedmark & Associates Ltd"
print("GWAL 76288: own company excluded, vendor found via domain OK")

# ---- company_names() must survive a failing CompanyQueryRq: the .QBW
# file name is an independent second source, so the exclusion can't be
# silently empty just because one query failed. ----

from qb_client import QuickBooks

broken = QuickBooks.__new__(QuickBooks)   # no COM session
broken.company_file_path = lambda: r"C:\QB\K Paul Architect Inc.QBW"


def _boom(_):
    raise RuntimeError("no COM")


broken.request = _boom
assert broken.company_names() == ["K Paul Architect Inc"], \
    "the QBW file name must keep the exclusion alive when the query fails"
assert broken.company_names() is broken.company_names(), "cached"
# The exclusion tolerates the stem's missing 'Inc.' period (norm match).
assert guess_vendor(p2["vendor_text"], VENDORS,
                    exclude_names=tuple(broken.company_names())) \
    == "Goodkey, Weedmark & Associates Ltd"

CO_RS = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<CompanyQueryRs statusCode="0" statusMessage="Status OK">
<CompanyRet><CompanyName>K Paul Architect Inc.</CompanyName>
<LegalCompanyName>K Paul Architect Incorporated</LegalCompanyName>
</CompanyRet></CompanyQueryRs></QBXMLMsgsRs></QBXML>"""
ok_client = QuickBooks.__new__(QuickBooks)
ok_client.company_file_path = lambda: r"C:\QB\K Paul Architect Inc.QBW"
ok_client.request = lambda x: CO_RS
assert ok_client.company_names() == [
    "K Paul Architect Inc.", "K Paul Architect Incorporated",
    "K Paul Architect Inc"], ok_client.company_names()
print("company_names: query + QBW-stem redundancy, fail-soft, cached OK")

print("=" * 70)
print("TOTAL + VENDOR TEST DONE")
