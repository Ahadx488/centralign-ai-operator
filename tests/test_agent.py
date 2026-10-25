from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.agent.context import ContextManager
from core.agent.executor import AgentExecutor
from core.agent.interpreter import GoalInterpreter
from core.agent.models import (
    AgentAction,
    Goal,
    Intent,
    IntentStatus,
    ObservationType,
    Plan,
    Reflection,
)
from core.agent.planner import InitialPlanOutput, Planner
from core.agent.reflector import Reflector
from core.llm.fake import FakeLLM
from core.llm.models import LLMResponse, TokenUsage
from core.runs.manager import RunManager
from core.runs.models import RunStatus, StepRecord
from core.tools.registry import BaseTool, RiskTier, ToolRegistry, ToolResult
from core.verify.models import EvidenceBundle, VerificationOutcome, VerificationResult
from core.verify.verifier import IndependentVerifier


class MockVerifier(IndependentVerifier):
    async def verify(self, run, goal, working_memory, steps, **kwargs):
        bundle = EvidenceBundle(
            run_id=run.run_id,
            task=run.task,
            outcome=VerificationOutcome.PASS,
            checks=[],
            discrepancies=[],
        )
        return VerificationResult(
            outcome=VerificationOutcome.PASS,
            checks=[],
            discrepancies=[],
            evidence_bundle=bundle,
        )


class DummyArgs(BaseModel):
    target: str = ""
    question: str = ""
    options: list[str] = []
    action: str = ""
    reason: str = ""


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def run_manager(tmp_path) -> RunManager:
    return RunManager(db_path=tmp_path / "agent_runs.db", artifacts_dir=tmp_path / "artifacts")


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    class MockClickTool(BaseTool):
        name = "click"
        description = "Mock click"
        risk_tier = RiskTier.READ
        args_schema = DummyArgs

        async def _run(self, **kwargs) -> ToolResult:
            return ToolResult.success({"clicked": kwargs.get("target")})

    class MockSnapshotTool(BaseTool):
        name = "snapshot"
        description = "Mock snapshot"
        risk_tier = RiskTier.READ
        args_schema = DummyArgs

        async def _run(self, **kwargs) -> ToolResult:
            return ToolResult.success("Page snapshot: Login form with button ref=e1")

    registry.register(MockClickTool())
    registry.register(MockSnapshotTool())
    return registry


# -------------------------------------------------------------------------
# GoalInterpreter Tests
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_goal_interpreter_non_blocking(fake_llm: FakeLLM):
    interpreter = GoalInterpreter(fake_llm)
    expected_goal = Goal(
        outcome="Find latest invoice from Acme Corp Ltd and enter it into ERP",
        success_criteria=["Bill created in Acme ERP with attached PDF"],
        constraints=["Do not duplicate bills"],
        unknowns=[],
        is_blocking=False,
    )
    fake_llm.queue_response(
        LLMResponse(
            content=expected_goal.model_dump_json(),
            parsed=expected_goal,
            usage=TokenUsage(prompt_tokens=20, completion_tokens=20, total_tokens=40),
            provider="fake",
        )
    )

    goal = await interpreter.interpret("Find latest invoice from Acme Corp Ltd and enter it into ERP")
    assert not goal.is_blocking
    assert "Acme Corp Ltd" in goal.outcome
    assert len(goal.success_criteria) == 1


@pytest.mark.asyncio
async def test_goal_interpreter_blocking(fake_llm: FakeLLM):
    interpreter = GoalInterpreter(fake_llm)
    blocking_goal = Goal(
        outcome="Pay vendor",
        success_criteria=[],
        constraints=[],
        unknowns=["Vendor name is missing"],
        is_blocking=True,
        clarification_question="Which vendor would you like to process payments for?",
    )
    fake_llm.queue_response(
        LLMResponse(
            content=blocking_goal.model_dump_json(),
            parsed=blocking_goal,
            usage=TokenUsage(prompt_tokens=15, completion_tokens=15, total_tokens=30),
            provider="fake",
        )
    )

    goal = await interpreter.interpret("Pay the vendor")
    assert goal.is_blocking
    assert goal.clarification_question is not None


