# Reports and Exports SOP

Owner: People Ops. Applies to requests for lists, headcounts and exports.

## Headcount export
1. Sign in to AcmeHR with the ops service account (see the Systems Guide).
2. Read the employee directory. A headcount counts only employees with status active; inactive people are never included.
3. Filter to the requested department (the Department column in the directory).
4. Write a CSV to the path the requester gave, or to outputs/<department>_headcount.csv if none was given. Use the header name,email,department,start_date and one row per active employee, in directory order.
5. Do not email the export to anyone unless asked.

## Completion criteria
- The file exists at the requested path with the header above.
- It contains exactly the active employees of that department in AcmeHR, no more and no fewer.
