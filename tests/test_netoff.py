"""Sessions without network (docs/SPEC.md §7.2, netoff.py): the environment and the argv wrappers of each isolation
(Docker, a Linux network namespace, the best effort elsewhere) with fakes, their detection, the sandbox's firewall
and its entry, the members' commands, jobs and MCP servers of a session with network off, refused remote MCP
servers and members' skill installs, the members told once, DreamTeam's git without hooks, the settings and the
surfaces (CLI, launcher MCP, Docker argv).

The real isolations are tested too, where they exist: a Linux network namespace (`unshare -rn`, skipped without it)
and the Docker sandbox's firewall (opt-in: DREAMTEAM_DOCKER_TESTS=1, it builds an image under a tag of its own and
removes it). Neither sends anything to the internet: the addresses are TEST-NET ones (RFC 5737)."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import make_skill
from dreamteam import cli, netoff, prompts, skill_deps
from dreamteam import docker as docker_module
from dreamteam import mcp_forge as forge_module
from dreamteam.config import Config
from dreamteam.models import USER, ToolError
from dreamteam.skill_deps import EnvManager, EnvSpec
from dreamteam.tools import ToolContext, call_tool
from test_job_tools import agent_ctx, fast, ok, until  # noqa: F401 - `fast` is a fixture
from test_mcp_forge import forge_factory  # noqa: F401 - a fixture
from test_runtime import call, make_swarm, shutdown

WINDOWS = sys.platform == "win32"
TEST_NET = "192.0.2.1"   # RFC 5737: never routed
ANNA = "Anna Smith"


@pytest.fixture(autouse=True)
def fresh_detection():
    netoff.reset()
    yield
    netoff.reset()


def force(kind: str, prefix: list[str] | None = None, ids: tuple[int, int] = (10001, 10001)) -> None:
    """Pretend this machine's isolation is `kind` (netoff.isolation() then answers it without probing); `ids`: the
    offline user's of "docker"."""
    netoff._detected.update(kind=kind, prefix=list(prefix or []), **({"uid": ids[0], "gid": ids[1]}
                                                                        if kind == "docker" else {}))


def offline_config(tmp_path: Path, **raw) -> Config:
    return Config.from_dict(tmp_path / "project", {"swarm": {"network": "off"}, **raw})


# ---- the environment ---------------------------------------------------------------------------------------
def test_the_environment_points_every_tool_at_nothing_and_keeps_loopback():
    force("env")
    env = {"PATH": "p", "NO_PROXY": "corp.example", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.x",
           "GIT_CONFIG_VALUE_0": "y"}
    out = netoff.environment(env)
    assert env["NO_PROXY"] == "corp.example" and "HTTP_PROXY" not in env   # the caller's dict is untouched
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        assert out[name] == out[name.lower()] == "http://127.0.0.1:9"
    assert out["NO_PROXY"] == out["no_proxy"] == "localhost,127.0.0.1,::1"   # local servers keep working
    assert out["PIP_NO_INDEX"] == "1" and out["UV_OFFLINE"] == "1" and out["npm_config_offline"] == "true"
    assert out["DREAMTEAM_NETWORK"] == "off" and out["PATH"] == "p"
    # git: appended after the inherited entry, which stays
    assert out["GIT_CONFIG_COUNT"] == "6" and (out["GIT_CONFIG_KEY_0"], out["GIT_CONFIG_VALUE_0"]) == ("core.x", "y")
    pairs = {(out[f"GIT_CONFIG_KEY_{i}"], out[f"GIT_CONFIG_VALUE_{i}"]) for i in range(1, 6)}
    assert ("protocol.https.allow", "never") in pairs and ("protocol.ssh.allow", "never") in pairs
    assert "HOME" not in out   # only the Docker isolation changes the home
    assert netoff.environment({"GIT_CONFIG_COUNT": "x"})["GIT_CONFIG_KEY_0"] == "protocol.http.allow"


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
def test_git_refuses_its_network_transports_and_still_clones_a_local_repository(tmp_path):
    force("env")
    env = netoff.environment(dict(os.environ))
    remote = subprocess.run(["git", "ls-remote", f"https://{TEST_NET}/x.git"], env=env, capture_output=True,
                            text=True, timeout=60)
    assert remote.returncode != 0 and "not allowed" in remote.stderr
    source = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(source)], check=True, timeout=60)
    local = subprocess.run(["git", "clone", "-q", str(source), str(tmp_path / "copy")], env=env, capture_output=True,
                           text=True, timeout=60)
    assert local.returncode == 0, local.stderr


def test_the_docker_isolation_gives_the_offline_user_its_own_home(monkeypatch):
    force("docker", ["setpriv", "--"])
    fake_pwd = types.SimpleNamespace(getpwnam=lambda name: types.SimpleNamespace(pw_dir=f"/home/{name}"))
    monkeypatch.setitem(sys.modules, "pwd", fake_pwd)
    out = netoff.environment({"HOME": "/root", "UV_CACHE_DIR": "/dreamteam/skill-envs/.cache/uv"})
    assert out["HOME"] == "/home/dreamteam-agent" and out["USER"] == out["LOGNAME"] == "dreamteam-agent"
    assert out["UV_CACHE_DIR"] == "/home/dreamteam-agent/.cache/uv"


def test_a_session_with_network_changes_nothing(tmp_path):
    force("netns", ["unshare", "-rn", "--"])
    config = Config.from_dict(tmp_path / "p", {})
    argv, env = ["sh", "-c", "x"], {"A": "1"}
    assert netoff.command(config, argv, env) == (argv, env)
    assert netoff.describe(config) == {"mode": "on", "isolation": None}
    off = offline_config(tmp_path)
    assert netoff.command(off, argv, env)[0] == ["unshare", "-rn", "--", "sh", "-c", "x"]
    assert netoff.describe(off) == {"mode": "off", "isolation": "netns"}


def test_wrap_per_isolation():
    force("env")
    assert netoff.wrap(["bash", "-c", "x"]) == ["bash", "-c", "x"]
    netoff.reset()
    force("docker", ["setpriv", "--reuid=10001", "--"])
    assert netoff.wrap(["bash", "-c", "x"]) == ["setpriv", "--reuid=10001", "--", "bash", "-c", "x"]
    assert netoff.wrap('cmd.exe /d /s /c "x"') == 'cmd.exe /d /s /c "x"'   # a string: cmd.exe on Windows, "env"


