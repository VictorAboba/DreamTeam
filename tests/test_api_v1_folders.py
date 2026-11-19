"""The HTTP API v1's server folders (docs/SPEC.md §21.10): `[api] folders` roots named by a create's `folder`, copied
(read below the root without following a link) or worked in in place, the checks of the roots and of the paths, the
link swapped in at each step, one folder for one session (by file identity too), the git rules of an in-place
folder (no gitfile, no git dir or common dir outside it), the checks at every later start, DELETE keeping the folder,
answers that never name a path of the hub's computer, and the roots in the Docker sandbox (mounted after a check on
the host, found there through DREAMTEAM_API_FOLDERS_DIR in the container only; no Docker runs: the argv and the
container's hub are tested). Every request goes through the launcher's app and its guard
on the test's own loop (test_api_v1.Hub): no port, ScriptedLLM only. Links are junctions on Windows (no privilege
needed) and symlinks elsewhere."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from starlette.testclient import TestClient

from dreamteam import apifolders, cli, copyfrom, docker as docker_module, hubauth, netoff, nofollow, sessions, shell
from dreamteam import settings as st
from dreamteam.config import API_FOLDERS_ENV, ApiFolder, Config
from dreamteam.copyfrom import CopyError, CopyLimits, Source, copy_sources
from dreamteam.launcher import create_launcher_app
from dreamteam.sessions import FOLDER_BUSY, SessionError, SessionManager
from dreamteam.workspace import rmtree
from test_api_sessions import git, needs_git, repository
from test_api_v1 import Hub, _home, _manager, _no_path, no_start  # noqa: F401 - a fixture
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures
from test_hub_auth import TOKEN

WINDOWS = sys.platform == "win32"
SECRET = b"SECRET outside the root"
LIMITS = CopyLimits(max_mb=1, max_files=100)


def _write(path: Path, data: bytes | str = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _dir_link(target: Path, link: Path) -> None:
    """A link to the folder `target` at `link`: a junction on Windows, a symlink elsewhere."""
    if WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _swap_for_link(path: Path, target: Path) -> None:
    """Put a link to the folder `target` in place of the folder `path` (moved aside to `<name>.old`)."""
    path.rename(path.with_name(path.name + ".old"))
    _dir_link(target, path)


def _outside(tmp_path: Path) -> Path:
    """A folder outside every root, with a secret in it (and in a `site` folder)."""
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", SECRET)
    _write(outside / "site" / "secret.txt", SECRET)
    return outside


def _files(folder: Path) -> dict[str, bytes]:
    """Every regular file below `folder` (no link followed): relative path -> bytes."""
    out = {}
    for base, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))
                   and not _is_junction(Path(base, d))]
        for name in names:
            path = Path(base, name)
            if not path.is_symlink():
                out[path.relative_to(folder).as_posix()] = path.read_bytes()
    return out


def _is_junction(path: Path) -> bool:
    return bool(getattr(path, "is_junction", lambda: False)())


def _hub(tmp_path: Path, *, git_on: bool = False, folders: dict[str, Any] | None = None,
         **sections: Any) -> tuple[Hub, Path, Path]:
    """A hub whose home lists two roots: `jobs` (copies only) and `places` (in place too)."""
    jobs, places = tmp_path / "srv" / "jobs", tmp_path / "srv" / "places"
    jobs.mkdir(parents=True, exist_ok=True)
    places.mkdir(parents=True, exist_ok=True)
    roots = folders if folders is not None else {"jobs": {"path": str(jobs)},
                                                 "places": {"path": str(places), "in_place": True}}
    workspace = {"git": git_on, **sections.pop("workspace", {})}
    api = {"max_running": 0, "folders": roots, **sections.pop("api", {})}
    return Hub(_manager(_home(tmp_path, workspace=workspace, api=api, **sections))), jobs, places


def _site(jobs: Path) -> Path:
    """jobs/acme/site: what a copy takes, and what it leaves out."""
    site = jobs / "acme" / "site"
    _write(site / "index.html", "<h1>Acme</h1>")
    _write(site / "src" / "app.js", "run()")
    _write(site / "src" / ".gitignore", "build/")     # (only the top-level .gitignore is the session's own)
    _write(site / ".git" / "config", "[core]")         # a nested repository's, at any depth too
    _write(site / "src" / ".git" / "HEAD", "ref")
    _write(site / "node_modules" / "x" / "i.js", "x")
    _write(site / ".gitignore", "*")                   # the session's own names, at the top
    _write(site / ".scratch" / "anna" / "notes.md", "n")
    _write(site / "mcp" / "calc" / "server.py", "s")
    _write(site / "Thumbs.db", "litter")
    return site


async def _state(hub: Hub, uid: str) -> dict[str, Any]:
    status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}")
    assert status == 200, answer
    return answer["session"]


# ---- copy ---------------------------------------------------------------------------------------------------------
async def test_a_copy_takes_the_folder_and_leaves_out_links_hard_links_and_the_sessions_names(tmp_path, api_token,
                                                                                              no_start):
    hub, jobs, _places = _hub(tmp_path)
    no_start(hub.manager)
    outside = _outside(tmp_path)
    site = _site(jobs)
    _dir_link(outside, site / "out")                       # a link that leads out: left out, never followed
    os.link(outside / "secret.txt", site / "hard.txt")     # another name of a file outside the root: never read
    before = _files(site)
    try:
        status, answer = await hub.create({"goal": "Fix the site", "folder": {"root": "jobs", "path": "acme/site"}})
        assert status == 202, answer
        session = answer["session"]
        assert session["folder"] == {"root": "jobs", "path": "acme/site", "mode": "copy"}
        _no_path(answer, tmp_path)
        sid = session["name"]
        workspace = hub.manager.default_workspace(sid)
        assert _files(workspace) == {"index.html": b"<h1>Acme</h1>", "src/app.js": b"run()",
                                     "src/.gitignore": b"build/"}
        assert not os.path.lexists(workspace / "out") and not os.path.lexists(workspace / "hard.txt")
        assert _files(site) == before   # the original is never written
        meta = hub.manager.meta(sid)
        assert meta["api"]["folder"] == {"root": "jobs", "path": "acme/site", "mode": "copy"}
        assert meta["copied_from"]["source"] == "jobs/acme/site" and meta["copied_from"]["files"] == 3
        assert meta.get("workspace") is None   # (its own folder: a copy is never busy, never kept)
    finally:
        await hub.close()


async def test_a_zip_under_a_root_is_unpacked_as_an_upload_and_never_a_hard_linked_one(tmp_path, api_token,
                                                                                      no_start):
    hub, jobs, _places = _hub(tmp_path)
    no_start(hub.manager)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        for name in ("site/index.html", "site/.gitignore", "site/.scratch/x.md", "site/docs/a.md"):
            zf.writestr(name, name)
    _write(jobs / "drop" / "site.zip", data.getvalue())
    _write(jobs / "drop" / "twice.zip", data.getvalue())
    os.link(jobs / "drop" / "twice.zip", tmp_path / "elsewhere.zip")
    try:
        status, answer = await hub.create({"goal": "Unpack", "folder": {"root": "jobs", "path": "drop/site.zip"}})
        assert status == 202, answer
        workspace = hub.manager.default_workspace(answer["session"]["name"])
        assert _files(workspace) == {"index.html": b"site/index.html", "docs/a.md": b"site/docs/a.md"}
        status, answer = await hub.create({"goal": "Unpack", "folder": {"root": "jobs", "path": "drop/twice.zip"}})
        assert status == 403 and answer["code"] == "forbidden_folder", answer
        assert "jobs/drop/twice.zip" in answer["error"] and len(hub.manager.ids()) == 1
        _no_path(answer, tmp_path)
    finally:
        await hub.close()


async def test_a_copy_names_a_folder_or_a_zip_that_is_there(tmp_path, api_token, no_start):
    hub, jobs, _places = _hub(tmp_path, workspace={"copy_max_files": 3})
    no_start(hub.manager)
    _write(jobs / "readme.txt", "r")
    for n in range(4):
        _write(jobs / "many" / f"f{n}.txt", "x")
    try:
        cases = [("missing", 404, "not_found"), ("readme.txt", 400, "bad_request"), ("many", 413, "too_large")]
        for path, status_wanted, code in cases:
            status, answer = await hub.create({"goal": "G", "folder": {"root": "jobs", "path": path}})
            assert (status, answer["code"]) == (status_wanted, code), answer
            assert f"jobs/{path}" in answer["error"], answer
            _no_path(answer, tmp_path)
        assert hub.manager.ids() == []
        for n in (2, 3):
            (jobs / "many" / f"f{n}.txt").unlink()
        status, answer = await hub.create({"goal": "G", "folder": {"root": "jobs"}})   # the root itself
        assert status == 202 and answer["session"]["folder"] == {"root": "jobs", "path": "", "mode": "copy"}
        assert set(_files(hub.manager.default_workspace(answer["session"]["name"]))) == {"readme.txt",
                                                                                          "many/f0.txt",
                                                                                          "many/f1.txt"}
    finally:
        await hub.close()


async def test_files_land_next_to_a_copy_and_a_clash_leaves_every_token_usable(tmp_path, api_token, no_start):
    hub, jobs, _places = _hub(tmp_path)
    no_start(hub.manager)
    _site(jobs)
    uploads = hub.manager.api_uploads
    try:
        brief = (await hub.upload("brief.md", b"# Brief"))[1]["upload"]
        clash = (await hub.upload("index.html", b"mine"))[1]["upload"]
        folder = {"root": "jobs", "path": "acme/site"}
        status, answer = await hub.create({"goal": "G", "folder": folder, "files": [clash]})
        assert status == 409 and answer["code"] == "file_conflict" and "jobs/acme/site" in answer["error"], answer
        assert [m["token"] for m in uploads.peek_many([clash], caller="api")] == [clash]
        status, answer = await hub.create({"goal": "G", "folder": {**folder, "mode": "in_place"}, "files": [brief]})
        assert status == 400 and answer["code"] == "bad_request" and "in_place" in answer["error"]
        status, answer = await hub.create({"goal": "G", "folder": folder, "files": [brief]})
        assert status == 202, answer
        workspace = hub.manager.default_workspace(answer["session"]["name"])
        assert (workspace / "brief.md").read_bytes() == b"# Brief" and (workspace / "index.html").exists()
        assert hub.manager.meta(answer["session"]["name"])["copied_from"]["source"] == \
            "jobs/acme/site, upload: brief.md"
        assert not any(name.startswith(brief) for name in copyfrom._IN_USE)
    finally:
        await hub.close()


async def test_two_copies_of_one_folder_at_once(tmp_path, api_token, no_start):
    hub, jobs, _places = _hub(tmp_path)
    no_start(hub.manager)
    _site(jobs)
    try:
        body = {"goal": "G", "folder": {"root": "jobs", "path": "acme/site"}}
        answers = await asyncio.gather(hub.create(body), hub.create(body))
        assert [status for status, _ in answers] == [202, 202], answers
        for _, answer in answers:
            assert "index.html" in _files(hub.manager.default_workspace(answer["session"]["name"]))
    finally:
        await hub.close()


async def test_a_retry_that_spells_the_folder_another_way_is_a_repeat(tmp_path, api_token, no_start):
    """request_digest takes the folder as parsed: a path or mode left out, null or given as its default is one
    request (docs/SPEC.md §21.4 `client_ref`); another path is another job (409)."""
    hub, jobs, _places = _hub(tmp_path)
    no_start(hub.manager)
    _site(jobs)
    try:
        status, first = await hub.create({"goal": "G", "client_ref": "job-1", "folder": {"root": "jobs"}})
        assert status == 202, first
        for folder in ({"root": "jobs", "path": None}, {"root": "jobs", "path": "", "mode": "copy"},
                       {"root": "jobs", "mode": None}):
            status, again = await hub.create({"goal": "G", "client_ref": "job-1", "folder": folder})
            assert status == 200 and again["session"]["id"] == first["session"]["id"], (folder, again)
        status, other = await hub.create({"goal": "G", "client_ref": "job-1", "folder": {"root": "jobs",
                                                                                        "path": "acme"}})
        assert status == 409 and other["code"] == "conflict", other
        assert len(hub.manager.ids()) == 1
    finally:
        await hub.close()


# ---- the request's checks -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["..", "a/../b", "/abs", "a\\b", "a\x00b", ".git", "acme/.GIT", "a:b", "CON",
                                  "aux.txt", "x/.scratch", ".scratch", "GIT~1", "acme ", "acme.", "a//b", "a\u202eb"])
async def test_a_path_a_client_may_not_make_is_a_bad_name(tmp_path, api_token, path):
    hub, _jobs, _places = _hub(tmp_path)
    try:
        for mode in ("copy", "in_place"):
            status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": path, "mode": mode}})
            assert status == 400 and answer["code"] == "bad_name", (path, answer)
        assert hub.manager.ids() == []
    finally:
        await hub.close()


async def test_the_folder_names_a_listed_root_and_a_mode_it_allows(tmp_path, api_token):
    hub, _jobs, _places = _hub(tmp_path)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "nope"}})
        assert status == 400 and answer["code"] == "unknown_folder" and "Roots: jobs, places" in answer["error"]
        status, answer = await hub.create({"goal": "G", "folder": {"root": "jobs", "mode": "in_place"}})
        assert status == 403 and answer["code"] == "forbidden_folder" and "copies only" in answer["error"]
        for folder in ({"path": "x"}, {"root": ""}, {"root": "jobs", "path": 3}, {"root": "jobs", "mode": "move"},
                       {"root": ["jobs"]}):
            status, answer = await hub.create({"goal": "G", "folder": folder})
            assert status == 400 and answer["code"] == "bad_request", (folder, answer)
        status, answer = await hub.create({"goal": "G", "folder": {"root": "jobs", "where": "x"}})
        assert status == 400 and answer["code"] == "unknown_field"
        assert hub.manager.ids() == []
        status, answer = await hub.call("GET", "/api/v1/options")
        assert answer["folders"] == [{"name": "jobs", "in_place": False}, {"name": "places", "in_place": True}]
        _no_path(answer, tmp_path)
    finally:
        await hub.close()


async def test_a_link_under_the_root_that_leads_out_is_forbidden(tmp_path, api_token, no_start):
    hub, jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    outside = _outside(tmp_path)
    _dir_link(outside, jobs / "out")
    _dir_link(outside, places / "out")
    try:
        for folder in ({"root": "jobs", "path": "out"}, {"root": "jobs", "path": "out/site"},
                       {"root": "places", "path": "out", "mode": "in_place"},
                       {"root": "places", "path": "out/new", "mode": "in_place"}):
            status, answer = await hub.create({"goal": "G", "folder": folder})
            assert status == 403 and answer["code"] == "forbidden_folder", (folder, answer)
            _no_path(answer, tmp_path)
        assert hub.manager.ids() == [] and not (outside / "new").exists()
        (places / "inside").mkdir()
        _dir_link(places / "inside", places / "alias")   # a link that stays inside the root is followed
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "alias/new",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        assert (places / "inside" / "new").is_dir()
    finally:
        await hub.close()


async def test_a_link_swapped_in_after_the_request_was_resolved_ends_the_create(tmp_path, api_token, no_start,
                                                                                 monkeypatch):
    """The containment is checked again on the create's own way: for a copy by the walk below the root, which never
    follows a link; in place by the last check before the missing folders are made (_change_folder's _plan)."""
    hub, jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    outside = _outside(tmp_path)
    _site(jobs)
    (places / "acme").mkdir()
    real_resolve = apifolders.resolve_request

    def resolve_then_swap(*args: Any, **kwargs: Any) -> apifolders.FolderRequest:
        resolved = real_resolve(*args, **kwargs)
        _swap_for_link(jobs / "acme", outside)
        return resolved

    monkeypatch.setattr(apifolders, "resolve_request", resolve_then_swap)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "jobs", "path": "acme/site"}})
        assert status == 403 and answer["code"] == "forbidden_folder" and "jobs/acme/site" in answer["error"], answer
        _no_path(answer, tmp_path)
        assert hub.manager.ids() == []
        monkeypatch.setattr(apifolders, "resolve_request", real_resolve)
        real_change = SessionManager._change_folder

        async def swap_then_change(self, *args: Any, **kwargs: Any):
            _swap_for_link(places / "acme", outside)
            return await real_change(self, *args, **kwargs)

        monkeypatch.setattr(SessionManager, "_change_folder", swap_then_change)
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme/site/new",
                                                                   "mode": "in_place"}})
        assert status == 403 and answer["code"] == "forbidden_folder", answer
        _no_path(answer, tmp_path)
        assert hub.manager.ids() == [] and not (outside / "site" / "new").exists()
    finally:
        await hub.close()