# -------------------------------------------------------------------------
# Planner Tests
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_planner_creates_initial_plan(fake_llm: FakeLLM):
    planner = Planner(fake_llm)
    initial_output = InitialPlanOutput(
        intents=[
            Intent(id="intent_1", description="Download invoice from portal", expected_outcome="PDF downloaded"),
            Intent(id="intent_2", description="Extract invoice data", expected_outcome="Data in memory"),
            Intent(id="intent_3", description="Submit bill into ERP", expected_outcome="Bill submitted"),
        ]
    )
    fake_llm.queue_response(
        LLMResponse(
            content=initial_output.model_dump_json(),
            parsed=initial_output,
            provider="fake",
        )
    )

    goal = Goal(outcome="Process Acme invoice", success_criteria=["Bill submitted"])
    plan = await planner.plan(goal, ["open_url", "click", "read_file"])

    assert len(plan.intents) == 3
    assert plan.intents[0].status == IntentStatus.IN_PROGRESS
    assert plan.intents[1].status == IntentStatus.PENDING
    assert plan.current_intent_index == 0


@pytest.mark.asyncio
async def test_planner_replan_adapts_pending_intents(fake_llm: FakeLLM):
    planner = Planner(fake_llm)

    existing_plan = Plan(
        intents=[
            Intent(id="intent_1", description="Download invoice from portal", expected_outcome="PDF downloaded", status=IntentStatus.COMPLETED),
            Intent(id="intent_2", description="Submit bill to ERP", expected_outcome="Bill submitted", status=IntentStatus.FAILED),
        ],
        current_intent_index=1,
        replan_count=0,
    )

    replan_output = InitialPlanOutput(
        intents=[
            Intent(id="new_intent", description="Check Mailbox for alternative invoice and submit", expected_outcome="Alternative bill submitted"),
        ]
    )
    fake_llm.queue_response(
        LLMResponse(
            content=replan_output.model_dump_json(),
            parsed=replan_output,
            provider="fake",
        )
    )

    goal = Goal(outcome="Process invoice")
    reflection = Reflection(summary="Invoice not found in portal", observation_type=ObservationType.SURPRISE)

    updated_plan = await planner.replan(goal, existing_plan, reflection, {}, last_error="Invoice not found in portal")
    assert updated_plan.replan_count == 1
    # Completed intent 1 is preserved
    assert updated_plan.intents[0].status == IntentStatus.COMPLETED
    assert updated_plan.intents[0].id == "intent_1"
    # New adapted intent is added
    assert len(updated_plan.intents) == 2
    assert "Mailbox" in updated_plan.intents[1].description


# -------------------------------------------------------------------------
# Reflector Tests
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reflector_heuristic_fast_path():
    reflector = Reflector(llm_client=None)
    goal = Goal(outcome="Process invoice")
    action = AgentAction(thought="Download file", tool="download_file")

    # Success with download
    res_success = ToolResult.success({"downloaded_to": "/tmp/inv.pdf", "file_name": "inv.pdf"})
    intent = Intent(id="i1", description="Download latest invoice PDF", expected_outcome="Downloaded file")
    ref = await reflector.reflect(goal, intent, action, res_success, {})

    assert ref.observation_type == ObservationType.PROGRESS
    assert ref.intent_completed is True
    assert ref.facts_learned["downloaded_file"] == "/tmp/inv.pdf"

    # Error handling
    res_fail = ToolResult.failure("Session expired - please login")
    ref_fail = await reflector.reflect(goal, intent, action, res_fail, {})
    assert ref_fail.observation_type == ObservationType.SURPRISE
    assert not ref_fail.intent_completed


# -------------------------------------------------------------------------
# ContextManager Tests
# -------------------------------------------------------------------------
def test_context_manager_artifact_offload(run_manager: RunManager):
    ctx_mgr = ContextManager(run_manager, max_observation_chars=100)
    run = run_manager.create_run(task="Artifact offload test")

    huge_text = "A" * 500
    res = ToolResult.success(huge_text)
    summary, data = ctx_mgr.normalize_observation(run.run_id, "snapshot", res)

    assert "Truncated" in summary
    assert "artifact_id" in data
    # Check that artifact actually exists on disk and in DB
    arts = run_manager.get_artifacts(run.run_id)
    assert len(arts) == 1
    assert arts[0].size_bytes == 500


def test_context_manager_rolling_summarization(run_manager: RunManager):
    ctx_mgr = ContextManager(run_manager, max_recent_steps=2)
    run = run_manager.create_run(task="Rolling test")

    # Add 4 steps
    for i in range(1, 5):
        run_manager.record_step(
            run.run_id,
            StepRecord(
                run_id=run.run_id,
                step_index=i,
                thought=f"Step thought {i}",
                tool_name="click",
                tool_args={"target": f"e{i}"},
                observation_summary=f"Result {i}",
                ok=True,
            ),
        )

    steps = run_manager.get_steps(run.run_id)
    prompt = ctx_mgr.build_prompt_context(
        goal=Goal(outcome="Test goal"),
        plan=Plan(),
        working_memory={"key": "val"},
        steps=steps,
    )

    # Should contain condensed summary of earlier steps 1..2
    assert "Earlier Steps 1..2 Summary" in prompt
    # Detailed recent steps 3 and 4 should be present
    assert "--- Step 3 (click) ---" in prompt
    assert "--- Step 4 (click) ---" in prompt


