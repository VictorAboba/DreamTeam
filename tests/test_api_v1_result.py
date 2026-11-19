"""The external HTTP API v1's result (docs/SPEC.md §21.7): result.build_result with v1's state, the uid, the summary's
goal, the tasks (with parent_id and crew) and members of a running or a stopped session (read from its file, never
loaded), the running jobs and the duration from `api.started_at`; then error, final, board_url and `changed_files`,
the working tree against `api.base_commit` (workspace.changed_since: added, modified, deleted, untracked and held
files; `.scratch` and ignored ones left out; read with DreamTeam's guarded git, so nothing planted in `.git/config`
runs; null with the reason when git is off, there is no base commit or it is gone, or an in-place folder's
repository lies outside it). A final result is cached. Every request goes through the launcher's app and its guard on
the test's own loop (test_api_v1.Hub): no port, ScriptedLLM only, real git."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dreamteam import api_v1, apifolders, hubgit
from dreamteam import settings as st
from dreamteam import workspace as workspace_mod
from dreamteam.api_v1 import BASE_GONE_TEXT, GIT_OFF_TEXT, NO_BASE_TEXT, job_view
from dreamteam.models import USER
from dreamteam.sessions import session_uid
from dreamteam.workspace import BaseCommitGone, Workspace, changed_since, init_repo
from test_api_sessions import git, needs_git, repository
from test_api_v1 import MANUAL, Hub, _home, _no_path, _until
from test_api_v1_events import _manager, running
from test_api_v1_folders import _hub
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures
from test_hub_auth import TOKEN
from test_hub_git import plant_config, racy, ran

KEYS = ["type", "status", "session", "goal", "summary", "tasks", "cost_usd", "duration_s", "agents", "jobs", "error",
        "final", "board_url", "changed_files", "changed_files_error", "changed_files_truncated"]


@pytest.fixture(autouse=True)
def _fresh_scans():
    hubgit.forget()
    yield
    hubgit.forget()


async def result_of(hub: Hub, uid: str, **kwargs: Any) -> dict[str, Any]:
    status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/result", **kwargs)
    assert status == 200, answer
    return answer


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


async def with_files(hub: Hub, files: dict[str, str], **body: Any) -> tuple[str, str]:
    """A v1 session created with these uploads (in its first commit, the base), running: (its id, its folder name)."""
    tokens = []
    for name, text in files.items():
        status, answer = await hub.upload(name, text.encode("utf-8"))
        assert status == 201, answer
        tokens.append(answer["upload"])
    status, answer = await hub.create({"goal": "Write a haiku", **MANUAL, "files": tokens, **body})
    assert status == 202, answer
    await hub.settled()
    return answer["session"]["id"], answer["session"]["name"]


# ---------------------------------------------------------------------------------------------- the result
async def test_the_result_of_a_running_session_is_not_final(tmp_path, api_token, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        parent = swarm.board.create(USER, "Write it")
        child = swarm.board.create(USER, "First line", parent_id=parent.id)
        member = swarm.members()[0]["name"]
        monkeypatch.setattr(swarm.jobs, "running_of", lambda owner=None: [SimpleNamespace(
            id=4, label="dev", owner=member, command="npm run dev " + "x" * 300, origin="shell", status="running")])
        result = await result_of(hub, uid)
        assert list(result) == KEYS
        assert result["type"] == "result" and result["status"] == "running" and result["session"] == uid
        assert result["final"] is False and result["error"] is None and result["summary"] == ""
        assert result["goal"] == {"title": "Write a haiku", "description": "About spring.", "status": "active",
                                  "summary": ""}
        tasks = {task["title"]: task for task in result["tasks"]}
        assert tasks["First line"] == {"id": child.id, "title": "First line", "status": "todo", "assignee": None,
                                       "parent_id": parent.id, "crew": 0}
        assert tasks["Write it"]["parent_id"] is None
        assert result["agents"] == [m["name"] for m in swarm.members()]
        assert result["jobs"] == [{"id": 4, "label": "dev", "owner": member, "command": ("npm run dev " + "x" * 300)[
            :200], "origin": "shell"}]
        assert result["cost_usd"] == 0.0 and isinstance(result["duration_s"], float) and result["duration_s"] >= 0
        assert result["changed_files"] is None and result["changed_files_error"] == GIT_OFF_TEXT
        assert result["changed_files_truncated"] is False
        status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}")
        assert result["board_url"] == answer["session"]["board_url"]
        assert not hub.api._results   # (not final: never kept)
        _no_path(result, tmp_path)
    finally:
        await hub.close()


async def test_a_done_result_is_final_with_the_goals_summary_and_is_kept(tmp_path, api_token, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    manager = hub.manager
    try:
        status, answer = await hub.create({"goal": "Write a haiku", "answers": "manual"})   # (stop_when_done)
        assert status == 202, answer
        await hub.settled()
        uid, sid = answer["session"]["id"], answer["session"]["name"]
        manager.swarms[sid].complete_goal(USER, "The haiku is written.")
        await _until(lambda: sid not in manager.running and manager.is_final(sid), "the stop of the done job")
        result = await result_of(hub, uid)
        assert result["status"] == "done" and result["final"] is True
        assert result["summary"] == "The haiku is written." and result["goal"]["status"] == "done"
        api = manager.meta(sid)["api"]
        assert result["duration_s"] == round(api["finished_at"] - api["started_at"], 3)
        assert [key[:3] for key in hub.api._results] == [(uid, api["finished_at"], "done")]
        monkeypatch.setattr(api_v1, "read_lists", lambda *args: pytest.fail("read again"))   # (kept: not read again)
        monkeypatch.setattr(hub.api, "changed_files", lambda *args: pytest.fail("compared again"))
        assert await result_of(hub, uid) == result
        assert sid not in manager.swarms   # (read from its file)
    finally:
        await hub.close()


async def test_a_result_is_kept_per_state_and_head(tmp_path, api_token):
    """A PATCH of a stopped session writes no finished_at, but may change its state (a budget pause lifted)."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        first = await result_of(hub, uid)
        assert first["status"] == "stopped" and first["final"] is True and len(hub.api._results) == 1
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{uid}", body={"max_cost_usd": 5})
        assert status == 200, answer
        second = await result_of(hub, uid)
        assert second["final"] is True and len(hub.api._results) == 2   # (the PATCH journaled swarm.limits)
    finally:
        await hub.close()