@pytest.mark.parametrize("when", ["before_carry_out", pytest.param("during_git_init", marks=needs_git)])
async def test_a_link_swapped_in_after_the_last_plan_leaves_nothing_outside(tmp_path, api_token, no_start,
                                                                             monkeypatch, when):
    """_carry_out finds the folders it makes inside the root again: a link swapped in after _change_folder's last
    _plan (under the workspace claims) ends the create before a folder is made outside, and one swapped in while git
    init runs (by path) is found right after it, the repository it made outside removed by the undo."""
    hub, _jobs, places = _hub(tmp_path, git_on=when == "during_git_init")
    no_start(hub.manager)
    outside = _outside(tmp_path)
    (places / "acme" / "site").mkdir(parents=True)
    if when == "before_carry_out":
        real_carry_out = SessionManager._carry_out

        def swap_then_carry_out(self, sid, plan, undo):
            if plan.within is not None:
                _swap_for_link(places / "acme", outside)
            return real_carry_out(self, sid, plan, undo)

        monkeypatch.setattr(SessionManager, "_carry_out", swap_then_carry_out)
    else:
        (outside / "site" / "new").mkdir()   # where the swapped-in junction leads git init
        real_init = sessions.init_repo

        def swap_then_init(folder: Path, *args: Any, **kwargs: Any) -> str | None:
            _swap_for_link(places / "acme", outside)
            return real_init(folder, *args, **kwargs)

        monkeypatch.setattr(sessions, "init_repo", swap_then_init)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme/site/new",
                                                                   "mode": "in_place"}})
        assert status == 403 and answer["code"] == "forbidden_folder", answer
        assert "places/acme/site" in answer["error"].replace(os.sep, "/"), answer
        _no_path(answer, tmp_path)
        assert hub.manager.ids() == [] and os.listdir(outside / "site") == ["secret.txt"]
        made = os.listdir(places / "acme.old" / "site")   # (made inside before the swap: an empty folder at most)
        assert made == ([] if when == "before_carry_out" else ["new"])
        assert all(os.listdir(places / "acme.old" / "site" / name) == [] for name in made)
    finally:
        await hub.close()


