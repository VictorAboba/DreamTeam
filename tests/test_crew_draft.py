"""The draft of a crew task's result (docs/SPEC.md §4.2 "Finishing": items, cuts, the budget B)."""

from __future__ import annotations

import random
import re

import pytest

from dreamteam import prompts
from dreamteam.board import CREW_DRAFT_CHARS, CREW_DRAFT_MIN, NOTIFY_TEXT_LIMIT, Board
from dreamteam.models import Status
from helpers.crew import finish_raw, make_crew_raw

ANNA, BOB, CARL, EVA = "Anna Smith", "Bob Brown", "Carl Clark", "Eva Orlova"


@pytest.fixture
def swarm(fake_swarm):
    fake_swarm.board = Board(fake_swarm)
    for name in (ANNA, BOB, CARL):
        fake_swarm.add_member(name, language="en")
    fake_swarm.add_member(EVA, language="ru")
    return fake_swarm


@pytest.fixture
def board(swarm) -> Board:
    return swarm.board


def header(lang: str, tid: int) -> str:
    return "\n" + prompts.t(lang, "crew_draft", id=tid, items="")


def more(lang: str, k: int, tid: int) -> str:
    return prompts.t(lang, "crew_draft_more", k=k, id=tid)


def pointer(lang: str, tid: int) -> str:
    return "    " + prompts.t(lang, "crew_draft_cut", id=tid)


def crew_with_results(board: Board, results: list[str | None], crew: int = 3):
    """A crew task with one direction per entry: a str is a done direction with that result (held by BOB), None
    a cancelled one."""
    task, subs = make_crew_raw(board, ANNA, crew, [f"part {i}" for i in range(len(results))])
    for sub, result in zip(subs, results):
        if result is None:
            finish_raw(board, sub.id, Status.CANCELLED)
        else:
            finish_raw(board, sub.id, Status.DONE, result, assignee=BOB)
    return board.get(task.id), [board.get(s.id) for s in subs]


# --- items ----------------------------------------------------------------------------------------------------

def test_items_in_creation_order_with_holder_results_and_cancelled_lines(board):
    task, subs = make_crew_raw(board, ANNA, 3, ["parser", "cli", "docs", "spare"], priority=2)
    board.db.execute("UPDATE tasks SET priority = 3 WHERE id = ?", (subs[2].id,))  # order is by id, not priority
    finish_raw(board, subs[0].id, Status.DONE, "line one\nline two", assignee=BOB)
    finish_raw(board, subs[1].id, Status.CANCELLED)
    finish_raw(board, subs[2].id, Status.DONE, "", assignee=CARL)
    finish_raw(board, subs[3].id, Status.DONE, None)
    draft = board._crew_draft(task, "en")
    assert draft == (
        f"\nDraft for #{task.id}'s result: its directions in the order they were created, with the results of the "
        "done ones:\n"
        f"- #{subs[0].id} [done] \"parser\" @{BOB}\n    line one\n    line two\n"
        f"- #{subs[1].id} [cancelled] \"cli\"\n"
        f"- #{subs[2].id} [done] \"docs\" @{CARL}\n    (no result)\n"
        f"- #{subs[3].id} [done] \"spare\"\n    (no result)")


def test_a_long_title_is_cut_like_the_board_listing(board):
    task, (sub,) = make_crew_raw(board, ANNA, 2, ["x" * 150])
    finish_raw(board, sub.id, Status.DONE, "ok", assignee=BOB)
    line = board._crew_draft(task, "en").split("\n")[2]
    assert line == f"- #{sub.id} [done] \"{'x' * 79}…\" @{BOB}"


def test_a_failed_direction_has_its_line_and_no_result(board):
    task, subs = make_crew_raw(board, ANNA, 2, ["a", "b"])
    finish_raw(board, subs[0].id, Status.FAILED, "it broke", assignee=BOB)
    finish_raw(board, subs[1].id, Status.DONE, "fine", assignee=CARL)
    draft = board._crew_draft(task, "en")
    assert f"- #{subs[0].id} [failed] \"a\" @{BOB}\n- #{subs[1].id}" in draft
    assert "it broke" not in draft


def test_no_directions_gives_no_draft(board):
    task = board.create(ANNA, "solo")
    assert board._crew_draft(task, "en") == ""


