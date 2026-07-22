"""
Parser for the Jimmy John's CMX "Food Safety - Self Assessment" PDF.

This is a DIFFERENT report from the EcoSure/TrueView evaluation:
  - internal self-audit by a district manager (not a third-party advisor)
  - table-based layout (Question / Observation / Earned / Avail / Risk)
  - "answer:" + "Issue Notes:" instead of "Response:" + "Findings:"
  - departments carry "&" and there are no J-codes

The email ships three PDFs (full / non-compliant / IHR). Only the FULL report
("Food Safety - Self Assessment", without "(Non-Compliant Report)") is parsed —
the other two are subsets. is_self_assessment() detects the full report.

parse_report(path) -> (data_dict, photos), mirroring the EcoSure parser so the
same Supabase ingest can consume it.
"""
import re
import fitz

DEPARTMENTS = [
    "Imminent Health Risk", "Cleaning & Sanitation", "Employee Health and Hygiene",
    "Employee Health", "Time & Temperature", "Time &Temperature",
    "Good Retail Practices", "Pest Management", "Documentation",
]
RISK_VALUES = {"Acceptable", "Minor", "Major", "Critical", "Imminent Health Risk"}
# map self-assessment department names onto the EcoSure category names so both
# report types group together in the dashboard
CATEGORY_NORMALIZE = {
    "Cleaning & Sanitation": "Cleaning and Sanitation",
    "Employee Health": "Employee Health and Hygiene",
    "Employee Health and Hygiene": "Employee Health and Hygiene",
    "Time & Temperature": "Time and Temperature",
    "Time &Temperature": "Time and Temperature",
    "Imminent Health Risk": "Imminent Health Risk",
    "Good Retail Practices": "Good Retail Practices",
    "Pest Management": "Pest Management",
    "Documentation": "Documentation",
}
# real finding photos: wider than the logo (36pt), heavier than the signature (~4KB)
PHOTO_MIN_W = 100
PHOTO_MIN_BYTES = 8000


def is_self_assessment(path):
    """True only for the FULL self-assessment (not the non-compliant/IHR subsets)."""
    try:
        t = fitz.open(path)[0].get_text()
    except Exception:
        return False
    first = t.strip().splitlines()[0] if t.strip() else ""
    return first.strip() == "Food Safety - Self Assessment"


def _parse_header(doc):
    lines = [l.rstrip() for l in doc[0].get_text().split("\n")]
    txt = "\n".join(lines)
    out = {"location": {}, "evaluation": {}, "summary": {}}

    m = re.search(r"Activity Number\s+(\S+)", txt)
    out["evaluation"]["activity_number"] = m.group(1) if m else None

    # store number: the first standalone numeric line in the header. The label
    # before it varies by franchise type ("Franchisee", "Restaurant Team", ...),
    # so key off the number itself rather than the label.
    hdr = lines[:20]
    for i, l in enumerate(hdr):
        if re.fullmatch(r"\d{2,6}", l.strip()):
            out["location"]["unit_number"] = l.strip()
            rest = [x.strip() for x in hdr[i + 1:i + 3] if x.strip()]
            if rest:
                out["location"]["address"] = rest[0]
            if len(rest) >= 2:
                cs = rest[1]  # "Bellevue, Nebraska US"
                mm = re.match(r"^(.*?),\s*(.*?)\s+US$", cs) or re.match(r"^(.*?),\s*(.*)$", cs)
                if mm:
                    out["location"]["city"] = mm.group(1).strip()
                    out["location"]["state"] = mm.group(2).strip()
            break

    m = re.search(r"Assessor:\s*\n?\s*(.+)", txt)
    out["evaluation"]["assessor"] = m.group(1).strip() if m else None

    # two datetimes appear after the Start/End labels, each followed by a tz line
    dts = re.findall(r"([A-Z][a-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)", txt)
    if dts:
        out["evaluation"]["start_datetime"] = dts[0]
    if len(dts) > 1:
        out["evaluation"]["end_datetime"] = dts[1]

    m = re.search(r"Score:\s*([\d.]+)%", txt)
    out["score_pct"] = round(float(m.group(1))) if m else None
    m = re.search(r"Rating:\s*(.+)", txt)
    out["risk_level"] = m.group(1).strip() if m else None

    # The summary matrix's column layout varies (the Imminent Health Risk column
    # is dropped when there are none) and is absent entirely on a 100% report.
    # So the authoritative severity totals are computed from the parsed questions
    # (below, in parse_report); here we only capture the reported "Overall" sum,
    # if present, as an independent cross-check.
    mm = re.search(r"Overall((?:\s+\d+){2,4})", txt)
    if mm:
        out["summary"]["_overall_reported"] = sum(int(n) for n in re.findall(r"\d+", mm.group(1)))
    return out


def _split_question(qcell):
    """'Cleaning & Sanitation - Sanitizer is...' -> (normalized dept, question)."""
    q = re.sub(r"\s+", " ", (qcell or "").replace("\n", " ")).strip()
    # rejoin words broken by a hyphenated line wrap ("unders- tands" -> "understands")
    q = re.sub(r"(?<=[a-z])- (?=[a-z])", "", q)
    for d in sorted(DEPARTMENTS, key=len, reverse=True):
        if q.startswith(d + " - ") or q.startswith(d + " -"):
            return CATEGORY_NORMALIZE.get(d, d), q[len(d):].lstrip(" -").strip()
    return None, q


