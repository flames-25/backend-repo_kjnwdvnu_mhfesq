import os
import imaplib
import email
from email.header import decode_header, make_header
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents
from bson import ObjectId

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Utils ----------

def to_str_oid(doc: Dict[str, Any]):
    if not doc:
        return doc
    d = {**doc}
    if d.get("_id"):
        d["_id"] = str(d["_id"])  # type: ignore
    return d


def safe_decode(value):
    try:
        if isinstance(value, bytes):
            return value.decode(errors="ignore")
        return str(value)
    except Exception:
        return str(value)


def decode_mime_words(s):
    try:
        return str(make_header(decode_header(s))) if s else None
    except Exception:
        return s


# ---------- Schemas ----------
class AccountIn(BaseModel):
    provider: str
    host: str
    port: int = 993
    username: str
    password: str
    use_ssl: bool = True
    description: Optional[str] = None


class AccountOut(AccountIn):
    id: str


class SyncRequest(BaseModel):
    folders: Optional[List[str]] = None  # default INBOX
    days: int = 30


class MarkInterestedRequest(BaseModel):
    webhook_url: Optional[str] = None


# ---------- Basic Routes ----------
@app.get("/")
def read_root():
    return {"message": "Onebox backend running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "❌ Not Set",
        "database_name": "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Set"
            response["database_name"] = getattr(db, "name", "unknown")
            cols = db.list_collection_names()
            response["collections"] = cols
            response["connection_status"] = "Connected"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


# ---------- Accounts ----------
@app.post("/accounts", response_model=AccountOut)
def add_account(acc: AccountIn):
    acc_id = create_document("emailaccount", acc)
    return {"id": acc_id, **acc.model_dump()}


@app.get("/accounts", response_model=List[AccountOut])
def list_accounts():
    docs = get_documents("emailaccount")
    out: List[AccountOut] = []
    for d in docs:
        d = to_str_oid(d)
        out.append(
            AccountOut(
                id=d["_id"],
                provider=d.get("provider"),
                host=d.get("host"),
                port=d.get("port", 993),
                username=d.get("username"),
                password=d.get("password"),
                use_ssl=d.get("use_ssl", True),
                description=d.get("description"),
            )
        )
    return out


# ---------- IMAP Sync ----------

def categorize_email(subject: Optional[str], body: Optional[str]) -> Optional[str]:
    text = f"{subject or ''} \n {body or ''}".lower()
    if any(k in text for k in ["out of office", "ooo", "vacation auto-reply", "automatic reply"]):
        return "Out of Office"
    if any(k in text for k in ["not interested", "no longer interested", "unsubscribe"]):
        return "Not Interested"
    if any(k in text for k in ["meeting", "calendar", "schedule", "book a time", "let's meet"]):
        if any(k in text for k in ["book", "link", "schedule", "slot", "cal.com", "calendly"]):
            return "Meeting Booked"
    if any(k in text for k in ["buy", "interested", "sounds good", "let's talk", "let us talk"]):
        return "Interested"
    if any(k in text for k in ["viagra", "lottery", "claim prize", "crypto giveaway", "spam"]):
        return "Spam"
    return None


def extract_body(msg: email.message.Message):
    body_text = None
    body_html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition"))
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    body_text = safe_decode(payload)
            elif ctype == "text/html" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = safe_decode(payload)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            if msg.get_content_type() == "text/html":
                body_html = safe_decode(payload)
            else:
                body_text = safe_decode(payload)
    return body_text, body_html


def connect_imap(host: str, port: int, username: str, password: str, use_ssl: bool):
    if use_ssl:
        M = imaplib.IMAP4_SSL(host, port)
    else:
        M = imaplib.IMAP4(host, port)
    M.login(username, password)
    return M


def sync_account(account: Dict[str, Any], folders: Optional[List[str]], days: int):
    host = account.get("host")
    port = int(account.get("port", 993))
    username = account.get("username")
    password = account.get("password")
    use_ssl = bool(account.get("use_ssl", True))

    try:
        M = connect_imap(host, port, username, password, use_ssl)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"IMAP login failed: {e}")

    try:
        since_date = (datetime.now() - timedelta(days=days)).strftime('%d-%b-%Y')
        target_folders = folders or ["INBOX"]
        inserted = 0
        for folder in target_folders:
            try:
                M.select(folder, readonly=True)
            except Exception:
                continue
            typ, data = M.search(None, f'(SINCE {since_date})')
            if typ != 'OK':
                continue
            id_list = data[0].split() if data and data[0] else []
            for num in id_list[-1000:]:  # cap
                typ, msg_data = M.fetch(num, '(RFC822 UID)')
                if typ != 'OK' or not msg_data:
                    continue
                raw = None
                for part in msg_data:
                    if isinstance(part, tuple):
                        raw = part[1]
                        break
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                subject = decode_mime_words(msg.get('Subject'))
                from_ = decode_mime_words(msg.get('From'))
                to = msg.get_all('To', [])
                cc = msg.get_all('Cc', [])
                date_hdr = msg.get('Date')
                try:
                    date_parsed = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else None
                except Exception:
                    date_parsed = None
                body_text, body_html = extract_body(msg)
                snippet = (body_text or body_html or "")[:280]
                mid = msg.get('Message-ID') or f"uid-{safe_decode(num)}-{folder}-{username}"
                # check existing by message_id
                existing = db["emailmessage"].find_one({"message_id": mid, "account_id": str(account['_id'])})
                if existing:
                    continue
                ai_cat = categorize_email(subject, body_text or body_html)
                doc = {
                    "account_id": str(account["_id"]),
                    "message_id": mid,
                    "uid": None,
                    "folder": folder,
                    "subject": subject,
                    "sender": from_,
                    "to": to,
                    "cc": cc,
                    "date": date_parsed or datetime.now(timezone.utc),
                    "snippet": snippet,
                    "body_text": body_text,
                    "body_html": body_html,
                    "labels": [],
                    "ai_category": ai_cat,
                    "raw_headers": {k: safe_decode(v) for k, v in msg.items()},
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
                db["emailmessage"].insert_one(doc)
                inserted += 1
        try:
            M.logout()
        except Exception:
            pass
        return {"inserted": inserted}
    except Exception as e:
        try:
            M.logout()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sync/{account_id}")
def trigger_sync(account_id: str, req: SyncRequest, background_tasks: BackgroundTasks):
    acc = db["emailaccount"].find_one({"_id": ObjectId(account_id)})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # run sync in background
    background_tasks.add_task(sync_account, acc, req.folders, req.days)
    return {"status": "started"}


# ---------- Search & List ----------
@app.get("/emails")
def list_emails(
    account_id: Optional[str] = None,
    folder: Optional[str] = None,
    q: Optional[str] = Query(None, description="simple substring search in subject/body"),
    limit: int = 50,
    skip: int = 0,
):
    flt: Dict[str, Any] = {}
    if account_id:
        flt["account_id"] = account_id
    if folder:
        flt["folder"] = folder
    if q:
        flt["$or"] = [
            {"subject": {"$regex": q, "$options": "i"}},
            {"body_text": {"$regex": q, "$options": "i"}},
            {"body_html": {"$regex": q, "$options": "i"}},
        ]
    cur = db["emailmessage"].find(flt).sort("date", -1).skip(skip).limit(min(200, max(1, limit)))
    docs = [to_str_oid(d) for d in cur]
    return {"items": docs, "count": len(docs)}


@app.get("/emails/folders")
def list_folders(account_id: str):
    pipeline = [
        {"$match": {"account_id": account_id}},
        {"$group": {"_id": "$folder", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    res = list(db["emailmessage"].aggregate(pipeline))
    return [{"folder": r.get("_id"), "count": r.get("count", 0)} for r in res]


# ---------- Interested: Slack + Webhook ----------
import requests


def notify_slack(text: str):
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, json={"text": text}, timeout=5)
    except Exception:
        pass


@app.post("/emails/{email_id}/mark/interested")
def mark_interested(email_id: str, payload: MarkInterestedRequest):
    doc = db["emailmessage"].find_one({"_id": ObjectId(email_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Email not found")
    db["emailmessage"].update_one({"_id": doc["_id"]}, {"$set": {"ai_category": "Interested"}})

    # Slack
    subj = doc.get("subject") or "(no subject)"
    sender = doc.get("sender") or "unknown"
    notify_slack(f"New Interested email from {sender}: {subj}")

    # Webhook trigger
    if payload.webhook_url:
        try:
            requests.post(payload.webhook_url, json={
                "event": "interested",
                "email_id": str(doc["_id"]),
                "subject": subj,
                "sender": sender,
                "account_id": doc.get("account_id"),
            }, timeout=5)
        except Exception:
            pass

    return {"status": "ok"}


# ---------- Simple RAG: Suggested Replies ----------
# Store agenda docs and embed by naive bag-of-words; retrieve top docs and craft template reply

class AgendaIn(BaseModel):
    title: str
    content: str
    tags: Optional[List[str]] = None


@app.post("/agenda")
def add_agenda(doc: AgendaIn):
    _id = create_document("agendadoc", doc)
    return {"id": _id}


def simple_score(query: str, doc: str) -> int:
    q = set(query.lower().split())
    d = set(doc.lower().split())
    return len(q & d)


class SuggestRequest(BaseModel):
    email_id: str


@app.post("/suggest-reply")
def suggest_reply(req: SuggestRequest):
    em = db["emailmessage"].find_one({"_id": ObjectId(req.email_id)})
    if not em:
        raise HTTPException(status_code=404, detail="Email not found")
    subj = em.get("subject") or ""
    body = em.get("body_text") or em.get("body_html") or ""
    query = f"{subj} {body}"
    docs = list(db["agendadoc"].find())
    best = None
    best_score = -1
    for d in docs:
        score = simple_score(query, f"{d.get('title','')} {d.get('content','')}")
        if score > best_score:
            best_score = score
            best = d
    # naive template
    reply = "Thank you for your email."
    if best:
        reply += f"\n\n{best.get('content')}"
    # Specific action if interested
    if (em.get("ai_category") == "Interested") and ("cal.com" in (best.get("content", "") if best else "")):
        reply += "\n\nYou can book a slot using the link above."
    return {"suggestion": reply.strip()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
