"""The external HTTP API v1's files (docs/SPEC.md §21.8): a folder of a session's folder listed, a file downloaded and a
folder archived, every path read below the session's folder without following a link (nofollow), with one rule for
the three (api_v1.served, copyfrom.check_rel): never `.git` at any depth, never `.scratch/_skills`, `.scratch` only
with `scratch=true`, no link, no file with another name; path escapes refused as `bad_name`; an archive measured first
(413 before any byte) and cut, never finished, when its files change under it; a session that works in a server folder
in place read below its root. Every request goes through the launcher's app and its guard on the test's own loop (an
ASGI call read as it comes, as the server would make it): no port, ScriptedLLM only. Links are junctions on Windows (no
privilege needed) and symlinks elsewhere."""

from __future__ import annotations

import asyncio
import io
import json
import os
import errno
import shutil
import stat
import sys
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dreamteam import api_v1, nofollow
from dreamteam import settings as st
from dreamteam.api_v1 import ApiV1, attachment, served
from dreamteam.copyfrom import CopyError, check_rel
from dreamteam.sessions import SessionManager
from test_api_v1 import LOCAL, Hub, _home, _manager, _no_path, no_start  # noqa: F401 - a fixture
from test_api_v1_events import _manager as waiting_manager
from test_api_v1_events import running
from test_api_v1_folders import _dir_link, _hub, _outside, _swap_for_link
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures
from test_hub_auth import API_TOKEN, TOKEN

WINDOWS = sys.platform == "win32"
SECRET = b"SECRET outside the root"
TREE = {
    "README.md": "# Site",
    "src/app.js": "run()",
    "src/lib/util.js": "u()",
    "src/.git/HEAD": "ref: refs/heads/main",     # a nested repository: never listed, served or archived
    ".git/config": "[core]",                      # the folder's own repository
    "docs/.git": "gitdir: /elsewhere",            # a gitfile
    ".scratch/anna/notes.md": "notes",
    ".scratch/_skills/pdf/SKILL.md": "skill",     # the skills mirror: never, even with scratch=true
}
FILES = {"README.md": b"# Site", "src/app.js": b"run()", "src/lib/util.js": b"u()"}
FOLDERS = {"docs/", "empty/", "src/", "src/lib/"}


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


async def fetch(hub: Hub, path: str, query: dict[str, str] | str = "", *, token: str = API_TOKEN,
                on_body: Any = None, leave_after: int | None = None) -> SimpleNamespace:
    """GET `path` through the hub's app as the server would call it (ASGI 2.3: Starlette listens for the
    disconnect), read as it comes: status, headers, body, `ended` (the last body message came), `error` (what the
    app raised: an answer cut after it started). `on_body(n)` runs at the n-th body message; `leave_after`: the
    client goes away after that many."""
    if isinstance(query, dict):
        query = urllib.parse.urlencode(query)
    sent = [(b"host", b"127.0.0.1:8765"), (b"authorization", f"Bearer {token}".encode())]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET", "path": path,
             "raw_path": urllib.parse.quote(path).encode(), "headers": sent, "query_string": query.encode(),
             "scheme": "http", "root_path": "", "server": ("172.17.0.2", 8765), "client": LOCAL, "http_version": "1.1"}
    got = SimpleNamespace(status=None, headers={}, body=b"", ended=False, error=None, messages=0)
    gone = asyncio.Event()
    asked = [False]

    async def receive() -> dict[str, Any]:
        if not asked[0]:
            asked[0] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            got.status = message["status"]
            got.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            got.body += message.get("body", b"")
            got.messages += 1
            if on_body is not None:
                on_body(got.messages)
            if not message.get("more_body", False):
                got.ended = True
                gone.set()
            elif leave_after is not None and got.messages >= leave_after:
                gone.set()

    try:
        await hub.app(scope, receive, send)
    except Exception as exc:  # noqa: BLE001 - an answer cut after it started
        got.error = exc
    finally:
        gone.set()
    return got


def _json(got: SimpleNamespace) -> Any:
    return json.loads(got.body.decode("utf-8"))


async def listing(hub: Hub, uid: str, **query: str) -> SimpleNamespace:
    return await fetch(hub, f"/api/v1/sessions/{uid}/files", query)


async def download(hub: Hub, uid: str, rel: str, query: dict[str, str] | str = "", **kwargs: Any) -> SimpleNamespace:
    return await fetch(hub, f"/api/v1/sessions/{uid}/files/{rel}", query, **kwargs)


async def archive(hub: Hub, uid: str, query: dict[str, str] | str = "", **kwargs: Any) -> SimpleNamespace:
    return await fetch(hub, f"/api/v1/sessions/{uid}/archive", query, **kwargs)


def _names(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["name"] for entry in entries]


def _zip(got: SimpleNamespace) -> dict[str, bytes | None]:
    """The archive's entries: name -> bytes (None for a folder)."""
    with zipfile.ZipFile(io.BytesIO(got.body)) as zf:
        assert zf.testzip() is None
        return {info.filename: None if info.is_dir() else zf.read(info) for info in zf.infolist()}


