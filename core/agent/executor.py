from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from core.verify.models import VerificationOutcome

if TYPE_CHECKING:
    from core.verify.verifier import IndependentVerifier

logger = logging.getLogger(__name__)


def _require_run(run: RunRecord | None, run_id: str) -> RunRecord:
    if run is None:
        raise ValueError(f"Run {run_id} not found")
    return run


class AgentExecutor:
    """
    CentrAlign Core Agent Loop.
    Coordinates GoalInterpreter, Living Planner, Tool Execution,
    Reflector, Context Management, SQLite Run State Machine, and Independent Verifier.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        run_manager: RunManager | None = None,
        registry: ToolRegistry | None = None,
        company_dir: Path | str = "company",
        max_steps: int = 30,
        max_retries_per_intent: int = 3,
        max_verification_retries: int = 2,
        interpreter: GoalInterpreter | None = None,
        planner: Planner | None = None,
        reflector: Reflector | None = None,
        verifier: IndependentVerifier | None = None,
    ) -> None:
        self.llm = llm_client
        self.run_manager = run_manager or RunManager()
        self.tools = registry or tool_registry
        self.company_dir = Path(company_dir)
        self.max_steps = max_steps
        self.max_retries_per_intent = max_retries_per_intent
        self.max_verification_retries = max_verification_retries

        self.interpreter = interpreter or GoalInterpreter(self.llm)
        self.planner = planner or Planner(self.llm)
        self.reflector = reflector or Reflector(self.llm)
        self.context_manager = ContextManager(self.run_manager)
        if verifier is not None:
            self.verifier = verifier
        else:
            from core.verify.verifier import IndependentVerifier

            self.verifier = IndependentVerifier(artifacts_dir=self.run_manager.artifacts_dir)

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

    async def _recover_auth(self, run_id: str, system: str = "erp") -> None:
        """Re-authenticate using stored credentials from company systems vault."""
        logger.info(f"[Auth Recovery] Re-authenticating session for system '{system}'")
        if system == "portal":
            await self.tools.execute("browser_open_url", {"url": "http://127.0.0.1:8001/portal/login"})
            await self.tools.execute("browser_type_text", {"target": "#email", "text": "supplier@acme.com"})
            await self.tools.execute("browser_type_text", {"target": "#password", "text": "portal_pass123"})
            await self.tools.execute("browser_click", {"target": "#portal-submit-btn"})
        else:
            await self.tools.execute("browser_open_url", {"url": "http://127.0.0.1:8001/erp/login"})
            await self.tools.execute("browser_type_text", {"target": "#username", "text": "admin"})
            await self.tools.execute("browser_type_text", {"target": "#password", "text": "erp_pass123"})
            await self.tools.execute("browser_click", {"target": "#erp-submit-btn"})

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

            # 1. Build prompt context (including any active verification discrepancies to repair)
            context_prompt = self.context_manager.build_prompt_context(
                goal=goal,
                plan=plan,
                working_memory=working_memory,
                steps=steps,
                company_context=company_context,
            )

            # If active verification discrepancies exist, add prominent repair instructions
            active_discrepancies = working_memory.get("active_discrepancies")
            if active_discrepancies:
                context_prompt += (
                    f"\n\n🚨 ATTENTION — VERIFICATION FAILED PREVIOUSLY:\n"
                    f"The independent verifier detected discrepancies: {active_discrepancies}.\n"
                    f"You MUST take corrective action to resolve these before attempting to finish."
                )

            # 2. Query LLM for next action
            messages = [
                LLMMessage(role="system", content=EXECUTOR_SYSTEM_PROMPT),
                LLMMessage(role="user", content=context_prompt),
            ]

            action: AgentAction
            tokens_for_step = 0
            try:
                response = await self.llm.generate_async(
                    messages=messages,
                    tools=self.tools.export_definitions(),
                    response_model=AgentAction,
                    temperature=0.1,
                )
                tokens_for_step = response.usage.total_tokens

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

            # 3. Check for Task Completion & Trigger Independent Verifier
            if action.is_finish or (plan.is_completed and action.tool is None):
                self.run_manager.transition(run_id, RunStatus.VERIFYING, reason="Action indicated finish, verifying")

                # Execute Independent Verification
                verif_res = await self.verifier.verify(
                    run=run,
                    goal=goal,
                    working_memory=working_memory,
                    steps=steps,
                )
                working_memory["verification_outcome"] = verif_res.outcome.value
                working_memory["verification_discrepancies"] = verif_res.discrepancies

                if verif_res.outcome == VerificationOutcome.PASS:
                    self.run_manager.transition(
                        run_id,
                        RunStatus.COMPLETED,
                        reason="Independent verification PASSED with full evidence",
                    )
                    finish_step = StepRecord(
                        run_id=run_id,
                        step_index=step_index,
                        intent_id=plan.current_intent.id if plan.current_intent else None,
                        thought=action.thought,
                        tool_name="finish",
                        tool_args={"summary": action.finish_summary},
                        observation_summary=action.finish_summary or "Task verified and completed successfully.",
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
                else:
                    # Verification failed: Check bounded retry cap
                    v_attempts = working_memory.get("verification_attempts", 0) + 1
                    working_memory["verification_attempts"] = v_attempts

                    if v_attempts <= self.max_verification_retries:
                        logger.warning(
                            f"Verification failed on attempt {v_attempts}/{self.max_verification_retries}: "
                            f"{verif_res.discrepancies}. Injected into observations for self-correction."
                        )
                        self.run_manager.transition(
                            run_id,
                            RunStatus.RUNNING,
                            reason=f"Verification failed (attempt {v_attempts}): {verif_res.discrepancies}. Retrying correction.",
                        )
                        repair_step = StepRecord(
                            run_id=run_id,
                            step_index=step_index,
                            intent_id=plan.current_intent.id if plan.current_intent else None,
                            thought=action.thought,
                            tool_name="verify",
                            tool_args={},
                            observation_summary=(
                                f"VERIFICATION FAILED: {'; '.join(verif_res.discrepancies)}. "
                                f"Please fix these discrepancies before finishing."
                            ),
                            ok=False,
                            error="; ".join(verif_res.discrepancies),
                            tokens=tokens_for_step,
                        )
                        self.run_manager.record_step(run_id, repair_step)
                        working_memory["active_discrepancies"] = verif_res.discrepancies
                        self.run_manager.update_run(run_id, working_memory=working_memory)
                        run = _require_run(self.run_manager.get_run(run_id), run_id)
                        continue
                    else:
                        logger.error(
                            f"Verification failed after {v_attempts} attempts: {verif_res.discrepancies}. Transitioning to FAILED."
                        )
                        self.run_manager.transition(
                            run_id,
                            RunStatus.FAILED,
                            reason=f"Independent verifier rejected task completion due to discrepancies: {verif_res.discrepancies}",
                        )
                        fail_step = StepRecord(
                            run_id=run_id,
                            step_index=step_index,
                            intent_id=plan.current_intent.id if plan.current_intent else None,
                            thought=action.thought,
                            tool_name="verify",
                            tool_args={},
                            observation_summary=f"VERIFICATION REJECTED: {'; '.join(verif_res.discrepancies)}",
                            ok=False,
                            error="; ".join(verif_res.discrepancies),
                        )
                        self.run_manager.record_step(run_id, fail_step)
                        self.run_manager.update_run(
                            run_id,
                            error=f"Verification failed: {verif_res.discrepancies}",
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

            # 5. Execute Tool with Transient 500 Backoff Retry
            tool_name = action.tool or "snapshot"
            start_t = time.monotonic()
            tool_res = await self.tools.execute(tool_name, action.args)

            # Fault recovery: Transient 500 retry
            if not tool_res.ok and ("500" in str(tool_res.error) or "Internal Server Error" in str(tool_res.error)):
                logger.warning(f"Transient error encountered on tool '{tool_name}'. Retrying with backoff...")
                await asyncio.sleep(1.0)
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

            # Fault recovery: Session Expiry Mid-Task
            if reflection.facts_learned.get("session_expired"):
                logger.warning(f"Handling detected session expiry at step {step_index}")
                target_sys = "portal" if "/portal" in obs_summary else "erp"
                await self._recover_auth(run_id, system=target_sys)
                working_memory.pop("session_expired", None)
                working_memory.pop("auth_required", None)

            # Advance plan or trigger re-plan
            if reflection.intent_completed:
                logger.info(f"Intent {plan.current_intent.id if plan.current_intent else ''} completed!")
                plan.advance()
            elif reflection.observation_type.value in {"error", "no_op"} or reflection.needs_replan:
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
