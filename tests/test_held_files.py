"""Held new files (docs/SPEC.md §7 "Held new files"): the shell's and the jobs' autocommits commit changes to tracked
files at once, but a new untracked file only once it has outlived one more command of the member whose commit first
saw it (or at task_complete, the end of the member's turn, its leaving, the session's stop); a held file that
vanished never enters the history, and another member's commit never sweeps it up."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from dreamteam import workspace as workspace_mod
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.workspace import Workspace, scratch_rel
from test_runtime import call, make_swarm as make_policy_swarm, run_until, shutdown
from test_scratch import commits, git, gitlinks, nested_repo, put, python, swarm_config

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ANNA = "Anna Smith"
BORIS = "Boris King"
LEFTOVERS = ["tests/test-hooks/hooks/post_gen_project.bat", "tests/test-hooks/hooks/pre_gen_project.py",
             "tests/test-hooks/input.json"]


class Commands:
    """The members' commands, as JobManager.running_commands reports them: the test starts and ends them."""

    def __init__(self) -> None:
        self.running: dict[int, str] = {}
        self.last = 0

    def start(self, member: str) -> int:
        self.last += 1
        self.running[self.last] = member
        return self.last

    def end(self, serial: int) -> None:
        del self.running[serial]

    def running_commands(self) -> tuple[dict[int, str], int]:
        return dict(self.running), self.last


@pytest.fixture
async def repo(fake_swarm) -> Workspace:
    w = Workspace(fake_swarm)
    fake_swarm.workspace = w
    fake_swarm.jobs = Commands()
    await w.init()
    return w


@pytest.fixture
def cmds(repo) -> Commands:
    return repo.swarm.jobs


def history_files(root: Path) -> set[str]:
    """Every path any commit ever touched."""
    return {line for line in git(root, "log", "--all", "--name-only", "--format=").splitlines() if line.strip()}


async def command(ws: Workspace, actor: str, detail: str) -> str | None:
    """One `shell` call of `actor` that has ended, and its autocommit."""
    serial = ws.swarm.jobs.start(actor)
    ws.swarm.jobs.end(serial)
    return await ws.autocommit(actor, kind="shell", detail=detail, hold=True, ended=serial)


async def ends(ws: Workspace, actor: str, serial: int, detail: str) -> str | None:
    """The end of a command started earlier (a long test run, a job), and its autocommit."""
    ws.swarm.jobs.end(serial)
    return await ws.autocommit(actor, kind="shell", detail=detail, hold=True, ended=serial)


async def test_test_leftovers_that_the_next_run_deletes_never_enter_the_history(repo):
    root = repo.root
    put(root, "src/app.py", "v1\n")
    assert await repo.autocommit(ANNA, "seed")
    # a test run that edits a tracked file and leaves files behind: the edit is committed at once, the files wait
    put(root, "src/app.py", "v2\n")
    for path in LEFTOVERS:
        put(root, path, "x\n")
    assert await command(repo, ANNA, "pytest -q")
    assert commits(root)[0] == (ANNA, "shell: src/app.py", ["src/app.py"])
    assert sorted(repo.held()) == LEFTOVERS
    # the next run deletes them first: no add/delete commits, nothing of them in any commit
    shutil.rmtree(root / "tests")
    assert await command(repo, ANNA, "pytest -q") is None
    assert repo.held() == {} and len(commits(root)) == 3  # Initialize, seed, the edit
    assert not history_files(root) & set(LEFTOVERS)


async def test_a_new_file_is_committed_after_one_more_command(repo):
    root = repo.root
    put(root, "src/new.py", "print(1)\n")
    assert await command(repo, ANNA, "python gen.py") is None
    assert "src/new.py" not in history_files(root)
    commit = await command(repo, ANNA, "ls")  # it outlived a command: a commit of its own, under its owner
    assert commit and commits(root)[0] == (ANNA, "shell (new files): src/new.py", ["src/new.py"])
    body = git(root, "log", "-1", "--format=%b").strip()
    assert body == ("python gen.py\n\n" + workspace_mod.HELD_NOTE.format(
        owner=ANNA, others="", when=f"after the next command of {ANNA} left them in place"))
    assert repo.held() == {} and git(root, "status", "--porcelain") == ""
    assert await command(repo, ANNA, "ls") is None  # nothing more to commit


