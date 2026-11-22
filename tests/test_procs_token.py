"""Liveness by the process token on Linux (docs/SPEC.md §7 "Process layer", §7.1 origins): GNU `timeout` without
`--foreground` and `setsid` start a new process group, so `nohup timeout N cmd &` and `setsid cmd &` leave the
command's group empty while their processes run. The tree finds them by DREAMTEAM_PROCESS_TOKEN: it stays alive
while one runs, lists them, sees their end at once (the kept scan rechecks only the pids it found) and signals them
with terminate() and kill(); `alive_all` scans once for every such tree; a command that does not escape is decided
by its group as before. JobManager.run keeps such a command as a `detached` job (`background` with
background=true) whose end sends its notice.

Linux only (skipped elsewhere), except the uid rule, a pure function. Every process test checks at its end that no
process carrying one of its tokens is left (a leftover is killed and fails the test)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import signal
import sys
import time

import pytest

from dreamteam import procs
from dreamteam.shell import start_command
from test_jobs import ANNA, job_notices, row, run, until
from test_jobs import fast, kids, setup  # noqa: F401 - fixtures
from test_procs import wait_until

LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux /proc: the token is read from /proc/<pid>/environ")
NOHUP = "nohup timeout {t} sleep {s} &"  # timeout leaves the group (setpgid), sleep runs in timeout's group
SETSID = "setsid sleep {s} &"
ESCAPES = [pytest.param(NOHUP, id="nohup-timeout"), pytest.param(SETSID, id="setsid")]


def _needs(*programs: str) -> None:
    for program in programs:
        if not shutil.which(program):
            pytest.skip(f"{program} is not installed")


@pytest.fixture
def tokens():
    """The tokens of the test's commands; at the end none may still be carried by a live process."""
    made: list[str] = []
    yield made
    left = procs.token_pids(made)
    for token in left:
        procs.sweep_token(token)
    assert left == {}, "processes carrying a test token were left running"


@pytest.fixture
def marks():
    """Unique `sleep` lengths (mark(60) -> "60.0<6 digits>"); at the end a process still sleeping one is killed: a
    command that did not become a job (a failing test) left no token in `tokens`. Torn down before `tokens`."""
    made: list[bytes] = []

    def mark(seconds: float) -> str:
        made.append(f"{seconds:.1f}{secrets.randbelow(10 ** 6):06d}".encode())
        return made[-1].decode()

    yield mark
    for pid in procs._proc_pids():
        with contextlib.suppress(OSError):
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
            if any(m in argv for m in made):
                os.kill(int(pid), signal.SIGKILL)


async def escaped(tmp_path, tokens: list[str], template: str, *, t: int = 10, s: float = 2):
    """A command that leaves its group, started through `sh` with a token of its own; returns once its shell has
    exited and its group is empty."""
    _needs("timeout", "setsid", "nohup", "sleep")
    token = "tok" + os.urandom(8).hex()
    tokens.append(token)
    cmd = await start_command(template.format(t=t, s=s), cwd=tmp_path, program="sh", env=dict(os.environ),
                              token=token)
    assert await cmd.wait_exit(10)
    # the background child calls setsid (or timeout setpgid) a moment after the shell may have exited
    assert wait_until(lambda: not procs.live_groups([cmd.tree.pgid]), 3)
    return cmd


def counting(monkeypatch) -> list[list[str]]:
    """Every token scan the trees ask for (procs.token_pids), with its tokens."""
    calls: list[list[str]] = []
    real = procs.token_pids

    def scan(tokens):
        tokens = list(tokens)
        calls.append(tokens)
        return real(tokens)

    monkeypatch.setattr(procs, "token_pids", scan)
    return calls


def test_the_uid_rule():
    """The hub's own processes; a hub running as root (the Docker sandbox's offline user) looks at every uid."""
    assert procs._uid_ok(1000, 1000) and procs._uid_ok(0, 0)
    assert not procs._uid_ok(1001, 1000) and not procs._uid_ok(0, 1000)
    assert procs._uid_ok(1001, 0) and procs._uid_ok(1000, 0)


