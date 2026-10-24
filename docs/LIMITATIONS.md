# Limitations

Honest assessment of the current boundaries and trade-offs of CentrAlign AI:

1. **Browser Environment Scope**:
   - The primary browser driver uses Playwright with Chromium in headless/headed mode. It interacts with standard HTML5/DOM and accessibility trees.
   - Canvas-rendered, WebGL, or proprietary desktop OS plugins lacking accessible DOM structures require coordinate-based VLM fallbacks.

2. **Document Types for File Ingestion**:
   - Out-of-the-box support includes PDF (text extraction), CSV, JSON, TXT, Markdown, and standard tabular data.
   - Heavily distorted, handwritten, or scanned paper documents rely on clean OCR pre-processing.

3. **Concurrency and Multi-Tenant Isolation**:
   - Current background queue is backed by SQLite with WAL mode, optimized for single-node durable operations and development/eval setups.
   - High-throughput multi-tenant deployments with hundreds of concurrent workers would migrate the queue backend to PostgreSQL or distributed workers.

4. **Context Window Token Boundaries**:
   - Rolling context compaction and observation summarization prevent context overflow. However, tasks with hundreds of dense steps will rely on retrieved historical memory chunks rather than raw prompt logs.

5. **Sandbox Isolation**:
   - Designed strictly for simulated environments and internal test harnesses. External network actions (e.g., real bank transfers, production ERPs) are bounded by policy guardrails and require human approvals.