async def test_a_held_file_rewritten_by_the_next_command_waits_again(repo):
    root = repo.root
    put(root, "out/report.txt", "first\n")
    assert await command(repo, ANNA, "make report") is None
    put(root, "out/report.txt", "second, longer\n")  # deleted and written again by the next run
    assert await command(repo, ANNA, "make report again") is None
    assert repo.held()["out/report.txt"].detail == "make report again"
    assert await command(repo, ANNA, "ls")
    assert commits(root)[0][1:] == ("shell (new files): out/report.txt", ["out/report.txt"])
    assert git(root, "log", "-1", "--format=%b").startswith("make report again\n\n")


async def test_held_files_of_another_member_stay_out_of_its_commits(repo):
    root = repo.root
    put(root, "shared.txt", "v1\n")
    assert await repo.autocommit(ANNA, "seed")
    put(root, "anna/notes.md", "a\n")
    put(root, "anna/tmp.log", "t\n")
    assert await command(repo, ANNA, "python anna.py") is None
    # Boris's command edits a tracked file and makes a file of its own: his commit has the edit only
    put(root, "shared.txt", "v2\n")
    put(root, "boris.txt", "b\n")
    assert await command(repo, BORIS, "python boris.py")
    assert commits(root)[0] == (BORIS, "shell: shared.txt", ["shared.txt"])
    (root / "anna" / "tmp.log").unlink()  # deleted by a command of either
    # Boris's next command commits his own new file, never Anna's (held for her: her next command decides)
    assert await command(repo, BORIS, "ls")
    assert commits(root)[0] == (BORIS, "shell (new files): boris.txt", ["boris.txt"])
    assert sorted(repo.held()) == ["anna/notes.md"] and repo.held()["anna/notes.md"].owner == ANNA
    assert await command(repo, ANNA, "ls")
    author, subject, files = commits(root)[0]
    assert (author, subject, files) == (ANNA, "shell (new files): anna/notes.md", ["anna/notes.md"])
    assert f"first seen after this command of {ANNA}" in git(root, "log", "-1", "--format=%b")
    assert "anna/tmp.log" not in history_files(root) and git(root, "status", "--porcelain") == ""


async def test_leftovers_of_another_members_running_command_wait_for_its_next_one(repo, cmds):
    # Boris's long test run leaves files while Anna's quick command ends: her commit sees them first
    root = repo.root
    run = cmds.start(BORIS)
    for path in LEFTOVERS:
        put(root, path, "x\n")
    assert await command(repo, ANNA, "cat README.md") is None
    held = repo.held()[LEFTOVERS[0]]
    assert (held.owner, held.others, held.waiting, held.running) == (ANNA, (BORIS,), {ANNA, BORIS}, {run: BORIS})
    assert await command(repo, ANNA, "ls") is None  # her next command alone does not decide
    assert await ends(repo, BORIS, run, "pytest -q") is None  # the end of the run that may have left them
    assert sorted(repo.held()) == LEFTOVERS
    shutil.rmtree(root / "tests")  # his next run deletes them first
    assert await command(repo, BORIS, "pytest -q") is None
    assert repo.held() == {} and not history_files(root) & set(LEFTOVERS)


async def test_a_file_seen_while_another_member_ran_a_command_names_both(repo, cmds):
    root = repo.root
    run = cmds.start(BORIS)
    put(root, "out/result.json", "{}\n")
    assert await command(repo, ANNA, "python solve.py") is None
    assert await ends(repo, BORIS, run, "pytest -q") is None
    assert await command(repo, ANNA, "ls") is None
    assert await command(repo, BORIS, "ls")  # it has outlived the next command of each: committed under Anna
    assert commits(root)[0] == (ANNA, "shell (new files): out/result.json", ["out/result.json"])
    assert git(root, "log", "-1", "--format=%b").strip() == "python solve.py\n\n" + workspace_mod.HELD_NOTE.format(
        owner=ANNA, others=f" ({BORIS} also had a command running then; git cannot tell who wrote a file)",
        when=f"after the next commands of {ANNA} and {BORIS} left them in place")


