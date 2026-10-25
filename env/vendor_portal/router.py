from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from env.db import query_all, query_one
from env.faults.manager import fault_manager
from env.seed.generator import INVOICES_DIR

router = APIRouter(prefix="/portal", tags=["Vendor Portal"])
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_current_portal_user(request: Request) -> str | None:
    return request.cookies.get("session_portal")


@router.get("/login")
async def portal_login_page(request: Request, expired: int = 0) -> Response:
    error = "Your session has expired. Please log in again to continue." if expired else None
    return templates.TemplateResponse(
        request=request,
        name="portal/login.html",
        context={
            "title": "SupplierConnect - Vendor Portal Login",
            "app_name": "SupplierConnect Vendor Portal",
            "system_name": "Portal",
            "error": error,
        },
    )


@router.post("/login")
async def portal_login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    user = query_one(
        "SELECT * FROM users WHERE username = ? AND password = ? AND system = 'portal'",
        (email, password),
    )
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="portal/login.html",
            context={
                "title": "SupplierConnect - Vendor Portal Login",
                "app_name": "SupplierConnect Vendor Portal",
                "system_name": "Portal",
                "error": "Invalid email or password for SupplierConnect portal.",
            },
            status_code=401,
        )

    response = RedirectResponse(url="/portal/invoices", status_code=303)
    response.set_cookie(key="session_portal", value=email, httponly=True)
    return response


@router.get("/logout")
async def portal_logout() -> Response:
    response = RedirectResponse(url="/portal/login", status_code=303)
    response.delete_cookie("session_portal")
    return response


@router.get("/invoices")
async def portal_invoices_list(request: Request, q: str = "") -> Response:
    # 1. Check faults
    if fault_manager.should_expire_session("portal"):
        resp = RedirectResponse(url="/portal/login?expired=1", status_code=303)
        resp.delete_cookie("session_portal")
        return resp

    await fault_manager.maybe_delay(request.url.path)

    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    # 2. Check auth
    user = get_current_portal_user(request)
    if not user:
        return RedirectResponse(url="/portal/login", status_code=303)

    # 3. Query invoices
    sql = "SELECT * FROM invoices WHERE is_on_portal = 1"
    params: list[Any] = []
    if q.strip():
        sql += " AND (invoice_number LIKE ? OR vendor_name LIKE ?)"
        wildcard = f"%{q.strip()}%"
        params.extend([wildcard, wildcard])
    sql += " ORDER BY id DESC"

    all_invoices = query_all(sql, tuple(params))

    # Apply missing invoice fault if active
    if fault_manager.state.missing_portal_invoice:
        missing_num = fault_manager.state.missing_invoice_number
        all_invoices = [inv for inv in all_invoices if inv["invoice_number"] != missing_num]

    return templates.TemplateResponse(
        request=request,
        name="portal/invoices.html",
        context={
            "title": "SupplierConnect - Invoices",
            "app_name": "SupplierConnect Vendor Portal",
            "system_name": "Portal",
            "user": user,
            "logout_url": "/portal/logout",
            "invoices": all_invoices,
            "search_query": q,
        },
    )


@router.get("/invoice/{invoice_number}")
async def portal_invoice_detail(request: Request, invoice_number: str) -> Response:
    if fault_manager.should_expire_session("portal"):
        resp = RedirectResponse(url="/portal/login?expired=1", status_code=303)
        resp.delete_cookie("session_portal")
        return resp

    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    user = get_current_portal_user(request)
    if not user:
        return RedirectResponse(url="/portal/login", status_code=303)

    invoice = query_one("SELECT * FROM invoices WHERE invoice_number = ?", (invoice_number,))
    if not invoice or (not invoice["is_on_portal"] and fault_manager.state.missing_portal_invoice):
        raise HTTPException(status_code=404, detail="Invoice not found")

    return templates.TemplateResponse(
        request=request,
        name="portal/invoice_detail.html",
        context={
            "title": f"Invoice {invoice_number} - SupplierConnect",
            "app_name": "SupplierConnect Vendor Portal",
            "system_name": "Portal",
            "user": user,
            "logout_url": "/portal/logout",
            "invoice": invoice,
        },
    )


@router.get("/download/{filename}")
async def portal_download_pdf(filename: str) -> Response:
    safe_filename = os.path.basename(filename)
    filepath = os.path.join(INVOICES_DIR, safe_filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="application/pdf", filename=safe_filename)
