# Parallel upgrade validation — X-Use 3.1.0

This is the earlier validation snapshot. The subsequent
[Patchright hardening report](PATCHRIGHT_HARDENING.md) records session/cookie changes.

Three agents reviewed and improved MCP/queue execution, async inbox controls,
and legacy Selenium compatibility. The combined result remains **3.1.0 with
82 tools**, unpublished and unpushed. No PR was opened. No new live X writes
were performed in this follow-up. Tests ran on Windows with Python 3.14.7.

## Implemented improvements

**MCP and queued work:** the queue ID is committed alongside the browser action
reservation before execution. An additive SQLite migration preserves existing
draft/campaign references. Failure and cancellation results retain action IDs,
including journal-write errors. `resolve_action_outcome` repairs queue history
after restart: succeeded work becomes done, known-not-sent work becomes cancelled.
Recovery never automatically resends, ignores stale reservations and remains
bound to the same account. Queue pacing rechecks paused accounts and duplicate
work. Empty/false executor results cannot become confirmed success. Draft approval
requires explicit already-executed evidence instead of guessing from error wording.

**Inbox controls:** verified menus are dismissed after supported failures without
hiding the original error. Cleanup stops if identity changes. Disabled or ambiguous
notification controls are rejected. Repeated mute/unmute can report `already_done`
without a click. Disabled request acceptance is neither advertised nor executed.
Forty additional browser cases cover cleanup, changing popup IDs, repeated actions,
disabled controls and uncertain results.

**Selenium:** partially initialized browsers close on setup failure. Cookie
injection requires a verified HTTPS X/Twitter origin; driver errors cannot echo
cookie payloads into logs. Requested media must survive preparation and upload
in full. Post/reply/quote success requires a fresh explicit sent notification;
an old toast or detached editor is insufficient. Failed target navigation stops
reply/repost actions. Own timestamp links establish article identity, excluding
quoted or mentioned targets and misleading status URLs.
The optional smoke launcher's timeout cleanup targets only its owned process tree
on Windows or isolated process group on POSIX, with bounded waits.

**Runtime diagnostics:** `get_runtime_info` reports configured async/Selenium
workflow support and effective unlock policy without starting a browser or reading
account journals. Configuration does not prove authentication or live UI support.
The existing local development unlock allowance is 20 per UTC day; the normal
default remains five, with history, pauses and pacing preserved.

## Verification

| Partition | Passed | Skipped |
|---|---:|---:|
| Combined source suite | 1,716 | 8 |
| Final installed-wheel affected paths | 512 | 0 |
| Agent: MCP and queue | 213 | 0 |
| Agent: native inbox controls | 196 | 0 |
| Agent: Selenium compatibility | 143 | 0 |
| Final Selenium smoke launcher | 9 | 0 |

All partitions finished with zero failures/errors. They overlap and must not be
added as unique tests. The combined source and installed-wheel runs started after
runtime changes were frozen. The source partition assigns ten native/smoke cases
to other runs; [exact skip reasons and counts](audit-2026-10-12/parallel310-summary.json)
are recorded. Existing UTC deprecation warnings remain unrelated to these fixes.
Nine smoke-launcher tests were added after the combined suite collected and passed
as a separate partition. They simulate Windows, Linux and macOS cleanup branches;
they do not establish native Linux or macOS browser compatibility.

The first combined source run passed 1,715 cases but found one routing-skill length
regression (153 lines against the under-150 limit). Duplicate guidance was removed,
restoring 149 lines; focused skill checks and the complete rerun passed.

The queue agent first reproduced **16 failing regressions**, then fixed them.
Coverage includes the four queue write types, old-schema migration, hard process
exit during execution, restart recovery, cross-account isolation, stale actions,
journal failures and cancellation. The separate draft test run was already green
when recorded and is not represented as pre-fix evidence.

The final installed wheel's [stdio audit](audit-2026-10-12/parallel310-protocol.json)
made **172 calls across all 82 tools**. It returned positive results for
54 tools without X credentials. The source [wrapper trace](audit-2026-10-12/parallel310-functional.json)
recorded 422 positive calls across 75 tools. Their union provides
positive automated evidence for every registered tool; the
[82-row matrix](audit-2026-10-12/parallel310-tool-coverage.csv) names each passing test.
These are not live-X coverage percentages. Draft preparation is not publication.

The clean [package smoke](audit-2026-10-12/parallel310-package-smoke.json) passed all
three entrypoints, seven skills, seven prompts and protocol-output checks. The
stdio audit checked four resource patterns and persistence across restart.
All 113 installed Python files match source. Strict distribution metadata,
93-package dependency consistency, offline lock consistency and exclusion of
private maintainer notes passed. This is not a new vulnerability-advisory scan.

The [native Selenium smoke](audit-2026-10-12/parallel310-selenium-smoke.json) passed
6 checks with Chrome 154.0.8037.99 and ChromeDriver
154.0.8037.92. It used a temporary profile/configuration, blocked
external browser DNS, navigated local HTML only, exercised the production composer
and selectors, and closed its browser. Both Patchright and Playwright separately
ran the intercepted inbox fixtures against the installed wheel.

## Limits and reproduction

Modern inbox deletion, per-message controls, message attachments, group management,
calls, archive and mark-unread remain unsupported. Selenium confirmation currently
recognizes explicit English sent notifications; unknown presentations fail closed.
Firefox and macOS/Linux native behavior were not validated on this host. Cross-platform
CI remains required before release. The existing earlier live mute/unmute evidence
is preserved in [the initial 3.1.0 report](RELEASE_3_1_0_VALIDATION.md); these follow-on
changes were verified with isolated fixtures, not another live account mutation.

Use [CONTRIBUTING](../CONTRIBUTING.md) and `.github/workflows/ci.yml` for the source,
MCP and browser partitions. Opt into local Selenium verification with:

```sh
python scripts/selenium_browser_smoke.py --timeout 120 --output selenium-smoke.json
```

Selenium Manager may download a matching driver. No account setup is needed.
Reconnect a running MCP client to load the updated process code. No tool count
increase or switch away from the default Patchright backend is required.

The review consulted the [MCP tool contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[MCP cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation),
the [pinned Python SDK documentation](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0),
and Selenium's [cookie-origin](https://www.selenium.dev/documentation/webdriver/interactions/cookies/)
and [session cleanup](https://www.selenium.dev/documentation/webdriver/browser/windows/) guidance.
