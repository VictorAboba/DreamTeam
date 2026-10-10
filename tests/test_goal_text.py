"""goal_text.py: how a goal typed as one text becomes {title, description} (docs/SPEC.md §3)."""

from __future__ import annotations

import pytest

from dreamteam.goal_text import GOAL_TITLE_MAX, shorten_title, split_goal

PARAGRAPH = ("Build a snake game in Python that runs in the terminal, keeps the high scores of every player and "
             "works on any computer. It needs arrow-key control, a score line, levels that speed up and a pytest "
             "suite for the logic of the board, the snake and the food, plus a README that explains how to install "
             "and run it. Keep the code small and readable.")
FIRST_SENTENCE = PARAGRAPH.split(". ")[0] + "."

# (title argument, description argument, title, description)
CASES = [
    ("Snake game", "", "Snake game", ""),
    ("Snake game\nPython, pygame\n\n- score\n  - records", "", "Snake game", "Python, pygame\n\n- score\n  - records"),
    ("\n\n  Snake   game \t \r\nPython\r\n\r\n", "", "Snake game", "Python"),
    ("# Snake game\nDetails", "", "Snake game", "Details"),
    ("###### Snake game", "", "Snake game", ""),
    ("#3 fix the crash", "", "#3 fix the crash", ""),
    ("####### Seven", "", "####### Seven", ""),
    ("Snake game\nPython", "Tests required", "Snake game", "Python\n\nTests required"),
    ("", "Snake game\nPython", "Snake game", "Python"),
    (" \n\t\n ", "Snake game\nPython", "Snake game", "Python"),
    ("", "", "", ""),
    ("Snake\rPython\rtests", "", "Snake", "Python\ntests"),
    (PARAGRAPH, "", FIRST_SENTENCE, PARAGRAPH),
]


@pytest.mark.parametrize("title, description, want_title, want_description", CASES)
def test_split_goal(title, description, want_title, want_description):
    assert split_goal(title, description) == (want_title, want_description)


def test_long_paragraph_keeps_its_first_sentence_and_loses_nothing():
    assert len(PARAGRAPH) > GOAL_TITLE_MAX and 100 < len(FIRST_SENTENCE) <= GOAL_TITLE_MAX
    title, description = split_goal(PARAGRAPH + "\nMore details", "Extra")
    assert title == FIRST_SENTENCE
    assert description == PARAGRAPH + "\nMore details\n\nExtra"


def test_long_line_without_sentences_is_cut_at_a_word():
    line = ("word " * 100).strip()
    title, description = split_goal(line)
    assert title.endswith("…") and len(title) <= GOAL_TITLE_MAX
    assert title == "word " * 39 + "word…"     # the last whole word that fits, then the ellipsis
    assert description == line


@pytest.mark.parametrize("line", [
    "x" * 500,                                   # no space at all: a hard cut
    "a. " + "y" * 400,                           # a sentence too short to make a title
    ("Sentence one is here. " * 20).strip(),     # several sentences fit
    "word, " * 80,                               # the cut drops the trailing punctuation
])
def test_shorten_title_never_exceeds_the_limit(line):
    for limit in (GOAL_TITLE_MAX, 60, 8):
        short = shorten_title(line, limit)
        assert 0 < len(short) <= limit
    assert shorten_title("short", 60) == "short"
    assert shorten_title(("Sentence one is here. " * 20).strip(), 60) == "Sentence one is here. Sentence one is here."


@pytest.mark.parametrize("title, description", [(c[0], c[1]) for c in CASES]
                         + [(PARAGRAPH + "\nMore", "Extra"), (("word " * 100).strip(), "")])
def test_split_goal_is_idempotent(title, description):
    once = split_goal(title, description)
    assert split_goal(*once) == once
