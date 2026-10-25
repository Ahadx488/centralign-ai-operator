# Standard Operating Procedure: Cross-System Employee Onboarding

## Purpose
This SOP guides the end-to-end IT & HR onboarding process for newly hired employees, ensuring ticketing accountability and access matrix recording.

## Relevant Systems & Contacts
- **Service Desk & HR Portal**: `http://localhost:8001/tickets`
- **File Share**: Shared Drive (`./env/fileshare/data/system_access_list.csv`)
- **People Operations Lead**: Deepak Sharma (`deepak.sharma@company.com`)
- **IT Administrator**: Sneha Patel (`sneha.patel@company.com`)

## Execution Steps
1. **Open HR Onboarding Ticket**:
   - Access Service Desk at `/tickets/new`.
   - Submit new ticket:
     - **Title**: "Onboard new hire Priya Sharma"
     - **Description**: "Provision employee account, issue hardware, and configure system permissions."
     - **Assignee**: "Deepak Sharma" (People Operations) or "Sneha Patel" (IT Admin)
     - **Priority**: "high"
   - Note the assigned ticket number (e.g. `TICK-102`).

2. **Update Access Control Matrix**:
   - Read the existing access file in the file share: `./env/fileshare/data/system_access_list.csv`.
   - Append the new hire access row:
     `Priya Sharma,Software Engineer,Engineering,ERP;Tickets,Active,2026-10-25`
   - Save the updated file using `files_write_file`.

3. **Confirm & Finalize**:
   - Conclude with a clear confirmation statement citing the generated ticket number and verified access list update.
