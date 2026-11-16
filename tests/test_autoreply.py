"""autoreply.py: automatic answers in the user's place (docs/TUI.md §2, docs/SPEC.md §21.5)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import dreamteam
from dreamteam import autoreply
from dreamteam.autoreply import AUTO_REPLY, AUTO_REVIEW, reply_text, review_key, review_text, wants_reply
from dreamteam.tui import printmode


class TestPure:
    @pytest.mark.parametrize("msg, wanted", [
        ({"id": 3, "sender": "Anna Smith", "recipients": ["user"]}, True),
        ({"id": 3, "sender": "Anna Smith", "recipients": ["Boris Lee", "user"]}, True),
        ({"id": 3, "sender": "user", "recipients": ["user"]}, False),          # the user's own
        ({"id": 3, "sender": "Anna Smith", "recipients": ["Boris Lee"]}, False),
        ({"id": 3, "sender": "Anna Smith", "recipients": []}, False),
        ({"id": 3, "sender": "Anna Smith", "recipients": None}, False),
        ({"id": 3, "sender": "Anna Smith"}, False),
        ({"id": "3", "sender": "Anna Smith", "recipients": ["user"]}, False),  # no journaled id
        ({"sender": "Anna Smith", "recipients": ["user"]}, False),
    ])
    def test_wants_reply(self, msg, wanted):
        assert wants_reply(msg) is wanted

    def test_review_key(self):
        task = {"id": 7, "title": "Write", "status": "review", "reviewer": "user", "updated_at": 12.5}
        assert review_key(task) == (7, 12.5)
        assert review_key({**task, "updated_at": 13.0}) != review_key(task)   # submitted again: another round
        assert review_key({**task, "updated_at": None}) == (7, None)
        assert review_key({k: v for k, v in task.items() if k != "updated_at"}) == (7, None)
        assert review_key({**task, "reviewer": "Anna Smith"}) is None        # a member reviews it
        assert review_key({**task, "reviewer": None}) is None
        for status in ("todo", "in_progress", "waiting", "done", "failed", "cancelled"):
            assert review_key({**task, "status": status}) is None
        assert review_key({**task, "id": "7"}) is None
        assert review_key({}) is None

    def test_texts(self):
        assert set(AUTO_REPLY) == set(AUTO_REVIEW) == {"ru", "en"}
        for lang in ("ru", "en"):
            assert reply_text(lang) == AUTO_REPLY[lang] and review_text(lang) == AUTO_REVIEW[lang]
        for lang in (None, "", "de"):
            assert reply_text(lang) == AUTO_REPLY["en"] and review_text(lang) == AUTO_REVIEW["en"]

    def test_print_mode_uses_the_same_texts_and_limit(self):
        assert printmode.AUTO_REPLY is autoreply.AUTO_REPLY and printmode.AUTO_REVIEW is autoreply.AUTO_REVIEW
        assert printmode.AUTO_REPLY_MAX == autoreply.AUTO_REPLY_MAX == 50


def test_the_shared_run_modules_import_no_tui():
    """Hub code imports autoreply, result and instruct: none of them may pull in dreamteam.tui, whose package imports
    prompt_toolkit (a fresh, isolated interpreter, with this test run's sources first on its path)."""
    src = str(Path(dreamteam.__file__).resolve().parent.parent)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import dreamteam.autoreply, dreamteam.instruct, dreamteam.result\n"
        f"assert dreamteam.__file__.startswith({src!r}), dreamteam.__file__\n"
        "bad = sorted(m for m in sys.modules if m == 'prompt_toolkit' or m.startswith(('prompt_toolkit.',"
        " 'dreamteam.tui')))\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