async def test_a_kept_result_says_the_error_its_summary_says_now(tmp_path, api_token, monkeypatch):
    """A `continue` whose text never reached the session records `api.error` and nothing else (no event, no
    finished_at): the kept result answers with that error, as the summary does."""
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        first = await result_of(hub, uid)
        assert first["final"] is True and first["error"] is None and len(hub.api._results) == 1
        await hub.api._not_delivered(sid, uid)
        status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}")
        error = answer["session"]["error"]
        assert error is not None and api_v1.NOT_DELIVERED_TEXT in str(error), error
        monkeypatch.setattr(api_v1, "read_lists", lambda *args: pytest.fail("read again"))   # (still kept)
        second = await result_of(hub, uid)
        assert second["error"] == error and list(second) == list(first)
        assert {**second, "error": None} == first and len(hub.api._results) == 1
    finally:
        await hub.close()


@needs_git
async def test_a_kept_result_goes_once_workspace_git_is_edited_by_hand(tmp_path, api_token):
    """`[workspace] git` turned off in dreamteam.toml while a result is kept: the next result says git is off, as one
    computed afresh does (the settings are read again first)."""
    hub = Hub(_manager(_home(tmp_path, workspace={"git": True})))
    manager = hub.manager
    try:
        uid, sid = await with_files(hub, {"a.txt": "a\n"})
        put(manager.swarms[sid].workspace.root, "new.txt", "new\n")
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        first = await result_of(hub, uid)
        assert first["changed_files"] == [{"path": "new.txt", "change": "added"}] and len(hub.api._results) == 1
        raw = st.read_raw(manager.home)
        raw["workspace"]["git"] = False
        st.write_raw(manager.home, raw)
        second = await result_of(hub, uid)
        assert second["changed_files"] is None and second["changed_files_error"] == GIT_OFF_TEXT
        assert [key[0] for key in hub.api._results] == [uid]   # (kept again, with the settings of now)
    finally:
        await hub.close()


