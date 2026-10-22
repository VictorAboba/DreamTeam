"""skill_deps.py, the pure part (docs/SPEC.md §19 "Script dependencies"): finding and checking a skill's manifests,
the environment key and folder name, the shell form of a path, redaction, the installers' environment and the tool
lookups. No installer runs here."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_skill
from dreamteam import skill_deps
from dreamteam.config import Config
from dreamteam.skill_deps import (DIRNAME_RE, ToolInfo, check_lock, detect, env_dirname, env_key, installer_env,
                                  python_satisfies, redact, shell_ready, trust_class)

WINDOWS = sys.platform == "win32"


def skill(tmp_path: Path, files: dict[str, str | bytes], name: str = "demo") -> Path:
    return make_skill(tmp_path / name, name, "Does a demo thing.", files)


def unsupported(tmp_path: Path, files: dict[str, str | bytes]) -> str:
    found = detect(skill(tmp_path, files)).python
    assert found is not None and found.unsupported, found
    return found.unsupported


# ---------------------------------------------------------------------------------------------------- detect
def test_detect_finds_each_location_and_unites_the_python_files_in_order(tmp_path):
    folder = skill(tmp_path, {"requirements.txt": "requests>=2\n", "scripts/requirements.txt": "rich\n",
                              "pyproject.toml": '[project]\nname = "x"\ndependencies = ["pypdf==4.0"]\n',
                              "scripts/run.py": "print(1)\n", "SKILL-notes.md": "pip install nothing\n"})
    py = detect(folder).python
    assert py.files == ("requirements.txt", "pyproject.toml", "scripts/requirements.txt")
    assert py.args == ("requirements.txt", "pyproject-deps-1.txt", "scripts/requirements.txt")
    assert py.packages == ("requests", "pypdf", "rich") and py.has_pyproject and not py.unsupported
    assert dict(py.generated)["pyproject-deps-1.txt"] == "pypdf==4.0\n"
    assert [rel for rel, _ in py.material] == ["pyproject.toml#deps", "requirements.txt", "scripts/requirements.txt"] \
        or {rel for rel, _ in py.material} == {"pyproject.toml#deps", "requirements.txt", "scripts/requirements.txt"}
    assert detect(folder).node is None
    for rel in ("scripts/pyproject.toml",):
        alone = skill(tmp_path, {rel: '[project]\ndependencies = ["a"]\n'}, "alone")
        assert detect(alone).python.files == (rel,)


def test_no_manifest_and_empty_lists_mean_none(tmp_path):
    assert detect(skill(tmp_path, {"scripts/run.py": "x"})) == skill_deps.Detected(None, None)
    empty = skill(tmp_path, {"requirements.txt": "# nothing\n\n", "package.json": '{"name": "x"}',
                             "pyproject.toml": '[project]\nname = "x"\n'}, "empty")
    assert detect(empty) == skill_deps.Detected(None, None)


def test_node_detection_root_wins_and_the_lock_is_chosen(tmp_path):
    folder = skill(tmp_path, {"package.json": json.dumps({"dependencies": {"a": "^1"}, "devDependencies": {"d": "1"},
                                                          "scripts": {"postinstall": "evil"}}),
                              "scripts/package.json": json.dumps({"dependencies": {"b": "1"}}),
                              "yarn.lock": "# yarn\n"})
    node = detect(folder).node
    assert node.dir == "" and node.files == ("package.json",) and node.lock is None and node.packages == ("a",)
    assert any("scripts/package.json" in i for i in node.ignored) and any("yarn.lock" in i for i in node.ignored)
    generated = json.loads(node.generated)
    assert generated == {"name": "dreamteam-skill-env", "private": True, "dependencies": {"a": "^1"}}
    lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/a": {"resolved": "https://r.example/a.tgz"}}}
    locked = skill(tmp_path, {"scripts/package.json": json.dumps({"name": "s", "version": "2.0.0",
                                                                  "dependencies": {"a": "1"},
                                                                  "devDependencies": {"d": "1"},
                                                                  "scripts": {"install": "x"}, "bin": "x.js",
                                                                  "volta": {}}),
                              "scripts/package-lock.json": json.dumps(lock),
                              "scripts/npm-shrinkwrap.json": json.dumps(lock)}, "locked")
    node = detect(locked).node
    assert node.dir == "scripts" and node.lock == "npm-shrinkwrap.json"
    assert node.files == ("scripts/package.json", "scripts/npm-shrinkwrap.json")
    assert json.loads(node.generated) == {"name": "s", "version": "2.0.0", "dependencies": {"a": "1"},
                                          "devDependencies": {"d": "1"}}


def test_manifests_that_are_links_oversized_or_not_utf8_are_unsupported(tmp_path):
    assert "larger than" in unsupported(tmp_path / "a", {"requirements.txt": "x\n" * 200_000})
    assert "UTF-8" in unsupported(tmp_path / "b", {"requirements.txt": b"caf\xe9\n"})
    folder = skill(tmp_path / "c", {"real.txt": "requests\n"})
    try:
        os.symlink(folder / "real.txt", folder / "requirements.txt")
    except OSError:
        pytest.skip("symlinks need a privilege here")
    assert "link" in detect(folder).python.unsupported


def test_a_skills_own_node_modules_is_never_seen(tmp_path):
    folder = skill(tmp_path, {"node_modules/x/package.json": '{"dependencies": {"y": "1"}}',
                              ".venv/requirements.txt": "z\n"})
    assert detect(folder) == skill_deps.Detected(None, None)


def test_hashes_are_all_or_nothing(tmp_path):
    good = "a==1.0 --hash=sha256:" + "0" * 64 + "\n" + "b===2 \\\n    --hash=sha256:" + "1" * 64 + "\n"
    py = detect(skill(tmp_path, {"requirements.txt": good})).python
    assert py.hashed and not py.unsupported and py.packages == ("a", "b")
    text = unsupported(tmp_path / "mix", {"requirements.txt": good, "scripts/requirements.txt": "rich\n"})
    assert text == ("requirements.txt uses --hash, but scripts/requirements.txt line 1 for rich has none: pip would "
                    "refuse the mix")
    text = unsupported(tmp_path / "py", {"requirements.txt": good,
                                         "pyproject.toml": '[project]\ndependencies = ["c"]\n'})
    assert text == "requirements.txt uses --hash, but pyproject.toml lists unhashed dependencies"
    text = unsupported(tmp_path / "pin", {"requirements.txt": "a>=1 --hash=sha256:" + "0" * 64 + "\n"})
    assert "line 1 for a has --hash but is not pinned with ==" in text
    assert detect(skill(tmp_path / "rh", {"requirements.txt": "--require-hashes\nb\n"})).python.unsupported


# -------------------------------------------------------------------------------------------- requirements
def test_accepted_requirement_lines(tmp_path):
    folder = skill(tmp_path, {"requirements.txt": (
        "# a comment\nrequests[socks]>=2,<3 ; python_version >= '3.8'  # why\n--pre\n--prefer-binary\n"
        "--only-binary :all:\npkg @ https://example.com/pkg-1.0.tar.gz\n"
        "tool @ git+https://github.com/x/tool.git@v1\nlong-name \\\n  >= 1.0\n-r more.txt\n-c pins/constraints.txt\n"),
        "more.txt": "extra-pkg\n", "pins/constraints.txt": "requests==2.31\n"})
    py = detect(folder).python
    assert not py.unsupported, py.unsupported
    assert py.packages == ("requests", "pkg", "tool", "long-name", "extra-pkg")
    assert py.includes == ("more.txt", "pins/constraints.txt")
    assert {rel for rel, _ in py.material} == {"requirements.txt", "more.txt", "pins/constraints.txt"}
    assert py.copies == ("requirements.txt", "more.txt", "pins/constraints.txt")


@pytest.mark.parametrize("line, text", [
    ("-e .", "requirements.txt line 1: -e, an editable install, is not supported"),
    ("./x", "requirements.txt line 1: a local path is not supported; a skill names packages, not files"),
    ("x-1.0-py3-none-any.whl", "requirements.txt line 1: a local path is not supported"),
    ("file:///tmp/x", "requirements.txt line 1: file: paths are not supported"),
    ("-i https://evil.example/simple", "requirements.txt line 1: -i: a skill cannot choose the package index"),
    ("--extra-index-url https://evil.example/simple",
     "requirements.txt line 1: --extra-index-url: a skill cannot choose the package index"),
    ("-f ./wheels", "requirements.txt line 1: -f: a skill cannot choose the package index"),
    ("--trusted-host evil.example", "requirements.txt line 1: --trusted-host: a skill cannot choose the package"),
    ("x @ https://h/${TOKEN}/x.whl", "requirements.txt line 1: environment variables in requirements are not"),
    ("x @ http://h/x.whl", "requirements.txt line 1: http URLs are not supported, use https"),
    ("x @ https://user:pass@h/x.whl", "requirements.txt line 1: a URL with credentials is not supported"),
    ("x @ git+ssh://git@h/x.git", "requirements.txt line 1: git+ssh URLs are not supported"),
    ("-r ../outside.txt", "requirements.txt line 1: an include outside the skill is not supported"),
    ("-r /etc/x.txt", "requirements.txt line 1: an include must be a file inside the skill"),
    ("--use-feature fast-deps", "requirements.txt line 1: the option --use-feature is not supported"),
    ("x --config-settings a=b", "requirements.txt line 1: the option --config-settings is not supported"),
])
def test_refused_requirement_lines(tmp_path, line, text):
    assert unsupported(tmp_path, {"requirements.txt": line + "\n"}).startswith(text)


def test_include_rules(tmp_path):
    files = {"requirements.txt": "-r r1.txt\n"}
    for i in range(1, 7):
        files[f"r{i}.txt"] = f"-r r{i + 1}.txt\n" if i < 6 else "x\n"
    assert "nested more than 5 deep" in unsupported(tmp_path / "deep", files)
    folder = skill(tmp_path / "link", {"requirements.txt": "-r inc.txt\n", "real.txt": "x\n"})
    try:
        os.symlink(folder / "real.txt", folder / "inc.txt")
    except OSError:
        return
    assert "inc.txt is a link" in detect(folder).python.unsupported


# --------------------------------------------------------------------------------------------- pyproject
def test_pyproject_rules(tmp_path):
    folder = skill(tmp_path, {"pyproject.toml": '[project]\nname = "x"\nversion = "1"\ndependencies = ["a>=1"]\n'
                                                '[project.optional-dependencies]\ndev = ["pytest"]\n'})
    py = detect(folder).python
    assert py.packages == ("a",) and py.has_pyproject
    # the key covers the dependency list, not the version field
    other = skill(tmp_path, {"pyproject.toml": '[project]\nname = "x"\nversion = "2"\ndependencies = ["a>=1"]\n'},
                  "other")
    assert detect(other).python.material == py.material
    assert unsupported(tmp_path / "dyn", {"pyproject.toml": '[project]\ndynamic = ["dependencies"]\n'}) == \
        "pyproject.toml: the dependencies are dynamic"
    assert "Poetry" in unsupported(tmp_path / "poetry", {"pyproject.toml": '[tool.poetry.dependencies]\na = "1"\n'})
    assert detect(skill(tmp_path / "rp", {"pyproject.toml": '[project]\nrequires-python = ">=3.12"\n'
                                                            'dependencies = ["a"]\n'})).python.requires_python == ">=3.12"


@pytest.mark.parametrize("spec, version, expected", [
    (">=3.10", "3.11.15", True), (">=3.12", "3.11.15", False), ("<3.12,>=3.9", "3.11.0", True),
    ("~=3.11", "3.13.1", True), ("~=3.11", "4.0", False), ("~=3.11.2", "3.11.15", True), ("~=3.11.2", "3.12", False),
    ("==3.*", "3.11.15", True), ("==3.11.*", "3.12.0", False), ("!=3.11.*", "3.12.0", True),
    ("==3.11", "3.11.15", False), ("==3.11.15", "3.11.15", True), (">3.11", "3.11.15", True), ("<=3.11", "3.11.0", True),
    ("garbage", "3.11.15", None), (">=3.*", "3.11", None),
])
def test_python_satisfies(spec, version, expected):
    assert python_satisfies(spec, version) is expected


# -------------------------------------------------------------------------------------------- package.json
@pytest.mark.parametrize("spec", ["file:../x", "link:../x", "workspace:*", "portal:x", "git+file:///x",
                                  "http://h/x.tgz", "./x", "/abs", "~/x", "https://u:p@h/x.tgz", "git+ssh://git@h/x",
                                  "git://github.com/a/b.git", "ssh://github.com/a/b", "ftp://x/y.tgz", "git+http://h/x",
                                  "npm:y@file:../y", "npm:@s/y@git://h/y"])
def test_refused_node_specs(tmp_path, spec):
    node = detect(skill(tmp_path, {"package.json": json.dumps({"dependencies": {"x": spec}})})).node
    assert node.unsupported and "dependencies.x" in node.unsupported


@pytest.mark.parametrize("spec, why", [
    ("git+ssh://git@github.com/a/b.git", "git+ssh URLs are not supported; only https and git+https are installed "
                                         "automatically"),
    ("git://github.com/a/b.git", "git URLs are not supported; only https and git+https are installed automatically"),
    ("http://h/x.tgz", "http URLs are not supported, use https"),
    ("https://u:p@h/x.tgz", "a URL with credentials is not supported"),
])
def test_node_spec_refusals_say_why(tmp_path, spec, why):
    """An allowlist like the Python side's: unencrypted git: and the hub's own ssh keys are never used."""
    node = detect(skill(tmp_path, {"package.json": json.dumps({"dependencies": {"x": spec}})})).node
    assert node.unsupported.endswith(f": {why}"), node.unsupported


def test_accepted_node_specs_and_workspaces(tmp_path):
    deps = {"a": "^1.2", "b": "latest", "c": "npm:other@1", "d": "https://h/d.tgz", "e": "git+https://github.com/x/e",
            "f": "github:x/f", "i": "x/i#semver:^1", "j": "npm:@s/j@^2", "k": "*", "l": ""}
    node = detect(skill(tmp_path, {"package.json": json.dumps({"dependencies": deps,
                                                               "optionalDependencies": {"g": "1"},
                                                               "overrides": {"a": {"h": "1"}}})})).node
    assert not node.unsupported and node.packages == tuple(deps)
    assert set(json.loads(node.generated)) == {"name", "private", "dependencies", "optionalDependencies", "overrides"}
    bad = detect(skill(tmp_path / "ws", {"package.json": json.dumps({"workspaces": ["a"],
                                                                      "dependencies": {"x": "1"}})})).node
    assert bad.unsupported == "package.json: workspaces are not supported"


def test_check_lock():
    ok = {"lockfileVersion": 3, "packages": {"": {"name": "x"}, "node_modules/a": {"resolved": "https://r/a.tgz"},
                                             "node_modules/b": {"version": "1"}}}
    assert check_lock(ok, "package-lock.json") is None
    assert "lockfileVersion 1" in check_lock({"lockfileVersion": 1}, "package-lock.json")
    linked = {"lockfileVersion": 2, "packages": {"node_modules/a": {"link": True}}}
    assert check_lock(linked, "package-lock.json") == "package-lock.json: node_modules/a is a link to a local folder"
    for resolved in ("file:../x.tgz", "http://h/x.tgz", "git+ssh://git@h/x.git"):
        lock = {"lockfileVersion": 3, "packages": {"node_modules/x": {"resolved": resolved}}}
        assert "only https is installed automatically" in check_lock(lock, "package-lock.json")


# --------------------------------------------------------------------------------------------- key and name
def _tool(**fields) -> ToolInfo:
    base = {"impl": "cpython", "minor": "3.11", "platform": "win-amd64", "cache_tag": "cpython-311", "abiflags": "",
            "base": "c:\\py\\python.exe", "libc": "-", "os": ""}
    return ToolInfo("python", "python", "3.11.15", {**base, **fields}, "uv", "uv 1")


class _Spec:
    def __init__(self, skill="pdf", trust="user", material=(("requirements.txt", "aa"),), ecosystem="python"):
        self.skill, self.trust, self.material, self.ecosystem = skill, trust, material, ecosystem


def test_env_key_covers_exactly_the_manifests_and_the_tool():
    base = env_key(_Spec(), _tool())
    assert base == env_key(_Spec(material=(("requirements.txt", "aa"),)), _tool())
    two = (("a.txt", "1"), ("b.txt", "2"))
    assert env_key(_Spec(material=two), _tool()) == env_key(_Spec(material=tuple(reversed(two))), _tool())
    for changed in (_Spec(material=(("requirements.txt", "ab"),)), _Spec(trust="agent"), _Spec(skill="pdf2"),
                    _Spec(ecosystem="node")):
        assert env_key(changed, _tool()) != base
    for field, value in (("minor", "3.12"), ("platform", "linux-x86_64"), ("base", "d:\\py.exe"),
                         ("libc", "glibc-2.36"), ("os", "debian-13")):
        assert env_key(_Spec(), _tool(**{field: value})) != base, field
    # not the installer: uv and pip resolve the same requirements
    assert env_key(_Spec(), ToolInfo("python", "x", "3.11.1", _tool().key_fields, "pip", "pip 24")) == base


def test_env_key_follows_the_pyproject_dependencies_not_the_skills_other_files(tmp_path):
    files = {"requirements.txt": "a\n", "scripts/run.py": "print(1)\n"}
    one = detect(skill(tmp_path, files, "one")).python
    two = detect(skill(tmp_path, {**files, "scripts/run.py": "print(2)\n", "extra.md": "x"}, "two")).python
    assert one.material == two.material
    three = detect(skill(tmp_path, {**files, "requirements.txt": "a\nb\n"}, "three")).python
    assert three.material != one.material


def test_env_dirname():
    key = "0123456789ab"
    name = env_dirname("a-very-long-skill-name-that-goes-on-and-on", "python", key)
    assert len(name) <= 40 and DIRNAME_RE.fullmatch(name) and name.endswith("-py-" + key)
    assert env_dirname("abcdefghijklmnopqrstuvw-xyz", "node", key) == f"abcdefghijklmnopqrstuvw-js-{key}"
    assert DIRNAME_RE.fullmatch(env_dirname("pdf", "node", key))


# ------------------------------------------------------------------------------------------- shell and logs
@pytest.mark.parametrize("path, shell, expected", [
    ("C:\\Users\\ann\\.dreamteam\\skill-envs\\pdf-py-1\\venv\\Scripts\\python.exe", "powershell",
     "C:\\Users\\ann\\.dreamteam\\skill-envs\\pdf-py-1\\venv\\Scripts\\python.exe"),
    ("C:\\Users\\John Smith\\it's\\python.exe", "pwsh", "& 'C:\\Users\\John Smith\\it''s\\python.exe'"),
    ("/home/ann/.dreamteam/skill-envs/pdf-py-1/venv/bin/python", "bash",
     "/home/ann/.dreamteam/skill-envs/pdf-py-1/venv/bin/python"),
    ("/home/a b/venv/bin/python", "sh", "'/home/a b/venv/bin/python'"),
    ("C:\\Users\\R&D\\python.exe", "cmd", '"C:\\Users\\R&D\\python.exe"'),
    ("C:\\py\\python.exe", "cmd", '"C:\\py\\python.exe"'),
])
def test_shell_ready(path, shell, expected):
    assert shell_ready(path, shell) == expected


def test_powershell_runs_the_quoted_form(tmp_path):
    if not WINDOWS:
        pytest.skip("PowerShell")
    folder = tmp_path / "sp ace" / "it's env"
    folder.mkdir(parents=True)
    script = folder / "run.bat"
    script.write_text("@echo 42\n")
    for command in (shell_ready(str(script), "powershell"), shell_ready(sys.executable, "powershell")
                    + " -c 'print(42)'"):
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=60)
        assert done.stdout.strip() == "42", (command, done.stderr)


def test_redact():
    text = "fetching https://alice:pa55word@idx.example/simple and git+https://tok@h/x.git; secret s3cr3tTOKEN"
    out = redact(text, ["s3cr3tTOKEN", "", "ab"])
    assert "pa55word" not in out and "alice" not in out and "tok@" not in out and "s3cr3tTOKEN" not in out
    assert "https://***@idx.example" in out and "git+https://***@h" in out


def test_venv_paths():
    root = Path("env")
    if os.name == "nt":
        assert skill_deps.venv_python(root) == str(Path("env/venv/Scripts/python.exe"))
        assert skill_deps.venv_bin(root) == str(Path("env/venv/Scripts"))
    else:
        assert skill_deps.venv_python(root) == "env/venv/bin/python"
        assert skill_deps.venv_bin(root) == "env/venv/bin"


def test_trust_class():
    assert [trust_class(f) for f in ("library", "claude-user", "host-library", "agent", "claude-project")] == \
        ["user", "user", "user", "agent", "agent"]


def test_installer_env(tmp_path, monkeypatch):
    for name, value in (("VIRTUAL_ENV", "/v"), ("PYTHONPATH", "/p"), ("PIP_USER", "1"), ("UV_PYTHON", "3.9"),
                        ("PIP_INDEX_URL", "https://idx/simple"), ("MY_INDEX_TOKEN", "tok123")):
        monkeypatch.setenv(name, value)
    config = Config.from_dict(tmp_path, {"skills": {"install_env": ["MY_INDEX_TOKEN"]}})
    user = installer_env(config, "user")
    assert not {"VIRTUAL_ENV", "PYTHONPATH", "PIP_USER", "UV_PYTHON", "DREAMTEAM_SKILL_ENVS_DIR"} & set(user)
    assert user["PIP_INDEX_URL"] == "https://idx/simple" and user["MY_INDEX_TOKEN"] == "tok123"
    assert user["PYTHONNOUSERSITE"] == "1" and user["PIP_NO_INPUT"] == "1" and user["UV_NO_PROGRESS"] == "1"
    agent = installer_env(config, "agent")
    assert "MY_INDEX_TOKEN" not in agent and agent["PIP_INDEX_URL"] == "https://idx/simple"


def test_the_pip_probe_gets_the_installers_environment(tmp_path, monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw.get("env")))
        return subprocess.CompletedProcess(argv, 0, b"pip 24.0 from somewhere", b"")

    monkeypatch.setattr(skill_deps.subprocess, "run", fake_run)
    monkeypatch.setattr(skill_deps, "_uv", lambda env: None)
    monkeypatch.setattr(skill_deps, "_PROBES", {})
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1", "MARK": "x"}
    py = ToolInfo("python", sys.executable, "3.11.15", _tool().key_fields)
    tool = skill_deps.pick_installer(py, env, tmp_path)
    assert tool.installer == "pip" and tool.label == "pip 24.0"
    assert seen and seen[0][0][1:] == ["-m", "pip", "--version"] and seen[0][1] is env


def test_a_failed_probe_is_run_again_later_and_a_good_one_is_kept(tmp_path, monkeypatch):
    """One timeout (a cold start, an antivirus scan) never leaves every Python skill unavailable until a restart."""
    runs = []

    def flaky(argv, env, cwd, timeout=30.0):
        runs.append(argv)
        return None if len(runs) == 1 else json.dumps({"impl": "cpython", "minor": "3.11", "version": "3.11.15"})

    monkeypatch.setattr(skill_deps, "_run_quiet", flaky)
    monkeypatch.setattr(skill_deps, "_PROBES", {})
    probe = lambda: skill_deps.probe_python(sys.executable, {}, tmp_path)   # noqa: E731
    assert probe() is None and probe() is None and len(runs) == 1   # within the retry time: not run again
    skill_deps.forget_failed_probes()   # skill_reinstall
    assert probe()["version"] == "3.11.15" and len(runs) == 2
    skill_deps.forget_failed_probes()
    assert probe()["version"] == "3.11.15" and len(runs) == 2   # a good result is kept
    monkeypatch.setattr(skill_deps, "_PROBES", {})
    runs.clear()
    monkeypatch.setattr(skill_deps, "PROBE_RETRY_SECONDS", 0.0)
    assert probe() is None and probe()["version"] == "3.11.15" and len(runs) == 2   # the failure expired


def test_resolve_node_needs_node_exe_and_finds_npm_beside_the_real_binary(tmp_path, monkeypatch):
    shim = tmp_path / "shims"
    shim.mkdir()
    (shim / "node.cmd").write_text("@echo off\n")
    env = {"PATH": str(shim)}
    if WINDOWS:
        assert skill_deps.resolve_node(env, tmp_path) == {"code": "unavailable", "detail": "Node.js"}
    real = tmp_path / "real" / ("node.exe" if WINDOWS else "node")
    cli = real.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// npm")
    (shim / real.name).write_text("shim")
    real.write_text("real")
    monkeypatch.setattr(skill_deps, "probe_node", lambda path, env, cwd: [str(real), "24.16.0", "137", "win32",
                                                                          "x64"])
    tool = skill_deps.resolve_node(env, tmp_path)
    assert tool.path == str(real) and tool.extra["npm_cli"] == str(cli) and tool.key_fields["major"] == 24
    (cli).unlink()
    assert skill_deps.resolve_node(env, tmp_path)["code"] == "unavailable"


def test_resolve_python_reports_a_missing_interpreter(tmp_path):
    config = Config.from_dict(tmp_path, {"skills": {"python": str(tmp_path / "nope" / "python.exe")}})
    reason = skill_deps.resolve_python(config, dict(os.environ), tmp_path)
    assert reason == {"code": "unavailable", "detail": f"Python '{tmp_path / 'nope' / 'python.exe'}' of [skills] python"}
    assert "(" not in reason["detail"]


def test_the_real_interpreter_probe(tmp_path):
    config = Config.from_dict(tmp_path, {})
    tool = skill_deps.resolve_python(config, installer_env(config, "user"), tmp_path)
    assert isinstance(tool, ToolInfo), tool
    assert tool.key_fields["minor"] == "%d.%d" % sys.version_info[:2] and tool.version.startswith(tool.key_fields["minor"])
    assert set(tool.key_fields) == {"impl", "minor", "platform", "cache_tag", "abiflags", "base", "libc", "os"}


def test_long_paths_check(tmp_path, monkeypatch):
    if not WINDOWS:
        pytest.skip("Windows paths")
    deep = tmp_path / "venv" / "Lib" / "site-packages" / "deeppkg"
    path = deep
    while len(str(path)) < 270:
        path = path / ("d" * 20)
    os.makedirs(skill_deps.long_path(path), exist_ok=True)
    with open(skill_deps.long_path(path / "f.py"), "w") as f:
        f.write("x")
    found = skill_deps.longest_paths(tmp_path / "venv")
    assert found is not None and found[0] >= 260 and "deeppkg" in found[1]
    assert skill_deps.longest_paths(tmp_path / "venv" / "Lib" / "missing") is None


def test_the_commands_of_a_venv_leave_out_only_its_own_entries():
    """uv writes every shell's activator on every platform: a POSIX bin/ also has deactivate.bat and pydoc.bat."""
    own = ["activate", "activate.bat", "activate.csh", "activate.fish", "activate.nu", "activate.ps1", "activate.xsh",
           "activate_this.py", "deactivate.bat", "pydoc.bat", "Activate.ps1"]
    posix = own + ["python", "python3", "python3.11", "pip", "pip3", "pip3.11",
                   "markitdown", "uvicorn", "pip-compile", "pipdeptree", "uv"]
    assert skill_deps._command_names(posix, windows=False) == \
        ["markitdown", "pip-compile", "pipdeptree", "uv", "uvicorn"]
    nt = own + ["python.exe", "pythonw.exe", "pip.exe", "pip3.exe", "pip3.11.exe", "markitdown.exe", "uvicorn.exe",
                "pip-compile.exe", "pipdeptree.exe", "readme.txt"]
    assert skill_deps._command_names(nt, windows=True) == ["markitdown", "pip-compile", "pipdeptree", "uvicorn"]


