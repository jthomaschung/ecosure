"""
Poll Gmail for new EcoSure reports and ingest each one.

Matches your atlas-training-automation pattern (Gmail API + scheduled run).
Runs the search, downloads the PDF attachment of each unseen message, calls
supabase_ingest.ingest(), then labels the message PROCESSED so it isn't
re-ingested. Ingest itself is idempotent, so a double-run is harmless.

Auth: a Google service account with domain-wide delegation, or an OAuth
token.json for the mailbox that receives the reports (guestfeedback@atlaswe.com
appears on the distribution). Reuse whatever the Certified-Manager Gmail
monitor already uses.

Env:
  GMAIL_USER            mailbox to read (e.g. guestfeedback@atlaswe.com)
  GOOGLE_APPLICATION_CREDENTIALS  service-account json (delegated)
  plus the SUPABASE_* vars used by supabase_ingest
"""
import os, sys, base64, tempfile
from googleapiclient.discovery import build
from google.oauth2 import service_account
import supabase_ingest
from ops_ingest import ingest_ops
from ops_parser import NotAnOpsAssessment

# Match the two food-safety report emails only:
#   - EcoSure/TrueView evaluations            (subject contains "EcoSure")
#   - CMX Food Safety self-assessments        (subject: "Food Safety - Self Assessment")
# Explicitly exclude the Ops self-assessment  (subject contains "Ops"), which is a
# different survey with no parser here. The self-assessment email ships three PDFs;
# ingest() keeps the full report and skips the two subsets.
# NOTE: full-year backfill window. After the backfill completes, change this to
# newer_than:30d for steady-state runs.
# Two report families, each with its own query and its own ingest.
#
# FOOD SAFETY — EcoSure/TrueView evaluations and CMX Food Safety
# self-assessments. "-subject:Ops" keeps the Ops reports out: they share the
# words "Self Assessment" but are a different report with different
# departments.
SEARCH = ('{subject:EcoSure subject:"Food Safety - Self Assessment"} '
          '-subject:Ops has:attachment filename:pdf '
          '-label:ecosure-processed after:2026/01/01')

# OPS ASSESSMENTS — "Ops Assessment" (FBC visit) and "Ops Self-Assessment"
# (the store's own audit).
#
# These used to come from a Playwright scrape of the CMX Tableau dashboard.
# That failed silently: the dashboard serves whatever period its default
# filter is on, so once the period rolled the scraper re-downloaded the same
# stale crosstab every morning — 367 identical rows upserted daily, every run
# green, nothing new in the database for days.
#
# The email carries the same numbers per assessment, so there is no period
# filter to drift and no dashboard layout to break.
OPS_SEARCH = ('subject:"Ops" subject:"Assessment" has:attachment filename:pdf '
              '-label:ecosure-processed after:2026/01/01')
PROCESSED_LABEL = "ecosure-processed"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _service():
    creds = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=SCOPES
    ).with_subject(os.environ["GMAIL_USER"])
    return build("gmail", "v1", credentials=creds)


def _ensure_label(svc):
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for l in labels:
        if l["name"] == PROCESSED_LABEL:
            return l["id"]
    return svc.users().labels().create(
        userId="me", body={"name": PROCESSED_LABEL}).execute()["id"]


def _pdf_attachments(svc, msg_id):
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    out = []
    def walk(parts):
        for p in parts or []:
            fn = p.get("filename", "")
            if fn.lower().endswith(".pdf") and p.get("body", {}).get("attachmentId"):
                att = svc.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=p["body"]["attachmentId"]).execute()
                out.append((fn, base64.urlsafe_b64decode(att["data"])))
            walk(p.get("parts"))
    walk(msg["payload"].get("parts"))
    return out


def _all_message_ids(svc, query=None):
    """Page through every message matching SEARCH (list() returns one page).

    includeSpamTrash=True is load-bearing, not defensive.

    Gmail search excludes Trash and Spam by default. These assessment emails
    arrive in volume — several a day across the fleet — and get cleared out of
    the inbox by hand, often the same day. This job runs once daily at 09:02
    UTC, so anything deleted before that run was invisible to it and silently
    never ingested.

    That is what stopped ingestion after 2026-09-27: the job kept running and
    kept reporting "0 message(s) to process" while nine days of assessments sat
    in Trash. A clean success with nothing to show is the worst failure mode
    there is, because nothing alerts on it.

    Note Gmail purges Trash after 30 days, so a message deleted and then purged
    before a run is gone for good. The ecosure-processed label and
    email_message_id in the database still prevent double-ingestion of anything
    already handled.
    """
    ids, page_token = [], None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=query or SEARCH, maxResults=500, pageToken=page_token,
            includeSpamTrash=True).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _process(svc, label_id, msg_ids, handler, what):
    """Run one family of reports through its ingest.

    `handler(path, email_id)` does the work. A NotAnOpsAssessment (or any
    handler-declared skip) is not a failure: the Ops emails carry a "Report
    Tags" PDF alongside the assessment, and skipping it is the correct
    outcome, not an error.
    """
    ok = failed = skipped = 0
    for mid in msg_ids:
        try:
            handled_any = False
            for fn, blob in _pdf_attachments(svc, mid):
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    tf.write(blob); path = tf.name
                try:
                    try:
                        handler(path, mid)
                        handled_any = True
                    except NotAnOpsAssessment as e:
                        skipped += 1
                        print(f"   skip {fn}: {e}")
                finally:
                    os.unlink(path)

            # Label only when the message handled without error, so a
            # transient failure is retried next run instead of lost.
            svc.users().messages().modify(
                userId="me", id=mid, body={"addLabelIds": [label_id]}).execute()
            if handled_any:
                ok += 1
        except Exception as e:
            failed += 1
            print(f"!! error on message {mid}: {e} (left unlabeled for retry)")

    print(f"{what}: {ok} ingested, {skipped} attachment(s) skipped, "
          f"{failed} failed/left for retry")
    return ok, failed


def run():
    svc = _service()
    label_id = _ensure_label(svc)

    food_ids = _all_message_ids(svc, SEARCH)
    ops_ids = _all_message_ids(svc, OPS_SEARCH)
    print(f"food safety: {len(food_ids)} message(s); ops: {len(ops_ids)} message(s)")

    ok1, failed1 = _process(
        svc, label_id, food_ids,
        lambda path, mid: supabase_ingest.ingest(path, email_id=mid),
        "food safety")

    ok2, failed2 = _process(
        svc, label_id, ops_ids,
        lambda path, mid: ingest_ops(path, email_id=mid),
        "ops assessments")

    total_ok, total_failed = ok1 + ok2, failed1 + failed2
    print(f"done: {total_ok} message(s) processed, {total_failed} failed/left for retry")

    # A run that ingests nothing while messages matched is the failure mode
    # that hid the last outage: green runs, no data, nobody alerted. Say so.
    if (food_ids or ops_ids) and total_ok == 0:
        print("!! messages matched but nothing was ingested", file=sys.stderr)

    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
