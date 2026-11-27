"""The web UI of a thread session (docs/SPEC.md §22.8 "Surfaces"): the board page's thread tab (the feed of posts, the
open post with its comments, likes and dislikes, #1's vote and decision, the composers), the ⚙ dialog's mode line and
thread fields, the Events tab's forum lines, the sessions page's settings, new-session modal and card. Static checks
always run; the app.js / home.js helpers run in Node.js with the tiny DOM stub of tests/test_ui.py (skipped without
`node`); the last test renders a real thread hub's snapshot (in-process test client, ScriptedLLM, no network)."""

from __future__ import annotations

import json
import re

import pytest
from starlette.testclient import TestClient
from test_ui import (
    APP_JS,
    EVIL,
    HOME_HTML,
    HOME_JS,
    INDEX,
    QUESTION_HARNESS,
    SETTINGS_HARNESS,
    STATIC,
    assert_escaped,
    home_i18n,
    i18n_blocks,
    needs_node,
    run_js,
    used_keys,
)
from test_web_thread import BASE, llm_names, make_swarm

from dreamteam import web
from dreamteam.models import USER

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

APP_THREAD_KEYS = {
    "tab_thread", "tab_events", "search_posts", "new_post", "clarify_wait_thread", "sf_mode_line", "mode_board",
    "mode_thread", "sg_thread", "sf_goal_rule", "goal_rule_democracy", "goal_rule_veto", "sf_digest", "sf_digest_hint",
    "sf_plan_hint_thread", "sf_clarify_thread_hint", "rule_democracy", "rule_veto", "rule_user", "th_rule_line",
    "th_vote_bar", "th_decided_vote", "th_decided_user", "goal_vote_short", "mark_goal_reached", "reopen_goal",
    "goal_summary_opt", "goal_rewrite_note", "goal_rewrite_ask", "th_subs", "sub_auto",
}
# the ev_* strings of the six forum events (docs/SPEC.md §22.2) and of a decided goal
EV_KEYS = {
    "ev_post_created", "ev_post_created_system", "ev_post_updated", "ev_post_rewritten", "ev_post_votes_reset",
    "ev_post_comment", "ev_vote_like", "ev_vote_dislike", "ev_vote_none", "ev_sub_created", "ev_sub_changed",
    "ev_sub_removed", "ev_subs_dropped", "ev_sub_feed", "ev_sub_reactions", "ev_sub_workspace", "ev_files_committed",
    "ev_commit_shell", "ev_commit_job", "ev_commit_jobs", "ev_commit_held", "ev_goal_vote", "ev_goal_user",
}
HOME_THREAD_KEYS = {
    "f_mode", "f_mode_board", "f_mode_board_hint", "f_mode_thread", "f_mode_thread_hint", "f_rule", "f_rule_democracy",
    "f_rule_veto", "f_clarify_thread_hint", "thread_badge", "goal_votes", "posts_n", "sw_mode", "sw_mode_hint",
    "sw_goal_rule", "sw_goal_rule_hint", "sw_rule_democracy", "sw_rule_veto", "ag_digest", "ag_digest_hint",
}


def ru_values(src: str, keys: set[str]) -> dict[str, str]:
    """The Russian texts of `keys` in a script's I18N table (its ru block comes first)."""
    ru = src.split("\n  en: {", 1)[0]
    return {key: m.group(1) for key in keys if (m := re.search(rf"\b{key}: \"((?:[^\"\\]|\\.)*)\"", ru))}


def between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


# ------------------------------------------------------------------------------------------------ static checks
def test_thread_texts_exist_in_both_languages_without_yo():
    app = i18n_blocks()
    home = home_i18n()
    for lang in ("ru", "en"):
        assert not sorted((APP_THREAD_KEYS | EV_KEYS) - app[lang]), lang
        assert not sorted(HOME_THREAD_KEYS - home[lang]), lang
    texts = {**ru_values(APP_JS, APP_THREAD_KEYS | EV_KEYS), **ru_values(HOME_JS, HOME_THREAD_KEYS)}
    assert len(texts) == len(APP_THREAD_KEYS | EV_KEYS | HOME_THREAD_KEYS)
    assert not [key for key, text in texts.items() if "ё" in text]
    # the SPEC's own wordings
    assert texts["clarify_wait_thread"] == "{name} ждет ваших ответов на вопросы по цели (#2) — ответьте в чате"
    assert texts["sf_mode_line"] == "Режим работы: {mode} — выбирается при создании сессии"
    assert texts["goal_rewrite_note"] == "Посты #1 и #2 будут переписаны, голоса за цель сброшены"
    assert texts["f_mode_thread_hint"].startswith("без заданной структуры: агенты публикуют объявления")
    assert texts["goal_votes"] == "Голоса за цель: {for} из {counted} (нужно {needed})"
    # the keys the new code passes to t() are its own (the tables' parity and coverage are test_ui's)
    in_index = set(re.findall(r'data-i18n(?:-placeholder)?="([a-z0-9_]+)"', INDEX))
    assert {"th_comment_btn", "th_write_comment", "tab_thread"} <= used_keys(APP_JS) | in_index
    assert {"f_mode", "thread_badge", "goal_votes", "posts_n", "sw_rule_veto"} <= used_keys(HOME_JS)


def test_the_board_page_has_the_thread_tab_and_its_composer():
    """index.html: the thread tab first and hidden (a board session never shows it), its pane with the feed, the open
    post and the comment composer (static, so a live re-render never touches the draft), the drawer's subscriptions."""
    tab = ('<button type="button" role="tab" data-tab="thread" class="hidden">'
           '<span data-i18n="tab_thread"></span></button>')
    assert tab in INDEX and INDEX.index('data-tab="thread"') < INDEX.index('data-tab="board"')
    assert '<section class="tab-pane thread-pane hidden" id="pane-thread">' in INDEX
    for ident in ("thread-feed", "thread-post", "tpost-view", "tpost-composer", "tcomm-reply", "tcomm-text",
                  "tcomm-menu", "tcomm-btn", "drawer-subs"):
        assert f'id="{ident}"' in INDEX, ident
    assert INDEX.index('id="drawer-jobs"') < INDEX.index('id="drawer-subs"') < INDEX.index('class="drawer-tabs"')
    # the board keeps its tab and pane, active at first
    assert '<button type="button" role="tab" data-tab="board" class="active">' in INDEX
    assert '<section class="tab-pane board-pane" id="pane-board">' in INDEX
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for rule in (".thread-layout", ".tfeed-row", ".tfeed-new", ".vote-btn.like.on", ".goal-bar-mark", ".goal-decision",
                 ".drawer-subs", ".mode-line", ".goal-note"):
        assert rule in css, rule
    home_css = (STATIC / "home.css").read_text(encoding="utf-8")
    assert ".ns-ws[hidden] { display: none; }" in home_css and ".sc-vote > i" in home_css
    assert "ns-mode" not in HOME_HTML   # the modal is built by home.js


