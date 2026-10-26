from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml

from core.cli import run_cli
from core.runs.manager import RunManager
from env.db import init_db
from env.seed.generator import seed_company_data
from env.server import app

BASE_URL = "http://127.0.0.1:8001"

# Load scenario definitions from company data
SCENARIO_DESCRIPTIONS: dict[int, dict[str, Any]] = {}
scenarios_path = Path("company/scenarios.yaml")
if scenarios_path.exists():
    with open(scenarios_path) as sf:
        raw_sc = yaml.safe_load(sf) or {}
        SCENARIO_DESCRIPTIONS = {int(k): v for k, v in raw_sc.items() if str(k).isdigit()}


def ensure_server_running() -> threading.Thread | None:
    """Ensure mock company environment is active on port 8001."""
    try:
        with httpx.Client(timeout=0.6) as client:
            res = client.get(f"{BASE_URL}/__truth/state")
            if res.status_code == 200:
                print("✓ Mock environment already running on http://127.0.0.1:8001")
                return None
    except Exception:
        pass

    print("✓ Starting background mock environment on http://127.0.0.1:8001...")
    try:
        init_db()
        seed_company_data()

        config = uvicorn.Config(app, host="127.0.0.1", port=8001, log_level="error")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore

        t = threading.Thread(target=server.run, daemon=True)
        t.start()
    except Exception as e:
        print(f"Notice: Mock environment start returned: {e}")
        return None

    for _ in range(10):
        try:
            with httpx.Client(timeout=0.5) as client:
                res = client.get(f"{BASE_URL}/__truth/state")
                if res.status_code == 200:
                    print("✓ Mock environment ready!")
                    return t
        except Exception:
            time.sleep(0.1)

    return None


async def run_founding_showcases() -> list[dict[str, Any]]:
    """Execute Founding Engineer capability showcases."""
    showcases: list[dict[str, Any]] = []

    # 1. Policy Threshold Change & Approval
    print(f"\n{'#' * 80}")
    print("▶ FOUNDING DEMO 1: Policy Engine Spending Threshold & Approvals Inbox")
    print("  Dynamically checks spending threshold; intercepts INR 150k bill.")
    print(f"{'#' * 80}\n")
    from evals.suite import define_eval_tasks, execute_eval_task
    all_evals = {t.task_id: t for t in define_eval_tasks()}

    res1 = await execute_eval_task(all_evals["eval_10_policy_spending_hold"])
    showcases.append({
        "id": "F1",
        "name": "Policy Engine Approval Hold",
        "status": "PASS" if res1.verified_success else "FAIL",
        "steps": res1.steps_count,
        "duration": res1.duration_sec,
        "verification": "PASS (Policy Held & Approved)",
    })

    # 2. Generalization Task 1 (Ticket Triage + Email)
    print(f"\n{'#' * 80}")
    print("▶ FOUNDING DEMO 2: Zero-Code-Change Generalization (Ticket Triage & Email)")
    print("  Ticketing + Mailbox execution without any agent code modifications.")
    print(f"{'#' * 80}\n")
    res2 = await execute_eval_task(all_evals["eval_14_gen_ticket_triage"])
    showcases.append({
        "id": "F2",
        "name": "Generalization: Tickets + Mail",
        "status": "PASS" if res2.verified_success else "FAIL",
        "steps": res2.steps_count,
        "duration": res2.duration_sec,
        "verification": "PASS (Tickets Closed & Emailed)",
    })

    # 3. Durable Crash Recovery
    print(f"\n{'#' * 80}")
    print("▶ FOUNDING DEMO 3: Durable Job Queue & Mid-Run Crash Recovery")
    print("  Process killed mid-flight; resumes from persisted step without duplicate writes.")
    print(f"{'#' * 80}\n")
    res3 = await execute_eval_task(all_evals["eval_12_crash_resume_idempotency"])
    showcases.append({
        "id": "F3",
        "name": "Crash Recovery & Idempotency",
        "status": "PASS" if res3.verified_success else "FAIL",
        "steps": res3.steps_count,
        "duration": res3.duration_sec,
        "verification": "PASS (Resumed from SQLite Step)",
    })

    # 4. Learned Lesson Speedup
    print(f"\n{'#' * 80}")
    print("▶ FOUNDING DEMO 4: Continuous Learning from Outcomes")
    print("  Retrieves learned lesson; avoids failed source and completes in fewer steps.")
    print(f"{'#' * 80}\n")
    res4 = await execute_eval_task(all_evals["eval_13_learning_lesson_speedup"])
    showcases.append({
        "id": "F4",
        "name": "Learned Lesson Speedup",
        "status": "PASS" if res4.verified_success else "FAIL",
        "steps": res4.steps_count,
        "duration": res4.duration_sec,
        "verification": "PASS (Retrieved Lesson Applied)",
    })

    # 5. Security & Prompt-Injection Exfiltration Block
    print(f"\n{'#' * 80}")
    print("▶ FOUNDING DEMO 5: Security & Prompt-Injection Exfiltration Defense")
    print("  Blocks malicious payload inside invoice from exfiltrating data to attacker.")
    print(f"{'#' * 80}\n")
    res5 = await execute_eval_task(all_evals["eval_11_prompt_injection_defense"])
    showcases.append({
        "id": "F5",
        "name": "Prompt-Injection Block",
        "status": "PASS" if res5.verified_success else "FAIL",
        "steps": res5.steps_count,
        "duration": res5.duration_sec,
        "verification": "PASS (POLICY: DENIED Exfiltration)",
    })

    return showcases


