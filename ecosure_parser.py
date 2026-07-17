"""
EcoSure / TrueView Food Safety Assessment PDF parser.

Parses the template-generated Jimmy John's EcoSure report into a structured
dict and extracts the finding photos (filtering out the ✗/✓/dash icons and
brand logos). Designed to be deterministic across all units since the report
layout is a fixed Aspose-generated template.

Usage:
    from ecosure_parser import parse_report
    data, photos = parse_report("report.pdf")
    # data  -> nested dict (JSON-serializable)
    # photos -> list of dicts: {code, index, ext, bytes, page}
"""
import re
import fitz  # PyMuPDF

JCODE_RE = re.compile(r"^(J-[A-Z]+\.\d+)$")
PRIORITY_SET = {"Minor", "Major", "Critical", "Imminent Health Risk"}
# icon fingerprints (byte size of the reused JPEG) — used only as a secondary
# guard; the primary filter is geometry (width in points).
PHOTO_MIN_WIDTH_PT = 20  # real photos ~50pt wide; icons are 8-10pt
FINDING_DETAIL_X = 98    # observations sit at ~100; issue text/wraps at ~85-95
FINDING_GAP_PT = 15      # blank line separates the issue label from its observation

HEADER_LABELS = {
    "Unit Number": "unit_number",
    "Restaurant \nName": "restaurant_name",  # label wraps two lines
    "Restaurant Name": "restaurant_name",
    "Store Type": "store_type",
    "Brand": "brand",
    "Address": "address",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Start Date Time": "start_datetime",
    "End Date Time": "end_datetime",
    "Advisor Id": "advisor_id",
    "Visit": "visit",
}

SUMMARY_CATEGORIES = [
    "Imminent Health Risk", "Cleaning and Sanitation", "Employee Health and Hygiene",
    "Time and Temperature", "Good Retail Practices", "Pest Management", "Documentation",
]


def _ordered_events(doc):
    """Flatten all pages into a single reading-order stream of text lines and
    photo-image events. Each event: (page_index, y, kind, payload)."""
    events = []
    for pno, page in enumerate(doc):
        d = page.get_text("dict")
        for b in d["blocks"]:
            if b["type"] == 1:  # image
                bb = b["bbox"]
                w = bb[2] - bb[0]
                x0 = bb[0]
                # keep only real photos: wide, in the left content column
                if w >= PHOTO_MIN_WIDTH_PT and x0 < 490:
                    events.append((pno, bb[1], "photo", {
                        "bytes": b["image"], "ext": b.get("ext", "jpeg"),
                        "x0": x0, "w": w,
                    }, x0))
            else:
                for line in b["lines"]:
                    txt = "".join(s["text"] for s in line["spans"]).strip()
                    if txt:
                        events.append((pno, line["bbox"][1], "text", txt, line["bbox"][0]))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def _parse_header_and_summary(doc):
    """Header fields, score, risk level, and the summary count matrix come from
    page 1 in a fixed order."""
    lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
    out = {"location": {}, "evaluation": {}, "summary": {}}

    # --- header key/value pairs ---
    loc_keys = {"unit_number", "restaurant_name", "store_type", "brand",
                "address", "city", "state", "zip"}
    label_lookup = {k.replace("\n", " ").strip(): v for k, v in HEADER_LABELS.items()}
    i = 0
    while i < len(lines):
        lbl = lines[i]
        # two-line wrapped label "Restaurant \n Name"
        if lbl == "Restaurant" and i + 2 < len(lines) and lines[i + 1] == "Name":
            out["location"]["restaurant_name"] = lines[i + 2]
            i += 3
            continue
        if lbl in label_lookup and i + 1 < len(lines):
            field = label_lookup[lbl]
            val = lines[i + 1]
            (out["location"] if field in loc_keys else out["evaluation"])[field] = val
            i += 2
            continue
        i += 1

    # --- score + risk level ---
    for i, l in enumerate(lines):
        if l == "Score":
            # next non-numeric line is the risk band; the "NN %" is the score
            for j in range(i + 1, min(i + 6, len(lines))):
                m = re.match(r"^(\d+)\s*%$", lines[j])
                if m:
                    out["score_pct"] = int(m.group(1))
                if lines[j] in {"Moderate Risk", "High Risk", "Low Risk",
                                "Imminent Health Risk", "Pass", "Fail"}:
                    out["risk_level"] = lines[j]
            break

    # --- summary matrix: category name followed by 4 integers ---
    for cat in SUMMARY_CATEGORIES + ["Total Counts"]:
        if cat in lines:
            idx = lines.index(cat)
            nums = []
            for k in range(idx + 1, len(lines)):
                if re.fullmatch(r"\d+", lines[k]):
                    nums.append(int(lines[k]))
                    if len(nums) == 4:
                        break
                else:
                    break
            if len(nums) == 4:
                key = "total" if cat == "Total Counts" else cat
                out["summary"][key] = dict(zip(
                    ["imminent_health_risk", "critical", "major", "minor"], nums))

    # report generated timestamp
    m = re.search(r"Report Generated:\s*(.+?)\s*Page", doc[0].get_text())
    if m:
        out["report_generated"] = m.group(1).strip()
    return out