# ------------------------------------------------------------------------------------------------ app.js in Node
@needs_node
def test_the_page_is_laid_out_for_the_sessions_mode():
    """A thread session: the thread tab (the default) in place of the board, «События» for the feed, the thread's search
    placeholder, «+ Объявление»; back on a board session, everything as before."""
    out = run_js(QUESTION_HARNESS + r"""
      app.S.lang = "en";
      const tab = (name) => el(`tabs > button[data-tab="${name}"]`,
                               { classList: classList(name === "thread" ? ["hidden"] : []) });
      const tabs = { thread: tab("thread"), board: tab("board") };
      const label = el('tabs > button[data-tab="feed"] > span[data-i18n]', { dataset: { i18n: "tab_feed" } });
      const search = el("search", { dataset: { i18nPlaceholder: "search" } });
      const add = el("new-task-btn", { dataset: { i18n: "new_task" } });
      for (const id of ["thread-feed", "tpost-view"]) el(id);
      for (const id of ["tpost-composer", "tcomm-reply"]) el(id, { classList: classList(["hidden"]) });
      const look = () => [tabs.thread.classList.contains("hidden"), tabs.board.classList.contains("hidden"),
                          label.textContent, label.dataset.i18n, search.placeholder, add.textContent, app.S.tab];
      const state = (sw) => ({ swarm: swarm(sw), agents, mcp: [], triggers: [], jobs: [], posts: [] });
      app.S.state = state({});
      app.renderAll();
      out.board = look();
      app.S.state = state({ mode: "thread" });
      app.renderAll();
      out.thread = look();
      out.empty = elements["thread-feed"].innerHTML;
      app.S.state = state({ mode: "board" });
      app.renderAll();
      out.back = look();
    """)
    assert out["board"] == [True, False, "Feed", "tab_feed", "Search tasks…", "+ Task", "board"]
    assert out["thread"] == [False, True, "Events", "tab_events", "Search the thread…", "+ Post", "thread"]
    assert "No posts yet" in out["empty"]
    assert out["back"] == out["board"]


THREAD_STATE = r"""
  const E = process.env.EVIL;
  const post = (id, extra) => Object.assign({ id, kind: "post", title: `Post ${id}`, author: "Anna Smith",
                                              created_at: 1, edited_at: null, active_at: id, likes: 0, dislikes: 0,
                                              comments: 0, my_vote: null }, extra);
  const posts = [post(1, { kind: "goal", title: "Build the parser", author: null, active_at: 5, likes: 1 }),
                 post(2, { kind: "plan", title: "How we work", author: null, active_at: 5 }),
                 post(4, { title: E, author: "Boris Lee", active_at: 30, likes: 2, dislikes: 1, comments: 3 }),
                 post(3, { title: "Use a PEG", author: "user", active_at: 20 })];
  const thread = (extra) => ({
    swarm: Object.assign({ mode: "thread", name: "Solo run", goal_rule: "democracy",
                           goal: { title: "Build the parser", status: "active" } }, extra),
    posts, agents: [{ name: "Anna Smith", kind: "llm", status: "idle" },
                    { name: "Boris Lee", kind: "llm", status: "idle" },
                    { name: "Carl Ext", kind: "external", status: "idle" }],
    goal_vote: { rule: "democracy", counted: 2, for: ["Anna Smith"], against: [], needed: 2, met: false } });
"""
STORAGE = r"""
  globalThis.localStorage = { data: {}, getItem(k) { return k in this.data ? this.data[k] : null; },
                              setItem(k, v) { this.data[k] = String(v); } };
"""


@needs_node
def test_the_feed_of_posts_search_and_news_dots():
    out = run_js(THREAD_STATE + r"""
      app.S.lang = "en";
      el("thread-feed");
      app.S.state = thread({});
      const html = () => elements["thread-feed"].innerHTML;
      const ids = () => (html().match(/data-open-post="(\d+)"/g) || []).map((m) => Number(m.match(/\d+/)[0]));
      app.renderThreadFeed();
      out.all = html();
      out.searched = ["boris", "#3", "peg", "build", "zzz"].map((q) => { app.S.search = q; app.renderThreadFeed();
                                                                       return ids(); });
      out.none = html();
      app.S.search = "";
      // seen: no dot until a post becomes active again; the open post has none
      out.key = app.seenKey();
      app.markSeen(posts[2]); app.markSeen(posts[0]);
      app.renderThreadFeed();
      out.dots = (html().match(/tfeed-new/g) || []).length;
      posts[2].active_at = 40;
      app.S.openPostId = 3;
      app.renderThreadFeed();
      out.later = html();
      out.stored = JSON.parse(localStorage.getItem("dt-seen:Solo run"));
    """, pre=STORAGE, env={"EVIL": EVIL})
    html = out["all"]
    order = [html.index(f'data-open-post="{n}"') for n in (1, 2, 4, 3)]
    assert order == sorted(order)   # the snapshot's order: #1, #2, then by activity
    assert html.count("tfeed-new") == 4   # never opened: every post has news
    assert 'class="tfeed-row system" data-open-post="1"' in html and '<span class="badge">goal</span>' in html
    assert '<span class="badge plain">plan</span>' in html and "user (you)" in html
    assert '<span class="tfeed-count" title="likes">▲ 2</span>' in html
    assert '<span class="tfeed-count" title="dislikes">▼ 1</span>' in html
    assert '<i class="ic ic-chat"></i>3</span>' in html
    assert_escaped(html)
    assert out["searched"] == [[4], [3], [3], [1], []]
    assert "Nothing found" in out["none"]
    assert out["key"] == "dt-seen:Solo run"   # a standalone run: the swarm's name
    assert out["dots"] == 2
    later = out["later"]
    assert later.count("tfeed-new") == 2   # #4 again (active since), #2; the open #3 has none
    assert "tfeed-new" in between(later, 'data-open-post="4"', "</button>")
    assert 'class="tfeed-row active" data-open-post="3" data-tf="row-3" aria-current="true"' in later
    assert out["stored"] == {"1": 5, "4": 30}