def test_a_level_made_through_a_link_swapped_in_meanwhile_is_refused_and_removed(tmp_path, monkeypatch):
    """_carry_out makes the missing levels by path: one made through a junction swapped in at that instant is found
    outside the root right after, refused (403), and the undo removes it."""
    manager = _manager(_home(tmp_path))
    root = tmp_path / "srv" / "places"
    (root / "acme").mkdir(parents=True)
    root = root.resolve()
    outside = _outside(tmp_path)
    target = root / "acme" / "a" / "b"
    plan = sessions._Plan(target=target, old=target, mode="moved", default=False, adopted=False, git="none",
                          old_exists=False, within=root)
    real_mkdir = Path.mkdir

    def mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == root / "acme" / "a":   # the junction comes in just before the level is made
            _swap_for_link(root / "acme", outside)
        real_mkdir(self, *args, **kwargs)

    undo = sessions._Undo()
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(SessionError) as err:
        manager._carry_out(None, plan, undo)
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    assert err.value.status == 403 and err.value.code == "forbidden_folder"
    assert (outside / "a").is_dir() and not (outside / "a" / "b").exists()   # (made outside, nothing below it)
    assert undo.run() == []
    assert not (outside / "a").exists() and os.listdir(root / "acme.old") == []


@pytest.mark.parametrize("case", ["user_home", "contains_user_home", "dreamteam_home", "inside_dreamteam_home",
                                  "other_home", "drive_root", "foreign_path", "missing", "a_file"])
async def test_a_root_is_checked_where_it_lies_at_each_use(tmp_path, api_token, monkeypatch, case):
    user = tmp_path / "people" / "me"
    user.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: user)
    paths = {
        "user_home": str(user), "contains_user_home": str(tmp_path / "people"), "dreamteam_home": str(tmp_path),
        "inside_dreamteam_home": str(tmp_path / "home" / "drop"), "other_home": str(tmp_path / "other" / "x"),
        "drive_root": tmp_path.anchor, "foreign_path": "/srv/jobs" if WINDOWS else "C:\\jobs",
        "missing": str(tmp_path / "gone"), "a_file": str(tmp_path / "file.txt"),
    }
    wanted = {"missing": (404, "not_found"), "a_file": (404, "not_found")}.get(case, (403, "forbidden_folder"))
    _write(tmp_path / "other" / "dreamteam.toml", "")
    (tmp_path / "other" / "sessions").mkdir()
    (tmp_path / "other" / "x").mkdir()
    _write(tmp_path / "file.txt")
    hub, _jobs, _places = _hub(tmp_path, folders={"root": {"path": paths[case], "in_place": True}})
    if case == "inside_dreamteam_home":
        (tmp_path / "home" / "drop").mkdir()
    try:
        for mode in ("copy", "in_place"):
            status, answer = await hub.create({"goal": "G", "folder": {"root": "root", "mode": mode}})
            assert (status, answer["code"]) == wanted, (mode, answer)
            assert "'root'" in answer["error"], answer
            _no_path(answer, tmp_path, user)
        assert hub.manager.ids() == []
    finally:
        await hub.close()


async def test_the_hub_logs_the_roots_it_cannot_use_at_its_start(tmp_path, caplog):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    home = _home(tmp_path, api={"folders": {"jobs": {"path": str(jobs)}, "gone": {"path": str(tmp_path / "gone")},
                                            "all": {"path": str(tmp_path)}}})
    manager = _manager(home)
    with caplog.at_level(logging.INFO, logger=apifolders.__name__):
        apifolders.log_refusals(manager.global_config(), manager.home)
    logged = {record.getMessage().split("]")[0][1:]: record.levelno for record in caplog.records}
    assert logged == {"api.folders.gone": logging.INFO, "api.folders.all": logging.WARNING}


def test_the_launcher_checks_the_roots_when_it_starts(tmp_path, caplog):
    """The launcher's lifespan starts the check (task "api-folders-check", in a daemon thread)."""
    home = _home(tmp_path, api={"folders": {"all": {"path": str(tmp_path)}}})
    manager = _manager(home)

    def logged() -> bool:
        return any(record.getMessage().startswith("[api.folders.all]") for record in caplog.records)

    with caplog.at_level(logging.WARNING, logger=apifolders.__name__):
        with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as client:
            deadline = time.monotonic() + 1
            while not logged() and time.monotonic() < deadline:
                time.sleep(0.02)
            client.portal.call(manager.shutdown)
    assert logged()


# ---- in place ----------------------------------------------------------------------------------------------------
@needs_git
@pytest.mark.parametrize("path", ["new/site", "empty"])
async def test_in_place_in_a_missing_or_empty_folder_makes_a_repository_and_delete_keeps_it(tmp_path, api_token,
                                                                                            path):
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    (places / "empty").mkdir()
    try:
        folder = {"root": "places", "path": path, "mode": "in_place"}
        status, answer = await hub.create({"goal": "Build a site", "folder": folder})
        assert status == 202, answer
        session = answer["session"]
        assert session["folder"] == folder
        _no_path(answer, tmp_path)
        target = places.joinpath(*path.split("/"))
        assert (target / ".git").is_dir() and (target / ".gitignore").is_file()
        await hub.settled()
        assert (await _state(hub, session["id"]))["state"] == "running"
        meta = hub.manager.meta(session["name"])
        assert Path(meta["workspace"]["path"]) == target.resolve() and meta["api"]["folder"] == folder
        assert meta["api"]["base_commit"] == git(target, "rev-parse", "HEAD")
        status, answer = await hub.call("DELETE", f"/api/v1/sessions/{session['id']}", query=f"confirm={session['id']}")
        assert status == 200 and answer == {"ok": True, "folder_kept": {"root": "places", "path": path}}
        assert (target / ".git").is_dir() and hub.manager.ids() == []
    finally:
        await hub.close()


