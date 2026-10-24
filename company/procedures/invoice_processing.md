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
- **Vendor Name**: Match against approved ERP vendors. If ambiguous (e.g. "Apex Ltd" vs "Apex Inc"), clarify before submitting.
- **Invoice Number**: Alphanumeric reference code.
- **Invoice Date & Due Date**: Standardize to `YYYY-MM-DD`.
- **Total Amount**: Numeric amount.
- **Currency**: Currency code (e.g., INR, USD, EUR).

## Entry into Acme ERP
1. Log in to Acme ERP (`http://localhost:8001`) using stored credentials.
2. Navigate to **New Bill** form.
3. Check for existing bill with same vendor and invoice number:
   - If a bill already exists, flag as **duplicate**; do not submit a duplicate bill.
4. Fill in:
   - Vendor Name
   - Invoice Reference
   - Amount & Currency
   - Due Date
   - Attach the original PDF document.
5. If total amount exceeds ₹50,000, the system will trigger approval hold per policy.
6. Submit bill and verify confirmation.
