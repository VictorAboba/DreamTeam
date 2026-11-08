"""Calls that cannot be priced (docs/SPEC.md §10, §17.1, §18.3): where a profile's prices come from, the user's
one notice per start when a call counted tokens as $0, the settings' price_source and the test request's cost
source, and the note after the `serve` / `run` banners."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from dreamteam import cli
from dreamteam import settings as st
from dreamteam.config import Config, ModelProfile
from dreamteam.llm import Usage, uncounted_fields
from dreamteam.models import USER
from test_runtime import call, make_swarm, run_until, shutdown

UNPRICED = {"provider": "openai", "model": "some-model-v1", "base_url": "http://127.0.0.1:9/v1", "api_key": "x"}


def cost_notices(swarm) -> list[dict]:
    return swarm.db.all("SELECT text, data FROM notifications WHERE recipient = ? AND kind = 'u_cost_unknown' "
                        "ORDER BY id", (USER,))


# ---- ModelProfile and llm.uncounted_fields ------------------------------------------------------------------
@pytest.mark.parametrize("profile, model, source, missing", [
    (ModelProfile(provider="openai", model="some-model-v1"), None, "none", ("input_price", "output_price")),
    (ModelProfile(provider="openai", model="x", input_price=2.0), None, "partial", ("output_price",)),
    (ModelProfile(provider="openai", model="x", output_price=0), None, "partial", ("input_price",)),
    (ModelProfile(provider="openai", model="x", input_price=0, output_price=0), None, "free", ()),
    (ModelProfile(provider="openai", model="x", input_price=1.25, output_price=10.0), None, "profile", ()),
    (ModelProfile(model="claude-opus-5-5"), None, "table", ()),
    (ModelProfile(model="claude-opus-5-5", input_price=1.0), None, "table", ()),
    (ModelProfile(model="claude-opus-5-5", input_price=0, output_price=0), None, "free", ()),
    (ModelProfile(model="claude-opus-5-5", input_price=1.0, output_price=2.0), None, "profile", ()),
    # the response's model: a known one prices the call even when the profile's model is not in the table
    (ModelProfile(provider="openai", model="alias"), "claude-haiku-5-5", "table", ()),
    (ModelProfile(provider="openai", model="alias"), "other-model", "none", ("input_price", "output_price")),
])
def test_price_source_and_missing_prices(profile, model, source, missing):
    assert profile.price_source(model) == source
    assert profile.missing_prices(model) == missing
    assert profile.has_prices is (not profile.missing_prices())


def test_a_profile_with_only_an_input_price_is_not_fully_priced():
    """The output tokens of such a profile cost $0: has_prices used to say it was priced."""
    profile = ModelProfile(provider="openai", model="x", input_price=2.0)
    assert profile.prices() == (2.0, 0.0, 0.2, 2.5) and profile.has_prices is False


def test_uncounted_fields():
    unpriced = ModelProfile(provider="openai", model="some-model-v1")
    assert uncounted_fields(unpriced, Usage(input_tokens=10)) == ("input_price", "output_price")
    assert uncounted_fields(unpriced, Usage(input_tokens=10, cost_usd=0.0)) == ()     # the provider's cost counts
    assert uncounted_fields(ModelProfile(provider="openai", model="x", input_price=1.0), Usage()) == ("output_price",)
    assert uncounted_fields(ModelProfile(provider=" Scripted ", model="s"), Usage()) == ()  # no bill
    assert uncounted_fields(ModelProfile(provider="openai", model="x"), Usage(), "claude-opus-5-5") == ()
    assert uncounted_fields(SimpleNamespace(provider="openai"), Usage()) == ()           # a test double


# ---- the runtime notice -----------------------------------------------------------------------------------------
async def test_the_user_is_told_once_per_start_when_calls_count_as_zero(tmp_path):
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0})
    try:
        unpriced = ModelProfile(name="remote", **UNPRICED)
        for _ in range(5):
            assert swarm.record_usage("x", Usage(input_tokens=1000, output_tokens=100), unpriced, "some-model-v1") == 0
        (notice,) = cost_notices(swarm)
        assert notice["text"].startswith('DreamTeam cannot count the cost of model profile "remote" (model '
                                         'some-model-v1): it has no input_price, output_price, and the provider')
        assert json.loads(notice["data"]) == {"profile": "remote", "model": "some-model-v1",
                                              "fields": "input_price, output_price"}
        # no notice when the provider reports the cost, for a free (0 / 0) or a scripted profile
        swarm.record_usage("x", Usage(input_tokens=1000, cost_usd=0.01), unpriced, "some-model-v1")
        free = ModelProfile(name="local", provider="openai", model="qwen", input_price=0, output_price=0)
        swarm.record_usage("x", Usage(input_tokens=1000), free, "qwen")
        swarm.record_usage("x", Usage(input_tokens=1000), ModelProfile(name="s", provider="scripted", model="s"), "s")
        assert len(cost_notices(swarm)) == 1
        # a partial profile names the missing price; another model of the same profile is told about too
        partial = ModelProfile(name="half", provider="openai", model="m", input_price=1.0)
        swarm.record_usage("x", Usage(input_tokens=1000, output_tokens=10), partial, "m")
        swarm.record_usage("x", Usage(input_tokens=1000), unpriced, "some-model-v2")
        data = [json.loads(n["data"]) for n in cost_notices(swarm)]
        assert [(d["profile"], d["model"], d["fields"]) for d in data[1:]] == [
            ("half", "m", "output_price"), ("remote", "some-model-v2", "input_price, output_price")]
        # the next start tells again (a quiet notice: it wakes nobody)
        await swarm.start(run_agents=False)
        await swarm.stop()
        swarm.record_usage("x", Usage(input_tokens=1000), unpriced, "some-model-v1")
        assert len(cost_notices(swarm)) == 4
    finally:
        swarm.close()


async def test_a_renamed_profile_is_not_told_about_again(tmp_path):
    """Swarm.rename_profiles (a rename saved in the settings, docs/SPEC.md §17.2): the same profile under its new
    name brings no second notice in the same start."""
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0})
    try:
        swarm.record_usage("x", Usage(input_tokens=1000), ModelProfile(name="remote", **UNPRICED), "some-model-v1")
        swarm.rename_profiles({"remote": "remote-2"})
        swarm.record_usage("x", Usage(input_tokens=1000), ModelProfile(name="remote-2", **UNPRICED), "some-model-v1")
        assert [json.loads(n["data"])["profile"] for n in cost_notices(swarm)] == ["remote"]
    finally:
        swarm.close()


async def test_agents_on_an_unpriced_profile_produce_one_notice(tmp_path):
    """Real activations of a member whose profile has no prices and whose provider reports no cost (ScriptedLLM
    standing in for an OpenAI-compatible endpoint): several LLM calls, one notice to the user, in Russian too."""
    steps = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        steps["n"] += 1
        return call("board") if steps["n"] < 4 else call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"language": "ru"}, models={"default": UNPRICED})
    await swarm.start()
    try:
        swarm.set_goal(USER, "Write a haiku")
        await run_until(lambda: steps["n"] >= 4)
        (notice,) = cost_notices(swarm)
        assert notice["text"].startswith("DreamTeam не может посчитать стоимость профиля модели «default» (модель "
                                         "some-model-v1): у него не указаны input_price, output_price, а провайдер")
        assert rec.count(swarm.members()[0]["name"]) >= 4 and swarm.spent_usd() == 0
    finally:
        await shutdown(swarm)


# ---- settings: public view, round trip, presets, the test request --------------------------------------------
def test_public_view_carries_the_price_source_and_an_update_drops_it(tmp_path):
    raw = {"models": {"default": {"provider": "anthropic", "model": "claude-opus-5-5"},
                      "remote": UNPRICED, "half": {**UNPRICED, "input_price": 0.5},
                      "local": {**UNPRICED, "input_price": 0, "output_price": 0},
                      "priced": {**UNPRICED, "input_price": 0.3, "output_price": 1.2}}}
    view = st.public_view(raw)
    assert {name: p["price_source"] for name, p in view["models"].items()} == {
        "default": "table", "remote": "none", "half": "partial", "local": "free", "priced": "profile"}
    new = st.apply_update(raw, {"models": view["models"]})
    assert all("price_source" not in p for p in new["models"].values())
    assert new["models"]["local"]["input_price"] == 0 and "input_price" not in new["models"]["remote"]
    st.validate(tmp_path, new)
    profile = st.profile_from_view(raw, {**view["models"]["half"], "api_key": "x"}, name="half")
    assert profile.price_source() == "partial"
    openai = next(p for p in st.PROVIDER_CHOICES if p["id"] == "openai")
    assert (openai["input_price"], openai["output_price"]) == (1.25, 10.0)


async def test_the_test_request_says_how_its_cost_was_obtained():
    replies: list[dict] = []

    class Stub(BaseHTTPRequestHandler):  # an OpenAI-compatible endpoint; `usage.cost` only when told to
        def log_message(self, *args):
            pass

        def _send(self, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"object": "list", "data": [{"id": "m", "object": "model"}]})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._send({"id": "c", "object": "chat.completion", "created": 1, "model": "m",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "OK"}}],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10, **replies[-1]}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        cases = [({"cost": 0.0002}, {}, "provider"), ({}, {}, "none"), ({}, {"input_price": 1.0}, "partial"),
                 ({}, {"input_price": 0, "output_price": 0}, "free"),
                 ({}, {"input_price": 1.0, "output_price": 2.0}, "profile")]
        for usage, prices, expected in cases:
            replies.append(usage)
            profile = st.profile_from_view({}, {"provider": "openai", "model": "m", "base_url": base,
                                                "api_key": "test-key", **prices})
            result = await st.check_profile(profile, live=True, timeout=5)
            assert result["ok"] and result["cost_source"] == expected, (usage, prices, result)
        assert (await st.check_profile(profile, timeout=5))["cost_source"] is None  # not live: no request
    finally:
        server.shutdown()
        server.server_close()


# ---- the CLI banners ------------------------------------------------------------------------------------------
def test_unpriced_line_names_the_profiles_without_prices(tmp_path):
    def config(models: dict) -> Config:
        return Config.from_dict(tmp_path, {"models": models})

    line = st.unpriced_line(config({
        "default": {"provider": "anthropic", "model": "claude-opus-5-5"}, "remote": UNPRICED,
        "half": {**UNPRICED, "model": "kimi", "output_price": 2.0},
        "local": {**UNPRICED, "input_price": 0, "output_price": 0},
        "test": {"provider": "scripted", "model": "scripted"}}))
    assert line.startswith("Note: model profiles without prices: remote (some-model-v1), half (kimi: no input_price). ")
    assert "unless the provider reports the cost" in line and "0 for a free model" in line and line.isascii()
    assert st.unpriced_line(config({"default": {"provider": "scripted", "model": "s"}})) is None


async def test_serve_and_run_print_the_note_after_their_banner(tmp_path, monkeypatch, capsys):
    from dreamteam import launcher, web

    async def fake_launcher(manager, host, port, on_started=None, on_listening=None):
        on_listening()
        on_started()
        await asyncio.Event().wait()

    async def fake_board(swarm, host, port, on_started=None):
        on_started()
        await asyncio.Event().wait()

    monkeypatch.setattr(launcher, "serve_launcher", fake_launcher)
    monkeypatch.setattr(web, "serve", fake_board)
    monkeypatch.setattr(cli, "_port_free", lambda host, port: True)
    scripted = {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}
    common = {"swarm": {"name": "Run", "language": "en", "start_agents": 0}, "workspace": {"git": False},
              "skills": {"env_retention_days": 0}, "mcp": {"library_dir": str(tmp_path / "library")}}
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    st.write_raw(home, {**common, "models": {"default": scripted, "remote": UNPRICED}})
    stop = asyncio.Event()
    stop.set()  # stops right after the banner
    assert await cli._serve_home(home, "127.0.0.1", 1, console=False, open_browser=False, stop=stop) == 0
    err = capsys.readouterr().err
    assert "DreamTeam is running." in err and err.index("DreamTeam is running.") < err.index(
        "Note: model profiles without prices: remote (some-model-v1).")

    for project, models, noted in (("proj", {"default": scripted, "remote": UNPRICED}, True),
                                   ("proj2", {"default": scripted}, False)):
        config = Config.from_dict(tmp_path / project, {**common, "models": models})
        assert await cli._serve_swarm(config.project_dir, config, "127.0.0.1", 1, None, False, False, stop=stop) == 0
        err = capsys.readouterr().err
        assert 'DreamTeam "Run" is running.' in err
        assert ("Note: model profiles without prices: remote (some-model-v1)." in err) is noted, err