@needs_git
async def test_in_place_in_a_clean_repository_works_on_it(tmp_path, api_token):
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    commits = repository(places / "repo", 2)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "repo",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        sid = answer["session"]["name"]
        meta = hub.manager.meta(sid)
        assert meta["workspace"]["adopted"] is True and meta["api"]["base_commit"] == commits[-1]
        assert (await _state(hub, answer["session"]["id"]))["state"] == "running"
        # DreamTeam's commits land in the folder's own repository, on the owner's branch
        repo = places / "repo"
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        _write(repo / "note.txt", "hi\n")
        assert await hub.manager.swarms[sid].workspace.autocommit("Ava", "shell: echo hi > note.txt")
        head = git(repo, "rev-parse", "HEAD")
        assert head != commits[-1] and commits[-1] in git(repo, "rev-list", "HEAD").split()
        assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["note.txt"]
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == branch and git(repo, "status", "--porcelain") == ""
        assert Path(git(repo, "rev-parse", "--absolute-git-dir")).resolve() == (repo / ".git").resolve()
    finally:
        await hub.close()


@needs_git
async def test_a_dirty_repository_is_not_ready_at_the_create_and_at_a_later_start(tmp_path, api_token):
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    repository(places / "repo", 1)
    _write(places / "repo" / "draft.txt", "mine")
    folder = {"root": "places", "path": "repo", "mode": "in_place"}
    try:
        status, answer = await hub.create({"goal": "G", "folder": folder})
        assert status == 409 and answer["code"] == "folder_not_ready", answer
        assert f"places{os.sep}repo" in answer["error"]   # (a path of the hub's computer: named below its root)
        _no_path(answer, tmp_path)
        (places / "repo" / "draft.txt").unlink()
        status, answer = await hub.create({"goal": "G", "folder": folder})
        assert status == 202, answer
        uid = answer["session"]["id"]
        await hub.settled()
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        _write(places / "repo" / "draft.txt", "mine again")   # the owner worked in it meanwhile
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/continue", body={"text": "Go on"})
        assert status == 200, answer
        await hub.settled()
        session = await _state(hub, uid)
        assert session["state"] == "failed" and "uncommitted changes" in session["error"], session
        assert f"places{os.sep}repo" in session["error"]
        _no_path(session, tmp_path)
        assert hub.manager.meta(session["name"])["api"]["start_error"]["code"] == "folder_not_ready"
    finally:
        await hub.close()


def _linked_worktree(tmp_path: Path, folder: Path) -> Path:
    """A repository outside the roots with a linked worktree at `folder` (its .git a gitfile); the main one."""
    main = tmp_path / "outside" / "main"
    repository(main, 1)
    git(main, "worktree", "add", "-q", "-b", "side", str(folder))
    return main


def _git_link_inside(folder: Path, outside: Path, case: str) -> None:
    """In the repository `folder`, something inside its `.git` folder that leads outside: `objects` moved to `outside`
    and linked back, `logs/refs/heads` linked to `outside`, or the reflog `logs/HEAD` a hard link of a file there."""
    dot = folder / ".git"
    outside.mkdir(parents=True, exist_ok=True)
    if case == "objects_link":
        (dot / "objects").rename(outside / "objects")
        _dir_link(outside / "objects", dot / "objects")
    elif case == "logs_heads_link":
        rmtree(dot / "logs" / "refs" / "heads")
        _dir_link(outside, dot / "logs" / "refs" / "heads")
    else:
        victim = _write(outside / "victim.toml", "[settings]\n")
        (dot / "logs" / "HEAD").unlink()
        os.link(victim, dot / "logs" / "HEAD")


@needs_git
@pytest.mark.parametrize("case", ["gitfile", "link", "commondir", "objects_link", "logs_heads_link",
                                  "reflog_hard_link"])
async def test_a_repository_whose_git_writes_outside_the_folder_is_not_ready(tmp_path, api_token, case):
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    folder = places / "wt"
    outside = tmp_path / "outside" / "git"
    if case in ("objects_link", "logs_heads_link", "reflog_hard_link"):
        repository(folder, 1)
        _git_link_inside(folder, outside, case)
        before = _files(outside)
    elif case == "gitfile":
        _linked_worktree(tmp_path, folder)
    elif case == "link":
        main = tmp_path / "outside" / "main"
        repository(main, 1)
        _write(folder / "file0.txt", "0\n")
        _dir_link(main / ".git", folder / ".git")
    else:   # a .git folder of its own whose common dir (the objects and refs) lies outside
        main = _linked_worktree(tmp_path, folder)
        private = Path(git(main, "rev-parse", "--absolute-git-dir")) / "worktrees" / "wt"
        (folder / ".git").unlink()
        rmtree_copy = folder / ".git"
        rmtree_copy.mkdir()
        for name in os.listdir(private):
            if (private / name).is_file():
                (rmtree_copy / name).write_bytes((private / name).read_bytes())
        (rmtree_copy / "commondir").write_text(str(main / ".git") + "\n", encoding="utf-8")
        (rmtree_copy / "gitdir").write_text(str(rmtree_copy) + "\n", encoding="utf-8")
        assert git(folder, "rev-parse", "--git-common-dir").replace("\\", "/").endswith("main/.git")
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "wt",
                                                                   "mode": "in_place"}})
        assert status == 409 and answer["code"] == "folder_not_ready", answer
        assert f"places{os.sep}wt" in answer["error"]
        _no_path(answer, tmp_path)
        assert hub.manager.ids() == []
        if case in ("objects_link", "logs_heads_link", "reflog_hard_link"):
            assert _files(outside) == before   # (refused before git ran there)
            assert ("hard link" if case == "reflog_hard_link" else "link or a junction") in answer["error"]
    finally:
        await hub.close()


@needs_git
@pytest.mark.parametrize("case", ["gitfile", "objects_link"])
async def test_a_git_folder_changed_after_the_create_refuses_the_start(tmp_path, api_token, case):
    hub, _jobs, places = _hub(tmp_path, git_on=True)
    main = tmp_path / "outside" / "main"
    repository(main, 1)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "new",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        uid = answer["session"]["id"]
        await hub.settled()
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        if case == "gitfile":
            rmtree(places / "new" / ".git")
            _write(places / "new" / ".git", f"gitdir: {main / '.git'}\n")
        else:
            _git_link_inside(places / "new", tmp_path / "outside" / "git", case)
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/continue", body={"text": "Go on"}))[0] == 200
        await hub.settled()
        session = await _state(hub, uid)
        why = "gitfile" if case == "gitfile" else "link or a junction"
        assert session["state"] == "failed" and why in session["error"], session
        _no_path(session, tmp_path)
        assert hub.manager.meta(session["name"])["api"]["start_error"]["code"] == "folder_not_ready"
    finally:
        await hub.close()


@pytest.mark.parametrize("change", ["removed", "copies_only", "swapped_inside", "swapped_outside", "root_moved"])
async def test_every_start_checks_the_root_and_the_folder_again(tmp_path, api_token, change):
    hub, _jobs, places = _hub(tmp_path)
    outside = _outside(tmp_path)
    try:
        folder = {"root": "places", "path": "acme/site", "mode": "in_place"}
        status, answer = await hub.create({"goal": "G", "folder": folder})
        assert status == 202, answer
        uid, sid = answer["session"]["id"], answer["session"]["name"]
        await hub.settled()
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        raw = st.read_raw(hub.manager.home)
        if change == "removed":
            del raw["api"]["folders"]["places"]
        elif change == "copies_only":
            raw["api"]["folders"]["places"]["in_place"] = False
        elif change == "root_moved":   # the owner points the root elsewhere: the folder is not below it any more
            (tmp_path / "srv" / "other").mkdir()
            raw["api"]["folders"]["places"]["path"] = str(tmp_path / "srv" / "other")
        st.write_raw(hub.manager.home, raw)
        if change == "swapped_inside":   # a link that still leads inside the root, to another folder there
            (places / "acme").rename(places / "acme.old")
            _dir_link(places / "acme.old", places / "acme")
        elif change == "swapped_outside":
            _swap_for_link(places / "acme", outside)
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/continue", body={"text": "Go on"})
        assert status == 200, answer
        await hub.settled()
        session = await _state(hub, uid)
        assert session["state"] == "failed" and session["final"] is True, session
        assert hub.manager.meta(sid)["api"]["start_error"]["code"] == "forbidden_folder"
        assert "places/acme/site" in session["error"] or "'places'" in session["error"], session
        _no_path(session, tmp_path)
        assert sid not in hub.manager.running and not (outside / "site" / ".scratch").exists()
    finally:
        await hub.close()


