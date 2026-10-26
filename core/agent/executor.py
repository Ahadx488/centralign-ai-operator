from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agent.context import EXECUTOR_SYSTEM_PROMPT, ContextManager
from core.agent.interpreter import GoalInterpreter
from core.agent.models import AgentAction, Goal, Plan, Reflection
from core.agent.planner import Planner
from core.agent.reflector import Reflector
from core.jobs.idempotency import IdempotencyStore
from core.llm.client import LLMClient
from core.llm.fake import FakeLLM
from core.llm.models import LLMMessage
from core.memory.company import CompanyMemoryIndex
from core.memory.lessons import LessonStore
from core.memory.proposed import ProposedMemoryStore
from core.policy.approvals import ApprovalInbox
from core.policy.engine import PolicyEngine
from core.policy.models import PolicyDecision, PolicyEffect, RunCapabilities
from core.runs.manager import RunManager
from core.runs.models import RunRecord, RunStatus, StepRecord
from core.tools.registry import RiskTier, ToolRegistry, ToolResult, tool_registry
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
        step_callback: Callable[[StepRecord], None] | None = None,
    ) -> None:
        self.llm = llm_client
        self.run_manager = run_manager or RunManager()
        self.tools = registry or tool_registry
        self.company_dir = Path(company_dir)
        self.max_steps = max_steps
        self.max_retries_per_intent = max_retries_per_intent
        self.max_verification_retries = max_verification_retries
        self.step_callback = step_callback

        self.interpreter = interpreter or GoalInterpreter(self.llm)
        self.planner = planner or Planner(self.llm)
        self.reflector = reflector or Reflector(None if isinstance(self.llm, FakeLLM) else self.llm)
        self.context_manager = ContextManager(self.run_manager)
        self.company_memory = CompanyMemoryIndex(company_dir=self.company_dir)
        self.proposed_memory = ProposedMemoryStore()
        self.policy_engine = PolicyEngine(policy_file=self.company_dir / "policies.yaml")
        self.idempotency = IdempotencyStore(db_path=self.run_manager.db_path)
        self.approval_inbox = ApprovalInbox(db_path=self.run_manager.db_path)
        self.lesson_store = LessonStore(db_path=self.run_manager.db_path)

        if verifier is not None:
            self.verifier = verifier
        else:
            from core.verify.verifier import IndependentVerifier

            self.verifier = IndependentVerifier(artifacts_dir=str(self.run_manager.artifacts_dir))

        self._company_context_cache: str | None = None

    def _record_and_notify_step(self, run_id: str, step: StepRecord) -> None:
        self.run_manager.record_step(run_id, step)
        if self.step_callback:
            try:
                self.step_callback(step)
            except Exception as e:
                logger.warning(f"Error in step callback: {e}")

    def load_company_context(self, query: str | None = None) -> str:
        """Dynamically retrieve relevant company memory, procedures, policies, and learned lessons."""
        if query:
            docs = self.company_memory.retrieve(query, top_k=6)
            base_context = self.company_memory.format_context(docs)
            lessons = self.lesson_store.retrieve_lessons(query)
            if lessons:
                base_context += self.lesson_store.format_lessons(lessons)
            return base_context

        if self._company_context_cache is not None:
            return self._company_context_cache

        docs = self.company_memory.retrieve("", top_k=10)
        self._company_context_cache = self.company_memory.format_context(docs)
        return self._company_context_cache

    async def run(
        self,
        task: str,
        run_id: str | None = None,
        capabilities: RunCapabilities | None = None,
    ) -> RunRecord:
        """Execute a task from start to completion or pause."""
        company_context = self.load_company_context(query=task)

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

        return await self._execute_loop(run.run_id, goal, plan, company_context, capabilities=capabilities)

    async def resume(
        self,
        run_id: str,
        human_input: str | None = None,
        approval_granted: bool = True,
        edited_args: dict[str, Any] | None = None,
        capabilities: RunCapabilities | None = None,
    ) -> RunRecord:
        """Resume a paused run after human input or approval."""
        run = self.run_manager.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        company_context = self.load_company_context(query=run.task)
        goal = Goal.model_validate(run.goal) if run.goal else Goal(outcome=run.task)
        plan = Plan.model_validate(run.plan) if run.plan else Plan()
        working_memory = dict(run.working_memory)

        if run.status == RunStatus.WAITING_USER:
            if human_input:
                working_memory["user_clarification"] = human_input
            self.run_manager.update_run(run_id, working_memory=working_memory)
            self.run_manager.transition(run_id, RunStatus.RUNNING, reason="User input provided, resuming")
            return await self._execute_loop(run_id, goal, plan, company_context, capabilities=capabilities)

        elif run.status == RunStatus.WAITING_APPROVAL:
            if approval_granted:
                working_memory["approval_status"] = "APPROVED"
                if edited_args is not None:
                    working_memory["edited_args"] = edited_args
                self.run_manager.update_run(run_id, working_memory=working_memory)
                self.run_manager.transition(run_id, RunStatus.RUNNING, reason="Approval granted, resuming")
                return await self._execute_loop(run_id, goal, plan, company_context, capabilities=capabilities)
            else:
                working_memory["approval_status"] = "REJECTED"
                self.run_manager.update_run(run_id, working_memory=working_memory)
                self.run_manager.transition(run_id, RunStatus.ESCALATED, reason="Action approval rejected by human")
                return _require_run(self.run_manager.get_run(run_id), run_id)

        raise ValueError(f"Run {run_id} is in status {run.status.value}, cannot resume.")

    async def resume_interrupted(
        self,
        run_id: str,
        capabilities: RunCapabilities | None = None,
    ) -> RunRecord:
        """
        Resume an interrupted or orphaned run from the exact last persisted step in SQLite,
        leveraging idempotency keys to ensure no completed write actions are repeated.
        """
        run = self.run_manager.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        company_context = self.load_company_context(query=run.task)
        goal = Goal.model_validate(run.goal) if run.goal else Goal(outcome=run.task)
        plan = Plan.model_validate(run.plan) if run.plan else Plan()

        logger.info(f"[Crash Recovery] Resuming execution loop for run {run_id} (steps logged: {run.step_count})")
        return await self._execute_loop(
            run_id,
            goal,
            plan,
            company_context,
            capabilities=capabilities,
        )

    async def _recover_auth(self, run_id: str, system: str = "erp") -> None:
        """Re-authenticate using stored credentials from company systems vault."""
        logger.info(f"[Auth Recovery] Re-authenticating session for system '{system}'")
        import yaml

        from core.memory.vault import credential_vault

        systems_path = Path("company/systems.yaml")
        systems_cfg: dict[str, Any] = {}
        if systems_path.exists():
            try:
                systems_cfg = yaml.safe_load(systems_path.read_text(encoding="utf-8")) or {}
            except Exception:
                pass

        sys_dict = systems_cfg.get("systems", {})
        sys_key = system
        if sys_key not in sys_dict:
            for k in sys_dict:
                if system.lower() in k.lower() or k.lower() in system.lower():
                    sys_key = k
                    break

        sys_info = sys_dict.get(sys_key, {})
        login_info = sys_info.get("login", {})
        login_url = login_info.get("login_url") or sys_info.get("url", "")
        if not login_url.endswith("/login") and "/login" not in login_url:
            login_url = f"{login_url.rstrip('/')}/login"

        user_field = login_info.get("username_field", "username")
        pass_field = login_info.get("password_field", "password")
        vault_ref = login_info.get("default_credentials", f"vault:{sys_key}_credentials")
        creds = credential_vault.get(vault_ref)

        user_val = creds.get("username") or creds.get("email", "")
        pass_val = creds.get("password", "")

        if login_url and user_val and pass_val:
            await self.tools.execute("browser_open_url", {"url": login_url})
            await self.tools.execute("browser_type_text", {"target": f"#{user_field}", "text": user_val})
            await self.tools.execute("browser_type_text", {"target": f"#{pass_field}", "text": pass_val})
            await self.tools.execute("browser_click", {"target": "button[type='submit']"})

    async def _execute_loop(
        self,
        run_id: str,
        goal: Goal,
        plan: Plan,
        company_context: str,
        capabilities: RunCapabilities | None = None,
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
                combined_finish_text = f"{action.thought} {action.finish_summary or ''}".lower()
                if "duplicate" in combined_finish_text and ("exist" in combined_finish_text or "skip" in combined_finish_text or "prevent" in combined_finish_text or "detect" in combined_finish_text):
                    working_memory["is_duplicate"] = True
                    working_memory["duplicate_detected"] = True

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
                    # Auto-promote proposed facts from this verified run into active company memory
                    for fact in self.proposed_memory.get_facts(run_id=run_id, status="PROPOSED"):
                        if fact.id is not None:
                            self.proposed_memory.promote_fact(fact.id)
                    self.company_memory.reindex(additional_facts=self.proposed_memory.get_promoted_facts())

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
                    self._record_and_notify_step(run_id, finish_step)
                    self.run_manager.update_run(
                        run_id,
                        plan=plan.model_dump(),
                        working_memory=working_memory,
                    )

                    # Dynamic Continuous Learning: LLM extracts lessons from completed step history
                    try:
                        all_steps = self.run_manager.get_steps(run_id)
                        await self.lesson_store.extract_and_record_lessons(
                            llm=self.llm,
                            run_id=run_id,
                            goal_outcome=goal.outcome,
                            steps=all_steps,
                        )
                    except Exception as e:
                        logger.warning(f"[Continuous Learning] Extraction skipped or failed: {e}")

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
                        self._record_and_notify_step(run_id, repair_step)
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
                        self._record_and_notify_step(run_id, fail_step)
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
                self._record_and_notify_step(run_id, pause_step)
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
                self._record_and_notify_step(run_id, pause_step)
                self.run_manager.transition(run_id, RunStatus.WAITING_APPROVAL, reason=f"Awaiting approval for: {action_desc}")
                return _require_run(self.run_manager.get_run(run_id), run_id)

            # 5. Evaluate Policy Engine before executing tool
            raw_tool = (action.tool or "snapshot").strip()
            tool_name = "snapshot"
            for t_obj in self.tools.list_tools():
                if t_obj.name in raw_tool:
                    tool_name = t_obj.name
                    break
            else:
                raw_clean = re.sub(r"[^a-zA-Z0-9_]", "", raw_tool.split("(")[0])
                tool_name = raw_clean or "snapshot"

            tool_def = self.tools.get(tool_name)
            risk_tier = tool_def.risk_tier if tool_def else RiskTier.READ
            tier_str = risk_tier.value if isinstance(risk_tier, RiskTier) else str(risk_tier)

            # If this action was just approved by the user via resume(), bypass policy hold
            was_approved = working_memory.pop("approval_status", None) == "APPROVED"
            if was_approved:
                policy_decision = PolicyDecision.allow("Approved by authorized human operator", risk_tier=risk_tier)
            else:
                policy_decision = self.policy_engine.evaluate(
                    tool_name=tool_name,
                    tool_args=action.args,
                    risk_tier=risk_tier,
                    capabilities=capabilities,
                    context=working_memory,
                )

            if policy_decision.effect == PolicyEffect.REQUIRE_APPROVAL:
                action_desc = f"{tool_name}({action.args})"
                reason = policy_decision.reason
                working_memory["pending_approval_action"] = action_desc
                working_memory["approval_reason"] = reason
                self.run_manager.update_run(run_id, working_memory=working_memory)
                # Create durable approval inbox entry for human operator
                self.approval_inbox.create_request(
                    run_id=run_id,
                    action=action_desc,
                    args=action.args,
                    reason=reason,
                    risk_tier=tier_str,
                    timeout_seconds=300.0,
                )
                pause_step = StepRecord(
                    run_id=run_id,
                    step_index=step_index,
                    intent_id=plan.current_intent.id if plan.current_intent else None,
                    thought=action.thought,
                    tool_name=tool_name,
                    tool_args=action.args,
                    observation_summary=f"Waiting for manager approval: {action_desc} ({reason})",
                    ok=True,
                    policy_decision=policy_decision.effect.value,
                    policy_reason=reason,
                    tokens=tokens_for_step,
                )
                self._record_and_notify_step(run_id, pause_step)
                self.run_manager.transition(
                    run_id,
                    RunStatus.WAITING_APPROVAL,
                    reason=f"Policy hold: {reason}",
                )
                return _require_run(self.run_manager.get_run(run_id), run_id)

            elif policy_decision.effect == PolicyEffect.DENY:
                logger.warning(f"Action '{tool_name}' DENIED by policy: {policy_decision.reason}")
                if policy_decision.rule_id == "prompt_injection_exfiltration_blocked":
                    working_memory["security_alert"] = policy_decision.reason
                    logger.error(f"[SECURITY ALERT] {policy_decision.reason}")
                tool_res = ToolResult.failure(
                    error=f"DENIED by policy rule '{policy_decision.rule_id}': {policy_decision.reason}"
                )
                duration_ms = 0.0
            else:
                # ALLOW
                effective_args = dict(action.args)
                if was_approved and "edited_args" in working_memory:
                    edited = working_memory.pop("edited_args")
                    if isinstance(edited, dict):
                        effective_args.update(edited)
                        logger.info(f"[Approval Edited Args] Using edited arguments: {effective_args}")

                # Parameter hygiene & thought-extraction fallback
                if "email_id" in effective_args and effective_args["email_id"] is not None:
                    try:
                        effective_args["email_id"] = int(effective_args["email_id"])
                    except (ValueError, TypeError):
                        pass
                if tool_name == "mail_read" and effective_args.get("email_id") is None:
                    m = re.search(r"(?:email(?:\s+id)?|id)\s*[:#]?\s*(\d+)", action.thought, re.IGNORECASE)
                    if m:
                        effective_args["email_id"] = int(m.group(1))
                if tool_name == "mail_save_attachment":
                    if effective_args.get("email_id") is None and not effective_args.get("filename"):
                        m = re.search(r"(?:email(?:\s+id)?|id)\s*[:#]?\s*(\d+)", action.thought, re.IGNORECASE)
                        if m:
                            effective_args["email_id"] = int(m.group(1))
                    if not effective_args.get("filename"):
                        m_file = re.search(r"([\w\-]+\.(?:pdf|csv|png))", action.thought, re.IGNORECASE)
                        if m_file:
                            effective_args["filename"] = m_file.group(1)
                if tool_name == "files_search_files" and not effective_args.get("pattern"):
                    m_pat = re.search(r"pattern\s*['\"]([^'\"]+)['\"]", action.thought, re.IGNORECASE) or re.search(r"matching\s*['\"]([^'\"]+)['\"]", action.thought, re.IGNORECASE)
                    if m_pat:
                        effective_args["pattern"] = m_pat.group(1)
                    else:
                        effective_args["pattern"] = "*invoice*"
                if tool_name == "mail_search" and not effective_args.get("query"):
                    m_q = re.search(r"(?:search(?:ing)? for|query)\s*['\"]([^'\"]+)['\"]", action.thought, re.IGNORECASE)
                    if m_q:
                        effective_args["query"] = m_q.group(1)
                if tool_name in ("files_read_file", "files_write_file") and not effective_args.get("path") and not effective_args.get("file_path"):
                    m_path = re.search(r"['\"]?([a-zA-Z0-9_\-\./\\]+\.(?:pdf|csv|json|txt|md|html))['\"]?", action.thought, re.IGNORECASE)
                    if m_path:
                        effective_args["path"] = m_path.group(1).strip("'\"")
                if tool_name == "browser_open_url" and not effective_args.get("url"):
                    m_url = re.search(r"(https?://[^\s'\"\)]+)", action.thought)
                    if m_url:
                        effective_args["url"] = m_url.group(1).rstrip(".,;")
                if tool_name == "browser_upload_file":
                    if not effective_args.get("file_path"):
                        effective_args["file_path"] = (
                            effective_args.get("path")
                            or effective_args.get("value")
                            or effective_args.get("text")
                        )
                    if not effective_args.get("file_path"):
                        m_f = re.search(r"['\"]?([a-zA-Z0-9_\-\./\\]+\.(?:pdf|csv|json|txt))['\"]?", action.thought, re.IGNORECASE)
                        if m_f:
                            effective_args["file_path"] = m_f.group(1).strip("'\"")

                # Check Idempotency for backend write tools (avoid caching interactive browser UI clicks/typing)
                is_write = (
                    risk_tier in {RiskTier.IRREVERSIBLE_WRITE, RiskTier.EXTERNAL}
                    or (risk_tier == RiskTier.REVERSIBLE_WRITE and not tool_name.startswith("browser_"))
                )
                context_hint = ""
                if tool_name.startswith("browser_"):
                    try:
                        from core.tools.browser import get_browser_manager
                        bm = get_browser_manager()
                        if bm._page:
                            context_hint = bm._page.url
                    except Exception:
                        pass
                idemp_key = self.idempotency.compute_key(run_id, tool_name, effective_args, context_hint=context_hint) if is_write else ""

                if is_write and self.idempotency.is_executed(idemp_key):
                    cached_res = self.idempotency.get_cached_result(idemp_key)
                    logger.info(f"[Idempotency Cache Hit] Skipping duplicate write for {tool_name} (key={idemp_key[:8]}...)")
                    tool_res = cached_res or ToolResult.success(data={"idempotency_cached": True})
                    duration_ms = 0.0
                else:
                    start_t = time.monotonic()
                    tool_res = await self.tools.execute(tool_name, effective_args)

                    # Fault recovery: Transient 500 retry
                    if not tool_res.ok and ("500" in str(tool_res.error) or "Internal Server Error" in str(tool_res.error)):
                        logger.warning(f"Transient error encountered on tool '{tool_name}'. Retrying with backoff...")
                        await asyncio.sleep(1.0)
                        tool_res = await self.tools.execute(tool_name, effective_args)

                    duration_ms = (time.monotonic() - start_t) * 1000.0

                    if is_write and tool_res.ok:
                        self.idempotency.record_execution(idemp_key, run_id, tool_name, effective_args, tool_res)

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

            # Update working memory & record learned facts to proposed memory
            if reflection.facts_learned:
                working_memory.update(reflection.facts_learned)
                for k, v in reflection.facts_learned.items():
                    if k not in {"session_expired", "auth_required", "active_discrepancies", "options", "pending_question"}:
                        self.proposed_memory.record_fact(
                            run_id=run_id,
                            step_index=step_index,
                            category="learned_fact",
                            key=k,
                            value=str(v),
                        )

            # Fault recovery: Session Expiry Mid-Task
            if reflection.facts_learned.get("session_expired"):
                logger.warning(f"Handling detected session expiry at step {step_index}")
                target_sys = "portal" if "/portal" in obs_summary else ("tickets" if "/tickets" in obs_summary else "erp")
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
                policy_decision=policy_decision.effect.value,
                policy_reason=policy_decision.reason,
            )
            self._record_and_notify_step(run_id, step_record)

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
