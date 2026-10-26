from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from core.cli import run_cli
from core.runs.manager import RunManager
from core.runs.models import RunStatus
from env.faults.manager import fault_manager
from env.server import app
from scripts.demo import run_all_demos
from tests.conftest import BASE_URL


@pytest.fixture(autouse=True)
def reset_faults():
    fault_manager.reset_faults()
    yield
    fault_manager.reset_faults()


def test_ui_dashboard_endpoints():
    """Verify web dashboard pages, live partials, and launch endpoints."""
    client = TestClient(app)

    # 1. Root redirect to /ui
    res_root = client.get("/", follow_redirects=False)
    assert res_root.status_code == 307 or res_root.status_code == 302 or res_root.status_code == 303
    assert res_root.headers["location"] == "/ui"

    # 2. Main Dashboard Page
    res_ui = client.get("/ui")
    assert res_ui.status_code == 200
    assert "CentrAlign AI" in res_ui.text
    assert "Autonomous Goal Launcher" in res_ui.text
    assert "btn-scenario-1" in res_ui.text

    # 3. Launch a scenario via POST /ui/launch
    res_launch = client.post("/ui/launch?scenario=3")
    assert res_launch.status_code == 200
    data = res_launch.json()
    assert data["ok"] is True
    run_id = data["run_id"]

    # 4. Check live partial
    res_live = client.get(f"/ui/runs/{run_id}/live")
    assert res_live.status_code == 200
    assert run_id in res_live.text

    # 5. Resume clarification
    res_resume = client.post(
        f"/ui/runs/{run_id}/resume",
        json={"human_input": "Acme Corp Ltd"},
    )
    assert res_resume.status_code == 200
    assert res_resume.json()["ok"] is True

    # 6. Approve endpoint
    res_appr = client.post(
        f"/ui/runs/{run_id}/approve",
        json={"approved": True},
    )
    assert res_appr.status_code == 200
    assert res_appr.json()["ok"] is True

    # 7. Evidence endpoint
    res_ev = client.get(f"/ui/runs/{run_id}/evidence")
    assert res_ev.status_code in (200, 404)


@pytest.mark.asyncio
async def test_cli_execution_scenario_3_clarification(tmp_path):
    """Verify CLI handles scenario 3 with auto-supplied clarification."""
    code = await run_cli(
        scenario=3,
        auto_input="Acme Corp Ltd",
        llm_mode="mock",
        base_url=BASE_URL,
        runs_db=str(tmp_path / "cli_runs.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert code == 0

    rm = RunManager(db_path=tmp_path / "cli_runs.db", artifacts_dir=tmp_path / "artifacts")
    runs = rm.list_runs()
    assert len(runs) >= 1
    assert runs[0].status == RunStatus.COMPLETED
    assert runs[0].working_memory.get("user_clarification") == "Acme Corp Ltd"


@pytest.mark.asyncio
async def test_cli_execution_scenario_4_duplicate(tmp_path):
    """Verify CLI executes scenario 4 duplicate invoice detection."""
    code = await run_cli(
        scenario=4,
        llm_mode="mock",
        base_url=BASE_URL,
        runs_db=str(tmp_path / "cli_dup.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert code == 0

    rm = RunManager(db_path=tmp_path / "cli_dup.db", artifacts_dir=tmp_path / "artifacts")
    runs = rm.list_runs()
    assert len(runs) >= 1
    assert runs[0].status == RunStatus.COMPLETED
    assert runs[0].working_memory.get("is_duplicate") is True


@pytest.mark.asyncio
async def test_automated_demo_script_execution(monkeypatch):
    """Verify scripts/demo.py checks API key and executes cleanly."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        await run_all_demos()

    monkeypatch.setenv("GEMINI_API_KEY", "dummy_key")

    async def mock_run_cli(**kwargs):
        rm = RunManager(
            db_path=kwargs.get("runs_db", "artifacts/runs.db"),
            artifacts_dir=kwargs.get("artifacts_dir", "artifacts"),
        )
        r = rm.create_run(task="Demo run")
        rm.update_run(r.run_id, working_memory={"verification_outcome": "PASS"})
        rm.transition(r.run_id, RunStatus.COMPLETED, reason="Mock completed", force=True)
        return 0

    monkeypatch.setattr("scripts.demo.run_cli", mock_run_cli)

    async def mock_founding():
        return []

    monkeypatch.setattr("scripts.demo.run_founding_showcases", mock_founding)
    code = await run_all_demos()
    assert code == 0
