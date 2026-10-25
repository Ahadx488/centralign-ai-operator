# CentrAlign AI — Live Walkthrough & Interview Guide

This guide is designed for live code walkthroughs, technical discussions, and rapid codebase navigation.

---

## 1. Where to Change X (Codebase Map)

| To Change... | Edit File(s) | How to Do It |
|---|---|---|
| **Add a new tool** | `core/tools/` (e.g. `slack.py`, `database.py`) | Subclass `BaseTool`, define Pydantic `args_schema` and `risk_tier`, implement `async def _run(**kwargs)`, and register via `tool_registry.register(MyTool())` in `core/tools/__init__.py`. |
| **Change or add a policy rule** | `company/policies.yaml` | Add or modify thresholds (e.g., `approval_threshold: 50000`), blocked domains, or restricted file directories. |
| **Add a company procedure / SOP** | `company/procedures/<name>.md` | Write standard Markdown describing system URLs, form field mappings, and fallback steps. The Goal Interpreter & Planner automatically ingest these. |
| **Modify independent verification** | `core/verify/verifier.py` | Update `IndependentVerifier.verify()` to add state queries against SQLite or source document regex extractors. |
| **Add a new fault injection** | `env/faults/manager.py` & app routers | Add boolean field to `FaultState`, implement intercept in the FastAPI middleware/router, and toggle via `/__truth/faults`. |
| **Tune the core loop / retries** | `core/agent/executor.py` | Adjust `max_steps`, `max_retries_per_intent`, or loop detection threshold in `AgentExecutor._execute_loop()`. |

---

## 2. Core Technical Questions & Answers

### Q1: Why did the agent choose that specific tool?
**Answer:** The LLM receives the living plan's current milestone intent (e.g. `"Download latest invoice for Acme Corp Ltd"`), the list of registered tools with their descriptions and JSON schemas, and recent observations. It selects the tool whose description and schema match the current sub-goal. In our hand-written loop, every tool invocation is preceded by a structured thought explaining the rationale, recorded durably in SQLite.

### Q2: How does it detect an infinite loop?
**Answer:** In `core/agent/executor.py` (lines ~430-445), CentrAlign computes an SHA-256 hash of the `(tool_name, tool_args_json, observation_hash)`. If the identical tuple occurs 3 times within recent history without advancing state, loop detection triggers an automatic re-plan with an adaptive recovery intent, breaking out of circular failures.

### Q3: How does the Independent Verifier avoid trusting the agent?
**Answer:** The Verifier (`core/verify/verifier.py`) completely ignores the agent's summary message. Instead, it reads the target invoice document directly from disk using `pypdf`, extracts canonical values (amount, vendor, invoice number, due date), and executes independent SQL queries against `env/company.db` (`erp_bills`). If values do not match within ₹0.01 tolerance, or if the attachment file is missing on disk, verification returns `FAIL` and blocks false completion.

### Q4: How would this architecture scale to hundreds of tenants?
**Answer:** 
1. **Queue Migration:** The embedded SQLite job queue (`RunManager`) swaps cleanly to PostgreSQL + Redis (Celery/BullMQ) with tenant schema isolation.
2. **Stateless Workers:** `AgentExecutor` is already stateless; all run state is read/written to durable storage.
3. **Browser Pool:** Headless Playwright workers run across ephemeral container pools (e.g., Browserless or AWS ECS Fargate) with isolated browser contexts per tenant.

### Q5: What breaks first in production?
**Answer:**
1. **Dynamic CAPTCHAs & Bot Detectors:** Solved via authenticated enterprise sessions / single-sign-on (SSO) cookies or human escalation.
2. **Third-party DOM structural overhauls:** Mitigated by our accessibility distiller (`core/tools/browser.py`) which resolves elements semantically rather than relying on brittle XPath strings.
3. **Degraded physical document scans:** Complex handwriting or low-DPI scans require vision models or specialized OCR preprocessing.

---

## 3. Practice Modifications (Hands-on Tour)

### Practice Modification 1: Add a New Tool (`currency_converter`)
1. Create `core/tools/currency.py`:
   ```python
   from pydantic import BaseModel, Field
   from core.tools.registry import BaseTool, RiskTier, ToolResult

   class CurrencyConvertArgs(BaseModel):
       amount: float = Field(description="Amount to convert")
       from_currency: str = Field(description="Source 3-letter currency code")
       to_currency: str = Field(description="Target 3-letter currency code")

   class CurrencyConvertTool(BaseTool):
       name = "convert_currency"
       description = "Convert an amount between USD, EUR, and INR at fixed demo rates."
       args_schema = CurrencyConvertArgs
       risk_tier = RiskTier.READ

       async def _run(self, **kwargs) -> ToolResult:
           rates = {"USD": 1.0, "EUR": 0.92, "INR": 83.2}
           amt, f_curr, t_curr = kwargs["amount"], kwargs["from_currency"].upper(), kwargs["to_currency"].upper()
           converted = (amt / rates[f_curr]) * rates[t_curr]
           return ToolResult.success(data={"converted_amount": round(converted, 2), "currency": t_curr})
   ```
2. Register in `core/tools/__init__.py`:
   ```python
   registry.register(CurrencyConvertTool())
   ```

### Practice Modification 2: Add a Policy Rule (Block Deletions)
1. In `company/policies.yaml`:
   ```yaml
   invariants:
     - "Never delete bills, invoices, or customer records. Soft-delete or archive only."
   restricted_tools:
     - tool: "database_delete"
       action: "DENY"
       reason: "Permanent deletion violates financial audit compliance."
   ```

### Practice Modification 3: Add a New Fault Injection (`corrupted_pdf`)
1. In `env/faults/manager.py`:
   ```python
   corrupted_pdf: bool = False
   ```
2. In `env/fileshare/router.py`:
   ```python
   if fault_manager.state.corrupted_pdf:
       return Response(content=b"%PDF-corrupted-bytes", media_type="application/pdf")
   ```
3. Test agent recovery: When `read_file` fails, the Reflector catches the unreadable PDF and re-plans to fetch from the Mailbox!