async def test_a_missing_root_at_a_start_is_not_ready(tmp_path, api_token):
    hub, _jobs, places = _hub(tmp_path)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "a", "mode": "in_place"}})
        uid, sid = answer["session"]["id"], answer["session"]["name"]
        await hub.settled()
        await hub.call("POST", f"/api/v1/sessions/{uid}/stop")
        raw = st.read_raw(hub.manager.home)
        raw["api"]["folders"]["places"]["path"] = str(tmp_path / "unplugged")
        st.write_raw(hub.manager.home, raw)
        await hub.call("POST", f"/api/v1/sessions/{uid}/continue", body={"text": "Go on"})
        await hub.settled()
        session = await _state(hub, uid)
        assert session["state"] == "failed" and hub.manager.meta(sid)["api"]["start_error"]["code"] == \
            "folder_not_ready", session
    finally:
        await hub.close()


async def test_a_start_checks_the_folder_off_the_event_loop(tmp_path, api_token, monkeypatch):
    """A root on a share that does not answer never stalls the hub: the start's folder check runs in a thread."""
    hub, _jobs, _places = _hub(tmp_path)
    threads: list[int] = []
    real_check = SessionManager._check_server_folder

    def check(self, sid: str, meta: dict[str, Any]) -> None:
        threads.append(threading.get_ident())
        real_check(self, sid, meta)

    monkeypatch.setattr(SessionManager, "_check_server_folder", check)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "a", "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        assert (await _state(hub, answer["session"]["id"]))["state"] == "running"
        assert threads and threading.get_ident() not in threads
    finally:
        await hub.close()


async def test_a_workspace_move_by_the_owner_drops_the_server_folder(tmp_path, api_token):
    hub, _jobs, _places = _hub(tmp_path)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "a", "mode": "in_place"}})
        uid, sid = answer["session"]["id"], answer["session"]["name"]
        await hub.settled()
        await hub.manager.set_workspace(sid, str(tmp_path / "mine"))
        assert hub.manager.meta(sid)["api"]["folder"] is None and (await _state(hub, uid))["folder"] is None
        raw = st.read_raw(hub.manager.home)
        del raw["api"]["folders"]["places"]   # its checks no longer apply
        st.write_raw(hub.manager.home, raw)
        await hub.manager.stop(sid)
        await hub.manager.start(sid)
        assert sid in hub.manager.running
    finally:
        await hub.close()


# ---- one folder, one session ----------------------------------------------------------------------------------------
async def test_a_folder_another_session_works_in_is_busy(tmp_path, api_token, hub_token, no_start):
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    try:
        status, first = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme",
                                                                  "mode": "in_place"}})
        assert status == 202, first
        uid = first["session"]["id"]
        for path in ("acme", "acme/sub", ""):   # the folder, one inside it, one containing it
            status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": path,
                                                                       "mode": "in_place"}})
            assert status == 409 and answer["code"] == FOLDER_BUSY and answer.get("session") == uid, (path, answer)
            _no_path(answer, tmp_path)
        board = (await hub.manager.create("Owner's", "Mine", start=False, workspace=str(places / "board")))["id"]
        body = {"goal": "G", "folder": {"root": "places", "path": "board", "mode": "in_place"}}
        status, answer = await hub.create(body)
        assert status == 409 and answer["code"] == FOLDER_BUSY and "session" not in answer   # not the API's
        assert "Owner" not in answer["error"] and board not in answer["error"]
        status, answer = await hub.create(body, token=TOKEN)
        assert status == 409 and answer["session"] == hub.manager.summary(board)["uid"]   # the owner sees it
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme"}})
        assert status == 202, answer   # a copy is never busy
    finally:
        await hub.close()


async def test_another_spelling_of_a_folder_is_the_same_folder_where_the_file_system_says_so(tmp_path, api_token,
                                                                                            no_start):
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", nfc)
    try:
        for made, other in (("acme", "ACME"), (nfc, nfd)):
            body = {"goal": "G", "folder": {"root": "places", "path": made, "mode": "in_place"}}
            assert (await hub.create(body))[0] == 202
            same = (places / other).exists()   # (the file system folds letter case, or Unicode form)
            status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": other,
                                                                       "mode": "in_place"}})
            assert (status, answer.get("code")) == ((409, FOLDER_BUSY) if same else (202, None)), (other, answer)
    finally:
        await hub.close()


def folds_case(folder: Path) -> bool:
    """The file system of `folder` ignores letter case (NTFS, APFS as macOS formats it)."""
    probe = folder / "Case-Probe"
    probe.mkdir(parents=True)
    try:
        return (folder / "CASE-PROBE").exists()
    finally:
        probe.rmdir()


def resolve_keeps_spelling(monkeypatch) -> None:
    """Path.resolve as POSIX resolves on a file system that ignores letter case (APFS, a Windows drive seen from WSL
    or a Docker Desktop bind mount): a path naming an entry in another letter case keeps the spelling it was given,
    where Windows gives the folders' own."""
    resolve = Path.resolve

    def keeps(self: Path, strict: bool = False) -> Path:
        found = resolve(self, strict=strict)
        given = Path(os.path.abspath(self))
        return given if str(given).casefold() == str(found).casefold() else found

    monkeypatch.setattr(Path, "resolve", keeps)


def test_each_name_below_a_root_is_taken_as_its_folder_lists_it(tmp_path, monkeypatch):
    """apifolders.as_listed: a name the file system opens in another letter case or Unicode form is replaced by the
    listed name of the same entry; from a name that is not there on, the names stay as given."""
    _write(tmp_path / "Docs" / "api" / "Read.md")
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", nfc)
    (tmp_path / "Docs" / nfc).mkdir()
    folds = folds_case(tmp_path)
    assert apifolders.as_listed(tmp_path, ()) == ()
    assert apifolders.as_listed(tmp_path, ("Docs", "api", "Read.md")) == ("Docs", "api", "Read.md")
    assert apifolders.as_listed(tmp_path, ("Docs", "new", "api")) == ("Docs", "new", "api")
    other = ("DOCS", "API", "read.MD")
    assert apifolders.as_listed(tmp_path, other) == (("Docs", "api", "Read.md") if folds else other)
    form = (tmp_path / "Docs" / nfd).exists()   # (the file system folds Unicode form: APFS)
    assert apifolders.as_listed(tmp_path, ("Docs", nfd)) == ("Docs", nfc if form else nfd)
    if folds:   # a file system without file ids: the one listed name that differs in letter case only
        lstat = os.lstat

        def without_ids(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            found = lstat(path, *args, **kwargs)
            return os.stat_result((found.st_mode, 0, *tuple(found)[2:10]))

        monkeypatch.setattr(os, "lstat", without_ids)
        assert apifolders.as_listed(tmp_path, ("dOCS", "Api")) == ("Docs", "api")


async def test_the_other_sessions_folders_are_compared_by_file_identity_too(tmp_path, monkeypatch):
    """A folder that another session's stored path names by another spelling (here: through a link, which the
    file system resolves to the same folder) is that session's folder: _plan compares by identity, not by text
    alone."""
    manager = _manager(_home(tmp_path))
    target = tmp_path / "work" / "acme"
    target.mkdir(parents=True)
    alias = tmp_path / "alias"
    _dir_link(target, alias)
    monkeypatch.setattr(manager, "_other_workspaces", lambda sid: [("001-other", alias)])
    for path in (target, target / "sub"):
        with pytest.raises(SessionError) as err:
            manager._plan(None, str(path), git_on=False)
        assert err.value.code == FOLDER_BUSY and err.value.holder == "001-other", path


async def test_another_home_is_never_a_folder_but_a_sessions_own_folder_always_is(tmp_path):
    """_target_refusal refuses another DreamTeam home (and a folder inside one); a session's own folder is its own,
    even when its agents built a home's layout there (/cd --default always leads back)."""
    manager = _manager(_home(tmp_path))
    try:
        sid = (await manager.create("Own", "G", start=False))["id"]
        default = manager.default_workspace(sid)
        _write(default / "dreamteam.toml", "")
        (default / "sessions").mkdir()
        assert manager._target_refusal(default.resolve(), sid) is None
        _write(tmp_path / "other" / "dreamteam.toml", "")
        (tmp_path / "other" / "sessions").mkdir()
        (tmp_path / "other" / "drop").mkdir()
        for folder in (tmp_path / "other", tmp_path / "other" / "drop"):
            assert "is a DreamTeam home" in (manager._target_refusal(folder.resolve(), sid) or ""), folder
    finally:
        await manager.shutdown()


async def test_max_running_counts_the_sessions_of_server_folders(tmp_path, api_token, no_start):
    hub, _jobs, _places = _hub(tmp_path, api={"max_running": 1})
    hold = asyncio.Event()
    no_start(hub.manager, hold)
    try:
        status, _answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "a",
                                                                    "mode": "in_place"}})
        assert status == 202
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "b",
                                                                   "mode": "in_place"}})
        assert status == 429 and answer["code"] == "too_many_sessions" and len(hub.manager.ids()) == 1
    finally:
        hold.set()
        await hub.close()


