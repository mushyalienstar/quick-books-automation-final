"""Vendor-agnostic bill PDF parser.

Best-effort extraction of the fields needed to enter a vendor bill:
invoice/ref number, invoice date, total, tax amounts, and candidate
charge lines (label + amount). Everything returned here is a *suggestion*
that the entry UI lets the user confirm or override — this module never
validates against QuickBooks and never rejects a bill.

Two document types are distinguished (doc_type in the result):
  "bill"      — a single-vendor invoice; one total, optional tax lines.
  "statement" — a credit-card/account statement with a running
                transaction table (date / posting date / description /
                amount). Every transaction row is extracted so each can
                become its own bill line, and the statement's own stated
                totals are captured for reconciliation.
"""
import difflib
import re
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

# Amounts may carry a currency marker before the digits — "$", "C$ 7,091.32"
# (Rimkus), "CAD 100.00" — all of which _to_float strips.
MONEY = r"-?(?:[A-Z]{1,3})?\$?\s*-?[\d,]{1,12}\.\d{2}"

DATE_PATTERNS = [
    # "Jul 16, 2026" / "July 16 2026"
    (r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}",
     ["%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%b. %d, %Y"]),
    # "16 Jul 2026"
    (r"\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}", ["%d %b %Y", "%d %B %Y"]),
    # "2026-07-16"
    (r"\d{4}-\d{2}-\d{2}", ["%Y-%m-%d"]),
    # "07/16/2026" (month-first assumed) / "07/16/26"
    (r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
     ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d/%m/%Y"]),
]

DATE_LABELS = r"(?:invoice\s*date|date\s*of\s*issue|bill(?:ing)?\s*date|issued?|date)"

# Labels whose amounts are totals/taxes/etc., not enterable charge lines.
NON_CHARGE_WORDS = re.compile(
    r"total|subtotal|balance|amount\s*due|payment|paid|due|gst|hst|pst|qst"
    r"|vat|tax|remit|cheque|check|credit\s*card|page|account\s*number",
    re.IGNORECASE)

TAX_WORD = r"(?:GST|HST|PST|QST|VAT|Sales\s*Tax|Tax(?:es)?)"

# "Total QST - Telecom included in this bill $0.09" — label, trailing
# words tolerated, then the amount. A tax-registration number and a
# parenthesized rate may ride between the word and the amount
# ("HST 89786 6877 RT ( 13%)  429.00").
# The registration number may carry "No." and a TQ (Quebec) or RT
# suffix: "QST No. 1002924494 TQ0001  195.21" (live 2026-08-21 — the
# missed QST overstated the bill's pre-tax by exactly the QST). The
# trailing tolerated-words run is TEMPERED so one tax's label can never
# swallow a DIFFERENT tax word sitting before the amount ("GST/HST No.
# … RT0001  GST 97.85" must stay labeled GST, not HST).
TAX_LABEL = re.compile(
    r"((?:[A-Za-z][A-Za-z ]{0,20})?" + TAX_WORD +
    r"(?:\s*\([A-Z]{2}\))?(?:\s+No\.?)?(?:\s+[\d/ ]{4,20}(?:RT|TQ)\d*)?"
    r"(?:\s*\(\s*[\d.]+\s*%\s*\))?(?:(?!" + TAX_WORD +
    r")[A-Za-z \-()%]){0,40}?)"
    r"\s*(?:@\s*[\d.]+%)?\s*[:\s]*(" + MONEY + r")")

# "$4.25Total GST included in this bill" — two-column PDFs (e.g. Bell)
# render the amount before its label on the same extracted line.
AMOUNT_FIRST_LINE = re.compile(
    r"^\s*(-?\$\s*-?[\d,]{1,12}\.\d{2})\s*([A-Za-z][^\n]{0,70})$")

# Most specific first. "subtotal"/"sub total"/"sub-total" is a DIFFERENT
# number (pre-tax) and must never satisfy a "total" tier — the fixed-width
# lookbehinds block it (live 2026-08-21: GWAL "Engineering Fees subtotal
# 1.75" fed the HOURS column to the bare "total" tier).
_NOT_SUB = r"(?<!sub)(?<!sub[ -])"
TOTAL_LABELS = [
    r"grand\s*total", _NOT_SUB + r"total\s*due", r"amount\s*due",
    r"balance\s*due", r"invoice\s*total", _NOT_SUB + r"total\s*amount",
    _NOT_SUB + r"total",
]


def _to_float(value: str) -> float:
    # Drop currency markers ("$", "C$", "CAD") along with grouping commas.
    value = re.sub(r"[A-Za-z$, ]", "", value)
    # tolerate trailing-minus formats like "51.91-"
    if value.endswith("-"):
        value = "-" + value[:-1]
    return float(value)


def _find_date(text: str) -> str:
    """Labeled date first, then any date; returned as YYYY-MM-DD."""
    candidates = []
    for pattern, formats in DATE_PATTERNS:
        m = re.search(DATE_LABELS + r"\s*[:.\s]*\s*(" + pattern + r")",
                      text, re.IGNORECASE)
        if m:
            candidates.append((0, m.group(1), formats))
    if not candidates:
        for pattern, formats in DATE_PATTERNS:
            m = re.search(pattern, text)
            if m:
                candidates.append((1, m.group(0), formats))
    for _, raw, formats in sorted(candidates):
        for fmt in formats:
            try:
                return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


