from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from env.db import query_all, reset_db
from env.faults.manager import fault_manager
from env.seed.generator import seed_company_data

router = APIRouter(prefix="/__truth", tags=["Ground Truth & Evals"])


@router.get("/state")
async def get_system_ground_truth_state() -> dict[str, Any]:
    """
    Exposes complete system state used exclusively by test harnesses and verifiers.
    Strictly isolated from the agent runtime.
    """
    return {
        "erp_bills": query_all("SELECT * FROM erp_bills ORDER BY id DESC"),
        "invoices": query_all("SELECT * FROM invoices ORDER BY id DESC"),
        "vendors": query_all("SELECT * FROM vendors ORDER BY id ASC"),
        "emails": query_all("SELECT * FROM emails ORDER BY id DESC"),
        "tickets": query_all("SELECT * FROM tickets ORDER BY id DESC"),
        "faults": fault_manager.get_faults(),
    }


@router.post("/reset")
async def reset_ground_truth_environment() -> dict[str, Any]:
    """Wipes all state, regenerates clean seed invoices/PDFs, and resets faults."""
    reset_db()
    seed_res = seed_company_data()
    faults_res = fault_manager.reset_faults()
    return {
        "status": "reset_success",
        "seed": seed_res,
        "faults": faults_res,
    }


@router.post("/seed")
async def seed_ground_truth_environment() -> dict[str, Any]:
    """Runs data seeding."""
    return seed_company_data()


@router.get("/faults")
async def get_active_faults() -> dict[str, Any]:
    """Returns current active faults configuration."""
    return fault_manager.get_faults()


@router.post("/faults")
async def update_active_faults(updates: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Dynamically activates or updates runtime faults without restarting the server."""
    updated = fault_manager.update_faults(updates)
    return {"status": "updated", "faults": updated}


@router.post("/faults/reset")
async def reset_active_faults() -> dict[str, Any]:
    """Resets all runtime faults to default neutral state."""
    reset = fault_manager.reset_faults()
    return {"status": "reset", "faults": reset}


@router.get("/bills")
async def get_erp_bills_truth() -> list[dict[str, Any]]:
    """Ground truth list of bills entered in ERP."""
    return query_all("SELECT * FROM erp_bills ORDER BY id DESC")


@router.get("/invoices")
async def get_invoices_truth() -> list[dict[str, Any]]:
    """Ground truth list of invoices."""
    return query_all("SELECT * FROM invoices ORDER BY id DESC")


@router.get("/emails")
async def get_emails_truth() -> list[dict[str, Any]]:
    """Ground truth list of emails."""
    return query_all("SELECT * FROM emails ORDER BY id DESC")


@router.get("/tickets")
async def get_tickets_truth() -> list[dict[str, Any]]:
    """Ground truth list of tickets."""
    return query_all("SELECT * FROM tickets ORDER BY id DESC")
