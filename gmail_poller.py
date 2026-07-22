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

# Match both report emails: EcoSure evaluations and CMX self-assessments.
# The self-assessment email ships three PDFs; ingest() skips the two subsets.
SEARCH = ('{subject:EcoSure subject:"Self Assessment Has Been Completed"} '
          'has:attachment filename:pdf -label:ecosure-processed newer_than:7d')
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


def run():
    svc = _service()
    label_id = _ensure_label(svc)
    msgs = svc.users().messages().list(userId="me", q=SEARCH).execute().get("messages", [])
    print(f"{len(msgs)} EcoSure message(s) to process")
    for m in msgs:
        mid = m["id"]
        for fn, blob in _pdf_attachments(svc, mid):
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                tf.write(blob); path = tf.name
            try:
                supabase_ingest.ingest(path, email_id=mid)
            finally:
                os.unlink(path)
        svc.users().messages().modify(
            userId="me", id=mid, body={"addLabelIds": [label_id]}).execute()


if __name__ == "__main__":
    run()