async def test_a_command_of_the_actor_still_running_as_a_job_holds_its_files(repo, cmds):
    # Anna's test run went on as a job; her quick commands meanwhile (reading its log) do not count for its files
    root = repo.root
    job = cmds.start(ANNA)
    for path in LEFTOVERS:
        put(root, path, "x\n")
    assert await command(repo, ANNA, "cat .scratch/anna.smith/jobs/j1.log") is None
    assert repo.held()[LEFTOVERS[0]].running == {job: ANNA}
    assert await command(repo, ANNA, "ls") is None
    assert await command(repo, ANNA, "ls tests") is None
    assert sorted(repo.held()) == LEFTOVERS
    shutil.rmtree(root / "tests")  # the job's teardown deletes them: no add/delete commits
    assert await ends(repo, ANNA, job, "pytest -q") is None
    assert repo.held() == {} and not history_files(root) & set(LEFTOVERS)


async def test_files_a_job_leaves_at_its_end_wait_for_a_command_started_after_it(repo, cmds):
    root = repo.root
    job = cmds.start(ANNA)
    put(root, "report.md", "r\n")
    assert await command(repo, ANNA, "cat log") is None  # first seen while the job runs
    later = cmds.start(ANNA)  # started before the job ended: it does not count
    for path in LEFTOVERS:
        put(root, path, "x\n")
    assert await ends(repo, ANNA, job, "pytest -q") is None  # the job's end leaves them: they wait
    assert await ends(repo, ANNA, later, "ls") is None
    assert sorted(repo.held()) == sorted([*LEFTOVERS, "report.md"])
    shutil.rmtree(root / "tests")  # the next run deletes its leftovers; the report stays and is committed
    assert await command(repo, ANNA, "pytest -q")
    assert commits(root)[0] == (ANNA, "shell (new files): report.md", ["report.md"])
    assert repo.held() == {} and not history_files(root) & set(LEFTOVERS)


async def test_a_flush_waits_for_the_commands_that_may_have_written_the_files(repo, cmds, monkeypatch):
    root = repo.root
    run = cmds.start(BORIS)
    put(root, "a.txt", "a\n")
    assert await command(repo, ANNA, "python a.py") is None
    real_git = repo._git

    async def no_git(*args, **kwargs):
        raise AssertionError("git ran")

    monkeypatch.setattr(repo, "_git", no_git)
    assert await repo.commit_held("Dana White", "at the end of Dana White's turn") is None  # holds nothing of hers
    # the end of Anna's turn while Boris's run that may have left a.txt goes on: nothing committed, no git call
    assert await repo.commit_held(ANNA, f"at the end of {ANNA}'s turn") is None
    assert await repo.commit_held(BORIS, f"at the end of {BORIS}'s turn") is None  # his run is still going
    monkeypatch.setattr(repo, "_git", real_git)
    assert repo.held()["a.txt"].waiting == {BORIS}  # Anna's turn has ended since it was first seen
    assert await ends(repo, BORIS, run, "pytest -q") is None
    put(root, "c.txt", "c\n")
    assert await command(repo, "Carl Lee", "python c.py") is None
    # Boris's task completion counts as his next command: a.txt is committed under Anna, c.txt is not his
    assert await repo.commit_held(BORIS, f"when {BORIS} completed task #3")
    assert commits(root)[0] == (ANNA, "shell (new files): a.txt", ["a.txt"])
    assert f"committed when {BORIS} completed task #3" in git(root, "log", "-1", "--format=%b")
    assert list(repo.held()) == ["c.txt"]


async def test_a_flush_does_not_commit_what_the_members_own_job_may_still_delete(repo, cmds):
    root = repo.root
    job = cmds.start(ANNA)
    for path in LEFTOVERS:
        put(root, path, "x\n")
    assert await command(repo, ANNA, "cat log") is None
    assert await repo.commit_held(ANNA, f"at the end of {ANNA}'s turn") is None  # she waits for her job
    shutil.rmtree(root / "tests")
    assert await ends(repo, ANNA, job, "pytest -q") is None
    assert repo.held() == {} and not history_files(root) & set(LEFTOVERS)


async def test_a_member_leaving_is_no_suspect_any_more(repo, cmds):
    root = repo.root
    run = cmds.start(BORIS)
    put(root, "a.txt", "a\n")
    assert await command(repo, ANNA, "python a.py") is None
    cmds.end(run)  # stopped as Boris leaves
    assert await repo.commit_held(BORIS, f"when {BORIS} left the team", leaving=True) is None  # Anna's turn goes on
    assert repo.held()["a.txt"].waiting == {ANNA} and repo.held()["a.txt"].running == {}
    assert await command(repo, ANNA, "ls")
    assert commits(root)[0] == (ANNA, "shell (new files): a.txt", ["a.txt"])