async def session_with(hub: Hub, files: dict[str, bytes | str], **body: Any) -> tuple[str, str, Path]:
    """A v1 session that never starts (the `no_start` fixture installed), with these files written into its folder
    (a name ending in '/' is an empty folder): (its id, its folder name, its folder)."""
    status, answer = await hub.create({"goal": "Write a haiku", **body})
    assert status == 202, answer
    await hub.settled()
    sid, uid = answer["session"]["name"], answer["session"]["id"]
    folder = Path(hub.manager.workspace_of(sid)["path"])
    for rel, data in files.items():
        if rel.endswith("/"):
            (folder / rel).mkdir(parents=True, exist_ok=True)
        else:
            _write(folder / rel, data)
    return uid, sid, folder


async def tree_session(hub: Hub) -> tuple[str, str, Path]:
    return await session_with(hub, {**TREE, "empty/": ""})


# ---------------------------------------------------------------------------------------------- the one rule
@pytest.mark.parametrize("parts, scratch, taken", [
    (("README.md",), False, True), (("src", "lib"), False, True), (("sub", ".scratch", "x"), False, True),
    ((".git",), False, False), (("src", ".GIT", "HEAD"), False, False), (("docs", ".Git"), False, False),
    ((".scratch",), False, False), ((".scratch", "anna"), False, False), ((".scratch", "anna"), True, True),
    ((".scratch", "_skills"), True, False), ((".SCRATCH", "_Skills", "x"), True, False),
    (("GIT~1", "config"), False, False), (("SCRATC~1",), True, False), (("v1~2.md",), False, False),
    (("bad\udcffname",), False, False),
    (("run-10:00.log",), False, not WINDOWS), (("CON",), False, not WINDOWS), (("aux.txt",), False, not WINDOWS),
    (("notes.",), False, not WINDOWS), (("what?.md",), False, not WINDOWS),
])
def test_one_rule_says_what_is_listed_served_and_archived(parts, scratch, taken):
    assert served(parts, scratch) is taken
    if taken:
        assert check_rel("/".join(parts), scratch=scratch) == parts
    else:
        with pytest.raises(CopyError) as err:
            check_rel("/".join(parts), scratch=scratch)
        assert err.value.code == "bad_name" and err.value.status == 400


def test_a_name_that_is_no_utf8_text_is_refused_and_named_printably():
    with pytest.raises(CopyError) as err:
        check_rel("a/b\udcffc")
    assert err.value.code == "bad_name" and "\udcff" not in str(err.value)
    str(err.value).encode("utf-8")


@pytest.mark.parametrize("name, value", [
    ("app.js", "attachment; filename=\"app.js\"; filename*=UTF-8''app.js"),
    ("отчёт v1.pdf",
     "attachment; filename=\"______v1.pdf\"; filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82%20v1.pdf"),
    ('a"b;c.txt', "attachment; filename=\"a_b_c.txt\"; filename*=UTF-8''a%22b%3Bc.txt"),
    ("Ж", "attachment; filename=\"_\"; filename*=UTF-8''%D0%96"),
])
def test_a_download_is_an_attachment_named_in_utf8_with_an_ascii_fallback(name, value):
    assert attachment(name) == value


