"""A child process that holds a skill environment lock the way another DreamTeam process would:
`python hold_lock.py <lock file> shared|exclusive [<env dir to start half-built> | <env dir> --finish]`.
It prints "locked" once it holds the lock (or "busy" and exits 3), then waits until its stdin closes or it is
killed. With an env dir it first writes a half-built env there (a builder in the middle of its work); with
--finish it completes that env (an `ok` stamp) before it lets go, as a builder in another process would."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dreamteam import skill_deps


def main(argv: list[str]) -> int:
    path, mode = Path(argv[0]), argv[1]
    fd = skill_deps._open_lock(path)
    if not skill_deps._oslock(fd, exclusive=mode == "exclusive"):
        print("busy", flush=True)
        return 3
    env = Path(argv[2]) if len(argv) > 2 else None
    if env is not None:
        (env / "src").mkdir(parents=True, exist_ok=True)
        (env / "partial").write_text("half-built", encoding="utf-8")
    print("locked", flush=True)
    if env is not None and "--finish" in argv:
        time.sleep(float(os.environ.get("HOLD_SECONDS", "1")))
        (env / "env.json").write_text(json.dumps({"ecosystem": "python", "installed": []}), encoding="utf-8")
        python = env / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("fake", encoding="utf-8")
        (env / "partial").unlink()
        (env / "ok").write_text(json.dumps({"at": time.time(), "key": "other-process"}), encoding="utf-8")
        os.close(fd)
        print("finished", flush=True)
        return 0
    sys.stdin.read()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
