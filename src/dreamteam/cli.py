"""`dreamteam` command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, roles
from .config import CONFIG_FILE, PROVIDER_PRESETS, Config, render_template
from .goal_text import split_goal
from .models import USER

log = logging.getLogger("dreamteam")


def setup_logging(project_dir: Path | None, quiet_stderr: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if project_dir is not None:
        log_dir = project_dir / ".dreamteam" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "dreamteam.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not quiet_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("httpx", "httpx2", "anthropic", "openai", "uvicorn.access", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---- hub discovery --------------------------------------------------------------------------------
def hub_file(project_dir: Path) -> Path:
    return project_dir / ".dreamteam" / "hub.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_hub_file(project_dir: Path) -> dict | None:
    try:
        info = json.loads(hub_file(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("base_url"), str):
        return None
    return info


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def hub_identity(base_url: str, timeout: float = 3.0) -> dict | None:
    """What the hub at base_url says about itself (GET /api/hub): {"pid", "project", "name", "version"}."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/hub", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def running_hub(project_dir: Path) -> str | None:
    """Base URL of a hub already serving this project, if any.

    hub.json can outlive its hub (crash, kill), its pid can be reused by an unrelated process and its port
    can be taken by the hub of another project, so the hub must confirm over /api/hub that it is the very
    process (pid) serving the very project that wrote the file. A hub running in Docker writes its own
    view of the project ("/swarm") and pid; only the identity check applies then."""
    info = _read_hub_file(project_dir)
    if info is None:
        return None
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    written_for = info.get("project")
    local = written_for is None or _same_path(str(written_for), str(Path(project_dir).resolve()))
    if local and not _pid_alive(pid):
        return None  # crashed or killed hub: stale file
    base = info["base_url"].rstrip("/")
    ident = hub_identity(base)
    if not ident or ident.get("pid") != pid:
        return None
    if written_for is not None and not _same_path(str(ident.get("project") or ""), str(written_for)):
        return None
    return base


