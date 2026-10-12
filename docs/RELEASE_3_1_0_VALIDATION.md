# X-Use 3.1.0 validation

This page preserves the initial 3.1.0 preparation results. The subsequent
[parallel upgrade validation](PARALLEL_UPGRADE_VALIDATION.md) covers the current
MCP/queue, inbox and Selenium improvements and their combined test results.

This is an **unpublished 3.1.0 preparation with 82 tools**, tested locally on
Windows with Python 3.14.7. Published 3.0.0 has 70 tools. No code was pushed,
PR opened, package published or release created. The earlier GitHub About-only
update was separately authorized.

## Changes and fixes

- A development server may set `XUSE_ENV=development` and
  `XUSE_DEV_UNLOCK_DAILY_CAP=20`. The normal unlock default remains five per UTC
  day. The override accepts 1–50, requires explicit development mode, and cannot
  enable an explicitly disabled unlock action. It preserves the durable attempt
  ledger, pauses, cooldowns, minute limits and all other action budgets.
- `get_account_safety` exposes remaining allowances, the next UTC reset and
  the active development override. The local MCP registration uses 20; it stores
  no PIN. [Configuration](CONFIG_REFERENCE.md) and
  [development instructions](../CONTRIBUTING.md#development-inbox-unlocking).
- Modern inbox controls verify the same chat's `/info` route, participant
  identities and the More button's bound popover. Numeric conversation references
  survive draft approval instead of being forced into the legacy messages route.
  Initial headers may hydrate; later identity checks remain strict. Reads and
  notification actions close the info sheet so the next operation can proceed.
- Conversation controls report supported actions and explicit reasons for
  unsupported flows. The modern deletion confirmation is not implemented;
  the tested legacy deletion flow remains available where observed.
- Package, registry and plugin versions agree on 3.1.0. Public installation,
  capability, workflow and contribution documentation is updated. Maintainer-only
  planning notes stay in ignored local storage and are excluded from archives.

## Automated verification

| Partition | Passed | Skipped |
|---|---:|---:|
| Source functional suite | 1,617 | 8 |
| Installed-wheel native browser suite | 874 | 1 |
| Final installed-wheel targeted suite | 300 | 0 |

Every listed partition finished with zero failures and zero errors. They overlap
and must not be added as a unique test count. The source suite also deselected ten
cases assigned to other partitions. Skips cover OS-specific checks and unavailable
symlink privileges; [exact reasons](audit-2026-10-12/release310-test-summary.json)
are recorded. Three existing `datetime.utcnow` deprecation warnings remain.

The first source run had one cold-start subprocess timeout. The ownership test's
startup allowance increased from 10 to 30 seconds, retaining its exact lock and
release assertions; the full source rerun passed. Native fixture work also caught
a DOM text-matching error, corrected before the final runs.

The source and broad wheel runs preceded the final native-reference and info-sheet
cleanup fixes in `browser/controls.py` and `mcp/control_tools.py`. The final targeted
run tests the final installed wheel, covering every control DOM fixture with both
Patchright and Playwright, MCP workflows, development budgets, contracts,
annotations and version/skill metadata. Browser fixtures use fictional intercepted
pages, not a logged-in X account. They cover successful and unchanged states,
missing/duplicate controls, stale reviews, changed identities/routes, unconfirmed
writes, recovery, and sequential read → mute → read → unmute → read behavior.

The final wheel's [SDK stdio audit](audit-2026-10-12/release310-protocol.json)
made **172 calls across all 82 tools**, including expected validation errors.
It recorded positive calls for 54 tools without X credentials. The
[passing-test trace](audit-2026-10-12/release310-functional.json) recorded
372 positive registered-wrapper calls across 75 tools. Their union
provides positive evidence for all 82 tools; the
[per-tool matrix](audit-2026-10-12/release310-tool-coverage.csv) names each test.
Draft creation means a local draft was prepared, not that an X write succeeded.
The legacy `run_cycle` success test injects its orchestrator; that tool remains
unavailable on the async browser backend.

The [package smoke](audit-2026-10-12/release310-package-smoke.json) verifies module,
CLI and registry entrypoints, seven bundled skills, seven prompts and clean MCP
protocol output. The stdio audit reads all four resource patterns and checks
restart persistence. All 113 installed Python files match source;
dependency consistency, the offline lock check, strict wheel/source metadata and
archive exclusions passed. These checks are not a fresh dependency-advisory scan.

## Live inbox evidence

Owner-provided PIN unlocking succeeded. A controls read identified the current
Conversation info popover. The MCP prepared and approved a mute action, verified
the inverse Unmute control, respected normal write pacing, then prepared and
approved unmute. A final independent controls read verified that the original
notification state was restored. This is **two confirmed notification writes**,
with no additional DMs, public posts, deletions or relationship changes.

Two earlier attempts stopped before any action write boundary. Their outcomes
were reconciled only after checking the unchanged live notification state, and
the earlier drafts were rejected. The final cycle's
[sanitized aggregate evidence](audit-2026-10-12/release310-live-inbox.json) contains
no account handles, conversation IDs, message text, PINs or cookies. Private
browser diagnostics and live scripts stay outside public artifacts.

Earlier read, bookmark and explicitly authorized single-DM evidence is preserved
in the [initial audit](AUDIT_2026-10-12.md) and the historical
[control expansion audit](CONTROL_TEST_AUDIT_2026-10-12.md). This retest does not
claim that every tool was exercised against live X.

## Remaining boundaries

- Modern conversation deletion, per-message editing/deletion, reactions,
  attachment sending, group administration, calls, archive and mark-unread remain
  unsupported. Supported operations and layouts are listed in [CONTROLS](CONTROLS.md).
- Live post inspection exposed Delete but not Edit post. Editing depends on X
  eligibility; local fixtures do not establish eligibility or live deletion support.
- Browser data is partial. Unknown locales, account plans, challenges, layout
  changes, races and network conditions can still interrupt workflows. Exhaustive
  coverage of every possible X scenario is not claimed.
- macOS/Linux CI and container execution were not run on this Windows host.
  Cross-platform CI remains required before publishing.

The next useful expansion is verified support for the modern inbox deletion and
message-action dialogs, with exact target checks and disposable test data, followed
by attachments and group management. Existing working controls should retain
regression fixtures before adding more operations.

## Reproduce

Use the commands in [CONTRIBUTING](../CONTRIBUTING.md) and the partitions in
`.github/workflows/ci.yml`. Run `scripts/audit_mcp.py` against a clean installed wheel
and `scripts/audit_test_coverage.py` against the source tests. Set
`XUSE_REQUIRE_BROWSER_TESTS=1` and `XUSE_TEST_BROWSER_DRIVERS=patchright,playwright`
for native fixtures. Live tests require separate explicit targets and credentials;
they are not part of the credential-free scripts or CI.
