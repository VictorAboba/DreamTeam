"""Sandbox: run the whole swarm (agents' shell commands and MCP servers included) inside a Docker container."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from . import hubauth, netoff
from .config import API_FOLDERS_ENV, Config

IMAGE = "dreamteam:latest"

DOCKERFILE = """# the Debian release pinned (trixie, git 2.47): netoff.trust_workspace's <root>/* needs git 2.46, and the bare tag
# meant bookworm (git 2.39) before trixie, which a machine that pulled it then may still have cached
FROM python:3.13-slim-trixie
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git curl ca-certificates build-essential iptables \\
 && rm -rf /var/lib/apt/lists/*
# uv at a fixed version: it installs the image's packages from uv.lock below, and the skills' environments
RUN pip install uv==0.12.23
# sessions without network run their members' commands as this user, whose packets a firewall rejects
# (`dreamteam sandbox` sets it up); git's settings are the system's, so that they hold for both users
RUN useradd --uid 10001 --user-group --create-home --shell /bin/bash dreamteam-agent
WORKDIR /opt/dreamteam
COPY pyproject.toml README.md uv.lock* ./
COPY src ./src
# the versions uv.lock pins, checked against its hashes, go into the system Python as before (`--locked` stops the
# build when uv.lock is behind pyproject.toml); `dreamteam sandbox` of an installed DreamTeam has no uv.lock
# (docker.build_context), and its image gets the version ranges of pyproject.toml
RUN if [ -f uv.lock ]; then \\
      uv export --locked --no-dev --no-emit-project --format requirements.txt -o /tmp/requirements.txt \\
   && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \\
   && uv pip install --system --no-cache --no-deps . \\
   && rm /tmp/requirements.txt; \\
    else pip install .; fi
# no safe.directory: git trusts no repository of another owner here; the processes of a session get their own
# workspace (netoff.trust_workspace), DreamTeam's own git names its repository per command
RUN git config --system user.name dreamteam \\
 && git config --system user.email dreamteam@localhost
# the image's own marker: netoff.trust_workspace applies also under a runtime that creates no /.dockerenv
ENV DREAMTEAM_IMAGE=1
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


def _installed_dependencies() -> list[str]:
    """The [project] dependencies the installed dreamteam was built with (its package metadata): what an
    installed package without the repository's pyproject.toml next to it has to declare in the image."""
    from importlib import metadata

    try:
        requires = metadata.requires("dreamteam") or []
    except metadata.PackageNotFoundError:
        raise SystemExit("dreamteam is neither installed nor run from its repository: cannot tell the image "
                         "which packages it needs.") from None
    return [r for r in requires if "extra ==" not in r]


def build_context(dest: Path) -> Path:
    """Assemble a docker build context with the installed dreamteam sources (and the repository's uv.lock)."""
    pkg = _package_root()
    src = dest / "src" / "dreamteam"
    shutil.copytree(pkg, src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    repo_root = pkg.parent.parent
    pyproject = repo_root / "pyproject.toml"
    if _is_dreamteam_pyproject(pyproject):
        shutil.copy(pyproject, dest / "pyproject.toml")
        # the versions DOCKERFILE installs; the generated pyproject.toml below goes without one (its ranges)
        if (repo_root / "uv.lock").is_file():
            shutil.copy(repo_root / "uv.lock", dest / "uv.lock")
    else:
        from . import __version__

        (dest / "pyproject.toml").write_text(
            "[project]\nname = \"dreamteam\"\nversion = \"" + __version__ + "\"\nrequires-python = \">=3.11\"\n"
            "dependencies = " + json.dumps(_installed_dependencies()) + "\n[project.scripts]\n"
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
    # the skills variables and the folder of the [api] folders roots are set by sandbox_command; a host value passed
    # by name would come later and win. The hub token is run_sandbox's own, never the host's, and the API token's
    # digest comes from run_sandbox alone
    names -= SKILLS_ENV | TOKEN_NAMES | {API_FOLDERS_ENV}
    return sorted(n for n in names if n and n in os.environ)


CONTAINER_PORT = 8765
HOST_SKILLS_MOUNT = "/dreamteam/host-skills"        # the host's skills library inside the sandbox (read-only)
# the sandbox's own, writable library (in the mounted home). Not .dreamteam/skills, the default library's name: a
# home mounted from the user's home folder would make the host's own library writable there
SANDBOX_SKILLS_DIR = "/swarm/.dreamteam/skills-library"
SANDBOX_ENVS_DIR = "/dreamteam/skill-envs"   # the skills' dependency environments: a named volume per home
SKILLS_ENV = frozenset({"DREAMTEAM_SKILLS_DIR", "DREAMTEAM_HOST_SKILLS_DIR", "DREAMTEAM_SKILL_ENVS_DIR",
                        "DREAMTEAM_MCP_LIBRARY_DIR", "UV_CACHE_DIR"})
# the hub token's and the API token's names (hubauth.py): never passed from the host's environment, never inherited
# by the docker command; run_sandbox sets the two digests itself
TOKEN_NAMES = frozenset({hubauth.TOKEN_ENV, hubauth.DIGEST_ENV, hubauth.API_TOKEN_ENV, hubauth.API_DIGEST_ENV})
# the roots of a home's [api] folders inside the sandbox, one folder per root's name (docs/SPEC.md §21.10 "Sandbox"):
# config.API_FOLDERS_ENV names it to the hub there
API_FOLDERS_MOUNT = "/dreamteam/api-folders"


@dataclass(frozen=True)
class FolderMount:
    """A root of a home's `[api] folders` that its sandbox mounts (api_folder_mounts): the root's name, its host
    folder (resolved) and whether the container may only read it (a root without `in_place`)."""

    name: str
    source: PurePath
    read_only: bool

    @property
    def target(self) -> str:
        return f"{API_FOLDERS_MOUNT}/{self.name}"

    @property
    def volume(self) -> str:
        """`docker run -v`'s argument."""
        return f"{self.source}:{self.target}" + (":ro" if self.read_only else "")


_LETTER_DRIVE = re.compile(r"[A-Za-z]:")


def volume_source_refusal(path: PurePath) -> str | None:
    """Why `docker run -v SOURCE:TARGET[:ro]` cannot take `path` as its source, as the words of a warning; None when
    it can. docker splits the argument at ':' and knows only a Windows drive letter's: a Windows path must be on one
    (not a network share's `\\\\server\\share\\...`, which a root on a mapped network drive resolves to, nor a
    `\\\\?\\` path), and no other ':' may follow (a POSIX folder name with ':' in it would be cut there). Such a
    source would most likely make the whole `docker run` fail, not that root alone."""
    text = str(path)
    if isinstance(path, PureWindowsPath):
        if not _LETTER_DRIVE.fullmatch(path.drive):
            return ("docker's -v takes a Windows folder only by a drive letter's path, not a network share's or a "
                    "\\\\?\\ one: use a folder on a local drive.")
        text = text[len(path.drive):]
    if ":" in text:
        return "docker's -v cannot take a folder whose path has ':' in it."
    return None


def api_folder_mounts(project_dir: Path, config: Config) -> tuple[list[FolderMount], list[str]]:
    """The roots of a home's `[api] folders` that its sandbox mounts, by name, and warning lines about the others
    (docs/SPEC.md §21.10 "Sandbox"). Each root is checked on the host as the hub outside the container checks it at
    each use (apifolders.resolve_root: absolute on this computer, there, a folder, and none root_refusal refuses: a
    drive's root, the user's home folder, the DreamTeam home, DreamTeam's own folders, another home...): inside, the
    hub sees only the mount point and cannot tell what the host folder was. A root missing on the host, a refused one,
    and one docker's `-v` cannot name (volume_source_refusal) are not mounted. Each is mounted as resolved, read-only
    unless `in_place`; a read-only one inside a writable one gets a warning too (agents write it through the other).
    Blocking (the roots are resolved; one may lie on a share)."""
    from .apifolders import resolve_root
    from .copyfrom import CopyError
    from .treecopy import is_within

    mounts: list[FolderMount] = []
    notes: list[str] = []
    for name, folder in sorted(config.api.folders.items()):
        try:
            real = resolve_root(folder, home=project_dir, config=config)
        except CopyError as exc:
            notes.append(f"Warning: [api.folders.{name}] ({folder.path}) is not mounted into the container: {exc}")
            continue
        why = volume_source_refusal(real)
        if why is not None:
            notes.append(f"Warning: [api.folders.{name}] ({real}) is not mounted into the container: {why}")
            continue
        mounts.append(FolderMount(name, real, read_only=not folder.in_place))
    for mount in mounts:
        over = next((other.name for other in mounts if mount.read_only and not other.read_only
                     and is_within(mount.source, other.source)), None)
        if over is not None:
            notes.append(f"Warning: [api.folders.{mount.name}] is mounted read-only, but it lies inside "
                         f"[api.folders.{over}], which is mounted writable: agents in the container can write it "
                         "there.")
    return mounts, notes


def api_root_of(folder: str) -> str | None:
    """The name of the root of [api] folders whose mount point in the sandbox (API_FOLDERS_MOUNT/<name>) holds the
    stored folder `folder`, as the hub in the container stores it; None for any other path (a host path among them)."""
    parts, base = PurePosixPath(folder).parts, PurePosixPath(API_FOLDERS_MOUNT).parts
    if len(parts) <= len(base) or parts[:len(base)] != base or ".." in parts:
        return None
    return parts[len(base)]


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


def outside_home_warning(home: Path, mounts: Iterable[FolderMount] = ()) -> list[str]:
    """Lines warning about the sessions of a home that work in a folder outside it: only the home is mounted
    into the container, so they cannot start there (empty when there are none). A folder inside the home is
    stored relative to it as well (session.json "in_home"), so it is found where the container mounts the home.
    `mounts`: the roots of [api] folders the sandbox mounts (api_folder_mounts). A session made in the container in
    a folder under a writable one (`in_place`) stored it under that root's mount point (api_root_of) and finds it
    there again: it is left out (docs/SPEC.md §21.10 "Sandbox"). One under a root mounted read-only stays listed (it
    works in place, and its start refuses a root that is no longer `in_place`), as does one made outside the container
    (it stored the host's path)."""
    from .sessions import external_workspaces

    mounts = list(mounts)
    writable = {mount.name for mount in mounts if not mount.read_only}
    found = [(sid, folder) for sid, folder in external_workspaces(home) if api_root_of(folder) not in writable]
    if not found:
        return []
    head = (f"Warning: only {home} and the in_place roots of [api] folders are mounted writable into the container; "
            "these sessions work in a folder outside them and cannot start there:" if mounts else
            f"Warning: only {home} is mounted into the container; these sessions work in a folder outside it and "
            "cannot start there:")
    return ([head] + [f"  {sid}: {folder}" for sid, folder in found]
            + ["  Move them into the home first (/cd <folder inside the home>, or /cd --default)."])


def sandbox_command(project_dir: Path, port: int, config: Config, goal: str | None = None,
                    image: str = IMAGE, docker: str = "docker", tty: bool = False,
                    home: bool | None = None, firewall: bool = False, network: str | None = None,
                    api_token: bool = False, folders: Sequence[FolderMount] | None = None) -> list[str]:
    """`docker run` argv for the sandbox. The hub inside listens on CONTAINER_PORT, published on the host's
    127.0.0.1:port only; DREAMTEAM_PUBLIC_URL tells it that host URL, so the board shows the right MCP URL
    and hub.json (in the mounted project) lets host-side `dreamteam mcp/console/status` find it.
    `home`: serve the directory as a launcher home (True) or `run` it as a single-swarm project (False);
    None decides by its sessions/ directory. `firewall`: the container may set up the firewall of the sessions
    without network (NET_ADMIN, which DreamTeam drops right after: netoff.sandbox_entry). `network`: `--network`
    of the command inside (a project's run, or the session `goal` creates).

    The hub token's SHA-256 (hubauth.DIGEST_ENV) is passed by name, its value from the environment of the docker
    command (run_sandbox): the hub has to listen on 0.0.0.0 (a published port reaches the container's interface, never
    its loopback), and so every container on the same bridge reaches it too unless it asks for the token. Only the
    digest: the container's environment never holds the token itself. `api_token`: the host has an API token
    (hubauth.api_digest), whose SHA-256 (hubauth.API_DIGEST_ENV) is passed the same way to a home's hub, the only one
    that serves the external API (docs/SPEC.md §21.1).

    A home's hub also gets the roots of its `[api] folders` (docs/SPEC.md §21.10 "Sandbox"): `folders` (as
    api_folder_mounts gives them; None: computed here, the warnings dropped), each mounted at
    API_FOLDERS_MOUNT/<name>, read-only unless `in_place`, and API_FOLDERS_ENV naming that folder, so the hub there
    takes its roots from it (apifolders.roots). Every agent in the container can then read those folders, and write
    the `in_place` ones."""
    public = f"http://127.0.0.1:{port}"
    # --init: a tiny init (tini) is PID 1 and reaps orphans (processes the agents' commands leave running), so a
    # zombie never keeps a process group alive; it forwards SIGTERM/SIGINT to dreamteam. Ports that agents' dev
    # servers open inside the container are not published.
    # --security-opt no-new-privileges: no program in the container gains privileges through a set-user-ID bit or
    # file capabilities; the firewall's NET_ADMIN (given at the start) is kept, and setpriv only drops privileges.
    cmd = [docker, "run", "--rm", "--init", "--security-opt", "no-new-privileges:true", "--name",
           container_name(port), "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}", "-v",
           f"{project_dir.resolve()}:/swarm", "-e", f"DREAMTEAM_PUBLIC_URL={public}", "-e", hubauth.DIGEST_ENV]
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
    if home:  # the external API: a home's hub serves it (a single-swarm project's `run` has none)
        if api_token:
            cmd += ["-e", hubauth.API_DIGEST_ENV]
        # its server folders, and their variable even with no root: a root added while the container runs is then
        # looked for there, never at its host path
        for mount in api_folder_mounts(project_dir, config)[0] if folders is None else folders:
            cmd += ["-v", mount.volume]
        cmd += ["-e", f"{API_FOLDERS_ENV}={API_FOLDERS_MOUNT}"]
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


def container_name(port: int) -> str:
    return f"dreamteam-{port}"


def container_exists(name: str) -> bool:
    try:
        result = subprocess.run([_docker(), "container", "inspect", name], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return result.returncode == 0


def access_lines(project_dir: Path, port: int, token: str, stored: Path | None, home: bool,
                 api: bool = False) -> list[str]:
    """What `dreamteam sandbox` prints: the board URL with its one-time `?token=` (the hub turns it into a cookie),
    the MCP URL with the header Claude Code needs, and how DreamTeam's commands on the host get the token. `api`: a
    home's hub with an API token (docs/SPEC.md §21.1): the external API's URL too, with the header it needs, which
    names the variable, never the token or its digest."""
    base = f"http://127.0.0.1:{port}"
    where = f'--home "{project_dir}"' if home else f'"{project_dir}"'
    lines = [f"Swarm UI: {base}/?{hubauth.QUERY_PARAM}={token}",
             f"MCP:      {base}/mcp  (header Authorization: Bearer {token})",
             f'  claude mcp add --transport http dreamteam {base}/mcp --header "Authorization: Bearer {token}"',
             "  The token is new for every start of the sandbox; a registration that survives restarts:",
             f"  claude mcp add dreamteam -- dreamteam mcp {where}"]
    if api:
        lines.append(f"API:      {base}/api/v1  (header Authorization: Bearer <your {hubauth.API_TOKEN_ENV}>)")
    if stored is not None:
        lines.append(f"`dreamteam console`, `tui`, `mcp` and `status` on this computer read the token from {stored}.")
    else:
        lines.append(f"Warning: {hubauth.tokens_dir()} is inside a mounted folder, so the token is not stored: set "
                     f"{hubauth.TOKEN_ENV} to it for `dreamteam console`, `tui`, `mcp` and `status`.")
    return lines


def run_sandbox(project_dir: Path, port: int, build: bool = False, goal: str | None = None,
                image: str = IMAGE, home: bool | None = None, network: str | None = None) -> int:
    config = Config.load(project_dir)
    if container_exists(container_name(port)):  # (its stored token must not be replaced by one nobody asks for)
        print(f"A DreamTeam sandbox already runs on port {port} (container {container_name(port)}): stop it first, "
              "or choose another --port.", file=sys.stderr)
        return 1
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
    mounts, folder_notes = api_folder_mounts(project_dir, config) if is_home else ([], [])
    if is_home:
        for line in outside_home_warning(project_dir, mounts):
            print(line, file=sys.stderr)
    for line in library_inside_warning(project_dir, config):
        print(line, file=sys.stderr)
    for line in folder_notes:
        print(line, file=sys.stderr)
    if mounts:   # (a grant to every agent in the container, docs/SPEC.md §21.10 "Sandbox": said where it is made)
        shown = ", ".join(f"{m.name} ({'read-only' if m.read_only else 'writable'})" for m in mounts)
        print(f"[api] folders mounted into the container: {shown}. Every agent of every session there can read "
              "them, and write the writable ones.", file=sys.stderr)
    from .cli import _stdin_is_terminal  # (NUL claims to be a tty on Windows: `docker run -it` would refuse it)

    # Docker would create a missing host folder of a bind mount itself, owned by root on Linux
    sandbox_host_library(config).mkdir(parents=True, exist_ok=True)

    # the API token's SHA-256 from hubauth: main() has already taken both of its names out of os.environ
    api_digest = hubauth.api_digest() if is_home else None
    cmd = sandbox_command(project_dir, port, config, goal, image, docker=_docker(), tty=_stdin_is_terminal(),
                          home=home, firewall=firewall, network=network, api_token=api_digest is not None,
                          folders=mounts)
    if firewall:
        print("Sessions without network: their agents' commands run as a separate user whose network the "
              "container's firewall closes (NET_ADMIN only for setting it up).", file=sys.stderr)
    # a fresh token per container: kept on the host outside every mounted folder (the agents must not read it
    # there; the [api] folders roots too, though api_folder_mounts already refuses one around the tokens); the hub
    # gets only its SHA-256, through docker's environment, never on a command line
    token = hubauth.new_token()
    stored = hubauth.store_token(port, token, project_dir,
                                 also_mounted=[sandbox_host_library(config), *(mount.source for mount in mounts)])
    env = {k: v for k, v in os.environ.items() if k not in TOKEN_NAMES}
    env[hubauth.DIGEST_ENV] = hubauth.digest(token)
    if api_digest is not None:
        env[hubauth.API_DIGEST_ENV] = api_digest
    try:
        for line in access_lines(project_dir.resolve(), port, token, stored, is_home, api=api_digest is not None):
            print(line, file=sys.stderr)
        return subprocess.call(cmd, env=env)
    finally:
        hubauth.forget_token(port, token)
