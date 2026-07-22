"""
Ingest one EcoSure PDF into Supabase for the Lovable app.

  1. parse the PDF                (ecosure_parser.parse_report)
  2. upload finding photos + raw PDF to Supabase Storage
  3. upsert the assessment row, then replace child rows
     (violations / photos / detailed standards)

Idempotent: re-running the same report updates in place (keyed on
unit_number + start_datetime), so retries and re-sends are safe.

Env vars required:
  SUPABASE_URL                 https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service-role key (server-side only!)
Optional:
  ECOSURE_PHOTO_BUCKET         default 'ecosure-photos'
  ECOSURE_PDF_BUCKET           default 'ecosure-reports'

Usage:
  python supabase_ingest.py report.pdf                 # ingest
  python supabase_ingest.py report.pdf --dry-run       # parse + write payload/photos locally, no network
  python supabase_ingest.py report.pdf --email-id <gmail_msg_id>
"""
import os, sys, json, argparse
from datetime import datetime
import fitz
import ecosure_parser
import selfassess_parser

PHOTO_BUCKET = os.environ.get("ECOSURE_PHOTO_BUCKET", "ecosure-photos")
PDF_BUCKET = os.environ.get("ECOSURE_PDF_BUCKET", "ecosure-reports")


def detect_report_type(path):
    """Return 'ecosure' | 'self_assessment' | 'subset' | 'unknown'.

    The self-assessment email ships three PDFs; only the full one is ingested,
    the Non-Compliant and IHR subsets are skipped so nothing is double-counted.
    Titles are matched against the first several non-empty lines (a logo or blank
    line can push the title off line 0), and unknowns are logged so a genuinely
    new/unexpected attachment is easy to identify from the run log.
    """
    try:
        txt = fitz.open(path)[0].get_text()
    except Exception as e:
        print(f"   [detect] could not open PDF: {e}")
        return "unknown"
    head = [l.strip() for l in txt.splitlines() if l.strip()][:8]
    for l in head:
        if l == "Food Safety - Self Assessment":
            return "self_assessment"
        if l.startswith("Food Safety - Self Assessment (Non-Compliant") or l == "IHR Violations":
            return "subset"
    if "Advisor Id" in txt or "EcoSure" in txt or any(l == "Food Safety Assessment" for l in head):
        return "ecosure"
    print(f"   [detect] unknown report — first lines: {head[:4]!r}")
    return "unknown"


def _parser_for(rtype):
    return selfassess_parser if rtype == "self_assessment" else ecosure_parser


