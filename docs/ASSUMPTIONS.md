# Assumptions

Key operational and architectural assumptions guiding CentrAlign AI:

1. **Authentication in Mock Environment**:
   - Systems in the mock company environment use standard credential pairs (e.g., `admin / password` or role-based user accounts) managed through an encrypted local credential vault or company configuration files.
   - Session cookies / JWTs can expire periodically to test automatic re-authentication capabilities.

2. **Network & System Topology**:
   - The unified mock company environment runs on a single local process: `http://localhost:8001`
     - Vendor Portal: `http://localhost:8001/portal`
     - Acme ERP: `http://localhost:8001/erp`
     - Mailbox: `http://localhost:8001/mail`
     - Shared Files: `http://localhost:8001/files`
     - Ticketing & Service Desk: `http://localhost:8001/tickets`
     - Agent Dashboard UI: `http://localhost:8001/ui`
     - Ground-Truth API: `http://localhost:8001/__truth`
   - External internet connectivity is unnecessary for executing tasks in the mock environment; all communication stays within sandbox or local mock hosts.

3. **Ground-Truth Separation**:
   - Independent verifiers and eval harnesses have direct read access to SQLite databases and file systems via `/__truth` endpoints or direct DB queries.
   - The agent is **strictly barred** from accessing ground-truth endpoints or verifying its own work through privileged backdoors.

4. **Task-Specific Knowledge Isolation**:
   - The core agent loop (`core/agent/`) contains **zero** domain-specific heuristics, vendor names, or invoice parsing rules.
   - All domain knowledge is derived at runtime from `company/` markdown procedures and YAML configuration.
