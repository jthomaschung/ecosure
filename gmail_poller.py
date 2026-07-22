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
import os, base64, tempfile
from googleapiclient.discovery import build
from google.oauth2 import service_account
import supabase_ingest

# Match the two food-safety report emails only:
#   - EcoSure/TrueView evaluations            (subject contains "EcoSure")
#   - CMX Food Safety self-assessments        (subject: "Food Safety - Self Assessment")
# Explicitly exclude the Ops self-assessment  (subject contains "Ops"), which is a
# different survey with no parser here. The self-assessment email ships three PDFs;
# ingest() keeps the full report and skips the two subsets.
# NOTE: full-year backfill window. After the backfill completes, change this to
# newer_than:30d for steady-state runs.
SEARCH = ('{subject:EcoSure subject:"Food Safety - Self Assessment"} '
          '-subject:Ops has:attachment filename:pdf '
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


def _all_message_ids(svc):
    """Page through every message matching SEARCH (list() returns one page)."""
    ids, page_token = [], None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=SEARCH, maxResults=500, pageToken=page_token).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def run():
    svc = _service()
    label_id = _ensure_label(svc)
    msg_ids = _all_message_ids(svc)
    print(f"{len(msg_ids)} message(s) to process")
    ok = failed = 0
    for mid in msg_ids:
        try:
            for fn, blob in _pdf_attachments(svc, mid):
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    tf.write(blob); path = tf.name
                try:
                    supabase_ingest.ingest(path, email_id=mid)
                finally:
                    os.unlink(path)
            # only label processed if the whole message handled without error,
            # so a transient failure is retried on the next run instead of lost
            svc.users().messages().modify(
                userId="me", id=mid, body={"addLabelIds": [label_id]}).execute()
            ok += 1
        except Exception as e:
            failed += 1
            print(f"!! error on message {mid}: {e} (left unlabeled for retry)")
    print(f"done: {ok} message(s) processed, {failed} failed/left for retry")


if __name__ == "__main__":
    run()
