"""Sandbox: run the whole swarm (agents' shell commands and MCP servers included) inside a Docker container."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import netoff
from .config import Config

IMAGE = "dreamteam:latest"
# pyproject.toml's [project] dependencies, for an installed package (no repo pyproject.toml next to it);
# tests/test_surfaces.py keeps the two lists equal
DEPENDENCIES = ["mcp>=2.3,<3", "anthropic>=1.12,<2", "openai>=3.26,<4", "starlette>=0.48", "uvicorn>=0.31",
                "httpx2>=2.10", "websockets>=13", "prompt-toolkit>=3.0.43", "rich>=13.7"]

DOCKERFILE = """FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential iptables \\
 && rm -rf /var/lib/apt/lists/*
RUN pip install uv
# sessions without network run their members' commands as this user, whose packets a firewall rejects
# (`dreamteam sandbox` sets it up); git's settings are the system's, so that they hold for both users
RUN useradd --uid 10001 --user-group --create-home --shell /bin/bash dreamteam-agent
WORKDIR /opt/dreamteam
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
RUN git config --system user.name dreamteam \\
 && git config --system user.email dreamteam@localhost \\
 && git config --system --add safe.directory '*'
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


DOCKERFILE_LABEL = "dreamteam.dockerfile"


def dockerfile_hash() -> str:
    """The label build_image puts on the image: an image built from another DOCKERFILE (an older DreamTeam, or
    `docker compose build`) has another one, or none."""
    return hashlib.sha256(DOCKERFILE.encode("utf-8")).hexdigest()[:16]


def image_dockerfile(image: str = IMAGE) -> str:
    """The DOCKERFILE label of an image ("" without one, or when it cannot be read)."""
    result = subprocess.run([_docker(), "image", "inspect", "--format",
                             "{{ index .Config.Labels \"" + DOCKERFILE_LABEL + "\" }}", image],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return "" if value == "<no value>" else value


def build_image(image: str = IMAGE) -> None:
    with tempfile.TemporaryDirectory(prefix="dt-build-") as tmp:
        ctx = build_context(Path(tmp))
        print(f"Building {image} ...", file=sys.stderr)
        code = subprocess.call([_docker(), "build", "--label", f"{DOCKERFILE_LABEL}={dockerfile_hash()}", "-t", image,
                                str(ctx)])
        if code != 0:
            raise SystemExit(f"docker build failed with code {code}")


def wants_firewall(project_dir: Path, config: Config, network: str | None = None) -> bool:
    """Whether the sandbox needs the firewall of the sessions without network (docs/SPEC.md §7.2): `--network off`,
    [swarm] network = "off" in the settings, or a session of the home that has it in its overrides. A session
    switched off later in a container started without it gets the best-effort isolation only (the UI says so)."""
    if network == "off" or config.swarm.network == "off":
        return True
    sessions = project_dir / "sessions"
    for meta in sessions.glob("*/session.json") if sessions.is_dir() else ():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        overrides = data.get("overrides") if isinstance(data, dict) else None
        swarm = overrides.get("swarm") if isinstance(overrides, dict) else None
        if isinstance(swarm, dict) and swarm.get("network") == "off":
            return True
    return False


def env_passthrough(config: Config) -> list[str]:
    """Names of environment variables to pass into the container (values are taken from the host env)."""
    names = {p.api_key_env for p in config.models.values() if p.api_key_env}
    names |= {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} | set(config.shell.pass_env)
    # the skills variables are set by sandbox_command; a host value passed by name would come later and win
    names -= SKILLS_ENV
    return sorted(n for n in names if n and n in os.environ)


CONTAINER_PORT = 8765
HOST_SKILLS_MOUNT = "/dreamteam/host-skills"        # the host's skills library inside the sandbox (read-only)
# the sandbox's own, writable library (in the mounted home). Not .dreamteam/skills, the default library's name: a
# home mounted from the user's home folder would make the host's own library writable there
SANDBOX_SKILLS_DIR = "/swarm/.dreamteam/skills-library"
SANDBOX_ENVS_DIR = "/dreamteam/skill-envs"   # the skills' dependency environments: a named volume per home
SKILLS_ENV = frozenset({"DREAMTEAM_SKILLS_DIR", "DREAMTEAM_HOST_SKILLS_DIR", "DREAMTEAM_SKILL_ENVS_DIR",
                        "UV_CACHE_DIR"})


def envs_volume(project_dir: Path) -> str:
    """The named volume of a home's skill environments: a Linux file system (venv symlinks, hardlinks and flock work
    there), kept across `--rm` containers, never shared with another home."""
    return f"dreamteam-skill-envs-{hashlib.sha256(str(project_dir.resolve()).encode()).hexdigest()[:10]}"


def sandbox_host_library(config: Config) -> Path:
    """The host's skills library as it is mounted into the sandbox (created by run_sandbox, never here)."""
    return config.skills_library_dir.expanduser().resolve()