@needs_node
def test_the_open_post_its_comments_votes_and_the_goal_card():
    out = run_js(THREAD_STATE + r"""
      app.S.lang = "en";
      app.S.state = thread({});
      const votes = [{ voter: "Anna Smith", value: "like", counted: true },
                     { voter: "user", value: "like", counted: false },
                     { voter: "Carl Ext", value: "dislike", counted: false }];
      const text = "Reach it: see #3 and c7, @Anna Smith.";
      const goal = { post: Object.assign({}, posts[0], { text, my_vote: "like" }), comments: [], votes };
      out.goal = app.postViewHtml(goal);
      const decision = { rule: "veto", for: ["Anna Smith"], against: [], counted: 2, wrap_up: false };
      app.S.state.swarm.goal = { title: "Build the parser", status: "done", summary: "All **good**", decision };
      out.done = app.postViewHtml(goal);
      app.S.state.swarm.goal.decision = { rule: "user", for: [], against: [], counted: 2, wrap_up: false };
      out.user = app.postViewHtml(goal);
      app.S.state = thread({});
      const comment = (id, author, text, reply_to, extra) => Object.assign(
        { id, author, text, reply_to, ts: id, likes: 0, dislikes: 0, my_vote: null }, extra);
      const mine = { post: Object.assign({}, posts[3], { text: "", likes: 1, edited_at: 50 }),
                     comments: [comment(5, "Anna Smith", "Agreed, @user", null, { dislikes: 2, my_vote: "dislike" }),
                                comment(7, "user", "Thanks", 5, { likes: 1 })],
                     votes: [{ voter: "Anna Smith", value: "like", counted: false }] };
      out.mine = app.postViewHtml(mine);
      out.plan = app.postViewHtml({ post: posts[1], comments: [], votes: [] });
      out.evil = app.postViewHtml({ post: post(9, { title: E, text: E, author: E }),
                                    comments: [comment(11, E, E, null)],
                                    votes: [{ voter: E, value: "like", counted: false }] });
      app.S.lang = "ru";
      app.S.state.swarm.goal = { title: "G", status: "done", summary: "", decision: { rule: "democracy" } };
      out.ru = app.postViewHtml(goal);
    """, env={"EVIL": EVIL})
    goal = out["goal"]
    assert '<article class="tpost" id="tpost-1">' in goal and "Goal decision: democracy" in goal
    assert "for: 1 of 2, needs 2" in goal and 'style="width:50.0%"' in goal and 'style="left:100.0%"' in goal
    counted = between(goal, "Votes of the AI agents:", "</div>")
    assert counted.index("Anna Smith") < counted.index("Boris Lee") and "Carl Ext" not in counted
    assert '<span class="chip vote-chip like" title="Like">' in counted and 'title="no vote"' in counted
    apart = between(goal, "Shown, not counted:", "</div>")
    assert "user (you)" in apart and "Carl Ext" in apart and '<span class="chip vote-chip dislike"' in apart
    assert 'data-goal-act="edit"' in goal and 'data-goal-act="complete"' in goal
    assert 'data-goal-act="reopen"' not in goal
    assert "data-post-edit" not in goal   # #1 and #2 are the swarm's posts
    assert 'class="vote-btn like on" data-vote="p1" data-vote-value="none"' in goal and " disabled" not in goal
    # the text's #N and cN are thread links, mentions highlighted (md() takes the mode)
    assert 'data-open-post="3">#3</button>' in goal and 'data-open-comment="7">c7</button>' in goal
    assert '<span class="mention">@Anna Smith</span>' in goal and "data-open-task" not in goal
    done = out["done"]
    assert "Goal reached by vote (veto)" in done and "<strong>good</strong>" in done
    assert 'data-goal-act="reopen"' in done and 'data-goal-act="complete"' not in done
    assert done.count(" disabled") == 2 and "#1 is closed for votes" in done   # the vote on a done goal
    assert "Goal reached by the user&#39;s decision" in out["user"]
    mine = out["mine"]
    assert mine.count("You cannot vote on your own post or comment") == 4   # its post's and its comment's buttons
    assert 'data-post-edit="3"' in mine and "edited " in mine and "Comments (2)" in mine
    assert '<div class="post tcomm" id="tcomm-5" style="margin-left:0px">' in mine
    assert '<div class="post tcomm reply" id="tcomm-7" style="margin-left:22px">' in mine
    assert 'data-open-comment="5" data-tf="to-7"' in mine and 'data-reply-comment="5"' in mine
    assert 'class="vote-btn dislike on" data-vote="c5" data-vote-value="none"' in mine
    assert 'data-vote="c5" data-vote-value="like"' in mine and '<span class="mention">@user</span>' in mine
    assert "Votes:</span>" in mine and "goal-card" not in mine
    assert "data-post-edit" not in out["plan"] and '<span class="badge plain">plan</span>' in out["plan"]
    assert_escaped(out["evil"], at_least=4)
    assert "Цель достигнута голосованием (демократия)" in out["ru"] and "Вернуть цель в работу" in out["ru"]


ACT_PRE = r"""
  globalThis.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url, body });
    let data = { ok: true, result: "done" };
    const m = url.match(/^\/api\/posts\/(\d+)(\?since_id=(c\d+))?$/);
    if (m) {
      const id = Number(m[1]);
      const all = [{ id: 5, author: "Anna Smith", text: "First", reply_to: null, ts: 1, likes: 0, dislikes: 0 },
                   { id: 9, author: "Boris Lee", text: "Later", reply_to: 5, ts: 2, likes: 0, dislikes: 0 }];
      const post = { id, kind: "post", title: `Post ${id}`, text: "Body", author: "Anna Smith", created_at: 1,
                     edited_at: null, active_at: 7, likes: 0, dislikes: 0, comments: 2, my_vote: null };
      data = { post, comments: m[3] ? all.filter((c) => c.id > Number(m[3].slice(1))) : all.slice(0, 1), votes: [] };
    }
    if (url.endsWith("/api/act/post_create")) data = { ok: true, result: 'Posted #12 "Hi".' };
    return { ok: true, json: async () => data, text: async () => JSON.stringify(data) };
  };
"""
ACT_HARNESS = r"""
  const handlers = {};
  const on = (id) => el(id, { addEventListener: (type, fn) => { handlers[id + ":" + type] = fn; } });
  el("modal"); el("modal-backdrop", { classList: classList(["hidden"]) });
  el("toasts", { children: [], appendChild: () => {} });
  document.createElement = () => ({ className: "", textContent: "", remove() {}, addEventListener() {} });
  for (const id of ["thread-feed", "tpost-view", "tp-menu", "gc-summary", "gr-comment", "tp-title"]) {
    el(id, { focus() {} });
  }
  for (const id of ["tpost-composer", "tcomm-reply"]) el(id, { classList: classList(["hidden"]) });
  el("tcomm-text", { focus() {} }); el("tcomm-btn");
  for (const id of ["tp-save", "tp-text", "gc-go", "gr-go"]) on(id);
  const tick = () => new Promise((r) => setImmediate(r));
  const acts = () => calls.filter((c) => c.url.includes("/api/act/")).map((c) => [c.url.split("/").pop(), c.body]);
  const gets = () => calls.filter((c) => c.url.startsWith("/api/posts/")).map((c) => c.url);
"""