@linux_only
@pytest.mark.parametrize("template", ESCAPES)
async def test_an_escaped_command_keeps_its_tree_alive_until_it_ends(tmp_path, tokens, template):
    cmd = await escaped(tmp_path, tokens, template, s=2)
    try:
        tree = cmd.tree
        assert not tree._shell_running() and tree.alive() and procs.alive_all([tree]) == [True]
        found = procs.token_pids([tree.token])[tree.token]
        assert tree.pids() == found and tree.pid not in found
        names = tree.processes()
        assert "sleep" in names and ("timeout" in names) == (template == NOHUP)
        assert wait_until(lambda: not tree.alive(), 8)
        assert procs.alive_all([tree]) == [False] and tree.pids() == [] and tree.processes() == []
        assert procs.token_pids([tree.token]) == {}
    finally:
        cmd.tree.kill()
        cmd.close()


@linux_only
async def test_an_end_is_seen_at_once_and_a_reused_pid_does_not_count(tmp_path, tokens, monkeypatch):
    """The kept scan rechecks only the pids it found (no new scan while one of them lives); when none of them carries
    the token any more (it ended, or its pid now names another process), a new scan decides."""
    monkeypatch.setattr(procs, "TOKEN_SCAN_SECONDS", 600.0)
    cmd = await escaped(tmp_path, tokens, SETSID, s=30)
    calls = counting(monkeypatch)
    try:
        tree = cmd.tree
        assert tree.alive() and len(calls) == 1
        assert all(tree.alive() for _ in range(5)) and procs.alive_all([tree]) == [True] and len(calls) == 1
        [pid] = tree._token_seen[1]
        tree._token_seen = (time.monotonic(), [os.getpid()])  # a pid given to a process of no command of this tree
        assert tree.alive() and len(calls) == 2 and tree._token_seen[1] == [pid]
        os.kill(pid, signal.SIGKILL)  # the test's own child
        assert wait_until(lambda: not tree.alive(), 2)  # long before TOKEN_SCAN_SECONDS
    finally:
        cmd.tree.kill()
        cmd.close()


@linux_only
async def test_alive_all_scans_once_for_every_escaped_tree(tmp_path, tokens, monkeypatch):
    """Two escaped trees, a tree whose group still runs (`sleep &`) and a finished one: one token scan, for the
    trees whose group is gone; the next pass decides from the kept scans alone."""
    first = await escaped(tmp_path, tokens, NOHUP, s=20)
    second = await escaped(tmp_path, tokens, SETSID, s=20)
    plain_token, done_token = "plain" + os.urandom(8).hex(), "done" + os.urandom(8).hex()
    tokens.extend([plain_token, done_token])
    plain = await start_command("sleep 20 &", cwd=tmp_path, program="sh", env=dict(os.environ), token=plain_token)
    done = await start_command("true", cwd=tmp_path, program="sh", env=dict(os.environ), token=done_token)
    cmds = [first, second, plain, done]
    try:
        assert await plain.wait_exit(10) and await done.wait_exit(10)
        assert wait_until(lambda: not procs.live_groups([done.tree.pgid]), 3)
        calls = counting(monkeypatch)
        assert procs.alive_all(c.tree for c in cmds) == [True, True, True, False]
        assert len(calls) == 1 and set(calls[0]) == {first.tree.token, second.tree.token, done_token}
        assert procs.alive_all(c.tree for c in cmds) == [True, True, True, False] and len(calls) == 1
    finally:
        for c in cmds:
            c.tree.kill()
            c.close()


