"""Read-aloud: markdown cleanup, voice choice, and a real synthesis when a voice and onnxruntime are available."""
import io, os, wave

import pytest

import tts


def test_strip_markdown_code_links_emphasis():
    md = ("# Title\n\nSee **bold** and *italic* with [the docs](https://x.org/a).\n\n"
          "```python\nprint('hi')\n```\n\nUse `pip install` then my_var_name. Visit https://example.com now")
    out = tts.strip_markdown(md)
    assert out.startswith("Title. See bold and italic with the docs.")
    assert "code block." in out and "print" not in out
    assert "pip install" in out and "my_var_name" in out
    assert "http" not in out and "#" not in out and "*" not in out and "`" not in out


def test_strip_markdown_tables_lists_french():
    md = "| Nom | Âge |\n|---|:--:|\n| Ana | 30 |\n\n- un\n- deux\n1. trois\n> cité\n\n```\nx\n```"
    out = tts.strip_markdown(md, "fr")
    assert out == "Nom, Âge. Ana, 30. un. deux. trois. cité. bloc de code."


def test_strip_markdown_think_and_images():
    assert tts.strip_markdown("<think>hidden</think>Hello ![a cat](c.png)") == "Hello a cat."


def test_pick_voice_by_language(monkeypatch):
    monkeypatch.setattr(tts, "ready", lambda vid: False)
    assert tts.pick_voice("fr") == "fr_FR-siwis-medium"
    assert tts.pick_voice("French") == "fr_FR-siwis-medium"
    assert tts.pick_voice("fr-FR") == "fr_FR-siwis-medium"
    assert tts.pick_voice("English") == "en_US-lessac-medium"
    assert tts.pick_voice("ja") == "en_US-lessac-medium"  # no voice: English
    assert tts.pick_voice(None) == "en_US-lessac-medium"
    assert tts.pick_voice("en", "de_DE-thorsten-medium") == "de_DE-thorsten-medium"


def test_guess_lang():
    assert tts.guess_lang("Voici la réponse : il est possible que vous ayez raison.") == "fr"
    assert tts.guess_lang("Here is the answer and it is what you need.") == "en"


def test_status_and_urls():
    st = tts.status()
    assert {v["id"] for v in st["voices"]} == set(tts.BY_ID)
    assert all("ready" in v and "job" in v for v in st["voices"])
    assert tts._url("fr_FR-siwis-medium", ".onnx").endswith("/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx")


def test_speak_without_voice_fails(monkeypatch):
    monkeypatch.setattr(tts, "ready", lambda vid: False)
    with pytest.raises(RuntimeError):
        tts.speak("Hello", "en")


# the test config points VOICE_DIR at a temp folder: reuse a voice from the real models/voice/piper if present
REAL = os.environ.get("SPARKY_TEST_PIPER_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "voice", "piper")


@pytest.mark.skipif(not os.environ.get("SPARKY_TEST_PIPER_DIR")
                    and not (tts.installed() and os.path.exists(os.path.join(REAL, "en_US-lessac-medium.onnx.json"))),
                    reason="onnxruntime/espeakng-loader or the English Piper voice not installed")
def test_real_synthesis(monkeypatch):
    monkeypatch.setattr(tts, "DIR", REAL)
    assert tts.installed() and tts.ready("en_US-lessac-medium")
    data = tts.speak("## Hello\n\nThis is **Sparky**. It reads replies aloud, sentence by sentence!", "en")
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 22050 and w.getsampwidth() == 2
        assert 1.5 < w.getnframes() / w.getframerate() < 12
    fast = tts.speak("This is Sparky.", "en", speed=2.0)
    slow = tts.speak("This is Sparky.", "en", speed=0.7)
    assert len(fast) < len(slow)
