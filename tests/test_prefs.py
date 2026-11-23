"""Tu or vous, and the operator's dictionary."""

import asyncio
import json
import os
import stat

import pytest

from talk_with_hermes import persona
from talk_with_hermes.desktop_bridge import DesktopCall, HandleStore, config_from
from talk_with_hermes.prefs import Prefs, PrefsError, PrefsStore, Word, apply_words, parse

KAVYRO = Word("Kavyro", ("Caviro", "Cavaro", "Kaviro"))


# -- parsing ------------------------------------------------------------------


def test_defaults_are_tu_and_no_words():
    assert parse({}) == Prefs("tu", ())


def test_an_edit_is_cleaned_not_refused_for_harmless_mess():
    prefs = parse({"address": "vous", "words": [
        {"word": "  Kavyro ", "heard": ["Caviro", " caviro", "Kavyro", "", "Cavaro"]},
        {"word": "", "heard": ["rien"]},  # an empty row left on the page
        {"word": "FluentCart", "heard": "fluent cart, fluent carte"},
    ]})
    assert prefs.address == "vous"
    assert prefs.words == (Word("Kavyro", ("Caviro", "Cavaro")), Word("FluentCart", ("fluent cart", "fluent carte")))


@pytest.mark.parametrize("raw, reason", [
    ([], "illisibles"),
    ({"address": "toi"}, "tu ou vous"),
    ({"words": "Kavyro"}, "liste"),
    ({"words": [{"word": "Kavyro"}, {"word": "kavyro"}]}, "déjà"),
    ({"words": [{"word": "x" * 61}]}, "60"),
    ({"words": [{"word": f"w{i}"} for i in range(201)]}, "200"),
    ({"words": [{"word": "a", "heard": [f"v{i}" for i in range(13)]}]}, "12"),
])
def test_bad_edits_are_refused_in_french(raw, reason):
    with pytest.raises(PrefsError, match=reason):
        parse(raw)


# -- corrections --------------------------------------------------------------


def test_heard_variants_become_the_right_word_whole_words_only():
    text = "Combien de fichiers dans mon dossier caviro ? Et Cavaro-bis, et Caviros."
    assert apply_words(text, (KAVYRO,)) == "Combien de fichiers dans mon dossier Kavyro ? Et Kavyro-bis, et Caviros."


def test_longer_variants_win():
    words = (Word("FluentCart", ("fluent cart",)), Word("Kart", ("cart",)))
    assert apply_words("ouvre fluent cart puis cart", words) == "ouvre FluentCart puis Kart"


def test_no_words_no_change():
    assert apply_words("caviro", ()) == "caviro"


# -- storage ------------------------------------------------------------------


def test_store_round_trip_private_file(tmp_path):
    store = PrefsStore(tmp_path / "s" / "prefs.json")
    assert store.load() == Prefs()
    store.save(Prefs("vous", (KAVYRO,)))
    assert store.load() == Prefs("vous", (KAVYRO,))
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_a_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text("{nope", encoding="utf-8")
    assert PrefsStore(path).load() == Prefs()
    path.write_text(json.dumps({"address": "toi"}), encoding="utf-8")
    assert PrefsStore(path).load() == Prefs()


# -- what the voice model and the agent receive ------------------------------


def test_the_address_block_comes_last_and_matches_the_choice():
    tu = persona.system_instruction("full", "tu")
    vous = persona.system_instruction("full", "vous")
    assert tu.endswith(persona._ADDRESS["tu"]) and vous.endswith(persona._ADDRESS["vous"])
    assert "tu vouvoies toujours" in vous and "tu tutoies toujours" in tu
    assert persona.system_instruction("full", "nimporte") == tu


def test_the_dictionary_reaches_the_voice_model():
    text = persona.system_instruction("full", "tu", (KAVYRO, Word("Iris")))
    assert "Kavyro (souvent entendu : Caviro, Cavaro, Kaviro)" in text and " ; Iris." in text
    assert "Vocabulaire" not in persona.system_instruction("full", "tu", ())


def test_the_agent_is_told_how_to_address_the_operator():
    assert "vouvoie l'opérateur" in persona.delegated_prompt("x", "vous")
    assert "tutoie l'opérateur" in persona.delegated_prompt("x")


def test_config_carries_the_prefs():
    config = config_from({}, "k", Prefs("vous", (KAVYRO,)))
    assert config.address == "vous" and config.words == (KAVYRO,)


class _Session:
    instances = []

    def __init__(self, config, handlers, **_):
        self.config, self.handlers = config, handlers
        _Session.instances.append(self)

    async def start(self):
        return None

    async def close(self):
        return None


def test_a_delegated_request_is_corrected_and_prefs_are_read_at_start(tmp_path):
    store = PrefsStore(tmp_path / "prefs.json")
    store.save(Prefs("vous", (KAVYRO,)))

    async def run():
        call = DesktopCall(settings=dict, api_key=lambda: "k", handles=HandleStore(tmp_path / "r.json"),
                           prefs=store, session_factory=_Session)
        await call.start()
        session = _Session.instances[-1]
        task = asyncio.ensure_future(session.handlers.on_tool_call("c1", "fichiers dans caviro ?"))
        await asyncio.sleep(0)
        event = call.poll(0)["events"][0]
        call.answer("c1", "ok")
        await task
        return session, event

    session, event = asyncio.run(run())
    assert session.config.address == "vous"
    assert event["request"] == "fichiers dans Kavyro ?"
    assert event["prompt"] == persona.delegated_prompt("fichiers dans Kavyro ?", "vous")


def test_a_voice_is_kept_and_checked_against_the_service():
    from talk_with_hermes.gemini_live import VOICES, voices

    names = [n for n, _ in VOICES]
    assert len(names) == 30 and len(set(names)) == 30
    assert voices()[0] == {"id": "Zephyr", "label": "Zephyr", "trait": "bright"}
    assert parse({"voice": "Kore"}, voices=names).voice == "Kore"
    assert parse({}, voices=names).voice == ""
    with pytest.raises(PrefsError, match="Voix inconnue"):
        parse({"voice": "Siri"}, voices=names)
    assert parse({"voice": "Kore"}).to_json()["voice"] == "Kore"


def test_the_chosen_voice_wins_over_the_setting():
    from talk_with_hermes.desktop_bridge import config_from
    from talk_with_hermes.prefs import Prefs

    assert config_from({"voice": "Puck"}, "k", Prefs(voice="Kore")).voice == "Kore"
    assert config_from({"voice": "Charon"}, "k", Prefs()).voice == "Charon"
    assert config_from({}, "k").voice == "Puck"
