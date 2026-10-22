"""Skill folders, sources and the shared library (skills.py; docs/SPEC.md §19). No network: Git sources are local
repositories cloned through file:// URLs."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import make_skill
from dreamteam import skills
from dreamteam.config import Config, render_template
from dreamteam.skills import Library, Limits, SkillError
from dreamteam.workspace import init_repo, run_git

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
LIMITS = Limits()


def can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "probe-target").write_text("x", encoding="utf-8")
        os.symlink(tmp_path / "probe-target", tmp_path / "probe-link")
        return True
    except (OSError, NotImplementedError):
        return False


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


@pytest.fixture(autouse=True)
def _fast_renames(monkeypatch):
    monkeypatch.setattr(skills, "RENAME_PAUSE", 0.0)


# ---------------------------------------------------------------------------------------------- front matter
def test_frontmatter_values():
    text = ("\ufeff\n---\r\nname: pdf-tools   # a comment\r\ndescription: 'It''s for PDFs'\r\n"
            'title: "a \\"b\\" \\\\ c\\nd\\te"\r\nlicense: MIT\r\n---\r\nbody\r\n')
    meta = skills.parse_frontmatter(text)
    assert meta["name"] == "pdf-tools"
    assert meta["description"] == "It's for PDFs"
    assert meta["title"] == 'a "b" \\ c\nd\te'
    assert meta["license"] == "MIT"


def test_frontmatter_block_scalars_and_nested():
    text = ("---\nname: x\ndescription: |\n  line one\n  line two\nsummary: >-\n  folded\n  text\n\n  next\n"
            "metadata:\n  owner: me\n  tags:\n    - a\nallowed-tools:\n  - Bash\nplain:\n  continued\n  here\n"
            "disable-model-invocation: true\n---\n")
    meta = skills.parse_frontmatter(text)
    assert meta["description"] == "line one\nline two"
    assert meta["summary"] == "folded text\nnext"
    assert meta["metadata"] == "" and meta["allowed-tools"] == ""
    assert "owner" not in meta and "tags" not in meta
    assert meta["plain"] == "continued here"
    assert meta["disable-model-invocation"] == "true"


@pytest.mark.parametrize("text", ["no front matter\n", "", "name: x\n---\n", "---\nname: x\n" + "a: b\n" * 300])
def test_frontmatter_missing_or_unclosed(text):
    with pytest.raises(SkillError):
        skills.parse_frontmatter(text)


def test_frontmatter_unclosed_quote():
    with pytest.raises(SkillError, match="closing"):
        skills.parse_frontmatter('---\ndescription: "open\n---\n')


# ---------------------------------------------------------------------------------------------- names
@pytest.mark.parametrize("name", ["..", ".", "..\\x", "a/b", "..\\..\\sessions", "a" * 65, "", "A", "a--b", "-a",
                                  "a-", "a b", None, 3])
def test_check_name_refuses_before_any_file_system_call(name, monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("the file system was reached")

    monkeypatch.setattr(os, "scandir", boom)
    with pytest.raises(SkillError) as info:
        skills.check_name(name)
    assert info.value.status == 400
    lib = Library(tmp_path / "lib")
    for call in (lib.get, lib.show, lib.path, lib.remove):
        with pytest.raises(SkillError):
            call(name)
    with pytest.raises(SkillError):
        skills.child(tmp_path, name)


def test_check_name_accepts_claude_and_anthropic():
    assert skills.check_name("claude-helper") == "claude-helper"
    assert skills.check_name("anthropic-docs") == "anthropic-docs"
    assert skills.check_name("a" * 64)


def test_child_refuses_a_link_out(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "elsewhere").mkdir()
    if not dir_link(tmp_path / "elsewhere", tmp_path / "root" / "evil"):
        pytest.skip("cannot make links here")
    with pytest.raises(SkillError):
        skills.child(tmp_path / "root", "evil")


def test_slug():
    assert skills.slug("My Skill") == "my-skill"
    assert skills.slug("  PDF -> Text!! ") == "pdf-text"
    assert skills.slug("Отчёт по данным") == "otchet-po-dannym"
    assert skills.slug("Café") == "cafe"
    assert skills.slug("???") == ""
    assert len(skills.slug("x" * 100)) == 64


def test_for_agents_replaces_angle_brackets():
    assert skills.for_agents("a -> b, < 10 MB, <tag>") == "a -\u203a b, \u2039 10 MB, \u2039tag\u203a"


# ---------------------------------------------------------------------------------------------- references
def test_parse_ref():
    assert skills.parse_ref("pdf") == (None, "pdf")
    assert skills.parse_ref(" library:pdf ") == ("library", "pdf")
    assert skills.parse_ref("claude-user:pdf") == ("claude-user", "pdf")
    assert skills.parse_ref("claude-project:pdf") == ("claude-project", "pdf")
    assert skills.parse_ref("host-library:pdf") == ("host-library", "pdf")
    for text in ("git@host:x", "C:\\x", "C:/x", "https://h/x", "/abs/path", "./pdf", "pdf.zip"):
        assert not skills.looks_like_ref(text)
        with pytest.raises(SkillError):
            skills.parse_ref(text)
    with pytest.raises(SkillError, match="Unknown skill source"):
        skills.parse_ref("github:pdf")
    with pytest.raises(SkillError, match="Invalid skill name"):
        skills.parse_ref("library:..")


# ---------------------------------------------------------------------------------------------- check_skill_dir
def test_check_skill_dir_basic(tmp_path):
    folder = make_skill(tmp_path / "pdf", "pdf", "Fill PDF forms -> text, files > 10 MB", {
        "scripts/run.py": "print(1)\n", "img.png": b"\x89PNG\x00\x01binary"})
    info = skills.check_skill_dir(folder, LIMITS)
    assert info.name == "pdf" and info.files == 3 and not info.manual
    assert info.description == "Fill PDF forms -> text, files > 10 MB"  # kept; replaced only in agent texts
    assert info.size == sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    assert sorted(r for r, *_ in info.manifest) == ["SKILL.md", "img.png", "scripts/run.py"]
    assert len(info.digest) == 64
    # stable, independent of file creation order, sensitive to content and names
    other = make_skill(tmp_path / "other" / "pdf", "pdf", "Fill PDF forms -> text, files > 10 MB",
                       {"img.png": b"\x89PNG\x00\x01binary", "scripts/run.py": "print(1)\n"})
    assert skills.check_skill_dir(other, LIMITS).digest == info.digest
    (other / "img.png").write_bytes(b"changed")
    assert skills.check_skill_dir(other, LIMITS).digest != info.digest


def test_check_skill_dir_names_and_descriptions(tmp_path):
    lax = make_skill(tmp_path / "a", "My Skill", "Spaces   and\n  more")
    info = skills.check_skill_dir(lax, LIMITS)
    assert info.name == "my-skill" and info.renamed_from == "My Skill"
    with pytest.raises(SkillError, match="not valid"):
        skills.check_skill_dir(lax, LIMITS, strict=True)
    tag = make_skill(tmp_path / "b", "b", "Use <script> here")
    assert skills.check_skill_dir(tag, LIMITS).description == "Use <script> here"
    with pytest.raises(SkillError, match="tag-like"):
        skills.check_skill_dir(tag, LIMITS, strict=True)
    assert skills.check_skill_dir(make_skill(tmp_path / "c", "c", "Below < 10 MB"), LIMITS, strict=True)
    folder_name = make_skill(tmp_path / "from-folder", None, "No name line")
    assert skills.check_skill_dir(folder_name, LIMITS).name == "from-folder"
    manual = make_skill(tmp_path / "m", "m", "Manual", extra="disable-model-invocation: true\n")
    assert skills.check_skill_dir(manual, LIMITS).manual
    with pytest.raises(SkillError, match="description"):
        skills.check_skill_dir(make_skill(tmp_path / "d", "d", ""), LIMITS)
    with pytest.raises(SkillError, match="1024"):
        skills.check_skill_dir(make_skill(tmp_path / "e", "e", "x" * 1025), LIMITS)
    with pytest.raises(SkillError, match="letters or digits"):
        skills.check_skill_dir(make_skill(tmp_path / "f", "???", "desc"), LIMITS)


def test_check_skill_dir_skill_md_rules(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SkillError, match="No SKILL.md"):
        skills.check_skill_dir(empty, LIMITS)
    big = tmp_path / "big"
    big.mkdir()
    (big / "SKILL.md").write_text("---\nname: big\ndescription: d\n---\n" + "x" * 100_001, encoding="utf-8")
    with pytest.raises(SkillError) as info:
        skills.check_skill_dir(big, LIMITS)
    assert info.value.status == 413
    latin = tmp_path / "latin"
    latin.mkdir()
    (latin / "SKILL.md").write_bytes("---\nname: l\ndescription: caf\xe9\n---\n".encode("latin-1"))
    with pytest.raises(SkillError, match="UTF-8"):
        skills.check_skill_dir(latin, LIMITS)
    bom = tmp_path / "bom"
    bom.mkdir()
    (bom / "SKILL.md").write_bytes("\ufeff---\r\nname: bom\r\ndescription: with BOM\r\n---\r\n".encode("utf-8"))
    assert skills.check_skill_dir(bom, LIMITS).name == "bom"


def test_lowercase_skill_md_only_where_the_file_system_folds_case(tmp_path):
    folder = tmp_path / "low"
    folder.mkdir()
    (folder / "skill.md").write_text("---\nname: low\ndescription: d\n---\n", encoding="utf-8")
    insensitive = (folder / "SKILL.md").exists()
    if insensitive:
        info = skills.check_skill_dir(folder, LIMITS)
        assert [r for r, *_ in info.manifest] == ["SKILL.md"]
        copied = skills.copy_skill(folder, tmp_path / "out", None, LIMITS)
        assert "SKILL.md" in os.listdir(copied.path)
    else:
        with pytest.raises(SkillError, match="exactly SKILL.md"):
            skills.check_skill_dir(folder, LIMITS)


def test_check_skill_dir_caps(tmp_path):
    many = make_skill(tmp_path / "many", "many", "d", {f"f{i}.txt": "x" for i in range(5)})
    with pytest.raises(SkillError) as info:
        skills.check_skill_dir(many, Limits(max_files=3))
    assert info.value.status == 413
    large = make_skill(tmp_path / "large", "large", "d", {"blob.bin": b"\0" * (1024 * 1024 + 1)})
    with pytest.raises(SkillError) as info:
        skills.check_skill_dir(large, Limits(max_mb=1))
    assert info.value.status == 413
    deep = make_skill(tmp_path / "deep", "deep", "d", {"/".join("abcdefghi") + "/x.txt": "x"})
    with pytest.raises(SkillError) as info:
        skills.check_skill_dir(deep, LIMITS)
    assert info.value.status == 413
    ok = make_skill(tmp_path / "ok", "ok", "d", {"/".join("abcdefgh") + "/x.txt": "x"})
    assert skills.check_skill_dir(ok, LIMITS).files == 2


def test_check_skill_dir_skips_noise_and_metadata(tmp_path):
    folder = make_skill(tmp_path / "s", "s", "d", {
        ".git/config": "x", "node_modules/a.js": "x", "__pycache__/a.pyc": "x", ".DS_Store": "x",
        "__MACOSX/x": "x", ".dreamteam.json": "{}", "keep.txt": "k"})
    info = skills.check_skill_dir(folder, LIMITS)
    assert sorted(r for r, *_ in info.manifest) == ["SKILL.md", "keep.txt"]


def test_check_skill_dir_links_skipped_and_reported(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    folder = make_skill(tmp_path / "s", "s", "d")
    if not dir_link(outside, folder / "linked"):
        pytest.skip("cannot make links here")
    info = skills.check_skill_dir(folder, LIMITS)
    assert info.skipped == [{"path": "linked", "error": "a link (not copied)"}]
    copied = skills.copy_skill(folder, tmp_path / "out", None, LIMITS)
    assert not os.path.lexists(copied.path / "linked")
    # a skill folder that is itself a link is not one
    if dir_link(folder, tmp_path / "alias"):
        with pytest.raises(SkillError, match="not a folder"):
            skills.check_skill_dir(tmp_path / "alias", LIMITS)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot create these names")
@pytest.mark.parametrize("bad", ["a:b.txt", "con.txt", "trail.", "back\\slash"])
def test_check_skill_dir_portable_names(tmp_path, bad):
    folder = make_skill(tmp_path / "s", "s", "d", {bad: "x"})
    with pytest.raises(SkillError):
        skills.check_skill_dir(folder, LIMITS)


@pytest.mark.parametrize("bad", ["a:b.txt", "con.txt", "NUL", "com1.log", "trail.", "space ", "back\\slash",
                                 "q?.md", "ctl\x01", "del\x7f"])
def test_portable_name_rule_on_every_os(bad):
    with pytest.raises(SkillError):
        skills._portable(bad, f"x/{bad}")
    skills._portable("fine-name.v2.txt", "x/fine-name.v2.txt")


def test_check_skill_dir_collisions(tmp_path):
    folder = make_skill(tmp_path / "s", "s", "d")
    (folder / "Scripts").mkdir()
    (folder / "Scripts" / "x").write_text("1", encoding="utf-8")
    try:
        (folder / "scripts").mkdir()
    except FileExistsError:
        pytest.skip("case-insensitive file system")
    (folder / "scripts" / "x").write_text("2", encoding="utf-8")
    with pytest.raises(SkillError, match="Scripts.*scripts|scripts.*Scripts"):
        skills.check_skill_dir(folder, LIMITS)


def test_check_skill_dir_unicode_collision(tmp_path):
    folder = make_skill(tmp_path / "s", "s", "d")
    nfc, nfd = "caf\u00e9.txt", "cafe\u0301.txt"
    (folder / nfc).write_text("1", encoding="utf-8")
    (folder / nfd).write_text("2", encoding="utf-8")
    if len([n for n in os.listdir(folder) if n.endswith(".txt")]) < 2:
        pytest.skip("the file system normalizes Unicode names")
    with pytest.raises(SkillError, match="Unicode"):
        skills.check_skill_dir(folder, LIMITS)


# ---------------------------------------------------------------------------------------------- copy_skill
def test_copy_skill_and_atomic_replace(tmp_path):
    src = make_skill(tmp_path / "src", "demo", "First", {"a.txt": "a"})
    dest = tmp_path / "lib"
    first = skills.copy_skill(src, dest, None, LIMITS)
    assert first.path == dest / "demo" and (dest / "demo" / "a.txt").read_text(encoding="utf-8") == "a"
    src2 = make_skill(tmp_path / "src2", "demo", "Second", {"b.txt": "b"})
    second = skills.copy_skill(src2, dest, None, LIMITS)
    assert second.digest != first.digest
    assert not (dest / "demo" / "a.txt").exists() and (dest / "demo" / "b.txt").exists()
    assert sorted(os.listdir(dest)) == ["demo"]  # no .t-* / .o-* left


def test_copy_skill_failed_swap_restores_the_old_copy(tmp_path, monkeypatch):
    dest = tmp_path / "lib"
    skills.copy_skill(make_skill(tmp_path / "src", "demo", "Old", {"old.txt": "o"}), dest, None, LIMITS)
    real = skills._rename

    def fail_second(src, dst):
        if Path(src).name.startswith(".t-"):
            raise PermissionError("held by a scanner")
        real(src, dst)

    monkeypatch.setattr(skills, "_rename", fail_second)
    with pytest.raises(PermissionError):
        skills.copy_skill(make_skill(tmp_path / "new", "demo", "New", {"new.txt": "n"}), dest, None, LIMITS)
    assert (dest / "demo" / "old.txt").exists()
    assert sorted(os.listdir(dest)) == ["demo"]


def test_rename_retries_permission_errors(tmp_path, monkeypatch):
    calls = []
    real = os.rename

    def flaky(src, dst):
        calls.append(src)
        if len(calls) <= 2:
            raise PermissionError("busy")
        real(src, dst)

    monkeypatch.setattr(os, "rename", flaky)
    copied = skills.copy_skill(make_skill(tmp_path / "src", "demo", "d"), tmp_path / "lib", None, LIMITS)
    assert copied.path.is_dir() and len(calls) == 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec bits")
def test_copy_skill_keeps_exec_bit(tmp_path):
    src = make_skill(tmp_path / "src", "demo", "d", {"run.sh": "#!/bin/sh\n"})
    os.chmod(src / "run.sh", 0o755)
    copied = skills.copy_skill(src, tmp_path / "lib", None, LIMITS)
    assert os.stat(copied.path / "run.sh").st_mode & 0o111


def test_copy_skill_refuses_a_file_swapped_for_a_link(tmp_path, monkeypatch):
    if not can_symlink(tmp_path):
        pytest.skip("cannot make symlinks here")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    src = make_skill(tmp_path / "src", "demo", "d", {"a.txt": "a"})
    info = skills.check_skill_dir(src, LIMITS)
    os.unlink(src / "a.txt")
    os.symlink(outside, src / "a.txt")  # after the check, before the copy
    with pytest.raises(SkillError, match="changed"):
        skills.copy_skill(src, tmp_path / "lib", None, LIMITS, info=info)
    assert not (tmp_path / "lib" / "demo").exists()


def test_copy_skill_refuses_a_file_that_is_not_the_checked_one(tmp_path, monkeypatch):
    """The opened file must be the one lstat saw (a link swapped in opens another file): on every OS."""
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    src = make_skill(tmp_path / "src", "demo", "d", {"a.txt": "a"})
    real_open = os.open

    def swapped(path, flags, *args):
        if str(path).endswith("a.txt") and not flags & os.O_WRONLY:
            path = str(outside)
        return real_open(path, flags, *args)

    monkeypatch.setattr(skills.os, "open", swapped)
    with pytest.raises(SkillError, match="changed while it was being copied"):
        skills.copy_skill(src, tmp_path / "lib", None, LIMITS)
    assert sorted(os.listdir(tmp_path / "lib")) == []


def test_copy_skill_refuses_content_changed_after_the_check(tmp_path):
    src = make_skill(tmp_path / "src", "demo", "d", {"a.txt": "a"})
    info = skills.check_skill_dir(src, LIMITS)
    (src / "a.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(SkillError, match="changed while it was being copied"):
        skills.copy_skill(src, tmp_path / "lib", None, LIMITS, info=info)
    assert not os.path.exists(tmp_path / "lib" / "demo")


def test_copy_skill_readonly_mirror(tmp_path):
    copied = skills.copy_skill(make_skill(tmp_path / "src", "demo", "d"), tmp_path / "m", None, LIMITS,
                               readonly=True)
    assert not os.access(copied.path / "SKILL.md", os.W_OK)
    skills.rmtree(copied.path)  # the shared rmtree clears read-only files
    assert not copied.path.exists()


def test_clean_stale(tmp_path):
    (tmp_path / ".t-abc123").mkdir()
    (tmp_path / ".o-abc123").mkdir()
    (tmp_path / ".t-fresh1").mkdir()
    named_now = tmp_path / skills._temp_name("o")   # an old mtime (kept by a rename) but made just now
    named_now.mkdir()
    named_long_ago = tmp_path / ".o-abc123-1000"
    named_long_ago.mkdir()
    old = 0
    for path in (tmp_path / ".t-abc123", tmp_path / ".o-abc123", named_now, named_long_ago):
        os.utime(path, (old, old))
    skills.clean_stale(tmp_path)
    assert sorted(os.listdir(tmp_path)) == sorted([".t-fresh1", named_now.name])


# ---------------------------------------------------------------------------------------------- library
def test_library_add_unchanged_conflict_replace_remove(tmp_path):
    lib = Library(tmp_path / "lib")
    src = make_skill(tmp_path / "src", "demo", "First", {"a.txt": "a"})
    entry = lib.add(src, "folder", {"path": str(src)}, by="user", via="home")
    assert entry["unchanged"] is False and entry["origin"] == "folder" and entry["by"] == "user"
    assert entry["via"] == "home" and entry["files"] == 2 and not entry["changed_outside"]
    meta = json.loads((tmp_path / "lib" / "demo" / ".dreamteam.json").read_text(encoding="utf-8"))
    assert meta["digest"] == entry["digest"] and meta["manifest"]
    again = lib.add(src, "folder", {"path": "elsewhere"})
    assert again["unchanged"] is True and again["source"] == {"path": "elsewhere"}
    other = make_skill(tmp_path / "other", "demo", "Second")
    with pytest.raises(SkillError) as info:
        lib.add(other, "zip", {"filename": "x.zip"})
    assert info.value.status == 409 and info.value.extra == {"exists": ["demo"]}
    replaced = lib.add(other, "zip", {"filename": "x.zip"}, replace=True)
    assert replaced["description"] == "Second" and replaced["origin"] == "zip"
    assert replaced["added_at"] == entry["added_at"]
    assert [e["name"] for e in lib.list()] == ["demo"]
    shown = lib.show("demo")
    assert shown["skill_md"].startswith("---") and {"path": "SKILL.md", "size": shown["files_list"][0]["size"]} \
        in shown["files_list"]
    lib.remove("demo")
    assert lib.list() == [] and lib.get("demo") is None
    with pytest.raises(SkillError) as info:
        lib.remove("demo")
    assert info.value.status == 404


def test_library_broken_rebuilt_and_tampered(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.add(make_skill(tmp_path / "src", "good", "Good", {"a.txt": "a"}), "git", {"url": "https://h/r.git"})
    broken = tmp_path / "lib" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no front matter", encoding="utf-8")
    make_skill(tmp_path / "lib" / "manual", "manual", "Dropped in by hand")
    make_skill(tmp_path / "lib" / "Bad Name", "x", "skipped")
    (tmp_path / "lib" / ".t-123456").mkdir()
    entries = {e["name"]: e for e in lib.list()}
    assert sorted(entries) == ["broken", "good", "manual"]
    assert "error" in entries["broken"] and lib.path("broken") is None
    assert entries["manual"]["origin"] == "folder" and (tmp_path / "lib" / "manual" / ".dreamteam.json").exists()
    assert not entries["good"]["changed_outside"]
    (tmp_path / "lib" / "good" / "a.txt").write_text("edited behind DreamTeam's back", encoding="utf-8")
    assert lib.get("good")["changed_outside"] is True
    # dropped in by hand: not added through DreamTeam, so flagged too (and the rebuilt metadata says so)
    manual = lib.get("manual")
    assert manual["changed_outside"] is True and manual["by"] == "" and manual["via"] == "outside"
    # re-importing the source clears the flag (the manifest is written again)
    lib.add(make_skill(tmp_path / "src", "good", "Good", {"a.txt": "a"}), "git", {"url": "https://h/r.git"},
            replace=True)
    assert lib.get("good")["changed_outside"] is False


def test_library_entry_whose_metadata_was_deleted_stays_flagged(tmp_path):
    """Deleting .dreamteam.json and then editing the files must not turn into a clean-looking import."""
    lib = Library(tmp_path / "lib")
    lib.add(make_skill(tmp_path / "src", "good", "Good"), "git", {"url": "https://h/r.git"}, by="user", via="home")
    (tmp_path / "lib" / "good" / ".dreamteam.json").unlink()
    (tmp_path / "lib" / "good" / "SKILL.md").write_text("---\nname: good\ndescription: Rewritten\n---\nEvil.\n",
                                                         encoding="utf-8")
    for _ in range(2):  # the rebuilt metadata is written back, and the flag survives the next read
        entry = lib.get("good")
        assert entry["changed_outside"] is True and entry["origin"] == "folder"
        assert (entry["by"], entry["via"]) == ("", "outside")
    # the user imports the same content again: they vouch for it, the flag is cleared and they are recorded
    again = lib.add(tmp_path / "lib" / "good", "folder", {"path": "x"}, by="user", via="cli")
    assert again["unchanged"] is True and again["changed_outside"] is False
    assert (again["by"], again["via"]) == ("user", "cli")
    assert lib.get("good")["changed_outside"] is False


def test_library_link_entry_is_listed_and_removed_alone(tmp_path):
    """A link in the library (a skill being developed elsewhere, or a planted one) never breaks the listing and is
    never followed: remove deletes the link only, and a replace swaps it out."""
    lib = Library(tmp_path / "lib")
    lib.add(make_skill(tmp_path / "src", "good", "Good"), "folder", {})
    outside = make_skill(tmp_path / "outside" / "evil", "evil", "Lives elsewhere", {"keep.txt": "k"})
    if not dir_link(outside, tmp_path / "lib" / "evil"):
        pytest.skip("cannot make links here")
    entries = {e["name"]: e for e in lib.list()}
    assert sorted(entries) == ["evil", "good"] and "link" in entries["evil"]["error"]
    assert lib.path("evil") is None and lib.digest_of("evil") is None
    assert lib.show("evil")["files_list"] == []
    assert not (outside / ".dreamteam.json").exists()  # nothing was written through the link
    with pytest.raises(SkillError) as info:
        lib.add(make_skill(tmp_path / "new", "evil", "Real one"), "folder", {})
    assert info.value.status == 409
    lib.remove("evil")
    assert not os.path.lexists(tmp_path / "lib" / "evil") and (outside / "keep.txt").exists()
    assert [e["name"] for e in lib.list()] == ["good"]
    # a replace over a link: the link is swapped out, its target untouched
    assert dir_link(outside, tmp_path / "lib" / "evil")
    replaced = lib.add(make_skill(tmp_path / "new", "evil", "Real one"), "folder", {}, replace=True)
    assert replaced["description"] == "Real one" and not skills.is_link(tmp_path / "lib" / "evil")
    assert sorted(os.listdir(outside)) == ["SKILL.md", "keep.txt"]


def test_failed_swap_keeps_an_old_copy_that_a_listing_sees_mid_swap(tmp_path, monkeypatch):
    """A rename keeps the old folder's mtime: a library listing between the two renames must not take the `.o-`
    copy for a stale leftover, or a failed swap has nothing to restore."""
    lib = Library(tmp_path / "lib")
    lib.add(make_skill(tmp_path / "src", "demo", "Old", {"old.txt": "o"}), "folder", {})
    old = 0
    for path in (tmp_path / "lib" / "demo", tmp_path / "lib" / "demo" / "old.txt"):
        os.utime(path, (old, old))
    real = skills._rename

    def list_then_fail(src, dst):
        if Path(src).name.startswith(".t-") and Path(dst).name == "demo":
            Library(tmp_path / "lib").list()   # another request, the UI polling
            raise PermissionError("held by a scanner")
        real(src, dst)

    monkeypatch.setattr(skills, "_rename", list_then_fail)
    with pytest.raises(PermissionError):
        lib.add(make_skill(tmp_path / "new", "demo", "New"), "folder", {}, replace=True)
    monkeypatch.setattr(skills, "_rename", real)
    assert (tmp_path / "lib" / "demo" / "old.txt").exists() and lib.get("demo")["description"] == "Old"


def test_library_read_only(tmp_path):
    root = tmp_path / "host"
    Library(root).add(make_skill(tmp_path / "src", "demo", "d"), "folder", {})
    host = Library(root, read_only=True)
    assert [e["name"] for e in host.list()] == ["demo"]
    for call in (lambda: host.add(tmp_path / "src", "folder", {}), lambda: host.remove("demo")):
        with pytest.raises(SkillError) as info:
            call()
        assert info.value.status == 403


def test_library_from_config_uses_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / "envlib"))
    config = Config.from_dict(tmp_path, {"skills": {"library_dir": str(tmp_path / "ignored")}})
    assert config.skills_library_dir == tmp_path / "envlib"
    assert Library.from_config(config).root == tmp_path / "envlib"
    monkeypatch.delenv("DREAMTEAM_SKILLS_DIR")
    assert Config.from_dict(tmp_path, {"skills": {"library_dir": str(tmp_path / "own")}}).skills_library_dir == \
        tmp_path / "own"


# ---------------------------------------------------------------------------------------------- zip
def zip_bytes(members: dict[str, bytes | str], *, mode: dict[str, int] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mode and name in mode:
                info.external_attr = mode[name] << 16
            zf.writestr(info, data)
    return buf.getvalue()


SKILL_TEXT = "---\nname: {name}\ndescription: The {name} skill\n---\nDo it.\n"


@pytest.mark.parametrize("bad", ["../x/SKILL.md", "/abs/SKILL.md", "C:/x/SKILL.md", "a/../../x", "a\\..\\..\\x",
                                 "//host/share/x"])
def test_extract_zip_refuses_escapes(tmp_path, bad):
    with pytest.raises(SkillError):
        skills.extract_zip(zip_bytes({bad: "x"}), tmp_path / "out", LIMITS)
    assert not (tmp_path / "x").exists()


def test_extract_zip_refuses_duplicates_collisions_links_and_encryption(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "1")
        with pytest.warns(UserWarning):
            zf.writestr("a.txt", "2")
    with pytest.raises(SkillError, match="same file twice"):
        skills.extract_zip(buf.getvalue(), tmp_path / "o1", LIMITS)
    with pytest.raises(SkillError, match="letter case"):
        skills.extract_zip(zip_bytes({"A/x": "1", "a/x": "2"}), tmp_path / "o2", LIMITS)
    with pytest.raises(SkillError, match="file and a folder"):
        skills.extract_zip(zip_bytes({"a": "1", "a/x": "2"}), tmp_path / "o3", LIMITS)
    with pytest.raises(SkillError, match="link"):
        skills.extract_zip(zip_bytes({"l": "target"}, mode={"l": 0o120777}), tmp_path / "o4", LIMITS)
    data = bytearray(zip_bytes({"e.txt": "x"}))
    # the general purpose flag of the local header and of the central directory entry: "encrypted"
    data[6] |= 1
    central = data.rfind(b"PK\x01\x02")
    data[central + 8] |= 1
    with pytest.raises(SkillError, match="Encrypted"):
        skills.extract_zip(bytes(data), tmp_path / "o5", LIMITS)
    with pytest.raises(SkillError, match="not a zip"):
        skills.extract_zip(b"not a zip at all", tmp_path / "o6", LIMITS)


def test_extract_zip_bomb_and_forged_size(tmp_path):
    with pytest.raises(SkillError, match="zip bomb") as info:
        skills.extract_zip(zip_bytes({"zeros.bin": b"\0" * (3 * 2 ** 20)}), tmp_path / "o1", LIMITS)
    assert info.value.status == 413
    # a member that claims to be tiny: the declared sizes pass, the bytes do not
    data = bytearray(zip_bytes({"big.bin": os.urandom(2 * 2 ** 20)}))
    central = data.rfind(b"PK\x01\x02")
    data[central + 24:central + 28] = (10).to_bytes(4, "little")      # uncompressed size in the central directory
    with pytest.raises(SkillError):
        skills.extract_zip(bytes(data), tmp_path / "o2", Limits(max_archive_mb=1))
    with pytest.raises(SkillError) as info:
        skills.extract_zip(zip_bytes({"big.bin": os.urandom(2 * 2 ** 20)}), tmp_path / "o3", Limits(max_archive_mb=1))
    assert info.value.status == 413


def test_extract_zip_skips_and_exec_bit(tmp_path):
    data = zip_bytes({"s/SKILL.md": SKILL_TEXT.format(name="s"), "__MACOSX/s/._SKILL.md": "junk",
                      "s/.DS_Store": "junk", "s/.dreamteam.json": "{}", "s/run.sh": "#!/bin/sh\n", "s/sub/": ""},
                     mode={"s/run.sh": 0o100755})
    skipped = skills.extract_zip(data, tmp_path / "out", LIMITS)
    assert {s["path"] for s in skipped} == {"__MACOSX/s/._SKILL.md", "s/.DS_Store", "s/.dreamteam.json"}
    assert sorted(p.name for p in (tmp_path / "out" / "s").iterdir()) == ["SKILL.md", "run.sh"]
    if os.name != "nt":
        assert os.stat(tmp_path / "out" / "s" / "run.sh").st_mode & 0o111


def test_import_zip_layouts_and_caps(tmp_path):
    lib = Library(tmp_path / "lib", Limits(max_mb=1, max_archive_mb=4))
    root_layout = zip_bytes({"SKILL.md": SKILL_TEXT.format(name="rooted"), "a.txt": "a"})
    report = skills.import_source(lib, {"zip": base64.b64encode(root_layout).decode(), "filename": "r.zip"})
    assert [e["name"] for e in report["imported"]] == ["rooted"] and report["imported"][0]["origin"] == "zip"
    assert report["imported"][0]["source"] == {"filename": "r.zip"}
    folder_layout = zip_bytes({"top/SKILL.md": SKILL_TEXT.format(name="topped")})
    report = skills.import_source(lib, {"zip": base64.b64encode(folder_layout).decode()})
    assert report["imported"][0]["name"] == "topped"
    # a "Download ZIP" of a repository: 2 MB of other files, a small skill picked from it
    repo = zip_bytes({"repo/big.bin": os.urandom(2 * 2 ** 20), "repo/skills/small/SKILL.md":
                      SKILL_TEXT.format(name="small"), "repo/skills/huge/SKILL.md": SKILL_TEXT.format(name="huge"),
                      "repo/skills/huge/data.bin": os.urandom(1024 * 1024 + 10)})
    report = skills.import_source(lib, {"zip": base64.b64encode(repo).decode()}, pick=["small"])
    assert [e["name"] for e in report["imported"]] == ["small"]
    assert report["imported"][0]["source"] == {"filename": "skills.zip", "subdir": "repo/skills/small"}
    with pytest.raises(SkillError) as info:
        skills.import_source(lib, {"zip": base64.b64encode(repo).decode()}, pick=["huge"])
    assert info.value.status == 413
    with pytest.raises(SkillError) as info:
        skills.import_source(Library(tmp_path / "l2", Limits(max_archive_mb=1)),
                             {"zip": "A" * (Limits(max_archive_mb=1).zip_b64_max + 4)})
    assert info.value.status == 413
    with pytest.raises(SkillError, match="base64"):
        skills.import_source(lib, {"zip": "not base64!!"})
    assert sorted(os.listdir(tmp_path / "lib")) == ["rooted", "small", "topped"]  # no staging left


def test_import_root_zip_without_a_name_uses_the_file_name(tmp_path):
    """A root-layout zip whose SKILL.md has no name: the zip's file name, never the staging folder's."""
    lib = Library(tmp_path / "lib")
    nameless = zip_bytes({"SKILL.md": "---\ndescription: no name here\n---\nDo it.\n"})
    report = skills.import_source(lib, {"zip": base64.b64encode(nameless).decode(), "filename": "PDF Tools.zip"})
    assert [e["name"] for e in report["imported"]] == ["pdf-tools"]
    archive = tmp_path / "csv-cleanup.zip"
    archive.write_bytes(nameless)
    assert [e["name"] for e in skills.import_source(lib, {"path": str(archive)})["imported"]] == ["csv-cleanup"]
    # inside the zip a skill folder keeps its own name
    folder = zip_bytes({"inner-one/SKILL.md": "---\ndescription: no name here\n---\n"})
    report = skills.import_source(lib, {"zip": base64.b64encode(folder).decode(), "filename": "outer.zip"})
    assert [e["name"] for e in report["imported"]] == ["inner-one"]


# ---------------------------------------------------------------------------------------------- find / stage
def test_find_skills_pick_and_duplicates(tmp_path):
    src = tmp_path / "src"
    make_skill(src / "skills" / "one", "one", "First")
    make_skill(src / "skills" / "two", "two", "Second")
    make_skill(src / "other" / "two-again", "two", "Duplicate")
    make_skill(src / "node_modules" / "hidden", "hidden", "never found")
    make_skill(src / "a" / "b" / "c" / "d" / "e", "too-deep", "below depth 4")
    lib = Library(tmp_path / "lib")
    report = skills.import_source(lib, {"path": str(src)})
    assert report["need_pick"] is True and report["imported"] == [] and lib.list() == []
    by_path = {c["path"]: c for c in report["candidates"]}
    assert sorted(by_path) == ["other/two-again", "skills/one", "skills/two"]
    assert "duplicate" in by_path["skills/two"].get("error", "") + by_path["other/two-again"].get("error", "")
    report = skills.import_source(lib, {"path": str(src)}, pick=["one"])
    assert [e["name"] for e in report["imported"]] == ["one"]
    assert report["imported"][0]["source"] == {"path": str((src / "skills" / "one").resolve())}
    with pytest.raises(SkillError, match="No skill 'zzz'"):
        skills.import_source(lib, {"path": str(src)}, pick=["zzz"])
    (tmp_path / "empty").mkdir()
    with pytest.raises(SkillError, match="No SKILL.md"):
        skills.import_source(lib, {"path": str(tmp_path / "empty")})


def test_stage_source_errors_and_discard(tmp_path):
    lib = Library(tmp_path / "lib")
    with pytest.raises(SkillError, match="absolute"):
        skills.stage_source(lib, {"path": "relative/folder"})
    with pytest.raises(SkillError, match="No folder"):
        skills.stage_source(lib, {"path": str(tmp_path / "missing")})
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no front matter", encoding="utf-8")
    with pytest.raises(SkillError, match="front matter"):
        skills.stage_source(lib, {"path": str(bad)})
    src = make_skill(tmp_path / "src", "demo", "d")
    with skills.stage_source(lib, {"path": str(src)}) as staged:
        assert [c.info.name for c in staged.picked] == ["demo"] and staged.conflicts() == []
        assert lib.list() == []
    assert lib.list() == [] and os.listdir(tmp_path / "lib") == []
    # a folder zip on disk is a zip source
    archive = tmp_path / "x.zip"
    archive.write_bytes(zip_bytes({"SKILL.md": SKILL_TEXT.format(name="zipped")}))
    report = skills.import_source(lib, {"path": str(archive)})
    assert report["imported"][0]["origin"] == "zip" and report["imported"][0]["source"] == {"filename": "x.zip"}


def test_staged_commit_checks_every_clash_first(tmp_path):
    lib = Library(tmp_path / "lib")
    lib.add(make_skill(tmp_path / "old", "two", "Old two"), "folder", {})
    src = tmp_path / "src"
    make_skill(src / "one", "one", "One")
    make_skill(src / "two", "two", "New two")
    staged = skills.stage_source(lib, {"path": str(src)}, pick=["one", "two"])
    assert staged.conflicts() == ["two"]
    with pytest.raises(SkillError) as info:
        staged.commit()
    assert info.value.status == 409 and info.value.extra["exists"] == ["two"]
    assert [e["name"] for e in lib.list()] == ["two"]  # "one" was not added either
    report = skills.import_source(lib, {"path": str(src)}, pick=["one", "two"], replace=True)
    assert [e["description"] for e in report["imported"]] == ["One", "New two"]


def test_import_request_shapes(tmp_path):
    config = Config.from_dict(tmp_path, {})
    for body, text in (({}, "exactly one"), ({"path": "/a", "git": "https://h/r"}, "exactly one"),
                       ({"path": "/a", "pick": "x"}, "'pick'"), ({"path": "/a", "replace": "yes"}, "'replace'"),
                       ([], "object")):
        with pytest.raises(SkillError, match=text) as info:
            skills.import_request(config, body)
        assert info.value.status == 400
    src = make_skill(tmp_path / "src", "demo", "d")
    report = skills.import_request(config, {"path": str(src)}, via="home")
    assert report["imported"][0]["via"] == "home"
    assert Library.from_config(config).get("demo") is not None


class FakeRequest:
    def __init__(self, body: bytes, content_type: str = "application/json", length: bool = True):
        self.headers = {"content-type": content_type, **({"content-length": str(len(body))} if length else {})}
        self._body = body

    async def stream(self):
        for i in range(0, len(self._body), 1000):
            yield self._body[i:i + 1000]


async def test_read_import_body(tmp_path):
    config = Config.from_dict(tmp_path, {"skills": {"max_archive_mb": 1}})
    assert await skills.read_import_body(FakeRequest(b'{"path": "/x"}'), config) == {"path": "/x"}
    big = b'{"zip": "' + b"A" * (skills.body_limit(config) + 1) + b'"}'
    for length in (True, False):
        with pytest.raises(SkillError) as info:
            await skills.read_import_body(FakeRequest(big, length=length), config)
        assert info.value.status == 413
    with pytest.raises(SkillError) as info:
        await skills.read_import_body(FakeRequest(b"x", "text/plain"), config)
    assert info.value.status == 415
    with pytest.raises(SkillError, match="object"):
        await skills.read_import_body(FakeRequest(b"[1]"), config)


# ---------------------------------------------------------------------------------------------- Git URLs
@pytest.mark.parametrize("text, url, ref, subdir, shown", [
    ("https://github.com/o/r/tree/main/skills/pdf", "https://github.com/o/r.git", "main", "skills/pdf",
     "https://github.com/o/r.git"),
    ("https://github.com/o/r/blob/v1/skills/pdf/SKILL.md", "https://github.com/o/r.git", "v1", "skills/pdf",
     "https://github.com/o/r.git"),
    ("https://github.com/o/r/tree/main", "https://github.com/o/r.git", "main", None, "https://github.com/o/r.git"),
    ("https://gitlab.com/g/sub/r/-/tree/dev/a/b", "https://gitlab.com/g/sub/r.git", "dev", "a/b",
     "https://gitlab.com/g/sub/r.git"),
    ("https://git.example.org/team/r/-/blob/dev/a/SKILL.md", "https://git.example.org/team/r.git", "dev", "a",
     "https://git.example.org/team/r.git"),
    ("https://host/x/r.git#feature/x:skills/a", "https://host/x/r.git", "feature/x", "skills/a",
     "https://host/x/r.git"),
    ("https://host/r.git#v2", "https://host/r.git", "v2", None, "https://host/r.git"),
    ("https://host/r.git#:skills/a/", "https://host/r.git", None, "skills/a", "https://host/r.git"),
    ("ssh://git@host:2222/o/r.git", "ssh://git@host:2222/o/r.git", None, None, "ssh://git@host:2222/o/r.git"),
    ("git@github.com:o/r.git#main:skills", "git@github.com:o/r.git", "main", "skills", "git@github.com:o/r.git"),
    ("file:///C:/repos/r", "file:///C:/repos/r", None, None, "file:///C:/repos/r"),
    ("https://u:ghp_x@github.com/o/r?token=1", "https://u:ghp_x@github.com/o/r?token=1", None, None,
     "https://github.com/o/r"),
])
def test_parse_git_source(text, url, ref, subdir, shown):
    src = skills.parse_git_source(text)
    assert (src.url, src.ref, src.subdir, src.shown) == (url, ref, subdir, shown)
    assert "ghp_x" not in repr(src)


@pytest.mark.parametrize("text", [
    "http://github.com/o/r", "ext::sh -c touch% /tmp/pwned", "fd::17", "-oProxyCommand=x@host:p",
    "-u@host:p", "user@-host:p", "https://github.com/o/r with space", "https://-host/r", "https://host/r#main:../x",
    "https://host/r#main:a\\b", "https://host/r#main:C:x", "https://host/r#main:/abs", "https://host/r#-x",
    "https://host/r#a..b", "https://host/r#x@{1}", "https://host/r#x.lock", "https://host/r#x/", "not a url",
    "host:path", "", "ftp://host/r", "ssh://-user@host/r",
])
def test_parse_git_source_refusals(text):
    with pytest.raises(SkillError):
        skills.parse_git_source(text)


def test_redact():
    src = skills.parse_git_source("https://u:ghp_secret123@example.com/o/r.git")
    text = ("fatal: 'u:ghp_secret123@example.com/o/r.git' does not appear to be a git repository\n"
            "fatal: unable to access 'https://u:ghp_secret123@example.com/o/r.git/': 403")
    redacted = skills.redact(text, src)
    assert "ghp_secret123" not in redacted and "u:" not in redacted
    assert "https://example.com/o/r.git" in redacted
    assert skills.redact("see https://a:b@other.org/x", src) == "see https://other.org/x"


# ---------------------------------------------------------------------------------------------- clone
def git_repo(tmp_path: Path, files: dict[str, str | bytes], name: str = "repo") -> Path:
    root = tmp_path / name
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    assert init_repo(root) is None
    run_git(root, "config", "uploadpack.allowFilter", "true")  # (the test repository's own config)
    return root


@pytest.fixture
def git_home(tmp_path, monkeypatch):
    """A HOME with its own (empty) git configuration: the user's ~/.gitconfig is never read or written."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"):
        monkeypatch.delenv(name, raising=False)
    return home