def _parse_observation(obs):
    """'answer: No\\nIssue Notes:\\n• a\\n• b' -> ('No', ['a','b'])."""
    obs = obs or ""
    ans = None
    m = re.search(r"answer:\s*(Yes|No|N/?A)", obs, re.I)
    if m:
        ans = m.group(1).upper().replace("N/A", "N/A") if m.group(1).upper() in ("N/A", "NA") else m.group(1).capitalize()
        if ans.upper() in ("NA", "N/A"):
            ans = "N/A"
    notes = [re.sub(r"\s+", " ", b).strip() for b in re.findall(r"•\s*([^\n•]+)", obs)]
    return ans, [n for n in notes if n]


def _norm_risk(risk):
    r = re.sub(r"\s+", " ", (risk or "").replace("\n", " ")).strip()
    return r or None


def parse_report(path):
    doc = fitz.open(path)
    result = _parse_header(doc)

    questions = []          # every question row (compliant + not)
    last = None
    for pno, page in enumerate(doc):
        for tab in page.find_tables().tables:
            for row in tab.extract():
                if not row or len(row) < 2:
                    continue
                qcell = row[0]
                if (qcell or "").strip() == "Question":
                    continue  # repeated header
                # column count varies (side-by-side photos can inject an extra
                # empty column), so locate cells by content, not fixed index:
                obs = next((c for c in row if c and "answer:" in c), "")
                risk = None
                for c in row:                       # risk = a cell that IS a risk value
                    nc = _norm_risk(c)
                    if nc in RISK_VALUES:
                        risk = nc
                dept, question = _split_question(qcell)
                # continuation fragment (wrapped question, no dept prefix, no obs)
                if dept is None and not (obs or "").strip() and last is not None:
                    last["question"] = (last["question"] + " " + question).strip()
                    continue
                if dept is None and not question:
                    continue
                ans, notes = _parse_observation(obs)
                rec = {
                    "category": dept, "question": question,
                    "answer": ans, "risk": risk,
                    "notes": notes, "page": pno + 1,
                    "_y": tab.bbox[1],
                }
                questions.append(rec)
                last = rec

    # photos: real finding images, associated to the nearest preceding question
    # in global reading order (handles photos that wrap onto the next page)
    photos = []
    order = sorted(((q["page"], q["_y"], qi) for qi, q in enumerate(questions)))
    counters = {}
    for pno, page in enumerate(doc):
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 1:
                continue
            bb = b["bbox"]; w = bb[2] - bb[0]
            if w < PHOTO_MIN_W or len(b["image"]) < PHOTO_MIN_BYTES:
                continue  # logo / signature
            key = (pno + 1, bb[1] + 40)
            preceding = [qi for (p, y, qi) in order if (p, y) <= key]
            qi = preceding[-1] if preceding else (order[0][2] if order else None)
            code = f"SA-{qi:03d}" if qi is not None else f"SA-p{pno+1}"
            idx = counters.get(code, 0); counters[code] = idx + 1
            photos.append({"code": code, "index": idx, "page": pno + 1,
                           "ext": b.get("ext", "jpeg"), "bytes": b["image"]})

    # build actionable (non-compliant) + detailed (everything), mirroring EcoSure
    actionable, detailed = [], []
    for qi, q in enumerate(questions):
        code = f"SA-{qi:03d}"
        passed = (q["answer"] in ("Yes", "N/A")) or (q["risk"] == "Acceptable")
        detailed.append({"category": q["category"], "code": code,
                         "question": q["question"], "answer": q["answer"],
                         "passed": passed})
        if not passed:
            actionable.append({
                "category": q["category"], "code": code, "question": q["question"],
                "priority": q["risk"], "response": q["answer"],
                "findings": [{"issue": n, "detail": ""} for n in q["notes"]],
                "photos": [p["index"] for p in photos if p["code"] == code],
            })

    result["actionable_standards"] = actionable
    result["detailed_standards"] = detailed
    result["source"] = "self_assessment"
    # authoritative severity totals, computed from the parsed items
    sev_key = {"Imminent Health Risk": "imminent_health_risk", "Critical": "critical",
               "Major": "major", "Minor": "minor"}
    total = {"imminent_health_risk": 0, "critical": 0, "major": 0, "minor": 0}
    for v in actionable:
        k = sev_key.get(v["priority"])
        if k:
            total[k] += 1
    result["summary"]["total"] = total
    result["manager"] = {"name": result["evaluation"].get("assessor"),
                         "signature_date": result["evaluation"].get("end_datetime")}
    return result, photos


def validate_parse(data, photos):
    w = []
    got = len(data.get("actionable_standards", []))
    reported = data.get("summary", {}).get("_overall_reported")
    if reported is not None and reported != got:
        w.append(f"non-compliant count {got} != summary overall {reported}")
    # every non-compliant item must map to a known severity (catches a risk value
    # that landed in the wrong column and wasn't recognized)
    tot = data.get("summary", {}).get("total")
    if tot is not None and sum(tot.values()) != got:
        w.append(f"severity totals sum to {sum(tot.values())} but {got} non-compliant items")
    if data.get("score_pct") is None:
        w.append("score_pct not found")
    if not data.get("risk_level"):
        w.append("risk_level not found")
    if not data["location"].get("unit_number"):
        w.append("unit_number not found")
    for v in data.get("actionable_standards", []):
        if not v.get("category"):
            w.append(f"{v['code']}: no department")
        if not v.get("question"):
            w.append(f"{v['code']}: empty question")
    return w


if __name__ == "__main__":
    import json, sys
    p = sys.argv[1] if len(sys.argv) > 1 else "self_full.pdf"
    print("is_self_assessment:", is_self_assessment(p))
    data, photos = parse_report(p)
    print(json.dumps({k: v for k, v in data.items()
                      if k not in ("detailed_standards",)}, indent=2)[:2000])
    print("warnings:", validate_parse(data, photos), "| photos:", len(photos),
          "| detailed:", len(data["detailed_standards"]))