# ---- detection ---------------------------------------------------------------------------------------------
def test_detection_prefers_the_firewall_then_a_namespace_then_the_environment(monkeypatch):
    monkeypatch.setattr(netoff, "offline_user", lambda: (1000, 1001))
    monkeypatch.setattr(netoff, "_probe_netns", lambda: pytest.fail("no probe inside the sandbox"))
    assert netoff.isolation() == "docker" and netoff.detected() == "docker"
    assert netoff._detected["prefix"] == ["setpriv", "--reuid=1000", "--regid=1001", "--clear-groups",
                                          "--no-new-privs", "--"]
    assert (netoff._detected["uid"], netoff._detected["gid"]) == (1000, 1001)
    netoff.reset()
    probes = []
    monkeypatch.setattr(netoff, "offline_user", lambda: None)
    monkeypatch.setattr(netoff, "_probe_netns", lambda: probes.append(1) or ["unshare", "-rn", "--"])
    assert netoff.isolation() == "netns" and netoff.isolation() == "netns" and probes == [1]   # detected once
    netoff.reset()
    monkeypatch.setattr(netoff, "_probe_netns", lambda: None)
    assert netoff.detected() is None and netoff.isolation() == "env" and netoff._detected["prefix"] == []


def test_the_offline_user_needs_the_marker_root_and_setpriv(monkeypatch):
    monkeypatch.setattr(netoff, "_POSIX", True)
    monkeypatch.setattr(netoff.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(netoff.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv(netoff.OFFLINE_USER_ENV, raising=False)
    assert netoff.offline_user() is None
    monkeypatch.setenv(netoff.OFFLINE_USER_ENV, "1000:1001")
    assert netoff.offline_user() == (1000, 1001)
    for bad in ("dreamteam-agent", "0:0", "1000", "1000:x"):   # never root, never a name
        monkeypatch.setenv(netoff.OFFLINE_USER_ENV, bad)
        assert netoff.offline_user() is None, bad
    monkeypatch.setenv(netoff.OFFLINE_USER_ENV, "1000:1001")
    monkeypatch.setattr(netoff.os, "geteuid", lambda: 1000, raising=False)
    assert netoff.offline_user() is None   # only root may switch to it
    monkeypatch.setattr(netoff, "_POSIX", False)
    assert netoff.offline_user() is None


def test_the_namespace_is_made_once_and_every_command_joins_it(monkeypatch):
    """One namespace per process (a dev server of one command is reachable from the next): the probe makes it, keeps
    its fds, and the prefix joins it through them; a probe that does not say "ok" closes them."""
    monkeypatch.setattr(netoff.sys, "platform", "linux")
    monkeypatch.setattr(netoff.shutil, "which", lambda name: "/usr/bin/unshare" if name == "unshare" else None)
    monkeypatch.setattr(netoff.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(netoff.os, "getgid", lambda: 1001, raising=False)
    closed, held, seen = [], [], []
    monkeypatch.setattr(netoff, "_hold_namespace", lambda unshare: held.append(unshare) or [70, 71])

    def run(argv, **kw):
        seen.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(netoff.subprocess, "run", run)
    pid, helper = os.getpid(), str(netoff.HELPER)
    join = [sys.executable, "-I", helper, "--join", f"/proc/{pid}/fd/70", f"/proc/{pid}/fd/71", "1000", "1001"]
    try:
        assert netoff._probe_netns() == [*join, "--"]
        assert held == ["/usr/bin/unshare"] and seen == [[*join, "--probe"]]
        assert netoff._ns_fds == [70, 71]   # kept open: the namespace outlives its holder process
        netoff._ns_fds.clear()
        monkeypatch.setattr(netoff.os, "close", closed.append)
        monkeypatch.setattr(netoff.subprocess, "run",
                            lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout=b"failed: EPERM", stderr=b""))
        assert netoff._probe_netns() is None and closed == [70, 71] and netoff._ns_fds == []
        monkeypatch.setattr(netoff, "_hold_namespace", lambda unshare: None)
        assert netoff._probe_netns() is None
        monkeypatch.setattr(netoff.sys, "platform", "darwin")
        assert netoff._probe_netns() is None
    finally:
        netoff._ns_fds.clear()   # (fake fds: reset() must not close real ones)


@pytest.mark.skipif(WINDOWS, reason="select() on a pipe")
def test_a_holder_that_cannot_make_the_namespace_leaves_nothing_behind(tmp_path):
    """The holder's answer is not "ok" (here: no unshare, so no CAP_NET_ADMIN for the loopback): no fds, no
    process left running."""
    fake = tmp_path / "unshare"
    fake.write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    fake.chmod(0o755)
    if os.geteuid() == 0:
        pytest.skip("root may bring a loopback up")
    assert netoff._hold_namespace(str(fake)) is None


# ---- MCP servers -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv, url", [
    (["npx", "mcp-remote", "https://mcp.example.com/sse"], "https://mcp.example.com/sse"),
    (["bridge", "--url=wss://example.org/ws"], "wss://example.org/ws"),
    (["uvx", "mcp-proxy", "http://127.0.0.1:8000/mcp"], None),
    (["node", "srv.js", "--base", "http://localhost:3000"], None),
    (["python", "server.py"], None),
])
def test_remote_url(argv, url):
    assert netoff.remote_url(argv) == url


