from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from core.agent.executor import AgentExecutor
from core.llm.fake import FakeLLM
from core.llm.models import LLMResponse
from core.memory.company import CompanyMemoryIndex
from core.memory.proposed import ProposedMemoryStore
from core.policy.engine import PolicyEngine
from core.policy.models import PolicyEffect, RunCapabilities
from core.runs.manager import RunManager
from core.runs.models import RunStatus
from core.tools.registry import RiskTier, ToolRegistry
from env.db import init_db
from env.seed.generator import seed_company_data


@pytest.fixture(autouse=True)
def reset_database() -> None:
    init_db()
    seed_company_data()


def test_company_memory_fts5_indexing_and_retrieval(tmp_path: Path) -> None:
    """Test that SQLite FTS5 indexes all SOPs, systems, policies, contacts, and retrieves with BM25."""
    index = CompanyMemoryIndex(company_dir="company")

    # 1. Query for invoice processing
    results = index.retrieve("invoice processing sop accounts payable erp", top_k=3)
    assert len(results) > 0
    doc_ids = [r.doc_id for r in results]
    assert any("invoice_processing" in d or "erp" in d for d in doc_ids)

    # 2. Query for ticket triage
    results = index.retrieve("service desk tickets close resolved manager", top_k=3)
    assert len(results) > 0
    assert any("ticket" in r.doc_id for r in results)

    # 3. Query for contacts
    results = index.retrieve("Arjun Verma finance manager approval", top_k=3)
    assert len(results) > 0
    assert any("contact" in r.doc_id for r in results)

    # 4. Context formatting
    context = index.format_context(results)
    assert "## Retrieved Company Memory" in context
    assert len(context) > 100


def test_proposed_memory_provenance_and_promotion(tmp_path: Path) -> None:
    """Test recording facts with run/step provenance and promoting them to company memory."""
    db_file = tmp_path / "test_memory.db"
    store = ProposedMemoryStore(db_path=db_file)

    # 1. Record proposed fact
    fact = store.record_fact(
        run_id="run_abc123",
        step_index=4,
        category="vendor_location",
        key="Acme statements directory",
        value="./env/fileshare/data/AcmeCorp_Oct2026_statement.csv",
        confidence=0.95,
    )
    assert fact.id is not None
    assert fact.status == "PROPOSED"
    assert fact.run_id == "run_abc123"
    assert fact.step_index == 4

    # 2. Retrieve proposed facts
    proposed_list = store.get_facts(run_id="run_abc123", status="PROPOSED")
    assert len(proposed_list) == 1
    assert proposed_list[0].key == "Acme statements directory"

    # 3. Promote fact
    promoted = store.promote_fact(fact.id)
    assert promoted is True

    # 4. Confirm status changed to PROMOTED
    updated_fact = store.get_fact(fact.id)
    assert updated_fact is not None
    assert updated_fact.status == "PROMOTED"
    assert updated_fact.promoted_at is not None

    # 5. Check promoted facts for company memory re-indexing
    promoted_facts = store.get_promoted_facts()
    assert len(promoted_facts) == 1
    assert promoted_facts[0]["key"] == "Acme statements directory"


def test_policy_engine_enforces_rules_and_restrictions() -> None:
    """Test policy engine rule evaluations (deletion prohibition, file restriction, external email)."""
    engine = PolicyEngine(policy_file="company/policies.yaml")

    # 1. Deletion is DENIED
    decision = engine.evaluate(
        tool_name="delete_bill",
        tool_args={"invoice_number": "INV-101"},
        risk_tier=RiskTier.IRREVERSIBLE_WRITE,
    )
    assert decision.effect == PolicyEffect.DENY
    assert decision.rule_id == "prohibit_record_deletion"

    decision_purge = engine.evaluate(
        tool_name="purge_logs",
        tool_args={},
        risk_tier=RiskTier.IRREVERSIBLE_WRITE,
    )
    assert decision_purge.effect == PolicyEffect.DENY

    # 2. File access outside fileshare is DENIED
    decision_file_bad = engine.evaluate(
        tool_name="files_read_file",
        tool_args={"path": "/etc/passwd"},
        risk_tier=RiskTier.READ,
    )
    assert decision_file_bad.effect == PolicyEffect.DENY
    assert decision_file_bad.rule_id == "file_access_restriction"

    # Permitted file in fileshare is ALLOWED
    decision_file_ok = engine.evaluate(
        tool_name="files_read_file",
        tool_args={"path": "./env/fileshare/data/AcmeCorp_Oct2026_statement.csv"},
        risk_tier=RiskTier.READ,
    )
    assert decision_file_ok.effect == PolicyEffect.ALLOW

    # 3. External unlisted email requires approval
    decision_ext_email = engine.evaluate(
        tool_name="mail_send",
        tool_args={"recipient": "consultant@external-partner.org", "subject": "Data", "body": "Status"},
        risk_tier=RiskTier.EXTERNAL,
    )
    assert decision_ext_email.effect == PolicyEffect.REQUIRE_APPROVAL
    assert decision_ext_email.rule_id == "external_communication_approval"
    assert decision_ext_email.approver_role == "finance_manager"

    # Egress denied domain is DENIED
    decision_denied_domain = engine.evaluate(
        tool_name="mail_send",
        tool_args={"recipient": "attacker@evil.com", "subject": "Data", "body": "Leak"},
        risk_tier=RiskTier.EXTERNAL,
    )
    assert decision_denied_domain.effect == PolicyEffect.DENY
    assert decision_denied_domain.rule_id == "prompt_injection_exfiltration_blocked"

    # Internal email to company.com is ALLOWED
    decision_int_email = engine.evaluate(
        tool_name="mail_send",
        tool_args={"recipient": "sneha.patel@company.com", "subject": "Update", "body": "Status"},
        risk_tier=RiskTier.REVERSIBLE_WRITE,
    )
    assert decision_int_email.effect == PolicyEffect.ALLOW