def parse_report(path):
    doc = fitz.open(path)
    result = _parse_header_and_summary(doc)
    events = _ordered_events(doc)

    actionable = []       # violations with findings + photos
    detailed = []         # pass/fail Q&A
    general_info = {}      # MANG_FIRST etc.

    section = None         # "actionable" | "detailed"
    current_category = None
    cur = None             # current violation being built
    photos = []
    photo_counters = {}

    mode = None            # within a violation: None | "response" | "findings"
    pending_finding = None
    pending_question_prefix = ""
    finding_phase = "issue"  # within a finding: "issue" (label) then "detail" (observation)
    prev_text_y = None

    i = 0
    n = len(events)
    while i < n:
        pno, y, kind, payload, x0 = events[i]

        if kind == "photo":
            if cur is not None:
                code = cur["code"]
                idx = photo_counters.get(code, 0)
                photo_counters[code] = idx + 1
                rec = {"code": code, "index": idx, "page": pno + 1,
                       "ext": payload["ext"], "bytes": payload["bytes"]}
                photos.append(rec)
                cur["photos"].append({"code": code, "index": idx})
            i += 1
            continue

        txt = payload

        # section switches
        if txt == "Actionable standards":
            section = "actionable"; i += 1; continue
        if txt == "Detailed standards":
            section = "detailed"
            if cur:
                if pending_finding:
                    cur["findings"].append(pending_finding); pending_finding = None
                actionable.append(cur); cur = None
            i += 1; continue

        # skip repeated footers/headers
        if txt.startswith("CONFIDENTIAL AND PROPRIETARY") or \
           txt.startswith("Report Generated:") or \
           re.match(r"^Page \d+ of \d+$", txt) or \
           txt in {"Question#", "Question", "Priority", "Question# Question Priority",
                    "Question# Question", "Score", "Food Safety Assessment"}:
            i += 1; continue

        # category headers
        known_cats = set(SUMMARY_CATEGORIES) | {"General Information"}
        if txt in known_cats:
            if txt != current_category:  # genuinely new group, not a page repeat
                if section == "actionable" and cur is not None and pending_finding:
                    cur["findings"].append(pending_finding); pending_finding = None
                    mode = None
                current_category = txt
            i += 1; continue

        if section == "actionable":
            # start of the summary-list entry on p1 and the detailed entries both
            # use J-codes; here we build violations.
            # A J-code may share a line with the question + priority, or be alone.
            m = JCODE_RE.match(txt)
            merged = None
            if not m:
                # line like "J-CS.5 All sinks ... " (code + question together)
                m2 = re.match(r"^(J-[A-Z]+\.\d+)\s+(.*)$", txt)
                if m2:
                    m = True; merged = m2
            if m:
                if cur:
                    if pending_finding:
                        cur["findings"].append(pending_finding)
                        pending_finding = None
                    actionable.append(cur)
                if merged:
                    code = merged.group(1); rest = merged.group(2).strip()
                else:
                    code = txt; rest = None
                # priority might be a trailing token in the merged text
                priority = None
                if rest:
                    for p in PRIORITY_SET:
                        if rest.endswith(p):
                            priority = p; rest = rest[: -len(p)].strip()
                # any loose text seen just before this code is the question's
                # first (wrapped) line, emitted early due to cell centering
                q = (pending_question_prefix + " " + (rest or "")).strip()
                cur = {"category": current_category, "code": code,
                       "question": q, "priority": priority,
                       "response": None, "findings": [], "photos": []}
                mode = None; pending_finding = None; pending_question_prefix = ""
                i += 1; continue

            # ---- body lines ----
            if cur is not None and txt in PRIORITY_SET and not cur["priority"]:
                cur["priority"] = txt; i += 1; continue
            if cur is not None and txt == "Response:":
                mode = "response"; i += 1; continue
            if cur is not None and mode == "response" and cur["response"] is None:
                cur["response"] = txt; mode = None; i += 1; continue
            if cur is not None and txt == "Findings:":
                mode = "findings"; i += 1; continue
            if cur is not None and mode == "findings":
                if txt.startswith("•"):
                    if pending_finding:
                        cur["findings"].append(pending_finding)
                    pending_finding = {"issue": txt.lstrip("•").strip(), "detail": ""}
                    finding_phase = "issue"
                    prev_text_y = y
                    i += 1; continue
                # a line sharing a row with the NEXT J-code is that code's question
                # (question cells wrap; the code is vertically centered on line 1),
                # so it must not be absorbed into the current finding.
                k = i + 1
                while k < n and (events[k][2] != "text" or
                                 events[k][3].startswith(("CONFIDENTIAL", "Report Generated:")) or
                                 re.match(r"^Page \d+ of \d+$", events[k][3])):
                    k += 1
                if k < n:
                    nt = events[k]
                    if (nt[2] == "text" and nt[0] == pno and abs(nt[1] - y) < 14 and
                            (JCODE_RE.match(nt[3]) or re.match(r"^J-[A-Z]+\.\d+\s", nt[3]))):
                        if pending_finding:
                            cur["findings"].append(pending_finding); pending_finding = None
                        mode = None
                        pending_question_prefix = (pending_question_prefix + " " + txt).strip()
                        i += 1; continue
                if pending_finding is not None:
                    same_page_gap = (prev_text_y is not None and y - prev_text_y >= FINDING_GAP_PT)
                    # observation starts at deeper indent, or after the blank-line gap
                    if finding_phase == "issue" and x0 < FINDING_DETAIL_X and not same_page_gap:
                        pending_finding["issue"] = (pending_finding["issue"] + " " + txt).strip()
                    else:
                        finding_phase = "detail"
                        pending_finding["detail"] = (pending_finding["detail"] + " " + txt).strip()
                    prev_text_y = y
                    i += 1; continue
                i += 1; continue
            # question continuation before Response:
            if cur is not None and cur["response"] is None:
                cur["question"] = (cur["question"] + " " + txt).strip()
                i += 1; continue
            # loose text between a completed violation and the next code:
            # buffer it as the next question's first wrapped line
            pending_question_prefix = (pending_question_prefix + " " + txt).strip()
            i += 1; continue

        if section == "detailed":
            # flush any dangling pending finding from actionable
            if pending_finding and cur:
                cur["findings"].append(pending_finding); pending_finding = None
            # General Information block: CODE / label / "Answer:" / value
            m = JCODE_RE.match(txt)
            gcode = re.match(r"^(MANG_[A-Z]+)$", txt)
            if m or (re.match(r"^J-[A-Z]+\.\d+", txt)):
                code = JCODE_RE.match(txt).group(1) if m else txt.split()[0]
                # collect question until "Answer:"
                q_parts = []
                j = i + 1
                answer = None
                while j < n:
                    _, _, k2, t2 = events[j][0], events[j][1], events[j][2], events[j][3]
                    if k2 != "text":
                        j += 1; continue
                    if t2.startswith("CONFIDENTIAL") or t2.startswith("Report Generated:") \
                       or t2 in {"Question#", "Question", "Priority"}:
                        j += 1; continue  # skip page furniture, keep looking
                    if t2 == "Answer:":
                        # value is next real text line (skip footers)
                        for k in range(j + 1, n):
                            if events[k][2] != "text":
                                continue
                            tk = events[k][3]
                            if tk.startswith("CONFIDENTIAL") or tk.startswith("Report Generated:"):
                                continue
                            answer = tk; j = k; break
                        break
                    if t2 in known_cats:
                        j += 1; continue  # repeated header across page break
                    if JCODE_RE.match(t2) or re.match(r"^MANG_", t2) or \
                       t2 == "Manager name":
                        j -= 1; break
                    q_parts.append(t2)
                    j += 1
                detailed.append({"category": current_category, "code": code,
                                 "question": " ".join(q_parts).strip(),
                                 "answer": answer})
                i = j + 1; continue
            i += 1; continue

        i += 1

    if cur:
        if pending_finding:
            cur["findings"].append(pending_finding)
        actionable.append(cur)

    # tidy: flush pending findings appended inline
    for v in actionable:
        # ensure last pending finding captured (handled above) & drop empties
        v["findings"] = [f for f in v["findings"] if f.get("issue")]

    result["actionable_standards"] = actionable
    result["detailed_standards"] = detailed

    # manager sign-off
    full = "\n".join(e[3] for e in events if e[2] == "text")
    mm = re.search(r"Manager name.*?Manager signature date\s*(.+?)\s+(\d{2}/\d{2}/\d{4}[^\n]*?(?:AM|PM))",
                   full, re.S)
    if mm:
        result["manager"] = {"name": mm.group(1).strip(),
                             "signature_date": mm.group(2).strip()}
    return result, photos


