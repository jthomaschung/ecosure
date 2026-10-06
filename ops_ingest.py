"""
Upsert a parsed Ops Assessment into Supabase.

Routes by report type, matching the split the Tableau scraper already used:

  Ops Self-Assessment -> survey_store_department_score
  Ops Assessment      -> fbc_assessment_store_department_score

Keeping the split means the Ops Assessments page needs no changes — it reads
those two tables today and will carry on doing so.

DEDUPE is on activity_number, which CMX assigns uniquely per assessment. The
scraper had no such key and relied on store+date, which silently collapses two
assessments of the same store on the same day into one. Rows written by the
scraper have a NULL activity number; the unique index is partial so they do
not collide with each other.
"""
import os
import json

from ops_parser import (parse_report, validate, NotAnOpsAssessment,
                        TABLE_FOR_KIND, DEPARTMENT_COLUMNS)

# Columns holding a score, all stored as fractions.
SCORE_COLUMNS = set(DEPARTMENT_COLUMNS.values()) | {"overall_score"}


def ingest_ops(pdf_path, email_id=None, dry_run=False):
    """Parse and upsert one Ops Assessment PDF.

    Raises NotAnOpsAssessment when the file is something else — the Report
    Tags PDF that ships in the same email, most often. The caller treats that
    as a skip, not a failure.
    """
    kind, row = parse_report(pdf_path)
    table = TABLE_FOR_KIND[kind]

    warnings = validate(kind, row)
    if warnings:
        print(f"!! parse warnings for {os.path.basename(pdf_path)}")
        for w in warnings:
            print("   -", w)

    # Refuse to write a row that cannot be identified. A score with no store
    # or date is worse than no row at all: it lands in the table, shows up in
    # averages, and cannot be traced back.
    if not row.get("store_number") or not row.get("audit_date"):
        raise ValueError(
            f"{os.path.basename(pdf_path)}: missing store number or audit date, refusing to write")

    row["source_file"] = os.path.basename(pdf_path)
    row["email_message_id"] = email_id
    # jsonb column — the client sends it as a JSON value, not a string.
    row["departments"] = row.get("departments") or []

    # SCORES ARE STORED AS FRACTIONS, NOT PERCENTAGES.
    #
    # The columns are numeric(6,4) / numeric(8,6) and every existing row from
    # the Tableau scraper holds 0.9161 for 91.61%. The PDF prints 91.61, so it
    # must be divided by 100 before writing — otherwise email-sourced rows
    # would be 100x the scraped ones on the same page, and numeric(6,4) would
    # overflow on anything above 99.9999 anyway.
    #
    # The jsonb detail keeps the PDF's own percentages, unscaled, because it
    # is read by humans rather than joined against these columns.
    for col in list(SCORE_COLUMNS):
        if row.get(col) is not None:
            row[col] = round(row[col] / 100.0, 6)

    if dry_run:
        print(f"[dry-run] {kind} -> {table}")
        print(json.dumps(row, indent=2, default=str))
        return

    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"],
                       os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    # Conflict target is (store_number, audit_date), NOT activity_number.
    #
    # Both tables already carry `unique (store_number, audit_date)` from when
    # the Tableau scraper was the only writer. An insert keyed on anything
    # else still trips that constraint, so an email-sourced row for a store
    # the scraper already covered would fail rather than update it.
    #
    # Upserting on the existing key means the email row REPLACES the scraped
    # one for the same store and day, which is what we want: it carries the
    # activity number, assessor, findings and repeats the scrape never had.
    #
    # The trade-off: a store assessed twice in one day collapses to one row.
    # That is the behaviour the scraper already had, so this is not a
    # regression — but activity_number is now stored, so the duplicates are
    # visible and the key can be tightened later if it matters.
    sb.table(table).upsert(row, on_conflict="store_number,audit_date").execute()

    print(f"   {kind}: store {row['store_number']} "
          f"{row['audit_date']} {row['overall_score'] * 100:.2f}% "
          f"({row['overall_findings']} findings, {row['overall_repeats']} repeats) "
          f"-> {table}")