# Vendors label their bill number differently; tried in order, first label
# family with a hit wins ("ref" last — bills quote unrelated references like
# airline "Booking Reference: BC78FL").
INVOICE_NUMBER_LABELS = [
    r"invoice\s*(?:number|no\.?|num\.?|#|:)",
    r"\bbill\s*(?:number|no\.?|num\.?|#)",
    r"\bref(?:erence)?\s*(?:number|no\.?|num\.?|#)",
]


def _find_invoice_number(text: str, alt_text: str = "") -> str:
    """alt_text is the layout-mode extraction: some PDFs (Rimkus) render
    labels and values as separate columns, so in raw text "Invoice Number:"
    and "7069744" land lines apart — layout mode keeps them on one line.
    Each label tier is tried on both texts before falling to the next, so
    an invoice-labelled hit anywhere still beats a ref-labelled one."""
    texts = [t for t in (text, alt_text) if t]
    for label in INVOICE_NUMBER_LABELS:
        label = label + r"\s*[:#]?\s*"
        for t in texts:
            # Numeric (with dashes) first — stops cleanly even when the PDF
            # glues the next label on ("...2-738-32725Account Number...").
            m = re.search(label + r"(\d[\d-]*\d|\d)", t, re.IGNORECASE)
            if m:
                return m.group(1)
            m = re.search(label + r"([A-Za-z]{1,6}[-/]?\d[\w/-]*)", t,
                          re.IGNORECASE)
            if m:
                return m.group(1)
    # Heading style: a line that is just "INVOICE 23117-B3" — the number
    # follows the bare word with no "No."/"#" token (Fekete). Anchoring to
    # the whole line keeps history rows ("Invoice 23117-B2  6,825.00") out.
    for t in texts:
        m = re.search(r"^\s*invoice\s*[:#]?\s+((?=[\w/-]*\d)[A-Za-z0-9][\w/-]*)\s*$",
                      t, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1)
    return ""


def _invoice_number_from_filename(file_path) -> str:
    """Fallback for image-only scans (no text layer): an explicitly
    "invoice"-labelled number in the filename, e.g.
    "12738-[KPA-25-1744]-CYLA-Invoice#25367.pdf". Same spirit as the PO
    panel's filename matching."""
    stem = re.sub(r"[_\s]+", " ", Path(file_path).stem)
    found = _find_invoice_number(stem)
    if found:
        return found
    # "INVOICE 23117-B3 from ..." — bare word then the number, mid-name.
    m = re.search(r"\binv(?:oice)?(?![a-z])[\s#:.-]*"
                  r"((?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]*)",
                  stem, re.IGNORECASE)
    return m.group(1) if m else ""


_PO_LABEL = (r"\b(?:p\.?\s?o\.?|purchase\s+order)\s*"
             r"(?:number|no\.?|num\.?|#|:)?\s*[:#]?\s*")
_PO_VALUE = r"[A-Za-z]{0,6}-?\d[\w/-]*"


def _find_po_numbers(text: str) -> list:
    """Every PO number printed on the bill, in order of appearance ("PO #",
    "P.O. No.", "Purchase Order: KPA-25-1744"). One label can carry a
    comma-separated list ("PO Number: KPA-25-1723, KPA-26-1039, …" — one
    invoice billing several POs), and the same number repeated in the body
    is returned once. "P.O. Box …" is a mailing address — the capture
    requires a digit, and "Box" is excluded explicitly."""
    numbers = []
    pattern = re.compile(
        _PO_LABEL + r"(?!box\b)(" + _PO_VALUE +
        r"(?:\s*,\s*" + _PO_VALUE + r")*)", re.IGNORECASE)
    for m in pattern.finditer(text):
        for value in re.split(r"\s*,\s*", m.group(1)):
            if value and value.lower() not in (n.lower() for n in numbers):
                numbers.append(value)
    return numbers


def _find_po_number(text: str) -> str:
    numbers = _find_po_numbers(text)
    return numbers[0] if numbers else ""


# ---------- per-PO progress-billing table ----------
#
# Progress invoices (J+B) bill several POs on one invoice, one table row
# per PO line:
#
#                      CONTRACT     PRIOR    CURRENT    TO DATE   REMAINING
#   KPA-26-1686 - Cascade Boilers   9,800.00    0.00   9,800.00   9,800.00 ...
#
# The CURRENT column is what THIS invoice bills against that row's PO —
# the invoice's own subtotal is just the sum, so a linked PO must get its
# row's amount, never the bottom line. Cells can be blank (nothing billed
# / no contract value), so amounts can't be matched to columns by token
# order; and in layout extraction the header words do NOT line up with
# their values (J+B's "CURRENT" header sits ~25 chars left of its
# numbers). What IS stable: numeric columns are right-aligned, so the
# money tokens' right edges cluster tightly per column across the rows.
# Clusters, left to right, map onto the header names in order.

