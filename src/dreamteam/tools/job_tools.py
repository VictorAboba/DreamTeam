"""Background jobs: shell commands that kept running after their call returned (jobs.py, docs/SPEC.md §7.1)."""

from __future__ import annotations

from typing import Any

from ..jobs import DETAIL_LINES, parse_job_id
from ..models import ToolError
from . import ToolContext, tool


def _job_id(value: Any) -> int:
    """The job a tool argument names ("j3", "J3", "%3", "#3" or "3")."""
    job_id = parse_job_id(value)
    if job_id is None:
        raise ToolError(f"Not a job id: {str(value)[:40]!r}. Pass one such as j3 (jobs without job_id lists them).")
    return job_id


@tool(
    "jobs",
    en="Background jobs: shell commands that kept running after their call returned (past their timeout, started "
       "in the background on purpose, or processes a command left running). Without job_id: the running jobs of "
       "the swarm and your jobs that ended in the last hour. With job_id: that job's state and the end of its "
       "output. You are notified when your job ends, so don't poll: do other work or call `wait`.",
    ru="Фоновые процессы — команды shell, которые продолжают работать после того, как вызов вернул результат (не "
       "уложились в timeout, запущены в фоне намеренно или остались работать после завершения команды). Без "
       "job_id — работающие фоновые процессы роя и твои, завершившиеся за последний час. С job_id — состояние "
       "процесса и конец его вывода. Когда твой процесс завершится, придёт уведомление, поэтому не опрашивай: "
       "займись другим делом или вызови `wait`.",
    properties={
        "job_id": {"type": "string", "description": "A job id such as j3; without it you get the list"},
        "tail": {"type": "integer", "description": "Lines of output to show (default 40, at most 400)"},
    },
)
async def jobs(ctx: ToolContext, args: dict[str, Any]) -> str:
    manager = ctx.swarm.jobs
    if not str(args.get("job_id") or "").strip():
        return manager.listing(ctx.actor)
    # any member may read any job; a live job is read from memory, an ended one from the end of its log
    return manager.describe(_job_id(args["job_id"]), actor=ctx.actor, lines=args.get("tail") or DETAIL_LINES,
                            max_chars=ctx.output_cap)


@tool(
    "job_stop",
    en="Stop a background job together with every process it started. Only its owner or the user can stop a job; "
       "its output stays in its log.",
    ru="Остановить фоновый процесс вместе со всеми процессами, которые он запустил. Остановить процесс может только "
       "его владелец или пользователь; его вывод остаётся в журнале.",
    properties={"job_id": {"type": "string", "description": "A job id such as j3"}},
    required=["job_id"],
)
async def job_stop(ctx: ToolContext, args: dict[str, Any]) -> str:
    # synchronous: the job and its processes are gone when this returns (a restart on the same port does not race)
    return await ctx.swarm.jobs.stop(_job_id(args["job_id"]), ctx.actor)