async def test_a_member_whose_commit_waits_for_the_lock_counts_as_running(repo):
    put(repo.root, "made.txt", "m\n")
    async with repo._git_lock:  # both commands have ended; their commits queue for the lock
        first = asyncio.create_task(command(repo, BORIS, "python b.py"))
        second = asyncio.create_task(command(repo, ANNA, "python a.py"))
        await asyncio.sleep(0.05)
    assert await first is None and await second is None
    held = repo.held()["made.txt"]
    assert (held.owner, held.others, held.waiting) == (BORIS, (ANNA,), {BORIS, ANNA})


async def test_a_moved_tracked_file_is_committed_at_once_with_its_old_path(repo):
    root = repo.root
    put(root, "src/app.py", "print('app')\n")
    put(root, "docs/a.md", "a\n")
    assert await repo.autocommit(ANNA, "seed")
    (root / "src" / "app.py").rename(root / "src" / "main.py")  # `mv`: the new path is untracked
    git(root, "mv", "docs/a.md", "docs/b.md")  # `git mv`: staged
    put(root, "src/junk.txt", "x\n")
    assert await command(repo, ANNA, "mv src/app.py src/main.py && git mv docs/a.md docs/b.md")
    assert commits(root)[0] == (ANNA, "shell: docs/a.md, docs/b.md, src/app.py, src/main.py",
                                ["docs/b.md", "src/main.py"])  # (`git log` shows a rename by its new path)
    assert "R100\tsrc/app.py\tsrc/main.py" in git(root, "show", "-M", "--name-status", "--format=", "HEAD")
    assert list(repo.held()) == ["src/junk.txt"]


async def test_holding_keeps_nested_repositories_and_the_scratch_folder_out(repo, caplog):
    root = repo.root
    nested_repo(root, "gen/proj")
    put(root, scratch_rel(ANNA) + "probe.py", "x\n")
    put(root, ".scratch/loose.txt", "x\n")
    put(root, "gen/README.md", "r\n")
    skipped: list[str] = []
    assert await repo.autocommit(ANNA, kind="shell", detail="make", skipped=skipped, hold=True) is None
    assert skipped == ["gen/proj"] and sorted(repo.held()) == ["gen/README.md"]
    assert await command(repo, ANNA, "ls")
    assert commits(root)[0][2] == ["gen/README.md"]
    assert gitlinks(root) == [] and not any(path.startswith(".scratch") for path in history_files(root))
    put(root, "gen/README.md", "r2\n")  # the tracked-only `git add -u` with the [.]scratch excludes: no add errors
    assert await command(repo, ANNA, "edit")
    assert "git add reported errors" not in caplog.text


async def test_many_held_files_go_to_git_through_stdin(repo, monkeypatch):
    # beyond PATHSPEC_ARGV_MAX the pathspecs go through stdin (--pathspec-from-file): Windows limits a command line
    monkeypatch.setattr(workspace_mod, "PATHSPEC_ARGV_MAX", 40)
    through_stdin: list[str] = []
    real_git = repo._git

    async def spy(actor, *args, **kwargs):
        if "--pathspec-from-file=-" in args:
            through_stdin.append(args[0])
        return await real_git(actor, *args, **kwargs)

    monkeypatch.setattr(repo, "_git", spy)
    root = repo.root
    put(root, "keep.txt", "k\n")
    assert await repo.autocommit(ANNA, "seed")
    mine = [f"many/a{i}[1].txt" for i in range(30)]  # "[1]": matched literally, no glob
    for path in mine:
        put(root, path, "x\n")
    assert await command(repo, ANNA, "python gen.py") is None
    put(root, "many/boris.txt", "b\n")  # one of the folder's files is Boris's: no single pathspec for the folder
    put(root, "keep.txt", "k2\n")
    assert await command(repo, BORIS, "python boris.py")
    assert commits(root)[0] == (BORIS, "shell: keep.txt", ["keep.txt"])
    assert await command(repo, BORIS, "ls")  # his own new file, none of Anna's
    assert commits(root)[0] == (BORIS, "shell (new files): many/boris.txt", ["many/boris.txt"])
    assert await command(repo, ANNA, "ls")
    assert commits(root)[0][0] == ANNA and sorted(commits(root)[0][2]) == sorted(mine)
    assert git(root, "status", "--porcelain") == ""
    assert {"add", "commit"} <= set(through_stdin)