def validate_parse(data, photos):
    """Return a list of human-readable warnings. Empty list = looks healthy.
    Catches the most likely symptoms of a template change so bad data doesn't
    land in Supabase silently."""
    w = []
    summ = data.get("summary", {}).get("total")
    if not summ:
        w.append("no summary total parsed")
    else:
        expected = sum(summ.values())
        got = len(data.get("actionable_standards", []))
        if expected != got:
            w.append(f"violation count {got} != summary total {expected}")
    if data.get("score_pct") is None:
        w.append("score_pct not found")
    if not data.get("risk_level"):
        w.append("risk_level not found")
    if not data["location"].get("unit_number"):
        w.append("unit_number not found")
    if not data.get("evaluation", {}).get("start_datetime"):
        w.append("start_datetime not found")
    for v in data.get("actionable_standards", []):
        if not v.get("question"):
            w.append(f"{v['code']}: empty question")
        if not v.get("priority"):
            w.append(f"{v['code']}: missing priority")
        for fnd in v.get("findings", []):
            if not fnd.get("issue"):
                w.append(f"{v['code']}: finding with empty issue")
    # detailed answers should be Yes/No/None only
    for d in data.get("detailed_standards", []):
        if d.get("answer") not in (None, "Yes", "No"):
            w.append(f"{d['code']}: unexpected answer {d['answer']!r}")
    return w


if __name__ == "__main__":
    import json, sys
    data, photos = parse_report(sys.argv[1] if len(sys.argv) > 1 else "report.pdf")
    warnings = validate_parse(data, photos)
    if warnings:
        print("VALIDATION WARNINGS:", *("  - " + x for x in warnings), sep="\n")
    print(json.dumps(data, indent=2))
    print(f"\n{len(photos)} finding photos extracted:",
          [(p["code"], p["index"]) for p in photos])
