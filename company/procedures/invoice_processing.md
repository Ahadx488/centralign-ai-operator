# Standard Operating Procedure: Accounts Payable Invoice Entry

## Purpose
This SOP guides the processing and ingestion of incoming vendor invoices into the internal Acme ERP.

## Sources for Invoices
1. **SupplierConnect Vendor Portal**:
   - Primary source for monthly statements and invoices from registered vendors.
   - Navigate to Invoices, filter or locate the vendor, and download the latest unpaid invoice PDF.
2. **Corporate Mailbox**:
   - Secondary source if the vendor is not on the portal or if an updated invoice was delivered via email.
3. **Company Shared Drive (`./env/fileshare/data`)**:
   - Scanned invoices and batch drops placed by operations.

## Data Extraction Rules
Extract the following fields from the invoice document:
- **Vendor Name**: Match against approved ERP vendors. If ambiguous (e.g. a request specifies "Acme Corp" which matches both "Acme Corp Ltd" and "Acme Corp Inc"), you MUST call the 'ask_user' tool to ask the user which entity to process before proceeding.
- **Invoice Number**: Alphanumeric reference code.
- **Invoice Date & Due Date**: Standardize to `YYYY-MM-DD`.
- **Total Amount**: Numeric amount.
- **Currency**: Currency code (e.g., INR, USD, EUR).

## Entry into Acme ERP
1. Log in to Acme ERP (`http://localhost:8001/erp/login`) using the username and vault password reference from system documentation.
2. In the Bills register (`/erp/bills`), execute the **MANDATORY DUPLICATE CHECK**:
   - Filter or search the bills list by typing the invoice number into `#erp-search-input` and clicking `#erp-filter-btn`.
   - Inspect the resulting table in the DOM snapshot:
     * If an existing bill with that invoice reference already exists in the table, **STOP**: flag the invoice as duplicate (`is_duplicate: true`), do NOT open the New Bill form or submit a duplicate bill. Conclude the task with duplicate status.
     * If the table displays "No bills found", proceed immediately to step 3.
3. Click "+ New Bill" (`#new-bill-btn`) to navigate to the entry form.
4. Fill in:
   - Vendor Name
   - Invoice Reference
   - Amount & Currency
   - Due Date (standardized to YYYY-MM-DD)
   - Attach the original PDF document.
5. If total amount exceeds ₹50,000, human approval will be required per company policy.
6. Submit bill and verify confirmation.
