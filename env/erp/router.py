from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from env.db import execute_write, query_all, query_one
from env.faults.manager import fault_manager
from env.seed.generator import INVOICES_DIR

router = APIRouter(prefix="/erp", tags=["Acme ERP"])
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_current_erp_user(request: Request) -> str | None:
    return request.cookies.get("session_erp")


@router.get("/login")
async def erp_login_page(request: Request, expired: int = 0) -> Response:
    error = "Your ERP session has expired due to inactivity. Please sign in again." if expired else None
    return templates.TemplateResponse(
        request=request,
        name="erp/login.html",
        context={
            "title": "Acme ERP - Login",
            "app_name": "Acme Internal ERP",
            "system_name": "ERP",
            "error": error,
        },
    )


@router.post("/login")
async def erp_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    user = query_one(
        "SELECT * FROM users WHERE username = ? AND password = ? AND system = 'erp'",
        (username, password),
    )
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="erp/login.html",
            context={
                "title": "Acme ERP - Login",
                "app_name": "Acme Internal ERP",
                "system_name": "ERP",
                "error": "Invalid ERP username or password.",
            },
            status_code=401,
        )

    response = RedirectResponse(url="/erp/bills", status_code=303)
    response.set_cookie(key="session_erp", value=username, httponly=True)
    return response


@router.get("/logout")
async def erp_logout() -> Response:
    response = RedirectResponse(url="/erp/login", status_code=303)
    response.delete_cookie("session_erp")
    return response


@router.get("/bills")
async def erp_bills_list(request: Request, q: str = "", added: str = "") -> Response:
    if fault_manager.should_expire_session("erp"):
        resp = RedirectResponse(url="/erp/login?expired=1", status_code=303)
        resp.delete_cookie("session_erp")
        return resp

    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    user = get_current_erp_user(request)
    if not user:
        return RedirectResponse(url="/erp/login", status_code=303)

    sql = "SELECT * FROM erp_bills"
    params: list[Any] = []
    if q.strip():
        sql += " WHERE vendor_name LIKE ? OR invoice_number LIKE ?"
        wildcard = f"%{q.strip()}%"
        params.extend([wildcard, wildcard])
    sql += " ORDER BY id DESC"

    bills = query_all(sql, tuple(params))
    msg = f"Successfully entered bill for invoice {added}." if added else None

    return templates.TemplateResponse(
        request=request,
        name="erp/bills.html",
        context={
            "title": "Acme ERP - Bills Register",
            "app_name": "Acme Internal ERP",
            "system_name": "ERP",
            "user": user,
            "logout_url": "/erp/logout",
            "bills": bills,
            "search_query": q,
            "message": msg,
        },
    )


@router.get("/bills/new")
async def erp_new_bill_page(request: Request) -> Response:
    if fault_manager.should_expire_session("erp"):
        resp = RedirectResponse(url="/erp/login?expired=1", status_code=303)
        resp.delete_cookie("session_erp")
        return resp

    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    user = get_current_erp_user(request)
    if not user:
        return RedirectResponse(url="/erp/login", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="erp/bill_new.html",
        context={
            "title": "Acme ERP - New Bill",
            "app_name": "Acme Internal ERP",
            "system_name": "ERP",
            "user": user,
            "logout_url": "/erp/logout",
            "renamed_elements": fault_manager.state.renamed_elements,
        },
    )


@router.post("/bills/new")
async def erp_new_bill_submit(
    request: Request,
    vendor_name: str | None = Form(None),
    supplier_entity: str | None = Form(None),
    invoice_number: str | None = Form(None),
    bill_reference_no: str | None = Form(None),
    amount: float | None = Form(None),
    total_payable: float | None = Form(None),
    currency: str | None = Form(None),
    currency_code: str | None = Form(None),
    due_date: str | None = Form(None),
    payment_due_date: str | None = Form(None),
    attachment: UploadFile | None = File(None),
    bill_document: UploadFile | None = File(None),
) -> Response:
    if fault_manager.should_expire_session("erp"):
        resp = RedirectResponse(url="/erp/login?expired=1", status_code=303)
        resp.delete_cookie("session_erp")
        return resp

    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    user = get_current_erp_user(request)
    if not user:
        return RedirectResponse(url="/erp/login", status_code=303)

    # Resolve normalized fields from standard or renamed inputs
    final_vendor = (vendor_name or supplier_entity or "").strip()
    final_inv = (invoice_number or bill_reference_no or "").strip()
    final_amount = amount if amount is not None else total_payable
    final_curr = (currency or currency_code or "INR").strip().upper()
    final_due = (due_date or payment_due_date or "").strip()
    upload = attachment or bill_document

    # Validation
    if not final_vendor or not final_inv or final_amount is None or not final_due:
        return templates.TemplateResponse(
            request=request,
            name="erp/bill_new.html",
            context={
                "title": "Acme ERP - New Bill",
                "app_name": "Acme Internal ERP",
                "system_name": "ERP",
                "user": user,
                "error": "All bill fields are required. Please check missing values.",
                "renamed_elements": fault_manager.state.renamed_elements,
            },
            status_code=422,
        )

    # Duplicate bill check (non-negotiable reliability requirement)
    if fault_manager.state.duplicate_invoice_rejection:
        existing = query_one(
            "SELECT * FROM erp_bills WHERE vendor_name = ? AND invoice_number = ?",
            (final_vendor, final_inv),
        )
        if existing:
            return templates.TemplateResponse(
                request=request,
                name="erp/bill_new.html",
                context={
                    "title": "Acme ERP - New Bill",
                    "app_name": "Acme Internal ERP",
                    "system_name": "ERP",
                    "user": user,
                    "error": (
                        f"Duplicate bill rejected: A bill for vendor '{final_vendor}' "
                        f"with invoice reference '{final_inv}' is already entered in the ERP register."
                    ),
                    "renamed_elements": fault_manager.state.renamed_elements,
                },
                status_code=409,
            )

    # Save uploaded file
    attachment_name: str | None = None
    if upload and upload.filename:
        attachment_name = os.path.basename(upload.filename)
        save_path = os.path.join(INVOICES_DIR, attachment_name)
        content = await upload.read()
        with open(save_path, "wb") as f:
            f.write(content)

    # Insert into erp_bills
    execute_write(
        """
        INSERT INTO erp_bills (vendor_name, invoice_number, amount, currency, due_date, attachment_filename, status)
        VALUES (?, ?, ?, ?, ?, ?, 'entered')
        """,
        (final_vendor, final_inv, float(final_amount), final_curr, final_due, attachment_name),
    )

    return RedirectResponse(url=f"/erp/bills?added={final_inv}", status_code=303)


@router.get("/vendors")
async def erp_vendors_list() -> dict[str, Any]:
    vendors = query_all("SELECT * FROM vendors ORDER BY name ASC")
    return {"vendors": vendors}