@needs_node
def test_the_users_forum_actions_call_the_api():
    out = run_js(THREAD_STATE + ACT_HARNESS + r"""
      app.S.lang = "en";
      app.S.state = thread({});
      app.S.tab = "thread";
      await app.openPost(3, { quiet: true });
      out.opened = [app.S.openPostId, gets(), elements["tpost-view"].innerHTML.includes('id="tpost-3"'),
                    elements["tpost-composer"].classList.contains("hidden")];
      // a reply to c5: comment with reply_to, then only the later comments are loaded and merged
      calls.length = 0;
      app.setThreadReply(5);
      out.replying = elements["tcomm-reply"].innerHTML;
      elements["tcomm-text"].value = "Me too";
      await app.sendThreadComment(); await tick(); await tick();
      out.comment = [acts(), gets(), elements["tcomm-text"].value, app.S.threadReply,
                     app.S.postData.comments.map((c) => c.id)];
      // votes: a like, a pressed one withdrawn through the page's click handler; a malformed target sends nothing
      calls.length = 0;
      await app.castVote("c5", "like"); await tick();
      const pressed = { dataset: { vote: "p3", voteValue: "none" } };
      app.onDocumentClick({ target: { closest: () => pressed } }); await tick(); await tick();
      await app.castVote("5", "like");
      out.votes = [acts(), gets()];
      // a new post: post_create, then the new post opens
      calls.length = 0;
      app.postComposer();
      elements["tp-title"].value = "";
      await handlers["tp-save:click"]();
      out.untitled = acts().length;
      elements["tp-title"].value = "Hi"; elements["tp-text"].value = "  ";
      await handlers["tp-save:click"](); await tick(); await tick();
      out.created = [acts(), gets(), app.S.openPostId];
      // the edit of an ordinary post sends only what changed; nothing changed: nothing sent; never a system post
      calls.length = 0;
      const peg = { id: 3, kind: "post", title: "Use a PEG", text: "Why not?" };
      app.postComposer(peg);
      out.editTitle = elements.modal.innerHTML.includes("Edit post #3");
      elements["tp-title"].value = "Use a PEG parser"; elements["tp-text"].value = "Why not?";
      await handlers["tp-save:click"](); await tick();
      app.postComposer(peg);
      elements["tp-title"].value = "Use a PEG"; elements["tp-text"].value = "Why not?\n";
      await handlers["tp-save:click"]();
      elements.modal.innerHTML = "";
      app.postComposer({ id: 1, kind: "goal", title: "G", text: "" });
      out.edits = [acts(), elements.modal.innerHTML];
      const was = { id: 4, title: "A", text: "B" };
      out.args = [app.postUpdateArgs(was, " A ", "C"), app.postUpdateArgs(was, "A", "B")];
      // #1's buttons: mark the goal reached (with or without a summary), reopen it (an empty comment is "-")
      calls.length = 0;
      app.goalAction("complete");
      out.completeAsk = elements.modal.innerHTML;
      elements["gc-summary"].value = "Done well"; await handlers["gc-go:click"]();
      app.completeGoalModal(); elements["gc-summary"].value = " "; await handlers["gc-go:click"]();
      app.goalAction("reopen");
      elements["gr-comment"].value = ""; await handlers["gr-go:click"]();
      app.reopenGoalModal(); elements["gr-comment"].value = "More tests"; await handlers["gr-go:click"]();
      out.goal = acts();
    """, pre=ACT_PRE)
    assert out["opened"] == [3, ["/api/posts/3"], True, False]
    assert "Replying to" in out["replying"] and "Anna Smith" in out["replying"] and "c5" in out["replying"]
    acts, gets, left, reply, ids = out["comment"]
    assert acts == [["comment", {"post_id": 3, "text": "Me too", "reply_to": "c5"}]]
    assert gets == ["/api/posts/3?since_id=c5"] and left == "" and reply is None and ids == [5, 9]
    acts, gets = out["votes"]
    assert acts == [["vote", {"target": "c5", "value": "like"}], ["vote", {"target": "p3", "value": "none"}]]
    assert gets == ["/api/posts/3", "/api/posts/3"]
    assert out["untitled"] == 0
    acts, gets, opened = out["created"]
    assert acts == [["post_create", {"title": "Hi"}]] and gets == ["/api/posts/12"] and opened == 12
    acts, html = out["edits"]
    assert out["editTitle"] is True
    assert acts == [["post_update", {"post_id": 3, "title": "Use a PEG parser"}]] and html == ""
    assert out["args"] == [{"post_id": 4, "text": "C"}, None]
    ask = out["completeAsk"]
    assert "Mark the goal reached? The vote on #1 closes" in ask and "Summary, optional" in ask
    assert out["goal"] == [["swarm_control", {"complete_goal": True, "goal_summary": "Done well"}],
                           ["swarm_control", {"complete_goal": True}],
                           ["swarm_control", {"reopen_goal_comment": "-"}],
                           ["swarm_control", {"reopen_goal_comment": "More tests"}]]


@needs_node
def test_live_forum_events_update_the_open_post():
    """WS post.* events reload the open post (only its later comments); a vote on one of its comments carries the
    counts; a comment's post is remembered for its cN links; «События» keeps only post.created of the forum's events."""
    out = run_js(THREAD_STATE + ACT_HARNESS + r"""
      app.S.lang = "en";
      app.S.state = thread({});
      app.S.tab = "thread";
      app.S.openPostId = 3;
      const first = { id: 5, author: "Anna Smith", text: "First", reply_to: null, ts: 1, likes: 0, dislikes: 0,
                      my_vote: null };
      app.S.postData = { post: Object.assign({}, posts[3], { text: "" }), comments: [first], votes: [] };
      const vote = { target: "c5", voter: "user", value: "like", likes: 3, dislikes: 1 };
      app.onPostEvent({ type: "post.vote", data: vote });
      const c = app.S.postData.comments[0];
      const view = elements["tpost-view"].innerHTML;
      out.counted = [c.likes, c.dislikes, c.my_vote, view.includes('data-vote="c5" data-vote-value="none"')];
      app.onPostEvent({ type: "post.comment", data: { post_id: 4, comment: { id: 8, post_id: 4, text: "x" } } });
      out.known = app.S.commentPost.get(8);
      calls.length = 0;
      await app.loadPost();
      out.since = [gets(), app.S.postData.comments.map((x) => x.id)];
      // a stale answer (another post opened meanwhile) is dropped
      let release;
      const held = new Promise((r) => { release = r; });
      const fetch0 = globalThis.fetch;
      globalThis.fetch = async (url, opts) => { await held; return fetch0(url, opts); };
      const loading = app.loadPost(true);
      app.S.openPostId = 4; app.S.postData = null;
      release(); await loading;
      out.stale = app.S.postData;
      globalThis.fetch = fetch0;
      // the comment's post is opened for a cN link
      calls.length = 0;
      await app.openComment(8);
      out.comment = [app.S.openPostId, gets()];
      // the Events tab: post.created is listed, the thread tab's events are not
      app.S.feed = []; app.S.feedMaxId = 0;
      const data = { post: { id: 20, title: "T" }, comment: { id: 30 }, post_id: 20 };
      const types = ["post.comment", "post.vote", "post.updated", "subscription.changed", "files.committed",
                     "post.created"];
      types.forEach((type, i) => app.handleEvent({ id: i + 1, ts: 0, type, actor: "Anna Smith", data }));
      out.feed = app.S.feed.map((e) => e.type);
      out.types = [app.feedTypes("thread").includes("post.created"), app.feedTypes("board").includes("post.created")];
    """, pre=ACT_PRE)
    assert out["counted"] == [3, 1, "like", True]
    assert out["known"] == 4
    assert out["since"] == [["/api/posts/3?since_id=c5"], [5, 9]]
    assert out["stale"] is None
    assert out["comment"] == [4, ["/api/posts/4"]]
    assert out["feed"] == ["post.created"]
    assert out["types"] == [True, False]