@needs_git
def test_import_git_two_skills_and_subdir(tmp_path, git_home):
    repo = git_repo(tmp_path, {"skills/one/SKILL.md": SKILL_TEXT.format(name="one"),
                               "skills/one/scripts/a.py": "print(1)\n",
                               "skills/two/SKILL.md": SKILL_TEXT.format(name="two"), "README.md": "readme\n",
                               "big/other.txt": "not needed\n"})
    lib = Library(tmp_path / "lib")
    url = repo.as_uri()
    report = skills.import_source(lib, {"git": url})
    assert report["need_pick"] and sorted(c["name"] for c in report["candidates"]) == ["one", "two"]
    report = skills.import_source(lib, {"git": url}, pick=["two"])
    entry = report["imported"][0]
    assert entry["origin"] == "git" and entry["source"]["url"] == url and entry["source"]["subdir"] == "skills/two"
    assert len(entry["source"]["commit"]) == 40
    with skills.stage_source(lib, {"git": f"{url}#:skills/one"}) as staged:
        assert [c.info.name for c in staged.picked] == ["one"]
        checkout = staged.dir / "r"
        assert (checkout / "skills" / "one" / "scripts" / "a.py").exists()
        if skills._git_version(shutil.which("git")) >= skills.GIT_PARTIAL:
            assert not (checkout / "README.md").exists() and not (checkout / "big").exists()
    with pytest.raises(SkillError, match="no folder"):
        skills.import_source(lib, {"git": f"{url}#:skills/missing"})
    assert sorted(os.listdir(tmp_path / "lib")) == ["two"]


