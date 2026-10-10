"""Sandbox: run the whole swarm (agents' shell commands and MCP servers included) inside a Docker container."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import Config

IMAGE = "dreamteam:latest"
# pyproject.toml's [project] dependencies, for an installed package (no repo pyproject.toml next to it);
# tests/test_surfaces.py keeps the two lists equal
DEPENDENCIES = ["mcp>=2.3,<3", "anthropic>=1.12,<2", "openai>=3.26,<4", "starlette>=0.48", "uvicorn>=0.31",
                "httpx2>=2.10", "websockets>=13", "prompt-toolkit>=3.0.43", "rich>=13.7"]

DOCKERFILE = """FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential \\
 && rm -rf /var/lib/apt/lists/*
RUN pip install uv
WORKDIR /opt/dreamteam
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
RUN git config --global user.name dreamteam \\
 && git config --global user.email dreamteam@localhost \\
 && git config --global --add safe.directory '*'
WORKDIR /swarm
EXPOSE 8765
ENTRYPOINT ["dreamteam"]
# the container port is fixed (8765) whatever dreamteam.toml says: port mappings point at it;
# /swarm is a DreamTeam home (sessions page + every session) or a single-swarm project (`run`)
CMD ["serve", "/swarm", "--host", "0.0.0.0", "--port", "8765", "--no-console", "--no-browser"]
"""


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _is_dreamteam_pyproject(path: Path) -> bool:
    """True for this project's own pyproject.toml (an installed package has none next to it, and the
    directory two levels up may hold some other project's file)."""
    import tomllib

    try:
        with path.open("rb") as f:
            return tomllib.load(f).get("project", {}).get("name") == "dreamteam"
    except (OSError, ValueError):
        return False


def build_context(dest: Path) -> Path:
    """Assemble a docker build context with the installed dreamteam sources."""
    pkg = _package_root()
    src = dest / "src" / "dreamteam"
    shutil.copytree(pkg, src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    repo_root = pkg.parent.parent
    pyproject = repo_root / "pyproject.toml"
    if _is_dreamteam_pyproject(pyproject):
        shutil.copy(pyproject, dest / "pyproject.toml")
    else:
        from . import __version__

        (dest / "pyproject.toml").write_text(
            "[project]\nname = \"dreamteam\"\nversion = \"" + __version__ + "\"\nrequires-python = \">=3.11\"\n"
            "dependencies = " + json.dumps(DEPENDENCIES) + "\n[project.scripts]\n"
            "dreamteam = \"dreamteam.cli:main\"\n[build-system]\nrequires = [\"hatchling\"]\n"
            "build-backend = \"hatchling.build\"\n[tool.hatch.build.targets.wheel]\npackages = [\"src/dreamteam\"]\n",
            encoding="utf-8",
        )
    readme = repo_root / "README.md"
    (dest / "README.md").write_text(readme.read_text(encoding="utf-8") if readme.exists() else "# DreamTeam\n",
                                    encoding="utf-8")
    (dest / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    return dest


def _docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        raise SystemExit("Docker is not installed or not on PATH. Install Docker Desktop and try again.")
    return exe


def image_exists(image: str = IMAGE) -> bool:
    result = subprocess.run([_docker(), "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def build_image(image: str = IMAGE) -> None:
    with tempfile.TemporaryDirectory(prefix="dt-build-") as tmp:
        ctx = build_context(Path(tmp))
        print(f"Building {image} ...", file=sys.stderr)
        code = subprocess.call([_docker(), "build", "-t", image, str(ctx)])
        if code != 0:
            raise SystemExit(f"docker build failed with code {code}")


def env_passthrough(config: Config) -> list[str]:
    """Names of environment variables to pass into the container (values are taken from the host env)."""
    names = {p.api_key_env for p in config.models.values() if p.api_key_env}
    names |= {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} | set(config.shell.pass_env)
    return sorted(n for n in names if n and n in os.environ)


CONTAINER_PORT = 8765


def outside_home_warning(home: Path) -> list[str]:
    """Lines warning about the sessions of a home that work in a folder outside it: only the home is mounted
    into the container, so they cannot start there (empty when there are none). A folder inside the home is
    stored relative to it as well (session.json "in_home"), so it is found where the container mounts the home."""
    from .sessions import external_workspaces

    found = external_workspaces(home)
    if not found:
        return []
    return ([f"Warning: only {home} is mounted into the container; these sessions work in a folder outside it "
             "and cannot start there:"]
            + [f"  {sid}: {folder}" for sid, folder in found]
            + ["  Move them into the home first (/cd <folder inside the home>, or /cd --default)."])


def sandbox_command(project_dir: Path, port: int, config: Config, goal: str | None = None,
                    image: str = IMAGE, docker: str = "docker", tty: bool = False,
                    home: bool | None = None) -> list[str]:
    """`docker run` argv for the sandbox. The hub inside listens on CONTAINER_PORT, published on the host's
    127.0.0.1:port only; DREAMTEAM_PUBLIC_URL tells it that host URL, so the board shows the right MCP URL
    and hub.json (in the mounted project) lets host-side `dreamteam mcp/console/status` find it.
    `home`: serve the directory as a launcher home (True) or `run` it as a single-swarm project (False);
    None decides by its sessions/ directory."""
    public = f"http://127.0.0.1:{port}"
    cmd = [docker, "run", "--rm", "--name", f"dreamteam-{port}", "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
           "-v", f"{project_dir.resolve()}:/swarm", "-e", f"DREAMTEAM_PUBLIC_URL={public}"]
    if tty:
        cmd.append("-it")
    for name in env_passthrough(config):
        cmd += ["-e", name]  # value taken from the host environment, never put on the command line
    if home is None:
        home = (project_dir / "sessions").is_dir()
    if home:  # a launcher home: the sessions page and all its sessions
        cmd += [image, "serve", "/swarm", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT), "--no-console",
                "--no-browser"]
    else:  # a single-swarm project made by `dreamteam init`
        cmd += [image, "run", "/swarm", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT), "--no-console"]
    if goal:
        cmd += ["--goal", goal]
    return cmd


def run_sandbox(project_dir: Path, port: int, build: bool = False, goal: str | None = None,
                image: str = IMAGE, home: bool | None = None) -> int:
    config = Config.load(project_dir)
    if build or not image_exists(image):
        build_image(image)
    for profile in config.models.values():
        if profile.base_url and any(h in profile.base_url for h in ("localhost", "127.0.0.1")):
            print(f"Note: model profile '{profile.name}' uses {profile.base_url}; inside Docker use "
                  "http://host.docker.internal:<port> instead.", file=sys.stderr)
    is_home = home if home is not None else (project_dir / "sessions").is_dir()
    if is_home:
        for line in outside_home_warning(project_dir):
            print(line, file=sys.stderr)
    from .cli import _stdin_is_terminal  # (NUL claims to be a tty on Windows: `docker run -it` would refuse it)

    cmd = sandbox_command(project_dir, port, config, goal, image, docker=_docker(), tty=_stdin_is_terminal(),
                          home=home)
    print(f"Swarm UI: http://127.0.0.1:{port}  (MCP: http://127.0.0.1:{port}/mcp)", file=sys.stderr)
    return subprocess.call(cmd)
