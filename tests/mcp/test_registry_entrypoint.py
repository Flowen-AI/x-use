"""Registry clients must be able to launch the package name as a stdio server."""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import xuse


ROOT = Path(__file__).resolve().parents[2]


def registry_entrypoint():
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    package = next(p for p in manifest["packages"] if p["registryType"] == "pypi")
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    scripts = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    entry = re.search(rf'^{re.escape(package["identifier"])}\s*=\s*"([^"]+)"',
                      scripts, flags=re.MULTILINE)
    assert entry, "Registry package name must provide its own console executable."
    assert not package.get("packageArguments"), "The package-name alias starts MCP without arguments."
    return package["identifier"], entry.group(1)


@pytest.mark.asyncio
async def test_registry_alias_starts_stdio_before_application_diagnostics(tmp_path):
    name, entry = registry_entrypoint()
    # A dependency diagnostic emitted while importing server services must not
    # contaminate the JSON-RPC stream. This also prevents wiring the alias to the
    # interactive Typer app, which only displays help without an `mcp` subcommand.
    code = """
import builtins
from importlib.metadata import EntryPoint
real_import = builtins.__import__
def diagnostic_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'xuse.mcp' and 'server' in fromlist:
        print('registry startup diagnostic')
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = diagnostic_import
EntryPoint(name=%r, value=%r, group='console_scripts').load()()
""" % (name, entry)
    permitted = {"PATH", "SYSTEMROOT", "USERPROFILE", "LOCALAPPDATA", "TEMP",
                 "TMP", "COMSPEC", "PATHEXT"}
    env = {key: value for key, value in os.environ.items() if key.upper() in permitted}
    env.update(X_USE_HOME=str(tmp_path / "data"), PYTHONNOUSERSITE="1",
               PYTHONPATH=str(Path(xuse.__file__).resolve().parents[1]))
    params = StdioServerParameters(command=sys.executable, args=["-c", code],
                                   cwd=str(tmp_path), env=env)

    async def check():
        with (tmp_path / "stderr.log").open("w", encoding="utf-8") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "x-use"
                    assert initialized.server_info.version == xuse.__version__
                    tools = await session.list_tools()
                    assert "list_accounts" in {tool.name for tool in tools.tools}
                    accounts = await session.call_tool("list_accounts", {})
                    assert not accounts.is_error
                    assert json.loads(accounts.content[0].text)["accounts"] == []

    await asyncio.wait_for(check(), timeout=90)
    assert "registry startup diagnostic" in (tmp_path / "stderr.log").read_text(encoding="utf-8")


def test_registry_metadata_has_a_useful_supported_description():
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert 0 < len(manifest["description"]) <= 100
    assert 0 < len(manifest["title"]) <= 100
    assert manifest["websiteUrl"] == manifest["repository"]["url"]
