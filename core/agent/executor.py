from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from core.agent.context import EXECUTOR_SYSTEM_PROMPT, ContextManager
from core.agent.interpreter import GoalInterpreter
from core.agent.models import AgentAction, Goal, Plan, Reflection
from core.agent.planner import Planner
from core.agent.reflector import Reflector
from core.llm.client import LLMClient
from core.llm.models import LLMMessage
from core.runs.manager import RunManager
from core.runs.models import RunRecord, RunStatus, StepRecord
from core.tools.registry import ToolRegistry, tool_registry

logger = logging.getLogger(__name__)


def _require_run(run: RunRecord | None, run_id: str) -> RunRecord:
    if run is None:
        raise ValueError(f"Run {run_id} not found")
    return run


class AgentExecutor:
    """
    CentrAlign Core Agent Loop.
    Coordinates GoalInterpreter, Living Planner, Tool Execution,
    Reflector, Context Management, and SQLite Run State Machine.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        run_manager: RunManager | None = None,
        registry: ToolRegistry | None = None,
        company_dir: Path | str = "company",
        max_steps: int = 30,
        max_retries_per_intent: int = 3,
        interpreter: GoalInterpreter | None = None,
        planner: Planner | None = None,
        reflector: Reflector | None = None,
    ) -> None:
        self.llm = llm_client
        self.run_manager = run_manager or RunManager()
        self.tools = registry or tool_registry
        self.company_dir = Path(company_dir)
        self.max_steps = max_steps
        self.max_retries_per_intent = max_retries_per_intent

        self.interpreter = interpreter or GoalInterpreter(self.llm)
        self.planner = planner or Planner(self.llm)
        self.reflector = reflector or Reflector(self.llm)
        self.context_manager = ContextManager(self.run_manager)

        self._company_context_cache: str | None = None

    def load_company_context(self) -> str:
        """Load company systems, procedures, and policies as contextual data."""
        if self._company_context_cache is not None:
            return self._company_context_cache

        context_parts: list[str] = []
        if self.company_dir.exists():
            for p in sorted(self.company_dir.rglob("*")):
                if p.is_file() and p.suffix in {".yaml", ".yml", ".md"}:
                    try:
                        rel_path = p.relative_to(self.company_dir)
                        content = p.read_text(encoding="utf-8").strip()
                        context_parts.append(f"### File: company/{rel_path}\n```\n{content}\n```")
                    except Exception as e:
                        logger.warning(f"Failed to read company file {p}: {e}")

        self._company_context_cache = "\n\n".join(context_parts)
        return self._company_context_cache

    async def run(
        self,
        task: str,
        run_id: str | None = None,
    ) -> RunRecord:
        """Execute a task from start to completion or pause."""
        company_context = self.load_company_context()

        # 1. Initialize Run in SQLite
        run = self.run_manager.create_run(task=task, run_id=run_id)
        logger.info(f"Initialized run {run.run_id} for task: {task[:60]}")

        # 2. Goal Interpretation
        self.run_manager.transition(run.run_id, RunStatus.CLARIFYING, reason="Interpreting task goal")
        goal = await self.interpreter.interpret(task, company_context)
        self.run_manager.update_run(run.run_id, goal=goal.model_dump())

        if goal.is_blocking:
            question = goal.clarification_question or "Clarification required to proceed."
            self.run_manager.update_run(run.run_id, working_memory={"clarification_question": question})
            self.run_manager.transition(
                run.run_id,
                RunStatus.WAITING_USER,
                reason=f"Blocking ambiguity: {question}",
            )
            return _require_run(self.run_manager.get_run(run.run_id), run.run_id)

        # 3. Initial Living Plan
        self.run_manager.transition(run.run_id, RunStatus.PLANNING, reason="Formulating living plan")
        available_tools = [t.name for t in self.tools.list_tools()]
        plan = await self.planner.plan(goal, available_tools, company_context)
        self.run_manager.update_run(run.run_id, plan=plan.model_dump())

        # 4. Transition to RUNNING
        self.run_manager.transition(run.run_id, RunStatus.RUNNING, reason="Starting execution loop")

        return await self._execute_loop(run.run_id, goal, plan, company_context)

    async def resume(
        self,
        run_id: str,
        human_input: str | None = None,
        approval_granted: bool = True,
    ) -> RunRecord:
        """Resume a paused run after human input or approval."""
        run = self.run_manager.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        company_context = self.load_company_context()
        goal = Goal.model_validate(run.goal) if run.goal else Goal(outcome=run.task)
        plan = Plan.model_validate(run.plan) if run.plan else Plan()
        working_memory = dict(run.working_memory)

        if run.status == RunStatus.WAITING_USER:
            if human_input:
                working_memory["user_clarification"] = human_input
            self.run_manager.update_run(run_id, working_memory=working_memory)
            self.run_manager.transition(run_id, RunStatus.RUNNING, reason="User input provided, resuming")
            return await self._execute_loop(run_id, goal, plan, company_context)

        elif run.status == RunStatus.WAITING_APPROVAL:
            if approval_granted:
                working_memory["approval_status"] = "APPROVED"
                self.run_manager.update_run(run_id, working_memory=working_memory)
                self.run_manager.transition(run_id, RunStatus.RUNNING, reason="Approval granted, resuming")
                return await self._execute_loop(run_id, goal, plan, company_context)
            else:
                working_memory["approval_status"] = "REJECTED"
                self.run_manager.update_run(run_id, working_memory=working_memory)
                self.run_manager.transition(run_id, RunStatus.ESCALATED, reason="Action approval rejected by human")
                return _require_run(self.run_manager.get_run(run_id), run_id)

        raise ValueError(f"Run {run_id} is in status {run.status.value}, cannot resume.")

    async def _execute_loop(
        self,
        run_id: str,
        goal: Goal,
        plan: Plan,
        company_context: str,
    ) -> RunRecord:
        """Inner step-by-step execution loop."""
        run = self.run_manager.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        working_memory: dict[str, Any] = dict(run.working_memory)
        action_history_hashes: list[str] = []

        while run.step_count < self.max_steps:
            steps = self.run_manager.get_steps(run_id)
            step_index = len(steps) + 1

            # 1. Build prompt context
            context_prompt = self.context_manager.build_prompt_context(
                goal=goal,
                plan=plan,
                working_memory=working_memory,
                steps=steps,
                company_context=company_context,
            )

            # 2. Query LLM for next action
            messages = [
                LLMMessage(role="system", content=EXECUTOR_SYSTEM_PROMPT),
                LLMMessage(role="user", content=context_prompt),
            ]

            action: AgentAction
            tokens_for_step = 0
            try:
                # Use tool definitions if available, plus structured response model
                response = await self.llm.generate_async(
                    messages=messages,
                    tools=self.tools.export_definitions(),
                    response_model=AgentAction,
                    temperature=0.1,
                )
                tokens_for_step = response.usage.total_tokens

                # If the LLM invoked a tool via native tool calling:
                if response.tool_calls:
                    tc = response.tool_calls[0]
                    action = AgentAction(
                        thought=response.content or f"Calling tool {tc.name}",
                        tool=tc.name,
                        args=tc.arguments,
                        is_finish=False,
                    )
                else:
                    action = response.get_parsed(AgentAction)

            except Exception as e:
                logger.error(f"Action generation error at step {step_index}: {e}")
                action = AgentAction(
                    thought=f"Encountered error generating action: {e}. Attempting recovery.",
                    tool="snapshot",
                    args={},
                )

            logger.info(
                f"[Run {run_id} | Step {step_index}] Intent: {plan.current_intent.id if plan.current_intent else 'None'} | "
                f"Action: {action.tool} | Finish: {action.is_finish}"
            )

            # 3. Check for Task Completion
            if action.is_finish or (plan.is_completed and action.tool is None):
                self.run_manager.transition(run_id, RunStatus.VERIFYING, reason="Action indicated finish, verifying")
                # Transition to COMPLETED
                self.run_manager.transition(run_id, RunStatus.COMPLETED, reason="Task successfully accomplished")
                finish_step = StepRecord(
                    run_id=run_id,
                    step_index=step_index,
                    intent_id=plan.current_intent.id if plan.current_intent else None,
                    thought=action.thought,
                    tool_name="finish",
                    tool_args={"summary": action.finish_summary},
                    observation_summary=action.finish_summary or "Task finished successfully.",
                    ok=True,
                    tokens=tokens_for_step,
                )
                self.run_manager.record_step(run_id, finish_step)
                self.run_manager.update_run(
                    run_id,
                    plan=plan.model_dump(),
                    working_memory=working_memory,
                )
                return _require_run(self.run_manager.get_run(run_id), run_id)

            # 4. Handle Human Tools Pause
            if action.tool == "ask_user":
                question = str(action.args.get("question", "User input requested"))
                options = action.args.get("options")
                working_memory["pending_question"] = question
                working_memory["options"] = options
                self.run_manager.update_run(run_id, working_memory=working_memory)
                pause_step = StepRecord(
                    run_id=run_id,
                    step_index=step_index,
                    intent_id=plan.current_intent.id if plan.current_intent else None,
                    thought=action.thought,
                    tool_name="ask_user",
                    tool_args=action.args,
                    observation_summary=f"Waiting for user input: {question}",
                    ok=True,
                    tokens=tokens_for_step,
                )
                self.run_manager.record_step(run_id, pause_step)
                self.run_manager.transition(run_id, RunStatus.WAITING_USER, reason=f"Awaiting user answer to: {question}")
                return _require_run(self.run_manager.get_run(run_id), run_id)

            if action.tool == "request_approval":
                action_desc = str(action.args.get("action", "Sensitive action"))
                reason = str(action.args.get("reason", "Policy threshold reached"))
                working_memory["pending_approval_action"] = action_desc
                working_memory["approval_reason"] = reason
                self.run_manager.update_run(run_id, working_memory=working_memory)
                pause_step = StepRecord(
                    run_id=run_id,
                    step_index=step_index,
                    intent_id=plan.current_intent.id if plan.current_intent else None,
                    thought=action.thought,
                    tool_name="request_approval",
                    tool_args=action.args,
                    observation_summary=f"Waiting for manager approval: {action_desc} ({reason})",
                    ok=True,
                    tokens=tokens_for_step,
                )
                self.run_manager.record_step(run_id, pause_step)
                self.run_manager.transition(run_id, RunStatus.WAITING_APPROVAL, reason=f"Awaiting approval for: {action_desc}")
                return _require_run(self.run_manager.get_run(run_id), run_id)

            # 5. Execute Tool
            tool_name = action.tool or "snapshot"
            start_t = time.monotonic()
            tool_res = await self.tools.execute(tool_name, action.args)
            duration_ms = (time.monotonic() - start_t) * 1000.0

            # 6. Normalize Observation & Offload Artifacts
            obs_summary, obs_data = self.context_manager.normalize_observation(
                run_id=run_id,
                tool_name=tool_name,
                result=tool_res,
            )

            # 7. Loop Detection Check
            action_hash = hashlib.md5(f"{tool_name}:{json.dumps(action.args, sort_keys=True)}:{obs_summary}".encode()).hexdigest()
            action_history_hashes.append(action_hash)
            if len(action_history_hashes) >= 3 and len(set(action_history_hashes[-3:])) == 1:
                logger.warning(f"Loop detected at step {step_index}! Same action & observation repeated 3 times.")
                plan = await self.planner.replan(
                    goal=goal,
                    current_plan=plan,
                    reflection=Reflection(
                        summary=f"Loop detected on tool {tool_name}: identical outcome repeated 3 times.",
                    ),
                    working_memory=working_memory,
                    last_error=f"Loop detected on tool {tool_name}: identical outcome repeated 3 times.",
                    company_context=company_context,
                )

            # 8. Reflection
            reflection = await self.reflector.reflect(
                goal=goal,
                current_intent=plan.current_intent,
                action=action,
                result=tool_res,
                current_memory=working_memory,
            )

            # Update working memory
            if reflection.facts_learned:
                working_memory.update(reflection.facts_learned)

            # Advance plan or trigger re-plan
            if reflection.intent_completed:
                logger.info(f"Intent {plan.current_intent.id if plan.current_intent else ''} completed!")
                plan.advance()
            elif reflection.observation_type.value in {"error", "no_op"}:
                curr = plan.current_intent
                if curr:
                    curr.retries += 1
                    if curr.retries >= curr.max_retries or reflection.needs_replan:
                        logger.warning(f"Retries exceeded or replan needed for intent {curr.id}. Re-planning...")
                        plan = await self.planner.replan(
                            goal=goal,
                            current_plan=plan,
                            reflection=reflection,
                            working_memory=working_memory,
                            last_error=reflection.summary,
                            company_context=company_context,
                        )

            # 9. Record Step in SQLite Audit Log
            step_record = StepRecord(
                run_id=run_id,
                step_index=step_index,
                intent_id=plan.current_intent.id if plan.current_intent else None,
                thought=action.thought,
                tool_name=tool_name,
                tool_args=action.args,
                observation_summary=obs_summary,
                observation_data=obs_data,
                ok=tool_res.ok,
                error=tool_res.error,
                evidence_refs=tool_res.evidence_refs,
                duration_ms=duration_ms,
                tokens=tokens_for_step,
            )
            self.run_manager.record_step(run_id, step_record)

            # Update Run State
            self.run_manager.update_run(
                run_id,
                plan=plan.model_dump(),
                working_memory=working_memory,
            )

            # Refresh run object
            run = _require_run(self.run_manager.get_run(run_id), run_id)

        # 10. Step budget exhausted without completion
        logger.error(f"Run {run_id} failed: exceeded max step budget ({self.max_steps}).")
        self.run_manager.transition(
            run_id,
            RunStatus.FAILED,
            reason=f"Exceeded max step budget ({self.max_steps}) without completing all criteria.",
        )
        return _require_run(self.run_manager.get_run(run_id), run_id)
