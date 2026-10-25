from __future__ import annotations

import datetime
import os
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from env.db import execute_write, query_all, query_one
from env.faults.manager import fault_manager

router = APIRouter(prefix="/tickets", tags=["Ticketing & HR"])
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_current_ticket_user(request: Request) -> str | None:
    return request.cookies.get("session_tickets")


@router.get("/login")
async def tickets_login_page(request: Request, expired: int = 0) -> Response:
    error = "Session expired. Please sign in again." if expired else None
    return templates.TemplateResponse(
        request=request,
        name="ticketing/login.html",
        context={
            "title": "Service Desk - Login",
            "app_name": "Service Desk & HR Portal",
            "system_name": "Ticketing",
            "error": error,
        },
    )


@router.post("/login")
async def tickets_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    user = query_one(
        "SELECT * FROM users WHERE username = ? AND password = ? AND system = 'tickets'",
        (username, password),
    )
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="ticketing/login.html",
            context={
                "title": "Service Desk - Login",
                "app_name": "Service Desk & HR Portal",
                "system_name": "Ticketing",
                "error": "Invalid service desk username or password.",
            },
            status_code=401,
        )

    response = RedirectResponse(url="/tickets/list", status_code=303)
    response.set_cookie(key="session_tickets", value=username, httponly=True)
    return response


@router.get("/logout")
async def tickets_logout() -> Response:
    response = RedirectResponse(url="/tickets/login", status_code=303)
    response.delete_cookie("session_tickets")
    return response


@router.get("/list")
async def tickets_list(request: Request, status: str = "") -> Response:
    if fault_manager.should_expire_session("tickets"):
        resp = RedirectResponse(url="/tickets/login?expired=1", status_code=303)
        resp.delete_cookie("session_tickets")
        return resp

    await fault_manager.maybe_delay(request.url.path)
    if fault_manager.should_trigger_500(request.url.path):
        raise HTTPException(status_code=500, detail="500 Internal Server Error (Fault Injected)")

    user = get_current_ticket_user(request)
    if not user:
        return RedirectResponse(url="/tickets/login", status_code=303)

    sql = "SELECT * FROM tickets"
    params: list[Any] = []
    if status.strip():
        sql += " WHERE status = ?"
        params.append(status.strip())
    sql += " ORDER BY id DESC"

    tickets = query_all(sql, tuple(params))

    return templates.TemplateResponse(
        request=request,
        name="ticketing/list.html",
        context={
            "title": "Service Desk - Tickets",
            "app_name": "Service Desk & HR Portal",
            "system_name": "Ticketing",
            "user": user,
            "logout_url": "/tickets/logout",
            "tickets": tickets,
        },
    )


@router.get("/new")
async def tickets_new_page(request: Request) -> Response:
    user = get_current_ticket_user(request)
    if not user:
        return RedirectResponse(url="/tickets/login", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="ticketing/new.html",
        context={
            "title": "Service Desk - New Ticket",
            "app_name": "Service Desk & HR Portal",
            "system_name": "Ticketing",
            "user": user,
            "logout_url": "/tickets/logout",
        },
    )


@router.post("/new")
async def tickets_new_submit(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    assignee: str = Form("Sneha Patel"),
    priority: str = Form("medium"),
) -> Response:
    user = get_current_ticket_user(request)
    if not user:
        return RedirectResponse(url="/tickets/login", status_code=303)

    # Generate next ticket number
    count_row = query_one("SELECT COUNT(*) as cnt FROM tickets")
    next_num = 100 + (count_row["cnt"] if count_row else 1) + 1
    ticket_num = f"TICK-{next_num}"
    created_at = datetime.date.today().isoformat()

    execute_write(
        """
        INSERT INTO tickets (ticket_number, title, description, assignee, status, priority, created_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?)
        """,
        (ticket_num, title.strip(), description.strip(), assignee.strip(), priority.strip(), created_at),
    )

    return RedirectResponse(url="/tickets/list", status_code=303)


@router.get("/{ticket_number}")
async def tickets_detail(request: Request, ticket_number: str) -> Response:
    user = get_current_ticket_user(request)
    if not user:
        return RedirectResponse(url="/tickets/login", status_code=303)

    ticket = query_one("SELECT * FROM tickets WHERE ticket_number = ?", (ticket_number,))
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    return templates.TemplateResponse(
        request=request,
        name="ticketing/detail.html",
        context={
            "title": f"Ticket {ticket_number} - Service Desk",
            "app_name": "Service Desk & HR Portal",
            "system_name": "Ticketing",
            "user": user,
            "logout_url": "/tickets/logout",
            "ticket": ticket,
        },
    )


@router.post("/{ticket_number}/update")
async def tickets_update_submit(
    request: Request,
    ticket_number: str,
    status: str = Form(...),
    assignee: str = Form(...),
) -> Response:
    user = get_current_ticket_user(request)
    if not user:
        return RedirectResponse(url="/tickets/login", status_code=303)

    resolved_at = datetime.date.today().isoformat() if status in ("resolved", "closed") else None

    execute_write(
        """
        UPDATE tickets
        SET status = ?, assignee = ?, resolved_at = COALESCE(?, resolved_at)
        WHERE ticket_number = ?
        """,
        (status, assignee, resolved_at, ticket_number),
    )

    return RedirectResponse(url=f"/tickets/{ticket_number}", status_code=303)
