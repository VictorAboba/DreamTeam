"""Record the README demo: a swarm of four agents working on a goal on the web board.

    uv run --with playwright --with imageio-ffmpeg python scripts/demo_gif.py

The agents run on a scripted model (dreamteam.llm.scripted.ScriptedLLM): every board action, message and file is
real and goes through the same tools a live model calls, but what the agents decide is written below in SCENARIO
order, with pauses so the eye can follow. No API key is needed and every run looks the same. Everything happens in a
throwaway home folder; nothing from your own ~/DreamTeam is read or shown.

The browser is Playwright's Chromium (`playwright install chromium` once), or Edge/Chrome with --channel. The GIF
is encoded by the ffmpeg that imageio-ffmpeg ships. Output: docs/assets/demo.gif (--out)."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dreamteam import swarm as swarm_module
from dreamteam.launcher import serve_launcher
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import Status
from dreamteam.sessions import SessionManager

ROOT = Path(__file__).resolve().parents[1]
GOAL = "Build a CLI that converts CSV to JSON, with tests"
TEAM = [("Emma Clark", "architect"), ("Liam Foster", "developer"), ("Maya Reed", "developer"),
        ("Owen Brooks", "tester")]
EMMA, LIAM, MAYA, OWEN = (name for name, _ in TEAM)

SETTINGS = f"""
[swarm]
language = "en"
start_agents = {len(TEAM)}
max_agents = {len(TEAM)}
max_cost_usd = 0
starting_roles = {[role for _, role in TEAM]!r}

[agents]
stagger_seconds = 0.3
idle_nudge_seconds = 0.5

[server]
resume_sessions = false

[models.default]
provider = "scripted"
model = "scripted"
input_price = 0
output_price = 0
cache_read_price = 0
cache_write_price = 0
"""

READER_PY = '''"""Read a CSV file into rows of typed values."""
import csv


def convert(cell: str):
    if cell == "":
        return None
    for kind in (int, float):
        try:
            return kind(cell)
        except ValueError:
            pass
    return cell


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{key: convert(value) for key, value in row.items()} for row in csv.DictReader(f)]
'''

CLI_PY = '''"""csvjson: convert a CSV file to JSON."""
import argparse
import json


from .reader import read_rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="csvjson")
    ap.add_argument("source")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    ap.add_argument("--indent", type=int, default=2)
    ap.add_argument("--ndjson", action="store_true", help="one JSON object per line")
    args = ap.parse_args(argv)
    rows = read_rows(args.source)
    text = ("\\n".join(json.dumps(r, ensure_ascii=False) for r in rows) if args.ndjson
            else json.dumps(rows, indent=args.indent, ensure_ascii=False))
    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    out.write(text + "\\n")
    return 0
'''

TESTS_PY = '''from csvjson.cli import main
from csvjson.reader import read_rows


def write(tmp_path, text):
    path = tmp_path / "in.csv"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_types(tmp_path):
    assert read_rows(write(tmp_path, "a,b,c,d\\n1,2.5,1e3,x\\n")) == [{"a": 1, "b": 2.5, "c": 1000.0, "d": "x"}]


def test_quoted_comma_and_empty_cell(tmp_path):
    assert read_rows(write(tmp_path, 'name,city\\n"Doe, J",\\n')) == [{"name": "Doe, J", "city": None}]


def test_ndjson(tmp_path, capsys):
    main([write(tmp_path, "a\\n1\\n2\\n"), "--ndjson"])
    assert capsys.readouterr().out == '{"a": 1}\\n{"a": 2}\\n'
'''

README_MD = """# csvjson

    csvjson people.csv                 # JSON array to stdout
    csvjson people.csv -o people.json  # to a file
    csvjson people.csv --ndjson        # one object per line