@needs_node
def test_forum_events_and_a_decided_goal_in_the_events_tab():
    out = run_js(r"""
      const ev = (type, data, actor = "Anna Smith") => app.describeEvent({ id: 1, ts: 0, type, actor, data });
      const sub = (id, kind, target, by, mode, auto) => ({ id, kind, target, by, mode, auto });
      const files = [{ path: "src/a.py", change: "M" }, { path: "b", change: "A" }];
      const lines = () => [
        ev("post.created", { post: { id: 9, title: "Lexer first", author: "Anna Smith" } }),
        ev("post.created", { post: { id: 1, title: "Build", author: null } }, null),
        ev("post.updated", { post: { id: 9 }, changes: ["title", "text"] }),
        ev("post.updated", { post: { id: 1 }, changes: ["text"] }, null),
        ev("post.updated", { post: { id: 1 }, changes: ["votes"] }, "user"),
        ev("post.comment", { post_id: 9, comment: { id: 31, author: "Boris Lee", text: "Agreed" } }, "Boris Lee"),
        ev("post.vote", { target: "p9", voter: "Anna Smith", value: "like", likes: 1, dislikes: 0 }),
        ev("post.vote", { target: "c31", voter: "user", value: "dislike" }, "user"),
        ev("post.vote", { target: "p9", voter: "Anna Smith", value: "none" }),
        ev("subscription.changed", { member: "Anna Smith", op: "created",
                                     subscription: sub(3, "post", "9", "Boris Lee", "wake", false) }),
        ev("subscription.changed", { member: "Anna Smith", op: "changed",
                                     subscription: sub(1, "feed", "", "", "digest", true) }),
        ev("subscription.changed", { member: "Anna Smith", op: "removed", subscription: sub(12, "file", "src") }),
        ev("subscription.changed", { member: "Anna Smith", op: "removed", count: 4 }, null),
        ev("files.committed", { kind: "job j3", commit: "abc", files, more: 1, suspects: [] }),
        ev("files.committed", { kind: "shell", commit: "abc", files: [], suspects: [] }),
        ev("goal.completed", { goal: { summary: "Parser done", decision: { rule: "democracy" } } }),
        ev("goal.completed", { goal: { summary: "", decision: { rule: "user" } } }, "user"),
        ev("goal.completed", { goal: { summary: "Board done" } }),
      ];
      app.S.lang = "en"; out.en = lines();
      app.S.lang = "ru"; out.ru = lines();
      app.S.lang = "en";
      app.S.state = { swarm: { mode: "thread" }, agents: [] };
      const created = { post: { id: 9, title: "Lexer", author: "Anna Smith" } };
      out.line = app.feedLine({ id: 1, ts: 0, type: "post.created", actor: "Anna Smith", data: created });
    """)
    assert out["en"] == [
        'Anna Smith posted #9 "Lexer first"', 'system post #1 "Build"', "Anna Smith edited #9",
        "#1 rewritten for the goal", "#1: votes reset", 'Boris Lee on #9 (c31): "Agreed"', "Anna Smith likes #9",
        "user (you) dislikes c31", "Anna Smith withdrew the vote on #9",
        "Anna Smith follows #9 (wake, only Boris Lee)", "Anna Smith follows the feed: changed to digest",
        "Anna Smith unfollows s12", "Anna Smith: subscriptions removed: 4",
        "Anna Smith: commit (job j3), files: 3: src/a.py, …", "Anna Smith: commit (shell), files: 0",
        "goal reached by vote (democracy) — Parser done", "goal completed by the user",
        "Anna Smith: goal achieved — Board done"]
    ru = out["ru"]
    assert ru[0] == "Anna Smith опубликовал(а) #9 «Lexer first»" and ru[4] == "#1: голоса сброшены"
    assert ru[15] == "цель достигнута голосованием (демократия) — Parser done"
    assert ru[16] == "цель завершена пользователем"
    assert "Anna Smith" not in out["en"][15]   # a decided goal never names the actor
    assert 'data-open-post="9">#9</button>' in out["line"] and "data-open-task" not in out["line"]


