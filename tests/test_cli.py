"""CLI: init, status and the stdio MCP server in a real subprocess."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dreamteam.cli import main
from dreamteam.config import Config


def _init(tmp_path: Path, *extra: str) -> Path:
    project = tmp_path / "swarm"
    assert main(["init", str(project), "--lang", "en", "--name", "CLI test", *extra]) == 0
    text = (project / "dreamteam.toml").read_text(encoding="utf-8")
    text = text.replace("start_agents = 3", "start_agents = 0").replace(
        'library_dir = "~/.dreamteam/library"', f'library_dir = "{(tmp_path / "lib").as_posix()}"')
    (project / "dreamteam.toml").write_text(text, encoding="utf-8")
    return project


def test_init_creates_loadable_project(tmp_path, capsys):
    project = _init(tmp_path, "--provider", "ollama", "--goal", 'Say "hi"')
    cfg = Config.load(project)
    assert cfg.swarm.name == "CLI test" and cfg.swarm.language == "en"
    assert cfg.swarm.goal == 'Say "hi"'
    assert cfg.model(None).provider == "openai" and "11434" in cfg.model(None).base_url
    assert (project / "workspace").is_dir()
    assert main(["init", str(project)]) == 1  # refuses to overwrite


def test_status_without_hub(tmp_path, capsys):
    project = _init(tmp_path)
    capsys.readouterr()
    assert main(["status", str(project)]) == 0
    out = capsys.readouterr().out
    assert "CLI test" in out and "Hub: not running" in out


async def test_stdio_mcp_server_subprocess(tmp_path):
    from mcp import Client, StdioServerParameters, stdio_client

    project = _init(tmp_path)
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONUTF8"] = "1"
    params = StdioServerParameters(command=sys.executable, args=["-m", "dreamteam", "mcp", str(project), "--no-web"],
                                   env=env, cwd=str(tmp_path))
    with open(tmp_path / "stderr.log", "w", encoding="utf-8") as errlog:
        async with Client(stdio_client(params, errlog=errlog)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"set_goal", "board", "join_swarm"} <= names
            res = await client.call_tool("set_goal", {"title": "Test goal over stdio"})
            assert not res.is_error, res.content[0].text
            res = await client.call_tool("board", {})
            assert "Test goal over stdio" in res.content[0].text
