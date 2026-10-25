# Systems Guide

## AcmeHR
- Internal HR system of record. Web UI at http://127.0.0.1:8765 (login page at /login).
- Sign in with username ops-bot. The password is a managed secret: type it as {{secret:ACMEHR_PASSWORD}} and never ask for it in chat.
- Sessions expire without warning; if you land on the sign-in page unexpectedly, sign in again and repeat the interrupted action.
- The server occasionally returns "500 Internal Server Error" on save. Reload the form and retry; it is transient. Check the directory before retrying in case the record was actually saved.
- Button labels change between releases. Read the page and pick the control by its purpose, not memorised text.

## Company drive
- data/team_roster.csv: master roster. Columns: name, email, department, start_date, status.
- outputs/: where generated documents go.
- outbox/: outgoing email log (written by the email tool).
