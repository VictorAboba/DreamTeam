"""Tests for the MCP forge (docs/SPEC.md §11). Real stdio server subprocesses are started."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from mcp import types as mcp_types

from dreamteam import mcp_forge, prompts
from dreamteam.mcp_forge import (
    SERVER_TEMPLATE,
    ConnectionFailed,
    MCPConnection,
    MCPForge,
    render_result,
    tool_signature,
)
from dreamteam.models import USER, ToolError
from dreamteam.shell import scrubbed_env

ALICE, BOB, CAROL, DAVE = "Alice Smith", "Bob Jones", "Carol White", "Dave Brown"
SERVER_KEYS = {"name", "owner", "scope", "task_id", "description", "status", "origin", "tools", "error",
               "requirements", "command", "created_at"}

CALC = '''
import os
import time
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("calc")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.tool()
def pid() -> int:
    """Return the server process id."""
    return os.getpid()


@mcp.tool()
def crash(x: str) -> str:
    """Raise an unexpected error."""
    raise ValueError("kaboom " + x)


@mcp.tool()
def big(n: int) -> str:
    """Return a long string."""
    return "x" * n


@mcp.tool()
def slow(seconds: float) -> str:
    """Sleep for a while."""
    time.sleep(seconds)
    return "slept"


@mcp.tool()
def env(key: str) -> str:
    """Read an environment variable."""
    return os.environ.get(key, "<unset>")


if __name__ == "__main__":
    mcp.run()
'''

CALC_V2 = CALC.replace("return a + b", "return a + b + 1000")

TEXTKIT = '''
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("textkit")


@mcp.tool()
def upper(text: str) -> str:
    """Uppercase a string."""
    return text.upper()


if __name__ == "__main__":
    mcp.run()
'''

FLAKY = '''
import os
import pathlib
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("flaky")
MARK = pathlib.Path("crashed.flag")


@mcp.tool()
def flaky() -> str:
    """Kill the process on the first call, answer afterwards."""
    if not MARK.exists():
        MARK.write_text("1")
        os._exit(3)
    return "recovered"


@mcp.tool()
def die() -> str:
    """Always kill the process."""
    os._exit(4)


if __name__ == "__main__":
    mcp.run()
'''

BROKEN = '''
import sys
print("starting broken server", file=sys.stderr)
raise RuntimeError("bad code here")
'''

NO_TOOLS = '''
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("empty")

if __name__ == "__main__":
    mcp.run()
'''

EXTERNAL = '''
import os
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("ext")


@mcp.tool()
def offset_add(a: int) -> int:
    """Add the configured offset."""
    return a + int(os.environ.get("CALC_OFFSET", "0"))


@mcp.tool()
def env(key: str) -> str:
    """Read an environment variable."""
    return os.environ.get(key, "<unset>")


if __name__ == "__main__":
    mcp.run()
'''


# --- helpers ---------------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        await asyncio.sleep(0.1)
    return not pid_alive(pid)


def events(swarm, prefix: str = "mcp.") -> list[dict]:
    return [e for e in swarm.bus.recent(limit=1000) if e["type"].startswith(prefix)]


def add_task(swarm, created_by: str, assignee: str | None = None, reviewer: str | None = None) -> int:
    now = time.time()
    return swarm.db.insert(
        "INSERT INTO tasks(title, status, assignee, reviewer, created_by, created_at, updated_at)"
        " VALUES ('t', 'todo', ?, ?, ?, ?, ?)",
        (assignee, reviewer, created_by, now, now),
    )


@pytest.fixture
async def forge_factory(make_fake_swarm, tmp_path: Path):
    forges: list[MCPForge] = []

    async def factory(mcp: dict | None = None, subdir: str = "project", agents: dict | None = None,
                      members=(ALICE, BOB, CAROL, DAVE)):
        raw = {"mcp": {"library_dir": str(tmp_path / "library"), "startup_timeout": 60, "call_timeout": 30,
                       **(mcp or {})}}
        if agents:
            raw["agents"] = agents
        swarm = make_fake_swarm(raw, subdir=subdir)
        for member in members:
            swarm.add_member(member)
        forge = MCPForge(swarm)
        swarm.forge = forge
        await forge.start()
        forges.append(forge)
        return swarm, forge

    yield factory
    for forge in forges:
        await forge.stop()


@pytest.fixture
async def env(forge_factory):
    return await forge_factory()


# --- pure helpers ----------------------------------------------------------------------------

def test_render_result_text_media_and_structured():
    result = mcp_types.CallToolResult(content=[
        mcp_types.TextContent(type="text", text="hello"),
        mcp_types.ImageContent(type="image", data="aGVsbG8=", mime_type="image/png"),
        mcp_types.AudioContent(type="audio", data="aGVsbG8=", mime_type="audio/wav"),
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.TextResourceContents(
            uri="file:///a.txt", text="inner text")),
        mcp_types.EmbeddedResource(type="resource", resource=mcp_types.BlobResourceContents(
            uri="file:///b.bin", blob="AAAA", mime_type="application/octet-stream")),
        mcp_types.ResourceLink(type="resource_link", uri="file:///c.txt", name="c"),
    ], structured_content={"ignored": True})
    text = render_result(result)
    assert text.startswith("hello")
    assert "[image: image/png" in text and "[audio: audio/wav" in text
    assert "[resource file:///a.txt]\ninner text" in text
    assert "[binary resource file:///b.bin: application/octet-stream]" in text
    assert "[resource link: file:///c.txt (c)]" in text
    assert "ignored" not in text  # structured content only when there is no text


def test_render_result_structured_only_and_empty():
    structured = mcp_types.CallToolResult(content=[], structured_content={"k": "привет"})
    assert json.loads(render_result(structured)) == {"k": "привет"}
    image_only = mcp_types.CallToolResult(
        content=[mcp_types.ImageContent(type="image", data="", mime_type="image/png")], structured_content=[1, 2])
    assert render_result(image_only).endswith("[1, 2]")
    assert render_result(mcp_types.CallToolResult(content=[])) == "(empty result)"


def test_tool_signature():
    tool = {"name": "add", "input_schema": {"type": "object", "properties": {"a": {}, "b": {}, "c": {}},
                                            "required": ["a", "b"]}}
    assert tool_signature(tool) == "add(a, b, c?)"
    assert tool_signature({"name": "ping", "input_schema": {}}) == "ping()"


def test_build_command(monkeypatch):
    assert MCPForge.build_command([]) == [sys.executable, "server.py"]
    monkeypatch.setattr(mcp_forge.shutil, "which", lambda name: "C:/bin/uv.exe" if name == "uv" else None)
    assert MCPForge.build_command([]) == [sys.executable, "server.py"]
    assert MCPForge.build_command(["httpx>=0.27", "bs4"]) == [
        "C:/bin/uv.exe", "run", "--no-project", "--with", "mcp>=2.3,<3", "--with", "httpx>=0.27", "--with", "bs4",
        "python", "server.py"]
    monkeypatch.setattr(mcp_forge.shutil, "which", lambda name: None)
    assert MCPForge.build_command(["httpx"]) == [sys.executable, "server.py"]


def test_build_command_rejects_uv_cmd_shim_on_windows(monkeypatch):
    # A .cmd shim would pass agent-controlled requirement strings through cmd.exe (< > are redirections).
    monkeypatch.setattr(mcp_forge.os, "name", "nt")
    monkeypatch.setattr(mcp_forge.shutil, "which", lambda name: "C:/bin/uv.cmd" if name == "uv" else None)
    assert MCPForge.build_command(["httpx<1"]) == [sys.executable, "server.py"]


def test_read_log_tail_uses_last_start(tmp_path: Path):
    log_path = tmp_path / "x.log"
    assert mcp_forge.read_log_tail(log_path) == ""
    marker = mcp_forge.LOG_MARKER
    lines = [f"{marker} first =====", "old error"] + [f"{marker} second =====", *[f"line {i}" for i in range(60)]]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tail = mcp_forge.read_log_tail(log_path, lines=5)
    assert tail.splitlines() == [f"line {i}" for i in range(55, 60)]
    assert "old error" not in mcp_forge.read_log_tail(log_path, lines=100)


def test_notification_texts_accept_forge_kwargs():
    for lang in ("ru", "en"):
        assert "calc" in prompts.t(lang, "u_mcp_pending", owner=ALICE, name="calc", description="d")
        assert "calc" in prompts.t(lang, "n_mcp_approved", name="calc")
        assert "why" in prompts.t(lang, "n_mcp_rejected", name="calc", comment="why")


# --- MCPConnection ---------------------------------------------------------------------------

async def test_connection_start_call_close(tmp_path: Path, fake_swarm):
    folder = tmp_path / "srv"
    folder.mkdir()
    (folder / "server.py").write_text(CALC, encoding="utf-8")
    log_path = tmp_path / "logs" / "mcp-calc.log"
    log_path.parent.mkdir()
    log_path.write_bytes(b"old noise\n" * (mcp_forge.LOG_MAX_BYTES // 10 + 1))  # oversized log is restarted
    conn = MCPConnection("calc", [sys.executable, "server.py"], folder, scrubbed_env(fake_swarm.config), log_path)
    tools = await conn.start(60)
    assert log_path.stat().st_size < 100_000
    try:
        assert conn.alive
        add = next(t for t in tools if t["name"] == "add")
        assert add["description"] == "Add two numbers."
        assert set(add["input_schema"]["properties"]) == {"a", "b"}
        assert await conn.call("add", {"a": 2, "b": 3}, 30) == ("5", False)
        text, is_error = await conn.call("add", {"a": "x"}, 30)
        assert is_error and "validation" in text.lower()
    finally:
        await conn.close()
    assert not conn.alive
    assert log_path.exists() and mcp_forge.LOG_MARKER in log_path.read_text("utf-8")
    with pytest.raises(ToolError):
        await conn.call("add", {"a": 1, "b": 1}, 5)


async def test_connection_start_timeout_kills_process(tmp_path: Path, fake_swarm):
    folder = tmp_path / "hang"
    folder.mkdir()
    (folder / "server.py").write_text(
        "import os, sys, time\nprint('PID=%d' % os.getpid(), file=sys.stderr, flush=True)\ntime.sleep(120)\n",
        encoding="utf-8")
    log_path = tmp_path / "hang.log"
    conn = MCPConnection("hang", [sys.executable, "server.py"], folder, scrubbed_env(fake_swarm.config), log_path)
    started = time.monotonic()
    with pytest.raises(ConnectionFailed) as info:
        await conn.start(3)
    assert time.monotonic() - started < 30
    assert "no answer" in str(info.value) and "PID=" in info.value.log_tail
    assert not conn.alive and conn._task.done()
    pid = int(info.value.log_tail.split("PID=")[1].split()[0])
    assert await wait_dead(pid)


async def test_connection_bad_command(tmp_path: Path, fake_swarm):
    conn = MCPConnection("nope", ["definitely-not-a-real-command-xyz"], tmp_path, scrubbed_env(fake_swarm.config),
                         tmp_path / "nope.log")
    with pytest.raises(ConnectionFailed) as info:
        await conn.start(20)
    assert "failed to start" in str(info.value)
    assert not conn.alive


# --- create / call ----------------------------------------------------------------------------

async def test_server_template_works(env):
    swarm, forge = env
    text = await forge.create(ALICE, "template", "Template server", SERVER_TEMPLATE)
    assert "is active" in text and "add(a, b): Add two numbers." in text
    assert await forge.call(BOB, "template", "add", {"a": 1, "b": 2}) == ("3", False)


async def test_create_and_call(env):
    swarm, forge = env
    text = await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    assert "MCP server 'calc' is active" in text
    assert "- add(a, b): Add two numbers." in text
    assert 'mcp_call(server="calc"' in text and "Saved to the shared MCP library" in text

    server = forge.get("calc")
    assert set(server) == SERVER_KEYS
    assert server["owner"] == ALICE and server["scope"] == "swarm" and server["status"] == "active"
    assert server["origin"] == "workspace" and server["error"] is None and server["requirements"] == []
    assert server["command"] == []  # a forged server runs its server.py: no argv of its own to show
    assert {"add", "pid", "crash", "big", "slow", "env"} <= set(server["tools"])

    folder = swarm.config.workspace_dir / "mcp" / "calc"
    assert (folder / "server.py").read_text("utf-8") == CALC
    meta = json.loads((folder / "meta.json").read_text("utf-8"))
    assert meta["owner"] == ALICE and meta["description"] == "Arithmetic helpers"
    assert (swarm.config.data_dir / "logs" / "mcp-calc.log").exists()

    created = [e for e in events(swarm) if e["type"] == "mcp.created"]
    assert len(created) == 1 and created[0]["data"]["server"]["name"] == "calc"
    assert created[0]["actor"] == ALICE

    # the smoke-test process is kept as the pooled connection
    conn = forge.connection("calc")
    assert conn is not None and conn.alive
    assert await forge.call(BOB, "calc", "add", {"a": 2, "b": 3}) == ("5", False)
    assert await forge.call(CAROL, "CALC ", "add", {"a": 1, "b": 1}) == ("2", False)  # lookups normalise
    assert forge.connection("calc") is conn

    tools = await forge.tools(BOB, "calc")
    add = next(t for t in tools if t["name"] == "add")
    assert set(add) == {"name", "description", "input_schema"}
    assert add["input_schema"]["required"] == ["a", "b"]


async def test_call_errors_and_truncation(forge_factory):
    swarm, forge = await forge_factory(agents={"tool_output_chars": 300})
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)

    text, is_error = await forge.call(BOB, "calc", "add", {"a": "x"})
    assert is_error and "validation" in text.lower()

    text, is_error = await forge.call(BOB, "calc", "crash", {"x": "boom"})
    assert is_error and "Error executing tool crash" in text and "kaboom boom" in text  # from the server log

    text, is_error = await forge.call(BOB, "calc", "big", {"n": 5000})
    assert not is_error and len(text) < 400 and "characters skipped" in text

    with pytest.raises(ToolError, match="no tool 'nope'.*add"):
        await forge.call(BOB, "calc", "nope", {})
    with pytest.raises(ToolError, match="JSON object"):
        await forge.call(BOB, "calc", "add", [1, 2])
    with pytest.raises(ToolError, match="tool is required"):
        await forge.call(BOB, "calc", "", {})
    with pytest.raises(ToolError, match="Unknown MCP server 'missing'.*calc"):
        await forge.call(BOB, "missing", "add", {})
    with pytest.raises(ToolError, match="Unknown MCP server"):
        await forge.tools(BOB, "missing")


async def test_secrets_are_scrubbed_from_server_env(env, monkeypatch):
    swarm, forge = env
    monkeypatch.setenv("DT_FORGE_TEST_API_KEY", "sekret")
    monkeypatch.setenv("DT_FORGE_PLAIN", "visible")
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    assert await forge.call(BOB, "calc", "env", {"key": "DT_FORGE_TEST_API_KEY"}) == ("<unset>", False)
    assert await forge.call(BOB, "calc", "env", {"key": "DT_FORGE_PLAIN"}) == ("visible", False)
    assert await forge.call(BOB, "calc", "env", {"key": "PYTHONUTF8"}) == ("1", False)


async def test_broken_code_fails_with_log_tail(env):
    swarm, forge = env
    text = await forge.create(ALICE, "broken", "Does not start", BROKEN)
    assert "FAILED" in text and "status: failed" in text
    assert "bad code here" in text and "starting broken server" in text
    assert "call mcp_create again with the same name" in text
    server = forge.get("broken")
    assert server["status"] == "failed" and "bad code here" in server["error"]
    assert forge.connection("broken") is None
    with pytest.raises(ToolError, match="failed its smoke test.*Alice Smith"):
        await forge.call(BOB, "broken", "x", {})
    assert not (swarm.config.library_dir / "broken").exists()

    # the owner fixes it under the same name
    text = await forge.create(ALICE, "broken", "Now it works", CALC)
    assert "is active" in text
    server = forge.get("broken")
    assert server["status"] == "active" and server["error"] is None
    assert await forge.call(BOB, "broken", "add", {"a": 1, "b": 2}) == ("3", False)
    assert len([e for e in events(swarm) if e["type"] == "mcp.created"]) == 2


async def test_syntax_error_and_no_tools(env):
    swarm, forge = env
    text = await forge.create(ALICE, "syntax", "Bad syntax", "def broken(:\n    pass\n")
    assert "FAILED" in text and "SyntaxError in server.py line 1" in text
    assert forge.get("syntax")["status"] == "failed"
    assert not (swarm.config.data_dir / "logs" / "mcp-syntax.log").exists()  # no process was started

    text = await forge.create(ALICE, "empty", "No tools", NO_TOOLS)
    assert "FAILED" in text and "exposes no tools" in text
    assert forge.get("empty")["status"] == "failed" and forge.connection("empty") is None


async def test_unparseable_nesting_fails_cleanly(env):
    # compile() raises RecursionError / MemoryError (not SyntaxError) for such code
    swarm, forge = env
    for code in ("x = 1" + "+1" * 60000, "-" * 150000 + "1"):
        text = await forge.create(ALICE, "deep", "Too deeply nested", code)
        assert "FAILED" in text and "too deeply nested" in text
        assert forge.get("deep")["status"] == "failed"
    assert not (swarm.config.data_dir / "logs" / "mcp-deep.log").exists()


async def test_interrupted_create_does_not_activate_unapproved_code(forge_factory):
    swarm, forge = await forge_factory(mcp={"require_approval": True})
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    await forge.approve(USER, "calc", True)
    assert await forge.call(BOB, "calc", "add", {"a": 1, "b": 2}) == ("3", False)

    # the owner rebuilds it, but the agent is cancelled (retired, swarm stopping...) mid smoke test
    slow_v2 = "import time\ntime.sleep(3)\n" + CALC_V2
    creating = asyncio.create_task(forge.create(ALICE, "calc", "Arithmetic helpers v2", slow_v2))
    await asyncio.sleep(1)
    creating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creating

    # the new code is on disk but was neither tested nor approved: it must not run
    server = forge.get("calc")
    assert server["status"] == "failed" and "interrupted" in server["error"]
    assert forge.connection("calc") is None
    with pytest.raises(ToolError, match="failed its smoke test.*interrupted"):
        await forge.call(BOB, "calc", "add", {"a": 1, "b": 2})
    assert [e["data"]["server"]["status"] for e in events(swarm) if e["type"] == "mcp.created"][-1] == "failed"

    # calling mcp_create again recovers
    await forge.create(ALICE, "calc", "Arithmetic helpers v2", CALC_V2)
    assert forge.get("calc")["status"] == "pending"


async def test_pending_server_is_announced_even_if_create_is_cancelled_late(forge_factory, monkeypatch):
    swarm, forge = await forge_factory(mcp={"require_approval": True})
    original = forge._write_workspace_meta

    def slow_meta(rec, save_to_library):
        time.sleep(1.5)
        original(rec, save_to_library)

    monkeypatch.setattr(forge, "_write_workspace_meta", slow_meta)
    creating = asyncio.create_task(forge.create(ALICE, "calc", "Arithmetic helpers", SERVER_TEMPLATE))
    deadline = time.monotonic() + 60
    while forge.get("calc") is None or forge.get("calc")["status"] != "pending":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    creating.cancel()  # the record is saved; the agent is cancelled while meta.json is written
    with pytest.raises(asyncio.CancelledError):
        await creating
    assert forge.get("calc")["status"] == "pending"
    assert swarm.notifications_for(USER) == [
        ("u_mcp_pending", {"owner": ALICE, "name": "calc", "description": "Arithmetic helpers"})]
    assert [e["data"]["server"]["status"] for e in events(swarm) if e["type"] == "mcp.created"] == ["pending"]
    await asyncio.sleep(1.6)  # let the meta thread finish before the tmp dir goes away


async def test_reject_stops_server_started_by_user(forge_factory):
    swarm, forge = await forge_factory(mcp={"require_approval": True})
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    pid = int((await forge.call(USER, "calc", "pid", {}))[0])  # the user tries it before deciding
    conn = forge.connection("calc")
    assert conn is not None and conn.alive
    await forge.approve(USER, "calc", False, "no")
    assert forge.connection("calc") is None and not conn.alive
    assert await wait_dead(pid)


async def test_caller_waiting_during_stop_starts_no_process(env):
    swarm, forge = env
    slow_start = "import time\ntime.sleep(2)\n" + SERVER_TEMPLATE
    creating = asyncio.create_task(forge.create(ALICE, "slowstart", "Starts slowly", slow_start))
    await asyncio.sleep(0.5)
    waiting = asyncio.create_task(forge.call(BOB, "slowstart", "add", {"a": 1, "b": 1}))  # blocked by create
    await asyncio.sleep(0.2)
    await forge.stop()
    await creating
    with pytest.raises(ToolError, match="stopped"):
        await waiting
    log_text = (swarm.config.data_dir / "logs" / "mcp-slowstart.log").read_text("utf-8")
    assert log_text.count(mcp_forge.LOG_MARKER) == 1  # only the smoke test started a process


def test_external_string_command_splitting(fake_swarm):
    forge = MCPForge(fake_swarm)
    rec = forge._external_record("x", {"command": '"C:\\Program Files\\nodejs\\node.exe" server.js --flag "a b"'})
    assert rec["command"] == ["C:\\Program Files\\nodejs\\node.exe", "server.js", "--flag", "a b"]
    rec = forge._external_record("x", {"command": "npx -y '@scope/pkg@1.0' --opt=\"v w\""})
    assert rec["command"] == ["npx", "-y", "@scope/pkg@1.0", "--opt=v w"]
    if os.name == "nt":  # backslashes in Windows paths are not escapes
        rec = forge._external_record("x", {"command": "C:\\tools\\srv.exe --root C:\\data\\#1"})
        assert rec["command"] == ["C:\\tools\\srv.exe", "--root", "C:\\data\\#1"]
    rec = forge._external_record("x", {"command": ["a"], "cwd": "tools/ext"})
    assert rec["cwd"] == str(fake_swarm.config.project_dir / "tools" / "ext")


async def test_external_string_command_with_quoted_executable(forge_factory, tmp_path: Path):
    ext_dir = tmp_path / "ext dir"
    ext_dir.mkdir()
    (ext_dir / "server.py").write_text(EXTERNAL, encoding="utf-8")
    external = {"quoted": {"command": f'"{sys.executable}" server.py', "cwd": str(ext_dir)}}
    swarm, forge = await forge_factory(mcp={"external": external})
    assert await forge.call(BOB, "quoted", "offset_add", {"a": 5}) == ("5", False)


async def test_missing_requirement_without_uv(env, monkeypatch):
    swarm, forge = env
    monkeypatch.setattr(mcp_forge.shutil, "which", lambda name: None)
    code = "import definitely_missing_module_xyz\n" + SERVER_TEMPLATE
    text = await forge.create(ALICE, "needs_dep", "Needs a package", code, requirements=["definitely-missing-xyz"])
    assert "FAILED" in text and "definitely_missing_module_xyz" in text and "uv is not on PATH" in text
    rec = forge._record("needs_dep")
    assert rec["command"] == [sys.executable, "server.py"]
    assert forge.get("needs_dep")["requirements"] == ["definitely-missing-xyz"]


async def test_input_validation(env):
    swarm, forge = env
    for bad in ("Calc", "a", "1abc", "my-tool", "x" * 41, "", "has space"):
        with pytest.raises(ToolError, match="Invalid MCP server name"):
            await forge.create(ALICE, bad, "d", SERVER_TEMPLATE)
    with pytest.raises(ToolError, match="description is required"):
        await forge.create(ALICE, "calc", "  ", SERVER_TEMPLATE)
    with pytest.raises(ToolError, match="code is empty"):
        await forge.create(ALICE, "calc", "d", "   ")
    with pytest.raises(ToolError, match="Invalid scope"):
        await forge.create(ALICE, "calc", "d", SERVER_TEMPLATE, scope="team")
    with pytest.raises(ToolError, match="needs task_id"):
        await forge.create(ALICE, "calc", "d", SERVER_TEMPLATE, scope="task")
    with pytest.raises(ToolError, match="does not exist"):
        await forge.create(ALICE, "calc", "d", SERVER_TEMPLATE, scope="task", task_id=999)
    with pytest.raises(ToolError, match="must be an integer"):
        await forge.create(ALICE, "calc", "d", SERVER_TEMPLATE, scope="task", task_id="abc")
    with pytest.raises(ToolError, match="Invalid requirement"):
        await forge.create(ALICE, "calc", "d", SERVER_TEMPLATE, requirements=["--index-url=http://evil"])
    with pytest.raises(ToolError, match="list of pip requirement"):
        await forge.create(ALICE, "calc", "d", SERVER_TEMPLATE, requirements={"a": 1})
    assert forge.list(USER) == [] and events(swarm) == []
    assert MCPForge._check_requirements("httpx, bs4\nhttpx") == ["httpx", "bs4"]


def test_requirement_version_ranges_are_not_split():
    check = MCPForge._check_requirements
    assert check("httpx>=0.27,<1, bs4") == ["httpx>=0.27,<1", "bs4"]
    assert check("pkg[a,b]>=1,!=1.5\nother") == ["pkg[a,b]>=1,!=1.5", "other"]
    assert check(["httpx>=0.27,<1"]) == ["httpx>=0.27,<1"]
    # a range split into separate items (e.g. by a comma-splitting caller) is reported, not passed to uv
    with pytest.raises(ToolError, match="'<1': it has no package name.*one list item"):
        check(["httpx>=0.27", "<1"])


async def test_overwrite_rules(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    old_conn = forge.connection("calc")

    with pytest.raises(ToolError, match="belongs to Alice Smith"):
        await forge.create(BOB, "calc", "Mine now", CALC_V2)
    assert forge.get("calc")["owner"] == ALICE

    # the owner replaces the code: the old process is stopped and the new code is live
    text = await forge.create(ALICE, "calc", "Arithmetic helpers v2", CALC_V2)
    assert "is active" in text
    assert not old_conn.alive
    assert await forge.call(BOB, "calc", "add", {"a": 1, "b": 2}) == ("1003", False)
    assert forge.get("calc")["description"] == "Arithmetic helpers v2"

    # the user may overwrite anyone's server; ownership stays with the agent
    await forge.create(USER, "calc", "Fixed by the user", CALC)
    server = forge.get("calc")
    assert server["owner"] == ALICE and server["description"] == "Fixed by the user"
    assert await forge.call(BOB, "calc", "add", {"a": 1, "b": 2}) == ("3", False)
    assert len([e for e in events(swarm) if e["type"] == "mcp.created"]) == 3


async def test_private_scope(env):
    swarm, forge = env
    text = await forge.create(ALICE, "secret", "Private helpers", CALC, scope="private")
    assert "Saved to the shared MCP library" not in text
    assert not (swarm.config.library_dir / "secret").exists()

    assert [s["name"] for s in forge.list(ALICE)] == ["secret"]
    assert forge.list(BOB) == []
    assert [s["name"] for s in forge.list(USER)] == ["secret"]
    with pytest.raises(ToolError, match="Unknown MCP server 'secret'"):
        await forge.call(BOB, "secret", "add", {"a": 1, "b": 1})
    with pytest.raises(ToolError, match="Unknown MCP server 'secret'"):
        await forge.tools(BOB, "secret")
    assert await forge.call(ALICE, "secret", "add", {"a": 1, "b": 1}) == ("2", False)
    assert await forge.call(USER, "secret", "add", {"a": 2, "b": 2}) == ("4", False)


async def test_task_scope(env):
    swarm, forge = env
    task_id = add_task(swarm, created_by=CAROL, assignee=BOB)
    await forge.create(ALICE, "tasky", "Task helpers", CALC, scope="task", task_id=task_id)
    server = forge.get("tasky")
    assert server["scope"] == "task" and server["task_id"] == task_id
    assert not (swarm.config.library_dir / "tasky").exists()

    for member in (ALICE, BOB, CAROL, USER):
        assert [s["name"] for s in forge.list(member)] == ["tasky"]
        assert await forge.call(member, "tasky", "add", {"a": 1, "b": 1}) == ("2", False)
    assert forge.list(DAVE) == []
    with pytest.raises(ToolError, match="Unknown MCP server"):
        await forge.call(DAVE, "tasky", "add", {"a": 1, "b": 1})

    # access follows the task: Dave becomes the reviewer
    swarm.db.execute("UPDATE tasks SET reviewer = ? WHERE id = ?", (DAVE, task_id))
    assert await forge.call(DAVE, "tasky", "add", {"a": 2, "b": 2}) == ("4", False)


async def test_approval_flow(forge_factory):
    swarm, forge = await forge_factory(mcp={"require_approval": True})
    text = await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    assert "waiting for the user's approval" in text and "status: pending" in text
    assert forge.get("calc")["status"] == "pending"
    assert forge.connection("calc") is None  # the smoke-test process is not kept
    assert swarm.notifications_for(USER) == [
        ("u_mcp_pending", {"owner": ALICE, "name": "calc", "description": "Arithmetic helpers"})]
    assert not (swarm.config.library_dir / "calc").exists()

    with pytest.raises(ToolError, match="waiting for the user's approval"):
        await forge.call(ALICE, "calc", "add", {"a": 1, "b": 1})
    with pytest.raises(ToolError, match="Only the user"):
        await forge.approve(ALICE, "calc", True)
    # the user may try a pending server before approving it
    assert await forge.call(USER, "calc", "add", {"a": 1, "b": 1}) == ("2", False)

    text = await forge.approve(USER, "calc", True)
    assert "approved" in text and "Saved to the shared MCP library" in text
    assert forge.get("calc")["status"] == "active"
    assert swarm.notifications_for(ALICE) == [("n_mcp_approved", {"name": "calc"})]
    assert (swarm.config.library_dir / "calc" / "server.py").exists()
    assert await forge.call(BOB, "calc", "add", {"a": 2, "b": 2}) == ("4", False)
    status_events = [e["data"] for e in events(swarm) if e["type"] == "mcp.status"]
    assert {"name": "calc", "status": "active"} in status_events
    with pytest.raises(ToolError, match="not waiting for approval"):
        await forge.approve(USER, "calc", True)

    # rejection
    await forge.create(BOB, "textkit", "String utilities", TEXTKIT)
    text = await forge.approve(USER, "textkit", False, "too risky")
    assert "rejected" in text
    server = forge.get("textkit")
    assert server["status"] == "rejected" and "too risky" in server["error"]
    assert ("n_mcp_rejected", {"name": "textkit", "comment": "too risky"}) in swarm.notifications_for(BOB)
    with pytest.raises(ToolError, match="rejected by the user"):
        await forge.call(BOB, "textkit", "upper", {"text": "a"})
    assert not (swarm.config.library_dir / "textkit").exists()
    with pytest.raises(ToolError, match="Unknown MCP server"):
        await forge.approve(USER, "nothing_here", True)

    # servers built by the user need no approval
    await forge.create(USER, "usertool", "User tool", SERVER_TEMPLATE)
    assert forge.get("usertool")["status"] == "active"


async def test_approval_respects_save_to_library_flag(forge_factory):
    swarm, forge = await forge_factory(mcp={"require_approval": True})
    await forge.create(ALICE, "calc", "Arithmetic helpers", SERVER_TEMPLATE, save_to_library=False)
    text = await forge.approve(USER, "calc", True)
    assert "library" not in text
    assert not (swarm.config.library_dir / "calc").exists()


async def test_library_save_and_import(forge_factory):
    swarm1, forge1 = await forge_factory(subdir="p1")
    await forge1.create(ALICE, "calc", "Arithmetic helpers", CALC)
    await forge1.create(ALICE, "nolib", "Not shared", SERVER_TEMPLATE, save_to_library=False)
    library = swarm1.config.library_dir
    assert (library / "calc" / "server.py").read_text("utf-8") == CALC
    meta = json.loads((library / "calc" / "meta.json").read_text("utf-8"))
    assert meta["owner"] == ALICE and meta["description"] == "Arithmetic helpers"
    assert "add" in [t["name"] for t in meta["tools"]]
    assert not (library / "nolib").exists()

    swarm2, forge2 = await forge_factory(subdir="p2", members=(BOB, CAROL))
    assert swarm2.config.library_dir == library
    listed = forge2.list(BOB)
    assert [s["name"] for s in listed] == ["calc"]
    entry = listed[0]
    assert set(entry) == SERVER_KEYS
    assert entry["origin"] == "library" and entry["status"] == "available" and "add" in entry["tools"]
    assert forge2.get("calc")["status"] == "available"
    assert forge2.list(BOB, "arithmetic") and not forge2.list(BOB, "nothing matches")

    # an agent cannot shadow a library server with its own code
    with pytest.raises(ToolError, match="library already has"):
        await forge2.create(CAROL, "calc", "Other calc", TEXTKIT)

    # using it imports it into the project first
    tools = await forge2.tools(BOB, "calc")
    assert "add" in [t["name"] for t in tools]
    imported = forge2.get("calc")
    assert imported["origin"] == "library" and imported["status"] == "active" and imported["owner"] == ALICE
    assert (swarm2.config.workspace_dir / "mcp" / "calc" / "server.py").read_text("utf-8") == CALC
    created = [e for e in events(swarm2) if e["type"] == "mcp.created"]
    assert len(created) == 1 and created[0]["data"]["server"]["origin"] == "library"
    assert await forge2.call(CAROL, "calc", "add", {"a": 20, "b": 22}) == ("42", False)
    assert [s["origin"] for s in forge2.list(BOB)] == ["library"]  # no duplicate entry

    # imported via call() directly in a third project
    swarm3, forge3 = await forge_factory(subdir="p3", members=(DAVE,))
    assert await forge3.call(DAVE, "calc", "add", {"a": 1, "b": 1}) == ("2", False)


async def test_library_import_respects_require_approval(forge_factory):
    swarm1, forge1 = await forge_factory(subdir="p1")
    await forge1.create(ALICE, "calc", "Arithmetic helpers", CALC)
    swarm2, forge2 = await forge_factory(subdir="p2", members=(BOB,), mcp={"require_approval": True})
    with pytest.raises(ToolError):
        await forge2.tools(BOB, "calc")
    assert forge2.get("calc")["status"] == "pending"
    assert ("user", "u_mcp_pending") in [(r, k) for r, k, _ in swarm2.notified]
    await forge2.approve(USER, "calc", True)
    assert "add" in [t["name"] for t in await forge2.tools(BOB, "calc")]


async def test_user_can_replace_library_server(forge_factory):
    swarm1, forge1 = await forge_factory(subdir="p1")
    await forge1.create(ALICE, "calc", "Arithmetic helpers", CALC)
    swarm2, forge2 = await forge_factory(subdir="p2")
    await forge2.create(USER, "calc", "User calc", CALC_V2)
    assert forge2.get("calc")["origin"] == "workspace"
    meta = json.loads((swarm2.config.library_dir / "calc" / "meta.json").read_text("utf-8"))
    assert meta["owner"] == USER and meta["description"] == "User calc"


async def test_external_config_server(forge_factory, tmp_path: Path, monkeypatch):
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "server.py").write_text(EXTERNAL, encoding="utf-8")
    monkeypatch.setenv("DT_FORGE_EXT_TOKEN", "sekret")
    external = {
        "ext-calc": {"command": [sys.executable, "server.py"], "cwd": str(ext_dir),
                     "description": "External calculator", "env": {"CALC_OFFSET": "10"}},
        "Broken": {"command": ["definitely-not-a-real-command-xyz"], "description": "Missing binary"},
        "invalid": {"description": "no command"},
    }
    swarm, forge = await forge_factory(mcp={"external": external})

    server = forge.get("ext-calc")
    assert server["origin"] == "config" and server["owner"] == USER and server["scope"] == "swarm"
    assert server["command"] == [sys.executable, "server.py"]
    assert server["status"] == "active" and server["tools"] == [] and server["description"] == "External calculator"
    assert forge.get("broken")["origin"] == "config"
    assert forge.get("invalid") is None
    assert forge.connection("ext-calc") is None  # lazy
    assert {s["name"] for s in forge.list(BOB)} == {"ext-calc", "broken"}
    assert len([e for e in events(swarm) if e["type"] == "mcp.created"]) == 2

    assert await forge.call(BOB, "ext-calc", "offset_add", {"a": 5}) == ("15", False)
    assert await forge.call(BOB, "ext-calc", "env", {"key": "DT_FORGE_EXT_TOKEN"}) == ("<unset>", False)
    assert set(forge.get("ext-calc")["tools"]) == {"offset_add", "env"}  # discovered on first use
    assert {"name": "ext-calc", "status": "active"} in [e["data"] for e in events(swarm) if e["type"] == "mcp.status"]

    with pytest.raises(ToolError, match="failed to start"):
        await forge.call(BOB, "broken", "x", {})
    assert forge.get("broken")["status"] == "active" and "failed to start" in forge.get("broken")["error"]

    # config servers cannot be replaced or removed by agents
    swarm.config.mcp.external["extcalc"] = external["ext-calc"]
    await forge.start()
    with pytest.raises(ToolError, match="from dreamteam.toml"):
        await forge.create(ALICE, "extcalc", "Mine", SERVER_TEMPLATE)
    with pytest.raises(ToolError, match="dreamteam.toml"):
        await forge.remove(USER, "extcalc")

    # restarting is idempotent; servers dropped from the config disappear
    await forge.start()
    assert len([e for e in events(swarm) if e["type"] == "mcp.created"]) == 3
    swarm.config.mcp.external = {"extcalc": external["ext-calc"]}
    await forge.start()
    assert forge.get("ext-calc") is None and forge.get("broken") is None
    assert forge.connection("ext-calc") is None
    removed = [e["data"]["name"] for e in events(swarm)
               if e["type"] == "mcp.status" and e["data"]["status"] == "removed"]
    assert sorted(removed) == ["broken", "ext-calc"]


async def test_external_name_clash_keeps_agent_server(forge_factory, tmp_path: Path):
    swarm, forge = await forge_factory()
    await forge.create(ALICE, "calc", "Arithmetic helpers", SERVER_TEMPLATE)
    swarm.config.mcp.external = {"calc": {"command": [sys.executable, "other.py"]}}
    await forge.start()
    server = forge.get("calc")
    assert server["origin"] == "workspace" and server["owner"] == ALICE


async def test_stop_terminates_processes(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    await forge.create(ALICE, "textkit", "String utilities", TEXTKIT)
    pid = int((await forge.call(BOB, "calc", "pid", {}))[0])
    assert pid_alive(pid)
    conns = [forge.connection("calc"), forge.connection("textkit")]
    assert all(c is not None and c.alive for c in conns)
    await forge.stop()
    assert all(not c.alive and c._task.done() for c in conns)
    assert forge.connection("calc") is None
    assert await wait_dead(pid)
    with pytest.raises(ToolError, match="stopped"):
        await forge.call(BOB, "calc", "add", {"a": 1, "b": 1})
    # a new start() makes the forge usable again
    await forge.start()
    assert await forge.call(BOB, "calc", "add", {"a": 1, "b": 1}) == ("2", False)


async def test_stop_during_lazy_start(env):
    swarm, forge = env
    slow_start = "import time\ntime.sleep(2)\n" + SERVER_TEMPLATE
    await forge.create(ALICE, "slowstart", "Starts slowly", slow_start)
    await forge.connection("slowstart").close()
    pending = asyncio.create_task(forge.call(BOB, "slowstart", "add", {"a": 1, "b": 1}))
    await asyncio.sleep(0.5)
    await forge.stop()
    with pytest.raises(ToolError, match="stopped"):
        await pending
    assert forge.connection("slowstart") is None


async def test_restart_after_process_death(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    pid = int((await forge.call(BOB, "calc", "pid", {}))[0])
    os.kill(pid, signal.SIGTERM)
    assert await wait_dead(pid)
    assert await forge.call(BOB, "calc", "add", {"a": 1, "b": 2}) == ("3", False)
    new_pid = int((await forge.call(BOB, "calc", "pid", {}))[0])
    assert new_pid != pid

    await forge.create(ALICE, "flaky", "Crashes once", FLAKY)
    assert await forge.call(BOB, "flaky", "flaky", {}) == ("recovered", False)
    text, is_error = await forge.call(BOB, "flaky", "die", {})
    assert is_error and "crashed again after a restart" in text
    assert forge.connection("flaky") is None
    assert await forge.call(BOB, "flaky", "flaky", {}) == ("recovered", False)


async def test_call_timeout_restarts_server(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    pid = int((await forge.call(BOB, "calc", "pid", {}))[0])
    text, is_error = await forge.call(BOB, "calc", "slow", {"seconds": 30}, timeout=1.5)
    assert is_error and "timed out after 1.5s" in text
    assert await wait_dead(pid)
    assert await forge.call(BOB, "calc", "add", {"a": 1, "b": 2}) == ("3", False)


async def test_call_uses_configured_timeout_by_default(forge_factory):
    swarm, forge = await forge_factory(mcp={"call_timeout": 1.5})
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    text, is_error = await forge.call(BOB, "calc", "slow", {"seconds": 30})
    assert is_error and "timed out after 1.5s" in text
    assert await forge.call(BOB, "calc", "slow", {"seconds": 0.1}) == ("slept", False)


async def test_concurrent_use_shares_one_process(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    await forge.connection("calc").close()  # force a lazy restart, raced by several callers
    results = await asyncio.gather(*(forge.call(m, "calc", "pid", {}) for m in (ALICE, BOB, CAROL, DAVE, USER)))
    assert len({text for text, _ in results}) == 1
    tools = await asyncio.gather(forge.tools(BOB, "calc"), forge.tools(CAROL, "calc"))
    assert tools[0] == tools[1]


async def test_list_query_filtering(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    await forge.create(BOB, "textkit", "String utilities", TEXTKIT)
    assert [s["name"] for s in forge.list(CAROL)] == ["calc", "textkit"]
    assert [s["name"] for s in forge.list(CAROL, "ARITH")] == ["calc"]
    assert [s["name"] for s in forge.list(CAROL, "upper")] == ["textkit"]  # tool names match
    assert [s["name"] for s in forge.list(CAROL, "uppercase string")] == ["textkit"]  # all terms must match
    assert [s["name"] for s in forge.list(CAROL, "two numbers")] == ["calc"]  # tool descriptions match
    assert forge.list(CAROL, "missing") == []
    assert all(set(s) == SERVER_KEYS for s in forge.list(CAROL))


async def test_remove(env):
    swarm, forge = env
    await forge.create(ALICE, "calc", "Arithmetic helpers", CALC)
    conn = forge.connection("calc")
    with pytest.raises(ToolError, match="Only the owner"):
        await forge.remove(BOB, "calc")
    text = await forge.remove(ALICE, "calc")
    assert "removed" in text and "also removed from the library" in text
    assert forge.get("calc") is None and not conn.alive
    assert not (swarm.config.workspace_dir / "mcp" / "calc").exists()
    assert not (swarm.config.library_dir / "calc").exists()
    assert {"name": "calc", "status": "removed"} in [e["data"] for e in events(swarm) if e["type"] == "mcp.status"]
    with pytest.raises(ToolError, match="Unknown MCP server"):
        await forge.remove(ALICE, "calc")


async def test_remove_library_copies(forge_factory):
    swarm1, forge1 = await forge_factory(subdir="p1")
    await forge1.create(ALICE, "calc", "Arithmetic helpers", SERVER_TEMPLATE)
    library = swarm1.config.library_dir
    swarm2, forge2 = await forge_factory(subdir="p2")
    await forge2.tools(BOB, "calc")  # import
    with pytest.raises(ToolError, match="Only the owner"):
        await forge2.remove(BOB, "calc")

    # the owner's name in another project: the local copy goes, the shared one stays
    text = await forge2.remove(ALICE, "calc")
    assert "library copy remains" in text
    assert (library / "calc" / "server.py").exists()
    assert forge2.get("calc")["status"] == "available"

    # library-only entries can be removed by the user only
    with pytest.raises(ToolError, match="only the user can remove it"):
        await forge2.remove(BOB, "calc")
    assert "removed from the library" in await forge2.remove(USER, "calc")
    assert not (library / "calc").exists() and forge2.get("calc") is None
    assert forge1.get("calc")["status"] == "active"  # the original project keeps its own copy


def test_crash_details_of_a_rich_traceback(tmp_path):
    """MCPServer logs through rich (a DreamTeam dependency): an 80-column boxed traceback wrapped 'The above
    exception was the direct cause' and padded every line, so the crash details lost the original error."""
    import io
    import logging

    from rich.console import Console
    from rich.logging import RichHandler

    buf = io.StringIO()
    logger = logging.getLogger("dreamteam-test-rich")
    logger.propagate = False
    handler = RichHandler(console=Console(file=buf, width=80), rich_tracebacks=True)
    logger.addHandler(handler)
    try:
        try:
            try:
                raise ValueError("kaboom boom")
            except ValueError as exc:
                raise RuntimeError("Error executing tool crash") from exc
        except RuntimeError:
            logger.exception("Error executing tool crash")
    finally:
        logger.removeHandler(handler)
    path = tmp_path / "mcp-calc.log"
    path.write_text(buf.getvalue(), encoding="utf-8")
    details = mcp_forge._crash_details(path, 0)
    assert "ValueError: kaboom boom" in details and "RuntimeError" not in details
    assert all(line == line.rstrip() for line in details.splitlines()) and len(details) < 1500
    assert mcp_forge.MCPConnection("x", ["x"], tmp_path, {}, path).env["COLUMNS"] == mcp_forge.LOG_COLUMNS
