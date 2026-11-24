"""A hub running as root reads the environment of another user's command under that command's own ids (docs/SPEC.md
§7 "Process layer"): when the kernel refuses root /proc/<pid>/environ (Docker's default capabilities: no
CAP_SYS_PTRACE, and the commands of a session without network run under `setpriv --reuid`), procs._environ opens it
once more with the file-system gid and uid of a short-lived thread of its own set to the process's (setfsgid, then
setfsuid) and sets them back at once (uid, then gid), also when the open fails, then the hub's dumpable flag, which
each change of the ids clears. Only a process whose real, effective and saved uids are one value, its gids too, and
not uid 0, is opened so; a switch that does not take counts as refused; ids that do not come back end that thread
and every later switch.

The rules run everywhere with fakes (a libc that records its calls, the plain open refused, the status read). On
Linux the real libc is loaded; as root with setpriv a real switch runs (WSL's root reads the file without it, so the
refusal is forced there). The real refusal is the Docker sandbox image's (opt-in: DREAMTEAM_DOCKER_TESTS=1, it builds
an image under a tag of its own and removes it): as root with Docker's default capabilities, a command started like
an offline one, whose processes left its group, is alive, listed and killed through its token."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from dreamteam import procs

LINUX = sys.platform.startswith("linux")
QUERY = 0xFFFFFFFF
PID = "4242"
UID, GID = 10001, 10002
DATA = b"PATH=/usr/bin\0DREAMTEAM_PROCESS_TOKEN=abc\0"


def status(uids: tuple[int, ...] = (UID,) * 4, gids: tuple[int, ...] = (GID,) * 4) -> bytes:
    return ("Name:\tsleep\nUmask:\t0022\nState:\tS (sleeping)\nUid:\t" + "\t".join(map(str, uids))
            + "\nGid:\t" + "\t".join(map(str, gids)) + "\nGroups:\t\n").encode()


class FakeLibc:
    """setfsuid/setfsgid of one thread: a call returns the old id; a value in `refused` leaves it; QUERY asks. A
    change of an id makes the process not dumpable, as the kernel does (fs.suid_dumpable 0); prctl reads and sets
    that flag (PR_SET_DUMPABLE fails when `dumpable_refused`)."""

    def __init__(self, refused: tuple[tuple[str, int], ...] = ()) -> None:
        self.ids = {"uid": 0, "gid": 0}
        self.calls: list[tuple[str, int]] = []
        self.refused = set(refused)
        self.dumpable = 1
        self.dumpable_sets: list[int] = []
        self.dumpable_refused = False

    def _set(self, kind: str, value: int) -> int:
        old = self.ids[kind]
        if value != QUERY:
            self.calls.append((kind, value))
            if (kind, value) not in self.refused:
                self.ids[kind] = value
                if value != old:
                    self.dumpable = 0
        return old

    def prctl(self, option: int, arg2: int, *rest: int) -> int:
        if option == 3:   # PR_GET_DUMPABLE
            return self.dumpable
        assert option == 4 and arg2 in (0, 1), (option, arg2)   # PR_SET_DUMPABLE
        self.dumpable_sets.append(arg2)
        if self.dumpable_refused:
            return -1
        self.dumpable = arg2
        return 0

    def setfsuid(self, value: int) -> int:
        return self._set("uid", value)

    def setfsgid(self, value: int) -> int:
        return self._set("gid", value)


class Hub:
    """The fakes of one test: what the plain open, the status read and the second open do, and what they saw."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, libc: FakeLibc) -> None:
        self.libc = libc
        self.plain: Exception | bytes = PermissionError(13, "Permission denied")
        self.status: bytes | None = status()
        self.second: Exception | bytes = DATA
        self.opened: list[dict[str, int]] = []   # the libc's ids at each second open
        self.threads: list[threading.Thread] = []   # the thread of each second open
        self.status_reads = 0
        monkeypatch.setattr(procs, "open", self.open, raising=False)
        monkeypatch.setattr(procs, "_open_fd", self.open_fd)
        monkeypatch.setattr(procs, "_libc", lambda: libc)
        monkeypatch.setattr(procs, "_fsids_stuck", False)
        for key in ("fsids-refused", "fsids-dumpable", "fsids-error"):
            procs._warned.discard(key)

    def open(self, path: str, mode: str = "r", *args, **kwargs):
        if path.endswith("/environ"):
            if isinstance(self.plain, Exception):
                raise self.plain
            return io.BytesIO(self.plain)
        if path.endswith("/status"):
            self.status_reads += 1
            if self.status is None:
                raise FileNotFoundError(2, "No such file or directory")
            return io.BytesIO(self.status)
        raise AssertionError(path)

    def open_fd(self, path: str) -> int:
        assert path == f"/proc/{PID}/environ"
        self.opened.append(dict(self.libc.ids))
        self.threads.append(threading.current_thread())
        if isinstance(self.second, Exception):
            raise self.second
        read_end, write_end = os.pipe()
        os.write(write_end, self.second)
        os.close(write_end)
        return read_end