# -------------------------------------------------------------------------------------------------- settings
def test_the_dependency_settings(tmp_path, monkeypatch):
    import tomllib

    from dreamteam.config import SkillsSettings, render_template

    defaults = SkillsSettings()
    assert (defaults.install, defaults.install_proposals, defaults.envs_dir, defaults.python, defaults.install_scripts,
            defaults.install_timeout, defaults.env_retention_days, defaults.install_env) == \
        (True, True, "~/.dreamteam/skill-envs", "", False, 900, 30, [])
    for bad, text in (({"install_env": ["OK_NAME", "x y"]}, "is not an environment variable name"),
                      ({"env_retention_days": -1}, "env_retention_days must be >= 0"),
                      ({"install_timeout": 0}, "install_timeout must be >= 1")):
        with pytest.raises(ValueError, match=text):
            Config.from_dict(tmp_path, {"skills": bad})
    raw = tomllib.loads(render_template("X", "ru", "anthropic"))
    assert raw["skills"]["install"] is True and raw["skills"]["install_env"] == []
    assert raw["skills"]["env_retention_days"] == 30 and raw["skills"]["envs_dir"] == "~/.dreamteam/skill-envs"
    Config.from_dict(tmp_path, raw)
    monkeypatch.delenv("DREAMTEAM_SKILL_ENVS_DIR")
    assert Config.from_dict(tmp_path, {"skills": {"envs_dir": str(tmp_path / "e")}}).skill_envs_dir == tmp_path / "e"
    monkeypatch.setenv("DREAMTEAM_SKILL_ENVS_DIR", str(tmp_path / "env-wins"))
    assert Config.from_dict(tmp_path, {"skills": {"envs_dir": str(tmp_path / "e")}}).skill_envs_dir == \
        tmp_path / "env-wins"
    assert skill_deps.settings_hash(defaults) != skill_deps.settings_hash(SkillsSettings(install=False))
    assert skill_deps.settings_hash(defaults) == skill_deps.settings_hash(SkillsSettings(max_mb=3))