@needs_node
def test_the_header_goal_modal_and_drawer_of_a_thread_session():
    out = run_js(QUESTION_HARNESS + ACT_HARNESS + r"""
      app.S.lang = "en";
      app.S.userInbox = [];
      el("drawer-subs", { classList: classList(["hidden"]) });
      for (const id of ["g-title", "g-desc", "g-reopen"]) el(id, { focus() {} });
      for (const id of ["g-save", "g-complete", "g-reopen-btn"]) on(id);
      const gv = { rule: "democracy", counted: 3, for: ["Anna Smith"], against: [], needed: 2, met: false };
      const subs = [{ id: 1, kind: "feed", target: "", by: "", mode: "digest", auto: true },
                    { id: 4, kind: "post", target: "7", by: "Boris Lee", mode: "wake", auto: false },
                    { id: 5, kind: "file", target: "", by: "", mode: "wake", auto: false }];
      const members = agents.map((a) => Object.assign({}, a, { subscriptions: a.name === "Anna Smith" ? subs : [] }));
      const state = (extra, sw) => Object.assign({
        swarm: swarm(Object.assign({ goal: { title: "G", status: "active" } }, sw)),
        agents: members, mcp: [], triggers: [], jobs: [], posts: [], goal_vote: gv }, extra);
      app.S.state = state({}, { mode: "thread", clarify_wait: { name: "Anna Smith", post_id: 2 } });
      app.renderHeader();
      out.status = elements["goal-status"].textContent;
      out.banner = elements["paused-banner"].textContent;
      app.S.chatWith = "Anna Smith"; app.renderDrawerHead();
      out.subs = [elements["drawer-subs"].innerHTML, elements["drawer-subs"].classList.contains("hidden")];
      // the goal button: #1 in the thread tab when it exists, else the goal dialog
      app.S.state.posts = [{ id: 1, kind: "goal", title: "G", author: null, active_at: 1 }];
      calls.length = 0;
      app.onGoalButton(); await tick();
      out.goalButton = [app.S.openPostId, app.S.tab, gets()];
      // the goal dialog with a goal: the rewrite note, the save asks first; «Цель достигнута» while it is active
      app.goalModal();
      out.modal = elements.modal.innerHTML;
      calls.length = 0;
      elements["g-title"].value = "New goal";
      answers.confirm = false; await handlers["g-save:click"]();
      out.refused = acts().length;
      answers.confirm = true; await handlers["g-save:click"]();
      out.saved = acts();
      handlers["g-complete:click"]();
      out.complete = elements.modal.innerHTML.includes('id="gc-go"');
      // a board session: no count, the board's banner, no subscriptions, the goal dialog as before
      app.S.state = state({ goal_vote: null }, { clarify_wait: { name: "Anna Smith", task_id: 1 } });
      app.renderHeader();
      out.boardStatus = elements["goal-status"].textContent;
      out.boardBanner = elements["paused-banner"].textContent;
      app.renderDrawerHead();
      out.boardSubs = elements["drawer-subs"].classList.contains("hidden");
      app.closeModal(true);
      app.goalModal();
      out.boardModal = elements.modal.innerHTML;
      app.S.state.swarm.goal = null;
      app.S.state.swarm.mode = "thread";
      app.goalModal();
      out.noGoal = elements.modal.innerHTML;
    """, pre=ACT_PRE)
    assert out["status"] == "active · 1 of 3 for, needs 2"
    assert out["banner"] == "Anna Smith waits for your answers about the goal (#2): reply in the chat"
    html, hidden = out["subs"]
    assert hidden is False and "Subscriptions" in html
    assert ('<span class="sub-id">s1</span><span class="badge plain">feed</span><span class="badge plain">digest</span>'
            '<span class="muted">auto</span>') in html
    assert 'data-open-post="7">#7</button><span>only Boris Lee</span><span class="badge">wake</span>' in html
    assert "whole workspace" in html
    assert out["goalButton"] == [1, "thread", ["/api/posts/1"]]
    modal = out["modal"]
    assert 'id="g-rewrite-note">Posts #1 and #2 will be rewritten and the goal votes cleared</p>' in modal
    assert modal.index('id="g-title"') < modal.index("g-rewrite-note") < modal.index('id="g-desc"')
    assert 'id="g-complete"' in modal and "Mark goal reached" in modal
    assert out["refused"] == 0 and out["saved"] == [["set_goal", {"title": "New goal", "description": ""}]]
    assert out["complete"] is True
    assert out["boardStatus"] == "active"
    assert out["boardBanner"] == "Anna Smith waits for your answers about the goal: reply in the chat"
    assert out["boardSubs"] is True
    assert "g-rewrite-note" not in out["boardModal"] and "g-complete" not in out["boardModal"]
    assert "g-rewrite-note" not in out["noGoal"] and "g-complete" not in out["noGoal"]


SETTINGS_PRE = r"""
  globalThis.location = { pathname: "/s/007-forum/" };
  const routes = {
    "/api/sessions/007-forum": {
      session: { id: "007-forum", overrides: { swarm: { mode: "thread", goal_rule: "democracy" },
                                               agents: { digest_seconds: 30 } } },
      config: { swarm: { mode: "thread" }, agents: {}, models: {} } },
    "/api/settings": { settings: { swarm: { goal_rule: "veto" },
                                   agents: { digest_seconds: 60, context_tokens: 120000 } } },
  };
  globalThis.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url, method: (opts && opts.method) || "GET", body });
    let data = routes[url] || { ok: true, result: "Limits set" };
    if (opts && opts.method === "PATCH") data = { session: routes["/api/sessions/007-forum"].session, config: {} };
    return { ok: true, json: async () => data, text: async () => "" };
  };
"""