_PO_TABLE_COLUMNS = [
    ("contract", r"\bcontract\b|\bscheduled?\s+value\b"),
    ("prior", r"\bprior\b|\bprevious(?:ly)?\b"),
    ("current", r"\bcurrent\b|\bthis\s+(?:invoice|period|billing|application)\b"),
    ("to_date", r"\bto\s+date\b"),
    ("remaining", r"\bremaining\b|\bbalance\b"),
]

# A row starts with an id-ish PO reference ("KPA-24-2190", EME-style
# "25-1760") followed by whitespace — the trailing-boundary lookahead
# keeps the totals row's "108,756.00" from donating "108" as an id.
_TABLE_ROW_ID = re.compile(
    r"^\s*((?=[A-Za-z0-9/-]{2,20}(?:\s|$))[A-Za-z]{0,8}-?\d[\w/-]*)")

_ROW_MONEY = re.compile(r"-?[\d,]{1,12}\.\d{2}")


def _table_header_columns(line: str):
    """The known column names present on a progress-table header line,
    left to right — or None when the line isn't such a header. CURRENT
    plus at least two companions is required, so ordinary prose that
    happens to say "current" can't start a table."""
    found = []
    for name, pattern in _PO_TABLE_COLUMNS:
        m = re.search(pattern, line, re.IGNORECASE)
        if m:
            found.append((m.start(), name))
    found.sort()
    names = [n for _, n in found]
    return names if "current" in names and len(names) >= 3 else None


def _parse_table_row(line: str):
    """(po_number, desc, [(start, end, value), ...]) for an id-led row
    with at least one amount on it, else None."""
    m = _TABLE_ROW_ID.match(line)
    if not m:
        return None
    tokens = [(t.start(), t.end(), _to_float(t.group(0)))
              for t in _ROW_MONEY.finditer(line, m.end(1))]
    if not tokens:
        return None
    desc = " ".join(line[m.end(1):tokens[0][0]].split()).strip(" -–—:")
    return m.group(1), desc, tokens


def _assign_columns(raw_rows: list, columns: list) -> list:
    """Assign each row's amounts to named columns by right-edge clustering.
    Returns [] whenever the geometry doesn't line up exactly — the caller
    then falls back to no table at all rather than guessing money."""
    edges = sorted(e for _, _, tokens in raw_rows for _, e, _ in tokens)
    clusters = []
    for e in edges:
        if clusters and e - clusters[-1][-1] <= 8:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    if len(clusters) != len(columns):
        return []
    out = []
    for po_number, desc, tokens in raw_rows:
        amounts = {}
        for _, e, value in tokens:
            idx = min(range(len(clusters)),
                      key=lambda k: min(abs(e - x) for x in clusters[k]))
            if columns[idx] in amounts:  # two amounts in one column —
                amounts = None           # misparsed row, don't guess
                break
            amounts[columns[idx]] = value
        if amounts is not None:
            # A blank CURRENT cell means the row simply isn't billed on
            # this invoice — 0.00, not "unknown".
            out.append({"po_number": po_number, "desc": desc,
                        "current": amounts.get("current", 0.0),
                        "amounts": amounts})
    return out


def _find_po_billing_table(layout_text: str) -> list:
    """All per-PO billing rows found under Contract/Prior/Current/... table
    headers, best-effort. Rows are collected until a few consecutive
    non-row lines (the totals row, "Invoice Subtotal", taxes) end the
    table; blank lines between rows are fine."""
    lines = layout_text.splitlines()
    rows, i = [], 0
    while i < len(lines):
        columns = _table_header_columns(lines[i])
        if not columns:
            i += 1
            continue
        raw_rows, misses, j = [], 0, i + 1
        while j < len(lines) and misses < 4:
            if not lines[j].strip():
                j += 1
                continue
            row = _parse_table_row(lines[j])
            if row:
                raw_rows.append(row)
                misses = 0
            else:
                misses += 1
            j += 1
        rows.extend(_assign_columns(raw_rows, columns))
        i = j
    return rows


# Words that don't identify WHAT work is billed — grammatical filler and
# generic billing nouns that would let any two descriptions look alike.
_STOP_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "these", "those",
    "are", "was", "were", "has", "have", "had", "all", "any", "each",
    "per", "our", "your", "their", "into", "onto", "will", "been", "not",
    "service", "services", "fee", "fees", "charge", "charges",
}