def _write_hub_file(project_dir: Path, base: str, config: Config) -> None:
    path = hub_file(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"base_url": base, "mcp_url": base + "/mcp", "pid": os.getpid(), "project": str(config.project_dir)}
    tmp = path.with_name(f"hub.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _remove_hub_file(project_dir: Path) -> None:
    """Remove hub.json if this process wrote it (never another live hub's file)."""
    info = _read_hub_file(project_dir)
    if info is not None and info.get("pid") != os.getpid():
        return
    try:
        hub_file(project_dir).unlink()
    except OSError:
        pass


def write_launcher_marker(home: Path) -> Path | None:
    """Mark that this process runs sessions of `home`: also without a hub.json (`dreamteam mcp --no-web`, the
    TUI's in-process launcher without its web UI), `sessions rm` must not delete their files behind its back."""
    path = home / ".dreamteam" / f"launcher-{os.getpid()}.pid"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return None
    return path


def remove_launcher_marker(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _session_holders(home: Path) -> list[str]:
    """Other live DreamTeam processes that may run sessions of `home` without a hub that answers: a hub.json whose
    process is alive (busy, starting) or that a Docker sandbox wrote, and launcher markers (a marker of a dead
    process is removed)."""
    found: list[str] = []
    info = _read_hub_file(home)
    if info is not None:
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        written_for = info.get("project")
        local = written_for is None or _same_path(str(written_for), str(Path(home).resolve()))
        if not local or (pid != os.getpid() and _pid_alive(pid)):
            found.append(f"the server of {hub_file(home)} (pid {pid}, {info['base_url']})")
    for marker in sorted((Path(home) / ".dreamteam").glob("launcher-*.pid")):
        try:
            pid = int(marker.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        if pid == os.getpid():
            continue
        if _pid_alive(pid):
            found.append(f"process {pid} ({marker})")
        else:  # crashed or killed
            remove_launcher_marker(marker)
    return found


def _stdin_is_terminal() -> bool:
    """sys.stdin.isatty(), except that on Windows the NUL device (stdin redirected from nowhere: a scheduled task,
    `start /b`, `< NUL`) claims to be a tty too. The console would read EOF from it at once, and its end stops
    the hub: only a real console handle counts."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if sys.platform != "win32":
            return True
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (AttributeError, ValueError, OSError):
        return False


def _port_free(host: str, port: int) -> bool:
    host = host.strip("[]")
    if host in ("localhost", ""):
        host = "127.0.0.1"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            return True
    except OSError:
        return False


class _StopSignals:
    """Ctrl+C, Ctrl+Break (Windows) and SIGTERM (`docker stop`, MCP clients) request a graceful stop: the
    web server closes, then `swarm.stop()` stops the agents and their MCP server processes. A second
    signal forces the exit."""

    def __init__(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event):
        self.loop = loop
        self.event = event
        self.count = 0
        self._previous: dict[int, object] = {}

    def install(self) -> "_StopSignals":
        if threading.current_thread() is not threading.main_thread():
            return self
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass
        return self

    def _handle(self, signum, frame) -> None:
        self.count += 1
        if self.count > 1:
            raise KeyboardInterrupt
        self.loop.call_soon_threadsafe(self.event.set)

    def restore(self) -> None:
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):
                pass
        self._previous.clear()


async def _wait_started(server: asyncio.Task, started: asyncio.Event, stop: asyncio.Event) -> bool:
    """Wait until the web server listens (True), fails to start or a stop is requested (False)."""
    waiters = {asyncio.ensure_future(started.wait()), asyncio.ensure_future(stop.wait())}
    try:
        await asyncio.wait({server, *waiters}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
    return started.is_set() and not server.done()


def _server_error(server: asyncio.Task | None) -> BaseException | None:
    if server is None or not server.done() or server.cancelled():
        return None
    return server.exception()


def _start_console(loop: asyncio.AbstractEventLoop, base: str, lang: str, stop: threading.Event) -> asyncio.Future:
    """Run the terminal console in a daemon thread: a thread blocked in input() must not keep the process
    alive after the hub stopped (asyncio.to_thread would make asyncio.run wait for it forever)."""
    from . import console as console_module

    done = loop.create_future()

    def finished() -> None:
        if not done.done():
            done.set_result(None)

    def target() -> None:
        try:
            console_module.run_console(base, lang, stop=stop, local=True)  # (in the hub's own process)
        except Exception:
            log.exception("console failed")
        finally:
            try:
                loop.call_soon_threadsafe(finished)
            except RuntimeError:  # the loop is already closed
                pass

    threading.Thread(target=target, name="dreamteam-console", daemon=True).start()
    return done


# ---- commands -------------------------------------------------------------------------------------
def cmd_init(args) -> int:
    project = Path(args.dir).resolve()
    project.mkdir(parents=True, exist_ok=True)
    cfg_path = project / CONFIG_FILE
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists (use --force to overwrite).", file=sys.stderr)
        return 1
    name = args.name or project.name
    cfg_path.write_text(render_template(name, args.lang, args.provider, args.goal or ""), encoding="utf-8")
    (project / "workspace").mkdir(exist_ok=True)
    print(f"Created {cfg_path}")
    print("Next steps:" if args.lang == "en" else "Дальше:")
    print(f"  1. Edit the model settings in {CONFIG_FILE} (API keys are read from environment variables)."
          if args.lang == "en" else f"  1. Проверьте настройки моделей в {CONFIG_FILE} (ключи API берутся из переменных окружения).")
    print(f"  2. dreamteam run \"{project}\"" + (' --goal "..."' if not args.goal else ""))
    return 0


ENV_CLEANUP_DELAY = 60.0   # a standalone `dreamteam run` removes unused skill environments once, this long after start


def _schedule_env_cleanup(config: Config) -> None:
    """Once, ENV_CLEANUP_DELAY after the swarm started, in a daemon thread: the skills' environments unused for
    [skills] env_retention_days (docs/SPEC.md §19; the launcher runs its own daily cleanup)."""
    days = config.skills.env_retention_days
    if days <= 0:
        return

    def run() -> None:
        from .skill_deps import EnvManager

        try:
            report = EnvManager.for_config(config).cleanup(days)
            if report["removed"]:
                log.info("removed unused skill environments: %s", ", ".join(report["removed"]))
        except Exception:  # noqa: BLE001 - a later run tries again
            log.exception("cannot remove the unused skill environments")

    timer = threading.Timer(ENV_CLEANUP_DELAY, run)
    timer.daemon = True
    timer.start()


async def _serve_swarm(project: Path, config: Config, host: str, port: int, goal: str | None,
                       console: bool, open_browser: bool, stop: asyncio.Event | None = None) -> int:
    """Run swarm + web board (+ terminal console) until a stop signal, the console's /quit or a server
    failure. hub.json exists exactly while the server listens; `swarm.stop()` always runs."""
    from .swarm import Swarm
    from .web import public_base, serve

    if not _port_free(host, port):
        print(f"Port {port} is busy. Use --port or stop the other process.", file=sys.stderr)
        return 1
    swarm = Swarm(config=config)
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    signals = _StopSignals(loop, stop).install()
    console_stop = threading.Event()
    server: asyncio.Task | None = None
    code = 0
    try:
        await swarm.start()
        _schedule_env_cleanup(config)
        if goal:
            current = swarm.goal()
            title, _ = split_goal(goal)  # stored split: a multi-line --goal must not reset the goal at every restart
            if not current or current.get("title") != title or current.get("status") != "active":
                swarm.set_goal(USER, goal)
        base = public_base(host, port)
        started = asyncio.Event()
        server = asyncio.create_task(serve(swarm, host, port, on_started=started.set))
        if not await _wait_started(server, started, stop):
            if not stop.is_set():
                print(f"The web server could not start on {host}:{port}: {_server_error(server) or 'stopped'}",
                      file=sys.stderr)
                code = 1
            return code
        _write_hub_file(project, base, config)
        print(f"DreamTeam \"{config.swarm.name}\" is running.\n  Board: {base}\n  MCP:   {base}/mcp", file=sys.stderr)
        if open_browser:
            webbrowser.open(base)
        stopper = asyncio.ensure_future(stop.wait())
        waiters = {server, stopper}
        if console:
            waiters.add(_start_console(loop, base, config.swarm.language, console_stop))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        error = _server_error(server)
        if error is not None:
            print(f"The web server stopped: {error}", file=sys.stderr)
            code = 1
    finally:
        console_stop.set()
        try:
            if server is not None and not server.done():
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
            _remove_hub_file(project)
        finally:
            try:
                await swarm.stop()
            finally:
                swarm.close()
                signals.restore()
                log.info("swarm stopped")
    return code


def cmd_run(args) -> int:
    project = Path(args.dir).resolve()
    config = Config.load(project)
    host = args.host or config.server.host
    port = args.port or config.server.port
    existing = running_hub(project)
    if existing:
        print(f"A hub is already running for {project}: {existing}", file=sys.stderr)
        return 1
    setup_logging(project)
    console = not args.no_console and _stdin_is_terminal()
    try:
        return asyncio.run(_serve_swarm(project, config, host, port, args.goal, console, args.open))
    except KeyboardInterrupt:
        return 0


def cmd_console(args) -> int:
    from .console import run_console

    local: bool | None = None  # the hub's address decides (a loopback one is local)
    if args.url:
        base = args.url.rstrip("/")
        lang = args.lang or "ru"
    else:
        project = _home_arg(args)
        base = running_hub(project)
        local = True  # found through the home's hub.json: the hub runs on this computer, whatever its address
        if not base:
            print(f"No running hub for {project}. Start it with `dreamteam serve` (or `dreamteam run`).",
                  file=sys.stderr)
            return 1
        try:
            lang = args.lang or Config.load(project).swarm.language
        except (ValueError, OSError):
            lang = "ru"
    ident = hub_identity(base)
    if ident is None:  # (the console would retry for a minute against nothing)
        print(f"No DreamTeam hub answers at {base}.", file=sys.stderr)
        return 1
    if ident.get("mode") == "launcher":
        try:
            session = _pick_session(base, args.session)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except (OSError, ValueError) as exc:  # the hub went away, or not a DreamTeam answer
            print(f"Cannot list the sessions at {base}: {exc}", file=sys.stderr)
            return 1
        folder = session.get("workspace") if isinstance(session.get("workspace"), dict) else {}
        lost = folder.get("default") is False and folder.get("exists") is False
        if session.get("status") != "running" and not lost:  # every command would get 409 "The session is stopped"
            print(f"Session {session['id']} is stopped. Start it on the sessions page ({base}/) first.",
                  file=sys.stderr)
            return 1
        print(f"Session {session['id']}: {session.get('title', '')}", file=sys.stderr)
        if session.get("status") != "running":  # it cannot start: /cd is what this console can do for it
            print(f"It is stopped and cannot start: its folder {folder.get('path')} does not exist. Move it with "
                  "/cd <folder> (or /cd --default for its own folder), then start it on the sessions page "
                  f"({base}/).", file=sys.stderr)
        base = f"{base}/s/{session['id']}"
        lang = args.lang or session.get("language") or lang
    run_console(base, lang, local=local)
    return 0


async def _mcp_in_process(project: Path, config: Config, web: bool, stop: asyncio.Event | None = None) -> None:
    """Stdio MCP server with the swarm in this process (plus the web board when its port is free). Ends
    when the client closes stdin or on a stop signal; `swarm.stop()` always runs. Nothing may write to
    stdout here: it carries the MCP protocol."""
    from .hub_mcp import serve_stdio
    from .swarm import Swarm
    from .web import public_base, serve

    swarm = Swarm(config=config)
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    signals = _StopSignals(loop, stop).install()
    server: asyncio.Task | None = None
    stdio: asyncio.Task | None = None
    try:
        await swarm.start()
        host, port = config.server.host, config.server.port
        if web and _port_free(host, port):
            started = asyncio.Event()
            server = asyncio.create_task(serve(swarm, host, port, on_started=started.set))
            if await _wait_started(server, started, stop):
                _write_hub_file(project, public_base(host, port), config)
            else:  # the board is optional in this mode: keep serving MCP over stdio
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
                log.warning("web board not started on %s:%s: %s", host, port, _server_error(server) or "cancelled")
                server = None
        if not stop.is_set():
            stdio = asyncio.create_task(serve_stdio(swarm))
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({stdio, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopper.cancel()
            if stdio.done() and not stdio.cancelled() and stdio.exception() is not None:
                log.error("stdio MCP server failed", exc_info=stdio.exception())
    finally:
        try:
            for task in (stdio, server):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if server is not None:
                _remove_hub_file(project)
        finally:
            try:
                await swarm.stop()
            finally:
                swarm.close()
                signals.restore()
                log.info("swarm stopped")


def cmd_mcp(args) -> int:
    project = _home_arg(args)
    home_mode = _home_mode(args, project)
    # load the config first: a wrong DIR must not get a .dreamteam/logs directory. A home is created on demand
    # (an MCP client may well be configured with `dreamteam mcp` before `dreamteam serve` ever ran).
    try:
        config = _load_home(project) if home_mode else Config.load(project)
    except FileNotFoundError:
        raise  # main() prints it
    except (ValueError, OSError) as exc:
        print(f"Cannot load {project / CONFIG_FILE}: {exc}", file=sys.stderr)
        return 1
    setup_logging(project, quiet_stderr=True)
    base = running_hub(project)
    try:
        if base:
            from .hub_mcp import serve_stdio_proxy

            asyncio.run(serve_stdio_proxy(base + "/mcp"))
            return 0
        if home_mode:
            asyncio.run(_mcp_launcher_in_process(project, config, web=not args.no_web))
        else:
            asyncio.run(_mcp_in_process(project, config, web=not args.no_web))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_sandbox(args) -> int:
    from .docker import run_sandbox

    project = _home_arg(args)
    home_mode = _home_mode(args, project)
    try:
        config = _load_home(project) if home_mode else Config.load(project)
    except FileNotFoundError:
        raise
    except (ValueError, OSError) as exc:
        print(f"Cannot load {project / CONFIG_FILE}: {exc}", file=sys.stderr)
        return 1
    existing = running_hub(project)
    if existing:  # two hubs on one home would run the same sessions (and their databases) twice
        print(f"DreamTeam is already running for {project}: {existing}/ (stop it first).", file=sys.stderr)
        return 1
    return run_sandbox(project, args.port or config.server.port, build=args.build, goal=args.goal, home=home_mode)


def cmd_status(args) -> int:
    project = _home_arg(args)
    base = running_hub(project)
    if (base and (hub_identity(base) or {}).get("mode") == "launcher") or (not base and _home_mode(args, project)):
        return _print_sessions(project, base)
    if base:
        req = urllib.request.Request(base + "/api/act/swarm_status", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(json.loads(resp.read().decode("utf-8"))["result"])
        print(f"Hub: {base}")
        return 0
    from .swarm import Swarm
    from .tools import ToolContext, call_tool

    swarm = Swarm(project)
    text, _ = asyncio.run(call_tool(ToolContext(swarm, USER), "swarm_status", {}))
    print(text)
    print("Hub: not running")
    swarm.close()
    return 0


# ---- launcher: sessions in a home directory (docs/SPEC.md §18.3) -------------------------
def is_home(path: Path) -> bool:
    """A launcher home (sessions/ next to dreamteam.toml), as opposed to a single-swarm project of `run`."""
    return (path / "sessions").is_dir() and (path / CONFIG_FILE).exists()


def _home_arg(args) -> Path:
    """--home, else the positional DIR, else the current directory when a hub, a home or a project is there,
    else the default home ($DREAMTEAM_HOME or ~/DreamTeam)."""
    from .settings import default_home

    if getattr(args, "home", None):
        return Path(args.home).expanduser().resolve()
    if getattr(args, "dir", None):
        return Path(args.dir).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / CONFIG_FILE).exists():
        return cwd
    return default_home()


def _home_mode(args, path: Path) -> bool:
    """Whether `path` (from _home_arg) is a launcher home rather than a single-swarm project of `run`: `--home`
    says so, the default home is one (even before `serve` created it), and a DIR is one when it has sessions/."""
    if getattr(args, "home", None) or is_home(path):
        return True
    return not getattr(args, "dir", None) and not (Path.cwd() / CONFIG_FILE).exists()


def _load_home(home: Path) -> Config:
    """Create the home if needed (settings file, sessions/) and load its settings; ValueError if they are invalid."""
    from .settings import ensure_home, read_raw, validate

    return validate(ensure_home(home), read_raw(home))


def _hub_json(base: str, path: str, timeout: float = 15.0):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_session(base: str, sid: str | None) -> dict:
    """The session a command works with: `sid`, else the running one (most recently active)."""
    sessions = _hub_json(base, "/api/sessions").get("sessions", [])
    if sid:
        for item in sessions:
            if item["id"] == sid:
                return item
        raise LookupError(f"Unknown session '{sid}'. Sessions: {', '.join(x['id'] for x in sessions) or 'none'}.")
    running = [x for x in sessions if x.get("status") == "running"]
    if running:
        return running[0]
    if len(sessions) == 1:
        return sessions[0]
    raise LookupError("No running session. Create or start one on the sessions page, or pass --session ID."
                      + (f" Sessions: {', '.join(x['id'] for x in sessions)}." if sessions else ""))


def _print_sessions(home: Path, base: str | None) -> int:
    from .launcher_mcp import session_line

    sessions = None
    if base:
        try:
            sessions = _hub_json(base, "/api/sessions").get("sessions", [])
        except (OSError, ValueError):  # the hub stopped meanwhile: read the home itself
            base = None
    if sessions is None:
        if is_home(home):
            from .sessions import SessionManager

            try:
                sessions = SessionManager(home).list()
            except (OSError, ValueError) as exc:
                print(f"Cannot list the sessions of {home}: {exc}", file=sys.stderr)
                return 1
        else:  # listing must not create a home (a typo, before the first `serve`) nor turn a `run` project into one
            sessions = []
    print("\n".join(session_line(s) for s in sessions) if sessions else "No sessions yet.")
    print(f"Home: {home}")
    print(f"Hub: {base}" if base else "Hub: not running (start it with `dreamteam serve`)")
    return 0


async def _serve_home(home: Path, host: str, port: int, *, console: bool, open_browser: bool,
                      goal: str | None = None, title: str | None = None, start: str | None = None,
                      stop: asyncio.Event | None = None, workspace: str | None = None,
                      skills: list[str] | None = None) -> int:
    """Run the launcher (sessions page, settings, every session's board, MCP) until a stop signal, the
    console's /quit or a server failure. `goal` creates and starts a new session (working in the absolute
    folder `workspace` when given, docs/SPEC.md §17.2; with the skills `skills`, §19); `start` starts an existing
    one. hub.json exists exactly while the server listens; all sessions stop at the end."""
    from .launcher import serve_launcher
    from .launcher_mcp import workspace_text
    from .sessions import SessionManager
    from .web import public_base

    if not _port_free(host, port):
        print(f"Port {port} is busy. Use --port or stop the other process.", file=sys.stderr)
        return 1
    manager = SessionManager(home)
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    signals = _StopSignals(loop, stop).install()
    console_stop = threading.Event()
    server: asyncio.Task | None = None
    marker = write_launcher_marker(home)  # sessions may run before hub.json exists (--goal, resume)
    code = 0
    try:
        focus = None
        try:
            if start:
                await manager.start(start)
                focus = start
            if goal:
                created = await manager.create(title or "", goal, workspace=workspace, skills=skills, via="cli")
                focus = created["id"]
                if created.get("workspace_result"):
                    print(f"Session {focus}: {workspace_text(created['workspace_result'])}", file=sys.stderr)
        except Exception as exc:  # unknown session, invalid settings, a refused folder, the swarm failed to start
            print(f"Could not start the session: {exc}", file=sys.stderr)
            return 1
        base = public_base(host, port)
        started = asyncio.Event()
        # hub.json as soon as the port is open (before the sessions resume), so that another `dreamteam serve`
        # or `dreamteam mcp` finds this hub instead of running the same sessions a second time
        server = asyncio.create_task(serve_launcher(manager, host, port, on_started=started.set,
                                                    on_listening=lambda: _write_hub_file(home, base, manager.config)))
        if not await _wait_started(server, started, stop):
            if not stop.is_set():
                print(f"The web server could not start on {host}:{port}: {_server_error(server) or 'stopped'}",
                      file=sys.stderr)
                code = 1
            return code
        print(f"DreamTeam is running.\n  Sessions: {base}/\n  MCP:      {base}/mcp\n  Home:     {home}",
              file=sys.stderr)
        if open_browser:
            webbrowser.open(base + (f"/s/{focus}/" if focus else "/"))
        stopper = asyncio.ensure_future(stop.wait())
        waiters = {server, stopper}
        if console:
            sid = focus or manager.default_session()
            if sid and sid in manager.running:
                lang = manager.config_for(sid).swarm.language
                waiters.add(_start_console(loop, f"{base}/s/{sid}", lang, console_stop))
            else:
                print("The terminal console attaches to a running session: start one on the sessions page, "
                      "then run `dreamteam console`.", file=sys.stderr)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
        error = _server_error(server)
        if error is not None:
            print(f"The web server stopped: {error}", file=sys.stderr)
            code = 1
    finally:
        console_stop.set()
        try:
            if server is not None and not server.done():
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
            _remove_hub_file(home)
        finally:
            try:
                await manager.shutdown()
            finally:
                signals.restore()
                remove_launcher_marker(marker)
                log.info("launcher stopped")
    return code


def cmd_serve(args) -> int:
    from .settings import default_home

    home = Path(args.home or args.dir or default_home()).expanduser().resolve()
    try:
        config = _load_home(home)
    except ValueError as exc:
        print(f"Invalid settings in {home / CONFIG_FILE}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:  # e.g. DIR is a file, or not writable
        print(f"Cannot use {home} as the DreamTeam home: {exc}", file=sys.stderr)
        return 1
    workspace = None
    if args.workspace:
        if not args.goal:
            print("--workspace needs --goal: it is the folder of the session that --goal creates (an existing "
                  "session moves with /cd in the TUI or the console).", file=sys.stderr)
            return 2
        workspace = _workspace_arg(args.workspace)
        if workspace is None:
            return 2
    skills = None
    if args.skill:
        if not args.goal:
            print("--skill needs --goal: it is a skill of the session that --goal creates (an existing session "
                  "attaches skills in its Skills tab).", file=sys.stderr)
            return 2
        skills = _skill_items(args.skill, home, workspace)
        if skills is None:
            return 2
    existing = running_hub(home)
    if existing:
        print(f"DreamTeam is already running for {home}: {existing}/", file=sys.stderr)
        if not args.no_browser:
            webbrowser.open(existing + "/")
        return 1
    setup_logging(home)
    console = not args.no_console and _stdin_is_terminal()
    host = args.host or config.server.host
    port = args.port or config.server.port
    try:
        return asyncio.run(_serve_home(home, host, port, console=console, open_browser=not args.no_browser,
                                       goal=args.goal, title=args.title, workspace=workspace, skills=skills))
    except KeyboardInterrupt:
        return 0


SESSIONS_USAGE = ("dreamteam sessions [DIR] [--home HOME]\n"
                  "       dreamteam sessions rm ID [ID ...] [--yes] [--skills save|discard] [--home HOME]")
DELETE_WORDS = ("rm", "delete", "del", "remove")


def cmd_sessions(args) -> int:
    """`dreamteam sessions [DIR]` lists the sessions; `dreamteam sessions rm ID...` deletes sessions."""
    words = list(args.words or [])
    if words and words[0] in DELETE_WORDS:
        args.dir = None
        return _delete_sessions(args, words[1:])
    if words and words[0] in ("ls", "list"):
        words = words[1:]
    if len(words) > 1:
        print(f"usage: {SESSIONS_USAGE}", file=sys.stderr)
        return 2
    args.dir = words[0] if words else None
    home = _home_arg(args)
    return _print_sessions(home, running_hub(home))


def _hub_delete_session(base: str, sid: str, skills: str | None = None) -> dict:
    """DELETE a session through the running hub (it stops the session first): its answer ({"ok", and
    "workspace_kept" for a folder outside the session}); RuntimeError with the hub's reason. `skills`: "save" or
    "discard" for proposals waiting for the user's library decision."""
    quoted = urllib.parse.quote(sid, safe="")
    extra = f"&skills={skills}&via=cli" if skills else ""   # (the library records the CLI as the surface)
    request = urllib.request.Request(f"{base}/api/sessions/{quoted}?confirm={quoted}{extra}", method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            answer = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            reason = json.loads(exc.read().decode("utf-8")).get("error")
        except (ValueError, AttributeError, OSError):
            reason = None
        raise RuntimeError(reason or f"HTTP {exc.code}") from None
    return answer if isinstance(answer, dict) else {}


def _kept_folder(summary: dict) -> str | None:
    """The workspace folder of a session that lies outside it (the user's project): deleting keeps it."""
    workspace = summary.get("workspace")
    if isinstance(workspace, dict) and workspace.get("default") is False:
        return str(workspace.get("path") or "") or None
    return None


def _delete_sessions(args, ids: list[str]) -> int:
    """Delete sessions with their board, messages, workspace, MCP servers and traces: through the running hub
    when there is one (a running session is stopped first), else in the home itself, unless another DreamTeam
    process may be running them. A workspace folder outside the session is kept. Asks unless --yes."""
    if not ids:
        print(f"usage: {SESSIONS_USAGE}", file=sys.stderr)
        return 2
    from .launcher_mcp import session_line
    from .sessions import SessionManager

    home = _home_arg(args)
    if not is_home(home):
        print(f"{home} is not a DreamTeam home: there are no sessions to delete.", file=sys.stderr)
        return 1

    def holders_refused() -> bool:
        holders = _session_holders(home)
        if holders:
            print("Nothing deleted: another DreamTeam process may be running these sessions ("
                  + "; ".join(holders) + "). Delete them through its web page or console, or stop it and try "
                  "again.", file=sys.stderr)
        return bool(holders)

    base = running_hub(home)
    if not base and holders_refused():
        return 1
    try:
        manager = None if base else SessionManager(home)
        sessions = _hub_json(base, "/api/sessions").get("sessions", []) if base else manager.list()
    except (OSError, ValueError) as exc:
        print(f"Cannot list the sessions of {home}: {exc}", file=sys.stderr)
        return 1
    by_id = {s["id"]: s for s in sessions}
    ids = list(dict.fromkeys(ids))
    unknown = [sid for sid in ids if sid not in by_id]
    if unknown:
        print(f"Unknown session{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
              f"Sessions: {', '.join(by_id) or 'none'}.", file=sys.stderr)
        return 1
    legacy = [sid for sid in ids if by_id[sid].get("legacy")]
    if legacy:
        print(f"Nothing deleted: {', '.join(legacy)} is the project's own session; its files are "
              f"{home / '.dreamteam'}, delete that folder by hand if you mean it.", file=sys.stderr)
        return 1
    if not args.yes:
        if not _stdin_is_terminal():
            print("Nothing deleted: pass --yes to delete without a confirmation.", file=sys.stderr)
            return 1
        for sid in ids:
            print(session_line(by_id[sid]))
            if _kept_folder(by_id[sid]):
                print(f"    its workspace folder {_kept_folder(by_id[sid])} is kept")
        what = "this session" if len(ids) == 1 else f"these {len(ids)} sessions"
        try:
            answer = input(f"Delete {what} with the task board, messages, workspace files, MCP servers and traces? "
                           "This cannot be undone. [y/N] ")
        except EOFError:
            answer = ""
        except KeyboardInterrupt:
            print("\nNothing deleted.")
            return 130
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Nothing deleted.")
            return 0
        if not base:  # a hub may have started while the user answered: it must do the deleting
            base = running_hub(home)
            if not base and holders_refused():
                return 1

    async def delete_all() -> int:
        code = 0
        for sid in ids:
            try:
                if base:
                    answer = await asyncio.to_thread(_hub_delete_session, base, sid, args.skills)
                else:
                    answer = {"workspace_kept": await manager.delete(sid, skills=args.skills, via="cli")}
            except (RuntimeError, OSError, ValueError) as exc:  # SessionError is a ValueError
                reason = str(exc)
                if "waiting for your library decision" in reason and not args.skills:
                    reason += " Pass --skills save (to the skills library first) or --skills discard."
                print(reason if reason.startswith(f"Could not delete session {sid}")
                      else f"Could not delete session {sid}: {reason}", file=sys.stderr)
                code = 1
            else:
                kept = answer.get("workspace_kept")  # (as it was when deleted: a /cd may have run since the listing)
                print(f"Deleted session {sid}." + (f" Its workspace folder {kept} was kept." if kept else ""))
        return code

    try:
        return asyncio.run(delete_all())
    except KeyboardInterrupt:  # the sessions deleted so far are printed
        print("Interrupted.", file=sys.stderr)
        return 130


async def _mcp_launcher_in_process(home: Path, config: Config, web: bool, stop: asyncio.Event | None = None) -> None:
    """Stdio launcher MCP with the sessions in this process (plus the web UI when its port is free).
    Nothing may write to stdout here: it carries the MCP protocol."""
    from .launcher import serve_launcher
    from .launcher_mcp import serve_stdio_launcher
    from .sessions import SessionManager
    from .web import public_base

    manager = SessionManager(home)
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    signals = _StopSignals(loop, stop).install()
    server: asyncio.Task | None = None
    stdio: asyncio.Task | None = None
    marker = write_launcher_marker(home)  # without the web UI there is no hub.json
    try:
        host, port = config.server.host, config.server.port
        if web and _port_free(host, port):
            started = asyncio.Event()
            server = asyncio.create_task(serve_launcher(
                manager, host, port, on_started=started.set,
                on_listening=lambda: _write_hub_file(home, public_base(host, port), config)))
            if not await _wait_started(server, started, stop):  # the web UI is optional: keep serving MCP over stdio
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
                log.warning("web UI not started on %s:%s: %s", host, port, _server_error(server) or "cancelled")
                server = None
                _remove_hub_file(home)
        if server is None and config.server.resume_sessions and not stop.is_set():
            await manager.resume()  # serve_launcher resumes them otherwise
        if not stop.is_set():
            stdio = asyncio.create_task(serve_stdio_launcher(manager))
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({stdio, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopper.cancel()
            if stdio.done() and not stdio.cancelled() and stdio.exception() is not None:
                log.error("stdio MCP server failed", exc_info=stdio.exception())
    finally:
        try:
            for task in (stdio, server):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            _remove_hub_file(home)  # (only the file this process wrote)
        finally:
            try:
                await manager.shutdown()
            finally:
                signals.restore()
                remove_launcher_marker(marker)
                log.info("launcher stopped")


def _session_db(home: Path, sid: str | None) -> Path | None:
    """The database of a session (or of a single-swarm project) for reading traces without a hub."""
    if not is_home(home):
        path = home / ".dreamteam" / "swarm.db"
        return path if path.exists() else None
    from .sessions import SessionManager

    manager = SessionManager(home)
    if not sid:  # no hub, nothing runs: the most recently active session (list() order), not the oldest id
        items = [s for s in manager.list() if not s.get("error")]
        sid = next((s["id"] for s in items if s.get("last_activity")), None) or next((s["id"] for s in items), None)
        if sid:
            print(f"Session {sid}", file=sys.stderr)
    if not sid or not manager.exists(sid):
        return None
    try:
        return manager.config_for(sid).data_dir / "swarm.db"
    except ValueError:  # its settings are invalid now: the database is still where it always is
        return manager.dir_of(sid) / ".dreamteam" / "swarm.db"


def cmd_traces(args) -> int:
    """Print agent traces of a session: the list, one trace, one span or the context of an LLM span."""
    from . import tracing

    home = _home_arg(args)
    base = running_hub(home)
    limit = max(1, min(args.limit, 500))
    if base:
        try:
            if (hub_identity(base) or {}).get("mode") == "launcher":
                base = f"{base}/s/{_pick_session(base, args.session)['id']}"
            if args.span and args.context:
                data = _hub_json(base, f"/api/spans/{args.span}/context")
            elif args.span:
                data = _hub_json(base, f"/api/spans/{args.span}")
            elif args.id:
                data = _hub_json(base, f"/api/traces/{args.id}?payload=0")
            else:
                query = urllib.parse.urlencode({k: v for k, v in (("agent", args.agent), ("limit", limit),
                                                                   ("status", args.status)) if v})
                data = _hub_json(base, f"/api/traces?{query}")["traces"]
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except urllib.error.HTTPError as exc:
            print(f"Not found ({exc.code}).", file=sys.stderr)
            return 1
        except (OSError, ValueError) as exc:  # the hub stopped meanwhile
            print(f"Cannot read traces from {base}: {exc}", file=sys.stderr)
            return 1
    else:
        from .db import Database

        db_path = _session_db(home, args.session)
        if db_path is None:
            print(f"Unknown session '{args.session}'." if args.session else "No session to read traces from.",
                  file=sys.stderr)
            return 1
        if not db_path.exists():  # a session that never started: nothing to show (and no database to create)
            if args.span or args.id:
                print("Not found.", file=sys.stderr)
                return 1
            print("[]" if args.json else "No traces.")
            return 0
        db = Database(db_path)
        try:
            tracer = tracing.Tracer(db)
            if args.span and args.context:
                data = tracer.span_context(args.span)
            elif args.span:
                data = tracer.get_span(args.span)
            elif args.id:
                data = tracer.get_trace(args.id)
            else:
                data = tracer.list_traces(args.agent, limit=limit, status=args.status)
        finally:
            db.close()
        if data is None:
            print("Not found.", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif args.span and args.context:
        print(tracing.render_context(data, limit=10_000, chars=20_000))
    elif args.span:
        print(tracing.render_span(data, limit=100_000))
    elif args.id:
        print(tracing.render_trace(data, detail_chars=2000))
    else:
        print("\n".join(tracing.trace_line(t) for t in data) if data else "No traces.")
    return 0


def _interactive() -> bool:
    """Both ends are a terminal: `dreamteam` alone opens the TUI there and serves headless elsewhere."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _piped_stdin() -> str:
    """Text piped (or redirected from a file) into the command; nothing for a terminal, NUL or a closed stdin."""
    import stat

    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        mode = os.fstat(sys.stdin.fileno()).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
            return ""
        return sys.stdin.read().strip()
    except (OSError, ValueError, AttributeError):
        return ""


def _workspace_arg(text: str) -> str | None:
    """`--workspace DIR` as the absolute folder the hub needs (`~` and relative paths resolved against the
    current directory); None, with the reason printed, for a file. The hub checks the rest (docs/SPEC.md §17.2)."""
    path = os.path.normpath(os.path.abspath(os.path.expanduser(text)))
    if os.path.isfile(path):
        print(f"dreamteam: --workspace {path} is a file, not a folder.", file=sys.stderr)
        return None
    return path


def _skill_items(items: list[str] | None, home: Path, workspace: str | None, *,
                 resuming: bool = False) -> list[str] | None:
    """`--skill REF|PATH|URL` (repeatable) as the hub needs it: a path (it has a `/` or `\\`, starts with `.` or `~`,
    ends in `.zip`, or is a folder or .zip here) made absolute against the current directory, a Git URL as it is,
    anything else a skill reference (`x`, `claude-user:x`, `claude-project:x`, ...). References are checked here,
    before anything starts (bare names: the home's library, then ~/.claude/skills; claude-project: the
    `--workspace` folder's .claude/skills, or with `resuming` left to the hub, which knows the session's folder):
    None, with the reason printed, when one is unknown. Sources are checked by the hub."""
    from . import skills as skill_lib
    from .settings import read_raw, validate

    out: list[str] = []
    refs: list[str] = []
    for raw in items or []:
        text = raw.strip()
        if not text:
            continue
        url = "://" in text or bool(re.match(r"[^@/\\\s]+@[^:/\\\s]+:", text))
        here = os.path.isdir(text) or (os.path.isfile(text) and text.lower().endswith(".zip"))
        if url:
            out.append(text)
        elif any(c in text for c in "/\\") or text[0] in ".~" or text.lower().endswith(".zip") or here:
            out.append(os.path.normpath(os.path.abspath(os.path.expanduser(text))))
        else:
            out.append(text)
            refs.append(text)
    if refs:
        try:
            config = validate(home, read_raw(home)) if (home / CONFIG_FILE).exists() else validate(home, {})
        except (OSError, ValueError):
            config = None   # the hub reports broken settings itself
        project = Path(workspace) if workspace else None
        for ref in refs if config is not None else []:
            if resuming and ref.startswith("claude-project:"):
                continue
            try:
                skill_lib.resolve_ref(config, ref, workspace=project)
            except skill_lib.SkillError as exc:
                print(f"dreamteam: --skill {ref}: {exc}", file=sys.stderr)
                return None
    return out


def cmd_tui(args) -> int:
    """The terminal UI in the style of Claude Code (docs/TUI.md), or with -p the non-interactive print mode."""
    home = _home_arg(args)
    overrides = {"model": args.model, "agents": args.agents, "max_agents": args.max_agents, "budget": args.budget,
                 "title": args.title}
    if args.team is not None:
        try:  # checked here, before a session or the TUI starts (the TUI turns it into swarm.starting_roles)
            roles.parse_team(args.team)
        except ValueError as exc:
            print(f"dreamteam: --team: {exc}", file=sys.stderr)
            return 2
        overrides["team"] = args.team
    session = args.resume or None
    prompt = args.prompt
    workspace = None
    if args.workspace:
        if args.continue_last or args.resume is not None:
            print("dreamteam: --workspace is the folder of a new session; move an existing one with /cd in the TUI.",
                  file=sys.stderr)
            return 2
        workspace = _workspace_arg(args.workspace)
        if workspace is None:
            return 2
    if args.skill:  # a new session's skills, or (-c / -r) attached to the resumed one
        skills = _skill_items(args.skill, home, workspace, resuming=args.continue_last or args.resume is not None)
        if skills is None:
            return 2
        overrides["skills"] = skills
    if args.print:
        from .tui import main_print

        piped = _piped_stdin()  # `cat log.txt | dreamteam -p "что тут не так?"`: the piped text goes along
        if piped:
            prompt = f"{prompt}\n\n{piped}" if prompt else piped
        if not prompt or not prompt.strip():
            print("dreamteam -p: nothing to do: pass a prompt (or pipe one in).", file=sys.stderr)
            return 2
        options = {"session": session, "continue_last": args.continue_last, "overrides": overrides,
                   "output_format": args.output_format, "verbose": args.verbose, "timeout": args.timeout,
                   "auto_reply": not args.no_auto_reply, "host": args.host, "port": args.port,
                   "workspace": workspace}
        if args.lang:
            options["lang"] = args.lang
        return main_print(home, prompt, **options)
    if not _interactive():
        print("dreamteam tui needs a terminal. Use `dreamteam -p \"...\"` in scripts, or `dreamteam serve`.",
              file=sys.stderr)
        return 2
    from .tui import main_tui

    if args.resume == "" and not prompt:  # `-r` without an id: pick a session from the list
        prompt = "/sessions"
    try:   # (session "": the one picked is the session resumed, which gets the --skill ones)
        return main_tui(home, lang=args.lang, session=args.resume, continue_last=args.continue_last,
                        initial_prompt=prompt, overrides=overrides, host=args.host, port=args.port,
                        verbosity="verbose" if args.verbose else "normal", workspace=workspace)
    except Exception as exc:  # e.g. prompt_toolkit's NoConsoleScreenBufferError in mintty without winpty
        if type(exc).__name__ != "NoConsoleScreenBufferError":
            raise
        print("This terminal is not supported by the TUI (Git Bash/mintty: run `winpty dreamteam`, or use "
              "Windows Terminal / PowerShell).", file=sys.stderr)
        return 2


TUI_FLAGS = frozenset({"-p", "--print", "-c", "--continue", "-r", "--resume", "--output-format", "--model",
                       "--budget", "--agents", "--team", "--max-agents", "--verbose", "--timeout", "--no-auto-reply",
                       "--lang"})
SERVE_FLAGS = frozenset({"--no-console", "--no-browser", "--goal"})


def _default_command(argv: list[str], commands: list[str]) -> list[str]:
    """Like `claude`: `dreamteam` alone or with TUI options opens the TUI in a terminal, `dreamteam "текст"`
    opens it with that first message; headless (no terminal, or serve-only options) it serves the launcher."""
    if not argv:
        return ["tui" if _interactive() else "serve"]
    first = argv[0]
    if first in commands or first in ("-h", "--help", "--version"):
        return argv
    if not first.startswith("-"):
        import difflib

        if " " not in first.strip() and difflib.get_close_matches(first, commands, n=1, cutoff=0.75):
            return argv  # a mistyped command (`dreamteam sesions`): argparse explains it, no session is created
        return ["tui", *argv]
    flags = {arg.split("=", 1)[0] for arg in argv if arg.startswith("-")}
    if flags & TUI_FLAGS:
        return ["tui", *argv]
    if flags & SERVE_FLAGS or not _interactive():
        return ["serve", *argv]
    return ["tui", *argv]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dreamteam", description="DreamTeam — self-organizing AI agent swarm")
    p.add_argument("--version", action="version", version=f"dreamteam {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("tui", aliases=["chat"],
                       help="the terminal UI in the style of Claude Code (the default command in a terminal); "
                            "-p runs one prompt non-interactively")
    s.add_argument("prompt", nargs="?", help="first message: the goal of a new session (its first line becomes the "
                                             "title, the rest the description; or, with -c/-r, the next instruction "
                                             "to that session)")
    s.add_argument("-p", "--print", action="store_true",
                   help="non-interactive: run the prompt, follow the session until the goal is done, print the result")
    s.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                   help="continue the most recently active session")
    s.add_argument("-r", "--resume", nargs="?", const="", default=None, metavar="ID",
                   help="resume session ID (without ID: pick one from the list)")
    s.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                   help="with -p: plain result text, one JSON result object, or every event as a JSON line")
    s.add_argument("--verbose", action="store_true", help="show thinking and tool results (with -p: progress on stderr)")
    s.add_argument("--timeout", type=float, help="with -p: give up after this many seconds (exit code 3)")
    s.add_argument("--no-auto-reply", action="store_true",
                   help="with -p: do not answer agents' questions with 'decide yourself'")
    s.add_argument("--home", help="home directory (default: $DREAMTEAM_HOME or ~/DreamTeam)")
    s.add_argument("--lang", choices=["ru", "en"], help="UI language")
    s.add_argument("--model", help="model profile of a new session's agents")
    s.add_argument("--agents", type=int, help="agents a new session starts with")
    s.add_argument("--team", help='starting team of a new session: roles in order, ROLE*N for several, e.g. '
                                  '"architect,developer*2,tester"')
    s.add_argument("--max-agents", type=int, help="agent limit of a new session")
    s.add_argument("--budget", type=float, help="money limit of a new session, USD")
    s.add_argument("--title", help="title of a new session")
    s.add_argument("--workspace", metavar="DIR",
                   help="the folder the new session works in instead of its own one: an empty or missing folder "
                        "becomes its workspace; a folder with files (your project) is adopted: the agents work "
                        "with its files and commit to its current git branch (refused while the repository has "
                        "uncommitted changes). An existing session moves with /cd")
    s.add_argument("--skill", action="append", metavar="REF|PATH|URL",
                   help="a skill for the new session (repeatable; with -c / -r attached to that session): a name "
                        "from the skills library or ~/.claude/skills (claude-user:NAME / claude-project:NAME picks "
                        "the source), or a folder, .zip or Git URL, imported into the library first")
    s.add_argument("--host", help="host of the launcher started in-process (when none runs)")
    s.add_argument("--port", type=int, help="port of the launcher started in-process")
    s.set_defaults(func=cmd_tui, dir=None)

    s = sub.add_parser("serve", help="start DreamTeam headless: the sessions page, settings, every session's "
                                     "board and the MCP endpoint (the default command without a terminal)")
    s.add_argument("dir", nargs="?", help="home directory (default: $DREAMTEAM_HOME or ~/DreamTeam)")
    s.add_argument("--home", help="same as DIR")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--goal", help="create and start a new session with this goal (first line: the title)")
    s.add_argument("--title", help="title of the session created by --goal")
    s.add_argument("--workspace", metavar="DIR",
                   help="with --goal: the folder that session works in (an empty or missing folder becomes its "
                        "workspace, a folder with files is adopted, as for `dreamteam --workspace`)")
    s.add_argument("--skill", action="append", metavar="REF|PATH|URL",
                   help="with --goal: a skill for that session (repeatable), as for `dreamteam --skill`")
    s.add_argument("--no-console", action="store_true")
    s.add_argument("--no-browser", action="store_true", help="do not open the browser")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sessions", help="list the sessions of a home; `sessions rm ID` deletes a session",
                       usage=SESSIONS_USAGE)
    s.add_argument("words", nargs="*", metavar="DIR | rm ID",
                   help="a home directory to list, or `rm` (delete, del, remove) and the ids of the sessions "
                        "to delete with their task board, messages, workspace files, MCP servers and traces")
    s.add_argument("--home")
    s.add_argument("-y", "--yes", action="store_true", help="delete without asking")
    s.add_argument("--skills", choices=["save", "discard"],
                   help="skills the session's members proposed that wait for your library decision: save them to "
                        "the skills library first, or delete them with the session (refused without it)")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("traces", help="show agent traces (LLM calls, thinking, tools, tokens, cost) of a session")
    s.add_argument("agent", nargs="?", help="only this agent")
    s.add_argument("--home", help="home or project directory")
    s.add_argument("--session", help="session id (default: the running one)")
    s.add_argument("--id", type=int, help="show one trace with all its steps")
    s.add_argument("--span", type=int, help="show one step in full")
    s.add_argument("--context", action="store_true", help="with --span of an LLM step: the messages the model saw")
    s.add_argument("--status", help="filter: failed, done, error, step_limit, running, ...")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_traces, dir=None)

    s = sub.add_parser("init", help="create a swarm project (dreamteam.toml + workspace)")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--lang", choices=["ru", "en"], default="ru")
    s.add_argument("--provider", choices=sorted(PROVIDER_PRESETS), default="anthropic")
    s.add_argument("--name")
    s.add_argument("--goal")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="start the swarm, the web board and the MCP endpoint")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--goal")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-console", action="store_true")
    s.add_argument("--open", action="store_true", help="open the board in a browser")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("console", help="chat with a running swarm (session) in the terminal")
    s.add_argument("dir", nargs="?")
    s.add_argument("--home")
    s.add_argument("--session", help="session id (default: the running one)")
    s.add_argument("--url")
    s.add_argument("--lang", choices=["ru", "en"])
    s.set_defaults(func=cmd_console)

    s = sub.add_parser("mcp", help="serve DreamTeam (or a swarm project) as an MCP server over stdio")
    s.add_argument("dir", nargs="?")
    s.add_argument("--home")
    s.add_argument("--no-web", action="store_true", help="do not start the web UI in-process")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("sandbox", help="run DreamTeam (a home, or a swarm project) inside Docker")
    s.add_argument("dir", nargs="?")
    s.add_argument("--home")
    s.add_argument("--build", action="store_true", help="rebuild the image")
    s.add_argument("--port", type=int)
    s.add_argument("--goal")
    s.set_defaults(func=cmd_sandbox)

    s = sub.add_parser("status", help="print the swarm status (the sessions, for a home)")
    s.add_argument("dir", nargs="?")
    s.add_argument("--home")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    commands = [name for action in parser._subparsers._group_actions for name in action.choices]
    args, extra = parser.parse_known_args(_default_command(argv, commands))
    if extra and getattr(args, "command", None) == "sessions" and not any(x.startswith("-") for x in extra):
        args.words += extra  # `sessions rm --yes ID`: argparse ends the words at the first option
    elif extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    try:
        return int(args.func(args) or 0)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
