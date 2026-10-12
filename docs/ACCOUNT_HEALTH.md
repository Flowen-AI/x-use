# Account health and readiness

`get_account_health(account)` returns an operational score out of 100, the
evidence behind each criterion, blockers and recommended next steps. It reads
local configuration and bounded local records. It does not open a browser,
contact X or change account state.

The score is not an X trust score or a prediction of suspension, reach, spam
classification or account reputation. Follow the reported readiness and blockers
before using the numeric score to compare checks over time.

## Scoring criteria

| Criterion | Points | Evidence |
|---|---:|---|
| Configuration | 10 | Account schema and configuration load errors |
| Account enabled | 5 | The configured active/pause state |
| Cookies | 20 | Strict async import validation; legacy presence/expiry checks receive at most 10 |
| Proxy configuration | 10 | Effective route and supported configuration; connectivity remains untested |
| Session transport | 10 | A ready/busy local context, connected browser and open tab |
| Durable safeguards | 15 | Readable action ledger without a recorded pause |
| Action outcomes | 10 | No unfinished or uncertain writes in the available bounded snapshot |
| Daily allowances | 5 | Known limits with remaining capacity for enabled action kinds |
| Recent reliability | 5 | Successful share of completed local actions in the latest 50 records, within 24 hours |
| Live authentication | 10 | Always unknown in this local-only tool |

Each criterion reports `status`, `points`, `max_points` and safe `evidence`.
Unknown evidence earns no points and is counted separately in `unknown_points`.
`evidence_coverage_percent` measures how much of the rubric is known; it is not
a probability or confidence estimate.

The current local-only maximum is **90/100**. A cold session and an empty action
history remain unknown. For example, an otherwise valid cold account without
recent activity can score 75/100 with 25 unknown points. Do not generate activity
or increase budgets to raise the score. A warm browser does not establish a live
X login, and this tool never awards the live-authentication points.

## Readiness takes priority

| Readiness | Meaning |
|---|---|
| `blocked` | A critical configuration, credential, proxy, transport or unresolved-write issue needs attention |
| `paused` | The account is disabled, all action kinds are disabled, or the durable ledger records a pause |
| `waiting` | A session transition, active/interrupted write, pacing interval or enabled daily allowance requires waiting or inspection |
| `incomplete` | Required local evidence is unavailable or truncated |
| `local_checks_passed` | No blocking or waiting condition was found and local transport is ready; non-blocking criteria may remain partial or unknown, and live authentication is unverified |
| `unverified` | No known blocker, but a local session has not established transport readiness |

An intentional pause or a normal cooldown is not account damage. Disabled action
kinds do not count as exhausted allowances. Daily limits reset at UTC midnight.
Pacing estimates use a bounded action snapshot; the execution-time ledger remains
authoritative. An uncertain write requires checking its original outcome and
reconciling it before retrying.

## Using the result

1. Inspect `health.readiness`, `health.blockers` and `health.waiting_reasons`.
2. Follow `health.recommendations` for the specific affected criteria.
3. Use `test_proxy` only when you explicitly want a connectivity probe; the health
   call validates local proxy configuration only.
4. When ready, use a bounded signed-in read to check live access. Handle login,
   PIN and challenge recovery through the existing flows.
5. Re-run the health check after a configuration fix or completed recovery.

Original configuration, cookie, session, queue, draft and metrics fields remain
available. Metrics expose known counters and timestamps; arbitrary stored bodies
and credentials are excluded. See [MCP reference](MCP_GUIDE.md) and
[browser runtime](BROWSER_RUNTIME.md) for tool and session behavior.