Numbers become numbers, empty cells become null.
"""


def call(name: str, /, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


class Scenario:
    """What each agent does next, decided from the board. Steps run in order per agent; a step whose condition is
    not met yet makes the agent wait (it is woken again by the board's idle nudge)."""

    def __init__(self, pace: float = 1.0) -> None:
        self.pace = pace   # multiplies every pause: below 1 is faster
        self.t0 = time.monotonic()
        self.done: dict[str, int] = {name: 0 for name, _ in TEAM}
        self.flags: set[str] = set()
        self.reviews: dict[int, list[tuple[bool, str]]] = {
            1: [(True, "Clear split: reader and CLI can go in parallel.")],
            2: [(False, "\"1e3\" stays a string. Exponents are numbers too."),
                (True, "Exponents parse now, empty cells are null.")],
            3: [(True, "Flags match the plan; stdout by default.")],
            4: [(True, "Cases cover the reader and both output modes.")],
            5: [(True, "Usage matches the flags.")],
        }

    # each step: (pause before acting, condition(swarm) -> bool, action(swarm) -> dict)
    def steps(self, name: str) -> list[tuple[float, Any, Any]]:
        board = lambda s: s.board  # noqa: E731

        def task(s, tid):
            return next((t for t in board(s).list(include_closed=True) if t.id == tid), None)

        def ready(tid):   # todo, nobody holds it, its dependencies are done
            return lambda s: any(t.id == tid for t in board(s).ready_unassigned())

        def flag(f):
            return lambda s: f in self.flags

        always = lambda s: True  # noqa: E731
        if name == EMMA:
            return [
                (1.2, ready(1), lambda s: call("task_claim", task_id=1)),
                (1.4, always, lambda s: call("task_comment", task_id=1, text=(
                    "Plan: a small `csvjson` package. The reader and the CLI are independent, so they go in "
                    "parallel; tests and the README follow."))),
                (0.8, always, lambda s: call(
                    "task_create", title="Parse CSV with typed values", reviewer=OWEN, tags=["code"],
                    goal="csvjson/reader.py: read_rows(path) returns dicts; numbers as numbers, empty cells as null")),
                (0.7, always, lambda s: call(
                    "task_create", title="JSON writer and CLI flags", reviewer=EMMA, tags=["code"],
                    goal="csvjson/cli.py: `csvjson in.csv [-o out.json] [--indent N] [--ndjson]`")),
                (0.7, always, lambda s: call(
                    "task_create", title="Tests: quoting, empty cells, NDJSON", reviewer=LIAM, tags=["test"],
                    depends_on=[2, 3], goal="pytest cases for the reader and both output modes")),
                (0.7, always, lambda s: call(
                    "task_create", title="Usage section in README", reviewer=MAYA, tags=["docs"], depends_on=[3],
                    goal="README.md shows the three ways to call csvjson")),
                (0.9, always, lambda s: call("task_complete", task_id=1,
                                             result="Tasks #2-#5: #2 and #3 in parallel, then #4 and #5.")),
                (0.8, ready(5), lambda s: call("task_claim", task_id=5)),
                (1.3, always, lambda s: call("write_file", path="README.md", content=README_MD)),
                (0.9, always, lambda s: call("task_complete", task_id=5, result="README.md: usage and the flags.")),
                (1.0, lambda s: all(t.status == Status.DONE for t in board(s).list(include_closed=True))
                    and len(board(s).list(include_closed=True)) == 5,
                 lambda s: call("goal_complete", summary="csvjson converts CSV to JSON or NDJSON; tests cover the "
                                                         "reader and both output modes.")),
            ]
        if name == LIAM:
            return [
                (1.0, ready(2), lambda s: call("task_claim", task_id=2)),
                (1.3, always, lambda s: call("task_comment", task_id=2, text=(
                    "Going with csv.DictReader; numbers become int or float, empty cells become null."))),
                (1.5, always, lambda s: call("write_file", path="csvjson/reader.py",
                                             content=READER_PY.replace("(int, float)", "(int,)"))),
                (0.9, always, lambda s: call("task_complete", task_id=2,
                                             result="read_rows(path) in csvjson/reader.py")),
                (1.0, lambda s: "rejected-2" in self.flags and task(s, 2).status == Status.IN_PROGRESS,
                 lambda s: call("write_file", path="csvjson/reader.py", content=READER_PY)),
                (0.9, always, lambda s: call("task_complete", task_id=2,
                                             result="Floats and exponents parse: 1e3 -> 1000.0")),
            ]
        if name == MAYA:
            return [
                (1.6, ready(3), lambda s: call("task_claim", task_id=3)),
                (1.2, always, lambda s: self.flag("asked", call("task_comment", task_id=3, text=(
                    f"@{LIAM} which types does read_rows return? I'll map them to JSON in the writer.")))),
                (1.6, flag("answered"), lambda s: call("write_file", path="csvjson/cli.py", content=CLI_PY)),
                (1.1, always, lambda s: call("task_comment", task_id=3, text=(
                    "Flags: -o/--output, --indent, --ndjson. JSON goes to stdout unless -o is given."))),
                (0.9, always, lambda s: call("task_complete", task_id=3, result="csvjson/cli.py with three flags")),
            ]
        if name == OWEN:
            return [
                (1.0, ready(4), lambda s: call("task_claim", task_id=4)),
                (1.5, always, lambda s: call("write_file", path="tests/test_csvjson.py", content=TESTS_PY)),
                (1.0, always, lambda s: call("task_comment", task_id=4, text=(
                    "Covered: typed values with exponents, a quoted comma, an empty cell, NDJSON output."))),
                (0.9, always, lambda s: call("task_complete", task_id=4, result="tests/test_csvjson.py")),
            ]
        return []

    def flag(self, f: str, action: dict) -> dict:
        self.flags.add(f)
        return action

    async def act(self, swarm, name: str) -> dict:
        action = await self._next(swarm, name)
        calls = action["tool_calls"]
        if calls[0]["name"] != "wait":
            what = ", ".join(f'{c["name"]}({c["arguments"].get("task_id", c["arguments"].get("path", ""))})'
                             for c in calls)
            print(f"{time.monotonic() - self.t0:5.1f}s  {name}: {what}", flush=True)
        return action

    async def _next(self, swarm, name: str) -> dict:
        # a question from a teammate is answered first
        if name == LIAM and "asked" in self.flags and "answered" not in self.flags:
            await asyncio.sleep(1.1 * self.pace)
            self.flags.add("answered")
            return call("task_comment", task_id=3, text=f"@{MAYA} int, float or str; None for an empty cell.")
        for t in swarm.board.reviews_of(name):
            verdicts = self.reviews.get(t.id) or [(True, "Looks good.")]
            approve, comment = verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]
            if not approve:
                self.flags.add(f"rejected-{t.id}")
            await asyncio.sleep(1.2 * self.pace)
            return call("task_review", task_id=t.id, approve=approve, comment=comment)
        steps = self.steps(name)
        if self.done[name] < len(steps):
            pause, ready, action = steps[self.done[name]]
            if ready(swarm):
                self.done[name] += 1
                await asyncio.sleep(pause * self.pace)
                return action(swarm)
        return call("wait", reason="waiting for the next task")


