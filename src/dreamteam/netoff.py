"""A session without network ([swarm] network = "off", docs/SPEC.md §7.2): the members' shell commands, their
background jobs and the stdio MCP servers DreamTeam starts are cut off from it; DreamTeam itself (the LLM calls, the
hub's port, the user's skill installs) keeps it.

How, depends on what the machine allows (`isolation()`, detected once per process):

* "docker": inside the Docker sandbox started with the firewall (`dreamteam sandbox`, docker.py): the commands run as
  an unprivileged user (the mounted folder's owner, else OFFLINE_USER: sandbox_ids), whose packets the container's
  firewall rejects except on loopback, the hub's port excepted (`sandbox_entry`, run by the CLI before anything
  else);
* "netns": Linux with user namespaces: the commands run in a network namespace of their own, which has only
  loopback: one per DreamTeam process, which all the offline commands join (netns_helper.py), so that a server one of
  them starts is reachable from the others; the machine's own localhost (the hub) is not;
* "env": anywhere else (Windows, macOS, Linux without user namespaces, a container without the firewall): best
  effort, not isolation. Proxy variables point at a closed local port and the package managers and git are told to
  stay offline; a program that ignores them (a raw socket, Windows PowerShell 5.1's web cmdlets) still gets out, and
  the hub's HTTP API stays reachable.

The variables of "env" are set in the other two as well: pip or npm then fail at once instead of retrying. Loopback
works in every mode (NO_PROXY lists it): local servers and the tests that use them keep working."""

from __future__ import annotations

import errno
import logging
import os
import re
import select
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

NETWORK_MODES = ("on", "off")
OFFLINE_USER = "dreamteam-agent"            # the sandbox image's user for the commands of sessions without network
OFFLINE_USER_ENV = "DREAMTEAM_OFFLINE_USER"  # "UID:GID", set by sandbox_entry once the firewall is in place
FIREWALL_ENV = "DREAMTEAM_OFFLINE_FIREWALL"  # docker.sandbox_command: set it up (the container has NET_ADMIN)
CHAIN = "DREAMTEAM_OFFLINE"
DOCKER_DNS = "127.0.0.11"                    # Docker's embedded DNS server on a user-defined network: on loopback
DEAD_PROXY = "http://127.0.0.1:9"            # the discard port: closed, so a connection is refused at once
LOOPBACK_HOSTS = "localhost,127.0.0.1,::1"
HELPER = Path(__file__).with_name("netns_helper.py")
PROBE_TIMEOUT = 15.0
_POSIX = os.name == "posix"

KINDS = ("docker", "netns", "env")
# git: every network transport refused (local paths and file:// still work); GIT_CONFIG_COUNT needs git 2.31
GIT_OFFLINE = (("protocol.http.allow", "never"), ("protocol.https.allow", "never"), ("protocol.git.allow", "never"),
               ("protocol.ssh.allow", "never"), ("http.proxy", DEAD_PROXY))
OFFLINE_ENV = {
    "PIP_NO_INDEX": "1", "PIP_RETRIES": "0", "UV_OFFLINE": "1", "npm_config_offline": "true",
    "YARN_ENABLE_OFFLINE_MODE": "1", "CARGO_NET_OFFLINE": "true", "GOPROXY": "off", "HF_HUB_OFFLINE": "1",
    "CONDA_OFFLINE": "true", "DREAMTEAM_NETWORK": "off",
}
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")

_lock = threading.Lock()
_detected: dict[str, Any] = {}
_ns_fds: list[int] = []   # "netns": this process's fds of the namespaces the commands join


def is_off(config: Any) -> bool:
    """[swarm] network = "off" (a config object, or anything without the setting: on)."""
    return getattr(getattr(config, "swarm", None), "network", "on") == "off"


