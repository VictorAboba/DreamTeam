"""A test double of skill_deps.INSTALLERS (the autouse fixture in conftest.py puts it in place: no test runs a real
installer unless it opts in). Its tools need no probe, and its commands run this file as a script, a real process
tree that writes what a real install would: a venv's python, dist-info folders and console scripts, or
node_modules with npm's hidden lockfile and .bin entries. A test sets `behavior[skill]` to make one fail, sleep or
hang; `calls` records every command list asked for (argv, cwd, environment, trust class)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT = str(Path(__file__).resolve())
WINDOWS = os.name == "nt"


class FakeInstallers:
    def __init__(self) -> None:
        self.python: bool | dict = True      # a dict: the `unavailable` reason python_tool returns
        self.node: bool | dict = True
        self.python_version = "3.11.15"
        self.minor = "3.11"
        self.node_major = 24
        self.behavior: dict[str, dict[str, Any]] = {}  # skill -> {"fail": text, "sleep": s, "hang": True,
        #                                                 "commands": [...], "skip": [packages not installed]}
        self.calls: list[dict[str, Any]] = []

    def python_tool(self, config, env, cwd):
        from dreamteam.skill_deps import ToolInfo

        if self.python is not True:
            return self.python if isinstance(self.python, dict) else {"code": "unavailable", "detail": "uv or pip"}
        return ToolInfo("python", sys.executable, self.python_version,
                        {"impl": "cpython", "minor": self.minor, "platform": "test", "cache_tag": "test",
                         "abiflags": "", "base": "fake-python", "libc": "-", "os": ""}, "fake", "fake-pip 1.0")

    def node_tool(self, config, env, cwd):
        from dreamteam.skill_deps import ToolInfo

        if self.node is not True:
            return self.node if isinstance(self.node, dict) else {"code": "unavailable", "detail": "Node.js"}
        return ToolInfo("node", sys.executable, f"{self.node_major}.0.0",
                        {"major": self.node_major, "abi": "137", "platform": "test", "arch": "x64", "os": ""}, "npm",
                        "fake-npm 1.0", {"npm_cli": SCRIPT})

    def _record(self, spec, env_dir: Path, ecosystem: str) -> str:
        self.calls.append({"skill": spec.skill, "ecosystem": ecosystem, "dirname": spec.dirname, "trust": spec.trust,
                           "env": dict(spec.options.env), "cwd": str(env_dir),
                           "args": list(getattr(spec.manifest, "args", ())), "scripts": spec.options.scripts})
        return json.dumps({"packages": list(spec.manifest.packages), **self.behavior.get(spec.skill, {})})

    def python_commands(self, spec, env_dir: Path):
        opts = self._record(spec, env_dir, "python")
        return [([sys.executable, SCRIPT, "venv", str(env_dir), opts], "venv"),
                ([sys.executable, SCRIPT, "python", str(env_dir), opts], "install")]

    def node_commands(self, spec, env_dir: Path):
        opts = self._record(spec, env_dir, "node")
        return [([sys.executable, SCRIPT, "node", str(env_dir), opts], "install")]


def default() -> FakeInstallers:
    return FakeInstallers()


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _site(env_dir: Path) -> Path:
    venv = env_dir / "venv"
    return venv / "Lib" / "site-packages" if WINDOWS else venv / "lib" / "python3.11" / "site-packages"


def main(argv: list[str]) -> int:
    mode, env_dir = argv[0], Path(argv[1])
    opts = json.loads(argv[2]) if len(argv) > 2 else {}
    _write(env_dir / f"fake-{mode}-cwd.txt", os.getcwd())
    if mode == "venv":
        _write(env_dir / "venv" / ("Scripts/python.exe" if WINDOWS else "bin/python"), "fake python")
        _site(env_dir).mkdir(parents=True, exist_ok=True)
        print("created a fake venv")
        return 0
    print(f"fake {mode} install of {', '.join(opts.get('packages') or [])}", flush=True)
    _write(env_dir / "partial", "an install in progress")
    if opts.get("sleep"):
        time.sleep(float(opts["sleep"]))
    if opts.get("hang"):
        time.sleep(3600)
    if opts.get("fail"):
        print("Collecting things", flush=True)
        print(opts["fail"], flush=True)
        return 1
    packages = [p for p in opts.get("packages") or [] if p not in (opts.get("skip") or [])]
    commands = opts.get("commands") or []
    if mode == "python":
        for name in packages:
            _write(_site(env_dir) / f"{name.replace('-', '_')}-1.0.dist-info" / "METADATA", f"Name: {name}\n")
            _write(_site(env_dir) / name.replace("-", "_") / "__init__.py", "")
        for command in commands:
            _write(env_dir / "venv" / ("Scripts" if WINDOWS else "bin") / (command + (".exe" if WINDOWS else "")),
                   "fake command")
        if opts.get("deep"):   # a package whose files pass 260 characters (written as uv does, Windows only)
            deep = _site(env_dir).resolve() / "deeppkg"
            while len(str(deep)) < 270:
                deep = deep / ("d" * 20)
            target = "\\\\?\\" + str(deep) if WINDOWS else str(deep)
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "f.py"), "w") as f:
                f.write("x")
    else:
        modules = env_dir / "node_modules"
        _write(modules / ".package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {}}))
        for name in packages:
            _write(modules.joinpath(*name.split("/")) / "package.json", json.dumps({"name": name, "version": "1.0.0"}))
            _write(modules.joinpath(*name.split("/")) / "index.js", "module.exports = 42;\n")
        for command in commands:
            _write(modules / ".bin" / command, "fake command")
            if WINDOWS:
                _write(modules / ".bin" / f"{command}.cmd", "fake command")
    (env_dir / "partial").unlink()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
