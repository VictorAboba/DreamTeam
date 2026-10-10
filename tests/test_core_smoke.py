from dreamteam import prompts
from dreamteam.config import Config, render_template
from dreamteam.names import generate_name
import tomllib


def test_fake_swarm_members(fake_swarm):
    fake_swarm.add_member("John Smith")
    assert fake_swarm.resolve_member("john  smith") == "John Smith"
    assert fake_swarm.resolve_member("user") == "user"
    fake_swarm.notify("John Smith", "n_restart")
    assert fake_swarm.has_pending("John Smith")


def test_templates_parse(tmp_path):
    for provider in ("anthropic", "openai", "openrouter", "ollama", "vllm"):
        raw = tomllib.loads(render_template("X", "ru", provider, goal='Say "hi"'))
        cfg = Config.from_dict(tmp_path, raw)
        assert cfg.model(None).provider in ("anthropic", "openai")


def test_prompts_render():
    for lang in ("ru", "en"):
        text = prompts.system_prompt(lang, name="A B", swarm="S", role="R", role_description="Does R.",
                                     instructions="do", shell="bash", scratch=".scratch/a.b/", max_agents=5)
        assert "A B" in text and "Does R." in text and "`.scratch/a.b/`" in text
        for key in prompts.TEXTS["en"]:
            assert key in prompts.TEXTS[lang], key


def test_names_unique():
    taken = set()
    for lang in ("ru", "en"):
        for _ in range(60):
            n = generate_name(taken, lang)
            assert n.lower() not in {t.lower() for t in taken}
            taken.add(n)