def _dt(s):
    """'07/16/2026 03:31:04 PM' -> ISO 8601 (or None)."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y",
                "%B %d, %Y %I:%M %p", "%B %d, %Y %I:%M:%S %p"):
        try:
            return datetime.strptime(s.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


def _slug(s):
    return "".join(c if c.isalnum() else "_" for c in (s or "")).strip("_")


def build_payload(data, unit, start_iso):
    """Rows for the assessment + children (photo storage paths filled later)."""
    assessment = {
        "unit_number": unit,
        "restaurant_name": data["location"].get("restaurant_name"),
        "store_type": data["location"].get("store_type"),
        "brand": data["location"].get("brand"),
        "address": data["location"].get("address"),
        "city": data["location"].get("city"),
        "state": data["location"].get("state"),
        "zip": data["location"].get("zip"),
        "start_datetime": start_iso,
        "end_datetime": _dt(data["evaluation"].get("end_datetime")),
        "advisor_id": data["evaluation"].get("advisor_id"),
        "visit": int(data["evaluation"]["visit"]) if data["evaluation"].get("visit", "").isdigit() else None,
        "score_pct": data.get("score_pct"),
        "risk_level": data.get("risk_level"),
        "report_generated": _dt(data.get("report_generated")),
        "manager_name": (data.get("manager") or {}).get("name"),
        "manager_signature_date": _dt((data.get("manager") or {}).get("signature_date")),
        "summary": data.get("summary"),
        "source": data.get("source", "ecosure"),
        "assessor": data["evaluation"].get("assessor"),
        "activity_number": data["evaluation"].get("activity_number"),
    }
    violations = [{
        "category": v["category"], "code": v["code"], "question": v["question"],
        "priority": v["priority"], "response": v["response"], "findings": v["findings"],
    } for v in data["actionable_standards"]]
    detailed = [{
        "category": d["category"], "code": d["code"], "question": d["question"],
        "answer": d["answer"], "passed": d.get("passed", d["answer"] == "Yes"),
    } for d in data["detailed_standards"]]
    return assessment, violations, detailed


def photo_path(unit, start_key, code, index, ext):
    return f"{_slug(unit)}/{start_key}/{_slug(code)}_{index}.{ext}"


# --------------------------------------------------------------------------
def ingest(pdf_path, email_id=None, dry_run=False):
    rtype = detect_report_type(pdf_path)
    if rtype == "subset":
        print(f"skipping subset report ({os.path.basename(pdf_path)}) — the full report carries everything")
        return
    if rtype == "unknown":
        print(f"!! unrecognized report type, skipping {os.path.basename(pdf_path)}")
        return
    parser = _parser_for(rtype)
    data, photos = parser.parse_report(pdf_path)
    warnings = parser.validate_parse(data, photos)
    if warnings:
        print("!! parse warnings for", os.path.basename(pdf_path))
        for w in warnings:
            print("   -", w)
    unit = data["location"].get("unit_number") or "unknown"
    start_iso = _dt(data["evaluation"].get("start_datetime"))
    start_key = (start_iso or "unknown")[:19].replace(":", "").replace("-", "")

    assessment, violations, detailed = build_payload(data, unit, start_iso)

    # ---- DRY RUN: write JSON + photos locally, no network ----------------
    if dry_run:
        out = "dryrun_out"; os.makedirs(out, exist_ok=True)
        for p in photos:
            path = photo_path(unit, start_key, p["code"], p["index"], p["ext"])
            full = os.path.join(out, path.replace("/", "__"))
            with open(full, "wb") as f:
                f.write(p["bytes"])
        payload = {"assessment": assessment,
                   "violations": violations,
                   "detailed_standards": detailed,
                   "photo_paths": [photo_path(unit, start_key, p["code"], p["index"], p["ext"]) for p in photos]}
        with open(os.path.join(out, "payload.json"), "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[dry-run] wrote {len(photos)} photos + payload.json to ./{out}/")
        print(f"[dry-run] assessment: unit {unit}, {assessment['score_pct']}% "
              f"{assessment['risk_level']}, {len(violations)} violations, {len(detailed)} detailed")
        return

    # ---- LIVE: talk to Supabase -----------------------------------------
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    sb = create_client(url, key)

    # 1) raw PDF to storage
    pdf_key = f"{_slug(unit)}/{start_key}/{os.path.basename(pdf_path)}"
    with open(pdf_path, "rb") as f:
        sb.storage.from_(PDF_BUCKET).upload(
            pdf_key, f.read(),
            {"content-type": "application/pdf", "upsert": "true"})
    assessment["source_pdf_path"] = pdf_key
    assessment["email_message_id"] = email_id
    assessment["parse_warnings"] = warnings or None

    # 2) upsert the assessment (unique on unit_number+start_datetime)
    res = sb.table("ecosure_assessments").upsert(
        assessment, on_conflict="unit_number,start_datetime,source").execute()
    assessment_id = res.data[0]["id"]

    # 3) replace child rows for a clean re-ingest
    for tbl in ("ecosure_violations", "ecosure_photos", "ecosure_detailed_standards"):
        sb.table(tbl).delete().eq("assessment_id", assessment_id).execute()

    if violations:
        for v in violations:
            v["assessment_id"] = assessment_id
        sb.table("ecosure_violations").insert(violations).execute()

    if detailed:
        for d in detailed:
            d["assessment_id"] = assessment_id
        sb.table("ecosure_detailed_standards").insert(detailed).execute()

    # 4) photos to storage + rows
    photo_rows = []
    for p in photos:
        key = photo_path(unit, start_key, p["code"], p["index"], p["ext"])
        sb.storage.from_(PHOTO_BUCKET).upload(
            key, p["bytes"],
            {"content-type": f"image/{p['ext']}", "upsert": "true"})
        public_url = sb.storage.from_(PHOTO_BUCKET).get_public_url(key)
        photo_rows.append({
            "assessment_id": assessment_id, "violation_code": p["code"],
            "photo_index": p["index"], "storage_path": key,
            "public_url": public_url, "page": p["page"]})
    if photo_rows:
        sb.table("ecosure_photos").insert(photo_rows).execute()

    print(f"Ingested unit {unit}: {assessment['score_pct']}% {assessment['risk_level']}, "
          f"{len(violations)} violations, {len(photo_rows)} photos, {len(detailed)} detailed "
          f"(assessment_id={assessment_id})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--email-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ingest(a.pdf, email_id=a.email_id, dry_run=a.dry_run)
