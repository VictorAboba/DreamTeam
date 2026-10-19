"""Offline packages for the real-installer tests: a hand-made wheel (`make_wheel`) and an npm tarball
(`make_npm_tarball`), so that pip, uv and npm install from a local folder with no index and no network."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(folder: Path, name: str, version: str = "1.0", files: dict[str, str] | None = None,
               requires: list[str] | None = None) -> Path:
    """`<folder>/<name>-<version>-py3-none-any.whl`; `files` default to a module `<name>` with `VALUE = 42`."""
    folder.mkdir(parents=True, exist_ok=True)
    dist = name.replace("-", "_")
    files = files if files is not None else {f"{dist}/__init__.py": "VALUE = 42\n"}
    info = f"{dist}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
        f"Requires-Dist: {r}\n" for r in requires or [])
    entries = {**files, f"{info}/METADATA": meta,
               f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: dreamteam-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = "".join(f"{path},{_record_hash(text.encode())},{len(text.encode())}\n" for path, text in entries.items())
    record += f"{info}/RECORD,,\n"
    path = folder / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name_in, text in {**entries, f"{info}/RECORD": record}.items():
            zf.writestr(name_in, text)
    return path


def wheel_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def make_npm_tarball(folder: Path, name: str, files: dict[str, str] | None = None, version: str = "1.0.0",
                     dependencies: dict[str, str] | None = None, scripts: dict[str, str] | None = None) -> Path:
    """`<folder>/<name>-<version>.tgz` as `npm pack` makes it (every entry under `package/`)."""
    folder.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name, "version": version, "main": "index.js"}
    if dependencies:
        manifest["dependencies"] = dependencies
    if scripts:
        manifest["scripts"] = scripts
    entries = {"package.json": json.dumps(manifest, indent=1),
               **(files if files is not None else {"index.js": f"module.exports = {{name: {name!r}}};\n"})}
    path = folder / f"{name}-{version}.tgz"
    with tarfile.open(path, "w:gz") as tar:
        for rel, text in entries.items():
            data = text.encode()
            item = tarfile.TarInfo(f"package/{rel}")
            item.size = len(data)
            item.mode = 0o644
            tar.addfile(item, io.BytesIO(data))
    return path