def library_inside_warning(project_dir: Path, config: Config) -> list[str]:
    """A warning when the host's skills library is inside the mounted directory: it is then writable through
    /swarm as well, so the read-only mount protects nothing (empty otherwise)."""
    library, mounted = sandbox_host_library(config), project_dir.resolve()
    if library != mounted and mounted not in library.parents:
        return []
    return [f"Warning: the skills library {library} is inside {mounted}, which is mounted writable: agents in the "
            "container can change it. Move it ([skills] library_dir or DREAMTEAM_SKILLS_DIR) outside."]


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
                    home: bool | None = None, firewall: bool = False, network: str | None = None) -> list[str]:
    """`docker run` argv for the sandbox. The hub inside listens on CONTAINER_PORT, published on the host's
    127.0.0.1:port only; DREAMTEAM_PUBLIC_URL tells it that host URL, so the board shows the right MCP URL
    and hub.json (in the mounted project) lets host-side `dreamteam mcp/console/status` find it.
    `home`: serve the directory as a launcher home (True) or `run` it as a single-swarm project (False);
    None decides by its sessions/ directory. `firewall`: the container may set up the firewall of the sessions
    without network (NET_ADMIN, which DreamTeam drops right after: netoff.sandbox_entry). `network`: `--network`
    of the command inside (a project's run, or the session `goal` creates)."""
    public = f"http://127.0.0.1:{port}"
    # --init: a tiny init (tini) is PID 1 and reaps orphans (processes the agents' commands leave running), so a
    # zombie never keeps a process group alive; it forwards SIGTERM/SIGINT to dreamteam. Ports that agents' dev
    # servers open inside the container are not published.
    cmd = [docker, "run", "--rm", "--init", "--name", f"dreamteam-{port}", "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
           "-v", f"{project_dir.resolve()}:/swarm", "-e", f"DREAMTEAM_PUBLIC_URL={public}"]
    # Skills (docs/SPEC.md §19): the agents have `shell` in the hub's container, so the host's library is mounted
    # read-only (the source `host-library` inside); the sandbox's own library is <home>/.dreamteam/skills-library,
    # which the host never reads by itself. ~/.claude/skills is not mounted: it would show all the user's personal
    # skills.
    cmd += ["-v", f"{sandbox_host_library(config)}:{HOST_SKILLS_MOUNT}:ro",
            "-e", f"DREAMTEAM_HOST_SKILLS_DIR={HOST_SKILLS_MOUNT}", "-e", f"DREAMTEAM_SKILLS_DIR={SANDBOX_SKILLS_DIR}"]
    # the skills' environments: never the host's (another platform, other interpreter paths); uv's cache beside them
    cmd += ["-v", f"{envs_volume(project_dir)}:{SANDBOX_ENVS_DIR}", "-e", f"DREAMTEAM_SKILL_ENVS_DIR={SANDBOX_ENVS_DIR}",
            "-e", f"UV_CACHE_DIR={SANDBOX_ENVS_DIR}/.cache/uv"]
    if firewall:
        cmd += ["--cap-add", "NET_ADMIN", "-e", f"{netoff.FIREWALL_ENV}=1"]
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
    if network and (goal or not home):  # (serve takes it only for the session its --goal creates)
        cmd += ["--network", network]
    return cmd


def run_sandbox(project_dir: Path, port: int, build: bool = False, goal: str | None = None,
                image: str = IMAGE, home: bool | None = None, network: str | None = None) -> int:
    config = Config.load(project_dir)
    firewall = wants_firewall(project_dir, config, network)
    if build or not image_exists(image):
        build_image(image)
    elif image_dockerfile(image) != dockerfile_hash():
        if firewall:  # the firewall needs the image's user and iptables
            print(f"{image} was built from another Dockerfile; rebuilding it for the sessions without network.",
                  file=sys.stderr)
            build_image(image)
        else:
            print(f"Note: {image} was built from another Dockerfile; `dreamteam sandbox --build` rebuilds it.",
                  file=sys.stderr)
    for profile in config.models.values():
        if profile.base_url and any(h in profile.base_url for h in ("localhost", "127.0.0.1")):
            print(f"Note: model profile '{profile.name}' uses {profile.base_url}; inside Docker use "
                  "http://host.docker.internal:<port> instead.", file=sys.stderr)
    is_home = home if home is not None else (project_dir / "sessions").is_dir()
    if is_home:
        for line in outside_home_warning(project_dir):
            print(line, file=sys.stderr)
    for line in library_inside_warning(project_dir, config):
        print(line, file=sys.stderr)
    from .cli import _stdin_is_terminal  # (NUL claims to be a tty on Windows: `docker run -it` would refuse it)

    # Docker would create a missing host folder of a bind mount itself, owned by root on Linux
    sandbox_host_library(config).mkdir(parents=True, exist_ok=True)

    cmd = sandbox_command(project_dir, port, config, goal, image, docker=_docker(), tty=_stdin_is_terminal(),
                          home=home, firewall=firewall, network=network)
    if firewall:
        print("Sessions without network: their agents' commands run as a separate user whose network the "
              "container's firewall closes (NET_ADMIN only for setting it up).", file=sys.stderr)
    print(f"Swarm UI: http://127.0.0.1:{port}  (MCP: http://127.0.0.1:{port}/mcp)", file=sys.stderr)
    return subprocess.call(cmd)