# ---- the Docker sandbox ------------------------------------------------------------------------------------
def test_firewall_rules_reject_the_hub_and_all_but_loopback_and_dockers_dns():
    rules = netoff.firewall_commands("/usr/sbin/iptables", 10001, [8765, 9000])
    assert rules[0][-2:] == ["-N", "DREAMTEAM_OFFLINE"]
    # the hub's port first: through its HTTP API a command could switch its session's network back on
    assert [r[3:] for r in rules[1:6]] == [["DREAMTEAM_OFFLINE", "-p", "tcp", "--dport", "8765", "-j", "REJECT"],
                                           ["DREAMTEAM_OFFLINE", "-p", "tcp", "--dport", "9000", "-j", "REJECT"],
                                           ["DREAMTEAM_OFFLINE", "-d", "127.0.0.11", "-j", "REJECT"],
                                           ["DREAMTEAM_OFFLINE", "-o", "lo", "-j", "ACCEPT"],
                                           ["DREAMTEAM_OFFLINE", "-j", "REJECT"]]
    assert rules[-1][2:] == ["-I", "OUTPUT", "1", "-m", "owner", "--uid-owner", "10001", "-j", "DREAMTEAM_OFFLINE"]
    six = netoff.firewall_commands("ip6tables", 10001, [8765])
    assert not any("127.0.0.11" in r for r in six) and any("8765" in r for r in six)


def test_the_hub_ports(monkeypatch):
    assert docker_module.CONTAINER_PORT == 8765
    assert netoff.hub_ports(["serve", "/swarm", "--host", "0.0.0.0", "--port", "8765"]) == [8765]
    assert netoff.hub_ports(["serve", "--port", "9100"]) == [8765, 9100]
    assert netoff.hub_ports(["run", "--port=9200", "--port", "x"]) == [8765, 9200]


def _fake_pwd(monkeypatch, uid: int = 10001) -> None:
    monkeypatch.setitem(sys.modules, "pwd", types.SimpleNamespace(
        getpwnam=lambda name: types.SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_dir=f"/home/{name}")))


def test_setup_firewall(monkeypatch):
    monkeypatch.setattr(netoff.shutil, "which", lambda name: f"/usr/sbin/{name}")
    ran: list[list[str]] = []
    fail, present = set(), set()

    def run(argv, **kw):
        ran.append(argv)
        if argv[2] == "-C":   # the jump of an entry that ran before
            return types.SimpleNamespace(returncode=0 if Path(argv[0]).name in present else 1, stdout=b"",
                                         stderr=b"")
        bad = Path(argv[0]).name in fail
        return types.SimpleNamespace(returncode=1 if bad else 0, stdout=b"", stderr=b"no table" if bad else b"")

    monkeypatch.setattr(netoff.subprocess, "run", run)
    assert netoff.setup_firewall(10001, [8765]) is None and len(ran) == (1 + 6) + (1 + 5)
    # in place already (`docker exec <container> dreamteam ...` runs the entry again): no false warning
    present.update({"iptables", "ip6tables"})
    ran.clear()
    assert netoff.setup_firewall(10001, [8765]) is None and [r[2] for r in ran] == ["-C", "-C"]
    present.clear()
    # no IPv6 in the container: ip6tables may fail
    fail.add("ip6tables")
    monkeypatch.setattr(netoff, "_has_global_ipv6", lambda: False)
    assert netoff.setup_firewall(10001) is None
    monkeypatch.setattr(netoff, "_has_global_ipv6", lambda: True)
    assert "no table" in netoff.setup_firewall(10001)
    fail.clear()
    monkeypatch.setattr(netoff.shutil, "which", lambda name: None)
    assert "iptables is not installed" in netoff.setup_firewall(10001)


def test_the_offline_ids_are_the_mounted_folders_owner_unless_root(tmp_path, monkeypatch):
    _fake_pwd(monkeypatch)
    real_stat = os.stat
    owners = {"/swarm": (1000, 1000), "/root-owned": (0, 0), "/no-group": (1000, 0)}

    def fake_stat(path, *a, **kw):
        if str(path) in owners:
            uid, gid = owners[str(path)]
            return types.SimpleNamespace(st_uid=uid, st_gid=gid)
        if str(path) == "/missing":
            raise FileNotFoundError(path)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(netoff.os, "stat", fake_stat)
    assert netoff.sandbox_ids() == (1000, 1000)          # a bind mount on a Linux host: the user's own ids
    assert netoff.sandbox_ids("/root-owned") == (10001, 10001)   # Docker Desktop, a named volume: the image's user
    assert netoff.sandbox_ids("/missing") == (10001, 10001)
    assert netoff.sandbox_ids("/no-group") == (1000, 1000)       # never root's group
    monkeypatch.setitem(sys.modules, "pwd", types.SimpleNamespace(getpwnam=lambda name: (_ for _ in ()).throw(KeyError)))
    with pytest.raises(LookupError, match="no user dreamteam-agent"):
        netoff.sandbox_ids("/root-owned")


class _Exec(Exception):
    pass


