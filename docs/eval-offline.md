# Eval report (scripted brain)

Date 2026-10-26 | repeats 1 | conditions ['', 'flaky', 'all']

| Task | Failure condition | Result | Status | Actions | Human | Replans | Checks | Ground truth | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|---|
| onboard | none | PASS | completed | 18 | 1 | 0 | 6/6 | 3/3 | 0 | - |
| onboard | flaky | PASS | completed | 27 | 1 | 0 | 6/6 | 3/3 | 0 | - |
| onboard | all | PASS | completed | 32 | 1 | 0 | 6/6 | 3/3 | 0 | - |
| onboard-admin-declined | none | PASS | completed | 19 | 2 | 0 | 5/6 | 1/1 | 0 | - |
| duplicate-stop | none | PASS | failed | 7 | 1 | 0 | 0/0 | 1/1 | 0 | - |
| missing-info | none | PASS | completed | 18 | 3 | 0 | 6/6 | 1/1 | 0 | - |
| offboard | none | PASS | completed | 13 | 1 | 0 | 2/2 | 2/2 | 0 | - |
| offboard | flaky | PASS | completed | 13 | 1 | 0 | 2/2 | 2/2 | 0 | - |
| offboard | all | PASS | completed | 20 | 1 | 0 | 2/2 | 2/2 | 0 | - |
| offboard-relabelled | relabel | PASS | completed | 13 | 1 | 0 | 2/2 | 1/1 | 0 | - |
| offboard-declined | none | PASS | completed | 13 | 1 | 0 | 0/2 | 1/1 | 0 | - |
| offboard-declined | relabel | PASS | completed | 13 | 1 | 0 | 0/2 | 1/1 | 0 | - |
| onboard-outage | outage | PASS | completed | 20 | 1 | 1 | 6/6 | 1/1 | 0 | - |
| onboard-new-required-field | newfield | PASS | failed | 28 | 1 | 2 | 0/0 | - | 0 | - |
| unseen-change-department | - | skipped | needs the claude brain | | | | | | | |
| unseen-headcount-export | - | skipped | needs the claude brain | | | | | | | |
| unseen-start-reminders | - | skipped | needs the claude brain | | | | | | | |

## Summary
- Success rate: **14/14** (100%), 3 skipped
- Average per run: 18.1 actions, 1.21 human interventions, 0.21 replans
- Tokens: 0 | no model cost (offline brain)