def _stem(word: str) -> str:
    """Light suffix stripping so different grammar for the same work
    shares a token ("cleaning"/"cleaned"/"cleans" → "clean", "supplies"
    → "supply"). Deliberately crude — the close-match fallback in
    _words_hit covers what this misses."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("ings", "ing", "ers", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def desc_words(s: str) -> set:
    """The stemmed, meaningful words of a description — what identifies
    the work once filler and generic billing nouns are dropped. Two
    phrasings of the same work ("Bathroom Cleaning" / "Cleaning services
    in the bathroom") share most of these regardless of word order."""
    return {_stem(w) for w in re.findall(r"[a-z]{3,}", s.lower())
            if w not in _STOP_WORDS}


def _words_hit(words: set, pool: set) -> int:
    return sum(
        1 for w in words
        if w in pool or difflib.get_close_matches(w, pool, n=1, cutoff=0.86))


def desc_match_ratio(desc: str, text: str) -> float:
    """How much of a PO line's description shows up in the invoice text —
    the sanity check for "is this bill really billing that PO line?".
    Concept-level and forgiving: word order, surrounding words and
    grammar don't matter ("Bathroom Cleaning" is in "Cleaning services
    performed in the bathroom"), close matches cover typos like
    "invioce"; only a description whose key words mostly DON'T appear
    anywhere should be flagged. 1.0 when there's nothing meaningful to
    check."""
    words = desc_words(desc)
    if not words:
        return 1.0
    return _words_hit(words, desc_words(text)) / len(words)


# Progress invoices restate history and contract context right next to
# what's actually billed now: the original/contract value ("Store design
# $10,695.00"), amounts already invoiced or paid ("Minus invoiced to date
# ($9,700.00)"), what remains open. Amounts under such labels are context,
# never what THIS invoice bills for a line — they must not be matched
# into bill lines even when the description fits perfectly.
HISTORY_LABEL = re.compile(
    r"\b(?:original(?:ly)?|contract|scheduled|prior|previous(?:ly)?"
    r"|minus|less|invoiced|billed|paid|to\s+date|remaining|outstanding)\b",
    re.IGNORECASE)


def desc_similarity(po_desc: str, invoice_desc: str) -> float:
    """Do a PO line's description and one of the invoice's own line
    descriptions name the same work? Scored over the PO side's meaningful
    words only, so extra words on the invoice side ("Cleaning services in
    the bathroom" vs PO "Bathroom Cleaning") don't dilute a real match.
    0.0 when either side has nothing meaningful."""
    a, b = desc_words(po_desc), desc_words(invoice_desc)
    if not a or not b:
        return 0.0
    return _words_hit(a, b) / len(a)


# An id-ish value: letters/digits/dashes, at least one digit, bounded length
# so a sentence can't be swallowed.
_ID_VALUE = r"((?=[A-Za-z0-9-]{0,20}\d)[A-Za-z0-9][A-Za-z0-9-]{0,19})"


def _find_project_number(text: str) -> str:
    """The customer-side project/job number printed on the bill ("Customer
    Proj #", "Client Project Number:", "Project No:", "Job #"). Tried most
    specific first; a separator token (No./#/:) is required so prose like
    "Project Manager" or "Project 25-079 SOMENAME" can't match. When a plain
    "Project No" label hits several times (the vendor's own project number
    vs the client's — EME prints both), a label starting its own line wins:
    vendor refs ride mid-line in address headers."""
    sep = r"\s*(?:number|no\.?|num\.?|#|:)\s*[:#]?\s*"
    tiers = [
        r"(?:customer|client)\s*proj(?:ect)?\.?" + sep,
        r"\bproj(?:ect)?" + sep,
        r"\bjob\s*(?:number|no\.?|num\.?|#)\s*[:#]?\s*",
    ]
    for label in tiers:
        matches = list(re.finditer(label + _ID_VALUE, text, re.IGNORECASE))
        if not matches:
            continue
        for m in matches:
            if re.search(r"(?:^|\n)[ \t]*$", text[:m.start()]):
                return m.group(1)
        return matches[0].group(1)
    return ""


def build_memo(project_number: str = "", po_number: str = "") -> str:
    """Human-readable memo line from whatever identifiers the bill has:
    "Project No. 2307116 PO KPA-23-1722", or just the half that exists.
    Empty when the bill has neither — the caller falls back (filename)."""
    parts = []
    if project_number and project_number.strip():
        parts.append(f"Project No. {project_number.strip()}")
    if po_number and po_number.strip():
        parts.append(f"PO {po_number.strip()}")
    return " ".join(parts)


def _find_total(text: str, raw_text: str = "") -> float:
    """Pass layout-mode text as `text` when available: matching runs per
    line, in both "label ... amount" and "amount label" order (two-column
    PDFs). `raw_text` (plain extraction) feeds only the label-line/
    amount-next-line fallback — some PDFs (GWAL, live 2026-08-21) detach
    the total's value from its label in layout mode but keep them on
    adjacent lines in raw reading order."""
    texts = [t for t in (text, raw_text) if t]
    for lbl in TOTAL_LABELS:
        # "(?:[A-Z]{1,3}\s*)?\$?" — currency markers between label and
        # amount: "CAD 100.00", "CAD   $211.48", "C$ 7,091.32", plain "$".
        # The trailing group swallows a whitespace-separated column run
        # after the first amount ("subtotal  1.75  171.25" = Hours then
        # Amount): dollars are the LAST money token of the run, never a
        # preceding hours/quantity/rate column.
        label_first = re.compile(
            lbl + r"\s*[:.\s]*(?:[A-Z]{1,3}\s*)?\$?\s*([\d,]+\.\d{2})"
                  r"((?:[ \t]+\$?[\d,]+\.\d{2})*)",
            re.IGNORECASE)
        amount_first = re.compile(
            r"\$\s*([\d,]+\.\d{2})([^\d\n]{0,20}?)" + lbl, re.IGNORECASE)
        hits = []
        for line in text.splitlines():
            m = label_first.search(line)
            if m:
                run = m.group(1) + m.group(2)
                hits.append(_to_float(
                    re.findall(r"[\d,]+\.\d{2}", run)[-1]))
            m = amount_first.search(line)
            if m and not re.search(r"previous|prior", m.group(2), re.IGNORECASE):
                hits.append(_to_float(m.group(1)))
        if not hits:
            # Column-detached PDFs: the label ends its line and the
            # amount opens the next non-blank one.
            label_at_end = re.compile(lbl + r"\s*:?[ \t]*$", re.IGNORECASE)
            amount_at_start = re.compile(
                r"^[ \t]*\$?\s*([\d,]+\.\d{2})(?:\s|$)")
            for t in texts:
                lines = t.splitlines()
                for i, line in enumerate(lines):
                    if not label_at_end.search(line):
                        continue
                    nxt = next(
                        (l for l in lines[i + 1:i + 3] if l.strip()), "")
                    m = amount_at_start.match(nxt)
                    if m:
                        hits.append(_to_float(m.group(1)))
        if hits:
            # Within the winning tier the payable amount is the LARGEST
            # hit: documents that repeat per-section "Total" lines under
            # the invoice-level one (FedEx prints one per shipment) put
            # section totals that sum to the payable BELOW it, so "last
            # hit" picks a section (live 2026-08-21). Progress-billing
            # history amounts (contract / to date), which can exceed the
            # payable, match the more specific tiers above bare "total".
            return max(hits)
    amounts = [_to_float(a) for a in re.findall(r"\$\s*([\d,]+\.\d{2})", text)]
    return max(amounts) if amounts else 0.0


def _tax_key(label: str) -> str:
    """Canonical tax token ("gst", "hst (on)") for duplicate detection.
    Two-column PDFs glue prose onto the label ("…charge a 10% late GST"
    vs "GST" for the same line), so dedup can't use the full label."""
    m = re.search(TAX_WORD + r"(?:\s*\([A-Z]{2}\))?", label, re.IGNORECASE)
    return " ".join(m.group(0).lower().split()) if m else label.lower()


def _find_taxes(text: str, total: float) -> tuple:
    """((label, amount) tax pairs, best-guess tax total). Pass layout-mode
    text when available — matching runs per line, in both orders."""
    taxes, seen = [], {}
    for line in text.splitlines():
        pairs = TAX_LABEL.findall(line)
        m = AMOUNT_FIRST_LINE.match(line)
        if m and re.search(TAX_WORD, m.group(2)):
            pairs.append((m.group(2), m.group(1)))
        for label, amount in pairs:
            label = " ".join(label.split()).strip(" .:-")
            value = _to_float(amount)
            if value <= 0 or value >= total > 0:
                continue
            if re.search(r"subtotal|rate", label, re.IGNORECASE):
                continue
            key = (_tax_key(label), value)
            if key in seen:  # the same tax repeated (detail pages, glued
                # prose) — keep one entry. "Total …" labels win (the stated
                # filter below depends on them), then the cleaner (shorter).
                old = seen[key]["label"]
                new_total = bool(re.search(r"total", label, re.IGNORECASE))
                old_total = bool(re.search(r"total", old, re.IGNORECASE))
                if (new_total, -len(label)) > (old_total, -len(old)):
                    seen[key]["label"] = label
                continue
            seen[key] = {"label": label, "amount": value}
            taxes.append(seen[key])

    # Lines like "Total GST included in this bill" are the per-tax totals;
    # when present they beat component lines ("OHST (8%) on telecom" is
    # already inside "Total HST"), which would double count.
    stated = [t for t in taxes if re.search(r"total", t["label"], re.IGNORECASE)]
    if stated:
        taxes = stated

    # An explicit tax subtotal beats summing lines (which can double count
    # when per-line detail repeats the invoice summary).
    m = re.search(r"(?:tax\s*subtotal|total\s*tax(?:es)?)\s*[:.\s]*\$?\s*([\d,]+\.\d{2})",
                  text, re.IGNORECASE)
    tax_total = _to_float(m.group(1)) if m else round(
        sum(t["amount"] for t in taxes), 2)
    return taxes, tax_total


# ---------- credit-card / account statements ----------

MONTHS = {}
for _i, _name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1):
    MONTHS[_name.lower()] = _i
    MONTHS[_name.lower()[:3]] = _i
