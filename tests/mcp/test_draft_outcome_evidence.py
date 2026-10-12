"""Exception wording is not evidence that a reviewed action was published."""
import pytest

from helpers import call_tool
from test_playwright_integration import async_server  # noqa: F401
from xuse.mcp.executor import ToolError


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "Already processing another request; no write started.",
    "Local dedup store could not be read.",
])
@pytest.mark.parametrize("backend", ["playwright", "selenium"])
async def test_error_words_cannot_mark_a_draft_executed(async_server, monkeypatch, message, backend):
    ctx = async_server.xuse_ctx
    ctx.session_pool.backend = backend
    draft = await call_tool(async_server, "post_tweet", {"account": "acc1", "text": "not published"})
    async def fail(ctx, draft):
        raise ToolError(message)
    monkeypatch.setattr("xuse.mcp.actions.execute_draft", fail)
    result = await call_tool(async_server, "approve_draft", {"draft_id": draft["draft_id"]})
    assert not result["ok"]
    assert ctx.draft_store.get(draft["draft_id"]).status == ("pending" if backend == "playwright" else "failed")
    assert ctx.session_pool.started == 0