# ---- the Docker sandbox ---------------------------------------------------------------------------------------------
def _sandboxed_home(tmp_path: Path, monkeypatch) -> tuple[Path, dict[str, Path]]:
    """A home whose [api] folders list roots of every kind for `dreamteam sandbox`: `jobs` (copies), `places` (in
    place) and `linked` (a link to the folder `real`), which it mounts, and `gone` (missing), `mine` (the user's home
    folder), `inner` (inside the DreamTeam home), `foreign` (absolute on the other OS only), `keep` (holds the store
    of hub tokens, where run_sandbox keeps the container's token) and `shelf` (holds the host's skills library, which
    the sandbox mounts read-only), which it must not."""
    user = tmp_path / "people" / "me"
    user.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: user)
    srv = tmp_path / "srv"
    folders = {name: srv / name for name in ("jobs", "places", "real", "keep", "shelf")}
    for folder in folders.values():
        folder.mkdir(parents=True)
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(folders["keep"] / "hub-tokens"))
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(folders["shelf"] / "library"))
    _dir_link(folders["real"], srv / "linked")
    roots = {"jobs": {"path": str(folders["jobs"])}, "places": {"path": str(folders["places"]), "in_place": True},
             "linked": {"path": str(srv / "linked")}, "gone": {"path": str(tmp_path / "gone")},
             "mine": {"path": str(user), "in_place": True}, "inner": {"path": str(tmp_path / "home" / "drop")},
             "foreign": {"path": "/srv/jobs" if WINDOWS else "C:\\jobs"},
             "keep": {"path": str(folders["keep"]), "in_place": True},
             "shelf": {"path": str(folders["shelf"]), "in_place": True}}
    home = _home(tmp_path, api={"folders": roots})
    (home / "drop").mkdir()
    (home / "sessions").mkdir()
    return home, {**folders, "user": user}


def _root_volumes(cmd: list[str]) -> list[str]:
    """The `-v` arguments of a sandbox command that mount a root of [api] folders."""
    image = cmd.index(docker_module.IMAGE)
    return [cmd[i + 1] for i in range(image) if cmd[i] == "-v" and ":/dreamteam/api-folders/" in cmd[i + 1]]


def test_the_sandbox_mounts_the_roots_it_checked_on_the_host(tmp_path, monkeypatch):
    """docs/SPEC.md §21.10 "Sandbox": each root is checked on the host as the hub outside the container checks it (the
    hub inside sees only the mount point), then mounted as resolved at /dreamteam/api-folders/<name>, read-only unless
    in_place; the others are named in warnings. The variable for a home's hub only, never the host's own."""
    home, folders = _sandboxed_home(tmp_path, monkeypatch)
    config = Config.load(home)
    mounts, notes = docker_module.api_folder_mounts(home, config)
    assert [(m.name, m.source, m.read_only) for m in mounts] == [
        ("jobs", folders["jobs"].resolve(), True), ("linked", folders["real"].resolve(), True),
        ("places", folders["places"].resolve(), False)]
    warned = {re.match(r"Warning: \[api\.folders\.([a-z]+)\] ", note).group(1): note for note in notes}
    assert set(warned) == {"foreign", "gone", "inner", "mine", "keep", "shelf"}
    assert "is not there" in warned["gone"] and "user's home folder" in warned["mine"]
    assert "DreamTeam home" in warned["inner"] and "not an absolute path" in warned["foreign"]
    # (writable in the container, the token store would hand every agent the hub's token; the skills library, which
    # the sandbox mounts read-only, would be writable through the root)
    assert "DreamTeam's own folders" in warned["keep"] and "DreamTeam's own folders" in warned["shelf"]
    assert all("is not mounted into the container" in note for note in notes)

    cmd = docker_module.sandbox_command(home, 9320, config, docker="docker")
    assert _root_volumes(cmd) == [f"{folders['jobs'].resolve()}:/dreamteam/api-folders/jobs:ro",
                                  f"{folders['real'].resolve()}:/dreamteam/api-folders/linked:ro",
                                  f"{folders['places'].resolve()}:/dreamteam/api-folders/places"]
    image = cmd.index(docker_module.IMAGE)
    at = cmd.index(f"{API_FOLDERS_ENV}=/dreamteam/api-folders")
    assert cmd[at - 1] == "-e" and at < image
    # (the skills library inside `shelf` is mounted, read-only, as the host's library only)
    assert not any(str(folders["user"]) in part or str(tmp_path / "gone") in part for part in cmd)
    assert not any(f"{folders['keep']}" in part for part in cmd)
    # the mounts run_sandbox computed, as given (none: the variable all the same, so a root added later is looked for
    # there); a single-swarm project's `run` serves no external API: neither
    assert _root_volumes(docker_module.sandbox_command(home, 9320, config, docker="docker",
                                                       folders=mounts[:1])) == _root_volumes(cmd)[:1]
    bare = docker_module.sandbox_command(home, 9320, config, docker="docker", folders=[])
    assert _root_volumes(bare) == [] and f"{API_FOLDERS_ENV}=/dreamteam/api-folders" in bare
    project = docker_module.sandbox_command(home, 9320, config, docker="docker", home=False)
    assert _root_volumes(project) == [] and not any(API_FOLDERS_ENV in part for part in project)
    # never the host's own value, not even through [shell] pass_env (a `-e NAME` would come later and win)
    monkeypatch.setenv(API_FOLDERS_ENV, str(tmp_path / "elsewhere"))
    passing = Config.from_dict(home, {"shell": {"pass_env": [API_FOLDERS_ENV]}})
    assert API_FOLDERS_ENV not in docker_module.env_passthrough(passing)
    assert [part for part in docker_module.sandbox_command(home, 9320, passing, docker="docker")
            if API_FOLDERS_ENV in part] == [f"{API_FOLDERS_ENV}=/dreamteam/api-folders"]


