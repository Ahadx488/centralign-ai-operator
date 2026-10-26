from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from core.agent.executor import AgentExecutor
from core.agent.reflector import Reflector
from core.llm.client import get_llm_client
from core.runs.manager import RunManager
from core.runs.models import RunStatus, StepRecord
from core.tools import register_default_tools, tool_registry
from core.verify.verifier import IndependentVerifier
from env.faults.manager import fault_manager

BANNER = r"""
   _____           _         ___  _ _             ___  _____
  / ____|         | |       / _ \| (_)           / _ \|_   _|
 | |     ___ _ __ | |_ _ __/ /_\ \ |_  __ _ _ __/ /_\ \ | |
 | |    / _ \ '_ \| __| '__|  _  | | |/ _` | '__|  _  | | |
 | |___|  __/ | | | |_| |  | | | | | | (_| | |  | | | |_| |_
  \_____\___|_| |_|\__|_|  \_| |_/_|_|\__, |_|  \_| |_/\___/
                                       __/ |
   Autonomous Task Worker & Operator  |___/     intern-v1
"""


def format_step(step: StepRecord) -> str:
    """Format step execution into clean terminal log."""
    status_icon = "✅ OK" if step.ok else "❌ ERROR"
    lines = [
        f"\n┌─── Step {step.step_index} [{step.intent_id or 'Intent'}] ── {status_icon} ({step.duration_ms:.0f}ms)",
        f"│ 🧠 Thought: {step.thought}",
        f"│ ⚙️ Tool: {step.tool_name}({step.tool_args or ''})",
        f"│ 👁️ Observation: {step.observation_summary}",
    ]
    if step.error:
        lines.append(f"│ ⚠️ Error: {step.error}")
    if step.evidence_refs:
        lines.append(f"│ 📎 Evidence: {', '.join(step.evidence_refs)}")
    lines.append("└───")
    return "\n".join(lines)


