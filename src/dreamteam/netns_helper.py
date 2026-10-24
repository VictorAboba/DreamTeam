"""The network namespace of the sessions without network on Linux (netoff.py, docs/SPEC.md §7.2). One namespace per
DreamTeam process, made once and joined by every offline command: a dev server one command starts as a background
job is then reachable from the next command, its tests and the MCP servers, as on the machine's own localhost.

    unshare -rn python -I netns_helper.py --hold
        inside the new user and network namespace: bring its loopback (down at first) up, print "ok", then wait
        until stdin closes (netoff opens the namespaces by /proc/PID/ns/* meanwhile and keeps them open)
    python -I netns_helper.py --join USERNS NETNS UID GID [--probe] -- PROGRAM [ARGS...]
        join those namespaces (USERNS, NETNS: their paths, /proc/<DreamTeam's pid>/fd/N), make the real user and
        group themselves again (uid 0 of the namespace would let a command write what its user cannot, and tools
        behave differently as root), then exec the command; --probe prints "ok" when every step works instead

Standard library only, and run with `python -I`: nothing is imported from the workspace or the environment. When
--join cannot enter the namespaces the command does not run (it would have the network); when only the user's ids
cannot be made themselves again, that is reported on stderr and the command runs (the network is gone already)."""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import os
import socket
import struct
import sys

SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
IFF_UP = 0x1
IFF_RUNNING = 0x40
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
_IFREQ = "16sH14x"


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


def loopback_up() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        flags = struct.unpack(_IFREQ, fcntl.ioctl(sock, SIOCGIFFLAGS, struct.pack(_IFREQ, b"lo", 0)))[1]
        fcntl.ioctl(sock, SIOCSIFFLAGS, struct.pack(_IFREQ, b"lo", flags | IFF_UP | IFF_RUNNING))


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="ascii") as f:
        f.write(text)


def join(userns: str, netns: str) -> None:
    """Enter the holder's user namespace (its owner, this user, has every capability there), then its network
    namespace (which needs CAP_SYS_ADMIN in it). The user namespace first: setns into the network one checks the
    capability in the caller's own user namespace too."""
    libc = _libc()
    # both opened first: inside the user namespace, DreamTeam's /proc/<pid>/fd is no longer this process's to open
    fds = [(os.open(path, os.O_RDONLY | os.O_CLOEXEC), path, kind)
           for path, kind in ((userns, CLONE_NEWUSER), (netns, CLONE_NEWNET))]
    try:
        for fd, path, kind in fds:
            if libc.setns(fd, kind) != 0:
                err = ctypes.get_errno()
                raise OSError(err, f"setns({path}): {os.strerror(err)}")
    finally:
        for fd, _path, _kind in fds:
            os.close(fd)


def map_back(uid: int, gid: int) -> None:
    """A nested user namespace in which the real uid and gid (0 in this one) are themselves again. Without
    CAP_SETGID in this namespace afterwards, setgroups must be denied before gid_map may be written."""
    if uid == 0 and gid == 0:
        return
    if _libc().unshare(CLONE_NEWUSER) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"unshare(CLONE_NEWUSER): {os.strerror(err)}")
    _write("/proc/self/uid_map", f"{uid} 0 1\n")
    _write("/proc/self/setgroups", "deny")
    _write("/proc/self/gid_map", f"{gid} 0 1\n")


def _loopback_works() -> bool:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        socket.create_connection(server.getsockname(), timeout=5).close()
    return True


def hold() -> int:
    try:
        loopback_up()
    except OSError as exc:
        print(f"failed: loopback: {exc}", flush=True)
        return 1
    print("ok", flush=True)
    sys.stdin.read()   # until netoff closes it (or dies)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--hold"]:
        return hold()
    try:
        assert argv[0] == "--join"
        userns, netns, uid, gid = argv[1], argv[2], int(argv[3]), int(argv[4])
    except (AssertionError, IndexError, ValueError):
        print("usage: netns_helper.py --hold | --join USERNS NETNS UID GID [--probe] -- PROGRAM ARGS...",
              file=sys.stderr)
        return 2
    rest = argv[5:]
    probe = rest[:1] == ["--probe"]
    command = rest[1:] if probe else rest
    command = command[1:] if command[:1] == ["--"] else command
    try:
        join(userns, netns)
    except OSError as exc:  # still in DreamTeam's network: the command must not run
        print(("failed: " if probe else "dreamteam: cannot enter the session's network namespace: ") + str(exc),
              file=sys.stdout if probe else sys.stderr)
        return 1 if probe else 126
    try:
        map_back(uid, gid)
    except OSError as exc:  # the network is gone already: the command runs, as uid 0 of the namespace
        if probe:
            print(f"failed: {exc}")
            return 1
        print(f"dreamteam: the network namespace is incomplete ({exc})", file=sys.stderr)
    if probe:
        try:
            ok = os.getuid() == uid and _loopback_works()
        except OSError as exc:
            ok = False
            print(f"failed: loopback: {exc}")
        else:
            print("ok" if ok else f"failed: uid {os.getuid()}")
        return 0 if ok else 1
    if not command:
        return 2
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"dreamteam: cannot run {command[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