# -------------------------------------------------------------------------
# AgentExecutor Integration Tests
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_executor_human_ask_user_pause_and_resume(
    fake_llm: FakeLLM,
    run_manager: RunManager,
    tool_registry: ToolRegistry,
):
    executor = AgentExecutor(
        llm_client=fake_llm,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=MockVerifier(),
    )

    # 1. Goal Interpreter response
    fake_llm.queue_response(
        LLMResponse(
            parsed=Goal(outcome="Clarify vendor task", is_blocking=False),
            provider="fake",
        )
    )

    # 2. Planner response
    fake_llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Clarify with user", expected_outcome="Got answer")]),
            provider="fake",
        )
    )

    # 3. Action 1: Ask user tool call
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="I need to ask the user which vendor they mean",
                tool="ask_user",
                args={"question": "Did you mean Acme Corp Ltd or Acme Corp Inc?", "options": ["Acme Corp Ltd", "Acme Corp Inc"]},
            ),
            provider="fake",
        )
    )

    # Run should pause in WAITING_USER
    paused_run = await executor.run("Process Acme bill")
    assert paused_run.status == RunStatus.WAITING_USER
    assert paused_run.working_memory["pending_question"] == "Did you mean Acme Corp Ltd or Acme Corp Inc?"

    # 4. Action after resume: Finish
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Received user answer, proceeding to finish",
                is_finish=True,
                finish_summary="Task completed for Acme Corp Ltd",
            ),
            provider="fake",
        )
    )

    resumed_run = await executor.resume(paused_run.run_id, human_input="Acme Corp Ltd")
    assert resumed_run.status == RunStatus.COMPLETED
    assert resumed_run.working_memory["user_clarification"] == "Acme Corp Ltd"


@pytest.mark.asyncio
async def test_executor_approval_pause_and_resume(
    fake_llm: FakeLLM,
    run_manager: RunManager,
    tool_registry: ToolRegistry,
):
    executor = AgentExecutor(
        llm_client=fake_llm,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=MockVerifier(),
    )

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Large payment", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Submit bill", expected_outcome="Bill submitted")]), provider="fake"))
    # Ask approval
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Amount exceeds 50000, need approval",
                tool="request_approval",
                args={"action": "Create bill for 75000 INR", "reason": "Amount > 50000 threshold"},
            ),
            provider="fake",
        )
    )

    paused_run = await executor.run("Create high-value bill")
    assert paused_run.status == RunStatus.WAITING_APPROVAL

    # Resume with approval
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Approval received, submitting and finishing",
                is_finish=True,
                finish_summary="Bill submitted after manager approval",
            ),
            provider="fake",
        )
    )

    resumed_run = await executor.resume(paused_run.run_id, approval_granted=True)
    assert resumed_run.status == RunStatus.COMPLETED
    assert resumed_run.working_memory["approval_status"] == "APPROVED"


@pytest.mark.asyncio
async def test_executor_loop_detection_triggers_replan(
    fake_llm: FakeLLM,
    run_manager: RunManager,
    tool_registry: ToolRegistry,
):
    executor = AgentExecutor(
        llm_client=fake_llm,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=MockVerifier(),
        max_steps=10,
    )

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Loop test", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Click button", expected_outcome="Button clicked")]), provider="fake"))

    # Produce same click 3 times in a row
    for _ in range(3):
        fake_llm.queue_response(
            LLMResponse(
                parsed=AgentAction(
                    thought="Clicking button e1",
                    tool="click",
                    args={"target": "e1"},
                ),
                provider="fake",
            )
        )

    # Replan response when loop is detected
    fake_llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(intents=[Intent(id="adapted", description="Try alternative button e2", expected_outcome="Alternative worked")]),
            provider="fake",
        )
    )

    # Then finish
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Alternative succeeded, finishing",
                is_finish=True,
                finish_summary="Done after loop recovery",
            ),
            provider="fake",
        )
    )

    run = await executor.run("Loop task")
    assert run.status == RunStatus.COMPLETED
    assert run.plan["replan_count"] >= 1