@pytest.fixture
def hub(monkeypatch):
    """A Linux hub running as root, with the fakes."""
    monkeypatch.setattr(procs, "_LINUX", True)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    return Hub(monkeypatch, FakeLibc())


BACK = [("uid", 0), ("gid", 0)]


def test_a_refused_read_is_opened_under_the_process_ids_and_they_are_set_back(hub):
    assert procs._environ(PID) == DATA
    assert hub.opened == [{"uid": UID, "gid": GID}]
    assert hub.libc.calls == [("gid", GID), ("uid", UID), *BACK]   # gid first; back: uid first
    assert hub.libc.ids == {"uid": 0, "gid": 0}
    assert hub.libc.dumpable == 1 and hub.libc.dumpable_sets == [1]   # cleared by the switch, set back after it


def test_the_switch_runs_in_a_thread_of_its_own(hub):
    """Never in the calling thread: Python runs signal handlers in the main thread alone, so none can raise while
    the ids are switched, and no caller's parent-death signal is cleared."""
    procs._environ(PID)
    assert len(hub.threads) == 1 and hub.threads[0] is not threading.current_thread()
    assert hub.threads[0] is not threading.main_thread() and not hub.threads[0].is_alive()


def test_a_hub_that_was_not_dumpable_stays_so(hub):
    hub.libc.dumpable = 0
    assert procs._environ(PID) == DATA
    assert hub.libc.dumpable == 0 and hub.libc.dumpable_sets == []


def test_a_dumpable_flag_that_does_not_come_back_is_logged_once(hub, caplog):
    hub.libc.dumpable_refused = True
    with caplog.at_level(logging.WARNING, logger="dreamteam.procs"):
        assert procs._environ(PID) == DATA
        assert procs._environ(PID) == DATA
    assert hub.libc.ids == {"uid": 0, "gid": 0}
    assert sum("dumpable flag back" in r.getMessage() for r in caplog.records) == 1


def test_the_ids_are_set_back_when_the_open_fails(hub):
    hub.second = PermissionError(13, "Permission denied")
    assert procs._environ(PID) is None
    assert hub.opened == [{"uid": UID, "gid": GID}]
    assert hub.libc.calls == [("gid", GID), ("uid", UID), *BACK]
    assert hub.libc.ids == {"uid": 0, "gid": 0}


def test_the_ids_are_set_back_to_the_threads_own_ones(hub):
    hub.libc.ids = {"uid": 7, "gid": 8}
    assert procs._environ(PID) == DATA
    assert hub.libc.calls == [("gid", GID), ("uid", UID), ("uid", 7), ("gid", 8)]


