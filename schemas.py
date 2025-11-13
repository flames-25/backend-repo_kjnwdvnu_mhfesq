"""
Database Schemas for Onebox Email Aggregator

Each Pydantic model corresponds to a MongoDB collection (lowercased class name).
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

class EmailAccount(BaseModel):
    provider: str = Field(..., description="e.g., gmail, outlook, custom")
    host: str = Field(..., description="IMAP host")
    port: int = Field(993, description="IMAP port")
    username: str = Field(..., description="IMAP username/email")
    password: str = Field(..., description="IMAP app password")
    use_ssl: bool = Field(True, description="Use SSL for IMAP")
    description: Optional[str] = Field(None, description="Label for account")

class EmailMessage(BaseModel):
    account_id: str = Field(..., description="Reference to emailaccount _id")
    message_id: str = Field(..., description="RFC Message-ID")
    uid: Optional[int] = Field(None, description="IMAP UID")
    folder: str = Field(..., description="Mailbox folder, e.g., INBOX")
    subject: Optional[str] = None
    sender: Optional[str] = None
    to: Optional[List[str]] = None
    cc: Optional[List[str]] = None
    date: Optional[datetime] = None
    snippet: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    labels: Optional[List[str]] = None
    ai_category: Optional[str] = Field(None, description="Interested | Meeting Booked | Not Interested | Spam | Out of Office")
    raw_headers: Optional[Dict[str, Any]] = None

class AgendaDoc(BaseModel):
    title: str
    content: str
    tags: Optional[List[str]] = None

class InterestedEvent(BaseModel):
    email_id: str
    webhook_url: Optional[str] = None
