"""The reads that the read-before-write rule (docs/SPEC.md §22.11) asks of a member before it writes to the forum,
for tests whose subject is something else: a member that has read every post and comment as they are now."""

from __future__ import annotations

from dreamteam.forum import post_version


def read_all(swarm, *names: str) -> None:
    """`names` have read every post and comment as they are now: what `post_read` of each post would mark."""
    marks: dict[str, str | None] = {f"p{r['id']}": post_version(r)
                                    for r in swarm.db.all("SELECT id, title, text FROM posts")}
    marks.update({f"c{r['id']}": None for r in swarm.db.all("SELECT id FROM post_comments")})
    for name in names:
        swarm.forum.mark_read(name, marks)