@needs_node
def test_the_settings_dialog_by_mode():
    """⚙ (docs/SPEC.md §22.8): the read-only mode line for every session; a thread session's «Тред» group (the goal
    rule: no inherit option, starts at the session's value, sent only when changed, never cleared; the digest age ≥ 10)
    and its hints; a board session's dialog reads only what it rendered and sends the same patch as before."""
    out = run_js(SETTINGS_HARNESS + r"""
      const F = app.SESSION_FIELDS;
      const rule = F.find((f) => f.key === "goal_rule"), digest = F.find((f) => f.key === "digest_seconds");
      out.filtered = [app.sessionFields("board").some((f) => f.modes), app.sessionFields("thread").length === F.length];
      out.parsed = [app.parseSessionField(rule, "veto"), app.parseSessionField(rule, "anarchy"),
                    app.parseSessionField(digest, "5"), app.parseSessionField(digest, "10")];
      const key = (f) => `${f.section}.${f.key}`;
      const texts = (o) => Object.fromEntries(F.map((f) => [key(f), o[key(f)] ?? ""]));
      const own = { swarm: { mode: "thread", goal_rule: "democracy" } };
      const patch = (rule, overrides, fields, defaults) =>
        app.sessionSettingsPatch(texts({ "swarm.goal_rule": rule }), overrides, fields, defaults).patch;
      const board = app.sessionFields("board");
      out.patches = [patch("", own), patch("democracy", own), patch("veto", own),
                     patch("veto", {}, F, { swarm: { goal_rule: "veto" } }), patch("veto", {}, board)];
      const defaults = { swarm: { goal_rule: "veto", clarify: false }, agents: { digest_seconds: 60 } };
      out.boardHtml = app.sessionFieldsHtml({}, defaults, {}, F, null);
      out.boardSame = out.boardHtml === app.sessionFieldsHtml({}, defaults, {}, board, null, "board");
      // a thread session's dialog, a PATCH of the rule and a refused digest
      app.S.state.swarm.mode = "thread";
      await app.settingsModal();
      out.html = elements.modal.innerHTML;
      Object.assign(elements["s-cost"], { value: "2" }); Object.assign(elements["s-agents"], { value: "3" });
      elements["sf-swarm-goal_rule"].value = "democracy"; elements["sf-agents-digest_seconds"].value = "5";
      calls.length = 0;
      await handlers["s-save:click"]();
      out.refused = [calls.length, out.focused];
      elements["sf-agents-digest_seconds"].value = "30"; elements["sf-swarm-goal_rule"].value = "veto";
      await handlers["s-save:click"]();
      out.saved = calls.map((c) => [c.method, c.url, c.body]);
      // a board session: the thread inputs are neither read nor sent, even holding values
      delete app.S.state.swarm.mode;
      routes["/api/sessions/007-forum"].session.overrides = { shell: { max_jobs: 3 } };
      await app.settingsModal();
      out.boardDialog = elements.modal.innerHTML;
      elements["sf-swarm-goal_rule"].value = "veto"; elements["sf-agents-digest_seconds"].value = "5";
      elements["sf-shell-max_jobs"].value = "4";
      calls.length = 0;
      await handlers["s-save:click"]();
      out.boardSaved = calls.map((c) => [c.method, c.url, c.body]);
      app.S.lang = "ru";
      out.ruLine = app.modeLineHtml("thread");
    """, pre=SETTINGS_PRE)
    assert out["filtered"] == [False, True]
    assert out["parsed"][0] == {"value": "veto"}
    assert out["parsed"][1] == {"error": "“Goal decision”: pick a value from the list"}
    assert out["parsed"][2] == {"error": "“Subscription digest, s”: a number ≥ 10 is needed"}
    assert out["parsed"][3] == {"value": 10}
    assert out["patches"] == [{}, {}, {"swarm": {"goal_rule": "veto"}}, {}, {}]
    assert out["boardSame"] is True
    board_html = out["boardHtml"]
    assert "Thread" not in board_html and "goal_rule" not in board_html and "digest" not in board_html
    html = out["html"]
    assert '<p class="mode-line muted">Work mode: thread, chosen when the session is created</p>' in html
    assert (html.index("mode-line") < html.index('id="s-cost"') < html.index("Clarifying the goal")
            < html.index(">Thread<"))
    assert between(html, 'id="sf-swarm-goal_rule">', "</select>") == (
        '<option value="democracy" selected>democracy: more than half of the AI agents for it</option>'
        '<option value="veto">veto: all AI agents for it, none against</option>')
    assert 'id="sf-agents-digest_seconds" inputmode="decimal" autocomplete="off" value="30" placeholder="60"' in html
    assert "An idle agent gets what its subscriptions gathered" in html
    assert html.count("Only while the goal is clarified") == 2 and "planning task" not in html
    assert "the discussion #2 starts with questions to you" in html
    assert 'id="sf-swarm-mode"' not in html
    assert out["refused"] == [0, "sf-agents-digest_seconds"]
    assert out["saved"] == [["PATCH", "/api/sessions/007-forum", {"settings": {"swarm": {"goal_rule": "veto"}}}]]
    board = out["boardDialog"]
    assert '<p class="mode-line muted">Work mode: task board, chosen when the session is created</p>' in board
    assert "sf-swarm-goal_rule" not in board and "sf-agents-digest_seconds" not in board and ">Thread<" not in board
    assert "to the member holding the planning task" in board
    assert out["boardSaved"] == [["PATCH", "/api/sessions/007-forum", {"settings": {"shell": {"max_jobs": 4}}}]]
    assert out["ruLine"] == '<p class="mode-line muted">Режим работы: тред — выбирается при создании сессии</p>'


