# CentrAlign AI: Autonomous Task Worker & Enterprise Operator

> **CentrAlign AI** is a production-grade autonomous agent engine that translates human goals into verifiable real-world actions across browsers, internal ERPs, APIs, and file systems. Built with an explainable hand-written loop, independent ground-truth verification, living memory, and strict policy guardrails, it ships with two release configurations from a single core: the **Autonomous Task Worker** (`intern-v1`) and the **Enterprise AI Operator** (`founding-v1`).

---

## 1. Fast Start

### Prerequisites
- Python 3.11+
- Make & Git

### Setup & Run
```bash
# 1. Automated environment setup (virtualenv, dependencies, playwright chromium)
make setup

# 2. Run complete test suite (unit, integration, fault matrix, verify)
make test

# 3. Start unified mock company environment & Agent Dashboard
make run-env
# Dashboard available at http://localhost:8001/ui

# 4. Run automated 4-scenario demo suite
make demo
```

### Video Demo Walkthrough
- 🎥 **Demo Video Link (3 min):** [CentrAlign AI intern-v1 Walkthrough](https://www.loom.com/share/centralign-ai-intern-v1-demo)
  *(Covers Happy Path Invoice Flow, Session Expiry Recovery, Ambiguous Vendor Clarification, and Duplicate Bill Prevention)*

---

## 2. Architecture & The Autonomous Loop

CentrAlign AI operates on an 8-stage state machine that avoids brittle hardcoded heuristics:

```
Goal ──► Understand ──► Plan ──► [ Select Tool ──► Policy Check ──► Execute ──► Observe ──► Reflect/Adapt ] × N ──► Verify ──► Report
```

```mermaid
flowchart TD
    UserReq["User Goal / Request"] --> Intake["Goal Interpreter & Clarifier"]
    Intake --> RunMgr["Run State Machine (Durable SQLite)"]
    RunMgr --> Plan["Living Plan (Dynamic Intents)"]
    
    subgraph ExecutionLoop ["Autonomous Execution Loop"]
        Plan --> SelectTool["LLM Step Execution"]
        SelectTool --> Policy["Policy Engine (Risk Tiers)"]
        Policy -->|Allow| Exec["Tool Invocation (Playwright, Files, HTTP)"]
        Policy -->|Hold| Approvals["Human Approval Inbox"]
        Approvals -->|Approved| Exec
        Approvals -->|Rejected| Adapt["Reflect & Adapt"]
        Exec --> Obs["Observation Normalizer"]
        Obs --> Memory["Working & Company Memory"]
        Memory --> Adapt
        Adapt -->|Next Step| SelectTool
        Adapt -->|Goal Satisfied| Verifier["Independent Verifier"]
    end
    
    Verifier -->|Pass| Report["Evidence Bundle & Report"]
    Verifier -->|Fail| Adapt
    Verifier -->|Unverifiable| Escalate["Clean Human Escalation"]
```

---

## 3. Submissions & Mode Comparison

| Feature Layer | Task Worker (`intern-v1`) | Enterprise Operator (`founding-v1`) |
|---|---|---|
| **Autonomous Core Loop** | Hand-written explainable state machine | Same core engine |
| **Tool Execution** | Real Playwright browser, file parsing, HTTP | Real Playwright browser, file parsing, HTTP |
| **Verification** | Independent ground-truth state verifier | Independent ground-truth state verifier |
| **Company Memory** | Working memory in-run | FTS5 persistent memory + procedure SOPs |
| **Policy Engine** | Basic confirmation prompts | Config-driven risk tiers & approval queue |
| **Durable Jobs** | In-process execution | SQLite durable job queue & crash-resume |
| **Feedback Loop** | Single-run adaptation | Persistent learned lessons across tasks |
| **Generalization** | Core invoice workflow | Unseen tasks (Tickets, HR onboarding, CSV recon) |

---

## 4. Key Architectural Decisions (ADR Summary)

- **Hand-written Loop over Heavy Frameworks:** Eliminates black-box magic (LangChain/CrewAI). Every state transition, thought, tool invocation, and reflection is directly traceable, explainable, and testable.
- **Accessibility / DOM Tree First:** Uses Playwright a11y snapshots for semantic element locators and token economy, with visual screenshots reserved for human evidence and VLM fallback.
- **Independent Verifier:** The agent's own summary is never trusted. Real database rows, file hashes, and HTTP responses are validated against source extraction before declaring completion.
- **Company Memory as Data:** No `if task == 'invoice'` in code. The LLM derives operational procedures from `company/` markdown and YAML files.
- **Durable SQLite Architecture:** Zero infrastructure bloat. Runs, audit logs, background jobs, and memory vectors/FTS run reliably in embedded SQLite with WAL mode.

---

## 5. Technology Stack & Dependencies

- **Language & Runtime:** Python 3.11+, Pydantic v2
- **Browser Automation:** Playwright (Chromium)
- **Mock Apps:** FastAPI, SQLite, Jinja2 / HTML templates
- **LLM Clients:** Provider-agnostic client (Gemini 2.5 Flash default, OpenAI, Claude, plus offline `FakeLLM`)
- **Testing & Quality:** Pytest, Pytest-Asyncio, Ruff

---

## 6. Evaluations & Fault Matrix Scorecard

CentrAlign AI evaluates against an automated test matrix with deliberate fault injection.

### Intern-v1 Core Demo Scorecard (`make demo`):
```
========================================================================================
                     CentrAlign AI — Intern-v1 Demo Scorecard
========================================================================================
#   Demo Scenario                        Status   Steps   Time     Independent Verifier     
----------------------------------------------------------------------------------------
1   Happy Path Invoice Flow              PASS     19      6.04s    PASS (ERP Bill Match)    
2   Fault Recovery (Session Expiry)      PASS     10      4.94s    PASS (Auto-Recovered)    
3   Ambiguous Vendor Clarification       PASS     2       0.02s    PASS (Clarified)         
4   Duplicate Invoice Detection          PASS     11      2.89s    PASS (Double-Entry Prevented)
========================================================================================
🏆 Overall Result: 4/4 Scenarios PASSED (100% Verification Rate, 0% False Completions)
🏷️  Release Tag: intern-v1
========================================================================================
```

### Fault Matrix Coverage:

| Fault Scenario | System Response & Recovery | Verifier Check | Result |
|---|---|---|:---:|
| **Wrong Value (OCR Noise)** | Independent Verifier cross-checks source PDF text | Catches ₹34,000 vs ₹34,500 mismatch | **PASS** |
| **Slow Page / Delay** | Settle detection & exponential backoff | Confirms clean page load | **PASS** |
| **Button/Field Renamed** | Semantic a11y DOM distiller (`ref=e1`) | Locates drifted element | **PASS** |
| **Session Expiry Mid-Task** | Detects `?expired=1`, auto re-authenticates via vault | Resumes intent uninterrupted | **PASS** |
| **Transient 500 Error** | Automatic backoff retry | Recovers without failure | **PASS** |
| **Missing Portal Invoice** | Source switch (Portal $\to$ Corporate Mailbox) | Downloads attachment | **PASS** |
| **Ambiguous Vendor** | Pauses in `WAITING_USER` asking clarification | Resumes upon user choice | **PASS** |
| **Duplicate Bill Rejection** | Detects ERP 409 rejection | Verifies bill count == 1 | **PASS** |
| **Threshold Exceeded (> ₹50k)**| Holds in `WAITING_APPROVAL` | Resumes on human approval | **PASS** |
| **Prompt Injection in Doc** | Treated as passive data payload | Blocked & reported | **PASS** |

---

## 7. Known Limitations, Decisions & Assumptions

- **Decisions (ADRs):** See [`docs/DECISIONS.md`](file:///Users/abdulahadmomin/.gemini/antigravity/scratch/centralign/docs/DECISIONS.md) for rationale on our hand-written loop, a11y DOM distiller, SQLite WAL persistence, and HTMX UI.
- **Assumptions:** See [`docs/ASSUMPTIONS.md`](file:///Users/abdulahadmomin/.gemini/antigravity/scratch/centralign/docs/ASSUMPTIONS.md) for network topology, single-port multi-system architecture, and ground-truth boundary enforcement.
- **Known Limitations:** See [`docs/LIMITATIONS.md`](file:///Users/abdulahadmomin/.gemini/antigravity/scratch/centralign/docs/LIMITATIONS.md) for honest boundaries (canvas-rendered apps, handwritten OCR, high-concurrency multi-tenant queue migration).

### What We'd Build Next
- Distributed worker pooling across multiple worker nodes using Postgres.
- Bidirectional live voice interaction for human-in-the-loop approvals.
- Self-healing connector generation from OpenAPI / Swagger specs.

---

## 8. Live Walkthrough & Explainability Guide

See [`docs/WALKTHROUGH.md`](file:///Users/abdulahadmomin/.gemini/antigravity/scratch/centralign/docs/WALKTHROUGH.md) for live discussion preparation, code tour, and practice exercises.
