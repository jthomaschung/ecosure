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

from ops_parser import parse_report, validate, NotAnOpsAssessment, TABLE_FOR_KIND


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

    if dry_run:
        print(f"[dry-run] {kind} -> {table}")
        print(json.dumps(row, indent=2, default=str))
        return

    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"],
                       os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    if row.get("activity_number"):
        sb.table(table).upsert(row, on_conflict="activity_number").execute()
    else:
        # No activity number means the PDF header did not carry one. Insert
        # rather than upsert — there is no safe key to merge on, and a bad
        # merge would overwrite a good row.
        sb.table(table).insert(row).execute()

    print(f"   {kind}: store {row['store_number']} "
          f"{row['audit_date']} {row['overall_score']}% "
          f"({row['overall_findings']} findings, {row['overall_repeats']} repeats) "
          f"-> {table}")