@pytest.mark.parametrize("uids, gids", [
    pytest.param((UID, 0, UID, UID), (GID,) * 4, id="effective-uid-other"),
    pytest.param((UID, UID, 0, UID), (GID,) * 4, id="saved-uid-other"),
    pytest.param((0, UID, UID, UID), (GID,) * 4, id="real-uid-other"),
    pytest.param((UID,) * 4, (GID, GID, 0, GID), id="saved-gid-other"),
    pytest.param((UID,) * 4, (GID, 5, GID, GID), id="effective-gid-other"),
    pytest.param((0,) * 4, (GID,) * 4, id="uid-0"),
    pytest.param((UID, UID), (GID,) * 4, id="short-line"),
])
def test_mixed_ids_and_uid_0_are_not_opened(hub, uids, gids):
    hub.status = status(uids, gids)
    assert procs._environ(PID) is None
    assert hub.opened == [] and hub.libc.calls == []


def test_a_gone_process_or_an_unreadable_status_is_not_opened(hub):
    hub.status = None
    assert procs._environ(PID) is None
    hub.status = b"Name:\tx\nUid:\tten\t10\t10\t10\nGid:\t1\t1\t1\t1\n"
    assert procs._environ(PID) is None
    assert hub.opened == [] and hub.libc.calls == []


def test_the_fsids_qualify_from_the_status_alone(hub):
    """The fourth field (the process's own file-system id) is not one of the ids the kernel compares."""
    hub.status = status((UID, UID, UID, 0), (GID, GID, GID, 0))
    assert procs._environ(PID) == DATA


@pytest.mark.parametrize("refused, calls", [
    pytest.param((("uid", UID),), [("gid", GID), ("uid", UID), *BACK], id="uid"),
    pytest.param((("gid", GID),), [("gid", GID), *BACK], id="gid"),   # the uid is never switched then
])
def test_a_switch_that_does_not_take_counts_as_refused_and_is_set_back(hub, refused, calls, caplog):
    hub.libc.refused = set(refused)
    with caplog.at_level(logging.WARNING, logger="dreamteam.procs"):
        assert procs._environ(PID) is None
        assert procs._environ(PID) is None
    assert hub.opened == []
    assert hub.libc.calls == calls * 2
    assert hub.libc.ids == {"uid": 0, "gid": 0}
    assert sum("cannot set the file-system ids" in r.getMessage() for r in caplog.records) == 1   # logged once
    assert all(r.thread == threading.get_ident() for r in caplog.records)   # by the caller, the ids set back


@pytest.mark.parametrize("stuck", [pytest.param(("uid", 0), id="uid"), pytest.param(("gid", 0), id="gid")])
def test_ids_that_do_not_come_back_end_their_thread_and_every_later_switch(hub, stuck, caplog):
    """The thread that has them reads nothing and ends; the hub logs it as critical and switches no more."""
    hub.libc.refused = {stuck}
    with caplog.at_level(logging.WARNING, logger="dreamteam.procs"):
        assert procs._environ(PID) is None
    assert hub.opened == [{"uid": UID, "gid": GID}]   # opened, never read
    assert hub.libc.calls == [("gid", GID), ("uid", UID), *BACK, *BACK]   # set back, tried twice
    assert procs._fsids_stuck
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1 and "did not come back" in critical[0].getMessage()
    assert critical[0].thread == threading.get_ident() and hub.threads[0].ident != critical[0].thread
    calls = len(hub.libc.calls)
    assert procs._environ(PID) is None
    assert len(hub.libc.calls) == calls and len(hub.opened) == 1


def test_an_error_in_the_switch_thread_is_logged_by_the_caller(hub, monkeypatch, caplog):
    def broken(path: str) -> int:
        raise ValueError("broken")

    monkeypatch.setattr(procs, "_open_fd", broken)
    with caplog.at_level(logging.WARNING, logger="dreamteam.procs"):
        assert procs._environ(PID) is None
    assert hub.libc.ids == {"uid": 0, "gid": 0} and hub.libc.dumpable == 1
    assert any("broken" in r.getMessage() and r.thread == threading.get_ident() for r in caplog.records)


