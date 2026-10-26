# CentrAlign AI — Current Implementation Status

**Date:** 2026-10-26  
**Active LLM:** Google Gemini (`gemini-3.5-flash-lite` via direct REST v1beta API)  
**Environment:** Single-process multi-system mock environment (`http://127.0.0.1:8001`)  
**Commit Milestone:** Real LLM Autonomy Transition (Replacing Simulated Autonomy)

---

## 1. What Works with the Real LLM

### Pure Autonomy & Real Tool Calling
- **Zero FakeLLM in Production/Demos:** `core/scenarios.py` and all canned response scripts have been permanently deleted. `FakeLLM` is strictly restricted to unit-testing plumbing within `tests/` (`get_llm_client()` raises a `RuntimeError` if invoked without a configured LLM key).
- **Constrained JSON Decoding & Schema Alignment:** In `core/llm/client.py`, `pydantic_to_gemini_schema` formats Pydantic models to Gemini OpenAPI 3.0 specification. Tool calling for `AgentAction` enforces `required: ["thought", "tool", "args", "is_finish"]` and populates all 23 tool argument parameters so Gemini passes arguments directly into tool execution envelopes without dropping keys.
- **Defensive Parameter Recovery:** `AgentExecutor` includes regex fallbacks to recover parameters from `action.thought` (e.g. email IDs, file paths, URLs, query keywords) if an argument is mentioned in reasoning, ensuring the agent never loops on schema omissions.

### Full End-to-End Invoice Flow Execution
- **Happy Path Live Execution:** The agent autonomously executes the full lifecycle:
  1. Searches vendor portal and fileshare for latest invoices.
  2. Resolves portal credentials dynamically via `CredentialVault` (`company/vault.yaml`).
  3. Downloads invoice PDF (`INV-ACME-106.pdf`) to `artifacts/downloads/`.
  4. Reads and extracts invoice details: vendor `Acme Corp Ltd`, amount `EUR 91,015.34`, due date `2026-10-28`.
  5. Navigates to Acme ERP, authenticates via vault credentials (`admin` / `erp_pass123`).
  6. Performs duplicate check against existing bills register.
  7. Navigates to `/erp/bills/new`, fills in all fields, uploads the source PDF, and submits the bill.
  8. Confirms bill creation (`#19 Acme Corp Ltd INV-ACME-106 91015.34 EUR 2026-10-28 INV-ACME-106.pdf entered`).

### Task-Agnostic Independent Verification (Hard Rule #4)
- **Zero Agent Self-Trust:** `IndependentVerifier` does not trust agent summaries.
- **Dynamic Check Compilation:** Given the goal and success criteria, an LLM call compiles checks into real SQLite queries (`env/company.db`), disk file inspections, and document cross-matching.
- **Discrepancy Catching:** Cross-checks ERP bill values against the raw extracted text of the source document on disk.
- In live happy path tests, independent verification returns `PASS` with complete evidence references.

### Dynamic Continuous Learning & Generic Lessons
- **LLM Lesson Extraction:** After run completion, an LLM call inspects the step execution history to distill generic operational lessons (e.g. auth recovery, UI drift, date format constraints) and stores them in SQLite with run provenance.
- **Empirical Proof Across Consecutive Real Runs:**
  - **Run 1 (`run_d46ae248a0b1`):** Encountered credential exploration on ERP login. After completion, the LLM autonomously extracted and recorded:
    - `trigger_pattern`: `"acme erp login invalid username or password"`
    - `learned_solution`: `"Always check company/vault.yaml for valid ERP credentials (username 'admin', password 'erp_pass123') rather than relying on default guesses."`
    - `confidence`: `0.95`
  - **Run 2 (`run_33f77ed1ed39`):** Retrieved the stored lesson at context assembly time. Directly navigated to SupplierConnect and ERP using vault credentials with zero credential guessing, completing the entire workflow in 99.91s with 100% successful steps.

### Policy Engine & Generic Egress Policy
- Hardcoded blocklists replaced with declarative domain egress policies in `company/policies.yaml`.
- External email recipients are checked against `allowed_domains` and `denied_domains`, triggering `REQUIRE_APPROVAL` or `DENY` dynamically.

### Codebase Cleanliness & Strict Typing
- **String Sanitization:** Zero vendor/person strings (`acme`, `globex`, `priya`, `sneha`, `supplier@acme.com`, `INV-ACME-*`) remain in `core/`.
- **Linter & Type Checker:** `ruff check .` passes with **0 errors**. `mypy core` passes with **0 errors** across 42 source files.

---

## 2. What Is Still Failing / In-Progress

1. **Free Tier Rate Limit (15 RPM Ceiling):**
   - On Google Gemini Free Tier, `gemini-3.5-flash-lite` enforces a limit of 15 requests per minute.
   - Long multi-step tasks that execute in rapid succession (< 4s per step) can trigger transient HTTP 429 `ResourceExhausted` with `retryDelay: 59s`.
   - *Mitigation implemented:* `GeminiLLMClient` now parses `retryDelay` and waits out quota resets with exponential backoff (up to 6 retries), but running larger batches requires pacing.

2. **Automated Evaluation Matrix (`make eval`):**
   - The eval runner script has been refactored to use real LLM client calls instead of `FakeLLM`.
   - The full $N$-run benchmark matrix across all 4 scenarios needs to be executed against the live Gemini model to produce the final `evals/report.json` and `evals/REPORT.md`.

---

## 3. Exact Next Steps

1. **Commit and Push:**
   - Commit all current modifications with a clean descriptive git message.
   - Verify working tree is clean.

2. **Execute Full Evaluation Harness (`make eval`):**
   - Run `python -m evals.runner` with real Gemini LLM.
   - Collect metrics across all trials: verified success rate, false-completion rate, recovery rate, step counts, token usage, cost in USD, and wall clock latency.
   - Update `evals/REPORT.md` and `evals/report.json` with honest, un-doctored results.

3. **Execute Full Demo Suite (`make demo`):**
   - Run `python -m scripts.demo` across the 4 Intern scenarios and 5 Founding capability showcases with real LLM.

4. **Tag Release:**
   - Create and push tag `founding-v1`.