MONTHS["sept"] = 9

# A transaction date cell: "JUN 3" / "3 JUN" / "06/03" / "06/03/26"
TXN_DATE = (r"(?:[A-Za-z]{3,9}\.?\s?\d{1,2}|\d{1,2}\s[A-Za-z]{3,9}\.?|"
            r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)")
TXN_AMOUNT = r"-?\$?\s?-?[\d,]{1,12}\.\d{2}"

TXN_ROW_PATTERNS = [
    # "JUN 3 JUN 8 $398.92AIR CAN* ..." — two dates, $amount glued to the
    # description (TD statements extract this way).
    re.compile(r"^\s*(?P<d1>" + TXN_DATE + r")\s+(?P<d2>" + TXN_DATE + r")\s+"
               r"(?P<amt>-?\$\s?-?[\d,]{1,12}\.\d{2})(?P<desc>.*)$"),
    # "JUN 3 JUN 8 SOME MERCHANT 398.92" — two dates, amount last.
    re.compile(r"^\s*(?P<d1>" + TXN_DATE + r")\s+(?P<d2>" + TXN_DATE + r")\s+"
               r"(?P<desc>.*?\S)\s+(?P<amt>" + TXN_AMOUNT + r")(?P<cr>\s*CR)?\s*$"),
    # "JUN 3 SOME MERCHANT 398.92" — one date, amount last.
    re.compile(r"^\s*(?P<d1>" + TXN_DATE + r")\s+"
               r"(?P<desc>.*?\S)\s+(?P<amt>" + TXN_AMOUNT + r")(?P<cr>\s*CR)?\s*$"),
]