# ---------------------------------------------------------------------------------------------- listing
async def test_a_folder_is_listed_in_name_order_without_git_or_scratch(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, sid, folder = await tree_session(hub)
        got = await listing(hub, uid)
        assert got.status == 200, got.body
        answer = _json(got)
        assert answer["path"] == "" and _names(answer["entries"]) == ["README.md", "docs", "empty", "src"]
        readme = answer["entries"][0]
        assert readme == {"name": "README.md", "path": "README.md", "type": "file", "size": 6,
                          "modified": readme["modified"]}
        assert abs(readme["modified"] - (folder / "README.md").stat().st_mtime) < 0.01
        assert answer["entries"][3] == {"name": "src", "path": "src", "type": "dir", "size": 0,
                                        "modified": answer["entries"][3]["modified"]}
        answer = _json(await listing(hub, uid, path="src"))
        assert answer["path"] == "src" and [(e["name"], e["path"], e["type"]) for e in answer["entries"]] == [
            ("app.js", "src/app.js", "file"), ("lib", "src/lib", "dir")]
        assert _json(await listing(hub, uid, path="docs"))["entries"] == []   # (its gitfile left out)
        assert _json(await listing(hub, uid, path="src/lib"))["entries"][0]["path"] == "src/lib/util.js"
        _no_path(answer, tmp_path)
        assert sid not in hub.manager.swarms   # (never loaded)
    finally:
        await hub.close()


async def test_scratch_is_listed_only_when_asked_and_its_skills_never(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await tree_session(hub)
        assert ".scratch" in _names(_json(await listing(hub, uid, scratch="true"))["entries"])
        assert ".scratch" not in _names(_json(await listing(hub, uid, scratch="false"))["entries"])
        assert _names(_json(await listing(hub, uid, path=".scratch", scratch="true"))["entries"]) == ["anna"]
        for query in ({"path": ".scratch"}, {"path": ".scratch/anna"}, {"path": ".scratch/_skills", "scratch": "true"},
                      {"path": ".scratch/_skills/pdf", "scratch": "true"}):
            got = await listing(hub, uid, **query)
            assert got.status == 400 and _json(got)["code"] == "bad_name", (query, got.body)
        got = await download(hub, uid, ".scratch/anna/notes.md")
        assert got.status == 400 and _json(got)["code"] == "bad_name"
        got = await download(hub, uid, ".scratch/anna/notes.md", {"scratch": "true"})
        assert got.status == 200 and got.body == b"notes"
        got = await download(hub, uid, ".scratch/_skills/pdf/SKILL.md", {"scratch": "true"})
        assert got.status == 400 and _json(got)["code"] == "bad_name"
        names = set(_zip(await archive(hub, uid, {"scratch": "true"})))
        assert ".scratch/anna/notes.md" in names and not any(name.startswith(".scratch/_skills") for name in names)
        assert not any(name.startswith(".scratch") for name in _zip(await archive(hub, uid)))
    finally:
        await hub.close()


async def test_what_a_listing_or_a_download_is_asked_for_must_be_there_and_of_its_kind(tmp_path, api_token,
                                                                                         no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await tree_session(hub)
        cases = [
            (listing(hub, uid, path="README.md"), 400, "bad_request", "is a file"),
            (listing(hub, uid, path="nope"), 404, "not_found", "No such file"),
            (listing(hub, uid, path="README.md/x"), 404, "not_found", "No such file"),
            (download(hub, uid, "src"), 400, "bad_request", "is a folder"),
            (download(hub, uid, ""), 400, "bad_request", "is a folder"),
            (download(hub, uid, "nope.txt"), 404, "not_found", "No such file"),
            (download(hub, uid, "README.md/x"), 404, "not_found", "No such file"),
            (archive(hub, uid, {"path": "README.md"}), 400, "bad_request", "is a file"),
            (archive(hub, uid, {"path": "nope"}), 404, "not_found", "No such file"),
            (listing(hub, uid, paths="x"), 400, "unknown_field", "Unknown field"),
            (download(hub, uid, "README.md", {"path": "x"}), 400, "unknown_field", "Unknown field"),
            (archive(hub, uid, {"scratch": "yes"}), 400, "bad_request", "true or false"),
            (fetch(hub, "/api/v1/sessions/0123456789abcdef0123456789abcdef/files"), 404, "not_found",
             "No such session"),
        ]
        for call, status, code, text in cases:
            got = await call
            assert got.status == status and _json(got)["code"] == code and text in _json(got)["error"], got.body
            _no_path(_json(got), tmp_path)
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- download
async def test_a_file_is_downloaded_as_an_attachment_in_chunks(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    monkeypatch.setattr(api_v1, "FILE_CHUNK", 1000)
    big = bytes(range(256)) * 10   # 2560 bytes: three reads
    try:
        uid, _sid, _folder = await session_with(hub, {"src/app.js": "run()", "data/отчёт.bin": big})
        got = await download(hub, uid, "src/app.js")
        assert got.status == 200 and got.ended and got.body == b"run()"
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["content-length"] == "5" and got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["content-disposition"] == attachment("app.js")
        got = await download(hub, uid, "data/отчёт.bin")
        assert got.status == 200 and got.body == big and got.messages == 4   # (3 chunks, then the end)
        assert got.headers["content-length"] == str(len(big))
        assert "filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82.bin" in got.headers["content-disposition"]
    finally:
        await hub.close()


async def test_a_file_that_shrinks_while_it_is_sent_cuts_the_answer(tmp_path, api_token, no_start, monkeypatch,
                                                                     caplog):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    monkeypatch.setattr(api_v1, "FILE_CHUNK", 4)
    try:
        uid, _sid, folder = await session_with(hub, {"log.txt": b"0123456789ab"})

        def shrink(n: int) -> None:
            if n == 1:
                with open(folder / "log.txt", "r+b") as f:
                    f.truncate(6)

        got = await download(hub, uid, "log.txt", on_body=shrink)
        assert got.status == 200 and got.headers["content-length"] == "12"
        assert got.body == b"012345" and not got.ended and isinstance(got.error, OSError)
        assert "was cut" in caplog.text
    finally:
        await hub.close()


async def test_a_download_left_by_its_client_closes_its_file(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    monkeypatch.setattr(api_v1, "FILE_CHUNK", 4)
    opened: list[Any] = []
    real = ApiV1._open_file

    def spy(self: ApiV1, sid: str, asked: tuple[str, ...]) -> Any:
        file, size = real(self, sid, asked)
        opened.append(file)
        return file, size

    monkeypatch.setattr(ApiV1, "_open_file", spy)
    try:
        uid, _sid, folder = await session_with(hub, {"big.bin": b"x" * 400})
        got = await download(hub, uid, "big.bin", leave_after=1)
        assert got.status == 200 and not got.ended and len(got.body) < 400
        assert len(opened) == 1 and opened[0].closed
        (folder / "big.bin").unlink()   # (nothing holds it: on Windows an open file would be in the way)
    finally:
        await hub.close()


async def test_a_file_replaced_while_it_is_opened_is_a_conflict(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    real = nofollow._FS.child

    def child(parent, name, rel, **kw):
        if name == "a.txt":
            raise nofollow.Refused(rel, "replaced")
        return real(parent, name, rel, **kw)

    try:
        uid, _sid, _folder = await session_with(hub, {"src/a.txt": "A"})
        monkeypatch.setattr(nofollow._FS, "child", child)
        got = await download(hub, uid, "src/a.txt")
        assert got.status == 409 and _json(got)["code"] == "conflict" and got.messages == 1, got.body
        assert "'src/a.txt' changed while it was read" in _json(got)["error"]
        _no_path(_json(got), tmp_path)
    finally:
        await hub.close()


async def test_a_step_cancelled_waits_for_its_thread_then_releases_what_it_gave():
    """A cancel of the task itself (the hub shutting down), even twice, waits for the thread, then hands what it gave
    to `release`: nothing it opened stays open, no thread reads a closed file."""
    done = threading.Event()
    released: list[str] = []

    def work() -> str:
        time.sleep(0.2)
        done.set()
        return "opened"

    task = asyncio.ensure_future(api_v1._thread_step(work, release=released.append))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.is_set() and released == ["opened"]


async def test_a_client_gone_during_a_long_step_leaves_the_event_loop_idle(tmp_path, api_token, no_start,
                                                                            monkeypatch):
    """Starlette cancels a streamed answer through an anyio cancel scope, which cancels a task still waiting again at
    every pass of the event loop: the wait for the step's thread is shielded from it, so the loop rests meanwhile."""
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    stepped = threading.Event()

    def slow_parts(*args: Any) -> Any:
        def parts() -> Any:
            yield b"PK"
            time.sleep(0.3)   # (one long step: a stalled share, a large file being read)
            stepped.set()
            yield b"more"
        return parts()

    monkeypatch.setattr(api_v1, "_zip_parts", slow_parts)
    loop = asyncio.get_running_loop()
    passes = [0]
    real = loop._run_once

    def counted() -> None:
        passes[0] += 1
        real()

    try:
        uid, _sid, _folder = await session_with(hub, {"a.txt": "A"})
        loop._run_once = counted   # (the loop's own pass: run_forever looks it up at every pass)
        try:
            got = await archive(hub, uid, leave_after=1)
        finally:
            del loop._run_once
        assert got.status == 200 and not got.ended and stepped.is_set()
        assert passes[0] < 300, passes[0]   # (a loop that spins makes tens of thousands in 0.3 s)
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- path escapes
ESCAPES = ["../secret.txt", "src/../../secret.txt", "./README.md", "src//app.js", "/etc/passwd", "src\\app.js",
           "a\x00b", ".git/config", ".GIT/config", "src/.git/HEAD", "docs/.git", "GIT~1/config", "SCRATC~1/_skills/x"]
# names NTFS reads as others: refused on a Windows hub only (elsewhere ordinary names, here not there)
NTFS = [".git::$INDEX_ALLOCATION/config", "README.md::$DATA", "CON", "aux.txt", "src/app.js.", "src /app.js",
        "what?.md"]


@pytest.mark.parametrize("rel", ESCAPES + NTFS)
async def test_a_path_out_of_the_folder_or_into_git_is_a_bad_name(tmp_path, api_token, no_start, rel):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await tree_session(hub)
        refused = rel in ESCAPES or WINDOWS
        for got in (await download(hub, uid, rel), await listing(hub, uid, path=rel),
                    await archive(hub, uid, {"path": rel})):
            answer = _json(got)
            assert (got.status, answer["code"]) == ((400, "bad_name") if refused else (404, "not_found")), answer
            assert SECRET not in got.body
            _no_path(answer, tmp_path)
    finally:
        await hub.close()


@pytest.mark.skipif(WINDOWS, reason="POSIX names")
async def test_a_name_with_a_colon_is_listed_served_and_archived_on_posix(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await session_with(hub, {"run-10:00.log": "ran", "what?.md": "q"})
        assert _names(_json(await listing(hub, uid))["entries"]) == ["run-10:00.log", "what?.md"]
        assert (await download(hub, uid, "run-10:00.log")).body == b"ran"
        assert _zip(await archive(hub, uid)) == {"run-10:00.log": b"ran", "what?.md": b"q"}
    finally:
        await hub.close()


@pytest.mark.skipif(not WINDOWS, reason="names NTFS reads as others")
async def test_names_ntfs_reads_as_others_are_left_out_on_windows(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    made: list[str] = []
    try:
        uid, _sid, folder = await session_with(hub, {"ok.txt": "ok"})
        for name in ("CON", "notes.", "trail "):   # (made through the \\?\ form, which takes a name as it is)
            made.append("\\\\?\\" + str(folder / name))
            with open(made[-1], "wb") as f:
                f.write(SECRET)
        assert _names(_json(await listing(hub, uid))["entries"]) == ["ok.txt"]
        assert _zip(await archive(hub, uid)) == {"ok.txt": b"ok"}
        got = await download(hub, uid, "CON")
        assert got.status == 400 and _json(got)["code"] == "bad_name"
    finally:
        for path in made:
            os.remove(path)
        await hub.close()


# ---------------------------------------------------------------------------------------------- links, hard links
async def test_a_link_to_outside_is_never_listed_served_or_archived(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    outside = _outside(tmp_path)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": "A"})
        _dir_link(outside, folder / "out")
        if not WINDOWS:   # (a symlink to a file needs a privilege on Windows)
            os.symlink(outside / "secret.txt", folder / "secret.txt")
        assert _names(_json(await listing(hub, uid))["entries"]) == ["a.txt"]
        for call in (download(hub, uid, "out/secret.txt"), listing(hub, uid, path="out"),
                     archive(hub, uid, {"path": "out"}), download(hub, uid, "out/site/secret.txt")):
            got = await call
            assert got.status == 404 and "is not served" in _json(got)["error"] and SECRET not in got.body, got.body
            _no_path(_json(got), tmp_path)
        if not WINDOWS:
            got = await download(hub, uid, "secret.txt")
            assert got.status == 404 and SECRET not in got.body
        got = await archive(hub, uid)
        assert _zip(got) == {"a.txt": b"A"} and SECRET not in got.body
    finally:
        await hub.close()


async def test_a_file_with_another_name_is_never_listed_served_or_archived(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    outside = _outside(tmp_path)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": "A", "b.txt": "B"})
        os.link(folder / "b.txt", folder / "b2.txt")              # (both names have st_nlink 2)
        os.link(outside / "secret.txt", folder / "hard.txt")     # another name of a file outside the folder
        assert _names(_json(await listing(hub, uid))["entries"]) == ["a.txt"]
        for rel in ("b.txt", "b2.txt", "hard.txt"):
            got = await download(hub, uid, rel)
            assert got.status == 404 and "is not served" in _json(got)["error"] and SECRET not in got.body
        got = await archive(hub, uid)
        assert _zip(got) == {"a.txt": b"A"} and SECRET not in got.body
    finally:
        await hub.close()


def _before_child(monkeypatch, name: str, action, *, times: int = 1) -> list[int]:
    """Run `action` once, just before nofollow opens `name` for the `times`-th time (after the listing that gave
    it): a swap between a walk's listing and its open."""
    real = nofollow._FS.child
    seen: list[int] = []

    def child(parent, entry_name, rel, **kw):
        if entry_name == name:
            seen.append(1)
            if len(seen) == times:
                action()
        return real(parent, entry_name, rel, **kw)

    monkeypatch.setattr(nofollow._FS, "child", child)
    return seen


async def test_a_folder_swapped_for_a_link_while_it_is_archived_cuts_the_archive(tmp_path, api_token, no_start,
                                                                                  monkeypatch, caplog):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    outside = _outside(tmp_path)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": "A", "sub/b.txt": "B", "sub/site/c.txt": "C"})
        # (the first walk measures and opens `sub` too; the swap comes at the second walk's open)
        _before_child(monkeypatch, "sub", lambda: _swap_for_link(folder / "sub", outside), times=2)
        got = await archive(hub, uid)
        assert got.status == 200 and not got.ended and isinstance(got.error, nofollow.Refused)
        assert SECRET not in got.body
        with pytest.raises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(got.body))
        assert "was cut" in caplog.text
    finally:
        await hub.close()


async def test_a_file_gone_between_the_two_walks_cuts_the_archive(tmp_path, api_token, no_start, monkeypatch):
    """The client never gets a valid zip that lacks a file: without its central directory it is no zip."""
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": "A", "b.txt": "B"})
        real = api_v1._zip_parts

        def parts(*args: Any) -> Any:
            (folder / "b.txt").unlink()
            return real(*args)

        monkeypatch.setattr(api_v1, "_zip_parts", parts)
        got = await archive(hub, uid)
        assert got.status == 200 and not got.ended and isinstance(got.error, api_v1._Cut)
        with pytest.raises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(got.body))
    finally:
        await hub.close()


async def test_a_folder_swapped_for_a_link_while_it_is_measured_is_a_conflict_before_any_byte(
        tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    outside = _outside(tmp_path)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": "A", "sub/b.txt": "B"})
        _before_child(monkeypatch, "sub", lambda: _swap_for_link(folder / "sub", outside), times=1)
        got = await archive(hub, uid)
        assert got.status == 409 and got.messages == 1 and got.error is None, got.body
        answer = _json(got)
        assert answer["code"] == "conflict" and "'sub' changed while it was read" in answer["error"]
        assert SECRET not in got.body
        _no_path(answer, tmp_path)
    finally:
        await hub.close()


async def test_a_file_that_shrinks_while_it_is_archived_cuts_the_archive(tmp_path, api_token, no_start, monkeypatch,
                                                                         caplog):
    """Never a valid zip that holds a torn file: the archive's files are read to the size they had when opened."""
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    monkeypatch.setattr(api_v1, "ZIP_SENT", 1)
    monkeypatch.setattr(api_v1, "FILE_CHUNK", 1000)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": b"0123456789" * 20000})

        def shrink(n: int) -> None:
            if n == 1:
                with open(folder / "a.txt", "r+b") as f:
                    f.truncate(6)

        got = await archive(hub, uid, on_body=shrink)
        assert got.status == 200 and not got.ended and isinstance(got.error, api_v1._Cut)
        with pytest.raises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(got.body))
        assert "shrank while it was read" in caplog.text
    finally:
        await hub.close()


async def test_a_file_that_grows_while_it_is_archived_is_taken_at_its_size_when_opened(tmp_path, api_token,
                                                                                      no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    monkeypatch.setattr(api_v1, "ZIP_SENT", 1)
    monkeypatch.setattr(api_v1, "FILE_CHUNK", 1000)
    try:
        uid, _sid, folder = await session_with(hub, {"log.txt": b"a" * 3000})

        def grow(n: int) -> None:
            if n == 1:
                with open(folder / "log.txt", "ab") as f:
                    f.write(b"b" * 5000)

        got = await archive(hub, uid, on_body=grow)
        assert got.status == 200 and got.ended and got.error is None
        assert _zip(got) == {"log.txt": b"a" * 3000}
    finally:
        await hub.close()


def _unreadable(monkeypatch, *rels: str) -> None:
    """nofollow lists the folders `rels` (relative to the root) as a folder the hub may not read: PermissionError."""
    real = nofollow._FS.listing

    def listing_(folder, rel):
        if rel in rels:
            raise OSError(errno.EACCES, "Access is denied.", rel or ".")
        return real(folder, rel)

    monkeypatch.setattr(nofollow._FS, "listing", listing_)


async def test_a_folder_that_cannot_be_read_is_a_conflict_everywhere(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await session_with(hub, {"a.txt": "A", "locked/b.txt": "B"})
        _unreadable(monkeypatch, "locked")
        for call in (listing(hub, uid, path="locked"), archive(hub, uid), archive(hub, uid, {"path": "locked"})):
            got = await call
            answer = _json(got)
            assert got.status == 409 and answer["code"] == "conflict" and got.messages == 1, got.body
            assert "'locked' cannot be read on the hub's computer (Access is denied)" in answer["error"]
            _no_path(answer, tmp_path)
        assert _names(_json(await listing(hub, uid))["entries"]) == ["a.txt", "locked"]
    finally:
        await hub.close()


async def test_a_session_folder_that_cannot_be_read_is_not_ready(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await session_with(hub, {"a.txt": "A"})
        _unreadable(monkeypatch, "")
        for call in (listing(hub, uid), archive(hub, uid)):
            got = await call
            assert got.status == 409 and _json(got)["code"] == "folder_not_ready", got.body
            assert _json(got)["error"] == api_v1.FOLDER_UNREADABLE_TEXT
    finally:
        await hub.close()


async def test_an_archive_left_by_its_client_closes_its_walk(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    monkeypatch.setattr(api_v1, "ZIP_SENT", 1)
    made: list[Any] = []
    real = api_v1._zip_parts

    def parts(*args: Any) -> Any:
        made.append(real(*args))
        return made[-1]

    monkeypatch.setattr(api_v1, "_zip_parts", parts)
    try:
        uid, _sid, _folder = await session_with(hub, {f"f{i}.txt": "x" * 100 for i in range(5)})
        got = await archive(hub, uid, leave_after=1)
        assert got.status == 200 and not got.ended
        assert len(made) == 1 and made[0].gi_frame is None   # (closed: its walk with it)
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- archive
async def test_the_archive_is_a_zip_of_exactly_what_is_listed(tmp_path, api_token, no_start, monkeypatch):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    opened: list[str] = []
    real = nofollow._FS.child
    monkeypatch.setattr(nofollow._FS, "child", lambda parent, name, rel, **kw: (opened.append(name),
                                                                                real(parent, name, rel, **kw))[1])
    try:
        uid, sid, folder = await tree_session(hub)
        os.utime(folder / "README.md", (0, 0))   # (before 1980: a zip holds no such time)
        os.chmod(folder / "src" / "app.js", 0o666)
        if not WINDOWS:
            os.chmod(folder / "src" / "lib" / "util.js", 0o4777)
        got = await archive(hub, uid)
        assert got.status == 200 and got.ended and got.error is None
        assert got.headers["content-type"] == "application/zip" and "content-length" not in got.headers
        assert got.headers["content-disposition"] == attachment(f"{sid}.zip")
        assert got.headers["x-content-type-options"] == "nosniff" and got.headers["cache-control"] == "no-store"
        entries = _zip(got)
        assert entries == {**FILES, **dict.fromkeys(FOLDERS)}
        assert ".git" not in opened   # (never entered, at any depth)
        with zipfile.ZipFile(io.BytesIO(got.body)) as zf:
            readme = zf.getinfo("README.md")
            assert readme.date_time == (1980, 1, 1, 0, 0, 0) and readme.compress_type == zipfile.ZIP_DEFLATED
            assert stat.S_ISREG(readme.external_attr >> 16) and stat.S_ISDIR(zf.getinfo("src/").external_attr >> 16)
            # (never writable by others, never setuid: 0o644, or 0o755 for a folder and an executable file)
            modes = {info.filename: stat.S_IMODE(info.external_attr >> 16) for info in zf.infolist()}
            assert modes["README.md"] == 0o644 and modes["src/"] == 0o755 and modes["src/app.js"] == 0o644
            assert modes["src/lib/util.js"] == (0o644 if WINDOWS else 0o755)
        got = await archive(hub, uid, {"path": "src"})
        assert _zip(got) == {"app.js": b"run()", "lib/": None, "lib/util.js": b"u()"}
        assert got.headers["content-disposition"] == attachment("src.zip")
        assert sid not in hub.manager.swarms
    finally:
        await hub.close()


async def test_an_archive_over_its_limit_is_refused_before_any_byte(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path, api={"archive_max_mb": 1})))
    no_start(hub.manager)
    try:
        uid, _sid, _folder = await session_with(hub, {"a.bin": b"x" * (1 << 19), "sub/b.bin": b"y" * (1 << 19) + b"z"})
        got = await archive(hub, uid)
        assert got.status == 413 and got.messages == 1 and got.error is None
        answer = _json(got)
        assert answer["code"] == "too_large" and "archive_max_mb (1 MB)" in answer["error"]
        assert _zip(await archive(hub, uid, {"path": "sub"})) == {"b.bin": b"y" * (1 << 19) + b"z"}
    finally:
        await hub.close()


@pytest.mark.skipif(WINDOWS or not hasattr(os, "geteuid") or os.geteuid() == 0, reason="POSIX modes, not root")
async def test_a_folder_that_cannot_be_read_refuses_the_archive_before_any_byte(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, _sid, folder = await session_with(hub, {"a.txt": "A", "locked/b.txt": "B"})
        (folder / "locked").chmod(0)
        got = await archive(hub, uid)
        assert got.status == 409 and _json(got)["code"] == "conflict" and "locked" in _json(got)["error"]
        _no_path(_json(got), tmp_path)
    finally:
        (folder / "locked").chmod(0o755)
        await hub.close()


# ---------------------------------------------------------------------------------------------- sessions
async def test_a_running_session_is_read_and_a_stopped_one_without_loading_it(tmp_path, api_token):
    hub = Hub(waiting_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        _write(Path(hub.manager.workspace_of(sid)["path"]) / "out.md", "written")
        assert (await download(hub, uid, "out.md")).body == b"written"
        assert (await hub.call("POST", f"/api/v1/sessions/{uid}/stop"))[0] == 200
        assert sid not in hub.manager.swarms
        assert "out.md" in _names(_json(await listing(hub, uid))["entries"])
        assert _zip(await archive(hub, uid))["out.md"] == b"written"
        assert sid not in hub.manager.swarms
    finally:
        await hub.close()


async def test_the_api_token_reads_only_the_sessions_of_the_api(tmp_path, api_token, hub_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        made = await hub.manager.create("Board work", "Board work", start=False)
        sid = made["id"]
        uid = hub.manager.meta(sid)["uid"]
        _write(Path(hub.manager.workspace_of(sid)["path"]) / "a.txt", "A")
        for call in (listing(hub, uid), download(hub, uid, "a.txt"), archive(hub, uid)):
            got = await call
            assert got.status == 404 and _json(got)["code"] == "not_found"
        got = await download(hub, uid, "a.txt", token=TOKEN)   # (the owner sees every session)
        assert got.status == 200 and got.body == b"A"
    finally:
        await hub.close()


async def test_a_missing_folder_is_not_ready(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    try:
        uid, sid, folder = await session_with(hub, {"a.txt": "A"})
        (folder / "a.txt").unlink()
        folder.rmdir()
        for call in (listing(hub, uid), download(hub, uid, "a.txt"), archive(hub, uid)):
            got = await call
            assert got.status == 409 and _json(got)["code"] == "folder_not_ready", got.body
            _no_path(_json(got), tmp_path)
    finally:
        await hub.close()


async def test_a_session_folder_swapped_for_a_link_is_never_read(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    no_start(hub.manager)
    outside = _outside(tmp_path)
    try:
        uid, _sid, folder = await session_with(hub, {"secret.txt": "the session's own"})
        _swap_for_link(folder, outside)
        for call in (listing(hub, uid), download(hub, uid, "secret.txt"), archive(hub, uid)):
            got = await call
            answer = _json(got)
            assert got.status == 403 and answer["code"] == "forbidden_folder", got.body
            assert answer["error"] == api_v1.FOLDER_LINKED_TEXT and SECRET not in got.body
            _no_path(answer, tmp_path)
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- server folders
async def test_an_in_place_folder_is_read_below_its_root(tmp_path, api_token, no_start):
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    _write(places / "acme" / "other.txt", "a file of the root, beside the session's folder")
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme/site",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        uid = answer["session"]["id"]
        site = places / "acme" / "site"
        _write(site / "index.html", "<h1>Acme</h1>")
        _write(site / ".git" / "config", "[core]")
        got = await listing(hub, uid)
        assert _names(_json(got)["entries"]) == ["index.html"] and _json(got)["path"] == ""
        assert (await download(hub, uid, "index.html")).body == b"<h1>Acme</h1>"
        assert _zip(await archive(hub, uid)) == {"index.html": b"<h1>Acme</h1>"}
        for call in (download(hub, uid, "../other.txt"), download(hub, uid, ".git/config")):
            got = await call
            assert got.status == 400 and _json(got)["code"] == "bad_name"
        shutil.rmtree(site)
        got = await listing(hub, uid)
        assert got.status == 409 and _json(got)["code"] == "folder_not_ready", got.body
        _no_path(_json(got), tmp_path)
    finally:
        await hub.close()


@pytest.mark.parametrize("when", ["before the check", "after the check"])
async def test_a_link_put_on_the_way_to_an_in_place_folder_is_never_followed(tmp_path, api_token, no_start,
                                                                             monkeypatch, when):
    """Before the check, the check refuses it (as a start would); after it, nofollow meets it below the root."""
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    outside = _outside(tmp_path)   # (outside/site/secret.txt: where the swapped acme/site would lead)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme/site",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        uid = answer["session"]["id"]
        _write(places / "acme" / "site" / "secret.txt", "the session's own")
        if when == "before the check":
            _swap_for_link(places / "acme", outside)
        else:
            real = SessionManager._check_server_folder

            def check(self: SessionManager, sid: str, meta: dict[str, Any], **kw: Any) -> Any:
                checked = real(self, sid, meta, **kw)
                if not (places / "acme.old").exists():
                    _swap_for_link(places / "acme", outside)
                return checked

            monkeypatch.setattr(SessionManager, "_check_server_folder", check)
        got = await download(hub, uid, "secret.txt")
        assert got.status == 403 and _json(got)["code"] == "forbidden_folder", got.body
        assert SECRET not in got.body
        _no_path(_json(got), tmp_path)
    finally:
        await hub.close()


async def test_an_in_place_folder_whose_root_the_owner_took_back_is_not_read(tmp_path, api_token, no_start):
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "a", "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        _write(places / "a" / "x.txt", "x")
        raw = st.read_raw(hub.manager.home)
        raw["api"]["folders"]["places"]["in_place"] = False
        st.write_raw(hub.manager.home, raw)
        got = await download(hub, answer["session"]["id"], "x.txt")
        assert got.status == 403 and _json(got)["code"] == "forbidden_folder", got.body
        assert _json(got)["error"].endswith(f"{api_v1.FILES_REFUSAL}.")   # (not a start's words)
        _no_path(_json(got), tmp_path)
    finally:
        await hub.close()


@pytest.mark.skipif(not WINDOWS, reason="a file system that ignores letter case, whose final paths name the case")
async def test_an_in_place_folder_whose_parent_was_renamed_in_letter_case_is_still_read(tmp_path, api_token,
                                                                                        no_start):
    """The names below the root are the resolved folder's (its spelling on disk), not the stored path's."""
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme/site",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        _write(places / "acme" / "site" / "index.html", "<h1>Acme</h1>")
        (places / "acme").rename(places / "ACME")
        got = await download(hub, answer["session"]["id"], "index.html")
        assert got.status == 200 and got.body == b"<h1>Acme</h1>", got.body
    finally:
        await hub.close()


async def test_an_in_place_folder_inside_its_root_by_file_identity_only_is_not_read(tmp_path, api_token, no_start,
                                                                                    monkeypatch):
    """A folder the check holds inside its root (a bind mount, another spelling) but whose resolved path does not
    name it below the root is refused, never read by its own path after the check."""
    hub, _jobs, places = _hub(tmp_path)
    no_start(hub.manager)
    real = SessionManager._check_server_folder
    elsewhere = tmp_path / "mount" / "site"
    _write(elsewhere / "index.html", SECRET)

    def check(self: SessionManager, sid: str, meta: dict[str, Any], **kw: Any) -> Any:
        root, _folder = real(self, sid, meta, **kw)
        return root, elsewhere

    try:
        status, answer = await hub.create({"goal": "G", "folder": {"root": "places", "path": "acme/site",
                                                                   "mode": "in_place"}})
        assert status == 202, answer
        await hub.settled()
        monkeypatch.setattr(SessionManager, "_check_server_folder", check)
        for call in (listing(hub, answer["session"]["id"]), download(hub, answer["session"]["id"], "index.html")):
            got = await call
            assert got.status == 403 and _json(got)["code"] == "forbidden_folder", got.body
            assert SECRET not in got.body
            _no_path(_json(got), tmp_path)
    finally:
        await hub.close()