def test_the_russian_draft(board):
    task, subs = make_crew_raw(board, ANNA, 2, ["парсер", "cli"])
    finish_raw(board, subs[0].id, Status.DONE, "готово", assignee=EVA)
    finish_raw(board, subs[1].id, Status.DONE, "", assignee=BOB)
    draft = board._crew_draft(task, "ru")
    assert draft == (f"\nЧерновик итога #{task.id}: её направления в порядке создания, с итогами выполненных:\n"
                     f"- #{subs[0].id} [done] «парсер» @{EVA}\n    готово\n"
                     f"- #{subs[1].id} [done] «cli» @{BOB}\n    (итога нет)")


def test_the_ru_twins_keep_the_placeholders():
    for key in ("crew_draft", "crew_draft_cut", "crew_draft_more", "crew_draft_no_result"):
        en, ru = prompts.TEXTS["en"][key], prompts.TEXTS["ru"][key]
        assert set(re.findall(r"{(\w+)}", en)) == set(re.findall(r"{(\w+)}", ru)), key
        assert en != ru


# --- the budget -----------------------------------------------------------------------------------------------

def test_the_budget_follows_tool_output_chars_at_each_call(swarm, board):
    assert board.draft_budget() == min(CREW_DRAFT_CHARS, swarm.config.agents.tool_output_chars // 2)
    swarm.config.agents.tool_output_chars = 1000
    assert board.draft_budget() == 500
    swarm.config.agents.tool_output_chars = 100_000
    assert board.draft_budget() == CREW_DRAFT_CHARS


def test_the_default_budget_is_draft_budget(swarm, board):
    swarm.config.agents.tool_output_chars = 1000
    task, _ = crew_with_results(board, ["r" * 2000] * 3)
    assert len(board._crew_draft(task, "en")) <= 500


def test_the_spec_case_eight_long_results_in_500_characters(swarm, board):
    swarm.config.agents.tool_output_chars = 1000  # B = 500
    task, subs = crew_with_results(board, ["y" * 2000] * 8)
    draft = board._crew_draft(task, "en")
    assert len(draft) <= 500
    lines = draft.split("\n")
    shown = [i for i, line in enumerate(lines) if line.startswith("- #")]
    assert 0 < len(shown) < 8
    for i in shown:  # every done item shown ends with its task_get pointer
        sid = int(re.match(r"- #(\d+)", lines[i]).group(1))
        assert lines[i + 1] == pointer("en", sid)
    assert lines[-1] == more("en", 8 - len(shown), task.id)


def test_a_cut_result_keeps_its_start_and_end_with_the_pointer(board):
    result = "START " + "m" * 5000 + " END"
    task, (sub,) = crew_with_results(board, [result])
    draft = board._crew_draft(task, "en", budget=8000)
    piece = draft.split("\n", 3)[3]
    assert len(piece) <= NOTIFY_TEXT_LIMIT  # no piece passes the cut of n_task_closed
    assert piece.startswith("    START ")
    assert piece.endswith(" END\n" + pointer("en", sub.id))
    assert "characters skipped] ...\n" in piece


def test_a_cut_that_would_keep_too_little_is_the_pointer_alone(board):
    task, subs = crew_with_results(board, ["z" * 3000, "q" * 3000])
    budget = 420  # the header, two lines and two shares far below CREW_DRAFT_MIN
    draft = board._crew_draft(task, "en", budget=budget)
    assert len(draft) <= budget
    assert draft.endswith(f"\n{pointer('en', subs[0].id)}\n- #{subs[1].id} [done] \"part 1\" @{BOB}\n"
                          f"{pointer('en', subs[1].id)}")
    assert "z" not in draft and "q" not in draft
    assert CREW_DRAFT_MIN == 300


def test_shortest_results_are_served_first(board):
    short = "short result"
    task, subs = crew_with_results(board, ["L" * 4000, short])
    draft = board._crew_draft(task, "en", budget=1200)
    assert len(draft) <= 1200
    assert f"    {short}\n" not in draft and draft.endswith(f"    {short}")  # the short one whole
    long_piece = draft.split(f"@{BOB}\n", 1)[1].split("\n- #")[0]
    assert long_piece.endswith(pointer("en", subs[0].id))
    assert long_piece.count("L") >= CREW_DRAFT_MIN  # the long one got the rest, cut


def test_a_short_result_shown_whole_however_small_the_budget_lets_items_in(board):
    task, subs = crew_with_results(board, ["ok"])
    full = board._crew_draft(task, "en", budget=CREW_DRAFT_CHARS)
    assert full.endswith("\n    ok")
    assert board._crew_draft(task, "en", budget=len(full)) == full


def test_more_counts_the_hidden_directions_k_1_and_k_2(board):
    # items longer than crew_draft_more, so that all but one can be shown
    task, subs = make_crew_raw(board, ANNA, 3, [f"{c} " + "t" * 90 for c in "abc"])
    for sub in subs:
        finish_raw(board, sub.id, Status.DONE, "r" * 3000, assignee=BOB)
    task = board.get(task.id)
    full = board._crew_draft(task, "en", budget=CREW_DRAFT_CHARS)
    seen: dict[int, str] = {}
    for budget in range(len(full) + 1):
        draft = board._crew_draft(task, "en", budget=budget)
        assert len(draft) <= budget
        found = re.search(r"more directions not shown: (\d+);", draft)
        if found and draft.count("- #"):
            seen.setdefault(int(found.group(1)), draft)
    assert set(seen) >= {1, 2}
    assert seen[1].endswith(more("en", 1, task.id)) and seen[1].count("- #") == 2
    assert seen[2].endswith(more("en", 2, task.id)) and seen[2].count("- #") == 1


def test_budgets_too_small_for_an_item_for_the_header_and_for_anything(board):
    task, subs = crew_with_results(board, ["a" * 3000] * 4)
    head, all_more = header("en", task.id), more("en", 4, task.id)
    line = f"- #{subs[0].id} [done] \"part 0\" @{BOB}"
    # room for the header and crew_draft_more but not for one item with its piece
    budget = len(head) + len(all_more) + len(line)
    assert board._crew_draft(task, "en", budget=budget) == head + all_more
    # not even for the header: crew_draft_more alone
    assert board._crew_draft(task, "en", budget=len(head) + len(all_more) - 1) == "\n" + all_more
    assert board._crew_draft(task, "en", budget=1 + len(all_more)) == "\n" + all_more
    # not for that either: no draft
    assert board._crew_draft(task, "en", budget=len(all_more)) == ""
    assert board._crew_draft(task, "en", budget=0) == ""


def test_the_russian_more_uses_the_colon_count(board):
    task, _ = crew_with_results(board, ["a" * 3000] * 8)
    draft = board._crew_draft(task, "ru", budget=400)
    assert len(draft) <= 400
    assert re.search(r"ещё не показано направлений: \d+;", draft)


@pytest.mark.parametrize("seed", range(12))
def test_the_draft_never_passes_its_budget(board, seed):
    rnd = random.Random(seed)
    sizes = [0, 1, 40, 299, 300, 1499, 1500, 2000, 5000, 20000]
    results: list[str | None] = []
    for _ in range(rnd.randint(1, 14)):
        if rnd.random() < 0.15:
            results.append(None)
            continue
        size = rnd.choice(sizes)
        text = "".join(rnd.choice("abc \n") for _ in range(size)).strip() if size else ""
        results.append(text)
    task, subs = crew_with_results(board, results)
    for budget in (0, 50, 100, 300, 500, 1000, 4000, 8000):
        for lang in ("en", "ru"):
            draft = board._crew_draft(task, lang, budget=budget)
            assert len(draft) <= budget, (seed, budget, lang)
            if not draft:
                continue
            assert draft.startswith("\n")
            lines = draft.split("\n")
            ids = [int(m.group(1)) for line in lines if (m := re.match(r"- #(\d+) ", line))]
            assert ids == [s.id for s in subs[:len(ids)]]  # a prefix, in creation order
            if len(ids) < len(subs):
                assert lines[-1] == more(lang, len(subs) - len(ids), task.id)


def test_several_calls_with_many_directions_stay_in_the_budget(board):
    task, subs = make_crew_raw(board, ANNA, 4, [f"d{i}" for i in range(30)])
    for i, sub in enumerate(subs):
        finish_raw(board, sub.id, Status.DONE, f"result {i} " * (i * 40), assignee=CARL)
    for budget in (500, 4000, 8000):
        draft = board._crew_draft(board.get(task.id), "en", budget=budget)
        assert 0 < len(draft) <= budget