# A page only counts as a transaction table if it has a header like
# "DATE ... ACTIVITY DESCRIPTION ... AMOUNT" or several row matches.
TABLE_HEADER = re.compile(
    r"date.{0,80}?(?:description|activity|merchant)[^\n]{0,80}?amount",
    re.IGNORECASE | re.DOTALL)

STATEMENT_MARKERS = [
    r"statement\s*(?:date|period)", r"minimum\s*payment", r"credit\s*limit",
    r"payment\s*due\s*date", r"new\s*balance", r"previous\s*balance",
    r"cash\s*advance", r"annual\s*interest\s*rate",
]

# Lines that end a wrapped-description continuation.
CONT_STOP = re.compile(
    r"continued|total|balance|subtotal|statement|minimum\s*payment"
    r"|payment\s*due|page\s*\d|_", re.IGNORECASE)

PAYMENT_WORDS = re.compile(r"payment|thank\s*you", re.IGNORECASE)


def _parse_txn_date(raw: str):
    """'JUN 3' / '3 JUN' / '06/03' -> (month, day), or None if not a date."""
    raw = raw.strip()
    m = re.match(r"^([A-Za-z]{3,9})\.?\s?(\d{1,2})$", raw)
    if m:
        month = MONTHS.get(m.group(1).lower())
        return (month, int(m.group(2))) if month else None
    m = re.match(r"^(\d{1,2})\s([A-Za-z]{3,9})\.?$", raw)
    if m:
        month = MONTHS.get(m.group(2).lower())
        return (month, int(m.group(1))) if month else None
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?$", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if month > 12 >= day:
            month, day = day, month
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day
    return None


def _match_row(line: str):
    """(date_raw, description, amount) if the line is a transaction row."""
    for pattern in TXN_ROW_PATTERNS:
        m = pattern.match(line)
        if not m:
            continue
        if not _parse_txn_date(m.group("d1")):
            continue
        d2 = m.groupdict().get("d2")
        if d2 and not _parse_txn_date(d2):
            continue
        desc = m.group("desc").strip()
        if not re.search(r"[A-Za-z]", desc):
            continue
        amount = _to_float(m.group("amt"))
        if m.groupdict().get("cr"):  # "12.34 CR" means a credit
            amount = -abs(amount)
        return m.group("d1"), desc, amount
    return None


def _is_continuation(line: str) -> bool:
    """A short trailing fragment of the previous row's description
    (wrapped city name, FOREIGN CURRENCY note, ...)."""
    s = line.strip()
    return (0 < len(s) <= 45 and bool(re.search(r"[A-Za-z]", s))
            and not CONT_STOP.search(s) and not _match_row(line))


def _extract_transactions(page_texts: list) -> tuple:
    """All transaction rows from pages that look like transaction tables.
    Returns (transactions, set of consumed source lines)."""
    txns, used = [], set()
    for page in page_texts:
        lines = page.splitlines()
        page_txns, page_used = [], []
        i = 0
        while i < len(lines):
            row = _match_row(lines[i])
            if not row:
                i += 1
                continue
            date_raw, desc, amount = row
            page_used.append(lines[i])
            cont = 0
            while (i + 1 < len(lines) and cont < 3
                   and _is_continuation(lines[i + 1])):
                i += 1
                cont += 1
                desc += " " + lines[i].strip()
                page_used.append(lines[i])
            page_txns.append({"date_raw": date_raw,
                              "description": " ".join(desc.split()),
                              "amount": amount})
            i += 1
        if page_txns and (len(page_txns) >= 3 or TABLE_HEADER.search(page)):
            txns.extend(page_txns)
            used.update(page_used)
    return txns, used


def _statement_score(text: str) -> int:
    return sum(bool(re.search(m, text, re.IGNORECASE))
               for m in STATEMENT_MARKERS)


