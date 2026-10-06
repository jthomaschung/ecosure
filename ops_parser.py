"""
Parser for the Jimmy John's CMX "Ops Assessment" / "Ops Self-Assessment" PDF.

WHY THIS EXISTS
---------------
These scores used to come from a Playwright scrape of the CMX Tableau
dashboard. That broke silently: the dashboard serves whatever period its
default filter is on, so when the period rolled over the scraper kept
downloading the same pre-October crosstab every morning, upserting 367
identical rows and reporting success. Nothing new reached the database for
days while every run went green.

The email carries the same numbers in a PDF and arrives per assessment, so
there is no period filter to drift and no dashboard layout to break.

TWO REPORT TYPES, ONE LAYOUT
----------------------------
  "Ops Assessment"       — FBC/field-team visit  -> fbc_assessment_store_department_score
  "Ops Self-Assessment"  — the store's own audit -> survey_store_department_score

They are structurally identical: same departments, same Summary by Department
table. Only the title and the header's third line differ ("Inspire Field Team"
vs "Franchisee"), so report_type() keys on the title.

NOTE the title check order: "Ops Self-Assessment" CONTAINS "Ops Assessment"
nowhere, but "Ops Assessment" is a substring of nothing here — still, the
self-assessment test runs first so a future "Ops Assessment Self" variant
cannot be misrouted.

WHAT IS EXTRACTED
-----------------
Header: activity number, store number, assessor, start date.
Summary by Department: findings, repeats and score per department, plus the
Overall row. Daypart and Additional Compliance Areas carry findings/repeats
but no score — they are kept in the jsonb detail and have no wide column.
"""
import re
import subprocess

# Department name in the PDF -> column in the score tables.
# Daypart and Additional Compliance Areas have no score and no column; they
# are deliberately absent here but still land in the jsonb detail.
DEPARTMENT_COLUMNS = {
    "Bread Baking": "bread_baking",
    "Sandwich Preparation": "sandwich_preparation",
    "Customer Service": "customer_service",
    "Formulas and Product Freshness": "formulas_product_freshness",
    "Systems and Procedures": "systems_procedures",
    "Uniforms": "uniforms",
    "Cleanliness": "cleanliness",
    "Equipment": "equipment",
}

# Every row the summary table can carry, in PDF order. Used to anchor parsing
# so a renamed or reordered department fails loudly instead of silently
# shifting values into the wrong column.
SUMMARY_ROWS = [
    "Daypart",
    "Bread Baking",
    "Sandwich Preparation",
    "Customer Service",
    "Formulas and Product Freshness",
    "Systems and Procedures",
    "Uniforms",
    "Cleanliness",
    "Equipment",
    "Additional Compliance Areas",
]

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


class NotAnOpsAssessment(ValueError):
    """The PDF is not an Ops Assessment report.

    The email also carries a "Report Tags" PDF, which has a similar header but
    a Question Tag Summary rather than a Summary by Department. Raising this
    lets the caller skip the attachment rather than fail the run.
    """