def test_a_root_is_mounted_by_a_path_dockers_volume_option_reads(tmp_path, monkeypatch):
    """`-v SOURCE:TARGET[:ro]` is split at ':': a Windows host's drive letter is kept as it is, any other ':' would cut
    the source, and a Windows path on no drive letter (a network share's, which a root on a mapped network drive
    resolves to, or a \\\\?\\ one) is no host folder for docker: such a root is not mounted (a warning says why), so one
    root cannot make the whole `docker run` fail."""
    windows = docker_module.FolderMount("jobs", PureWindowsPath("C:/srv/jobs"), read_only=True)
    assert windows.volume == "C:\\srv\\jobs:/dreamteam/api-folders/jobs:ro"
    posix = docker_module.FolderMount("places", PurePosixPath("/srv/places"), read_only=False)
    assert posix.volume == "/srv/places:/dreamteam/api-folders/places"
    assert posix.target == "/dreamteam/api-folders/places"
    refusal = docker_module.volume_source_refusal
    assert refusal(PureWindowsPath("C:/srv/jobs")) is None and refusal(PureWindowsPath("d:\\x")) is None
    assert refusal(PurePosixPath("/srv/jobs")) is None
    assert "':'" in refusal(PurePosixPath("/srv/a:b")) and "':'" in refusal(PureWindowsPath("C:/srv/a:b"))
    for share in ("\\\\server\\share\\jobs", "\\\\?\\C:\\very\\long", "\\\\?\\UNC\\server\\share\\x",
                  "\\\\.\\C:\\jobs"):
        assert "drive letter" in refusal(PureWindowsPath(share)), share
    if WINDOWS:   # a root on a share (as the resolution of a mapped network drive gives it): a warning, no mount
        home = _home(tmp_path, api={"folders": {"share": {"path": "\\\\server\\share\\jobs"}}})
        monkeypatch.setattr(apifolders, "resolve_root", lambda folder, **_: Path(folder.path))
        mounts, notes = docker_module.api_folder_mounts(home, Config.load(home))
        assert mounts == [] and len(notes) == 1 and "drive letter" in notes[0] and "[api.folders.share]" in notes[0]
    else:   # (NTFS takes no ':' in a name)
        odd = tmp_path / "srv" / "a:b"
        odd.mkdir(parents=True)
        home = _home(tmp_path, api={"folders": {"odd": {"path": str(odd)}}})
        mounts, notes = docker_module.api_folder_mounts(home, Config.load(home))
        assert mounts == [] and len(notes) == 1 and "':'" in notes[0] and "[api.folders.odd]" in notes[0]


def test_a_read_only_root_inside_a_writable_one_is_named_in_a_warning(tmp_path):
    """A copy root inside an in_place one is mounted read-only at its own mount point, but the agents write it through
    the other's: a warning says so (docs/SPEC.md §21.10 "Sandbox")."""
    places = tmp_path / "srv" / "places"
    (places / "drafts").mkdir(parents=True)
    home = _home(tmp_path, api={"folders": {"places": {"path": str(places), "in_place": True},
                                            "drafts": {"path": str(places / "drafts")}}})
    mounts, notes = docker_module.api_folder_mounts(home, Config.load(home))
    assert [(m.name, m.read_only) for m in mounts] == [("drafts", True), ("places", False)]
    assert notes == ["Warning: [api.folders.drafts] is mounted read-only, but it lies inside [api.folders.places], "
                     "which is mounted writable: agents in the container can write it there."]


def test_the_sandbox_warning_leaves_out_the_sessions_of_a_mounted_root(tmp_path, monkeypatch, capsys):
    """run_sandbox checks the roots once, for its warnings and for the command; outside_home_warning leaves out a
    session made in an earlier container in a folder of a root it mounts writable (stored under the mount point), and
    lists one made by a hub outside the container (the host's path), one of a root not mounted this time and one of a
    root mounted read-only now (an in-place session there cannot start: its root is no longer in_place). The token is
    never stored inside a mounted root, even were the check of the roots to let one around the token store through."""
    home, folders = _sandboxed_home(tmp_path, monkeypatch)
    stored = {"made-there": "/dreamteam/api-folders/places/site", "root-itself": "/dreamteam/api-folders/places",
              "not-mounted": "/dreamteam/api-folders/gone/site", "made-here": str(folders["places"] / "site"),
              "climbing": "/dreamteam/api-folders/places/../../swarm", "copy-only": "/dreamteam/api-folders/jobs/x"}
    for sid, folder in stored.items():
        _write(home / "sessions" / sid / "session.json", json.dumps({"workspace": {"path": folder,
                                                                                 "adopted": False}}))
    assert [docker_module.api_root_of(folder) for folder in stored.values()] == ["places", "places", "gone", None,
                                                                                 None, "jobs"]
    assert docker_module.api_root_of("/dreamteam/api-folders") is None
    assert docker_module.api_root_of("/dreamteam/api-folders-old/places") is None
    calls: list[list[str]] = []
    computed: list[int] = []
    real_mounts = docker_module.api_folder_mounts
    monkeypatch.setattr(docker_module, "api_folder_mounts", lambda *a: computed.append(1) or real_mounts(*a))
    monkeypatch.setattr(docker_module, "_docker", lambda: "docker")
    monkeypatch.setattr(docker_module, "image_exists", lambda image=docker_module.IMAGE: True)
    same = docker_module.dockerfile_hash()
    monkeypatch.setattr(docker_module, "image_dockerfile", lambda image=docker_module.IMAGE: same)
    monkeypatch.setattr(docker_module, "container_exists", lambda name: False)
    monkeypatch.setattr(docker_module.subprocess, "call", lambda cmd, env=None: calls.append(cmd) or 0)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    assert docker_module.run_sandbox(home, 9321) == 0
    assert computed == [1] and len(_root_volumes(calls[0])) == 3
    err = capsys.readouterr().err
    listed = {line.split(":")[0].strip() for line in err.splitlines() if re.match(r"  [a-z-]+: ", line)}
    assert listed == {"not-mounted", "made-here", "climbing", "copy-only"}
    assert "and the in_place roots of [api] folders are mounted writable into the container" in err
    assert ("[api] folders mounted into the container: jobs (read-only), linked (read-only), places (writable). "
            "Every agent of every session there can read them, and write the writable ones.") in err
    refused = ("foreign", "gone", "inner", "mine", "keep", "shelf")
    assert {name for name in refused if f"Warning: [api.folders.{name}]" in err} == set(refused)
    assert "is inside a mounted folder, so the token is not stored" not in err
    # a single-swarm project: no roots looked at, nothing said about them
    calls.clear()
    computed.clear()
    assert docker_module.run_sandbox(home, 9321, home=False) == 0
    err = capsys.readouterr().err
    assert computed == [] and _root_volumes(calls[0]) == [] and "[api" not in err
    # no mounted root: the old warning, word for word
    lines = docker_module.outside_home_warning(home)
    assert lines[0] == (f"Warning: only {home} is mounted into the container; these sessions work in a folder "
                        "outside it and cannot start there:") and len(lines) == 2 + len(stored)
    # were root_refusal to let `keep` through, it would be mounted, but the token is still not stored in it
    monkeypatch.setattr(apifolders, "root_refusal", lambda *a, **k: None)
    calls.clear()
    assert docker_module.run_sandbox(home, 9321) == 0
    assert any(volume.endswith("/dreamteam/api-folders/keep") for volume in _root_volumes(calls[0]))
    err = capsys.readouterr().err
    assert "is inside a mounted folder, so the token is not stored" in err
    assert not (folders["keep"] / "hub-tokens" / "9321.json").exists()


def test_the_roots_are_where_the_sandbox_mounts_them_in_its_container_only(tmp_path, monkeypatch):
    """apifolders.roots: in the image's container (copyfrom.hub_in_container) DREAMTEAM_API_FOLDERS_DIR gives each
    root's path, `<it>/<name>`, whatever the settings say (a Windows home's path too), `in_place` kept; elsewhere, or
    with a value that is not absolute there, the settings' paths. The variable never reaches the agents."""
    home = _home(tmp_path, api={"folders": {"jobs": {"path": "C:\\host\\jobs"},
                                            "places": {"path": "/host/places", "in_place": True}}})
    config = Config.load(home)
    settings = dict(config.api.folders)
    mounted = tmp_path / "api-folders"
    inside = {"jobs": ApiFolder("jobs", str(mounted / "jobs")),
              "places": ApiFolder("places", str(mounted / "places"), in_place=True)}
    assert apifolders.roots(config) == settings
    monkeypatch.setenv(API_FOLDERS_ENV, str(mounted))
    assert apifolders.roots(config) == settings   # a stray variable on a hub outside the image changes nothing
    monkeypatch.setenv(netoff.IMAGE_ENV, "1")
    assert apifolders.roots(config) == inside and config.api.folders == settings
    for value in ("", "api-folders", "/x" if WINDOWS else "C:\\x"):   # nothing absolute here
        monkeypatch.setenv(API_FOLDERS_ENV, value)
        assert apifolders.roots(config) == settings, value
    monkeypatch.delenv(netoff.IMAGE_ENV)
    monkeypatch.setenv(API_FOLDERS_ENV, str(mounted))
    monkeypatch.setenv("DREAMTEAM_HOST_SKILLS_DIR", "/dreamteam/host-skills")   # `dreamteam sandbox`'s own marker
    assert apifolders.roots(config) == inside
    (tmp_path / "bare").mkdir()
    assert apifolders.roots(Config.load(_home(tmp_path / "bare"))) == {}
    # never in the agents' environment nor in DreamTeam's own git's, whatever [shell] pass_env says
    passing = Config.from_dict(home, {"shell": {"pass_env": [API_FOLDERS_ENV]}})
    assert API_FOLDERS_ENV not in shell.scrubbed_env(passing) and API_FOLDERS_ENV not in shell.secret_free_env()