def test_the_sandbox_entry_sets_up_the_firewall_then_drops_net_admin(monkeypatch, capsys):
    monkeypatch.setattr(netoff, "_POSIX", True)
    monkeypatch.setattr(netoff.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(netoff.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(netoff.sys, "argv", ["/usr/local/bin/dreamteam", "serve", "/swarm", "--port", "9100"])
    monkeypatch.setattr(netoff, "sandbox_ids", lambda: (1000, 1001))
    masks, execs, homes, walls = [], [], [], []
    monkeypatch.setattr(netoff.os, "umask", lambda mask: masks.append(mask) or 0o022)
    monkeypatch.setattr(netoff, "_give_home", lambda uid, gid: homes.append((uid, gid)))

    def execv(path, argv):
        execs.append((path, argv))
        raise _Exec

    monkeypatch.setattr(netoff.os, "execv", execv)
    monkeypatch.delenv(netoff.OFFLINE_USER_ENV, raising=False)
    # not asked for: nothing happens
    monkeypatch.delenv(netoff.FIREWALL_ENV, raising=False)
    netoff.sandbox_entry()
    assert execs == [] and masks == []
    monkeypatch.setenv(netoff.FIREWALL_ENV, "1")
    monkeypatch.setattr(netoff, "setup_firewall", lambda uid, ports: walls.append((uid, ports)))
    with pytest.raises(_Exec):
        netoff.sandbox_entry()
    assert walls == [(1000, [8765, 9100])] and homes == [(1000, 1001)]
    # the hub: no NET_ADMIN, and the offline user's group (what it creates is the commands' to write)
    assert execs == [("/usr/bin/setpriv", ["/usr/bin/setpriv", "--bounding-set=-net_admin", "--regid=1001",
                                           "--keep-groups", "--", sys.executable, "-m", "dreamteam", "serve",
                                           "/swarm", "--port", "9100"])]
    assert masks == [0o002] and os.environ[netoff.OFFLINE_USER_ENV] == "1000:1001"
    assert netoff.FIREWALL_ENV not in os.environ
    # a failure: a warning, no switch, and the hub goes on
    monkeypatch.delenv(netoff.OFFLINE_USER_ENV)
    monkeypatch.setenv(netoff.FIREWALL_ENV, "1")
    monkeypatch.setattr(netoff, "setup_firewall", lambda uid, ports: "iptables is not installed in the image")
    execs.clear()
    netoff.sandbox_entry()
    assert execs == [] and netoff.OFFLINE_USER_ENV not in os.environ
    assert "best-effort isolation only" in capsys.readouterr().err
    monkeypatch.setenv(netoff.FIREWALL_ENV, "1")
    monkeypatch.setattr(netoff, "sandbox_ids", lambda: (_ for _ in ()).throw(LookupError("the image has no user x")))
    netoff.sandbox_entry()
    assert execs == [] and "the image has no user x" in capsys.readouterr().err


def test_cli_main_runs_the_sandbox_entry_only_for_the_real_command_line(monkeypatch):
    calls = []
    monkeypatch.setattr(netoff, "sandbox_entry", lambda: calls.append(1))
    monkeypatch.setattr(cli, "cmd_status", lambda args: 0)
    monkeypatch.setattr(sys, "argv", ["dreamteam", "status", "--home", "x"])
    cli.main()
    cli.main(["status", "--home", "x"])
    assert calls == [1]


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    for name, mode in (("src/a.py", 0o644), ("run.sh", 0o744), ("ro.txt", 0o444), ("mine.txt", 0o600)):
        (root / name).write_text("x")
        os.chmod(root / name, mode)
    os.chmod(root / "src", 0o755)
    return root


def _mode(path: Path) -> int:
    import stat

    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.mark.skipif(WINDOWS, reason="POSIX ownership and modes")
def test_prepare_workspace_opens_what_root_created_and_leaves_the_users_own_files(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    outside = tmp_path / "secret"
    outside.write_text("x")
    os.chmod(outside, 0o600)
    (root / "link").symlink_to(outside)
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    before = _mode(tmp_path)
    assert netoff.prepare_workspace(root) == 0   # not the Docker isolation: nothing to do
    # this test's files stand for root's (the image's user owns them here); the offline user is another uid
    _fake_pwd(monkeypatch, uid=os.getuid())
    force("docker", ["setpriv", "--"], ids=(os.getuid() + 4242, os.getgid()))
    assert netoff.prepare_workspace(root) == 0
    assert _mode(root / "src") == 0o775 and not _mode(root) & 0o2000   # no set-group-id: the hub has the group
    assert _mode(root / "src" / "a.py") == 0o664 and _mode(root / "run.sh") == 0o774
    assert _mode(root / "ro.txt") == 0o444   # read-only stays read-only (the skills mirror)
    assert _mode(root / "mine.txt") == 0o660
    assert _mode(outside) == 0o600 and _mode(tmp_path) == before   # never through a symlink
    # the offline user's own files (a bind mount's owner on a Linux host) are left as they are
    os.chmod(root / "run.sh", 0o700)
    os.chmod(root / "mine.txt", 0o600)
    netoff.reset()
    force("docker", ["setpriv", "--"], ids=(os.getuid(), os.getgid()))
    assert netoff.prepare_workspace(root) == 0
    assert _mode(root / "run.sh") == 0o700 and _mode(root / "mine.txt") == 0o600


@pytest.mark.skipif(WINDOWS, reason="POSIX ownership and modes")
def test_prepare_workspace_never_follows_a_symlink_swapped_in_meanwhile(tmp_path, monkeypatch):
    """An offline command may swap a file for a symlink to /etc/shadow while root walks the workspace: the walk works
    on what it opened (O_NOFOLLOW), never on a path it looked at before. Here a stat of the path says "a plain file"
    (as it was just before the swap): nothing may change behind the symlink."""
    root = _workspace(tmp_path)
    target = tmp_path / "shadow"
    target.write_text("x")
    os.chmod(target, 0o600)
    (root / "swapped").symlink_to(target)
    plain = os.lstat(root / "src" / "a.py")
    real_lstat = os.lstat
    monkeypatch.setattr(netoff.os, "lstat", lambda path, *a, **kw: plain if str(path).endswith("swapped")
                        else real_lstat(path, *a, **kw))
    _fake_pwd(monkeypatch, uid=os.getuid())
    force("docker", ["setpriv", "--"], ids=(os.getuid() + 4242, os.getgid()))
    assert netoff.prepare_workspace(root) == 0
    assert _mode(target) == 0o600


def test_sandbox_command_grants_net_admin_only_for_the_firewall(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / "lib"))
    project = tmp_path / "home"
    (project / "sessions").mkdir(parents=True)
    (project / "dreamteam.toml").write_text("", encoding="utf-8")
    config = Config.load(project)
    plain = docker_module.sandbox_command(project, 9200, config, docker="docker")
    assert "NET_ADMIN" not in plain and not any(netoff.FIREWALL_ENV in a for a in plain)
    cmd = docker_module.sandbox_command(project, 9200, config, docker="docker", firewall=True, goal="G",
                                        network="off")
    i = cmd.index("--cap-add")
    assert cmd[i:i + 4] == ["--cap-add", "NET_ADMIN", "-e", f"{netoff.FIREWALL_ENV}=1"]
    assert cmd[-4:] == ["--goal", "G", "--network", "off"]
    # a home without --goal: serve takes no --network (the firewall is what it needs)
    assert "--network" not in docker_module.sandbox_command(project, 9200, config, docker="docker", firewall=True,
                                                            network="off")
    single = docker_module.sandbox_command(project, 9200, config, docker="docker", home=False, network="off")
    assert single[-2:] == ["--network", "off"] and "run" in single


def test_the_firewall_is_wanted_by_the_flag_the_settings_or_a_session(tmp_path):
    home = tmp_path / "home"
    (home / "sessions" / "001-a").mkdir(parents=True)
    (home / "dreamteam.toml").write_text("", encoding="utf-8")
    config = Config.load(home)
    meta = home / "sessions" / "001-a" / "session.json"
    meta.write_text(json.dumps({"overrides": {"swarm": {"language": "en"}}}), encoding="utf-8")
    assert not docker_module.wants_firewall(home, config)
    assert docker_module.wants_firewall(home, config, "off")
    assert docker_module.wants_firewall(home, offline_config(tmp_path))
    meta.write_text(json.dumps({"overrides": {"swarm": {"network": "off"}}}), encoding="utf-8")
    assert docker_module.wants_firewall(home, config)
    (home / "sessions" / "002-b").mkdir()
    (home / "sessions" / "002-b" / "session.json").write_text("{broken", encoding="utf-8")
    assert docker_module.wants_firewall(home, config)


def test_run_sandbox_rebuilds_an_image_of_another_dockerfile_when_the_firewall_needs_it(tmp_path, monkeypatch):
    project = tmp_path / "p"
    project.mkdir()
    (project / "dreamteam.toml").write_text('[swarm]\nnetwork = "off"\n', encoding="utf-8")
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / "lib"))
    built, ran = [], []
    monkeypatch.setattr(docker_module, "_docker", lambda: "docker")
    monkeypatch.setattr(docker_module, "image_exists", lambda image=docker_module.IMAGE: True)
    monkeypatch.setattr(docker_module, "image_dockerfile", lambda image=docker_module.IMAGE: "old")
    monkeypatch.setattr(docker_module, "build_image", lambda image=docker_module.IMAGE: built.append(image))
    monkeypatch.setattr(docker_module.subprocess, "call", lambda cmd: ran.append(cmd) or 0)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    assert docker_module.run_sandbox(project, 9200, image="dreamteam-offline-test:x") == 0
    assert built == ["dreamteam-offline-test:x"] and "NET_ADMIN" in ran[0]
    # with network: only a note, no rebuild
    (project / "dreamteam.toml").write_text("", encoding="utf-8")
    built.clear()
    docker_module.run_sandbox(project, 9200, image="dreamteam-offline-test:x")
    assert built == [] and "NET_ADMIN" not in ran[-1]


