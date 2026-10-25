from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from env.db import query_all, query_one
from env.faults.manager import fault_manager
from env.seed.generator import INVOICES_DIR

router = APIRouter(prefix="/mail", tags=["Corporate Mailbox"])
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)


@router.get("/inbox")
async def mail_inbox_list(request: Request, q: str = "") -> Response:
    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    sql = "SELECT * FROM emails"
    params: list[Any] = []
    if q.strip():
        sql += " WHERE sender LIKE ? OR subject LIKE ? OR body LIKE ?"
        wildcard = f"%{q.strip()}%"
        params.extend([wildcard, wildcard, wildcard])
    sql += " ORDER BY id DESC"

    emails = query_all(sql, tuple(params))

    return templates.TemplateResponse(
        request=request,
        name="mailbox/inbox.html",
        context={
            "title": "Corporate Mailbox - Accounts Payable",
            "app_name": "Corporate Mailbox",
            "system_name": "Mailbox",
            "emails": emails,
            "search_query": q,
        },
    )


@router.get("/view/{email_id}")
async def mail_view_email(request: Request, email_id: int) -> Response:
    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    email = query_one("SELECT * FROM emails WHERE id = ?", (email_id,))
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    return templates.TemplateResponse(
        request=request,
        name="mailbox/view.html",
        context={
            "title": f"Email: {email['subject']}",
            "app_name": "Corporate Mailbox",
            "system_name": "Mailbox",
            "email": email,
        },
    )


@router.get("/download/{filename}")
async def mail_download_attachment(filename: str) -> Response:
    safe_filename = os.path.basename(filename)
    filepath = os.path.join(INVOICES_DIR, safe_filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Attachment file not found")
    return FileResponse(filepath, media_type="application/pdf", filename=safe_filename)