def _find_statement_date(text: str) -> str:
    m = re.search(r"statement\s*date\s*:?\s*"
                  r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
                  r"|\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
                  text, re.IGNORECASE)
    if m:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%B %d %Y",
                    "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(m.group(1).strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return _find_date(text)


def _stated_amount(text: str, *labels) -> float:
    """First labelled dollar figure, e.g. 'Purchases & Other Charges $19,445.02'."""
    for label in labels:
        m = re.search(label + r"\s*:?\s*\$?\s*(-?[\d,]+\.\d{2})(?!\s*%)",
                      text, re.IGNORECASE)
        if m:
            return _to_float(m.group(1))
    return 0.0


def _txn_date_iso(raw: str, statement_date: str) -> str:
    """Transaction date as YYYY-MM-DD, inferring the year from the
    statement date (a December row on a January statement is last year)."""
    parsed = _parse_txn_date(raw)
    if not parsed:
        return ""
    month, day = parsed
    year, stmt_month = datetime.now().year, 0
    if statement_date:
        year, stmt_month = int(statement_date[:4]), int(statement_date[5:7])
    if stmt_month and month > stmt_month:
        year -= 1
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _clean_label(label: str) -> str:
    """Trim glued-together PDF text ("Signed byC.ChristineFuel Surcharge")
    down to the last naturally-cased phrase ("Fuel Surcharge")."""
    label = " ".join(label.split()).strip(" .:-")
    parts = re.split(r"(?<=[a-z0-9.,])(?=[A-Z])", label)
    return parts[-1].strip(" .:-,")


def _find_charge_candidates(text: str, total: float) -> list:
    """Label+amount pairs that could be bill lines, for one-click adding."""
    candidates, seen = [], set()
    # "[:;]?" — invoices often write "Description: $1,575.00" (Rimkus),
    # and the amount may land on the NEXT extracted line ("…canopy: \n
    # $3,150.00"); without the separator the whole line item is missed.
    pattern = re.compile(
        r"([A-Za-z][A-Za-z0-9 &/().,'#-]{2,60}?)\s*[:;]?\s*\$?\s*(-?[\d,]+\.\d{2})(?!\d|%)")
    for label, amount in pattern.findall(text):
        label = _clean_label(label)
        value = _to_float(amount)
        if not label or NON_CHARGE_WORDS.search(label):
            continue
        if len(label) < 3 or value == 0:
            continue
        if total and abs(value) > total * 1.5:
            continue
        key = (label.lower(), value)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"label": label, "amount": value})
        if len(candidates) >= 60:
            break
    return candidates


def parse_bill(file_path: str) -> dict:
    """Parse any vendor's bill or statement PDF. Every field is best-effort;
    missing values come back empty/0 rather than raising."""
    reader = PdfReader(str(file_path))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(page_texts)

    transactions, used_lines = _extract_transactions(page_texts)
    if len(transactions) >= 5 and _statement_score(text) >= 3:
        return _parse_statement(text, transactions, used_lines)

    # Layout mode keeps each visual line together, so a two-column summary
    # ("$1,045.72Total amount due") can be matched line by line instead of
    # amounts gluing onto the next line's label.
    try:
        layout_text = "\n".join(page.extract_text(extraction_mode="layout") or ""
                                for page in reader.pages)
    except Exception:
        layout_text = text

    total = _find_total(layout_text, text)
    taxes, tax_total = _find_taxes(layout_text, total)

    invoice_number = (_find_invoice_number(text, layout_text)
                      or _invoice_number_from_filename(file_path))

    # Per-PO progress-billing table: each row's CURRENT amount is what
    # this invoice bills against that row's PO. One PO can span several
    # rows (J+B backs out 9,000 on one KPA-24-2190 row) — summed per PO.
    po_table_rows = _find_po_billing_table(layout_text)
    po_current_amounts = {}
    for row in po_table_rows:
        key = next((k for k in po_current_amounts
                    if k.lower() == row["po_number"].lower()),
                   row["po_number"])
        po_current_amounts[key] = round(
            po_current_amounts.get(key, 0.0) + row["current"], 2)

    po_numbers = _find_po_numbers(text)
    # Table rows ARE PO references being billed, even with no "PO" label
    # anywhere on the invoice — they feed matching like labeled ones.
    for row in po_table_rows:
        if row["po_number"].lower() not in (n.lower() for n in po_numbers):
            po_numbers.append(row["po_number"])
    po_number = po_numbers[0] if po_numbers else ""
    project_number = _find_project_number(text)
    if project_number.lower() in (p.lower() for p in po_numbers):
        # One number under two labels — "PO X" alone reads better than
        # "Project No. X PO X".
        project_number = ""

    return {
        "doc_type": "bill",
        "invoice_number": invoice_number,
        "po_number": po_number,
        "po_numbers": po_numbers,
        "po_table_rows": po_table_rows,
        "po_current_amounts": po_current_amounts,
        "project_number": project_number,
        "memo": build_memo(project_number, ", ".join(po_numbers)),
        "invoice_date": _find_date(text),
        "total": total,
        "taxes": taxes,
        "tax_total": tax_total,
        "pre_tax_total": round(total - tax_total, 2) if total else 0.0,
        "charge_candidates": _find_charge_candidates(text, total),
        "statement": None,
        "text": text,
        "vendor_text": text,
    }