# ---- detection --------------------------------------------------------------------------------------------------
def offline_user() -> tuple[int, int] | None:
    """The sandbox's (uid, gid) for offline commands (OFFLINE_USER_ENV "UID:GID", sandbox_ids), when the firewall for
    it is in place (and this process may switch to it: root)."""
    value = os.environ.get(OFFLINE_USER_ENV) or ""
    if not _POSIX or os.geteuid() != 0 or not shutil.which("setpriv"):
        return None
    try:
        uid, gid = (int(part) for part in value.split(":"))
    except ValueError:
        return None
    return (uid, gid) if uid > 0 and gid > 0 else None


def _hold_namespace(unshare: str) -> list[int] | None:
    """Make the offline sessions' namespace: a holder process (`unshare -rn` netns_helper.py --hold) creates a user
    and a network namespace and brings the loopback up; this process opens both (fds: [user, net]) and keeps them,
    and the holder exits (an agent's `pkill python` cannot take the namespace away). None when that fails."""
    try:
        holder = subprocess.Popen([unshare, "-rn", sys.executable, "-I", str(HELPER), "--hold"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        log.info("no network namespaces for offline sessions: %s", exc)
        return None
    fds: list[int] = []
    try:
        ready = select.select([holder.stdout], [], [], PROBE_TIMEOUT)[0]
        line = holder.stdout.readline().strip() if ready else b""
        if line == b"ok":
            for kind in ("user", "net"):
                fds.append(os.open(f"/proc/{holder.pid}/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC))
            return fds
        detail = line.decode("utf-8", "replace") or ("no answer" if not ready else "")
    except OSError as exc:
        detail = str(exc)
        for fd in fds:
            os.close(fd)
    finally:
        try:   # stdin closed: the holder's cue to exit
            err = holder.communicate(input=b"", timeout=5)[1]
        except subprocess.TimeoutExpired:
            holder.kill()
            err = holder.communicate()[1]
    detail = (detail + " " + err.decode("utf-8", "replace")).strip()[:300]
    log.info("no network namespaces for offline sessions: %s", detail or f"exit code {holder.returncode}")
    return None


def _probe_netns() -> list[str] | None:
    """The argv prefix of a command in the offline sessions' network namespace, when `unshare -rn` and the helper
    work here. One namespace per process (netns_helper.py): what one command serves on its localhost, the next one
    and the MCP servers reach. The commands join it through this process's fds (/proc/<pid>/fd/N)."""
    unshare = shutil.which("unshare")
    if not sys.platform.startswith("linux") or not unshare or not HELPER.is_file():
        return None
    fds = _hold_namespace(unshare)
    if fds is None:
        return None
    prefix = [sys.executable, "-I", str(HELPER), "--join", *(f"/proc/{os.getpid()}/fd/{fd}" for fd in fds),
              str(os.getuid()), str(os.getgid())]
    try:
        proc = subprocess.run([*prefix, "--probe"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=PROBE_TIMEOUT)
        detail = "" if proc.returncode == 0 and proc.stdout.strip() == b"ok" else \
            (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()[:300] or f"exit code {proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc)
    if detail:
        log.info("no network namespaces for offline sessions: %s", detail)
        for fd in fds:
            os.close(fd)
        return None
    _ns_fds[:] = fds
    return [*prefix, "--"]


def isolation() -> str:
    """The isolation a session without network gets on this machine: "docker", "netns" or "env" (module doc).
    Detected once (on Linux it makes the namespace: two short processes); thread-safe."""
    with _lock:
        if "kind" not in _detected:
            ids = offline_user()
            prefix = None if ids else _probe_netns()
            if ids:
                _detected.update(kind="docker", uid=ids[0], gid=ids[1],
                                 prefix=["setpriv", f"--reuid={ids[0]}", f"--regid={ids[1]}", "--clear-groups",
                                         "--no-new-privs", "--"])
            elif prefix:
                _detected.update(kind="netns", prefix=prefix)
            else:
                _detected.update(kind="env", prefix=[])
            log.info("sessions without network: isolation %s", _detected["kind"])
        return _detected["kind"]


def detected() -> str | None:
    """isolation() when it is known already (None: not detected yet), without probing."""
    return _detected.get("kind")


def reset() -> None:
    """Forget the detection (tests)."""
    with _lock:
        _detected.clear()
        for fd in _ns_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        _ns_fds.clear()


# ---- a command ---------------------------------------------------------------------------------------------------
def environment(env: dict[str, str]) -> dict[str, str]:
    """`env` for a command without network: proxies at a closed port (loopback exempt), the package managers
    offline, git's network transports refused; in the sandbox the offline user's home."""
    out = dict(env)
    for name in PROXY_NAMES:
        out[name] = out[name.lower()] = DEAD_PROXY
    out["NO_PROXY"] = out["no_proxy"] = LOOPBACK_HOSTS
    out.update(OFFLINE_ENV)
    try:
        count = max(int(out.get("GIT_CONFIG_COUNT") or 0), 0)
    except ValueError:
        count = 0
    for i, (key, value) in enumerate(GIT_OFFLINE, count):
        out[f"GIT_CONFIG_KEY_{i}"], out[f"GIT_CONFIG_VALUE_{i}"] = key, value
    out["GIT_CONFIG_COUNT"] = str(count + len(GIT_OFFLINE))
    if isolation() == "docker":
        home = _offline_home()
        # root's home and the skills' uv cache are not the offline user's to write
        out.update(HOME=home, USER=OFFLINE_USER, LOGNAME=OFFLINE_USER, UV_CACHE_DIR=f"{home}/.cache/uv")
    return out


def wrap(argv: list[str] | str) -> list[str] | str:
    """`argv` run without network (a string, cmd.exe's command line on Windows, only gets the "env" isolation)."""
    isolation()
    prefix = _detected["prefix"]
    if not prefix or isinstance(argv, str):
        return argv
    return [*prefix, *argv]


def command(config: Any, argv: list[str] | str, env: dict[str, str]) -> tuple[list[str] | str, dict[str, str]]:
    """(argv, env) of an agent's command, a job or an MCP server: unchanged while the session has network."""
    if not is_off(config):
        return argv, env
    return wrap(argv), environment(env)


def in_container() -> bool:
    """Whether this process runs in a container (Docker's /.dockerenv, Podman's /run/.containerenv)."""
    return _POSIX and (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"))


def shown(kind: str | None) -> str | None:
    """An isolation kind as the UI shows it: "env" in a container is "container", which a restart of the sandbox
    turns into "docker" (`dreamteam sandbox` adds the firewall when a session needs it)."""
    return "container" if kind == "env" and in_container() else kind


def describe(config: Any) -> dict[str, Any]:
    """The state for the UI: {"mode": "on"|"off", "isolation": shown(kind) or None (not detected yet, or network
    on)}."""
    off = is_off(config)
    return {"mode": "off" if off else "on", "isolation": shown(detected()) if off else None}


# ---- MCP servers -------------------------------------------------------------------------------------------------
_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s'\"<>]+")


def remote_url(argv: list[str]) -> str | None:
    """The first URL of a server outside this machine in an MCP server's command (a stdio bridge to a remote server,
    such as `npx mcp-remote https://...`), else None."""
    for arg in argv:
        for url in _URL.findall(str(arg)):
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                return url
            if host not in ("localhost", "::1") and not host.startswith("127.") and not host.endswith(".localhost"):
                return url
    return None


# ---- the Docker sandbox ------------------------------------------------------------------------------------------
SANDBOX_ROOT = "/swarm"   # the sandbox's mounted home or project (docker.sandbox_command)


def _has_global_ipv6() -> bool:
    try:
        lines = Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines()
    except OSError:
        return False
    return any(line.split()[-1] != "lo" for line in lines if line.split())


def _offline_home() -> str:
    import pwd

    try:
        return pwd.getpwnam(OFFLINE_USER).pw_dir
    except KeyError:
        return "/tmp"


def sandbox_ids(root: str = SANDBOX_ROOT) -> tuple[int, int]:
    """The (uid, gid) the offline commands run as: the owner of the mounted folder when it is not root (a bind mount
    on a Linux host: the user's own files stay theirs and nothing in them is opened to another id), else the image's
    OFFLINE_USER (Docker Desktop shows a bind mount as root's; a named volume is root's). LookupError: neither."""
    import pwd

    try:
        st = os.stat(root)
    except OSError:
        st = None
    if st is not None and st.st_uid != 0:
        return st.st_uid, st.st_gid or st.st_uid
    try:
        entry = pwd.getpwnam(OFFLINE_USER)
    except KeyError:
        raise LookupError(f"the image has no user {OFFLINE_USER} (rebuild it: dreamteam sandbox --build)") from None
    return entry.pw_uid, entry.pw_gid


def hub_ports(argv: list[str]) -> list[int]:
    """The ports DreamTeam's hub listens on in the container: docker.CONTAINER_PORT and a `--port` of the command."""
    from .docker import CONTAINER_PORT

    ports = {CONTAINER_PORT}
    for i, arg in enumerate(argv):
        value = argv[i + 1] if arg == "--port" and i + 1 < len(argv) else arg[7:] if arg.startswith("--port=") else ""
        if value.isdigit() and 0 < int(value) < 65536:
            ports.add(int(value))
    return sorted(ports)


def _jump(uid: int) -> list[str]:
    return ["-m", "owner", "--uid-owner", str(uid), "-j", CHAIN]


def firewall_commands(tool: str, uid: int, ports: list[int] = ()) -> list[list[str]]:
    """iptables / ip6tables commands of the offline user's chain: the hub's ports rejected (its HTTP API would let a
    command switch its session's network back on), Docker's DNS server rejected (it would resolve names for it), the
    rest of loopback accepted, everything else rejected."""
    rules = [[tool, "-w", "-N", CHAIN]]
    rules += [[tool, "-w", "-A", CHAIN, "-p", "tcp", "--dport", str(port), "-j", "REJECT"] for port in ports]
    if Path(tool).name == "iptables":
        rules.append([tool, "-w", "-A", CHAIN, "-d", DOCKER_DNS, "-j", "REJECT"])
    rules += [[tool, "-w", "-A", CHAIN, "-o", "lo", "-j", "ACCEPT"],
              [tool, "-w", "-A", CHAIN, "-j", "REJECT"],
              [tool, "-w", "-I", "OUTPUT", "1", *_jump(uid)]]
    return rules


def setup_firewall(uid: int, ports: list[int] = ()) -> str | None:
    """Reject the network of `uid` except loopback (IPv4 and, when the container has it, IPv6). None on success
    or when it is in place already (`docker exec <container> dreamteam ...` runs the entry again), else why not."""
    for tool in ("iptables", "ip6tables"):
        exe = shutil.which(tool)
        if exe is None:
            if tool == "ip6tables" and not _has_global_ipv6():
                continue
            return f"{tool} is not installed in the image (rebuild it: dreamteam sandbox --build)"
        check = subprocess.run([exe, "-w", "-C", "OUTPUT", *_jump(uid)], stdin=subprocess.DEVNULL,
                               capture_output=True)
        if check.returncode == 0:
            continue
        for argv in firewall_commands(exe, uid, ports):
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True)
            if proc.returncode != 0:
                if tool == "ip6tables" and not _has_global_ipv6():
                    break  # no IPv6 in this container: nothing to block there
                error = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:300]
                return f"{' '.join(argv[2:])}: {error or f'exit code {proc.returncode}'}"
    return None


def _give_home(uid: int, gid: int) -> None:
    """The image's home of OFFLINE_USER to the offline ids (in the container only; they differ from the image's when
    the mounted folder has an owner of its own, sandbox_ids)."""
    home = _offline_home()
    if home == "/tmp" or not os.path.isdir(home) or os.stat(home).st_uid == uid:
        return
    for _folder, dirs, files, dirfd in os.fwalk(home, follow_symlinks=False):
        os.chown(dirfd, uid, gid)
        for name in dirs + files:
            os.chown(name, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def sandbox_entry() -> None:
    """In the sandbox container (FIREWALL_ENV, root), before the CLI does anything: set up the firewall of the offline
    user (sandbox_ids), then run DreamTeam again without NET_ADMIN and with the offline user's group (setpriv), with
    OFFLINE_USER_ENV telling it the firewall is in place and a umask that lets the offline user write what DreamTeam
    creates in the workspaces (prepare_workspace does it for what is there). Without the firewall the sessions
    without network get the best-effort isolation only, and a warning says so."""
    if os.environ.get(FIREWALL_ENV) != "1" or os.environ.get(OFFLINE_USER_ENV) or not _POSIX:
        return
    os.environ.pop(FIREWALL_ENV, None)
    if os.geteuid() != 0:
        print("Warning: network off: the firewall needs root in the container; sessions without network get the "
              "best-effort isolation only.", file=sys.stderr)
        return
    try:
        uid, gid = sandbox_ids()
        problem = setup_firewall(uid, hub_ports(sys.argv[1:]))
    except LookupError as exc:
        problem = str(exc)
    if problem is not None:
        print(f"Warning: network off: the firewall could not be set up ({problem}); sessions without network get the "
              "best-effort isolation only.", file=sys.stderr)
        return
    try:
        _give_home(uid, gid)
    except OSError as exc:
        log.warning("cannot give %s to the offline user: %s", _offline_home(), exc)
    os.environ[OFFLINE_USER_ENV] = f"{uid}:{gid}"
    os.umask(0o002)
    setpriv = shutil.which("setpriv")
    if setpriv is None:  # (the hub keeps NET_ADMIN then; the offline user never has it)
        print("Warning: setpriv is missing: DreamTeam keeps NET_ADMIN in the container.", file=sys.stderr)
        return
    sys.stdout.flush()
    sys.stderr.flush()
    # the hub's group is the offline user's: what DreamTeam creates is the commands' to write (umask 002), and no
    # folder of the user's needs a set-group-id bit for that
    os.execv(setpriv, [setpriv, "--bounding-set=-net_admin", f"--regid={gid}", "--keep-groups", "--",
                       sys.executable, "-m", "dreamteam", *sys.argv[1:]])


def prepare_workspace(root: Path) -> int:
    """Docker isolation: open to the offline user what root created in the workspace (DreamTeam before, or a run
    with network) or the image's OFFLINE_USER did: such an entry gets the offline group with the owner's bits copied
    to the group (a read-only entry, e.g. the skills mirror, stays read-only). What the offline user owns already
    (the user's own files of a bind mount on Linux, sandbox_ids) and what anyone else owns is left as it is. Walked
    by file descriptors, never following a symlink: the members' commands may change the tree meanwhile. Returns the
    number of entries it could not change (logged); 0 when there is nothing to do."""
    if detected() != "docker" or not _POSIX:
        return 0
    import stat

    uid, gid = _detected["uid"], _detected["gid"]
    try:
        import pwd

        image_uid = pwd.getpwnam(OFFLINE_USER).pw_uid
    except (ImportError, KeyError):
        image_uid = -1
    failed = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)

    def fix(name: str, dirfd: int | None) -> None:
        nonlocal failed
        try:
            fd = os.open(name, flags, dir_fd=dirfd)
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.ENXIO, errno.ENOENT):   # a symlink, a socket, gone meanwhile
                failed += 1
                if failed <= 3:
                    log.warning("cannot open %s to the offline user: %s", name, exc)
            return
        try:
            st = os.fstat(fd)
            if (not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)) or st.st_uid == uid
                    or st.st_uid not in (0, image_uid)):
                return
            if st.st_gid != gid:
                os.fchown(fd, -1, gid)
            mode = stat.S_IMODE(st.st_mode)
            wanted = mode | ((mode & 0o700) >> 3)
            if wanted != mode:
                os.fchmod(fd, wanted)
        except OSError as exc:
            failed += 1
            if failed <= 3:
                log.warning("cannot open %s to the offline user: %s", name, exc)
        finally:
            os.close(fd)

    fix(str(root), None)
    for _folder, dirs, files, dirfd in os.fwalk(root, follow_symlinks=False):
        for name in dirs + files:
            fix(name, dirfd)
    return failed
