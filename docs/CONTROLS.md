# Publishing, account and inbox controls

These ten additional tools are available in **3.1.0 (82 tools)**. They were added after 3.0.0. All eight mutation tools prepare a local draft, even when `draft_mode` is false. Inspect its payload and approve that specific draft once. No bulk deletion or automatic approval is provided.

| Tool | Input and effect |
| --- | --- |
| `get_bookmarks` | Account and bounded limit; reads the default bookmarks feed, not folders |
| `bookmark_tweet` | Account and exact X post URL; adds a bookmark after approval |
| `remove_bookmark` | Account and exact post URL; removes its bookmark |
| `unlike_tweet` | Removes your like from the exact post |
| `undo_retweet` | Removes your repost; does not delete somebody else's post |
| `unfollow_profile` | Account and handle/profile URL; confirmation must name the target |
| `edit_tweet` | Account, post URL, `expected_text` and replacement `text` |
| `delete_tweet` | Account, post URL and `expected_text`; permanent deletion of your own post |
| `get_conversation_controls` | Account and conversation ID/URL; reports supported controls actually observed |
| `manage_conversation` | Account, conversation ID/URL and `operation`: `accept_request`, `mute`, `unmute` or `delete` |

For edit/delete, set the account's `self_handles` accurately. Both the rendered author and the current text must match the reviewed post. If the post changes after review, the action stops. For media-only deletion, supply an empty `expected_text`. Text-only editing preserves existing attachments; replacing/reordering media is not supported by this tool.

```python
get_tweet(account="personal", tweet_url="https://x.com/your_handle/status/123")
edit_tweet(account="personal", tweet_url="https://x.com/your_handle/status/123",
           expected_text="Original text", text="Corrected text")
# Inspect the returned draft, then approve only its exact draft_id.
get_draft(draft_id="<returned-id>")
approve_draft(draft_id="<reviewed-id>")
```

X controls post eligibility. Its current help documentation describes Premium, a one-hour edit window, a limit of five edits, same-device restrictions and excluded post types. A missing Edit post menu option returns `feature_unavailable`; the server cannot grant eligibility. [X: About Edit Post](https://help.x.com/en/using-x/edit-post).

Inbox management complements `get_inbox`, `search_conversations`, `get_conversation`, `send_message`, `unlock_inbox` and the existing request/other folders. Opening a conversation to inspect controls can mark it read. Request acceptance requires a verified request prompt and a writable composer afterward. Mute/unmute supports the verified menu and the modern Conversation info popover. The `/info` route must belong to the exact same chat, participants must match, and the popover must be bound to its More button. Initial header hydration is awaited without changing the target. Deletion requires the supported confirmation dialog and an observed deletion result; the modern popover's different deletion flow remains unavailable. Localized labels and snooze-duration sheets are not silently treated as supported.

`get_conversation_controls` returns the observed conversation `url`, supported
`actions`, `unsupported_actions` with reasons, `control_layout`, `can_reply` and
`acceptance_required`. Use its returned URL for subsequent actions. A numeric
conversation ID is retained in drafts so the browser can bind it to the observed
native route instead of forcing a legacy navigation.

Notification controls must be enabled and unambiguous. A reviewed mute/unmute
whose desired state is already observed returns `already_done` without a write.
The reader and action tools dismiss verified menus after supported failures;
cleanup cannot suppress a target mismatch or uncertain outcome.

Deleting a conversation removes it from your inbox; this tool does not promise erasure for recipients. It does not implement per-message edit/delete, reactions, attachment sending, group membership, calls, marking unread or archive. [X: Direct Messages](https://help.x.com/en/using-x/direct-messages).

## Failure and recovery

- Every approved action uses the account lock, pause checks, timeout and durable ledger. Bookmark and inbox actions have separate default daily caps (30 and 20). Edits/deletions share the post budget; reversal actions share the corresponding relationship/engagement budget.
- A click alone is insufficient. Confirmation requires an inverse control, a verified writable composer, updated post evidence or a deletion notification plus disappearance of the exact target.
- An uncertain result carries an action ID. Inspect X and `get_account_safety`, then reconcile with `resolve_action_outcome`. Do not create another draft to retry blindly.
- The `approve_draft` MCP annotation now declares possible destructive behavior because approval can delete posts or conversations. Preparing a deletion draft itself remains a local, non-destructive operation.
- Development sessions can use the explicit [unlock allowance](../CONTRIBUTING.md#development-inbox-unlocking). It raises only that process's configured unlock cap; prior attempts, cooldowns and challenge pauses remain enforced.

## Scope of verification

Native browser scenarios use intercepted synthetic pages with both Patchright and Playwright. They cover successful changes, already-applied state, missing/duplicate controls, wrong targets, stale text, changed participants/routes and unconfirmed clicks. MCP tests separately cover draft creation, approval, duplicate approval, account pauses, input validation and uncertainty persistence. These are not proof that every current X layout supports every operation.

Live testing includes authorized reads, the one previously authorized test DM, and a reviewed mute/unmute cycle on that conversation. The cycle verified both inverse controls and restored the original notification state. The bookmark reader returned two saved posts after fixing the observed `/i/history` redirect; selected-tab verification runs throughout collection. The own-post menu exposed Delete but no Edit post option. No additional messages, public posts, deletions, unfollows, likes or bookmark changes were performed in the 3.1.0 retest. Full X control is not a supported claim: paid features, account permissions, interface variants and the unsupported actions above remain constraints. See the [3.1.0 validation](RELEASE_3_1_0_VALIDATION.md) for the separate protocol, fixture and live results.