def _text(path, layout=True):
    """pdftotext -layout: the summary table only parses with layout preserved.

    Without -layout the columns interleave with the labels and the numbers
    cannot be attributed to a row.
    """
    cmd = ["pdftotext"]
    if layout:
        cmd.append("-layout")
    cmd += ["-f", "1", "-l", "1", str(path), "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise NotAnOpsAssessment(f"pdftotext failed: {out.stderr[:200]!r}")
    return out.stdout.decode("utf-8", errors="replace")


def report_type(text):
    """'self_assessment' | 'fbc', or None when this is not an Ops report."""
    if "Ops Self-Assessment" in text:
        return "self_assessment"
    if "Ops Assessment" in text:
        return "fbc"
    return None


def _parse_header(text):
    """Activity number, store number, assessor and the assessment date.

    The header is a two-column layout, so -layout interleaves the left block
    (activity number, store, address) with the right (assessor, dates). Each
    field is therefore matched independently rather than by position.
    """
    header = {}

    m = re.search(r"Activity Number\s+(\d+)", text)
    if m:
        header["activity_number"] = m.group(1)

    m = re.search(r"Assessor:\s*\n?\s*(.+)", text)
    if m:
        header["assessor"] = m.group(1).strip()

    # "October 5, 2026 1:42 PM" — the first date in the document is the start.
    m = re.search(r"(January|February|March|April|May|June|July|August|"
                  r"September|October|November|December)\s+(\d{1,2}),\s+(\d{4})", text)
    if m:
        header["audit_date"] = (
            f"{m.group(3)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}")

    # Store number: a standalone 3-5 digit line in the header block. Anchored
    # to the lines before "Total Duration" so a figure from the body cannot be
    # mistaken for it.
    head = text.split("Total Duration")[0]
    candidates = [ln.strip() for ln in head.splitlines()]
    for ln in candidates:
        # The line is the store number alone, or the store number followed by
        # spaces and right-column text from the two-column layout.
        m = re.match(r"^(\d{3,5})(?:\s{2,}.*)?$", ln)
        if m and m.group(1) != header.get("activity_number"):
            header["store_number"] = int(m.group(1))
            break

    return header


def _parse_summary(text):
    """The Summary by Department table: findings, repeats, score per row."""
    if "Summary by Department" not in text:
        raise NotAnOpsAssessment("no 'Summary by Department' table")

    body = text.split("Summary by Department", 1)[1]
    departments = []

    for name in SUMMARY_ROWS:
        # Row shape: NAME  findings  repeats  [score%]
        # Daypart and Additional Compliance Areas have no score.
        pattern = re.escape(name) + r"\s+(\d+)\s+(\d+)(?:\s+([\d.]+)%)?"
        m = re.search(pattern, body)
        if not m:
            continue
        departments.append({
            "name": name,
            "findings": int(m.group(1)),
            "repeats": int(m.group(2)),
            "score": float(m.group(3)) if m.group(3) else None,
        })

    m = re.search(r"Overall\s+(\d+)\s+(\d+)\s+([\d.]+)%", body)
    if not m:
        raise NotAnOpsAssessment("no Overall row in the summary table")

    return departments, {
        "overall_findings": int(m.group(1)),
        "overall_repeats": int(m.group(2)),
        "overall_score": float(m.group(3)),
    }


def parse_report(path):
    """Parse an Ops Assessment PDF.

    Returns (kind, row) where kind is 'self_assessment' or 'fbc' and row is a
    dict ready to upsert. Raises NotAnOpsAssessment for anything else —
    including the "Report Tags" PDF that arrives in the same email.
    """
    text = _text(path)

    kind = report_type(text)
    if kind is None:
        raise NotAnOpsAssessment("title is neither Ops Assessment nor Ops Self-Assessment")

    # The Report Tags PDF shares the title but summarises by question tag.
    if "Question Tag Summary" in text and "Summary by Department" not in text:
        raise NotAnOpsAssessment("this is the Report Tags PDF, not the assessment")

    header = _parse_header(text)
    departments, overall = _parse_summary(text)

    row = {
        "store_number": header.get("store_number"),
        "audit_date": header.get("audit_date"),
        "activity_number": header.get("activity_number"),
        "assessor": header.get("assessor"),
        "overall_score": overall["overall_score"],
        "overall_findings": overall["overall_findings"],
        "overall_repeats": overall["overall_repeats"],
        "departments": departments,
    }

    for d in departments:
        col = DEPARTMENT_COLUMNS.get(d["name"])
        if col and d["score"] is not None:
            row[col] = d["score"]

    return kind, row


def validate(kind, row):
    """Warnings for anything that looks wrong but is not fatal."""
    warnings = []
    if not row.get("store_number"):
        warnings.append("no store number found")
    if not row.get("audit_date"):
        warnings.append("no audit date found")
    if not row.get("activity_number"):
        warnings.append("no activity number — dedupe will fall back to store+date")

    scored = [d for d in row.get("departments", []) if d["score"] is not None]
    if len(scored) < 6:
        warnings.append(f"only {len(scored)} departments carried a score")

    missing = [c for c in DEPARTMENT_COLUMNS.values() if c not in row]
    if missing:
        warnings.append(f"no score for: {', '.join(missing)}")

    return warnings


TABLE_FOR_KIND = {
    "self_assessment": "survey_store_department_score",
    "fbc": "fbc_assessment_store_department_score",
}