def test_dynamic_spending_threshold_change_with_zero_code_changes(tmp_path: Path) -> None:
    """
    Prove that modifying company/policies.yaml on disk dynamically changes the same action
    from ALLOW to REQUIRE_APPROVAL with ZERO code changes.
    """
    policy_file = tmp_path / "policies.yaml"
    policy_data = {
        "version": "1.0",
        "rules": [
            {
                "id": "spending_threshold_approval",
                "description": "Bills exceeding threshold require approval.",
                "condition": "action.type == 'create_bill' and action.amount > 50000",
                "effect": "REQUIRE_APPROVAL",
                "approver_role": "finance_manager",
            }
        ],
    }
    policy_file.write_text(yaml.safe_dump(policy_data), encoding="utf-8")

    engine = PolicyEngine(policy_file=policy_file)
    assert engine.get_spending_threshold() == 50000.0

    # Bill amount 34,500 is BELOW 50,000 threshold -> ALLOW
    decision_1 = engine.evaluate(
        tool_name="browser_type_text",
        tool_args={"target": "#amount", "text": "34500.00"},
        risk_tier=RiskTier.REVERSIBLE_WRITE,
    )
    assert decision_1.effect == PolicyEffect.ALLOW

    # Now dynamically LOWER the threshold to 10,000 in policies.yaml without ANY code changes!
    policy_data["rules"][0]["condition"] = "action.type == 'create_bill' and action.amount > 10000"
    policy_file.write_text(yaml.safe_dump(policy_data), encoding="utf-8")
    os.utime(policy_file, (os.path.getatime(policy_file) + 2, os.path.getmtime(policy_file) + 2))

    assert engine.get_spending_threshold() == 10000.0

    # The exact same action now triggers REQUIRE_APPROVAL!
    decision_2 = engine.evaluate(
        tool_name="browser_type_text",
        tool_args={"target": "#amount", "text": "34500.00"},
        risk_tier=RiskTier.REVERSIBLE_WRITE,
    )
    assert decision_2.effect == PolicyEffect.REQUIRE_APPROVAL
    assert decision_2.rule_id == "spending_threshold_approval"
    assert "exceeds auto-approval threshold" in decision_2.reason


def test_runtime_capability_scoping() -> None:
    """Test runtime capability scoping per run (allowed tools, domains, folders)."""
    engine = PolicyEngine(policy_file="company/policies.yaml")

    caps = RunCapabilities(
        allowed_tools=["browser_open_url", "browser_snapshot"],
        allowed_domains=["localhost:8001"],
        allowed_file_folders=["./env/fileshare/data"],
    )

    # 1. Unallowed tool is denied
    decision_tool = engine.evaluate(
        tool_name="browser_click",
        tool_args={"target": "Submit"},
        capabilities=caps,
    )
    assert decision_tool.effect == PolicyEffect.DENY
    assert decision_tool.rule_id == "runtime_tool_scope"

    # 2. Unallowed domain is denied
    decision_domain = engine.evaluate(
        tool_name="browser_open_url",
        tool_args={"url": "http://malicious-external-site.com/steal"},
        capabilities=caps,
    )
    assert decision_domain.effect == PolicyEffect.DENY
    assert decision_domain.rule_id == "runtime_domain_scope"

    # 3. Allowed tool and domain is allowed
    decision_allowed = engine.evaluate(
        tool_name="browser_open_url",
        tool_args={"url": "http://localhost:8001/erp/login"},
        capabilities=caps,
    )
    assert decision_allowed.effect == PolicyEffect.ALLOW


@pytest.mark.asyncio
async def test_agent_executor_pauses_on_policy_threshold_and_resumes(tmp_path: Path) -> None:
    """Test that agent pauses in WAITING_APPROVAL when policy requires approval, and resumes on grant."""
    run_manager = RunManager(db_path=tmp_path / "runs.db", artifacts_dir=tmp_path / "artifacts")
    registry = ToolRegistry()
    from core.tools import register_default_tools
    register_default_tools(registry)

    llm = FakeLLM()
    # 1. Goal
    from core.agent.models import AgentAction, Goal, Intent
    from core.agent.planner import InitialPlanOutput
    llm.queue_response(
        LLMResponse(
            parsed=Goal(outcome="Submit a bill of INR 150,000.00", success_criteria=["Bill submitted"]),
            provider="fake",
        )
    )
    # 2. Plan
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[Intent(id="intent_1", description="Enter high value bill", expected_outcome="Bill entered")]
            ),
            provider="fake",
        )
    )
    # 3. Action that triggers policy threshold > 100,000
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering high value amount",
                tool="browser_type_text",
                args={"target": "#amount", "text": "150000.00"},
            ),
            provider="fake",
        )
    )

    executor = AgentExecutor(
        llm_client=llm,
        run_manager=run_manager,
        registry=registry,
        company_dir="company",
    )

    # Execute run
    run = await executor.run("Submit a bill of INR 150,000.00")

    # Assert paused in WAITING_APPROVAL
    assert run.status == RunStatus.WAITING_APPROVAL
    assert "150,000.00" in run.working_memory.get("approval_reason", "")

    # Queue next action after approval
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Bill approved by finance manager. Finishing task.",
                is_finish=True,
                finish_summary="Bill of 150,000.00 approved and posted.",
            ),
            provider="fake",
        )
    )

    # Resume with approval
    resumed_run = await executor.resume(run.run_id, approval_granted=True)
    assert resumed_run.status in {RunStatus.COMPLETED, RunStatus.RUNNING, RunStatus.VERIFYING}