def test_job_view_names_what_a_result_shows():
    job = SimpleNamespace(id=1, label=None, owner="Ann", command="make", origin="shell", status="running",
                          processes=["make"])
    assert job_view(job) == {"id": 1, "label": None, "owner": "Ann", "command": "make", "origin": "shell",
                             "status": "running"}


# ---------------------------------------------------------------------------------------------- changed_files
@needs_git
async def test_changed_files_lists_what_differs_from_the_base_commit(tmp_path, api_token, hub_token, monkeypatch):
    hub = Hub(_manager(_home(tmp_path, workspace={"git": True})))
    manager = hub.manager
    try:
        uid, sid = await with_files(hub, {"a.txt": "a\n", "b.txt": "b\n", "c.md": "c\n", "keep.txt": "k\n"})
        swarm = manager.swarms[sid]
        root = swarm.workspace.root
        base = manager.meta(sid)["api"]["base_commit"]
        assert base == git(root, "rev-parse", "HEAD")
        member = swarm.members()[0]["name"]
        put(root, "a.txt", "a, changed\n")
        (root / "b.txt").unlink()
        put(root, "made.txt", "made\n")
        assert await swarm.workspace.autocommit(member, kind="shell", detail="make")   # (committed since the base)
        put(root, "held.txt", "held\n")
        await swarm.workspace.autocommit(member, kind="shell", detail="again", hold=True)   # (held back, untracked)
        put(root, "sub/deep.txt", "deep\n")
        put(root, ".scratch/anna/notes.txt", "scratch\n")
        put(root, "node_modules/x/index.js", "ignored\n")
        git(root, "rm", "-q", "--cached", "c.md")   # deleted from the index, still on disk: modified
        expected = [{"path": "a.txt", "change": "modified"}, {"path": "b.txt", "change": "deleted"},
                    {"path": "c.md", "change": "modified"}, {"path": "held.txt", "change": "added"},
                    {"path": "made.txt", "change": "added"}, {"path": "sub/deep.txt", "change": "added"}]
        result = await result_of(hub, uid)
        assert result["changed_files"] == expected and result["changed_files_error"] is None
        assert result["changed_files_truncated"] is False
        assert not (root / ".git" / "index.lock").exists()
        # a long list stops at CHANGED_FILES_MAX entries, the first ones by path, and says so
        with monkeypatch.context() as patch:
            patch.setattr(api_v1, "CHANGED_FILES_MAX", 2)
            result = await result_of(hub, uid)
        assert result["changed_files"] == expected[:2] and result["changed_files_truncated"] is True
        # a session not made through v1 has no base commit
        made = await manager.create("Board session", "Something else", start=False)
        other = await result_of(hub, session_uid(manager.meta(made["id"])), token=TOKEN)
        assert other["changed_files"] is None and other["changed_files_error"] == NO_BASE_TEXT
        assert other["duration_s"] is None and other["status"] == "created" and other["final"] is True
        # the base commit gone (a history rewritten), or not a commit id at all (a hand edit of session.json)
        manager._api_write(sid, base_commit="f" * 40)
        result = await result_of(hub, uid)
        assert result["changed_files"] is None and result["changed_files_error"] == BASE_GONE_TEXT
        manager._api_write(sid, base_commit="--output=x")
        result = await result_of(hub, uid)
        assert result["changed_files"] is None and result["changed_files_error"] == NO_BASE_TEXT
        assert not (root / "x").exists()
    finally:
        await hub.close()


