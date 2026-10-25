from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from env.db import init_db
from env.seed.generator import seed_company_data
from env.server import app
from evals.models import EvalReport, EvalTaskResult
from evals.suite import define_eval_tasks, execute_eval_task

BASE_URL = "http://127.0.0.1:8001"
REPORT_JSON_PATH = "evals/report.json"
REPORT_MD_PATH = "evals/REPORT.md"
README_PATH = "README.md"


def ensure_server_running() -> threading.Thread | None:
    """Ensure mock company environment is active on port 8001."""
    try:
        with httpx.Client(timeout=0.6) as client:
            res = client.get(f"{BASE_URL}/__truth/state")
            if res.status_code == 200:
                print("✓ Mock environment server active on http://127.0.0.1:8001")
                return None
    except Exception:
        pass

    print("→ Starting background mock company environment on http://127.0.0.1:8001...")
    init_db()
    seed_company_data()

    config = uvicorn.Config(app, host="127.0.0.1", port=8001, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore

    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    for _ in range(30):
        try:
            with httpx.Client(timeout=0.5) as client:
                res = client.get(f"{BASE_URL}/__truth/state")
                if res.status_code == 200:
                    print("✓ Mock environment server ready!")
                    return t
        except Exception:
            time.sleep(0.1)

    raise RuntimeError("Failed to start mock environment server on port 8001")


def update_readme_with_results(markdown_content: str) -> None:
    """Embed the eval results table directly into README.md."""
    if not os.path.exists(README_PATH):
        return

    try:
        content = Path(README_PATH).read_text(encoding="utf-8")
        marker_start = "<!-- EVAL_RESULTS_START -->"
        marker_end = "<!-- EVAL_RESULTS_END -->"

        replacement_block = f"{marker_start}\n{markdown_content}\n{marker_end}"

        if marker_start in content and marker_end in content:
            pre = content.split(marker_start)[0]
            post = content.split(marker_end)[1]
            new_content = pre + replacement_block + post
        else:
            # Append before limitations or at the end
            if "## 7. Known Limitations" in content:
                parts = content.split("## 7. Known Limitations")
                new_content = parts[0] + f"## 7. Evaluation Results\n\n{replacement_block}\n\n## 8. Known Limitations" + parts[1]
            else:
                new_content = content + f"\n\n## Evaluation Results\n\n{replacement_block}\n"

        Path(README_PATH).write_text(new_content, encoding="utf-8")
        print("✓ Embedded evaluation scorecard directly into README.md")
    except Exception as e:
        print(f"Warning: Failed to update README.md with results: {e}")


async def run_evaluation_suite() -> int:
    """Run all 16 evaluation tasks, compute §7 metrics, and generate reports."""
    ensure_server_running()

    tasks = define_eval_tasks()
    total_tasks = len(tasks)

    print("\n" + "=" * 80)
    print("      CentrAlign AI — Founding-v1 Comprehensive Evaluation Suite")
    print(f"      Total Evaluation Variants: {total_tasks} | (§7 Target: >=15 variants)")
    print("=" * 80 + "\n")

    results: list[EvalTaskResult] = []

    for idx, t_var in enumerate(tasks, 1):
        print(f"[{idx:02d}/{total_tasks:02d}] Running `{t_var.task_id}`: {t_var.name} ... ", end="", flush=True)
        res = await execute_eval_task(t_var)
        results.append(res)
        status_str = "PASS" if res.verified_success else "FAIL"
        print(f"[{status_str}] ({res.steps_count} steps, {res.duration_sec:.1f}s)")

    # Compute Section 7 Metrics
    passed_tasks = sum(1 for r in results if r.verified_success)
    verified_success_rate = passed_tasks / float(total_tasks) if total_tasks else 0.0

    false_completions = sum(1 for r in results if r.is_false_completion)
    false_completion_rate = false_completions / float(total_tasks) if total_tasks else 0.0

    fault_tasks = [r for r in results if r.fault_recovered is not None]
    fault_recovery_rate = sum(1 for r in fault_tasks if r.verified_success) / float(len(fault_tasks)) if fault_tasks else 1.0

    gen_tasks = [r for r in results if r.category == "generalization"]
    gen_pass_rate = sum(1 for r in gen_tasks if r.verified_success) / float(len(gen_tasks)) if gen_tasks else 1.0

    clarify_tasks = [r for r in results if r.category == "human_interaction"]
    clarify_precision = sum(1 for r in clarify_tasks if r.clarification_precision_ok) / float(len(clarify_tasks)) if clarify_tasks else 1.0

    approval_tasks = [r for r in results if r.category == "policy_security" and "hold" in r.task_id]
    approval_precision = sum(1 for r in approval_tasks if r.approval_precision_ok) / float(len(approval_tasks)) if approval_tasks else 1.0

    avg_steps = sum(r.steps_count for r in results) / float(total_tasks) if total_tasks else 0.0
    avg_tokens = sum(r.tokens_used for r in results) / float(total_tasks) if total_tasks else 0.0
    avg_duration = sum(r.duration_sec for r in results) / float(total_tasks) if total_tasks else 0.0
    avg_cost = sum(r.cost_usd for r in results) / float(total_tasks) if total_tasks else 0.0

    report = EvalReport(
        total_tasks=total_tasks,
        passed_tasks=passed_tasks,
        verified_success_rate=verified_success_rate,
        false_completion_rate=false_completion_rate,
        fault_recovery_rate=fault_recovery_rate,
        generalization_pass_rate=gen_pass_rate,
        avg_steps_per_task=avg_steps,
        avg_tokens_per_task=avg_tokens,
        avg_duration_sec=avg_duration,
        avg_cost_usd=avg_cost,
        clarification_precision=clarify_precision,
        approval_precision=approval_precision,
        task_results=results,
    )

    # Save report.json
    Path(REPORT_JSON_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(report.model_dump(), f, indent=2)
    print(f"\n✓ Saved structured JSON evaluation report to {REPORT_JSON_PATH}")

    # Save REPORT.md
    md_content = report.to_markdown_table()
    with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(f"# CentrAlign AI — Founding-v1 Evaluation Report\n\n{md_content}\n")
    print(f"✓ Saved Markdown evaluation report to {REPORT_MD_PATH}")

    # Embed table in README.md
    update_readme_with_results(md_content)

    # Print summary table to terminal
    print("\n" + "=" * 80)
    print(md_content)
    print("=" * 80 + "\n")

    if verified_success_rate >= 0.90 and false_completion_rate == 0.0:
        print("🎉 ALL SECTION 7 EVALUATION CRITERIA MET CLEANLY! (False-completion rate: 0.0%)")
        return 0
    else:
        print(f"❌ Evaluation criteria unmet (Success rate: {verified_success_rate*100:.1f}%, False completions: {false_completion_rate*100:.1f}%)")
        return 1


def main() -> None:
    code = asyncio.run(run_evaluation_suite())
    sys.exit(code)


if __name__ == "__main__":
    main()