# ------------------------------------------------------------------------------------------------ home.js in Node
@needs_node
def test_home_work_mode_in_the_new_session_modal_and_the_overrides():
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      const base = { lang: "en", max: "8", budget: "5", team: home.teamRows([], 3), model: "default", steps: "40",
                     approval: false };
      const saved = (mode, rule) => ({
        swarm: { language: "en", start_agents: 3, max_agents: 8, max_cost_usd: 5, clarify: false, mode,
                 goal_rule: rule },
        agents: { default_model: "default", max_steps_per_turn: 40 }, mcp: {}, models: {} });
      const ov = (mode, rule, form) => {
        const sw = home.sessionOverrides(saved(mode, rule), Object.assign({}, base, form)).swarm;
        return [sw.mode ?? null, sw.goal_rule ?? null];
      };
      out.ov = [ov("board", "democracy", { mode: "board", goalRule: "veto" }),
                ov("board", "democracy", { mode: "thread", goalRule: "democracy" }),
                ov("board", "democracy", { mode: "thread", goalRule: "veto" }),
                ov("thread", "veto", { mode: "thread", goalRule: "veto" }),
                ov("thread", "veto", { mode: "board", goalRule: "democracy" }),
                ov(undefined, undefined, { mode: "board", goalRule: "democracy" }),
                ov("thread", "veto", {})];
      el("modal"); el("modal-backdrop");
      for (const [mode, rule] of [["board", "democracy"], ["thread", "veto"]]) {
        home.H.original = saved(mode, rule);
        await home.newSessionModal();
        out["ns_" + mode] = elements.modal.innerHTML;
      }
      // «Тред» chosen: the rule shows and the clarify hint swaps
      const hint = { textContent: "" };
      el("ns-rule", { hidden: true });
      el("ns-clarify", { closest: () => ({ querySelector: () => hint }) });
      const query = document.querySelector;
      let chosen = "thread";
      document.querySelector = (sel) => (sel === 'input[name="ns-mode"]:checked' ? { value: chosen } : query(sel));
      home.nsModeSync();
      out.sync = [elements["ns-rule"].hidden, hint.textContent, home.nsMode()];
      chosen = "board"; home.nsModeSync();
      out.syncBack = [elements["ns-rule"].hidden, hint.textContent];
      document.querySelector = query;
    """)
    assert out["ov"] == [[None, None], ["thread", None], ["thread", "veto"], [None, None], ["board", None],
                         [None, None], [None, None]]
    board = out["ns_board"]
    ids = ('id="ns-desc"', 'id="ns-mode"', 'id="ns-rule"', 'id="ns-clarify"', 'id="ns-ws"', 'id="ns-model"')
    order = [board.index(x) for x in ids]
    assert order == sorted(order)
    assert '<fieldset class="full ns-ws ns-rule" id="ns-rule" hidden>' in board
    assert 'id="ns-mode-board" value="board" checked>' in board and 'id="ns-mode-thread" value="thread">' in board
    assert 'id="ns-rule-democracy" value="democracy" checked>' in board
    assert 'Task board <span class="hint">tasks with owners, statuses and reviews</span>' in board
    assert "the goal is decided by a vote" in board and "the planner first asks you about the unclear points" in board
    thread = out["ns_thread"]
    assert '<fieldset class="full ns-ws ns-rule" id="ns-rule"><legend>Goal decision</legend>' in thread
    assert 'id="ns-mode-thread" value="thread" checked>' in thread
    assert 'id="ns-rule-veto" value="veto" checked>' in thread
    assert "Veto: all AI agents for it, none against" in thread
    assert "in the discussion #2" in thread and "the planner first asks you" not in thread
    assert out["sync"] == [False, "the agents first ask you about the unclear points of the goal in the discussion #2; "
                                  "an agent's critical question pauses the whole swarm until you answer", "thread"]
    assert out["syncBack"][0] is True and out["syncBack"][1].startswith("the planner first asks you")


@needs_node
def test_home_settings_page_and_the_card_of_a_thread_session():
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      // the settings page: the mode and the rule after clarify, the digest age among the agents' fields
      el("settings-form"); el("settings-nav"); el("profiles"); el("save-bar"); el("save-msg"); el("add-profile");
      const v = { swarm: { name: "S", language: "en", clarify: true, mode: "thread", goal_rule: "veto", network: "on" },
                  agents: { digest_seconds: 60 }, shell: {}, workspace: {}, server: {}, tracing: {},
                  mcp: { external: {} }, models: {} };
      home.H.view = JSON.parse(JSON.stringify(v)); home.H.original = JSON.parse(JSON.stringify(v));
      home.renderSettings(true);
      out.settings = elements["settings-form"].innerHTML;
      // the cards
      const card = (extra) => home.sessionCard(Object.assign({ id: "s1", title: "S", status: "running", network: "off",
                                                               tasks: { total: 4, done: 1 },
                                                               agents: { total: 3, working: 1 } }, extra));
      const vote = (rule, pro, counted, needed) => ({ rule, counted, for: pro, against: [], needed, met: false });
      out.thread = card({ mode: "thread", goal_rule: "veto", posts: 4, tasks: { total: 0, done: 0 },
                          goal_vote: vote("veto", ["A"], 3, 3) });
      out.noVote = card({ mode: "thread", goal_rule: "democracy", posts: 0, goal_vote: null });
      out.board = card({ mode: "board", goal_rule: null, posts: 0, goal_vote: null });
      out.older = card({});
      out.hint = home.t("api_hint_auto");
      home.H.lang = "ru";
      out.ruHint = home.t("api_hint_auto");
      out.ruThread = card({ mode: "thread", posts: 2, goal_vote: vote("democracy", [], 2, 2) });
    """)
    settings = out["settings"]
    assert (settings.index('data-path="swarm.clarify"') < settings.index('data-path="swarm.mode"')
            < settings.index('data-path="swarm.goal_rule"') < settings.index('data-path="swarm.network"'))
    assert '<option value="thread" selected>Thread</option>' in settings
    assert '<option value="veto" selected>Veto</option>' in settings
    assert "Chosen when a session is created: existing sessions keep their mode." in settings
    assert "Thread mode only. Democracy: the goal is reached when more than half" in settings
    assert 'data-path="agents.digest_seconds" data-type="float" type="number" step="10" min="10" value="60"' in settings
    assert "Thread mode only: an idle agent gets what its digest subscriptions gathered" in settings
    card = out["thread"]
    head = between(card, '<div class="sc-head">', "</div>")
    assert '<span class="badge warn"' in head and '<span class="badge plain">Thread</span>' in head
    assert head.index("No internet") < head.index(">Thread<")
    assert 'title="Veto: all AI agents for it, none against"' in card and "Goal votes: 1 of 3 (needs 3)" in card
    assert 'style="width:33%"' in card and 'style="left:100%"' in card and "posts: 4" in card and "tasks" not in card
    assert "Goal votes" not in out["noVote"] and "meter" not in out["noVote"] and "posts: 0" in out["noVote"]
    assert out["board"] == out["older"] and "1 of 4 tasks" in out["board"] and "Thread" not in out["board"]
    assert "(and, on the task board, reviews)" in out["hint"] and "(и, на доске задач, ревью)" in out["ruHint"]
    assert "ё" not in out["ruHint"] and "Голоса за цель: 0 из 2 (нужно 2)" in out["ruThread"]
    assert ">Тред<" in out["ruThread"]


# ------------------------------------------------------------------------------------------------ a real hub
@needs_node
async def test_the_thread_tab_renders_a_real_thread_snapshot(tmp_path):
    """The fields the thread tab reads exist in a real thread hub's answers: the snapshot's posts and goal_vote,
    GET /api/posts/{id} (#1 with its votes, an ordinary post with a reply), rendered by app.js."""
    sw = make_swarm(tmp_path, "thread")
    await sw.start(run_agents=False)
    try:
        a, b, c = llm_names(sw)
        sw.set_goal(USER, "Build the parser", "With tests.")
        post = sw.forum.create(a, "Lexer first", f"The **plan**, @{b}.")
        one = sw.forum.comment(b, post["id"], "Agreed")
        reply = sw.forum.comment(c, post["id"], "Me too", reply_to=one["id"])
        sw.forum.vote(a, "#1", "like")
        sw.forum.vote(USER, f"#{post['id']}", "like")
        with TestClient(web.create_app(sw), base_url=BASE) as client:
            state = client.get("/api/state").json()
            goal = client.get("/api/posts/1").json()
            detail = client.get(f"/api/posts/{post['id']}").json()
    finally:
        await sw.stop()
        sw.close()
    out = run_js(r"""
      app.S.lang = "en";
      app.S.state = JSON.parse(process.env.STATE);
      el("thread-feed");
      app.renderThreadFeed();
      out.feed = elements["thread-feed"].innerHTML;
      out.mode = app.sessionMode();
      out.short = app.goalVoteShort(app.S.state.goal_vote);
      out.goal = app.postViewHtml(JSON.parse(process.env.GOAL));
      out.post = app.postViewHtml(JSON.parse(process.env.DETAIL));
    """, env={"STATE": json.dumps(state), "GOAL": json.dumps(goal), "DETAIL": json.dumps(detail)})
    assert out["mode"] == "thread" and out["short"] == "1 of 3 for, needs 2"
    feed = out["feed"]
    rows = [feed.index(f'data-open-post="{n}"') for n in (1, 2, post["id"])]
    assert rows == sorted(rows) and "Lexer first" in feed
    goal_html = out["goal"]
    assert "Goal decision: democracy" in goal_html and "for: 1 of 3, needs 2" in goal_html
    assert all(name in goal_html for name in (a, b, c)) and 'data-goal-act="complete"' in goal_html
    post_html = out["post"]
    assert f'id="tcomm-{one["id"]}"' in post_html and f'id="tcomm-{reply["id"]}" style="margin-left:22px"' in post_html
    assert f'class="vote-btn like on" data-vote="p{post["id"]}"' in post_html and "<strong>plan</strong>" in post_html
    assert f'<span class="mention">@{b}</span>' in post_html and f'data-post-edit="{post["id"]}"' in post_html