async def test_write_file_commits_a_held_file_at_once(repo):
    put(repo.root, "plan.md", "draft\n")
    assert await command(repo, ANNA, "echo draft > plan.md") is None
    await repo.write(BORIS, "plan.md", "final\n")  # write_file / edit_file keep committing their file at once
    assert commits(repo.root)[0] == (BORIS, "write plan.md", ["plan.md"])
    assert await command(repo, ANNA, "ls") is None and repo.held() == {}


async def test_a_held_file_git_cannot_commit_now_stays_held_and_unstaged(repo, monkeypatch):
    put(repo.root, "new.txt", "n\n")
    assert await command(repo, ANNA, "make") is None
    real_git = repo._git

    async def failing_commit(actor, *args, **kwargs):
        if args[:1] == ("commit",) and "--" in args:  # the commit of held files (a merge in progress, say)
            return workspace_mod.subprocess.CompletedProcess(args, 1, b"", b"fatal: cannot do a partial commit")
        return await real_git(actor, *args, **kwargs)

    monkeypatch.setattr(repo, "_git", failing_commit)
    assert await command(repo, ANNA, "ls") is None
    assert list(repo.held()) == ["new.txt"] and git(repo.root, "status", "--porcelain") == "?? new.txt\n"
    monkeypatch.setattr(repo, "_git", real_git)
    assert await command(repo, BORIS, "ls")  # it has waited enough: retried at the next autocommit of anyone
    assert commits(repo.root)[0] == (ANNA, "shell (new files): new.txt", ["new.txt"])


async def test_without_hold_everything_is_committed_now(repo):
    put(repo.root, "new.txt", "n\n")
    assert await command(repo, ANNA, "make") is None
    assert await repo.autocommit(BORIS, "manual")  # an explicit commit of the whole tree
    assert commits(repo.root)[0] == (BORIS, "manual", ["new.txt"]) and repo.held() == {}


async def test_commit_held_without_held_files_runs_no_git(repo, monkeypatch):
    async def no_git(*args, **kwargs):
        raise AssertionError("git ran")

    monkeypatch.setattr(repo, "_git", no_git)
    assert await repo.commit_held(ANNA, "at the end of Anna's turn") is None
    assert await repo.commit_held(None, "when the session stopped") is None


async def test_a_command_that_only_made_new_files_tries_no_commit(repo, monkeypatch, caplog):
    calls: list[str] = []
    real_git = repo._git

    async def spy(actor, *args, **kwargs):
        calls.append(args[0])
        return await real_git(actor, *args, **kwargs)

    monkeypatch.setattr(repo, "_git", spy)
    put(repo.root, "new.txt", "n\n")
    assert await command(repo, ANNA, "make") is None
    assert calls == ["status"] and "git commit skipped" not in caplog.text


async def test_a_big_move_keeps_its_rename_in_one_commit(repo, monkeypatch):
    # beyond PATHSPEC_ARGV_MAX too: the deleted files' blobs come from `git cat-file --batch-check` through stdin
    monkeypatch.setattr(workspace_mod, "PATHSPEC_ARGV_MAX", 40)
    root = repo.root
    for i in range(10):
        put(root, f"old/file_{i:03}.py", f"print({i})\n")
    assert await repo.autocommit(ANNA, "seed")
    (root / "old").rename(root / "new")
    assert await command(repo, ANNA, "mv old new")
    assert repo.held() == {}
    status = git(root, "show", "-M", "--name-status", "--format=", "HEAD").splitlines()
    assert len(status) == 10 and all(line.startswith("R100\told/") for line in status)