@needs_git
async def test_a_stopped_sessions_changes_are_listed_and_a_failure_that_may_pass_is_not_kept(tmp_path, api_token,
                                                                                            monkeypatch):
    """A stopped session is compared with its own settings (manager.config_for: it is not loaded). A git that failed
    once (a timeout, a share unplugged) leaves the final result uncached, so the next request compares again; the
    result that lists the files is kept."""
    hub = Hub(_manager(_home(tmp_path, workspace={"git": True})))
    manager = hub.manager
    try:
        uid, sid = await with_files(hub, {"a.txt": "a\n", "b.txt": "b\n"})
        root = manager.swarms[sid].workspace.root
        put(root, "a.txt", "a, changed\n")
        put(root, "new.txt", "new\n")
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        assert sid not in manager.swarms
        real, calls = api_v1.changed_since, []

        def flaky(*args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            if len(calls) == 1:
                raise OSError("git diff did not finish")
            return real(*args, **kwargs)

        monkeypatch.setattr(api_v1, "changed_since", flaky)
        first = await result_of(hub, uid)
        assert first["final"] is True and first["changed_files"] is None
        assert first["changed_files_error"] == "git could not compare the files: git diff did not finish."
        assert not hub.api._results
        expected = [{"path": "a.txt", "change": "modified"}, {"path": "new.txt", "change": "added"}]
        second = await result_of(hub, uid)
        assert second["changed_files"] == expected and second["changed_files_error"] is None
        assert await result_of(hub, uid) == second and len(calls) == 2 and len(hub.api._results) == 1   # (kept)
        assert sid not in manager.swarms
    finally:
        await hub.close()


@needs_git
async def test_nothing_planted_in_the_repository_runs_while_the_changes_are_read(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path, workspace={"git": True})))
    try:
        uid, sid = await with_files(hub, {"notes.md": "# notes\n", "a.txt": "a\n"})
        root = hub.manager.swarms[sid].workspace.root
        marks = tmp_path / "marks"
        marks.mkdir()
        plant_config(root, marks)
        racy(root / "notes.md", "# NOTES\n", root / ".git" / "index")   # (same size: git must read the content)
        put(root, "fresh.md", "new\n")
        result = await result_of(hub, uid)
        assert ran(marks) == []
        changed = {item["path"]: item["change"] for item in result["changed_files"]}
        assert changed["notes.md"] == "modified" and changed["fresh.md"] == "added"
        assert changed[".gitattributes"] == "added" and "a.txt" not in changed
    finally:
        await hub.close()