def _parse_statement(text: str, transactions: list, used_lines: set) -> dict:
    statement_date = _find_statement_date(text)
    for t in transactions:
        t["date"] = _txn_date_iso(t["date_raw"], statement_date)
        # A negative "PAYMENT" row is the cardholder paying the card —
        # not an expense line. Refunds/credits stay enterable (negative).
        t["is_payment"] = (t["amount"] < 0
                           and bool(PAYMENT_WORDS.search(t["description"])))

    charges = round(sum(t["amount"] for t in transactions if t["amount"] > 0), 2)
    credits = round(sum(t["amount"] for t in transactions if t["amount"] < 0), 2)
    stated_purchases = _stated_amount(
        text, r"purchases\s*(?:&|and)\s*other\s*charges", r"total\s*purchases")
    total = stated_purchases or charges

    # Vendor guessing must ignore the transaction rows themselves, so a
    # merchant in the list (e.g. Bell Canada) can't be mistaken for the
    # card issuer named in the header/remittance area.
    used = set(used_lines)
    vendor_text = "\n".join(l for l in text.splitlines() if l not in used)

    return {
        "doc_type": "statement",
        "invoice_number": "",
        "po_number": "",
        "po_numbers": [],
        "po_table_rows": [],
        "po_current_amounts": {},
        "project_number": "",
        "memo": "",
        "invoice_date": statement_date,
        "total": total,
        "taxes": [],
        "tax_total": 0.0,
        "pre_tax_total": total,
        "charge_candidates": [
            {"label": f'{t["date"] or t["date_raw"]} {t["description"]}',
             "amount": t["amount"]} for t in transactions],
        "statement": {
            "statement_date": statement_date,
            "transactions": transactions,
            "charges_total": charges,
            "credits_total": credits,
            "stated_purchases": stated_purchases,
            "stated_payments_credits": _stated_amount(
                text, r"payments\s*(?:&|and)\s*credits"),
            "stated_new_balance": _stated_amount(
                text, r"(?:total\s*)?new\s*balance"),
            "stated_previous_balance": _stated_amount(
                text, r"previous\s*(?:statement\s*)?balance"),
        },
        "text": text,
        "vendor_text": vendor_text,
    }


# Freemail/hosting domains say nothing about who SENT an invoice.
_GENERIC_DOMAINS = {"gmail", "yahoo", "hotmail", "outlook", "live", "aol",
                    "icloud", "msn", "mail", "protonmail", "shaw", "rogers",
                    "bell", "telus", "sympatico", "cogeco", "videotron"}


def guess_vendor(text: str, vendor_names: list,
                 exclude_names: tuple = ()) -> str:
    """Pick the QuickBooks vendor whose name appears in the PDF text.

    The live vendor list is the source of truth — nothing is hardcoded.
    Longest match wins (so "FedEx Freight" beats "FedEx" when both appear).
    Matching ignores punctuation/symbols, so "TD® Aeroplan® Visa*" in the
    PDF still matches a vendor named "TD Aeroplan Visa".

    exclude_names: the open company file's OWN name(s) (CompanyQueryRq,
    live) — the bill-to party printed on every incoming invoice, never
    the vendor being paid, so it must never win even when it also exists
    as a vendor record (live 2026-08-21: a GWAL invoice guessed the
    user's own company from its prominent bill-to block).

    When no vendor NAME is in the text (letterhead is an image), fall
    back to the sender's email/web domains: a domain stem matching a
    vendor's first word or initials ("gwal.com" → "Goodkey, Weedmark &
    Associates Ltd") — but only when exactly ONE vendor matches; an
    ambiguous stem guesses nothing."""
    def norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&]", " ", s.lower())).strip()

    excluded = [norm(e) for e in exclude_names if e and norm(e)]

    def is_own(display):
        return any(display == e or display in e or e in display
                   for e in excluded)

    lowered = norm(text)
    best, best_len = "", 0
    for name in vendor_names:
        display = norm(name.split(":")[-1])
        if (len(display) >= 3 and display in lowered
                and len(display) > best_len and not is_own(display)):
            best, best_len = name, len(display)
    if best:
        return best

    stems = {m.group(1).lower()
             for m in re.finditer(r"(?:@|www\.)([A-Za-z][A-Za-z0-9-]{2,})\.",
                                  text)} - _GENERIC_DOMAINS
    matched = []
    for name in vendor_names:
        display = norm(name.split(":")[-1])
        if not display or is_own(display):
            continue
        words = [w for w in display.split() if w != "&"]
        acronym = "".join(w[0] for w in words)
        if any(s == words[0] and len(words[0]) >= 3 or
               s == acronym and len(acronym) >= 3 for s in stems):
            matched.append(name)
    return matched[0] if len(matched) == 1 else ""


if __name__ == "__main__":
    import json
    import sys

    result = parse_bill(sys.argv[1])
    result.pop("text")
    result.pop("vendor_text")
    print(json.dumps(result, indent=2))
