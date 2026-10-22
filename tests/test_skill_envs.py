"""skill_deps.EnvManager (docs/SPEC.md §19 "Script dependencies"): builds and their states, the cross-process build
and use locks, failures and their retry, broken envs, reinstall, cleanup and the per-session Node views. The
installers are the fakes of tests/helpers/fake_installers.py (real processes that write what an install would);
the tests marked `real_installers` run uv, pip and npm offline against local packages, skipped without them."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_skill
from dreamteam import skill_deps
from dreamteam.config import Config
from dreamteam.skill_deps import EnvManager, EnvSpec, NodeManifest, RealInstallers
from dreamteam.skills import SkillError
from helpers import fake_installers, hold_lock
from helpers.wheels import make_npm_tarball, make_wheel, wheel_hash
from proc_guard import kill_tree, read_line, spawn

HOLD = str(Path(hold_lock.__file__).resolve())
WINDOWS = sys.platform == "win32"


@pytest.fixture
def fake() -> fake_installers.FakeInstallers:
    return skill_deps.INSTALLERS


def config_for(tmp_path: Path, **skills_settings) -> Config:
    return Config.from_dict(tmp_path / "project", {"skills": skills_settings} if skills_settings else {})


def manager(tmp_path: Path) -> EnvManager:
    return EnvManager(tmp_path / "envs")


def plan(m: EnvManager, config: Config, folder: Path, name: str = "pdf", trust: str = "user") -> list:
    return m.plan(folder, name, trust, config.skills, config=config, proposal=trust == "agent")


def py_skill(tmp_path: Path, name: str = "pdf", requirements: str = "pypdf\nrich\n") -> Path:
    return make_skill(tmp_path / "skills" / name, name, "Reads PDFs.", {"requirements.txt": requirements})


def node_skill(tmp_path: Path, name: str = "web") -> Path:
    return make_skill(tmp_path / "skills" / name, name, "Web things.",
                      {"package.json": json.dumps({"dependencies": {"left-pad": "^1"}})})


def spec_of(tmp_path, m, config, name="pdf", **kw) -> EnvSpec:
    (spec,) = plan(m, config, py_skill(tmp_path, name, **kw), name)
    assert isinstance(spec, EnvSpec), spec
    return spec


class Phases:
    def __init__(self, m: EnvManager):
        self.seen: list[tuple[str, str, dict]] = []
        self.stop = m.subscribe(lambda dirname, state, phase: self.seen.append((dirname, phase, state)))

    def phases(self, dirname: str) -> list[str]:
        return [p for d, p, _s in self.seen if d == dirname]


def hold(path: Path, mode: str, *extra: str, env: dict | None = None) -> subprocess.Popen:
    proc = spawn([sys.executable, HOLD, str(path), mode, *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 env=env)
    line = read_line(proc, 30, "the lock holder").strip()
    assert line in (b"locked", b"busy"), line
    proc.locked = line == b"locked"   # type: ignore[attr-defined]
    return proc


async def until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


# ---------------------------------------------------------------------------------------------------- builds
async def test_a_python_build_becomes_ready(tmp_path, fake):
    fake.behavior["pdf"] = {"commands": ["pdftool"]}
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    phases = Phases(m)
    assert m.peek(spec)["status"] == "installing"
    state = await m.ensure(spec)
    assert state["status"] == "ready" and state["reused"] is False
    path = m.dir(spec.dirname)
    assert state["python"] == skill_deps.venv_python(path) and os.path.isfile(state["python"])
    assert state["commands"] == ["pdftool"] and state["bin"] == skill_deps.venv_bin(path)
    env_json = json.loads((path / "env.json").read_text())
    assert env_json["installed"] == ["pypdf", "rich"] and env_json["trust"] == "user"
    assert env_json["installer"] == "fake-pip 1.0" and env_json["seconds"] >= 0
    assert (path / "src" / "requirements.txt").read_text() == "pypdf\nrich\n"
    assert (path / "ok").exists() and not (path / "failed.json").exists() and (path / "used").exists()
    # the installers ran in the env folder (npm's INIT_CWD, a build backend's leftovers)
    assert os.path.samefile((path / "fake-python-cwd.txt").read_text(), path)
    assert "fake python install of pypdf, rich" in m.log_tail(spec.dirname)
    await until(lambda: "done" in phases.phases(spec.dirname))
    assert phases.phases(spec.dirname) == ["started", "done"]
    assert m.peek(spec)["status"] == "ready"


async def test_a_second_ensure_joins_the_build_and_a_ready_env_is_reused(tmp_path, fake):
    fake.behavior["pdf"] = {"sleep": 0.5}
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    one, two = m.ensure(spec), m.ensure(spec)
    first, second = await asyncio.gather(one, two)
    assert first == second and first["status"] == "ready"
    assert len([c for c in fake.calls if c["dirname"] == spec.dirname]) == 1
    again = await m.ensure(spec)
    assert again["status"] == "ready" and again["reused"] is True
    assert len(fake.calls) == 1


async def test_a_full_slot_queues_the_build(tmp_path, fake):
    fake.behavior["slow"] = {"sleep": 1.0}
    m, config = manager(tmp_path), config_for(tmp_path)
    m._slots = __import__("threading").Semaphore(1)
    slow, quick = spec_of(tmp_path, m, config, "slow"), spec_of(tmp_path, m, config, "quick")
    phases = Phases(m)
    first = m.ensure(slow)
    await until(lambda: "started" in phases.phases(slow.dirname))
    second = m.ensure(quick)
    await until(lambda: m.peek(quick).get("queued") is True)
    assert (await first)["status"] == (await second)["status"] == "ready"
    assert phases.phases(quick.dirname) == ["queued", "started", "done"]


async def test_a_failed_build_records_its_error_and_log(tmp_path, fake):
    fake.behavior["pdf"] = {"fail": "ERROR: No matching distribution found for pypdf"}
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    state = await m.ensure(spec)
    assert state["status"] == "failed"
    assert state["error"] == "fake: ERROR: No matching distribution found for pypdf"
    assert state["log"] == str(m.log_path(spec.dirname)) and "Collecting things" in m.log_tail(spec.dirname)
    failed = json.loads((m.dir(spec.dirname) / "failed.json").read_text())
    assert failed["installer"] == "fake-pip 1.0" and failed["exit"] == 1 and not failed["timed_out"]
    assert m.peek(spec)["status"] == "failed" and not (m.dir(spec.dirname) / "ok").exists()


async def test_a_build_that_times_out_is_killed(tmp_path, fake):
    fake.behavior["pdf"] = {"hang": True}
    m, config = manager(tmp_path), config_for(tmp_path, install_timeout=1)
    spec = spec_of(tmp_path, m, config)
    started = time.monotonic()
    state = await asyncio.wait_for(m.ensure(spec), 60)
    assert state["status"] == "failed" and state["error"] == "timed out after 1 s"
    assert time.monotonic() - started < 30
    assert json.loads((m.dir(spec.dirname) / "failed.json").read_text())["timed_out"] is True


async def test_a_venv_too_deep_for_windows_fails_with_a_clear_text(tmp_path, fake, monkeypatch):
    if not WINDOWS:
        pytest.skip("the 260-character limit is Windows'")
    monkeypatch.setattr(skill_deps, "long_paths_off", lambda: True)
    fake.behavior["pdf"] = {"deep": True}
    m, config = manager(tmp_path), config_for(tmp_path)
    state = await m.ensure(spec_of(tmp_path, m, config))
    assert state["status"] == "failed"
    assert state["error"].startswith("package deeppkg has files deeper than Windows allows with long paths off "
                                     "(a path of ")
    assert "enable LongPathsEnabled, or set [skills] envs_dir to a shorter folder" in state["error"]


async def test_failures_are_retried_after_an_hour_or_by_force(tmp_path, fake):
    fake.behavior["pdf"] = {"fail": "ERROR: offline"}
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    assert (await m.ensure(spec))["status"] == "failed"
    del fake.behavior["pdf"]
    assert (await m.ensure(spec))["status"] == "failed" and len(fake.calls) == 1   # not before an hour
    failed = m.dir(spec.dirname) / "failed.json"
    data = json.loads(failed.read_text())
    failed.write_text(json.dumps({**data, "at": time.time() - skill_deps.RETRY_FAILED_SECONDS - 5}))
    assert (await m.ensure(spec))["status"] == "ready" and len(fake.calls) == 2
    fake.behavior["pdf"] = {"fail": "ERROR: offline"}
    await m.ensure(spec, force=True)
    fake.behavior.clear()
    assert (await m.ensure(spec, force=True))["status"] == "ready" and len(fake.calls) == 4


async def test_a_broken_env_is_rebuilt(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(skill_deps, "VERIFY_SECONDS", 0.0)
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    await m.ensure(spec)
    site = skill_deps._site_packages(m.dir(spec.dirname))[0]
    shutil.rmtree(next(site.glob("rich-*.dist-info")))
    assert m.peek(spec)["status"] == "installing"   # and its rebuild starts
    await until(lambda: not m.building(spec.dirname) and m.peek(spec)["status"] == "ready")
    assert len(fake.calls) == 2


async def test_another_process_build_is_waited_for_without_a_slot_and_reused(tmp_path, fake, monkeypatch):
    m, config = manager(tmp_path), config_for(tmp_path)
    m._slots = __import__("threading").Semaphore(1)
    spec, other = spec_of(tmp_path, m, config), spec_of(tmp_path, m, config, "other")
    path = m.dir(spec.dirname)
    child = hold(m._lock_file(spec.dirname, "lock"), "exclusive", str(path), "--finish",
                 env={**os.environ, "HOLD_SECONDS": "2"})
    try:
        phases = Phases(m)
        waiting = m.ensure(spec)
        await until(lambda: "waiting" in phases.phases(spec.dirname))
        assert m.peek(spec).get("other_process") is True
        assert (await m.ensure(other))["status"] == "ready"   # the waiting build holds no slot
        state = await asyncio.wait_for(waiting, 30)
        assert state["status"] == "ready" and state["reused"] is True
        assert [c["skill"] for c in fake.calls] == ["other"]
    finally:
        kill_tree(child)


async def test_the_wait_for_another_process_is_bounded(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(skill_deps, "BUILD_WAIT_EXTRA", 0.0)
    m, config = manager(tmp_path), config_for(tmp_path, install_timeout=1)
    spec = spec_of(tmp_path, m, config)
    child = hold(m._lock_file(spec.dirname, "lock"), "exclusive")
    try:
        state = await asyncio.wait_for(m.ensure(spec), 30)
        assert state["status"] == "failed" and state["error"] == "another DreamTeam process is still installing it"
        assert not (m.dir(spec.dirname) / "failed.json").exists() and m.peek(spec)["status"] == "failed"
    finally:
        kill_tree(child)
    assert (await m.ensure(spec))["status"] == "ready"   # retried: a failure in memory only


async def test_a_builder_killed_mid_build_leaves_no_ok_and_the_next_one_rebuilds(tmp_path, fake):
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    path = m.dir(spec.dirname)
    child = hold(m._lock_file(spec.dirname, "lock"), "exclusive", str(path))
    (path / "old.txt").write_text("from the dead builder")
    kill_tree(child)
    assert not (path / "ok").exists() and m._disk_state(spec.dirname) == "incomplete"
    assert (await m.ensure(spec))["status"] == "ready"
    assert not (path / "old.txt").exists() and not (path / "partial").exists()


# ---------------------------------------------------------------------------------------------------- in use
async def test_use_locks_keep_reinstalls_and_cleanup_away(tmp_path, fake):
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    await m.ensure(spec)
    use = m._lock_file(spec.dirname, "use")
    m.use("session-a", {spec.dirname})
    child = hold(use, "exclusive")
    assert not child.locked   # this process holds it shared
    kill_tree(child)
    m.check_reinstall(spec, "session-a")   # the requester alone may
    m.use("session-b", {spec.dirname})
    with pytest.raises(SkillError) as info:
        m.check_reinstall(spec, "session-a")
    assert info.value.status == 409 and "used by 1 other running session(s)" in str(info.value)
    m.use("session-b", set())
    m.use("session-a", set())
    shared = hold(use, "shared")
    try:
        with pytest.raises(SkillError) as info:
            m.check_reinstall(spec, None)
        assert "used by another DreamTeam process" in str(info.value)
    finally:
        kill_tree(shared)
    m.check_reinstall(spec, None)
    # a failed env is rebuilt for everyone, even while others use it
    m.use("session-b", {spec.dirname})
    (m.dir(spec.dirname) / "ok").unlink()
    (m.dir(spec.dirname) / "failed.json").write_text(json.dumps({"at": time.time(), "error": "x"}))
    m.check_reinstall(spec, "session-a")
    assert (await m.ensure(spec, owner="session-a", force=True))["status"] == "ready"
    m.use("session-b", set())


async def test_a_reinstall_refused_inside_the_build_publishes_the_envs_real_state(tmp_path, fake):
    """A forced build whose exclusive lock is refused (another process uses the ready env) raises to its caller, and
    its subscribers, who saw it start, get the env's state as it is (still ready) instead of waiting for good; a
    peek of this process meanwhile never takes the shared lock back from its own exclusive take."""
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    await m.ensure(spec)
    phases = Phases(m)
    shared = hold(m._lock_file(spec.dirname, "use"), "shared")
    try:
        with pytest.raises(SkillError, match="used by another DreamTeam process"):
            await m.ensure(spec, force=True)
    finally:
        kill_tree(shared)
        phases.stop()
    await until(lambda: "done" in phases.phases(spec.dirname))
    done = [s for d, p, s in phases.seen if d == spec.dirname and p == "done"][-1]
    assert done["status"] == "ready" and m.peek(spec)["status"] == "ready" and not m.building(spec.dirname)
    # the own exclusive take: a shared take of this process waits for it to end
    m.use("me", {spec.dirname})
    with m.exclusive(spec.dirname, requester="me"):
        assert m._take_shared(spec.dirname, wait=0.0) is False and m.users(spec.dirname) == {"me"}
    assert m._holders[spec.dirname][0] is not None   # taken back afterwards
    m.use("me", set())


async def test_a_held_exclusive_lock_makes_use_wait_and_peek_say_installing(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(skill_deps, "USE_WAIT", 0.3)
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    await m.ensure(spec)
    child = hold(m._lock_file(spec.dirname, "use"), "exclusive")
    try:
        started = time.monotonic()
        m.use("session-a", {spec.dirname})
        assert time.monotonic() - started >= 0.25
        assert m.peek(spec, "session-a")["status"] == "installing"
    finally:
        kill_tree(child)
    assert m.peek(spec, "session-a")["status"] == "ready"   # taken at the next look
    m.use("session-a", set())


async def test_reinstall_of_a_ready_env_rebuilds_it(tmp_path, fake):
    m, config = manager(tmp_path), config_for(tmp_path)
    spec = spec_of(tmp_path, m, config)
    await m.ensure(spec)
    m.use("me", {spec.dirname})
    first = json.loads((m.dir(spec.dirname) / "ok").read_text())["at"]
    state = await m.ensure(spec, owner="me", force=True)
    assert state["status"] == "ready" and len(fake.calls) == 2
    assert json.loads((m.dir(spec.dirname) / "ok").read_text())["at"] > first
    m.use("other", {spec.dirname})
    with pytest.raises(SkillError):
        await m.ensure(spec, owner="me", force=True)
    assert m.peek(spec)["status"] == "ready"   # a refused reinstall leaves the env as it was
    m.use("other", set())
    m.use("me", set())


# --------------------------------------------------------------------------------------------------- cleanup
def _age(path: Path, days: float) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


async def test_cleanup(tmp_path, fake):
    m, config = manager(tmp_path), config_for(tmp_path)
    old, fresh, used, built = (spec_of(tmp_path, m, config, n) for n in ("old", "fresh", "used", "built"))
    for spec in (old, fresh, used, built):
        await m.ensure(spec)
    for spec in (old, used, built):
        _age(m.dir(spec.dirname) / "used", 40)
    assert m.cleanup(0) == {"removed": [], "kept": []}
    m.use("me", {used.dirname})
    child = hold(m._lock_file(built.dirname, "lock"), "exclusive")
    try:
        report = m.cleanup(30)
    finally:
        kill_tree(child)
    assert report["removed"] == [old.dirname]
    assert {"dirname": used.dirname, "why": "in use"} in report["kept"]
    assert {"dirname": built.dirname, "why": "being built"} in report["kept"]
    assert not m.dir(old.dirname).exists() and m.dir(fresh.dirname).exists()
    m.use("me", set())
    shared = hold(m._lock_file(used.dirname, "use"), "shared")
    try:
        assert {"dirname": used.dirname, "why": "in use"} in m.cleanup(30)["kept"]
    finally:
        kill_tree(shared)
    # reserved names and folders of other shapes are never touched; old trash goes
    for name in (".locks", ".cache", "not-an-env", "pdf-py-short"):
        (m.root / name).mkdir(exist_ok=True)
        _age(m.root / name, 400)
    trash = m.root / f".trash-abc123-{int(time.time()) - 7200}"
    trash.mkdir()
    m.cleanup(30)
    assert all((m.root / n).exists() for n in (".locks", ".cache", "not-an-env", "pdf-py-short"))
    assert not trash.exists()


async def test_the_users_cleanup_removes_every_unused_env_whatever_the_retention(tmp_path, fake):
    """"Remove unused now" (every_unused): a fresh env no session uses goes too, also with env_retention_days 0;
    one a session uses is kept and reported."""
    m, config = manager(tmp_path), config_for(tmp_path)
    fresh, used = (spec_of(tmp_path, m, config, n) for n in ("fresh", "used"))
    for spec in (fresh, used):
        await m.ensure(spec)
    assert m.cleanup(30) == {"removed": [], "kept": []}   # the automatic run: both younger than the retention
    m.use("me", {used.dirname})
    try:
        report = m.cleanup(0, every_unused=True)
    finally:
        m.use("me", set())
    assert report == {"removed": [fresh.dirname], "kept": [{"dirname": used.dirname, "why": "in use"}]}
    assert not m.dir(fresh.dirname).exists() and (m.dir(used.dirname) / "ok").exists()


async def test_cleanup_removes_old_failures_and_stale_views_and_survives_a_refused_rename(tmp_path, fake,
                                                                                            monkeypatch):
    m, config = manager(tmp_path), config_for(tmp_path)
    fake.behavior["broken"] = {"fail": "ERROR: x"}
    failed = spec_of(tmp_path, m, config, "broken")
    await m.ensure(failed)
    _age(m.dir(failed.dirname), 2)
    (web,) = plan(m, config, node_skill(tmp_path), "web")
    await m.ensure(web)
    stale, live = m.view(web, "stale-session"), m.view(web, "live-session")
    _age(stale.parent / "used", 2)
    report = m.cleanup(0)
    assert report["removed"] == [failed.dirname]
    assert not stale.parent.exists() and live.exists()
    keep = spec_of(tmp_path, m, config, "keep")
    await m.ensure(keep)
    _age(m.dir(keep.dirname) / "used", 40)

    def refuse(src, dst):
        raise PermissionError("a process has its cwd there")

    monkeypatch.setattr(skill_deps.skills, "_rename", refuse)
    report = m.cleanup(30)
    assert {"dirname": keep.dirname, "why": "in use"} in report["kept"]
    assert (m.dir(keep.dirname) / "ok").exists()


def test_a_standalone_run_schedules_one_cleanup(tmp_path, monkeypatch):
    from dreamteam import cli

    timers, cleanups = [], []

    class Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.daemon, self.started = delay, fn, False, False
            timers.append(self)

        def start(self):
            self.started = True

    def cleanup(self, days):
        cleanups.append((self.root, days))
        if len(cleanups) > 1:
            raise OSError("the disk went away")
        return {"removed": ["old-py-000000000000"], "kept": []}

    monkeypatch.setattr(cli.threading, "Timer", Timer)
    monkeypatch.setattr(EnvManager, "cleanup", cleanup)
    cli._schedule_env_cleanup(config_for(tmp_path, env_retention_days=0))   # 0: environments are kept
    assert not timers
    config = config_for(tmp_path, env_retention_days=7)
    cli._schedule_env_cleanup(config)
    (timer,) = timers
    assert timer.delay == cli.ENV_CLEANUP_DELAY and timer.daemon and timer.started and not cleanups
    timer.fn()
    assert cleanups == [(EnvManager.for_config(config).root, 7)]
    timer.fn()   # a failure is logged; the next run tries again
    assert len(cleanups) == 2


# ----------------------------------------------------------------------------------------------------- views
async def test_node_views_are_hardlink_farms_reused_and_rebuilt(tmp_path, fake, monkeypatch):
    m, config = manager(tmp_path), config_for(tmp_path)
    (web,) = plan(m, config, node_skill(tmp_path), "web")
    state = await m.ensure(web)
    assert state["status"] == "ready" and state["node_modules"] == str(m.dir(web.dirname) / "node_modules")
    env_file = m.dir(web.dirname) / "node_modules" / "left-pad" / "index.js"
    view = m.view(web, "session-1")
    assert view == m.dir(web.dirname) / "views" / skill_deps.owner_id("session-1") / "node_modules"
    copy = view / "left-pad" / "index.js"
    assert os.path.samefile(copy, env_file) or os.stat(copy).st_nlink == 2
    assert json.loads((view.parent / "view.json").read_text())["mode"] == "hardlink"
    assert m.view_ok(web, "session-1") == view
    mtime = (view.parent / "view.json").stat().st_mtime_ns
    assert m.view(web, "session-1") == view and (view.parent / "view.json").stat().st_mtime_ns == mtime
    # removing the view's files (an agent's rm -rf through the link) leaves the env intact
    shutil.rmtree(view)
    assert env_file.exists() and m.view_ok(web, "session-1") is None
    assert m.view(web, "session-1").exists()
    # after a rebuild of the env the view is made again; without hardlinks it is a copy
    await m.ensure(web, owner="session-1", force=True)
    assert m.view_ok(web, "session-1") is None

    def no_links(src, dst):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(skill_deps.os, "link", no_links)
    view = m.view(web, "session-1")
    assert json.loads((view.parent / "view.json").read_text())["mode"] == "copy"
    assert not os.path.samefile(view / "left-pad" / "index.js", m.dir(web.dirname) / "node_modules" / "left-pad"
                                / "index.js")


# ------------------------------------------------------------------------------------------- real installers
def _offline_env(monkeypatch, tmp_path: Path, wheels: Path) -> None:
    cache = tmp_path / "caches"
    for name, value in (("PIP_NO_INDEX", "1"), ("PIP_FIND_LINKS", str(wheels)), ("PIP_CONFIG_FILE", os.devnull),
                        ("PIP_CACHE_DIR", str(cache / "pip")), ("UV_OFFLINE", "1"), ("UV_NO_CONFIG", "1"),
                        ("UV_FIND_LINKS", str(wheels)), ("UV_CACHE_DIR", str(cache / "uv")),
                        ("UV_PYTHON_DOWNLOADS", "never"), ("npm_config_cache", str(cache / "npm")),
                        ("npm_config_userconfig", str(cache / "npmrc")), ("npm_config_offline", "true"),
                        ("npm_config_update_notifier", "false")):
        monkeypatch.setenv(name, value)


def _imports(python: str, module: str, cwd: Path) -> str:
    done = subprocess.run([python, "-c", f"import {module}; print({module}.VALUE)"], capture_output=True, text=True,
                          cwd=str(cwd), timeout=120)
    return done.stdout.strip() or done.stderr


@pytest.mark.real_installers
@pytest.mark.parametrize("installer", ["uv", "pip"])
async def test_real_python_installers_build_an_importable_venv(tmp_path, monkeypatch, installer):
    wheels = tmp_path / "wheels"
    make_wheel(wheels, "dtdep")
    make_wheel(wheels, "dtdemo", files={"dtdemo/__init__.py": "import dtdep\nVALUE = dtdep.VALUE + 1\n"},
               requires=["dtdep"])
    _offline_env(monkeypatch, tmp_path, wheels)
    real = RealInstallers()
    monkeypatch.setattr(skill_deps, "INSTALLERS", real)
    m, config = manager(tmp_path), config_for(tmp_path)
    env = skill_deps.installer_env(config, "user")
    tool = real.python_tool(config, env, tmp_path)
    if isinstance(tool, dict):
        pytest.skip(f"no interpreter: {tool}")
    if installer == "uv" and tool.installer != "uv":
        pytest.skip("uv is not installed")
    if installer == "pip":
        monkeypatch.setattr(skill_deps, "_uv", lambda env: None)
        tool = real.python_tool(config, env, tmp_path)
        if isinstance(tool, dict):
            pytest.skip(f"no pip: {tool}")
    (spec,) = plan(m, config, py_skill(tmp_path, "demo", "dtdemo\n"), "demo")
    state = await asyncio.wait_for(m.ensure(spec), 300)
    assert state["status"] == "ready", (state, m.log_tail(spec.dirname))
    assert _imports(state["python"], "dtdemo", tmp_path) == "43"
    assert json.loads((m.dir(spec.dirname) / "env.json").read_text())["installed"] == ["dtdemo"]


@pytest.mark.real_installers
@pytest.mark.parametrize("installer", ["uv", "pip"])
async def test_real_hash_checking(tmp_path, monkeypatch, installer):
    if installer == "uv" and shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    wheels = tmp_path / "wheels"
    make_wheel(wheels, "dtdep")
    demo = make_wheel(wheels, "dtdemo", requires=["dtdep"])
    _offline_env(monkeypatch, tmp_path, wheels)
    real = RealInstallers()
    monkeypatch.setattr(skill_deps, "INSTALLERS", real)
    m, config = manager(tmp_path), config_for(tmp_path)
    if installer == "pip":
        monkeypatch.setattr(skill_deps, "_uv", lambda env: None)
        tool = real.python_tool(config, skill_deps.installer_env(config, "user"), tmp_path)
        if isinstance(tool, dict) or tool.installer == "uv":
            pytest.skip(f"no pip: {tool}")
    wrong = f"dtdemo==1.0 --hash=sha256:{'0' * 64}\ndtdep==1.0 --hash={wheel_hash(wheels / 'dtdep-1.0-py3-none-any.whl')}\n"
    (spec,) = plan(m, config, py_skill(tmp_path, "wrong", wrong), "wrong")
    assert spec.manifest.hashed
    assert (await asyncio.wait_for(m.ensure(spec), 300))["status"] == "failed"
    # the transitive dtdep is not listed: both installers in require-hashes mode refuse it
    (spec,) = plan(m, config, py_skill(tmp_path, "partial", f"dtdemo==1.0 --hash={wheel_hash(demo)}\n"), "partial")
    state = await asyncio.wait_for(m.ensure(spec), 300)
    assert state["status"] == "failed" and state["error"].startswith(f"{installer}: "), state
    assert spec.installer.split()[0] == installer


def _node_tool():
    tool = skill_deps.resolve_node(dict(os.environ), Path.cwd())
    if isinstance(tool, dict):
        pytest.skip("Node.js is not installed")
    return tool


@pytest.mark.real_installers
async def test_real_npm_install_and_ci_run_in_the_env_folder(tmp_path, monkeypatch):
    tool = _node_tool()
    tgz = tmp_path / "tgz"
    marker = "require('fs').writeFileSync('POSTINSTALL_RAN', 'x')"
    dep = make_npm_tarball(tgz, "dtdep", scripts={"postinstall": f'node -e "{marker}"'})
    _offline_env(monkeypatch, tmp_path, tmp_path / "wheels")
    monkeypatch.setattr(skill_deps, "INSTALLERS", RealInstallers())
    m = manager(tmp_path)
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)   # npm's INIT_CWD would be this folder if the build ran from here

    def spec(name: str, lock: str | None, scripts: bool, generated: dict) -> EnvSpec:
        manifest = NodeManifest(dir="", files=("package.json",) + ((lock,) if lock else ()), lock=lock,
                                packages=("dtdep",), generated=json.dumps(generated))
        return EnvSpec(skill=name, ecosystem="node", trust="user", manifest=manifest, source=tmp_path, material=(),
                       tool=tool, installer="npm", key="0" * 12, dirname=f"{name}-js-{'0' * 12}",
                       options=skill_deps.BuildOptions(timeout=300, scripts=scripts,
                                                       env=skill_deps.installer_env(config_for(tmp_path), "user")))

    # a test-only manifest (detect refuses file:): written straight into the env by the build
    generated = {"name": "dreamteam-skill-env", "private": True, "dependencies": {"dtdep": f"file:{dep.as_posix()}"}}
    (tmp_path / "package.json").write_text(json.dumps(generated))
    quiet = spec("quiet", None, False, generated)
    state = await asyncio.wait_for(m.ensure(quiet), 300)
    assert state["status"] == "ready", m.log_tail(quiet.dirname)
    assert not (m.dir(quiet.dirname) / "POSTINSTALL_RAN").exists()
    loud = spec("loud", None, True, generated)
    assert (await asyncio.wait_for(m.ensure(loud), 300))["status"] == "ready", m.log_tail(loud.dirname)
    assert (m.dir(loud.dirname) / "node_modules" / "dtdep" / "POSTINSTALL_RAN").exists() or \
        (m.dir(loud.dirname) / "POSTINSTALL_RAN").exists()
    assert not (work / "POSTINSTALL_RAN").exists()
    # npm ci from the lock the first install wrote; the skill's own root postinstall never runs (not copied)
    lock = (m.dir(quiet.dirname) / "package-lock.json").read_text()
    (tmp_path / "package-lock.json").write_text(lock)
    locked = {**generated, "name": "s", "version": "1.0.0"}
    (tmp_path / "package.json").write_text(json.dumps(locked))
    ci = spec("ci", "package-lock.json", True, locked)
    state = await asyncio.wait_for(m.ensure(ci), 300)
    assert state["status"] == "ready", m.log_tail(ci.dirname)
    assert " ci " in " ".join(json.loads((m.dir(ci.dirname) / "env.json").read_text())["argv"][0]) + " "