@needs_git
async def test_an_in_place_folder_whose_git_leads_outside_it_is_not_read(tmp_path, api_token, monkeypatch):
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    repo = places / "repo"
    repository(repo, 2)
    try:
        status, answer = await hub.create({"goal": "G", **MANUAL,
                                           "folder": {"root": "places", "path": "repo", "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        uid = answer["session"]["id"]
        put(repo, "note.txt", "hi\n")
        result = await result_of(hub, uid)
        assert result["changed_files"] == [{"path": "note.txt", "change": "added"}]
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        moved = tmp_path / "outside" / "repo.git"
        moved.parent.mkdir(parents=True)
        (repo / ".git").rename(moved)
        (repo / ".git").write_text(f"gitdir: {moved}\n", encoding="utf-8")
        result = await result_of(hub, uid)
        assert result["changed_files"] is None and "gitfile" in result["changed_files_error"]
        _no_path(result, tmp_path)
        assert not hub.api._results   # (a refusal may pass: never kept)
        # git failing on the folder's repository: DreamTeam's words, never git's output (it may name a path)
        monkeypatch.setattr(apifolders, "git_dir_refusal", lambda *args, **kwargs: (
            f"{apifolders.GIT_UNREADABLE}: fatal: bad config line 1 in file {repo / '.git' / 'config'}"))
        result = await result_of(hub, uid)
        assert result["changed_files_error"] == f"Its folder's repository is not read: {apifolders.GIT_UNREADABLE}."
    finally:
        await hub.close()


@needs_git
@pytest.mark.parametrize("edit", ["removed", "copy only"])
@pytest.mark.parametrize("kept", [False, True])
async def test_a_hand_edit_of_the_folder_roots_counts_at_once(tmp_path, api_token, edit, kept):
    """[api] folders no longer lists the root of an in-place session, or lists it for copies only: its folder is not
    read, as no start runs there (the settings are read again first, as a start reads them), also when a result of
    the session was kept before the edit."""
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    repository(places / "repo", 2)
    try:
        status, answer = await hub.create({"goal": "G", **MANUAL,
                                           "folder": {"root": "places", "path": "repo", "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        uid = answer["session"]["id"]
        put(places / "repo", "new.txt", "new\n")
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        if kept:
            first = await result_of(hub, uid)
            assert first["changed_files"] == [{"path": "new.txt", "change": "added"}] and len(hub.api._results) == 1
        raw = st.read_raw(hub.manager.home)
        if edit == "removed":
            del raw["api"]["folders"]["places"]
        else:
            raw["api"]["folders"]["places"]["in_place"] = False
        st.write_raw(hub.manager.home, raw)
        result = await result_of(hub, uid)
        assert result["changed_files"] is None and "no longer lists" in result["changed_files_error"]
        assert not hub.api._results   # (a refusal may pass: never kept)
        _no_path(result, tmp_path)
        status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/files")
        assert status == 403 and answer["code"] == "forbidden_folder", answer
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- workspace.changed_since
@needs_git
def test_changed_since_reads_the_working_tree_against_a_commit(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    for name in ("a.txt", "b.txt", "c.txt"):
        put(root, name, f"{name}\n")
    assert init_repo(root) is None
    base = git(root, "rev-parse", "HEAD")
    assert changed_since(root, base) == []
    put(root, "a.txt", "changed\n")
    (root / "b.txt").unlink()
    put(root, "new/one.txt", "1\n")
    put(root, ".scratch/x.txt", "x\n")
    put(root, "__pycache__/m.pyc", "x")
    git(root, "rm", "-q", "--cached", "c.txt")
    seen: list[dict[str, str]] = []
    real = hubgit.run

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return real(argv, **kwargs)

    monkeypatch.setattr(hubgit, "run", run)
    assert changed_since(root, base) == [{"path": "a.txt", "change": "modified"},
                                         {"path": "b.txt", "change": "deleted"},
                                         {"path": "c.txt", "change": "modified"},
                                         {"path": "new/one.txt", "change": "added"}]
    assert seen and all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in seen)
    with pytest.raises(ValueError):
        changed_since(root, "--output=x")
    with pytest.raises(ValueError):
        changed_since(root, "HEAD")
    with pytest.raises(BaseCommitGone):
        changed_since(root, "0" * 40)
    tree = git(root, "rev-parse", "HEAD^{tree}")
    with pytest.raises(BaseCommitGone):   # (an object that is no commit)
        changed_since(root, tree)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(OSError):
        changed_since(plain, base)
    # a repository git cannot open is a failure, never "the commit is gone"
    (root / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")
    with pytest.raises(OSError):
        changed_since(root, base)


@needs_git
async def test_the_workspace_reads_its_changes_without_its_git_lock(fake_swarm):
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    base = git(ws.root, "rev-parse", "HEAD")
    put(ws.root, "out.txt", "result\n")
    async with ws._git_lock:   # (an agent's commit in progress never makes it wait)
        assert await asyncio.wait_for(ws.changed_since(base), 30) == [{"path": "out.txt", "change": "added"}]


def test_a_commit_id_is_hex_of_sha1_or_sha256():
    assert workspace_mod.COMMIT_ID.fullmatch("a" * 40) and workspace_mod.COMMIT_ID.fullmatch("0" * 64)
    for bad in ("A" * 40, "a" * 39, "a" * 41, "a" * 63, "--output=x", "HEAD", ""):
        assert not workspace_mod.COMMIT_ID.fullmatch(bad)

