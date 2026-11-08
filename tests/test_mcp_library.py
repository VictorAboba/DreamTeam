"""The MCP library on disk without a Swarm (mcp_library.py): listing, reading, atomic add with replace semantics,
remove, links never followed, the DREAMTEAM_MCP_LIBRARY_DIR override, and the same format mcp_forge writes and
reads (a real stdio server is started for the round trip)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from dreamteam import mcp_library, skills
from dreamteam.config import Config
from dreamteam.mcp_forge import MAX_CODE_BYTES, SERVER_TEMPLATE, MCPForge, snapshot_digest
from dreamteam.mcp_library import LibraryError
from dreamteam.models import USER, ToolError
from dreamteam.workspace import is_link

ALICE = "Alice Smith"
CODE = SERVER_TEMPLATE.encode("utf-8")
CODE_V2 = CODE + b"\n# v2\n"


@pytest.fixture(autouse=True)
def _fast_renames(monkeypatch):
    monkeypatch.setattr(skills, "RENAME_PAUSE", 0.0)


def make_entry(folder: Path, code: bytes = CODE, requirements: list | None = None, **meta) -> Path:
    """An entry folder in the library's format; meta.json records the digest unless `digest` is given."""
    folder.mkdir(parents=True)
    (folder / "server.py").write_bytes(code)
    reqs = [] if requirements is None else requirements
    data = {"name": folder.name, "description": "Arithmetic helpers", "owner": ALICE, "requirements": reqs,
            "tools": [{"name": "add", "description": "Add two numbers."}], "created_at": 1.0, "updated_at": 2.0,
            "project": "/somewhere/p1", "swarm": "p1", "digest": snapshot_digest(code, reqs), "approved_digest": None}
    data.update(meta)
    (folder / "meta.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


def dir_link(target: Path, link: Path) -> bool:
    """A link to a folder: a symlink, or on Windows without the symlink privilege a junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
            return True
        except OSError:
            pass
    return False


def leftovers(root: Path) -> list[str]:
    return sorted(n for n in os.listdir(root) if n.startswith("."))


# ---- reading -----------------------------------------------------------------------------------------------------
def test_read_entry_reports_digest_approval_and_changed_outside(tmp_path):
    root = tmp_path / "lib"
    digest = snapshot_digest(CODE, ["httpx>=0.27"])
    make_entry(root / "calc", requirements=["httpx>=0.27"], approved_digest=digest)
    entry = mcp_library.read_entry(root, "calc")
    assert entry is not None and entry.name == "calc" and entry.path == root / "calc"
    assert entry.code == CODE and entry.requirements == ["httpx>=0.27"] and entry.requirements_text == "httpx>=0.27\n"
    assert entry.digest == digest == entry.stored_digest == entry.approved_digest
    assert entry.approved and not entry.changed_outside
    assert entry.description == "Arithmetic helpers" and entry.owner == ALICE and entry.project == "/somewhere/p1"
    assert [t["name"] for t in entry.tools] == ["add"]

    # a hand edit of server.py: the recorded digest no longer matches, so the approval does not cover it either
    (root / "calc" / "server.py").write_bytes(CODE_V2)
    edited = mcp_library.read_entry(root, "calc")
    assert edited.changed_outside and not edited.approved and edited.digest == snapshot_digest(CODE_V2, ["httpx>=0.27"])
    assert edited.stored_digest == digest
    # a changed requirement counts as well (the digest covers it)
    meta = json.loads((root / "calc" / "meta.json").read_text("utf-8"))
    (root / "calc" / "server.py").write_bytes(CODE)
    meta["requirements"] = ["httpx>=0.28"]
    (root / "calc" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert mcp_library.read_entry(root, "calc").changed_outside


def test_hand_dropped_folder_is_listed(tmp_path):
    """A folder put there by hand (server.py and a minimal meta.json, no digest, no provenance) is an entry:
    mcp_forge lists it too. Nothing recorded its digest, so it counts as changed outside DreamTeam."""
    root = tmp_path / "lib"
    (root / "manual").mkdir(parents=True)
    (root / "manual" / "server.py").write_bytes(CODE)
    (root / "manual" / "meta.json").write_text('{"description": "By hand"}', encoding="utf-8")
    [entry] = mcp_library.list_entries(root)
    assert entry.name == "manual" and entry.description == "By hand" and entry.owner is None
    assert entry.requirements == [] and entry.digest == snapshot_digest(CODE, [])
    assert entry.stored_digest is None and entry.changed_outside and not entry.approved


def test_scan_skips_what_is_not_an_entry_and_says_why(tmp_path):
    root = tmp_path / "lib"
    make_entry(root / "good")
    make_entry(root / "Bad_Name")
    (root / "nometa").mkdir()
    (root / "nometa" / "server.py").write_bytes(CODE)
    (root / "noserver").mkdir()
    (root / "noserver" / "meta.json").write_text("{}", encoding="utf-8")
    make_entry(root / "brokenmeta")
    (root / "brokenmeta" / "meta.json").write_text("{not json", encoding="utf-8")
    make_entry(root / "listmeta")
    (root / "listmeta" / "meta.json").write_text("[1, 2]", encoding="utf-8")
    make_entry(root / "big", code=b"#" * (MAX_CODE_BYTES + 1))
    make_entry(root / "dirserver")
    (root / "dirserver" / "server.py").unlink()
    (root / "dirserver" / "server.py").mkdir()
    (root / ".t-abc-1").mkdir()
    (root / "notes.txt").write_text("x", encoding="utf-8")
    found = mcp_library.scan(root)
    assert [e.name for e in found.entries] == ["good"]
    reasons = dict(found.skipped)
    assert sorted(reasons) == ["Bad_Name", "big", "brokenmeta", "dirserver", "listmeta", "nometa", "noserver"]
    assert "not a valid MCP server name" in reasons["Bad_Name"]
    assert "too long" in reasons["big"] and "meta.json is missing" in reasons["nometa"]
    assert "server.py is missing" in reasons["noserver"] and "not valid JSON" in reasons["brokenmeta"]
    assert "not a JSON object" in reasons["listmeta"] and "not a regular file" in reasons["dirserver"]
    assert mcp_library.read_entry(root, "big") is None and mcp_library.read_entry(root, "Bad_Name") is None
    assert mcp_library.read_entry(root, "../lib/good") is None and mcp_library.read_entry(root, "missing") is None
    assert mcp_library.list_entries(tmp_path / "no-such-folder") == []


def test_server_py_of_exactly_the_limit_is_read(tmp_path):
    root = tmp_path / "lib"
    make_entry(root / "edge", code=b"#" * MAX_CODE_BYTES)
    assert len(mcp_library.read_entry(root, "edge").code) == MAX_CODE_BYTES


def test_link_entries_are_skipped_and_remove_deletes_only_the_link(tmp_path):
    root = tmp_path / "lib"
    make_entry(root / "good")
    outside = make_entry(tmp_path / "outside" / "evil")
    if not dir_link(outside, root / "evil"):
        pytest.skip("cannot make links here")
    found = mcp_library.scan(root)
    assert [e.name for e in found.entries] == ["good"] and dict(found.skipped) == {"evil": "it is a link"}
    assert mcp_library.read_entry(root, "evil") is None
    with pytest.raises(LibraryError, match="is a link"):
        mcp_library.read_folder(root / "evil")
    assert mcp_library.remove_entry(root, "evil") is True
    assert not os.path.lexists(root / "evil")
    assert (outside / "server.py").read_bytes() == CODE and (outside / "meta.json").exists()
    assert leftovers(root) == [] and [e.name for e in mcp_library.list_entries(root)] == ["good"]


def test_linked_files_inside_an_entry_are_not_read(tmp_path):
    root = tmp_path / "lib"
    make_entry(root / "calc")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"not for the library")
    (root / "calc" / "server.py").unlink()
    try:
        os.symlink(secret, root / "calc" / "server.py")
    except (OSError, NotImplementedError):
        pytest.skip("cannot make file symlinks here")
    assert mcp_library.read_entry(root, "calc") is None
    assert "not a regular file" in dict(mcp_library.scan(root).skipped)["calc"]
    with pytest.raises(LibraryError, match="not a regular file"):
        mcp_library.add_entry(tmp_path / "other", root / "calc")
    assert not (tmp_path / "other" / "calc").exists()


# ---- adding ------------------------------------------------------------------------------------------------------
def test_add_unchanged_and_rewrite(tmp_path):
    root, src = tmp_path / "lib", make_entry(tmp_path / "export" / "calc")
    assert mcp_library.add_entry(root, src) == "added"
    assert (root / "calc" / "server.py").read_bytes() == CODE
    assert json.loads((root / "calc" / "meta.json").read_text("utf-8")) == json.loads(
        (src / "meta.json").read_text("utf-8"))
    assert mcp_library.add_entry(root, src) == "unchanged"
    assert mcp_library.add_entry(root, src, replace=True) == "unchanged"

    # other code under the same name: a conflict unless replace
    other = make_entry(tmp_path / "export2" / "calc", code=CODE_V2, description="Calc v2")
    with pytest.raises(LibraryError) as err:
        mcp_library.add_entry(root, other)
    assert err.value.status == 409 and err.value.extra == {"exists": ["calc"]}
    assert (root / "calc" / "server.py").read_bytes() == CODE
    assert mcp_library.add_entry(root, other, replace=True) == "rewritten"
    entry = mcp_library.read_entry(root, "calc")
    assert entry.code == CODE_V2 and entry.description == "Calc v2" and not entry.changed_outside
    assert leftovers(root) == []

    # the same code with another meta.json is a difference too
    meta_only = make_entry(tmp_path / "export3" / "calc", code=CODE_V2, description="Calc v2, renamed")
    with pytest.raises(LibraryError):
        mcp_library.add_entry(root, meta_only)
    assert mcp_library.add_entry(root, meta_only, replace=True) == "rewritten"
    assert mcp_library.read_entry(root, "calc").description == "Calc v2, renamed"


def test_add_under_another_name_and_meta_updates(tmp_path):
    root, src = tmp_path / "lib", make_entry(tmp_path / "export" / "calc")
    moved = {"project": "/here/sessions/016-x"}
    assert mcp_library.add_entry(root, src, name="calc_copy", meta_updates=moved) == "added"
    entry = mcp_library.read_entry(root, "calc_copy")
    assert entry.project == "/here/sessions/016-x" and entry.code == CODE
    assert mcp_library.add_entry(root, src, name="calc_copy", meta_updates=moved) == "unchanged"
    with pytest.raises(LibraryError, match="different server 'calc_copy'") as err:
        mcp_library.add_entry(root, src, name="calc_copy")  # the project differs now
    assert err.value.status == 409
    assert not (src / "meta.json").read_text("utf-8").count("016-x")  # the source folder is never written


def test_add_keeps_an_approval_the_library_records_for_the_same_code(tmp_path):
    root = tmp_path / "lib"
    digest = snapshot_digest(CODE, [])
    make_entry(root / "calc", approved_digest=digest)
    unapproved = make_entry(tmp_path / "export" / "calc", approved_digest=None)
    assert mcp_library.add_entry(root, unapproved) == "unchanged"
    assert mcp_library.read_entry(root, "calc").approved
    changed = make_entry(tmp_path / "export2" / "calc", approved_digest=None, description="new text")
    assert mcp_library.add_entry(root, changed, replace=True) == "rewritten"
    entry = mcp_library.read_entry(root, "calc")
    assert entry.approved and entry.description == "new text"
    # other code: the old approval does not cover it
    v2 = make_entry(tmp_path / "export3" / "calc", code=CODE_V2, approved_digest=None)
    assert mcp_library.add_entry(root, v2, replace=True) == "rewritten"
    assert not mcp_library.read_entry(root, "calc").approved


def test_add_carries_the_exported_approval(tmp_path):
    digest = snapshot_digest(CODE, [])
    src = make_entry(tmp_path / "export" / "calc", approved_digest=digest)
    assert mcp_library.add_entry(tmp_path / "lib", src) == "added"
    assert mcp_library.read_entry(tmp_path / "lib", "calc").approved


@pytest.mark.parametrize("bad", ["Calc", "c", "1calc", "calc-tools", "x" * 41, "..", ".t-abc", "a/b", ""])
def test_add_refuses_bad_names(tmp_path, bad):
    src = make_entry(tmp_path / "export" / "calc")
    with pytest.raises(LibraryError, match="Invalid MCP server name"):
        mcp_library.add_entry(tmp_path / "lib", src, name=bad)
    assert not (tmp_path / "lib").exists() or os.listdir(tmp_path / "lib") == []
    with pytest.raises(LibraryError, match="Invalid MCP server name"):
        mcp_library.remove_entry(tmp_path / "lib", bad)


def test_add_refuses_a_bad_folder_name(tmp_path):
    src = make_entry(tmp_path / "export" / "Calc")
    with pytest.raises(LibraryError, match="Invalid MCP server name"):
        mcp_library.add_entry(tmp_path / "lib", src)


def test_add_refuses_an_oversized_server_py(tmp_path):
    src = make_entry(tmp_path / "export" / "huge", code=b"#" * (MAX_CODE_BYTES + 1))
    with pytest.raises(LibraryError, match="too long") as err:
        mcp_library.add_entry(tmp_path / "lib", src)
    assert err.value.status == 413
    assert not (tmp_path / "lib" / "huge").exists()


@pytest.mark.parametrize("requirements", ["httpx", [1], ["-e ."], ["--index-url=http://x"], ["httpx", "httpx"],
                                          [" httpx"], [">=1"], ["x\ny"], [f"r{i}" for i in range(31)]])
def test_add_refuses_requirements_mcp_forge_would_not_take(tmp_path, requirements):
    src = make_entry(tmp_path / "export" / "calc", requirements=requirements)
    with pytest.raises(LibraryError, match="MCP server 'calc'"):
        mcp_library.add_entry(tmp_path / "lib", src)
    assert not (tmp_path / "lib" / "calc").exists()


def test_check_requirements_keeps_valid_lists():
    assert mcp_library.check_requirements([]) == []
    assert mcp_library.check_requirements(["httpx>=0.27,<1", "pypdf"]) == ["httpx>=0.27,<1", "pypdf"]


def test_add_refuses_a_source_that_is_a_link_or_missing(tmp_path):
    real = make_entry(tmp_path / "real" / "calc")
    with pytest.raises(LibraryError) as err:
        mcp_library.add_entry(tmp_path / "lib", tmp_path / "nothing" / "calc")
    assert err.value.status == 404
    (tmp_path / "links").mkdir()
    if not dir_link(real, tmp_path / "links" / "calc"):
        pytest.skip("cannot make links here")
    with pytest.raises(LibraryError, match="is a link"):
        mcp_library.add_entry(tmp_path / "lib", tmp_path / "links" / "calc")


def test_add_over_a_link_needs_replace_and_never_follows_it(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    outside = make_entry(tmp_path / "outside" / "calc", code=CODE_V2)
    if not dir_link(outside, root / "calc"):
        pytest.skip("cannot make links here")
    src = make_entry(tmp_path / "export" / "calc")
    with pytest.raises(LibraryError) as err:
        mcp_library.add_entry(root, src)
    assert err.value.status == 409
    assert mcp_library.add_entry(root, src, replace=True) == "rewritten"
    assert not is_link(root / "calc") and (root / "calc" / "server.py").read_bytes() == CODE
    assert (outside / "server.py").read_bytes() == CODE_V2  # the link's target is untouched
    assert leftovers(root) == []


def test_a_failed_swap_puts_the_old_entry_back(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    make_entry(root / "calc")
    src = make_entry(tmp_path / "export" / "calc", code=CODE_V2)
    real_rename = skills._rename

    def rename(a, b):
        if Path(a).name.startswith(".t-"):
            raise PermissionError(13, "in use")
        real_rename(a, b)

    monkeypatch.setattr(mcp_library, "_rename", rename)
    with pytest.raises(PermissionError):
        mcp_library.add_entry(root, src, replace=True)
    assert mcp_library.read_entry(root, "calc").code == CODE
    assert leftovers(root) == []


def test_a_failed_write_leaves_nothing_behind(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    src = make_entry(tmp_path / "export" / "calc")

    def broken(folder, code, meta):
        os.mkdir(folder)
        (folder / "server.py").write_bytes(code[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mcp_library, "write_entry_folder", broken)
    with pytest.raises(OSError):
        mcp_library.add_entry(root, src)
    assert os.listdir(root) == []


def test_add_removes_stale_temp_folders(tmp_path):
    root = tmp_path / "lib"
    (root / ".t-abcdef-1000").mkdir(parents=True)
    (root / ".o-abcdef-1000").mkdir()
    (root / ".t-fresh-9999999999").mkdir()  # a write going on right now stays
    for name in (".t-abcdef-1000", ".o-abcdef-1000"):
        os.utime(root / name, (1000, 1000))
    assert mcp_library.add_entry(root, make_entry(tmp_path / "export" / "calc")) == "added"
    assert leftovers(root) == [".t-fresh-9999999999"]


# ---- removing ----------------------------------------------------------------------------------------------------
def test_remove_entry(tmp_path):
    root = tmp_path / "lib"
    make_entry(root / "calc")
    make_entry(root / "other")
    assert mcp_library.remove_entry(root, "calc") is True
    assert not (root / "calc").exists() and [e.name for e in mcp_library.list_entries(root)] == ["other"]
    assert mcp_library.remove_entry(root, "calc") is False
    assert mcp_library.remove_entry(tmp_path / "missing", "calc") is False
    assert leftovers(root) == []


def test_remove_reports_a_folder_in_use(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    make_entry(root / "calc")

    def refuse(a, b):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(mcp_library, "_rename", refuse)
    with pytest.raises(LibraryError, match="a file in use") as err:
        mcp_library.remove_entry(root, "calc")
    assert err.value.status == 409 and (root / "calc" / "server.py").exists()


# ---- configuration ---------------------------------------------------------------------------------------------
def test_env_override_wins_over_the_settings(tmp_path, monkeypatch):
    raw = {"mcp": {"library_dir": str(tmp_path / "configured")}}
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(tmp_path / "from-env"))
    assert Config.from_dict(tmp_path / "home", raw).library_dir == tmp_path / "from-env"
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", "")
    assert Config.from_dict(tmp_path / "home", raw).library_dir == tmp_path / "configured"
    monkeypatch.delenv("DREAMTEAM_MCP_LIBRARY_DIR")
    assert Config.from_dict(tmp_path / "home", raw).library_dir == tmp_path / "configured"
    home = Path(os.path.expanduser("~"))
    assert Config.from_dict(tmp_path / "home", {}).library_dir == home / ".dreamteam" / "library"


def test_tests_never_use_the_real_library(tmp_path, monkeypatch):
    """conftest's isolation names a fresh folder, and a test's own monkeypatch.undo() keeps it."""
    library = Config.from_dict(tmp_path, {}).library_dir
    assert library != Path(os.path.expanduser("~/.dreamteam/library")) and "mcplibrary" in library.parent.name
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(tmp_path / "mine"))
    monkeypatch.undo()
    assert Config.from_dict(tmp_path, {}).library_dir == library


# ---- the format mcp_forge writes and reads ---------------------------------------------------------------------
@pytest.fixture
async def forges(make_fake_swarm):
    started: list[MCPForge] = []

    async def factory(library: Path, subdir: str, **mcp):
        swarm = make_fake_swarm({"mcp": {"startup_timeout": 60, "call_timeout": 30, **mcp}}, subdir=subdir)
        swarm.add_member(ALICE)
        forge = MCPForge(swarm)
        swarm.forge = forge
        await forge.start()
        started.append(forge)
        return swarm, forge

    yield factory
    for forge in started:
        await forge.stop()


async def test_round_trip_with_mcp_forge(tmp_path, monkeypatch, forges):
    """An entry mcp_forge saved reads with its digest and approval; copied into another library with add_entry it
    imports there without a new approval; a hand edit there makes the import wait for the user again."""
    first, second = tmp_path / "lib1", tmp_path / "lib2"
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(first))
    swarm1, forge1 = await forges(first, "p1", require_approval=True)
    assert swarm1.config.library_dir == first
    await forge1.create(USER, "calc", "Arithmetic helpers", SERVER_TEMPLATE)
    saved = mcp_library.read_entry(first, "calc")
    assert saved is not None and saved.approved and not saved.changed_outside
    assert saved.digest == swarm1.db.one("SELECT digest FROM mcp_servers WHERE name = 'calc'")["digest"]
    assert saved.project == str(swarm1.config.project_dir) and "add" in [t["name"] for t in saved.tools]

    assert mcp_library.add_entry(second, saved.path, meta_updates={"project": "/other/computer/p1"}) == "added"
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(second))
    swarm2, forge2 = await forges(second, "p2", require_approval=True)
    assert forge2.get("calc")["status"] == "available"
    assert [t["name"] for t in await forge2.tools(ALICE, "calc")] == ["add"]
    assert forge2.get("calc")["status"] == "active" and swarm2.notifications_for(USER) == []

    (second / "calc" / "server.py").write_bytes(saved.code + b"\n# edited by hand\n")
    assert mcp_library.read_entry(second, "calc").changed_outside
    swarm3, forge3 = await forges(second, "p3", require_approval=True)
    with pytest.raises(ToolError, match="waiting for the user's approval"):
        await forge3.tools(ALICE, "calc")
    assert forge3.get("calc")["status"] == "pending"

    # remove_entry: mcp_forge no longer lists it
    assert mcp_library.remove_entry(second, "calc") is True
    swarm4, forge4 = await forges(second, "p4")
    assert forge4.get("calc") is None