@needs_git
def test_import_root_git_without_a_name_uses_the_repository_name(tmp_path, git_home):
    repo = git_repo(tmp_path, {"SKILL.md": "---\ndescription: no name here\n---\n",
                               "sub/deep/SKILL.md": "---\ndescription: nameless too\n---\n"}, name="my-skill-repo")
    lib = Library(tmp_path / "lib")
    report = skills.import_source(lib, {"git": repo.as_uri()})
    assert [e["name"] for e in report["imported"]] == ["my-skill-repo"]
    report = skills.import_source(lib, {"git": f"{repo.as_uri()}#:sub/deep"})
    assert [e["name"] for e in report["imported"]] == ["deep"]
    assert skills._repo_name(skills.parse_git_source("git@host:o/pdf-tools.git")) == "pdf-tools"
    assert skills._repo_name(skills.parse_git_source("https://host/o/r.git#main:skills/csv")) == "csv"


@needs_git
def test_clone_protocol_allow_list_holds_through_insteadof(tmp_path, git_home):
    """A user `url.<base>.insteadOf` rewrite cannot move an https source to another transport (file here; ext::
    or fd:: the same way): only https and ssh are allowed, file only for a file:// URL."""
    repo = git_repo(tmp_path, {"SKILL.md": SKILL_TEXT.format(name="redirected")})
    (git_home / ".gitconfig").write_text(f'[url "{repo.as_uri()}"]\n\tinsteadOf = https://example.invalid/r.git\n',
                                         encoding="utf-8")
    lib = Library(tmp_path / "lib")
    with pytest.raises(SkillError, match="not allowed"):
        skills.import_source(lib, {"git": "https://example.invalid/r.git"})
    assert os.listdir(tmp_path / "lib") == [] and lib.list() == []
    # the same repository named by its own file:// URL is allowed
    assert skills.import_source(lib, {"git": repo.as_uri()})["imported"][0]["name"] == "redirected"


