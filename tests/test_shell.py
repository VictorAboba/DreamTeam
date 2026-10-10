import sys
import time

import pytest

from dreamteam.shell import resolve_shell, run_shell, scrubbed_env, truncate_middle


async def test_echo_and_exit_code(fake_swarm):
    cwd = fake_swarm.config.workspace_dir
    code, out = await run_shell("echo привет", cwd=cwd, timeout=60)
    assert code == 0 and "привет" in out
    code, out = await run_shell("exit 3", cwd=cwd, timeout=60)
    assert code == 3


async def test_timeout_kills(fake_swarm):
    cwd = fake_swarm.config.workspace_dir
    cmd = "Start-Sleep -Seconds 30" if sys.platform == "win32" else "sleep 30"
    started = time.monotonic()
    code, _ = await run_shell(cmd, cwd=cwd, timeout=2)
    assert code is None
    assert time.monotonic() - started < 25


def test_scrubbed_env(fake_swarm, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "y")
    monkeypatch.setenv("HARMLESS", "z")
    env = scrubbed_env(fake_swarm.config, {"DREAMTEAM_AGENT": "A"})
    assert "ANTHROPIC_API_KEY" not in env and "MY_SERVICE_TOKEN" not in env
    assert env["HARMLESS"] == "z" and env["DREAMTEAM_AGENT"] == "A"
    fake_swarm.config.shell.pass_env = ["MY_SERVICE_TOKEN"]
    assert scrubbed_env(fake_swarm.config)["MY_SERVICE_TOKEN"] == "y"


def test_truncate_and_resolve():
    assert truncate_middle("a" * 100, 50).count("skipped") == 1
    name, argv = resolve_shell("auto")
    assert name and argv


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
async def test_cmd_output_is_decoded_with_the_oem_code_page(tmp_path):
    """cmd.exe writes in the console's OEM code page (cp866 on a Russian Windows), not the ANSI one (cp1251)."""
    code, out = await run_shell("echo привет", cwd=tmp_path, timeout=30, program="cmd")
    assert code == 0 and out.strip() == "привет"