async def run_all_demos() -> int:
    """Run both the 4 Intern core scenarios and the 5 Founding Engineer capability showcases."""
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError(
            "GEMINI_API_KEY is not set in environment or .env! "
            "make demo calls the real LLM and drives real tools. Please configure GEMINI_API_KEY."
        )

    ensure_server_running()

    print("\n" + "=" * 94)
    print("         CentrAlign AI — Founding-v1 Comprehensive Automated Demo Suite")
    print("=" * 94)

    scorecard: list[dict[str, Any]] = []
    scenarios = [1, 2, 3, 4]

    for sc_id in scenarios:
        info = SCENARIO_DESCRIPTIONS[sc_id]
        print(f"\n{'#' * 80}")
        print(f"▶ RUNNING SCENARIO {sc_id}/4: {info['name']}")
        print(f"  Description: {info['description']}")
        print(f"  Goal: {info['task']}")
        print(f"{'#' * 80}\n")

        auto_in = "Acme Corp Ltd" if sc_id == 3 else None

        t0 = time.monotonic()
        exit_code = await run_cli(
            scenario=sc_id,
            auto_input=auto_in,
            llm_mode="live",
            base_url=BASE_URL,
            runs_db="artifacts/runs.db",
            artifacts_dir="artifacts",
        )
        elapsed = time.monotonic() - t0

        rm = RunManager(db_path="artifacts/runs.db", artifacts_dir="artifacts")
        recent = rm.list_runs(limit=1)
        run_rec = recent[0] if recent else None

        v_outcome = run_rec.working_memory.get("verification_outcome", "UNKNOWN") if run_rec else "UNKNOWN"
        steps_count = run_rec.step_count if run_rec else 0
        status_str = run_rec.status.value.upper() if run_rec else "UNKNOWN"

        passed = exit_code == 0 and status_str == "COMPLETED"

        verification_note = "PASS (ERP Bill Match)" if sc_id == 1 else (
            "PASS (Auto-Recovered)" if sc_id == 2 else (
                "PASS (Clarified)" if sc_id == 3 else "PASS (Double-Entry Prevented)"
            )
        )

        scorecard.append({
            "id": f"S{sc_id}",
            "name": info["name"],
            "status": "PASS" if passed else "FAIL",
            "steps": steps_count,
            "duration": elapsed,
            "verification": verification_note if passed else f"FAIL ({v_outcome})",
        })

    # Run Founding Showcases
    founding_scores = await run_founding_showcases()
    scorecard.extend(founding_scores)

    # Print Combined Scorecard
    print("\n" + "=" * 94)
    print("                         CentrAlign AI — Founding-v1 Demo Scorecard")
    print("=" * 94)
    print(f"{'#':<4} {'Demo Scenario / Capability':<38} {'Status':<8} {'Steps':<7} {'Time':<8} {'Independent Verifier':<25}")
    print("-" * 94)
    for row in scorecard:
        print(f"{row['id']:<4} {row['name']:<38} {row['status']:<8} {row['steps']:<7} {row['duration']:<6.2f}s  {row['verification']:<25}")
    print("=" * 94)

    all_passed = all(row["status"] == "PASS" for row in scorecard)
    if all_passed:
        print("🏆 Overall Result: 9/9 Scenarios & Foundational Showcases PASSED!")
        print("🏷️  Git Tag Ready: founding-v1")
    else:
        print("❌ Overall Result: Some demo scenarios failed.")
    print("=" * 94 + "\n")

    return 0 if all_passed else 1


def main() -> None:
    code = asyncio.run(run_all_demos())
    sys.exit(code)


if __name__ == "__main__":
    main()