@linux_only
@pytest.mark.parametrize("stop", ["terminate", "kill"])
@pytest.mark.parametrize("template", ESCAPES)
async def test_terminate_and_kill_leave_no_token_process(tmp_path, tokens, template, stop):
    cmd = await escaped(tmp_path, tokens, template, t=60, s=60)
    try:
        tree = cmd.tree
        assert tree.alive()
        getattr(tree, stop)()
        assert wait_until(lambda: not procs.token_pids([tree.token]), 5), f"{stop} left {tree.processes()}"
        assert wait_until(lambda: not tree.alive(), 3)
    finally:
        cmd.tree.kill()
        cmd.close()


@linux_only
async def test_a_command_that_does_not_escape_is_decided_by_its_group(tmp_path, tokens, monkeypatch):
    """`sleep &` keeps its group: alive() never scans for the token while the group runs, and once the group is gone
    one scan finding nothing ends it. A tree without a token never scans."""
    _needs("sleep")
    token = "grp" + os.urandom(8).hex()
    tokens.append(token)
    calls = counting(monkeypatch)
    cmd = await start_command("sleep 1.5 &", cwd=tmp_path, program="sh", env=dict(os.environ), token=token)
    bare = await start_command("sleep 1.5 &", cwd=tmp_path, program="sh", env=dict(os.environ))
    try:
        assert await cmd.wait_exit(10) and await bare.wait_exit(10)
        assert cmd.tree.alive() and procs.alive_all([cmd.tree, bare.tree]) == [True, True] and calls == []
        assert wait_until(lambda: not cmd.tree.alive() and not bare.tree.alive(), 6)
        assert all(call == [token] for call in calls) and calls
        assert procs.alive_all([cmd.tree, bare.tree]) == [False, False]
    finally:
        for c in (cmd, bare):
            c.tree.kill()
            c.close()


@linux_only
async def test_sweep_token_follows_the_uid_rule(tmp_path, tokens, monkeypatch):
    """Seen as another user's hub, the command's processes are left alone; seen as root's, they are found."""
    cmd = await escaped(tmp_path, tokens, SETSID, s=30)
    uid = os.getuid()
    try:
        token = cmd.tree.token
        monkeypatch.setattr(procs.os, "getuid", lambda: uid + 1 if uid else 1)
        assert procs.token_pids([token]) == {} and procs.sweep_token(token) == 0
        monkeypatch.setattr(procs.os, "getuid", lambda: 0)
        assert procs.token_pids([token])[token] and procs.sweep_token(token) == 1
        monkeypatch.setattr(procs.os, "getuid", lambda: uid)
        assert wait_until(lambda: not procs.token_pids([token]), 3)
    finally:
        cmd.tree.kill()
        cmd.close()


@linux_only
async def test_an_empty_environment_read_is_read_again(tmp_path, tokens, monkeypatch):
    """A process in execve reads an empty /proc/<pid>/environ for a moment: the scan, the kept list's recheck and the
    sweep read it again instead of missing it (a miss would end the tree, or leave the process running)."""
    monkeypatch.setattr(procs, "TOKEN_SCAN_SECONDS", 600.0)
    cmd = await escaped(tmp_path, tokens, SETSID, s=30)
    try:
        tree, token = cmd.tree, cmd.tree.token
        [pid] = procs.token_pids([token])[token]
        real, blanked = procs._environ, set()

        def once_empty(p: str) -> bytes | None:
            if p == str(pid) and p not in blanked:
                blanked.add(p)
                return b""
            return real(p)

        monkeypatch.setattr(procs, "_environ", once_empty)
        calls = counting(monkeypatch)
        assert tree.alive() and tree._token_seen[1] == [pid] and len(calls) == 1  # the scan
        blanked.clear()
        assert tree.alive() and len(calls) == 1  # the recheck of the kept list
        blanked.clear()
        assert procs.token_pids([token]) == {token: [pid]}
        blanked.clear()
        assert procs.sweep_token(token) == 1
        assert wait_until(lambda: not procs.token_pids([token]), 3)
    finally:
        cmd.tree.kill()
        cmd.close()