async def test_a_git_without_pathspec_from_file_gets_the_command_line(repo, monkeypatch):
    monkeypatch.setattr(workspace_mod, "PATHSPEC_ARGV_MAX", 40)
    real_git = repo._git

    async def old_git(actor, *args, **kwargs):  # git before 2.25
        if "--pathspec-from-file=-" in args:
            return workspace_mod.subprocess.CompletedProcess(args, 129, b"",
                                                             b"error: unknown option `pathspec-from-file=-'\n")
        return await real_git(actor, *args, **kwargs)

    monkeypatch.setattr(repo, "_git", old_git)
    mine = [f"many/a{i}.txt" for i in range(12)]
    put(repo.root, "many/keep.txt", "k\n")
    assert await repo.autocommit(ANNA, "seed")  # (a tracked file: the folder's new files are named one by one)
    for path in mine:
        put(repo.root, path, "x\n")
    assert await command(repo, ANNA, "python gen.py") is None
    assert await command(repo, ANNA, "ls")
    assert sorted(commits(repo.root)[0][2]) == sorted(mine) and repo.held() == {}


# --------------------------------------------------------------------------------- flush points in the swarm
@pytest.fixture
async def swarm(tmp_path):
    s = Swarm(config=swarm_config(tmp_path, True), llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok"))
    await s.start(run_agents=False)
    yield s
    await s.stop()
    s.close()


async def tool(swarm: Swarm, actor: str, name: str, **args) -> str:
    text, is_error = await call_tool(ToolContext(swarm, actor), name, args)
    assert not is_error, text
    return text


def agent_of(swarm: Swarm) -> str:
    return next(m["name"] for m in swarm.members() if m["kind"] == AgentKind.LLM)


async def test_task_complete_commits_the_new_files_first(swarm):
    agent = agent_of(swarm)
    root = swarm.workspace.root
    task = swarm.board.create(USER, "Write the report", goal="A report", assignee=agent)
    await tool(swarm, agent, "shell", command="echo done > report.txt")
    assert "report.txt" not in history_files(root)
    await tool(swarm, agent, "task_complete", task_id=task.id, result="report.txt")
    assert commits(root)[0] == (agent, "shell (new files): report.txt", ["report.txt"])
    assert f"committed when {agent} completed task #{task.id}" in git(root, "log", "-1", "--format=%b")
    assert swarm.workspace.held() == {}


async def test_a_job_of_another_member_running_makes_it_wait_for_that_member(swarm):
    agent = agent_of(swarm)
    ext = await swarm.hire(USER, "Developer", name="Claude Code", kind=AgentKind.EXTERNAL)
    put(swarm.workspace.root, ".scratch/sleep.py", "import time\ntime.sleep(60)\n")
    await tool(swarm, ext, "shell", command=python(swarm, ".scratch/sleep.py"), background=True)
    running, _last = swarm.jobs.running_commands()
    assert list(running.values()) == [ext]
    await tool(swarm, agent, "shell", command="echo x > seen.txt")
    held = swarm.workspace.held()["seen.txt"]
    assert (held.owner, held.others, held.running) == (agent, (ext,), running)
    # the end of the agent's turn while the other member's job runs: it waits for that job (and ext's next command)
    assert await swarm.workspace.commit_held(agent, f"at the end of {agent}'s turn") is None
    assert swarm.workspace.held()["seen.txt"].waiting == {ext}


async def test_an_agents_own_job_holds_what_it_leaves_until_it_ends(swarm):
    # a test run that goes on as a job leaves files and deletes them at its end; the agent's quick commands meanwhile
    # (and the end of its turn) do not commit them
    agent = agent_of(swarm)
    root = swarm.workspace.root
    put(root, ".scratch/run.py", "import pathlib, time\np = pathlib.Path('tests/test-hooks/input.json')\n"
                                 "p.parent.mkdir(parents=True, exist_ok=True)\np.write_text('x')\n"
                                 "time.sleep(4)\np.unlink()\np.parent.rmdir()\n")
    await tool(swarm, agent, "shell", command=python(swarm, ".scratch/run.py"), background=True)
    await run_until(lambda: (root / "tests/test-hooks/input.json").exists(), 10)
    await tool(swarm, agent, "shell", command="echo reading the log")
    assert list(swarm.workspace.held()) == ["tests/test-hooks/input.json"]
    assert await swarm.workspace.commit_held(agent, f"at the end of {agent}'s turn") is None
    await run_until(lambda: not swarm.jobs.running_of() and not swarm.jobs._finishes, 20)
    assert swarm.workspace.held() == {} and "tests/test-hooks/input.json" not in history_files(root)


async def test_a_refused_task_complete_commits_nothing(swarm):
    agent = agent_of(swarm)
    ext = await swarm.hire(USER, "Developer", name="Claude Code", kind=AgentKind.EXTERNAL)
    task = swarm.board.create(USER, "Write the report", goal="A report", assignee=agent)
    await tool(swarm, ext, "shell", command="echo x > draft.txt")
    text, is_error = await call_tool(ToolContext(swarm, ext), "task_complete", {"task_id": task.id, "result": "x"})
    assert is_error and "draft.txt" not in history_files(swarm.workspace.root)
    assert list(swarm.workspace.held()) == ["draft.txt"]


async def test_a_member_leaving_commits_its_new_files(swarm):
    agent = agent_of(swarm)
    root = swarm.workspace.root
    await tool(swarm, agent, "shell", command="echo x > notes.txt")
    await swarm.retire(USER, agent)
    assert commits(root)[0] == (agent, "shell (new files): notes.txt", ["notes.txt"])
    assert f"committed when {agent} left the team" in git(root, "log", "-1", "--format=%b")


async def test_an_external_members_wait_commits_its_new_files(swarm):
    ext = await swarm.hire(USER, "Developer", name="Claude Code", kind=AgentKind.EXTERNAL)
    await tool(swarm, ext, "shell", command="echo x > ext.txt")
    await tool(swarm, ext, "wait_for_events", timeout=0)
    assert commits(swarm.workspace.root)[0] == (ext, "shell (new files): ext.txt", ["ext.txt"])


async def test_the_session_stop_commits_every_members_new_files(tmp_path):
    s = Swarm(config=swarm_config(tmp_path, True), llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok"))
    await s.start(run_agents=False)
    agent = agent_of(s)
    root = s.workspace.root
    try:
        await tool(s, agent, "shell", command="echo x > left.txt")
        assert "left.txt" not in history_files(root)
    finally:
        await s.stop()
        s.close()
    assert commits(root)[0] == (agent, "shell (new files): left.txt", ["left.txt"])
    assert "committed when the session stopped" in git(root, "log", "-1", "--format=%b")


async def test_the_end_of_an_agents_turn_commits_its_new_files(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if any(m["role"] == "tool" and m.get("name") == "shell" for m in messages):
            return call("wait")
        return call("shell", command="echo made > made.txt")

    swarm, _recorder = make_policy_swarm(tmp_path, policy, workspace={"git": True})
    await swarm.start()
    try:
        root = swarm.workspace.root
        agent = agent_of(swarm)
        swarm.notify(agent, "n_hired", by=USER, role="Generalist")
        await run_until(lambda: "made.txt" in history_files(root), 30)
        assert commits(root)[0] == (agent, "shell (new files): made.txt", ["made.txt"])
        assert f"committed at the end of {agent}'s turn" in git(root, "log", "-1", "--format=%b")
    finally:
        await shutdown(swarm)


async def test_an_agent_that_is_stopping_leaves_its_new_files_to_the_stop(tmp_path, monkeypatch):
    def policy(swarm, name, system, messages, tools):
        if any(m["role"] == "tool" and m.get("name") == "shell" for m in messages):
            swarm.runtimes[name].request_stop()  # (as Swarm.stop or retire do): its activation ends now
            return "stopping"
        return call("shell", command="echo made > made.txt")

    swarm, recorder = make_policy_swarm(tmp_path, policy, workspace={"git": True})
    await swarm.start()
    flushes: list[str] = []
    real = swarm.workspace.commit_held

    async def spy(actor, when, **kwargs):
        flushes.append(when)
        return await real(actor, when, **kwargs)

    monkeypatch.setattr(swarm.workspace, "commit_held", spy)
    root = swarm.workspace.root
    agent = agent_of(swarm)
    try:
        swarm.notify(agent, "n_hired", by=USER, role="Generalist")
        await run_until(lambda: recorder.count(agent) >= 2, 30)
        await asyncio.sleep(0.3)  # the activation ends
        assert flushes == [] and "made.txt" not in history_files(root)
    finally:
        await shutdown(swarm)
    assert flushes == ["when the session stopped"]
    assert commits(root)[0] == (agent, "shell (new files): made.txt", ["made.txt"])
