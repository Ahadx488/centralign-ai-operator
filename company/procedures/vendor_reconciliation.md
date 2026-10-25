# Standard Operating Procedure: Vendor Statement CSV vs ERP Reconciliation

## Purpose
This SOP guides the cross-reconciliation of monthly vendor billing statements from the file share against recorded bills in Acme ERP to identify unpaid, missing, or mismatched records.

## Relevant Systems
- **File Share**: Centralized Shared Drive (`./env/fileshare/data`)
- **ERP**: Acme ERP (`http://localhost:8001/erp/bills`)

## Execution Steps
1. **Locate & Ingest Vendor Statement CSV**:
   - Use `files_search_files` or `files_read_file` to locate the target vendor statement in `./env/fileshare/data/` (e.g., `AcmeCorp_Oct2026_statement.csv`).
   - Parse each invoice row: `Date`, `Invoice No`, `Vendor`, `Amount`, `Currency`, `Status`.

2. **Retrieve ERP Ledger Records**:
   - Navigate to `/erp/bills` in Acme ERP or inspect current registered bills.
   - For each statement entry, search for a matching bill by vendor name and invoice reference.

3. **Cross-Check Discrepancies**:
   - Compare amount, currency, and recorded status.
   - Categorize entries into:
     - **MATCHED**: Invoice exists in ERP with identical amount and status.
     - **AMOUNT_MISMATCH**: Invoice exists in ERP but recorded amount differs from the statement.
     - **MISSING_IN_ERP**: Invoice is present on vendor statement but absent from ERP.

4. **Generate Structured Reconciliation Output**:
   - Conclude task with a structured summary listing all mismatched invoices, observed variance amounts, and missing references.