@linux_only
async def test_a_listing_while_the_group_runs_does_not_hide_a_later_escape(tmp_path, tokens):
    """pids() scans afresh and keeps nothing: an empty answer while the group runs (here its processes hide the token)
    does not make alive() say False once the group is gone and a process outside it carries the token."""
    _needs("env", "setsid", "sleep")
    token = "late" + os.urandom(8).hex()
    tokens.append(token)
    env = procs.TOKEN_ENV
    script = f"exec env -u {env} sh -c 'sleep 0.6; {env}={token} setsid sleep 4 &'"
    cmd = await start_command(script, cwd=tmp_path, program="sh", env=dict(os.environ), token=token)
    try:
        tree = cmd.tree
        await asyncio.sleep(0.2)
        assert tree._shell_running() and tree.pids() and not procs.token_pids([token])
        assert await cmd.wait_exit(10)
        assert wait_until(lambda: not procs.live_groups([tree.pgid]), 3)
        assert wait_until(lambda: procs.token_pids([token]), 3)
        assert tree.alive() and procs.alive_all([tree]) == [True]
    finally:
        cmd.tree.kill()
        cmd.close()


# --- jobs (docs/SPEC.md §7.1 origins) -----------------------------------------------------------------------------

@linux_only
async def test_nohup_timeout_becomes_a_detached_job_whose_end_is_noticed(setup, tokens, marks):
    _needs("timeout", "nohup", "sleep")
    swarm, manager = setup
    out = await run(manager, ANNA, f"nohup timeout 30 sleep {marks(1.5)} & echo started", timeout=30)
    if out.job:
        tokens.append(out.job.cmd.tree.token)
    assert out.state == "detached" and out.code == 0 and "started" in out.output
    job = out.job
    # the names come from a new token scan at adoption: timeout's child, started outside the group, is listed
    assert job.origin == "detached" and {"timeout", "sleep"} <= set(out.names)
    assert out.render(cfg=swarm.config.shell).startswith("exit code: 0 [sh]; processes it started keep running as "
                                                         f"background job j{job.id}")
    assert "sleep" in job.public()["processes"]
    await until(lambda: row(swarm, job.id).get("status") == "ended" and job_notices(swarm, ANNA), 15)
    [(kind, quiet, text)] = job_notices(swarm, ANNA)
    assert kind == "n_job_done" and quiet  # a detached job without an active goal: shown at the next wake-up
    assert text.startswith(f"Your background job j{job.id} has ended (all its processes exited) after running ")


@linux_only
async def test_setsid_with_background_becomes_a_background_job(setup, tokens, marks):
    _needs("setsid", "sleep")
    swarm, manager = setup
    out = await run(manager, ANNA, f"setsid sleep {marks(1.5)} > /dev/null 2>&1 &", background=True)
    if out.job:
        tokens.append(out.job.cmd.tree.token)
    assert out.state == "started" and out.job.origin == "background" and out.job.command_done
    assert "sleep" in out.names
    await until(lambda: row(swarm, out.job.id).get("status") == "ended" and job_notices(swarm, ANNA), 15)
    [(kind, quiet, _)] = job_notices(swarm, ANNA)
    assert kind == "n_job_done" and not quiet  # a background job's end wakes its owner (no goal yet)


@linux_only
async def test_job_stop_ends_an_escaped_job(setup, tokens, marks):
    _needs("setsid", "sleep")
    swarm, manager = setup
    out = await run(manager, ANNA, f"setsid sleep {marks(60)} &", timeout=30)
    if out.job:
        tokens.append(out.job.cmd.tree.token)
    assert out.state == "detached"
    token = out.job.cmd.tree.token
    assert "Stopped job j1" in await manager.stop(out.job.id, ANNA, force=True)
    assert wait_until(lambda: not procs.token_pids([token]), 3)
    assert row(swarm, out.job.id)["status"] != "running"