@pytest.mark.skipif(not hasattr(os, "O_CLOEXEC"), reason="POSIX: O_CLOEXEC")
def test_the_second_open_is_read_only_and_close_on_exec(monkeypatch):
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(os, "open", lambda path, flags, *rest: seen.append((path, flags)) or 99)
    assert procs._open_fd("/proc/1/environ") == 99
    assert seen == [("/proc/1/environ", os.O_RDONLY | os.O_CLOEXEC)]


def test_a_hub_not_running_as_root_switches_nothing(hub, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert procs._environ(PID) is None
    assert hub.status_reads == 0 and hub.libc.calls == [] and hub.opened == []


def test_windows_and_macos_switch_nothing(hub, monkeypatch):
    monkeypatch.setattr(procs, "_LINUX", False)   # (sys.platform is read once, at import)
    assert procs._environ(PID) is None
    assert hub.status_reads == 0 and hub.libc.calls == [] and hub.opened == []


def test_a_plain_read_that_works_switches_nothing(hub):
    hub.plain = DATA
    assert procs._environ(PID) == DATA
    assert hub.status_reads == 0 and hub.libc.calls == []


def test_another_error_than_a_refusal_switches_nothing(hub):
    hub.plain = FileNotFoundError(2, "No such file or directory")
    assert procs._environ(PID) is None
    assert hub.status_reads == 0 and hub.libc.calls == []


def test_without_libc_the_process_stays_unread(hub, monkeypatch):
    monkeypatch.setattr(procs, "_libc", lambda: None)
    assert procs._environ(PID) is None
    assert hub.opened == []


def test_the_token_check_of_a_kept_pid_goes_through_the_switch(hub, monkeypatch):
    """_carries (the kept scan, alive()) reads through _environ, as the scan does."""
    monkeypatch.setattr(procs, "_may_exec", lambda pid: True)
    assert procs._carries(int(PID), b"DREAMTEAM_PROCESS_TOKEN=abc")
    assert not procs._carries(int(PID), b"DREAMTEAM_PROCESS_TOKEN=other")
    assert hub.libc.calls == [("gid", GID), ("uid", UID), *BACK] * 2


def test_a_command_joins_the_namespace_once_the_hub_is_dumpable_again(monkeypatch):
    """netns_helper: the open of DreamTeam's /proc/<pid>/fd/N, refused while the hub is not dumpable for a moment
    (a switch in another thread), is tried again; a refusal that lasts is still one."""
    netns_helper = pytest.importorskip("dreamteam.netns_helper")   # imports fcntl: not on Windows
    monkeypatch.setattr(netns_helper, "OPEN_PAUSE_SECONDS", 0)
    refusals = [PermissionError(13, "Permission denied")]
    opened: list[str] = []

    def fake_open(path: str, flags: int) -> int:
        opened.append(path)
        if refusals:
            raise refusals.pop()
        return 7

    monkeypatch.setattr(netns_helper.os, "open", fake_open)
    assert netns_helper._open_held("/proc/1/fd/3") == 7 and opened == ["/proc/1/fd/3"] * 2
    refusals[:] = [PermissionError(13, "Permission denied")] * netns_helper.OPEN_TRIES
    with pytest.raises(PermissionError):
        netns_helper._open_held("/proc/1/fd/3")


# --- the real libc and a real switch (Linux) -----------------------------------------------------------------------

def _thread_ids() -> dict[str, str]:
    """The calling thread's file-system uid and gid (the fourth field), its effective capabilities and the
    process's dumpable flag."""
    found: dict[str, str] = {}
    with open("/proc/thread-self/status", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("Uid", "Gid"):
                found[key] = rest.split()[3]
            elif key == "CapEff":
                found[key] = rest.strip()
    found["dumpable"] = str(procs._libc().prctl(3, 0, 0, 0, 0))
    return found


@pytest.mark.skipif(not LINUX, reason="Linux: setfsuid")
def test_the_real_libc_is_loaded_once_and_asks_without_changing():
    libc = procs._libc()
    assert libc is not None and procs._libc() is libc
    before = _thread_ids()
    assert libc.setfsuid(QUERY) == int(before["Uid"]) and libc.setfsgid(QUERY) == int(before["Gid"])
    assert _thread_ids() == before


@pytest.mark.skipif(not LINUX or os.name != "posix" or os.geteuid() != 0 or not shutil.which("setpriv"),
                    reason="Linux, root and setpriv: a real switch of the file-system ids")
def test_a_real_switch_reads_another_users_command_and_sets_the_ids_back(monkeypatch):
    """A command as another user (setpriv --reuid, like an offline command in the sandbox): the plain open is
    refused here by force where root may read it anyway (WSL); the second open runs under the command's ids in
    another thread, and the hub has root's ids, capabilities and dumpable flag again afterwards. The scan finds the
    command by its token."""
    uid = gid = 54321
    token = os.urandom(8).hex()
    env = dict(os.environ, DREAMTEAM_PROCESS_TOKEN=token)
    proc = subprocess.Popen(["setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--", "sleep", "30"],
                            env=env)
    real_open, real_fd = open, procs._open_fd
    during: list[dict[str, str]] = []

    def refusing_open(path, *args, **kwargs):
        if isinstance(path, str) and path == f"/proc/{proc.pid}/environ":
            raise PermissionError(13, "Permission denied (forced)")
        return real_open(path, *args, **kwargs)

    def watched_fd(path: str) -> int:
        during.append({**_thread_ids(), "thread": threading.get_ident()})
        return real_fd(path)

    try:
        deadline = time.monotonic() + 5
        while (procs._owner_ids(str(proc.pid)) or (0, 0))[0] != uid and time.monotonic() < deadline:
            time.sleep(0.01)
        before = _thread_ids()
        monkeypatch.setattr(procs, "open", refusing_open, raising=False)
        monkeypatch.setattr(procs, "_open_fd", watched_fd)
        data = procs._environ(str(proc.pid))
        assert data is not None and f"DREAMTEAM_PROCESS_TOKEN={token}".encode() in data.split(b"\0")
        assert during and during[0]["Uid"] == str(uid) and during[0]["Gid"] == str(gid)
        assert during[0]["thread"] != threading.get_ident()
        assert _thread_ids() == before   # fs ids 0, the file-system capabilities and the dumpable flag as they were
        assert procs.token_pids([token]) == {token: [proc.pid]}
        assert _thread_ids() == before
    finally:
        proc.kill()
        proc.wait()


# --- the Docker sandbox: the real refusal (opt-in) -----------------------------------------------------------------

DOCKER_IMAGE = f"dreamteam-fsids-test:pytest-{os.getpid()}"


def _docker_tests() -> bool:
    if os.environ.get("DREAMTEAM_DOCKER_TESTS") != "1" or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


DOCKER_CHECK = r"""
import asyncio, json, os, secrets, sys, time
from pathlib import Path
from dreamteam import netoff, procs
from dreamteam.shell import start_command


def thread_ids():
    found = {}
    with open("/proc/thread-self/status") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("Uid", "Gid"):
                found[key] = rest.split()[3]
            elif key == "CapEff":
                found[key] = rest.strip()
    found["dumpable"] = procs._libc().prctl(3, 0, 0, 0, 0)
    return found


def until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)
    return check()


def live(pid):
    stat = procs._proc_stat(str(pid))
    return bool(stat) and stat[0] not in ("Z", "X")


def plain(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            f.read()
        return "read"
    except PermissionError:
        return "refused"
    except OSError as exc:
        return f"error {exc.errno}"


def escaped():
    found = []
    for pid in procs._proc_pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if argv[:2] in ([b"sleep", b"31"], [b"timeout", b"32"], [b"sleep", b"32"]) and live(pid):
            found.append(int(pid))
    return sorted(found)


os.environ[netoff.OFFLINE_USER_ENV] = "10001:10001"
before = thread_ids()
token = secrets.token_hex(8)


async def start():
    return await start_command("setsid sleep 31 & nohup timeout 32 sleep 32 & echo started", cwd=Path("/tmp"),
                               token=token, offline=True)


cmd = asyncio.run(start())
tree = cmd.tree
tree.proc.wait(10)
group_gone = until(lambda: not procs.live_groups([tree.pgid]))
until(lambda: len(escaped()) == 3)
pids = escaped()
owners = sorted({procs._owner_ids(str(pid)) for pid in pids})
result = {"kind": netoff.isolation(), "prefix": netoff._detected.get("prefix"), "group_gone": group_gone,
          "escaped": pids, "owners": owners, "plain": sorted({plain(pid) for pid in pids}),
          "alive": tree.alive(), "listed": sorted(set(pids) - set(tree.pids())) == [],
          "names": sorted(tree.processes()), "found": procs.token_pids([token]).get(token, []),
          "ids_after_scan": thread_ids() == before}
tree.kill()
result["left"] = until(lambda: not any(live(pid) for pid in pids)) and procs.token_pids([token]) == {}
result["alive_after_kill"] = not until(lambda: not tree.alive())
result["ids_before"] = before
result["ids_after"] = thread_ids()
tree.release()
print(json.dumps(result))
"""


@pytest.mark.skipif(not _docker_tests(), reason="opt-in: DREAMTEAM_DOCKER_TESTS=1 and a Docker daemon (builds an "
                                                "image under a tag of its own)")
def test_in_the_sandbox_an_offline_commands_escaped_processes_are_tracked(tmp_path):
    """The sandbox image under a test tag, root with Docker's default capabilities (no CAP_SYS_PTRACE): a command
    started as the members' commands of a session without network are (shell.start_command, offline: netoff's
    setpriv prefix) runs `setsid sleep &` and `nohup timeout sleep &`. Root's plain open of their environment is
    refused; the tree is alive with its group gone, lists them and kill() ends them; the thread's file-system ids are
    root's again, and the hub is dumpable again."""
    from dreamteam import docker as docker_module

    (tmp_path / "check.py").write_text(DOCKER_CHECK, encoding="utf-8")
    try:
        docker_module.build_image(DOCKER_IMAGE)
        out = subprocess.run(["docker", "run", "--rm", "-v", f"{tmp_path}:/check:ro", "--entrypoint", "python",
                              DOCKER_IMAGE, "/check/check.py"], capture_output=True, text=True, timeout=300)
        lines = out.stdout.strip().splitlines()
        assert lines, out.stdout + out.stderr
        result = json.loads(lines[-1])
        assert result["kind"] == "docker" and result["prefix"][:2] == ["setpriv", "--reuid=10001"], result
        assert result["group_gone"] and len(result["escaped"]) == 3, result
        assert result["owners"] == [[10001, 10001]], result
        assert result["plain"] == ["refused"], result   # the container lacks CAP_SYS_PTRACE
        assert result["alive"] and result["listed"], result
        assert result["found"] == result["escaped"], result
        assert result["names"] == ["sleep", "sleep", "timeout"], result
        assert result["ids_after_scan"], result
        assert result["left"] and not result["alive_after_kill"], result
        assert result["ids_after"] == result["ids_before"], result
        assert result["ids_after"]["Uid"] == "0" and result["ids_after"]["Gid"] == "0", result
        assert result["ids_after"]["dumpable"] == 1, result
    finally:
        subprocess.run(["docker", "image", "rm", "-f", DOCKER_IMAGE], capture_output=True, timeout=120)
