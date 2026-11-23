"""The voice is the agent the plugin is installed on, not "Hermes"."""

import asyncio

from talk_with_hermes import identity, persona
from talk_with_hermes.desktop_bridge import DesktopCall, HandleStore, config_from
from talk_with_hermes.gemini_live import GeminiLiveSession, LiveConfig, LiveHandlers
from talk_with_hermes.prefs import PrefsStore

SOUL = """# Iris

Je suis Iris, le bras droit indépendant de Camille. Je l'aide à décider.
Deuxième ligne du même paragraphe.

Je travaille directement pour Camille. Ceci ne doit pas partir.

## Style

Secret de style.
"""


def profile(tmp_path, soul=None, name="iris"):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    if soul is not None:
        (home / "SOUL.md").write_text(soul, encoding="utf-8")
    return home


def test_name_and_first_paragraph_only(tmp_path):
    who = identity.load(profile(tmp_path, SOUL))
    assert who.name == "Iris"
    assert who.intro.startswith("Je suis Iris, le bras droit") and "Deuxième ligne" in who.intro
    assert "Ceci ne doit pas partir" not in who.intro and "Secret de style" not in who.intro


def test_without_soul_the_profile_name(tmp_path):
    assert identity.load(profile(tmp_path, None, "orion-coach")).name == "Orion Coach"
    assert identity.load(profile(tmp_path, "Pas de titre\n\n# Plus tard", "maia")).name == "Maia"


def test_without_a_soul_title_the_display_name_hermes_shows(tmp_path, host_yaml):
    (tmp_path / "profile.yaml").write_text("display_name: Scout\ndescription: x\n", encoding="utf-8")
    assert identity.load(tmp_path).name == "Scout"
    (tmp_path / "profile.yaml").write_text("- not a mapping\n", encoding="utf-8")
    assert identity.load(tmp_path).name == "Hermes"


def test_a_bare_home_is_hermes(tmp_path):
    assert identity.load(tmp_path) == identity.Identity("Hermes", "")
    assert identity.load(tmp_path / "missing").name == "Hermes"


def test_name_and_intro_are_bounded_and_cleaned(tmp_path):
    who = identity.load(profile(tmp_path, "# **Da\u202ente**\x07 " + "x" * 80 + "\n\n" + "Mot. " * 300))
    assert len(who.name) <= identity.MAX_NAME_CHARS and "\u202e" not in who.name and "*" not in who.name
    assert len(who.intro) <= identity.MAX_INTRO_CHARS and who.intro.endswith(".")


def test_the_voice_says_its_name():
    text = persona.system_instruction("full", "tu", (), "Iris", "Je suis Iris, le bras droit de Camille.")
    assert text.startswith("Tu es Iris,")
    assert "tu réponds Iris" in text and "Je suis Iris, le bras droit de Camille." in text
    assert "Hermes est le logiciel sur lequel Iris tourne" in text
    assert "Tu es Hermes" not in text and "\u2014" not in text
    assert "Hermes est le logiciel" not in persona.system_instruction("full")  # default stays Hermes


def test_the_live_setup_carries_the_identity():
    cfg = LiveConfig(api_key="k", name="Iris", intro="Je suis Iris.")
    setup = GeminiLiveSession(cfg, LiveHandlers()).setup_message(resume=None)["setup"]
    assert setup["systemInstruction"]["parts"][0]["text"].startswith("Tu es Iris,")


def test_a_call_names_the_agent_and_rereads_it(tmp_path):
    names = iter(["Iris", "Iris", "Maia"])

    class Session:
        def __init__(self, config, handlers, **_):
            Session.config = config

        async def start(self):
            return None

        async def close(self):
            return None

    async def run():
        call = DesktopCall(settings=dict, api_key=lambda: "k", handles=HandleStore(tmp_path / "r.json"),
                           prefs=PrefsStore(tmp_path / "p.json"), session_factory=Session,
                           identity=lambda: identity.Identity(next(names), ""))
        before = call.refresh_key()["agent"]
        started = await call.start()
        await call.stop()
        after = call.refresh_key()["agent"]
        return before, started["agent"], Session.config.name, after

    assert asyncio.run(run()) == ("Iris", "Iris", "Iris", "Maia")


def test_config_defaults_to_hermes():
    assert config_from({}, "k").name == "Hermes"


# -- style: the voice sounds like the agent -----------------

STYLED = """# Iris

Je suis Iris.

## Style

Je tutoie Camille. Calme, direct, précis.

- Ni flatterie, ni hype.
- Liste ou tableau seulement si utile.

### Détail

Une ligne sous un sous-titre.

## Franchise

Ceci ne doit pas partir.
"""


def test_the_style_section_is_read_whole_and_stops_at_the_next_section(tmp_path):
    who = identity.load(profile(tmp_path, STYLED))
    assert who.style.startswith("Je tutoie Camille. Calme, direct, précis.")
    assert "Ni flatterie, ni hype." in who.style and "- " not in who.style  # bullets become clauses
    assert "Une ligne sous un sous-titre." in who.style and "Détail" not in who.style
    assert "Ceci ne doit pas partir" not in who.style
    assert "Je tutoie" not in who.intro  # the intro stays the first paragraph only


def test_other_names_for_the_style_section(tmp_path):
    soul = "# Vega\n\nJe suis Vega.\n\n## Voix de Camille\n\nDirecte, chaleureuse, simple.\n"
    assert identity.load(profile(tmp_path, soul, "vega")).style == "Directe, chaleureuse, simple."
    assert identity.style_from_soul("# X\n\n## Tone\n\nDry.\n") == "Dry."
    assert identity.style_from_soul("# X\n\n## Stylesheet\n\nNot a style.\n") == ""


def test_no_style_section_no_style(tmp_path):
    assert identity.load(profile(tmp_path, SOUL.replace("## Style", "## Autre"))).style == ""
    assert identity.load(profile(tmp_path, None, "maia")).style == ""


def test_the_style_is_bounded_and_cleaned(tmp_path):
    soul = "# Iris\n\n## Style\n\n" + ("Très **long** style. " * 200) + "\n"
    style = identity.load(profile(tmp_path, soul)).style
    assert len(style) <= identity.MAX_STYLE_CHARS and "**" not in style and style.endswith(".")


def test_the_style_wins_over_the_default_tone_and_the_address_still_comes_last():
    text = persona.system_instruction(name="Iris", style="Calme, direct, précis.", address="vous")
    assert "« Calme, direct, précis. »" in text and "elle prime sur le ton par défaut" in text
    assert text.index("voix calme et posée") < text.index("Calme, direct, précis.")  # after the persona
    assert text.rstrip().endswith(persona.system_instruction(address="vous").rstrip().rsplit("\n\n", 1)[1])
    assert "Ton style" not in persona.system_instruction(name="Iris")  # nothing when there is none


def test_the_live_setup_carries_the_style(tmp_path):
    who = identity.load(profile(tmp_path, STYLED))
    cfg = config_from({}, "k", who=who)
    assert cfg.style == who.style
    setup = GeminiLiveSession(cfg, LiveHandlers()).setup_message(resume=None)
    assert "Calme, direct, précis." in setup["setup"]["systemInstruction"]["parts"][0]["text"]