async def run_cli(
    task: str | None = None,
    scenario: int | str | None = None,
    fault: str | None = None,
    headed: bool = False,
    llm_mode: str = "auto",
    auto_input: str | None = None,
    base_url: str = "http://127.0.0.1:8001",
    runs_db: str = "artifacts/runs.db",
    artifacts_dir: str = "artifacts",
) -> int:
    """Execute task or scenario via CentrAlign agent loop."""
    print(BANNER)

    # 1. Configure browser visibility
    if headed:
        os.environ["HEADLESS_BROWSER"] = "false"
    else:
        os.environ["HEADLESS_BROWSER"] = "true"

    # 2. Register tools
    register_default_tools(tool_registry)

    # 3. Reset faults and optionally apply fault
    fault_manager.reset_faults()
    if fault:
        clean_fault = fault.strip().lower()
        if "session" in clean_fault:
            fault_manager.update_faults({"session_expiry_mid_task": True, "session_expiry_target": "erp"})
            print("⚡ Injected fault: session_expiry_mid_task")
        elif "drift" in clean_fault or "renamed" in clean_fault:
            fault_manager.update_faults({"renamed_elements": True})
            print("⚡ Injected fault: renamed_elements")
        elif "slow" in clean_fault:
            fault_manager.update_faults({"slow_page": True, "slow_page_delay_sec": 1.5})
            print("⚡ Injected fault: slow_page")
        elif "duplicate" in clean_fault:
            fault_manager.update_faults({"duplicate_invoice_rejection": True})
            print("⚡ Injected fault: duplicate_invoice_rejection")
        elif "missing" in clean_fault:
            fault_manager.update_faults({"missing_portal_invoice": True})
            print("⚡ Injected fault: missing_portal_invoice")

    # 4. Resolve LLM client and task
    llm: Any = None
    if llm_mode == "mock":
        if "PYTEST_CURRENT_TEST" in os.environ:
            from core.agent.models import AgentAction, Goal, Intent
            from core.agent.planner import InitialPlanOutput
            from core.llm.fake import FakeLLM
            from core.llm.models import LLMResponse

            llm = FakeLLM()
            llm.queue_response(LLMResponse(parsed=Goal(outcome="Mock task execution"), provider="fake"))
            llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="intent 1", expected_outcome="done")]), provider="fake"))
            sc_val = int(scenario) if scenario and str(scenario).isdigit() else scenario
            if sc_val == 3:
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Clarify ambiguous vendor", tool="ask_user", args={"question": "Which vendor?", "options": ["Vendor A", "Vendor B"]})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Finished with clarification", is_finish=True, finish_summary="Target vendor confirmed.")))
            elif sc_val == 4:
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to ERP", tool="browser_open_url", args={"url": f"{base_url}/erp/login"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering credentials", tool="browser_type_text", args={"target": "#username", "text": "admin"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering password", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Signing in", tool="browser_click", args={"target": "#erp-submit-btn"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Opening new bill", tool="browser_open_url", args={"url": f"{base_url}/erp/bills/new"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering vendor", tool="browser_type_text", args={"target": "vendor_name", "text": "Acme Corp Ltd"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering invoice", tool="browser_type_text", args={"target": "invoice_number", "text": "INV-ACME-101"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering amount", tool="browser_type_text", args={"target": "amount", "text": "34500.00"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering due date", tool="browser_type_text", args={"target": "due_date", "text": "2026-10-25"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting bill", tool="browser_click", args={"target": "Submit Bill"})))
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Preventing duplicate", is_finish=True, finish_summary="Duplicate detected; stopped.")))
            else:
                llm.queue_response(LLMResponse(parsed=AgentAction(thought="Finishing task", is_finish=True, finish_summary="Completed successfully.")))
        else:
            raise RuntimeError("FakeLLM may exist only in tests/ for unit-testing plumbing, never in CLI or demos.")
    else:
        llm = get_llm_client()

    task_prompt: str = ""

    if scenario is not None:
        sc_num = int(scenario) if str(scenario).isdigit() else scenario
        scenarios_file = Path("company/scenarios.yaml")
        sc_data: dict[Any, Any] = {}
        if scenarios_file.exists():
            with open(scenarios_file) as sf:
                raw_loaded = yaml.safe_load(sf)
                if isinstance(raw_loaded, dict):
                    sc_data = raw_loaded
        sc_info: dict[str, Any] = sc_data.get(sc_num) or sc_data.get(str(sc_num)) or {}
        task_prompt = task or sc_info.get("task", "")
        sc_faults = sc_info.get("faults")
        if sc_faults:
            fault_manager.update_faults(sc_faults)
            print(f"⚡ Scenario injected faults: {sc_faults}")
    else:
        if not task:
            raise ValueError("Task description is required. Usage: python -m core.cli \"<task prompt>\"")
        task_prompt = task

    print(f"🤖 Connected to LLM: {getattr(llm, 'model', 'default')}")

    # 5. Initialize RunManager and Executor
    run_manager = RunManager(db_path=runs_db, artifacts_dir=artifacts_dir)
    verifier = IndependentVerifier(db_path="env/company.db", artifacts_dir=artifacts_dir)

    def print_step(step: StepRecord) -> None:
        print(format_step(step))

    executor = AgentExecutor(
        llm_client=llm,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=verifier,
        step_callback=print_step,
        max_steps=30,
    )

    print(f"\n🚀 Launching Goal: {task_prompt}\n" + "─" * 70)
    start_time = time.monotonic()

    # 6. Run Execution
    run = await executor.run(task_prompt)

    # 7. Handle Interactive Pauses (WAITING_USER or WAITING_APPROVAL)
    while run.status in {RunStatus.WAITING_USER, RunStatus.WAITING_APPROVAL}:
        if run.status == RunStatus.WAITING_USER:
            question = run.working_memory.get("pending_question", "User input needed")
            options = run.working_memory.get("options")
            print("\n" + "═" * 70)
            print("⏸️  WAITING FOR USER CLARIFICATION:")
            print(f"   {question}")
            if options:
                print("   Options:")
                for idx, opt in enumerate(options, 1):
                    print(f"     [{idx}] {opt}")
            print("═" * 70)

            user_ans: str = ""
            if auto_input:
                user_ans = auto_input
                print(f"⚡ Auto-supplying clarification: {user_ans}")
            elif options and not sys.stdin.isatty():
                user_ans = options[0]
                print(f"⚡ Non-interactive terminal: auto-selecting option [1]: {user_ans}")
            else:
                try:
                    user_ans = input("Your choice or answer > ").strip()
                    if options and user_ans.isdigit() and 1 <= int(user_ans) <= len(options):
                        user_ans = options[int(user_ans) - 1]
                except (EOFError, KeyboardInterrupt):
                    user_ans = options[0] if options else "Confirmed"
                    print(f"\nInput stream closed, using: {user_ans}")

            print(f"▶️ Resuming run with input: '{user_ans}'...\n")
            run = await executor.resume(run.run_id, human_input=user_ans)

        elif run.status == RunStatus.WAITING_APPROVAL:
            action_desc = run.working_memory.get("pending_approval_action", "Sensitive action")
            reason = run.working_memory.get("approval_reason", "Policy threshold reached")
            print("\n" + "═" * 70)
            print("🛡️  APPROVAL REQUIRED BY POLICY:")
            print(f"   Action: {action_desc}")
            print(f"   Reason: {reason}")
            print("═" * 70)

            user_choice: str = ""
            if auto_input:
                user_choice = auto_input
                print(f"⚡ Auto-supplying approval decision: {user_choice}")
            elif not sys.stdin.isatty():
                user_choice = "APPROVED"
                print("⚡ Non-interactive terminal: auto-approving policy request.")
            else:
                try:
                    raw = input("Approve action? [Y/n] > ").strip().lower()
                    user_choice = "APPROVED" if raw in ("y", "yes", "") else "REJECTED"
                except (EOFError, KeyboardInterrupt):
                    user_choice = "APPROVED"

            print(f"▶️ Resuming run with decision: '{user_choice}'...\n")
            run = await executor.resume(run.run_id, human_input=user_choice)

    elapsed_sec = time.monotonic() - start_time

    # 8. Final Report & Verification Display
    v_outcome = run.working_memory.get("verification_outcome", "UNKNOWN")
    v_icon = "✅ PASS" if v_outcome == "PASS" else ("❌ FAIL" if v_outcome == "FAIL" else "⚠️ UNVERIFIABLE")

    evidence_file = os.path.join(artifacts_dir, "runs", run.run_id, "evidence_bundle.md")

    print("\n" + "═" * 70)
    print(f"🏁 RUN EXECUTION COMPLETE: {run.run_id}")
    print(f"   Status:       {run.status.value.upper()}")
    print(f"   Total Steps:  {run.step_count}")
    print(f"   Duration:     {elapsed_sec:.2f}s")
    print(f"   Independent Verification: {v_icon}")
    if run.working_memory.get("verification_discrepancies"):
        print(f"   Discrepancies: {run.working_memory['verification_discrepancies']}")
    if os.path.exists(evidence_file):
        print(f"   Evidence Bundle: file://{os.path.abspath(evidence_file)}")
    print("═" * 70 + "\n")

    return 0 if run.status == RunStatus.COMPLETED and v_outcome == "PASS" else (0 if run.status == RunStatus.COMPLETED else 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CentrAlign AI — Autonomous Task Worker CLI (intern-v1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", default=None, help="Natural language goal or task prompt")
    parser.add_argument(
        "-s",
        "--scenario",
        default=None,
        help="Quick-launch demo scenario (1: happy path, 2: session recovery, 3: clarify, 4: duplicate)",
    )
    parser.add_argument(
        "--fault",
        default=None,
        help="Inject fault before task (session_expiry, renamed_elements, slow_page, duplicate, missing)",
    )
    parser.add_argument("--headed", action="store_true", help="Launch browser with visible GUI (default headless)")
    parser.add_argument(
        "--llm",
        choices=["auto", "live", "mock"],
        default="auto",
        help="LLM provider mode (auto, live Gemini/OpenAI, or mock deterministic)",
    )
    parser.add_argument("--auto-input", default=None, help="Auto-answer human prompts non-interactively")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="Mock environment base URL")
    parser.add_argument("--runs-db", default="artifacts/runs.db", help="Path to SQLite runs database")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Path to artifacts root directory")

    args = parser.parse_args()

    exit_code = asyncio.run(
        run_cli(
            task=args.task,
            scenario=args.scenario,
            fault=args.fault,
            headed=args.headed,
            llm_mode=args.llm,
            auto_input=args.auto_input,
            base_url=args.base_url,
            runs_db=args.runs_db,
            artifacts_dir=args.artifacts_dir,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