def remove(path: Path) -> None:
    """Delete a folder, read-only files included (git keeps its objects read-only, which Windows refuses to
    delete as they are)."""
    def writable_again(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=writable_again)
        else:
            shutil.rmtree(path, onerror=writable_again)
    except OSError as exc:
        print(f"could not delete {path}: {exc}")


def fixed_names() -> None:
    """The four names above instead of random ones, so every recording is the same."""
    order = [name for name, _ in TEAM]
    swarm_module.generate_name = lambda taken, language="en", rng=None: next(n for n in order if n not in taken)


async def record(args) -> Path:
    from playwright.async_api import async_playwright

    home = Path(tempfile.mkdtemp(prefix="dreamteam-demo-"))
    (home / "dreamteam.toml").write_text(SETTINGS, encoding="utf-8")
    fixed_names()
    scenario = Scenario(args.pace)
    holder: dict[str, Any] = {}

    def factory(profile, agent: str) -> ScriptedLLM:
        return ScriptedLLM(lambda system, messages, tools: scenario.act(manager.swarms[holder["sid"]], agent),
                           profile)

    manager = SessionManager(home, llm_factory=factory, language="en")
    listening = asyncio.Event()
    server = asyncio.ensure_future(serve_launcher(manager, "127.0.0.1", args.port, on_listening=listening.set))
    frames_dir = Path(tempfile.mkdtemp(prefix="dreamteam-demo-frames-"))
    frames: list[tuple[float, Path]] = []   # (wall-clock time the frame was painted, its PNG)
    try:
        await asyncio.wait_for(listening.wait(), 30)
        summary = await manager.create("csvjson", start=False)
        holder["sid"] = sid = summary["id"]
        swarm = await manager.start(sid)   # running, no goal yet: the agents wait for one
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel=args.channel) if args.channel else await pw.chromium.launch()
            context = await browser.new_context(viewport={"width": args.width, "height": args.height},
                                                color_scheme=args.theme, device_scale_factor=1)
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{args.port}/s/{sid}/")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1.5)
            # Chromium's screencast: a frame each time the page repaints, with its time; no video codec needed
            cdp = await context.new_cdp_session(page)

            async def on_frame(params: dict) -> None:
                path = frames_dir / f"{len(frames):05d}.png"
                path.write_bytes(base64.b64decode(params["data"]))
                frames.append((params["metadata"]["timestamp"], path))
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

            cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(on_frame(params)))
            await cdp.send("Page.startScreencast", {"format": "png", "maxWidth": args.width,
                                                    "maxHeight": args.height})
            await asyncio.sleep(args.lead)
            # the user gives the goal the way a person does: in the board's goal dialog
            await page.click("#goal-btn")
            await page.wait_for_selector("#g-title")
            await asyncio.sleep(0.4)
            await page.type("#g-title", GOAL, delay=38)
            await asyncio.sleep(0.5)
            await page.click("#g-save")
            scenario.t0 = time.monotonic()
            deadline = time.monotonic() + args.timeout
            while (swarm.goal() or {}).get("status") != "done":
                if time.monotonic() > deadline:
                    for t in swarm.board.list(include_closed=True):
                        print(f"  #{t.id} {t.status} {t.assignee} -> {t.reviewer}: {t.title}")
                    print(f"  steps done: {scenario.done}, flags: {sorted(scenario.flags)}")
                    raise RuntimeError("the scenario did not finish in time")
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.8)
            # the end: the task whose first version was sent back, scrolled to its discussion and review
            await page.click('.card[data-id="2"]')
            await page.wait_for_selector("#discussion-title")
            await asyncio.sleep(0.6)
            await page.evaluate("document.getElementById('discussion-title')"
                                ".scrollIntoView({block: 'start', behavior: 'smooth'})")
            await asyncio.sleep(args.tail)
            t_end = time.time()
            await cdp.send("Page.stopScreencast")
            await context.close()
            await browser.close()
        return encode(frames, t_end, frames_dir, args)
    finally:
        server.cancel()
        try:
            await server
        except BaseException:
            pass
        if not args.keep:
            remove(home)
            remove(frames_dir)
        else:
            print(f"kept {home} and {frames_dir}")


