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

---

## 3. Operator Interfaces & Delivery UX

### A. Terminal CLI (`core/cli.py`)
- Invocation: `python -m core.cli "<task>" --scenario <1-4>`
- Real-time step-by-step formatted audit stream: Thought $\to$ Tool $\to$ Observation $\to$ Verification.
- Interactive terminal prompts when run pauses in `WAITING_USER` (clarification) or `WAITING_APPROVAL` (policy check).
- Outputs final summary scorecard and link to `evidence_bundle.md`.

### B. Web Dashboard (`ui/` mounted on `/ui`)
- Built with FastAPI + Jinja2 + HTMX for instantaneous startup and zero npm/build overhead.
- Features:
  - **Task Launcher Bar:** Natural language prompt input + quick-launch buttons for core demo scenarios.
  - **Live Run Timeline:** Polled every 1s via HTMX / vanilla JS showing state transitions, living plan intents, and working memory facts.
  - **Interactive Human-in-the-Loop Cards:** Clickable clarification choices or Approve/Reject buttons that resume the paused run in place.
  - **Verification & Evidence Viewer:** Ground-truth check results table, field comparison grid (source PDF vs. ERP values), and direct markdown evidence viewing.

---

## 4. Founding Engineer Subsystems (`founding-v1`)

```mermaid
flowchart LR
    JobQueue["Durable Job Queue (SQLite)"] --> Worker["JobWorker (Heartbeats)"]
    Worker --> Exec["AgentExecutor"]
    Exec --> Idemp["IdempotencyStore (Context Hash)"]
    Exec --> Policy["PolicyEngine & ApprovalsInbox"]
    Exec --> Lessons["LessonStore (FTS Lessons)"]
    Exec --> Verifier["Independent Verifier"]
```

### A. Durable Job Queue & Crash Recovery (`core/jobs/`)
- **Queue Engine (`core/jobs/queue.py`):** Backed by SQLite table `jobs` supporting statuses `QUEUED`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, periodic worker heartbeats, cron/scheduled triggers, and automatic worker failure retries.
- **Heartbeat & Resumption (`core/jobs/worker.py`):** `JobWorker` maintains active heartbeats; orphaned tasks whose heartbeat times out are reclaimed via `reclaim_orphaned()`. Starting a worker runs `resume_interrupted_run(run_id)`, resuming execution from the exact last persisted SQLite step.

### B. Write-Action Idempotency Engine (`core/jobs/idempotency.py`)
- Every write-tier tool invocation computes an idempotency key:
  $$\text{Key} = \text{MD5}(\text{run\_id} \,\|\, \text{tool\_name} \,\|\, \text{args\_json} \,\|\, \text{context\_hint})$$
- Where `context_hint` captures the active URL or entity ID (preventing false collisions across different entities or pages).
- If a crashed worker resumes and attempts to repeat a previously completed write step, `IdempotencyStore` immediately returns the cached result without repeating the external mutation.

### C. Approvals Inbox (`core/policy/approvals.py` & `/ui/approvals`)
- Durable human-in-the-loop approvals table recording pending actions, tool arguments, risk tier, policy rule triggered, and auto-escalation timeouts.
- Human operators can `Approve`, `Reject`, or `Edit Args & Approve` directly from the HTMX dashboard or API.

### D. Continuous Learning & Lessons Store (`core/memory/lessons.py`)
- Whenever a run encounters a recoverable error (such as a missing source document or renamed UI element) or receives human feedback, `LessonStore` captures a structured `Lesson` in SQLite.
- Future tasks query `LessonStore.retrieve_lessons(query)` at `Plan`/`Execute` time, pre-emptively avoiding obsolete paths and achieving measurable step reductions.