async def test_in_the_sandbox_v1_finds_each_root_where_it_is_mounted(tmp_path, api_token, monkeypatch):
    """The hub in the sandbox's container: the settings name the host's folders (one a Windows path); a copy and an
    in-place session reach each root at `<DREAMTEAM_API_FOLDERS_DIR>/<name>`, the start checks the folder there
    again, a root not mounted is not there (404, though its host folder is), and no answer names a path. Outside the
    image the same variable changes nothing."""
    mounted = tmp_path / "api-folders"
    _site(mounted / "jobs")
    (mounted / "places").mkdir()
    gone = tmp_path / "host" / "gone"   # there on the host, but not mounted (no api-folders/gone)
    _site(gone)
    hub, _jobs, _places = _hub(tmp_path, folders={
        "jobs": {"path": "/host/jobs" if WINDOWS else "C:\\host\\jobs"},
        "places": {"path": str(tmp_path / "host" / "places"), "in_place": True},
        "gone": {"path": str(gone), "in_place": True}})
    monkeypatch.setenv(API_FOLDERS_ENV, str(mounted))
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "jobs", "path": "acme/site"}})
        assert (status, answer["code"]) == (403, "forbidden_folder"), answer   # the settings' path, not absolute here
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "new",
                                                                   "mode": "in_place"}})
        assert (status, answer["code"]) == (404, "not_found"), answer   # the settings' path, not there
        monkeypatch.setenv(netoff.IMAGE_ENV, "1")   # the image's container
        status, answer = await hub.create({"goal": "Fix the site", "folder": {"root": "jobs", "path": "acme/site"}})
        assert status == 202, answer
        _no_path(answer, tmp_path)
        meta = hub.manager.meta(answer["session"]["name"])
        assert meta["copied_from"]["source"] == "jobs/acme/site" and meta["copied_from"]["files"] == 3
        workspace = hub.manager.default_workspace(answer["session"]["name"])
        assert (workspace / "index.html").read_bytes() == b"<h1>Acme</h1>" and not (workspace / ".git").exists()
        folder = {"root": "places", "path": "new/site", "mode": "in_place"}
        status, answer = await hub.create({"goal": "Build a site", "folder": folder})
        assert status == 202, answer
        _no_path(answer, tmp_path)
        session = answer["session"]
        await hub.settled()
        state = await _state(hub, session["id"])
        assert state["state"] == "running" and state["folder"] == folder   # its start checked the folder there
        target = mounted / "places" / "new" / "site"
        assert target.is_dir() and Path(hub.manager.meta(session["name"])["workspace"]["path"]) == target.resolve()
        # a root the sandbox did not mount is not there in the container, whatever its host folder holds
        for mode in ("copy", "in_place"):
            status, answer = await hub.create({"goal": "G", "folder": {"root": "gone", "path": "acme/site",
                                                                       "mode": mode}})
            assert (status, answer["code"]) == (404, "not_found"), answer
            _no_path(answer, tmp_path)
        status, options = await hub.call("GET", "/api/v1/options")
        assert status == 200 and options["folders"] == [{"name": "gone", "in_place": True},
                                                        {"name": "jobs", "in_place": False},
                                                        {"name": "places", "in_place": True}]
        _no_path(options, tmp_path)
    finally:
        await hub.close()


# ---- copyfrom: the walk below the root -----------------------------------------------------------------------------
def _source(tmp_path: Path) -> Source:
    root = tmp_path / "root"
    _write(root / "acme" / "a.txt", "A")
    _write(root / "acme" / "b.txt", "B")
    _write(root / "acme" / "sub" / "c.txt", "C")
    return Source("folder", root / "acme", "jobs/acme", root=root.resolve(), parts=("acme",))


def _sessions(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return sessions


def _before_child(monkeypatch, name: str, action) -> None:
    """Run `action` once, just before the copy's walk opens `name` (after the listing that gave it)."""
    real = nofollow._FS.child
    done: list[int] = []

    def child(parent, entry_name, rel, **kw):
        if entry_name == name and kw.get("data", True) and not done:
            done.append(1)
            action()
        return real(parent, entry_name, rel, **kw)

    monkeypatch.setattr(nofollow._FS, "child", child)


def test_a_file_gone_while_a_server_folder_is_copied_is_reported(tmp_path, monkeypatch):
    source = _source(tmp_path)
    sessions = _sessions(tmp_path)
    calls: list[int] = []
    real_entries = copyfrom._server_entries

    def entries(src, *, open_files):   # the second walk (the copy) loses b.txt after its listing
        calls.append(1)
        if open_files:
            _before_child(monkeypatch, "b.txt", lambda: (src.root / "acme" / "b.txt").unlink())
        return real_entries(src, open_files=open_files)

    monkeypatch.setattr(copyfrom, "_server_entries", entries)
    result = copy_sources([source], sessions / "stage", LIMITS)
    assert result["files"] == 2 and result["vanished"] == ["b.txt"] and calls == [1, 1]
    assert _files(sessions / "stage") == {"a.txt": b"A", "sub/c.txt": b"C"}


def test_a_folder_swapped_for_a_link_while_a_server_folder_is_copied_ends_the_copy(tmp_path, monkeypatch):
    source = _source(tmp_path)
    sessions = _sessions(tmp_path)
    outside = _outside(tmp_path)
    _before_child(monkeypatch, "sub", lambda: _swap_for_link(source.root / "acme" / "sub", outside))
    with pytest.raises(CopyError) as err:
        copy_sources([source], sessions / "stage", LIMITS)
    assert err.value.status == 403 and err.value.code == "forbidden_folder" and "jobs/acme/sub" in str(err.value)
    assert os.listdir(sessions) == [] and str(tmp_path) not in str(err.value)


def test_a_folder_without_a_root_or_a_root_gone_is_never_copied(tmp_path):
    sessions = _sessions(tmp_path)
    source = _source(tmp_path)
    with pytest.raises(ValueError):   # (copy_tree never copies a server folder)
        copy_sources([Source("folder", source.path, "x")], sessions / "stage", LIMITS)
    rmtree(source.path)
    with pytest.raises(CopyError) as err:
        copy_sources([source], sessions / "stage", LIMITS)
    assert err.value.status == 404 and err.value.code == "not_found" and os.listdir(sessions) == []


def test_resolve_source_checks_its_own_resolution_against_the_root(tmp_path):
    """resolve_source(within=): the path it resolves itself must still lie inside the root (a link swapped in after
    the request was resolved), 403 `forbidden_folder`; a folder inside it passes its other rules as before."""
    manager = _manager(_home(tmp_path))
    root = (tmp_path / "srv" / "jobs")
    _write(root / "acme" / "a.txt")
    outside = _outside(tmp_path)
    _dir_link(outside, root / "out")
    kw = {"home": manager.home, "config": manager.config, "sandbox": False, "within": root.resolve()}
    assert copyfrom.resolve_source(str(root / "acme"), **kw).kind == "folder"
    with pytest.raises(CopyError) as err:
        copyfrom.resolve_source(str(root / "out"), **kw)
    assert err.value.status == 403 and err.value.code == "forbidden_folder"
    assert str(root / "out") in str(err.value) and str(outside) not in str(err.value)   # (named as given)
