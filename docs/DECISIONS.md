# Architectural Decision Records (ADRs)

This document captures key technical design decisions made in CentrAlign AI, explaining the rationale and alternatives considered.

---

## ADR 001: Hand-Written Loop vs Heavy Agent Frameworks (LangChain / CrewAI / AutoGen)

- **Status:** Accepted
- **Context:** We need complete explainability, deterministic control of state transitions, robust loop detection, and zero framework black-box overhead.
- **Decision:** Build a lightweight, hand-written agent loop using standard Python and Pydantic.
- **Rationale:**
  1. Live walkthrough explainability: Every state transition, thought, tool invocation, and reflection is directly traceable.
  2. Framework independence: Eliminates brittle abstractions and hidden prompts.
  3. Tailored state machine: Exact support for `WAITING_APPROVAL`, `WAITING_USER`, and bounded re-planning.

---

## ADR 002: DOM / Accessibility Tree Snapshot First vs Raw Screenshot-Driven Computer Use

- **Status:** Accepted
- **Context:** Web interaction can be performed via visual coordinates (screenshot VLM) or structural DOM / a11y trees.
- **Decision:** Use Playwright accessibility tree / DOM snapshot with stable semantic references as the primary observation, keeping screenshots as evidence and fallback.
- **Rationale:**
  1. Semantic stability: DOM/a11y trees provide element text, roles, and stable selectors less prone to rendering quirks.
  2. Latency and token economy: Textual snapshots consume significantly fewer tokens than full-resolution images.
  3. Determinism: Target elements can be selected with precise locators.

---

## ADR 003: Independent Verifier Isolated from the Agent Context

- **Status:** Accepted
- **Context:** LLMs frequently produce false-positive completion claims ("hallucinated success").
- **Decision:** The Verifier reads real ground-truth system state (database rows, live endpoints, file system hashes) directly, independent of the agent's textual output.
- **Rationale:**
  1. Eliminates false completions: If the bill does not exist in the ERP SQLite database with matching values and attachment, verification fails.
  2. Bounded recovery: A verification failure forces the agent loop to adapt or cleanly escalate rather than report spurious success.

---

## ADR 004: SQLite for Durable Run State, Memory, and Job Queue

- **Status:** Accepted
- **Context:** We need zero-setup persistence for runs, steps, memory, approvals, audit logs, and background jobs.
- **Decision:** Use SQLite (with WAL mode) across the system instead of external services like PostgreSQL, Redis, or Celery.
- **Rationale:**
  1. Zero external infrastructure dependencies: Runs cleanly out of the box in docker-compose or local shell.
  2. ACID compliance: Run state machine transitions are durable; worker crashes resume from the last persisted step.
  3. Full-Text Search (FTS5): Built-in fast text retrieval for company memory and SOPs without heavy vector database overhead.

---

## ADR 005: Company Procedures and Memory as Data, Not Code

- **Status:** Accepted
- **Context:** Hardcoding task-specific workflows (e.g. `if task == "invoice": ...`) violates core autonomy requirements.
- **Decision:** All business rules, systems, procedures, and credentials live in `company/*.yaml` and `company/procedures/*.md`.
- **Rationale:**
  1. Generalization: The agent discovers *how* to perform tasks from documentation and memory, not Python conditionals.
  2. Reconfigurability: Changing approval thresholds or system endpoints requires only updating data files.

---

## ADR 006: Provider-Agnostic LLM Client with Thin Abstraction & Deterministic FakeLLM

- **Status:** Accepted
- **Context:** Need support for Gemini (default), OpenAI, and Claude, while ensuring unit tests run offline with zero API cost.
- **Decision:** Implement a thin `LLMClient` with structured JSON schema support, retries with exponential backoff, and a full-featured `FakeLLM`.
- **Rationale:**
  1. Decoupled from proprietary SDK updates.
  2. 100% reproducible test suites with simulated transient faults and tool calls.

---

## ADR 007: Single-Process Multi-System Mock Architecture

- **Status:** Accepted
- **Context:** Running five independent mock applications (ERP, Portal, Mailbox, Files, Tickets) across separate TCP ports produces zombie processes, port collisions, and fragile test fixtures.
- **Decision:** Mount all five mock systems under a single FastAPI application using distinct path prefixes (`/portal`, `/erp`, `/mail`, `/files`, `/tickets`, `/__truth`, `/ui`) served by a single Uvicorn server on port 8001.
- **Rationale:**
  1. Zero zombie processes or port contention during local tests and eval suites.
  2. Independent session cookies and isolated CSS/templates preserve genuine multi-system behavior for Playwright.
  3. Single-command setup and instant startup.

---

## ADR 008: Server-Rendered HTMX + Jinja2 Dashboard over Heavy SPA Frameworks

- **Status:** Accepted
- **Context:** The agent operator requires a live timeline, task launcher, and clarification/approval interface.
- **Decision:** Use FastAPI + Jinja2 + HTMX for the Web Dashboard (`/ui`), avoiding React/Node/Webpack build tooling.
- **Rationale:**
  1. Instant startup with zero npm/node installation dependencies.
  2. Real-time reactive updates via HTMX polling (`hx-get="/ui/runs/{id}/live" hx-trigger="every 1s"`) with vanilla JS fallback.
  3. Human-in-the-loop clarification and approval actions execute with direct server-rendered interactions.