@needs_git
def test_clone_watchdog_stops_a_large_repository(tmp_path, git_home):
    repo = git_repo(tmp_path, {"SKILL.md": SKILL_TEXT.format(name="large"), "blob.bin": os.urandom(3 * 2 ** 20)})
    lib = Library(tmp_path / "lib", Limits(max_archive_mb=1))
    with pytest.raises(SkillError, match="max_archive_mb") as info:
        skills.import_source(lib, {"git": repo.as_uri()})
    assert info.value.status == 413
    assert os.listdir(tmp_path / "lib") == []


@needs_git
def test_clone_runs_no_global_hook(tmp_path, git_home):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    for hook in ("post-checkout", "post-merge", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8")
        os.chmod(hooks / hook, 0o755)
    (git_home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks.as_posix()}\n", encoding="utf-8")
    repo = git_repo(tmp_path, {"SKILL.md": SKILL_TEXT.format(name="hooked")})
    marker.unlink(missing_ok=True)  # (the test repository's own initial commit may have run them)
    report = skills.import_source(Library(tmp_path / "lib"), {"git": repo.as_uri()})
    assert report["imported"][0]["name"] == "hooked"
    assert not marker.exists()


@needs_git
def test_clone_keeps_the_users_ssh_command_and_never_prompts(tmp_path, git_home, monkeypatch):
    git = shutil.which("git")
    env = skills.clone_env(git, tmp_path)
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never" and env["GIT_LFS_SKIP_SMUDGE"] == "1"
    (git_home / ".gitconfig").write_text("[core]\n\tsshCommand = ssh -i mykey\n", encoding="utf-8")
    assert "GIT_SSH_COMMAND" not in skills.clone_env(git, tmp_path)
    (git_home / ".gitconfig").unlink()
    record = tmp_path / "ssh-argv.txt"
    fake = tmp_path / "fake_ssh.py"
    fake.write_text("import sys\nopen(sys.argv[1], 'w').write(' '.join(sys.argv[2:]))\nsys.exit(255)\n",
                    encoding="utf-8")
    command = f'"{Path(sys.executable).as_posix()}" "{fake.as_posix()}" "{record.as_posix()}"'
    monkeypatch.setenv("GIT_SSH_COMMAND", command)
    assert skills.clone_env(git, tmp_path)["GIT_SSH_COMMAND"] == command
    with pytest.raises(SkillError, match="Could not clone ssh://git@example.invalid/o/r.git"):
        skills.import_source(Library(tmp_path / "lib"), {"git": "ssh://git@example.invalid/o/r.git"})
    assert "example.invalid" in record.read_text(encoding="utf-8")


@needs_git
def test_clone_error_never_shows_credentials(tmp_path, git_home):
    missing = (tmp_path / "missing-repo").as_posix().lstrip("/")
    with pytest.raises(SkillError) as info:
        skills.import_source(Library(tmp_path / "lib"), {"git": f"file://u:ghp_secret123@localhost/{missing}"})
    assert "ghp_secret123" not in str(info.value) and "Could not clone" in str(info.value)


def test_clone_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(skills.shutil, "which", lambda name: None)
    with pytest.raises(SkillError, match="git is not installed"):
        skills.import_source(Library(tmp_path / "lib"), {"git": "https://example.invalid/o/r.git"})
    assert os.listdir(tmp_path / "lib") == []


# ---------------------------------------------------------------------------------------------- Claude Code
def test_claude_skills_and_badges(tmp_path, monkeypatch):
    claude = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills"
    make_skill(claude / "My Skill", "My Skill", "Turns A -> B (files > 10 MB too)")
    make_skill(claude / "same", "same", "In the library too")
    make_skill(claude / "differs", "differs", "Claude Code's version")
    (claude / "not-a-skill").mkdir(parents=True)
    broken = claude / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("no front matter", encoding="utf-8")
    workspace = tmp_path / "ws"
    make_skill(workspace / ".claude" / "skills" / "proj", "proj", "Project skill")
    config = Config.from_dict(tmp_path, {})
    lib = Library.from_config(config)
    lib.add(claude / "same", "claude-user", {"path": str(claude / "same")})
    lib.add(make_skill(tmp_path / "lib-differs", "differs", "Library's version"), "folder", {})
    items = {(i["origin"], i["name"]): i for i in skills.claude_skills(config, workspace)}
    assert set(items) == {("claude-user", "my-skill"), ("claude-user", "same"), ("claude-user", "differs"),
                          ("claude-user", "broken"), ("claude-project", "proj")}
    mine = items[("claude-user", "my-skill")]
    assert mine["renamed_from"] == "My Skill" and mine["description"].startswith("Turns A -> B")
    assert mine["library"] == "absent" and "error" not in mine
    assert items[("claude-user", "same")]["library"] == "same"
    assert items[("claude-user", "differs")]["library"] == "different"
    assert "error" in items[("claude-user", "broken")]
    assert items[("claude-project", "proj")]["agent_writable"] is True
    assert "agent_writable" not in mine
    assert skills.claude_skills(Config.from_dict(tmp_path, {"skills": {"claude": False}}), workspace) == []
    # "add to library" from Claude Code
    report = skills.import_source(lib, {"claude": "my-skill", "origin": "claude-user"})
    assert report["imported"][0]["origin"] == "claude-user"
    assert report["imported"][0]["source"] == {"path": str(claude / "My Skill")}


def test_claude_skill_links_are_not_followed(tmp_path):
    claude = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills"
    claude.mkdir(parents=True)
    if not dir_link(make_skill(tmp_path / "real", "linked", "d"), claude / "linked"):
        pytest.skip("cannot make links here")
    assert skills.claude_skills(Config.from_dict(tmp_path, {})) == []


def test_resolve_ref(tmp_path, monkeypatch):
    claude = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills"
    make_skill(claude / "both", "both", "Claude Code's")
    make_skill(claude / "only-claude", "only-claude", "d")
    workspace = tmp_path / "ws"
    make_skill(workspace / ".claude" / "skills" / "proj", "proj", "agent-writable")
    config = Config.from_dict(tmp_path, {})
    lib = Library.from_config(config)
    lib.add(make_skill(tmp_path / "src", "both", "Library's"), "folder", {})
    assert skills.resolve_ref(config, "both")["origin"] == "library"
    assert skills.resolve_ref(config, "claude-user:both")["path"] == claude / "both"
    assert skills.resolve_ref(config, "only-claude")["origin"] == "claude-user"
    assert skills.resolve_ref(config, "claude-project:proj", workspace=workspace)["origin"] == "claude-project"
    with pytest.raises(SkillError) as info:  # never implicitly from the agent-writable workspace
        skills.resolve_ref(config, "proj", workspace=workspace)
    assert info.value.status == 404 and "claude-project:proj" in str(info.value) and "Library: both" in str(info.value)
    with pytest.raises(SkillError):
        skills.resolve_ref(Config.from_dict(tmp_path, {"skills": {"claude": False}}), "only-claude")


def test_host_library_in_the_sandbox(tmp_path, monkeypatch):
    host_root = tmp_path / "host-skills"
    Library(host_root).add(make_skill(tmp_path / "src", "shared", "From the host"), "git", {"url": "https://h/r"})
    config = Config.from_dict(tmp_path, {})
    assert skills.host_skills(config) == [] and Library.host(config) is None
    monkeypatch.setenv("DREAMTEAM_HOST_SKILLS_DIR", str(host_root))
    items = skills.host_skills(config)
    assert [(i["name"], i["origin"], i["host_origin"], i["read_only"]) for i in items] == \
        [("shared", "host-library", "git", True)]
    assert skills.resolve_ref(config, "shared")["origin"] == "host-library"
    assert skills.resolve_ref(config, "host-library:shared")["path"] == host_root / "shared"
    report = skills.import_source(Library.from_config(config), {"host": "shared"})
    assert report["imported"][0]["origin"] == "host-library"
    assert skills.resolve_ref(config, "shared")["origin"] == "library"


# ---------------------------------------------------------------------------------------------- config
def test_skills_settings(tmp_path):
    config = Config.from_dict(tmp_path, {})
    assert (config.skills.library_dir, config.skills.claude, config.skills.max_mb, config.skills.max_files,
            config.skills.max_archive_mb, config.skills.max_attached, config.skills.max_proposals) == \
        ("~/.dreamteam/skills", True, 10, 200, 50, 40, 10)
    assert Config.from_dict(tmp_path, {"skills": {"max_proposals": 0}}).skills.max_proposals == 0
    for key in ("max_mb", "max_files", "max_archive_mb", "max_attached"):
        with pytest.raises(ValueError, match=rf"\[skills\] {key} must be >= 1"):
            Config.from_dict(tmp_path, {"skills": {key: 0}})
    with pytest.raises(ValueError, match="Unknown keys for \\[skills\\]"):
        Config.from_dict(tmp_path, {"skills": {"nope": 1}})
    with pytest.raises(ValueError, match="true or false"):
        Config.from_dict(tmp_path, {"skills": {"claude": "yes"}})
    import tomllib

    rendered = Config.from_dict(tmp_path, tomllib.loads(render_template("x")))
    assert rendered.skills.library_dir == "~/.dreamteam/skills" and rendered.skills.max_proposals == 10
    assert Limits.from_config(rendered) == Limits(10, 200, 50)


def test_settings_sections_and_dump_order():
    from dreamteam import settings

    assert settings.SECTIONS.index("skills") == settings.SECTIONS.index("mcp") + 1
    text = settings.dump_toml({"tracing": {"enabled": True}, "skills": {"max_mb": 5}, "mcp": {}})
    assert text.index("[mcp]") < text.index("[skills]") < text.index("[tracing]")
    assert settings.public_view({})["skills"]["max_attached"] == 40


def test_skills_library_is_refused_as_a_session_folder(tmp_path, monkeypatch):
    from dreamteam.sessions import SessionError, SessionManager

    library_dir = tmp_path / "outer" / "skills-lib"
    library_dir.mkdir(parents=True)
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(library_dir))
    manager = SessionManager(tmp_path / "home")
    for target in (library_dir, library_dir / "inside", tmp_path / "outer"):
        with pytest.raises(SessionError, match="skills library"):
            manager._plan(None, str(target), git_on=False)
