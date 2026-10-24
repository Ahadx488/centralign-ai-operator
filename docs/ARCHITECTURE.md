# CentrAlign AI Architecture

CentrAlign AI is an autonomous agent architecture with two entry points designed around a shared core loop:
1. **Autonomous AI Task Worker** (`centralign-ai-worker`, tag `intern-v1`)
2. **AI Operator** (`centralign-ai-operator`, tag `founding-v1`)

---

## 1. The Autonomous Loop

The core execution engine executes a continuous cycle:
```
Goal ──► Understand ──► Plan ──► [ Select Tool ──► Policy Check ──► Execute ──► Observe ──► Reflect/Adapt ] × N ──► Verify ──► Report
```

```mermaid
flowchart TD
    UserReq["User Request"] --> Intake["Goal Interpreter / Clarifier"]
    Intake --> RunMgr["Run Manager (Durable State)"]
    RunMgr --> Plan["Planner (Living Plan)"]
    
    subgraph ExecutionLoop ["Agent Execution Loop"]
        Plan --> SelectTool["LLM Tool Selection"]
        SelectTool --> Policy["Policy & Risk Engine"]
        Policy -->|Approved / Safe| Exec["Tool Execution (Browser, Files, API)"]
        Policy -->|Requires Approval| ApprovalQueue["Approvals Inbox (Human-in-the-Loop)"]
        ApprovalQueue -->|Approved| Exec
        ApprovalQueue -->|Rejected| Adapt["Reflector & Adapt"]
        Exec --> Obs["Observation Normalizer"]
        Obs --> Memory["Memory (Working, Run, Company)"]
        Memory --> Adapt
        Adapt -->|Next Step| SelectTool
        Adapt -->|Goal Met| Verifier["Independent Verifier"]
    end
    
    Verifier -->|Pass| Report["Evidence Bundle & Report"]
    Verifier -->|Fail (Bounded Retry)| Adapt
    Verifier -->|Unverifiable / Escalate| Escalation["Escalation to User"]
```

---

## 2. Component Taxonomy

### A. Intake & Goal Interpreter
- Transforms ambiguous or terse natural language requests into structured `Goal` schemas:
  - `outcome`: Target business state.
  - `success_criteria`: Concrete verifiable statements (e.g. "Bill exists in ERP with vendor X, amount A").
  - `constraints`: Allowed domains, spending limits, timing constraints.
  - `unknowns`: Blockers vs non-blockers. Clarification is requested **only** when missing data cannot be resolved via company memory or exploration.

### B. Dynamic Planner
- Produces an intentional plan (sequence of high-level intents, not brittle hardcoded steps).
- Living plan updated dynamically in working memory.
- If observations contradict expectations (e.g. invoice not in portal), re-planning is triggered automatically.

### C. Tool Registry & Uniform Envelope
- Every tool exposes:
  - `name`: Unique identifier.
  - `description`: LLM instruction on how and when to use.
  - `parameters`: JSON schema.
  - `risk_tier`: `READ`, `REVERSIBLE_WRITE`, `IRREVERSIBLE_WRITE`, `EXTERNAL`.
  - `execute()`: Uniform return envelope `{ok: bool, data: Any, error: Optional[str], evidence_refs: List[str]}`.
- Adding a tool requires registration only, zero core modifications.

### D. Multi-Tier Memory Layer
- **Working Memory**: In-flight state, accumulated facts, intermediate values (in-run).
- **Run State**: Complete state-machine transition history, steps, and audit trail.
- **Company Memory** (`company/`): Domain knowledge as data (systems, SOP procedures, contacts, policies).
- **Learned Lessons**: Distilled feedback from outcomes and corrections to prevent repeating past mistakes.

### E. Policy & Human-in-the-Loop Engine
- Evaluates risk of each action against `company/policies.yaml`.
- Enforces runtime permission boundaries (domain allowlists, file-system isolation).
- Pauses run when actions exceed risk thresholds (e.g., invoices > ₹50,000 or deletions) until approved.

### F. Independent Verifier
- **Crucial Rule**: The verifier never trusts the agent's subjective claim.
- Queries ground-truth system state (database records, files on disk, live HTTP responses) and verifies against the source document.
- Returns `PASS`, `FAIL`, or `UNVERIFIABLE` with cryptographic/raw evidence references.

### G. Observation Normalizer & Context Manager
- Sanitizes large outputs (e.g. raw HTML, huge PDFs) into structured summaries stored in the artifact store.
- Keeps LLM context window concise, focused, and token-efficient.
