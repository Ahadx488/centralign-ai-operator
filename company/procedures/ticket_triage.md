# Standard Operating Procedure: Service Desk Ticket Triage and Resolution Closure

## Purpose
This SOP guides the routine closure of resolved tickets assigned to the technical operator and communication of the status report to the IT management lead.

## Relevant Systems & Contacts
- **System**: Internal Service Desk & HR Portal (`http://localhost:8001/tickets`)
- **Login Credentials**: Username `operator`, Password `tickets_pass123`
- **Manager Contact**: Sneha Patel (`sneha.patel@company.com`), IT Administrator

## Execution Steps
1. **Access Service Desk**:
   - Navigate to `http://localhost:8001/tickets/login` and log in using operator credentials.
   - Go to `/tickets/list` to review active and resolved tickets.

2. **Filter & Identify Eligible Tickets**:
   - Find all tickets assigned to `operator` (or current user) where the status is currently `resolved`.

3. **Close Resolved Tickets**:
   - For each ticket identified, navigate to `/tickets/{ticket_number}`.
   - Submit the update form setting `status` to `closed` and keeping the assignee as `operator`.
   - Record the closed ticket references (e.g., ticket number, title, resolution note).

4. **Prepare & Send Summary Email**:
   - Query manager email address from company contacts (`sneha.patel@company.com`).
   - Use the mailbox tool (`mail_send`) to dispatch a closure summary email:
     - **Recipient**: `sneha.patel@company.com`
     - **Subject**: "Weekly Resolved Tickets Closure Summary"
     - **Body**: Detailed bulleted list of tickets updated from resolved to closed, including ticket numbers and titles.

5. **Completion Confirmation**:
   - Verify all targeted tickets reflect `closed` status and the confirmation email is logged in the corporate mailbox.