def encode(frames: list[tuple[float, Path]], t_end: float, frames_dir: Path, args) -> Path:
    """Frames with their own durations (the screencast sends one only on a repaint) -> a palette-optimised GIF."""
    import imageio_ffmpeg

    if not frames:
        raise RuntimeError("the browser sent no frames")
    lines = ["ffconcat version 1.0"]
    for (t, path), (t_next, _) in zip(frames, frames[1:] + [(t_end, None)]):
        lines += [f"file '{path.name}'", f"duration {max(0.001, t_next - t):.3f}"]
    lines.append(f"file '{frames[-1][1].name}'")   # the concat demuxer drops the last duration otherwise
    (frames_dir / "frames.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    graph = (f"fps={args.fps},scale={args.gif_width}:-1:flags=lanczos,split[a][b];"
             f"[a]palettegen=max_colors={args.colors}:stats_mode=diff[p];"
             "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", "frames.txt", "-vf", graph, "-loop", "0", str(out)], check=True, cwd=frames_dir)
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB, {t_end - frames[0][0]:.1f} s, {len(frames)} frames")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "docs" / "assets" / "demo.gif"))
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--channel", help="browser channel: msedge, chrome (default: Playwright's Chromium)")
    ap.add_argument("--theme", choices=["light", "dark"], default="dark")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--gif-width", type=int, default=960)
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--colors", type=int, default=128)
    ap.add_argument("--lead", type=float, default=0.8, help="seconds of the empty board before the goal is typed")
    ap.add_argument("--pace", type=float, default=0.75, help="multiplies the agents' pauses: below 1 is faster")
    ap.add_argument("--tail", type=float, default=2.8, help="seconds the finished task stays open at the end")
    ap.add_argument("--timeout", type=float, default=90, help="give up when the goal is not done after this long")
    ap.add_argument("--keep", action="store_true", help="keep the temporary home and the frames")
    args = ap.parse_args()
    asyncio.run(record(args))


if __name__ == "__main__":
    main()