# ---- the members' commands, jobs and MCP servers -----------------------------------------------------------
WRAPPER = ("import subprocess, sys\n"
           "args = sys.argv[1:]\n"
           "args = args[1:] if args[:1] == ['--'] else args\n"
           "print('WRAPPED', flush=True)\n"
           "sys.exit(subprocess.call(args))\n")
PROBE = ("import os\n"
         "print('PROXY=' + os.environ.get('HTTPS_PROXY', '-'), 'NET=' + os.environ.get('DREAMTEAM_NETWORK', '-'),\n"
         "      'UV=' + os.environ.get('UV_OFFLINE', '-'))\n")


async def user_tool(swarm, name: str, args: dict | None = None) -> str:
    text, is_error = await call_tool(ToolContext(swarm, USER), name, args or {})
    assert not is_error, text
    return text


def _fake_namespace(tmp_path: Path) -> None:
    """A stand-in for `unshare -rn ... --`: a Python script that says it ran and runs the rest."""
    wrapper = tmp_path / "fake_netns.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    force("netns", [sys.executable, str(wrapper), "--"])


def _probe_command(tmp_path: Path) -> str:
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    if WINDOWS:   # PowerShell (a list of arguments, which the wrapper gets; cmd.exe's string never does)
        return f"& $env:PYEXE '{probe}'"
    return f"\"$PYEXE\" '{probe}'"


async def _offline_team(tmp_path: Path, network: str = "off", **overrides):
    swarm, _rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0, "network": network},
                             shell={"program": "powershell" if WINDOWS else "sh"}, **overrides)
    await swarm.start(run_agents=False)
    await swarm.hire(USER, "Developer", name=ANNA)
    return swarm


async def test_a_command_and_its_job_run_wrapped_with_the_offline_environment(tmp_path, monkeypatch, fast):
    monkeypatch.setenv("PYEXE", sys.executable)
    _fake_namespace(tmp_path)
    swarm = await _offline_team(tmp_path)
    try:
        ctx = agent_ctx(swarm, ANNA)
        out = await ok(ctx, "shell", {"command": _probe_command(tmp_path)})
        assert "WRAPPED" in out and "PROXY=http://127.0.0.1:9 NET=off UV=1" in out, out
        started = await ok(ctx, "shell", {"command": _probe_command(tmp_path) + ("; Start-Sleep 3" if WINDOWS
                                                                                 else "; sleep 3"),
                                          "background": True})
        assert "j1" in started
        await until(lambda: not swarm.jobs.running_of(ANNA), 30)
        job = await ok(ctx, "jobs", {"job_id": "j1"})
        assert "WRAPPED" in job and "PROXY=http://127.0.0.1:9" in job, job
        # the setting switched on under the running session: the next command is a plain one again
        swarm.reconfigure(with_network(swarm.config, "on"))
        plain = await ok(ctx, "shell", {"command": _probe_command(tmp_path)})
        assert "WRAPPED" not in plain and "NET=-" in plain, plain
    finally:
        await shutdown(swarm)


def with_network(config: Config, network: str) -> Config:
    """`config` with another [swarm] network (what a settings change hands Swarm.reconfigure)."""
    return dataclasses.replace(config, swarm=dataclasses.replace(config.swarm, network=network))


class _FakeConnection:
    seen: list[tuple[list[str], dict]] = []

    def __init__(self, name, command, cwd, env, log_path):
        self.name, self.command, self.env = name, list(command), dict(env)
        _FakeConnection.seen.append((self.command, self.env))
        self.tools = [{"name": "t", "description": "", "input_schema": {}}]
        self.alive = True

    async def start(self, timeout):
        return list(self.tools)

    async def close(self):
        self.alive = False


