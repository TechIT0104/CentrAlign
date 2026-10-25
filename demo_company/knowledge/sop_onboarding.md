# Employee Onboarding SOP

Owner: People Ops. Applies to every new hire; the request usually only gives a name, department and start date.

## Identity and naming
- Work email format: first.last@acme.test, all lowercase (for example priya.sharma@acme.test).
- Department must be one of Engineering, Finance, Design, Sales, People Ops.
- Default access level is standard. Admin or finance access is never granted during onboarding unless the requester states it explicitly, and it needs human approval.

## Procedure
1. Sign in to AcmeHR with the ops service account (see the Systems Guide).
2. Search the AcmeHR directory for the work email first. If a record already exists (including an inactive one), stop and ask a People Ops human how to proceed. Never create duplicates.
3. Create the employee record in AcmeHR using the New employee form: full name, work email, department, start date, access level.
4. Add or update the person's row in the team roster (data/team_roster.csv) with columns name, email, department, start_date, status=active.
5. Write a welcome document to outputs/welcome_<first>_<last>.md following the Brand Voice guide.
6. Email the welcome document to the new hire's work email. Sending email requires approval from a human before it is sent.

## Completion criteria
- AcmeHR shows the employee as active with the right department, start date and access level.
- The roster has exactly one row for the employee.
- The welcome document exists and mentions the start date.
- The welcome email is in the outbox (unless a human declined to send it).
