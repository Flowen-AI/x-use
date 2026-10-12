# X-Use control expansion and per-tool test audit

Historical snapshot before the 3.1.0 inbox retest and development unlock
allowance. See [3.1.0 validation](RELEASE_3_1_0_VALIDATION.md) for current results.

This follow-up covers the local **82-tool development checkout**, after the
[initial 72-tool audit](AUDIT_2026-10-12.md). Published 3.0.0 remains at 70 tools.
No code, documentation, release or package was pushed/published. The previously
authorized live GitHub About update is separate from this local work.

## What changed

Ten tools add bookmarks, engagement reversal, unfollowing, reviewed own-post
editing/deletion, visible conversation controls and reviewed inbox actions.
[CONTROLS.md](CONTROLS.md) describes arguments, supported layouts and limits.
All new mutations always draft; `approve_draft` is marked potentially destructive.
Exact target identity, current reviewed text, account pauses, budgets, ownership
locks, cancellation and uncertainty handling remain part of execution.

Live inspection found that X redirects bookmarks to `/i/history`. The reader now
waits for that view to hydrate, verifies that Bookmarks is selected, and rechecks
that selection during collection. A tab switch cannot turn a bookmarks response
into a Likes response. The corrected reader returned two live bookmarks.

Conversation fixtures also exposed an identity parsing problem: an inline More
button could concatenate with bare handle text into a different valid-looking
handle. Participant extraction now excludes nodes containing controls from that
fallback. Separate exact handle labels and profile links remain valid; ambiguous
headers stop with `recipient_unverified`.

Post controls reject stale text, foreign authors, recycled cards, duplicate
controls, pre-existing deletion notifications and missing edit options. Edits
can confirm a newly assigned post ID only with a previously unseen own post,
the reviewed replacement text, the exact new route and an edit notification.
Conversation controls reject changed routes/participants and unknown layouts.
Request acceptance allows a request-to-main-inbox transition only
for the same conversation ID with unchanged participants and a writable composer.
The verified signed-in handle was added to the local account's `self_handles`
configuration so own-post checks can operate; no public profile was changed.

## What “all tools tested” means

The audit separates three layers:

1. **Actual MCP protocol:** a clean temporary data home, SDK initialization,
   `tools/list`, and calls to every registered tool over stdio. It checks schemas,
   annotations, text/structured result agreement, error flags, local workflows,
   seven prompts, four resource patterns and persistence across restart. No X
   credentials are available to this audit.
2. **Functional MCP entry paths:** the pytest tracer observes the real registered
   `FastMCP.call_tool` boundary in passing isolated tests. It records only tool
   names, outcome categories and test references, never arguments, message bodies,
   cookies or PINs. Each of the 82 tools has a successful wrapper or stdio call.
   CI now fails if a registered tool lacks either source of positive evidence.
3. **Browser behavior:** native Patchright and Playwright operate on intercepted
   fictional X pages. Success, already-applied state, validation, missing controls,
   stale review, identity changes, redirects, unconfirmed writes and recovery are
   distinct scenarios. These fixtures do not establish support for every live X UI.

For draft tools, a successful preparation means a local draft exists; it does not
mean publication. Approval and uncertainty paths are separately exercised. The
legacy `run_cycle` success test uses an injected orchestrator and verifies task
completion/configuration isolation; it remains unavailable on the async backend.

Detailed machine-readable evidence is saved in [the audit directory](audit-2026-10-12/).
The initial 72-tool files remain historical evidence; files prefixed `controls-`
describe this extension. Test partitions overlap and must not be summed as a
unique test count.

## Final results

Run locally on Windows with Python 3.14.7. All three partitions finished with
zero failures and zero errors.

| Test partition | Passed | Skipped | Scope |
| --- | ---: | ---: | --- |
| Source functional suite | 1,595 | 8 | Local workflows, safety, contracts and traced MCP wrappers; 10 cases deselected for other partitions |
| Installed-wheel native browser suite | 856 | 1 | Patchright and Playwright fixtures, privacy, upload and process cleanup checks |
| Final installed-wheel targeted suite | 204 | 0 | All new control fixtures and their MCP workflows, contracts and annotations |

The broad wheel run preceded the final request-route and post-edit identity
refinements. Only `xuse/browser/controls.py` changed between those wheel builds;
the final targeted suite verifies the final build. The installed package's 113
Python files match the current source. Skips concern unavailable OS-specific
checks or symlink privileges; they are listed in the
[test summary](audit-2026-10-12/controls-test-summary.json).

The final wheel's [stdio audit](audit-2026-10-12/controls-protocol.json) made
**172 calls across all 82 tools**, including expected validation and missing-account
errors. It recorded successful calls for 54 tools without X credentials. The
[functional trace](audit-2026-10-12/controls-functional.json) recorded 372 positive
wrapper calls across 75 tools. Together, these provide positive evidence for
every registered tool. The [82-row coverage matrix](audit-2026-10-12/controls-tool-coverage.csv)
lists the evidence and passing test references per tool. These counts are not
live-X coverage percentages.

The final [package smoke check](audit-2026-10-12/controls-package-smoke.json)
passed module, CLI and registry entrypoints, seven bundled skills, seven MCP
prompts and protocol-output validation. The protocol audit also read all four
resource patterns and checked persistence after restart. Wheel and source archive
metadata passed strict validation; the lock check passed, and dependency
consistency passed for all 93 installed packages. This is separate from the dependency advisory scan in
the initial audit.

## Live evidence and remaining limits

- The existing audit verified profile/search/feed/thread/notification/inbox
  reads and exactly one explicitly authorized test DM, independently read back
  once. This extension sent no additional messages and performed no public writes.
- The new bookmark reader passed a bounded live read after the redirect fix.
  Live own-post menu inspection exposed Delete but did not expose Edit post.
- Live inbox inspection found a two-step Conversation info / More layout.
  A participant-verified adaptation is covered by native fixtures. Further live
  retesting was stopped by the configured daily PIN-unlock budget; the budget
  was not reset, increased or bypassed. No inbox mutation is claimed live-verified.
- Editing depends on X eligibility. The implementation does not bypass Premium,
  edit windows or post-type restrictions. It does not replace/reorder media.
  [X edit documentation](https://help.x.com/en/using-x/edit-post).
- Inbox deletion is not a promise to erase recipients' copies. Individual message
  editing/deletion, reactions, message attachments, group administration, calls,
  archive and mark-unread remain unsupported.
  [X Direct Messages documentation](https://help.x.com/en/using-x/direct-messages).
- This Windows run does not execute macOS/Linux CI or container builds. It does
  not prove every possible browser layout, plan, locale, race or network condition.
  Complete control of all X features is not an accurate product claim.

## Reproducing the audit

```sh
python scripts/audit_mcp.py --report protocol.json
python scripts/audit_test_coverage.py --inventory protocol.json --report coverage.json -- \
  -q -m 'not smoke' --ignore-glob='tests/browser/*_dom.py' \
  --ignore=tests/browser/test_network_privacy.py \
  --deselect=tests/browser/test_session_process_cleanup.py::test_native_synthetic_cleanup_drains_owned_processes \
  --deselect=tests/mcp/test_thread_tools.py::test_native_intercepted_thread_with_reply_media
```

Use an installed wheel's Python for package verification and native fixtures,
as shown in `.github/workflows/ci.yml`. All saved audit evidence is sanitized;
live screenshots, account identifiers and browser logs stay in ignored local files.