async def test_mcp_servers_start_wrapped_and_a_remote_one_is_refused(forge_factory, tmp_path, monkeypatch):
    _FakeConnection.seen = []
    monkeypatch.setattr(forge_module, "MCPConnection", _FakeConnection)
    force("netns", ["unshare", "-rn", "--"])
    external = {"local": {"command": [sys.executable, "server.py"]},
                "remote": {"command": ["npx", "mcp-remote", "https://mcp.example.com/sse"]}}
    swarm, forge = await forge_factory(mcp={"external": external})
    swarm.config = Config.from_dict(swarm.config.project_dir, {"swarm": {"network": "off"},
                                                               "mcp": {"external": external}})
    await forge.tools(USER, "local")
    command, env = _FakeConnection.seen[-1]
    assert command == ["unshare", "-rn", "--", sys.executable, "server.py"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9" and env["DREAMTEAM_NETWORK"] == "off"
    with pytest.raises(ToolError, match=r"connects to https://mcp\.example\.com/sse, and this session has no network"):
        await forge.tools(USER, "remote")
    assert len(_FakeConnection.seen) == 1   # never started
    # the network back on: the next start is a plain one, and the remote server is allowed
    await forge.close_connections()
    swarm.config = Config.from_dict(swarm.config.project_dir, {"mcp": {"external": external}})
    await forge.tools(USER, "remote")
    assert _FakeConnection.seen[-1][0] == ["npx", "mcp-remote", "https://mcp.example.com/sse"]


async def test_a_server_started_while_the_setting_changes_never_keeps_the_old_one(forge_factory, monkeypatch):
    """close_connections() may run while a server is still starting under the old setting: that server is started
    again under the new one, and a pooled server of the old setting is never handed out."""
    starts: list[list[str]] = []
    flip: list[Config] = []

    class Slow(_FakeConnection):
        async def start(self, timeout):
            starts.append(self.command)
            if flip:   # the user switches the network off while this one starts
                forge.swarm.config = flip.pop()
                await forge.close_connections()
            return list(self.tools)

    monkeypatch.setattr(forge_module, "MCPConnection", Slow)
    force("netns", ["unshare", "-rn", "--"])
    external = {"local": {"command": [sys.executable, "server.py"]}}
    swarm, forge = await forge_factory(mcp={"external": external})
    swarm.config = Config.from_dict(swarm.config.project_dir, {"mcp": {"external": external}})
    off = Config.from_dict(swarm.config.project_dir, {"swarm": {"network": "off"}, "mcp": {"external": external}})
    flip.append(off)
    await forge.tools(USER, "local")
    assert starts == [[sys.executable, "server.py"], ["unshare", "-rn", "--", sys.executable, "server.py"]]
    conn = forge.connection("local")
    assert conn.offline and conn.command[:2] == ["unshare", "-rn"]
    # the setting back on without close_connections (it is still running): the pooled one is not reused
    swarm.config = Config.from_dict(swarm.config.project_dir, {"mcp": {"external": external}})
    await forge.tools(USER, "local")
    assert starts[-1] == [sys.executable, "server.py"] and not conn.alive


# ---- skills ------------------------------------------------------------------------------------------------
async def test_a_members_proposal_gets_no_install_without_network(tmp_path):
    config = Config.from_dict(tmp_path / "project", {})
    manager = EnvManager(tmp_path / "envs")
    folder = make_skill(tmp_path / "skills" / "pdf", "pdf", "Reads PDFs.", {"requirements.txt": "pypdf\n"})

    def plan(trust: str, network: bool) -> list:
        return manager.plan(folder, "pdf", trust, config.skills, config=config, proposal=trust == "agent",
                            network=network)

    (refused,) = plan("agent", False)
    assert refused["status"] == "off" and refused["reason"] == {"code": "network"}
    (user,) = plan("user", False)
    assert isinstance(user, EnvSpec)   # the user's own skills install as before: DreamTeam installs them
    (spec,) = plan("agent", True)
    assert isinstance(spec, EnvSpec)
    state = await manager.ensure(spec)
    assert state["status"] == "ready", state
    (ready,) = plan("agent", False)
    assert isinstance(ready, EnvSpec) and ready.dirname == spec.dirname   # what is installed already is used
    for lang in ("en", "ru"):
        reason = prompts.t(lang, "deps_reason_network")
        assert reason and "." not in reason


# ---- telling the members, DreamTeam's git ------------------------------------------------------------------
async def test_each_member_is_told_once_and_again_when_it_changes(tmp_path):
    force("env")
    swarm = await _offline_team(tmp_path)
    try:
        kinds = lambda: [r["kind"] for r in swarm.db.all(  # noqa: E731
            "SELECT kind FROM notifications WHERE recipient = ? AND kind LIKE 'n_network_%' ORDER BY id", (ANNA,))]
        text = swarm.render_wake(ANNA)
        assert "This session has no internet" in text and kinds() == ["n_network_off"]
        assert "no internet" not in (swarm.render_wake(ANNA) or "") and kinds() == ["n_network_off"]
        assert swarm.render_wake(ANNA, incoming=True) is None   # nothing new: no news at all
        swarm.reconfigure(with_network(swarm.config, "on"))
        news = swarm.render_wake(ANNA, incoming=True)
        assert "turned the internet back on" in news and kinds() == ["n_network_off", "n_network_on"]
        assert swarm.render_wake(ANNA, incoming=True) is None
        state = swarm.snapshot()["swarm"]["network"]
        assert state == {"mode": "on", "isolation": None}
    finally:
        await shutdown(swarm)
    # a member of a session with network is never told anything
    other = await _offline_team(tmp_path / "other", network="on")
    try:
        assert "internet" not in other.render_wake(ANNA)
        status = await user_tool(other, "swarm_status")
        assert "NO NETWORK" not in status
    finally:
        await shutdown(other)


async def test_swarm_status_and_the_snapshot_say_how_the_session_is_cut_off(tmp_path, monkeypatch):
    force("docker", ["setpriv", "--"])
    swarm = await _offline_team(tmp_path)
    try:
        status = await user_tool(swarm, "swarm_status")
        assert "NO NETWORK for the members' commands, jobs and MCP servers (the Docker sandbox's firewall)" in status
        assert swarm.snapshot()["swarm"]["network"] == {"mode": "off", "isolation": "docker"}
        # a sandbox started without the firewall: "env", shown as "container" (a restart of the sandbox adds it)
        netoff.reset()
        force("env")
        monkeypatch.setattr(netoff, "in_container", lambda: True)
        assert swarm.snapshot()["swarm"]["network"] == {"mode": "off", "isolation": "container"}
        assert "the sandbox was started without its firewall" in await user_tool(swarm, "swarm_status")
        monkeypatch.setattr(netoff, "in_container", lambda: False)
        assert swarm.snapshot()["swarm"]["network"] == {"mode": "off", "isolation": "env"}
    finally:
        await shutdown(swarm)


def test_shown_isolation(monkeypatch):
    from dreamteam.tui.i18n import TEXTS

    monkeypatch.setattr(netoff, "in_container", lambda: True)
    assert [netoff.shown(k) for k in ("docker", "netns", "env", None)] == ["docker", "netns", "container", None]
    monkeypatch.setattr(netoff, "in_container", lambda: False)
    assert netoff.shown("env") == "env"
    for lang in ("en", "ru"):   # the TUI's /status row
        assert "dreamteam sandbox" in TEXTS[lang]["net_off_container"]


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_dreamteam_commits_without_hooks_in_a_session_without_network(tmp_path):
    force("env")
    swarm = await _offline_team(tmp_path, workspace={"git": True})
    try:
        root = swarm.workspace.root
        argv = swarm.workspace._git_argv(None, ("status",))
        assert argv[argv.index("status") - 4:argv.index("status")] == ["-c", f"core.hooksPath={os.devnull}", "-c",
                                                                       "core.fsmonitor=false"]
        # an fsmonitor command a member wrote into .git/config: git would run it at every status and add
        subprocess.run(["git", "-C", str(root), "-c", f"safe.directory={root.as_posix()}", "config", "core.fsmonitor",
                        "echo FSMONITOR-RAN > fsmonitor-ran.txt; false"], check=True, timeout=60)
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\necho hook ran > hook-ran.txt\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        await ok(agent_ctx(swarm, ANNA), "write_file", {"path": "notes.md", "content": "hello\n"})
        log = subprocess.run(["git", "-C", str(root), "-c", f"safe.directory={root.as_posix()}", "log",
                              "--format=%s", "-1"], capture_output=True, text=True, timeout=60).stdout
        assert "notes.md" in log and not (root / "hook-ran.txt").exists()
        assert not (root / "fsmonitor-ran.txt").exists()
    finally:
        await shutdown(swarm)


# ---- settings and surfaces ---------------------------------------------------------------------------------
def test_the_setting_is_checked(tmp_path):
    assert Config.from_dict(tmp_path, {}).swarm.network == "on"
    assert offline_config(tmp_path).swarm.network == "off"
    for bad in ("maybe", "OFF", ""):
        with pytest.raises(ValueError, match="swarm.network must be 'on' or 'off'"):
            Config.from_dict(tmp_path, {"swarm": {"network": bad}})
    with pytest.raises(ValueError, match="must be a string"):
        Config.from_dict(tmp_path, {"swarm": {"network": False}})


async def test_a_session_is_created_and_changed_with_network_off(tmp_path):
    from dreamteam.launcher_mcp import call_launcher_tool, session_line
    from dreamteam.sessions import SessionError
    from test_sessions import _home, _manager

    force("env")
    manager = _manager(_home(tmp_path))
    try:
        text, err = await call_launcher_tool(manager, "session_create", {"goal": "Offline work", "network": "off",
                                                                         "start": False})
        assert not err, text
        sid = manager.ids()[0]
        summary = manager.summary(sid)
        assert summary["overrides"]["swarm"]["network"] == "off" and summary["network"] == "off"
        assert session_line(summary).splitlines()[0].endswith(" | no network")
        with pytest.raises(SessionError, match="swarm.network"):
            manager.update(sid, settings={"swarm": {"network": "sometimes"}})
        assert manager.update(sid, settings={"swarm": {"network": None}})["network"] == "on"
        with pytest.raises(SessionError, match="swarm.network"):
            await manager.create("x", "goal", overrides={"swarm": {"network": "no"}}, start=False)
    finally:
        await manager.shutdown()


def test_the_cli_flags(tmp_path, monkeypatch, capsys):
    from dreamteam.tui.commands import session_overrides

    parser = cli.build_parser()
    for argv in (["tui", "--network", "off"], ["serve", "--goal", "G", "--network", "off"],
                 ["run", ".", "--network", "off"], ["sandbox", "--network", "off"]):
        assert parser.parse_args(argv).network == "off", argv
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--network", "sometimes"])
    assert session_overrides({"network": "off"}) == ({"swarm": {"network": "off"}}, None)
    monkeypatch.setattr(cli, "running_hub", lambda home: None)
    assert cli.main(["serve", "--home", str(tmp_path / "home"), "--network", "off", "--no-browser"]) == 2
    assert "--network needs --goal" in capsys.readouterr().err


def test_texts_for_the_members():
    for lang in ("en", "ru"):
        off, on = prompts.t(lang, "n_network_off"), prompts.t(lang, "n_network_on")
        assert "localhost" in off and ("PyPI" in off and "npm" in off)
        assert on and "`" not in on


# ---- the real isolations -----------------------------------------------------------------------------------
def _netns_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    netoff.reset()
    try:
        return netoff.isolation() == "netns"
    finally:
        netoff.reset()


@pytest.mark.skipif(not _netns_available(), reason="no unprivileged network namespaces (unshare -rn) here")
async def test_a_real_network_namespace_keeps_loopback_and_the_users_id(tmp_path):
    from dreamteam.shell import start_command

    assert netoff.isolation() == "netns"
    script = tmp_path / "net.py"
    script.write_text(
        "import os, socket\n"
        "l = socket.socket(); l.bind(('127.0.0.1', 0)); l.listen()\n"
        "socket.create_connection(l.getsockname()).close(); print('loopback ok')\n"
        f"try:\n    socket.create_connection(('{TEST_NET}', 80), timeout=3).close(); print('ROUTED')\n"
        "except OSError as e:\n    print('unreachable', e.errno)\n"
        "print('uid', os.getuid())\n", encoding="utf-8")
    cmd = await start_command(f"'{sys.executable}' '{script}'", cwd=tmp_path, program="sh", offline=True,
                              env=netoff.environment(dict(os.environ)))
    try:
        assert await cmd.wait_exit(30)
        await cmd.wait_eof(5)
        out = cmd.output()
    finally:
        cmd.close()
    assert "loopback ok" in out and "unreachable" in out and "ROUTED" not in out, out
    assert f"uid {os.getuid()}" in out   # the real user, not root of the namespace


@pytest.mark.skipif(not _netns_available(), reason="no unprivileged network namespaces (unshare -rn) here")
async def test_a_server_of_one_offline_command_is_reachable_from_the_next_and_an_mcp_server(tmp_path):
    """The pattern agents use all the time: a dev server as a background job, then curl or the tests against it from
    the next command, or a stdio MCP server that talks to it. One namespace for all the offline commands."""
    from dreamteam.shell import start_command

    assert netoff.isolation() == "netns"
    serve = tmp_path / "serve.py"
    serve.write_text("import socket\n"
                     "l = socket.socket(); l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                     "l.bind(('127.0.0.1', 0)); l.listen()\n"
                     "open('port', 'w').write(str(l.getsockname()[1]))\n"
                     "while True:\n    c, _ = l.accept(); c.sendall(b'hello'); c.close()\n", encoding="utf-8")
    client = tmp_path / "client.py"
    client.write_text("import socket\n"
                      "port = int(open('port').read())\n"
                      "print('got', socket.create_connection(('127.0.0.1', port), timeout=5).recv(5).decode())\n",
                      encoding="utf-8")
    env = netoff.environment(dict(os.environ))
    server = await start_command(f"exec '{sys.executable}' '{serve}'", cwd=tmp_path, program="sh", offline=True,
                                 env=env)
    try:
        await until(lambda: (tmp_path / "port").exists(), 30)
        cmd = await start_command(f"'{sys.executable}' '{client}'", cwd=tmp_path, program="sh", offline=True, env=env)
        try:
            assert await cmd.wait_exit(30)
            await cmd.wait_eof(5)
            out = cmd.output()
        finally:
            cmd.close()
        assert "got hello" in out, out
        # an MCP server's process: netoff.wrap of a plain argv (MCPForge._new_connection)
        third = subprocess.run(netoff.wrap([sys.executable, str(client)]), cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=30)
        assert "got hello" in third.stdout, third.stdout + third.stderr
        # and DreamTeam's own side (the machine's localhost) does not see it
        mine = subprocess.run([sys.executable, str(client)], cwd=tmp_path, capture_output=True, text=True,
                              timeout=30)
        assert mine.returncode != 0 and "got hello" not in mine.stdout
    finally:
        server.tree.kill()
        await server.settle(5)
        server.close()


DOCKER_IMAGE = f"dreamteam-offline-test:pytest-{os.getpid()}"


def _docker_tests() -> bool:
    if os.environ.get("DREAMTEAM_DOCKER_TESTS") != "1" or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


DOCKER_CHECK = r"""
import json, os, socket, subprocess, sys, time
from dreamteam import netoff

PROBE = "import socket, sys\ntry:\n    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3)\n" \
        "    print('OPEN')\nexcept OSError as e:\n    print('errno', e.errno)\n"


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True).stdout.strip()


os.makedirs("/mounted")
os.chown("/mounted", 1000, 1000)   # a bind mount of a Linux host's user
ids = netoff.sandbox_ids("/mounted")
problem = netoff.setup_firewall(10001, [8765])
again = netoff.setup_firewall(10001, [8765])   # `docker exec <container> dreamteam ...`: in place already
os.environ[netoff.OFFLINE_USER_ENV] = "10001:10001"
hub = subprocess.Popen([sys.executable, "-c", "import socket, time\nl = socket.socket()\n"
                        "l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\nl.bind(('0.0.0.0', 8765))\n"
                        "l.listen()\ntime.sleep(60)\n"])
time.sleep(1)
agent = lambda *a: run(netoff.wrap([sys.executable, "-c", PROBE, *a]))
root = lambda *a: run([sys.executable, "-c", PROBE, *a])
print(json.dumps({"ids": ids, "problem": problem, "again": again, "kind": netoff.isolation(),
                  "agent_id": run(netoff.wrap(["id", "-u"])), "agent": agent(sys.argv[1], "80"),
                  "root": root(sys.argv[1], "80"), "agent_hub": agent("127.0.0.1", "8765"),
                  "root_hub": root("127.0.0.1", "8765")}))
hub.kill()
"""


@pytest.mark.skipif(not _docker_tests(), reason="opt-in: DREAMTEAM_DOCKER_TESTS=1 and a Docker daemon (builds an "
                                                "image under a tag of its own)")
def test_the_docker_firewall_cuts_off_the_offline_user_only(tmp_path):
    """The sandbox image under a test tag: the entry's firewall (twice: the second finds it in place), then a command
    as the offline user (no route but loopback, the hub's port refused) and one as root (DreamTeam's side: nothing
    rejected); the offline ids of a mounted folder with an owner of its own are that owner's."""
    (tmp_path / "check.py").write_text(DOCKER_CHECK, encoding="utf-8")
    try:
        docker_module.build_image(DOCKER_IMAGE)
        out = subprocess.run(["docker", "run", "--rm", "--cap-add", "NET_ADMIN", "-v", f"{tmp_path}:/check:ro",
                              "--entrypoint", "python", DOCKER_IMAGE, "/check/check.py", TEST_NET],
                             capture_output=True, text=True, timeout=300)
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["problem"] is None and result["again"] is None, result
        assert result["ids"] == [1000, 1000], result
        assert result["kind"] == "docker" and result["agent_id"] == "10001", result
        assert result["agent"] == "errno 111", result          # rejected at once: connection refused
        assert result["root"] != "errno 111", result           # DreamTeam's side is not filtered
        assert result["agent_hub"] == "errno 111" and result["root_hub"] == "OPEN", result   # the hub's API
    finally:
        subprocess.run(["docker", "image", "rm", "-f", DOCKER_IMAGE], capture_output=True, timeout=120)
