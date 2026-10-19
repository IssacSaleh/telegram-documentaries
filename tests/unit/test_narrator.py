"""Edge contracts for the Narrator (Phase 6) — audio boundary, typed.

The TTS response is untrusted (SPECS/TECH.md: "Contracts at boundaries"):
these tests pin the contracts the Narrator exposes before anything is
handed to Telegram —

  * ``OutboundVoice``     — the typed voice-note request (OGG only, never MP3),
  * ``verify_ogg_opus``   — magic-byte verification of the produced bytes,
  * ``parse_tts_response`` — the TTS-response parser; every malformed shape
    raises a *typed* ``NarrationError`` (never a bare ``KeyError``/
    ``IndexError`` from deep inside the SDK response),
  * ``GeminiTtsNarrator`` — the synthesis request shape (pinned model,
    AUDIO modality, deep-male voice preset, style-wrapped script) and its
    typed failure behaviour, with the ``google-genai`` client faked at the
    module boundary.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from app.narrator import (
    NARRATOR_MODEL,
    NARRATOR_SAMPLE_RATE,
    NARRATOR_STYLE,
    NARRATOR_VOICE,
    AudioFormatError,
    GeminiTtsNarrator,
    NarrationError,
    OutboundVoice,
    TtsResponseError,
    parse_tts_response,
    pcm_to_ogg_opus,
    verify_ogg_opus,
)
from tests.conftest import RecordingSleeper


def _ogg_page(body: bytes) -> bytes:
    """Craft one minimal Ogg page carrying ``body`` as its sole segment.

    Page layout (RFC 7845 §A): capture pattern, version, header type,
    granule, serial, sequence, checksum, segment count, segment table,
    then data. The checksum is zeroed — our verifier checks magic bytes,
    not CRCs.
    """
    assert len(body) <= 255, "a single segment holds at most 255 bytes"
    header = (
        b"OggS"                      # capture pattern
        + b"\x00"                    # stream structure version
        + b"\x02"                    # header type: beginning of stream
        + (0).to_bytes(8, "little")  # granule position
        + (1).to_bytes(4, "little")  # bitstream serial
        + (0).to_bytes(4, "little")  # page sequence
        + (0).to_bytes(4, "little")  # page checksum (unchecked here)
        + bytes([1])                 # page segment count
    )
    return header + bytes([len(body)]) + body


def _valid_ogg_opus() -> bytes:
    """A crafted minimal Ogg stream whose first page carries an OpusHead."""
    return _ogg_page(b"OpusHead" + bytes(8))


def _raw_pcm() -> bytes:
    """What the TTS model actually returns — little-endian 16-bit PCM."""
    t = np.arange(480, dtype=np.float64) / 24000.0
    return (np.sin(2 * np.pi * 220 * t) * 32000).astype("<i2").tobytes()


class TestOutboundVoice:
    """The transport request for a voice note — OGG only (TECH.md hard rule)."""

    def test_carries_the_chat_id_and_audio_bytes(self) -> None:
        voice = OutboundVoice(chat_id=555, audio_bytes=b"OggSdata")

        assert voice.chat_id == 555
        assert voice.audio_bytes == b"OggSdata"

    def test_defaults_to_voice_dot_ogg(self) -> None:
        voice = OutboundVoice(chat_id=555, audio_bytes=b"x")

        assert voice.filename == "voice.ogg"
        assert voice.mime_type == "audio/ogg"

    @pytest.mark.parametrize("bad_mime", ["audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg; codecs=opus"])
    def test_mime_type_is_constrained_to_audio_ogg(self, bad_mime: str) -> None:
        """MP3 is not a valid Telegram voice note — the type system rejects it."""
        with pytest.raises(ValidationError):
            OutboundVoice(chat_id=555, audio_bytes=b"x", mime_type=bad_mime)

    def test_requires_audio_bytes(self) -> None:
        with pytest.raises(ValidationError):
            OutboundVoice(chat_id=555)  # type: ignore[call-arg]


class TestVerifyOggOpus:
    """D5: never hand bytes to Telegram unless they are OGG/Opus."""

    def test_accepts_a_valid_ogg_opus_stream(self) -> None:
        verify_ogg_opus(_valid_ogg_opus())  # must not raise

    def test_accepts_a_second_page_after_a_plain_first_page_body(self) -> None:
        # OpusHead sits in the first page body at the usual offset 28.
        stream = _ogg_page(b"OpusHead" + b"\x01\x02\x03")
        verify_ogg_opus(stream)  # must not raise

    def test_rejects_empty_bytes(self) -> None:
        with pytest.raises(AudioFormatError):
            verify_ogg_opus(b"")

    def test_rejects_raw_pcm(self) -> None:
        with pytest.raises(AudioFormatError):
            verify_ogg_opus(_raw_pcm())

    @pytest.mark.parametrize(
        "mp3",
        [
            b"ID3\x04\x00\x00\x00\x00\x00\x00junkjunk",     # ID3v2 tag
            b"\xff\xfb\x90\x64" + bytes(64),                 # frame sync 0xFFEx
            b"\xff\xe3\x18\xc4" + bytes(64),
        ],
    )
    def test_rejects_mp3(self, mp3: bytes) -> None:
        with pytest.raises(AudioFormatError):
            verify_ogg_opus(mp3)

    @pytest.mark.parametrize(
        "truncated",
        [
            b"OggS",                                    # capture pattern only
            b"OggS\x00\x02" + bytes(10),                # partial 27-byte header
            _ogg_page(b"OpusHead")[:27],                # header, no segment table
            _ogg_page(b"OpusHead" + bytes(8))[:30],     # table cut short
            _ogg_page(b"OpusHead" + bytes(8))[:-4],     # body cut short
        ],
    )
    def test_rejects_truncated_headers(self, truncated: bytes) -> None:
        with pytest.raises(AudioFormatError):
            verify_ogg_opus(truncated)

    def test_rejects_an_opus_head_outside_the_first_page(self) -> None:
        """The Opus payload must live in the first page — a stray marker in a
        later page does not make the stream a valid voice note."""
        stream = _ogg_page(b"not an opus stream") + _ogg_page(b"OpusHead" + bytes(8))

        with pytest.raises(AudioFormatError):
            verify_ogg_opus(stream)

    def test_raises_a_typed_narration_error(self) -> None:
        with pytest.raises(NarrationError):
            verify_ogg_opus(b"garbage")


class _FakeTtsResponse:
    """Builds a google-genai-shaped response carrying one inline audio part."""

    @staticmethod
    def one_part(data: bytes | None, mime: str | None) -> SimpleNamespace:
        inline = SimpleNamespace(data=data, mime_type=mime)
        part = SimpleNamespace(inline_data=inline, text=None)
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
        )


VALID_MIME = "audio/l16; rate=24000; channels=1"


class TestParseTtsResponse:
    """The TTS response is untrusted: every shape error is typed."""

    def test_valid_audio_part_parses_to_the_payload_bytes(self) -> None:
        payload = bytes(64)

        parsed = parse_tts_response(_FakeTtsResponse.one_part(payload, VALID_MIME))

        assert parsed == payload

    def test_parameter_order_does_not_break_the_contract(self) -> None:
        payload = bytes(16)

        parsed = parse_tts_response(
            _FakeTtsResponse.one_part(payload, "audio/l16; channels=1; rate=24000")
        )

        assert parsed == payload

    @pytest.mark.parametrize(
        "shape",
        [
            SimpleNamespace(candidates=[]),
            SimpleNamespace(candidates=[SimpleNamespace(content=None)]),
            SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))]),
            SimpleNamespace(
                candidates=[
                    SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=None, text="just words")]))
                ]
            ),
        ],
        ids=["no_candidates", "no_content", "no_parts", "text_only"],
    )
    def test_missing_audio_part_raises_a_typed_error(self, shape) -> None:
        with pytest.raises(TtsResponseError) as exc_info:
            parse_tts_response(shape)

        assert not isinstance(exc_info.value, (KeyError, IndexError))

    @pytest.mark.parametrize(
        "mime",
        [
            "audio/mpeg",
            "audio/ogg",
            "audio/l16; rate=16000; channels=1",   # wrong rate
            "audio/l16; rate=24000; channels=2",    # wrong channel count
            "audio/l16",                            # no params at all
            None,
        ],
        ids=["mp3_mime", "ogg_mime", "wrong_rate", "stereo", "no_params", "none"],
    )
    def test_unexpected_mime_raises_a_typed_error(self, mime: str | None) -> None:
        with pytest.raises(TtsResponseError):
            parse_tts_response(_FakeTtsResponse.one_part(bytes(16), mime))

    @pytest.mark.parametrize("data", [b"", None], ids=["empty", "none"])
    def test_empty_audio_data_raises_a_typed_error(self, data: bytes | None) -> None:
        with pytest.raises(TtsResponseError):
            parse_tts_response(_FakeTtsResponse.one_part(data, VALID_MIME))

    def test_more_than_one_audio_part_is_rejected(self) -> None:
        """Contract: exactly one audio part — anything else is untrusted."""
        part = SimpleNamespace(
            inline_data=SimpleNamespace(data=bytes(8), mime_type=VALID_MIME), text=None
        )
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part, part]))]
        )

        with pytest.raises(TtsResponseError):
            parse_tts_response(response)

    def test_every_failure_is_a_narration_error(self) -> None:
        with pytest.raises(NarrationError):
            parse_tts_response(SimpleNamespace(candidates=[]))


# ---------------------------------------------------------------------------
# PCM -> OGG/Opus conversion (the TTS model returns raw s16le, not an Ogg).
# ---------------------------------------------------------------------------


def _fake_pcm(seconds: float = 0.05) -> bytes:
    """24 kHz little-endian 16-bit mono PCM — the model's real payload."""
    samples = int(NARRATOR_SAMPLE_RATE * seconds)
    t = np.arange(samples, dtype=np.float64) / NARRATOR_SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * 20000).astype("<i2").tobytes()


class TestPcmToOggOpus:
    def test_fake_pcm_becomes_verified_ogg_opus(self) -> None:
        ogg = pcm_to_ogg_opus(_fake_pcm())

        assert ogg[:4] == b"OggS", "Ogg capture tag at offset 0"
        verify_ogg_opus(ogg)  # OpusHead within the first page, typed checks

    def test_empty_pcm_is_rejected(self) -> None:
        with pytest.raises(AudioFormatError):
            pcm_to_ogg_opus(b"")

    def test_odd_length_pcm_is_rejected(self) -> None:
        """A 16-bit stream must be byte-pair aligned; a truncated sample is
        never silently padded into the voice note."""
        with pytest.raises(AudioFormatError):
            pcm_to_ogg_opus(_fake_pcm() + b"\x00")

    def test_output_is_never_an_mp3(self) -> None:
        ogg = pcm_to_ogg_opus(_fake_pcm())

        assert not ogg.startswith(b"ID3")
        assert ogg[0] != 0xFF


# ---------------------------------------------------------------------------
# GeminiTtsNarrator: request shape + typed failures (client faked at the
# module boundary — no network, fully deterministic).
# ---------------------------------------------------------------------------


class FakeGenAiTtsClient:
    """``google-genai`` client double: records the outgoing request.

    Mirrors the SDK surface the Narrator uses —
    ``client.models.generate_content(**kwargs)`` — so the request shape
    (pinned model, AUDIO modality, voice preset, style-wrapped script) is
    observable without a network call.
    """

    def __init__(self, *, response: object = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self._response = response
        self._error = error
        self.models = self

    def generate_content(self, **kwargs) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class TestNarratorConstants:
    def test_pinned_model(self) -> None:
        assert NARRATOR_MODEL == "gemini-3.1-flash-tts-preview"

    def test_deep_male_voice_preset(self) -> None:
        assert NARRATOR_VOICE == "Charon"

    def test_style_instruction_is_a_dramatic_posh_british_narrator(self) -> None:
        style = NARRATOR_STYLE.lower()
        assert "posh" in style and "british" in style
        assert "wildlife" in style and "documentary" in style


class TestGeminiTtsNarratorRequestShape:
    def test_happy_path_returns_bytes_that_pass_the_ogg_opus_contract(self) -> None:
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client)

        ogg = narrator.synthesise("In the quiet hours, a fox-like soul stirs.")

        assert ogg, "synthesis must return bytes"
        assert ogg[:4] == b"OggS"
        verify_ogg_opus(ogg)

    def test_request_pins_the_tts_model(self) -> None:
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client)

        narrator.synthesise("A script about a fox.")

        assert len(client.calls) == 1
        assert client.calls[0]["model"] == "gemini-3.1-flash-tts-preview"

    def test_request_asks_for_the_audio_modality(self) -> None:
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client)

        narrator.synthesise("A script about a fox.")

        config = client.calls[0]["config"]
        assert config.response_modalities == ["AUDIO"]

    def test_request_carries_the_deep_male_voice_preset(self) -> None:
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client)

        narrator.synthesise("A script about a fox.")

        config = client.calls[0]["config"]
        voice = config.speech_config.voice_config.prebuilt_voice_config.voice_name
        assert voice == "Charon"

    def test_style_instruction_wraps_the_script(self) -> None:
        """D4: the dramatic posh-British direction surrounds the script, so
        the character comes from the instruction, not just the preset."""
        script = "In the quiet hours, a fox-like soul stirs."
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client)

        narrator.synthesise(script)

        contents = client.calls[0]["contents"]
        assert isinstance(contents, str)
        assert script in contents, "the script must reach the model verbatim"
        assert "posh" in contents.lower() and "british" in contents.lower()
        assert contents.index("posh") < contents.index(script), (
            "the style instruction must lead the wrapped script"
        )

    def test_empty_script_is_refused_before_any_request(self) -> None:
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client)

        with pytest.raises(NarrationError):
            narrator.synthesise("   ")

        assert client.calls == [], "no request may go out for an empty script"


class TestGeminiTtsNarratorFailures:
    def test_client_failure_raises_a_typed_error_and_is_logged(self, caplog) -> None:
        client = FakeGenAiTtsClient(error=RuntimeError("gemini 503"))
        narrator = GeminiTtsNarrator(client=client)

        with (
            caplog.at_level(logging.ERROR, logger="telegram_documentaries.narrator"),
            pytest.raises(NarrationError) as exc_info,
        ):
            narrator.synthesise("A script about a fox.")

        assert "gemini 503" in str(exc_info.value.__cause__) or (
            "gemini 503" in str(exc_info.value)
        ), "the underlying failure must survive for the logs"
        assert any("event=narrator_error" in r.message for r in caplog.records)

    def test_malformed_response_raises_a_typed_error_and_is_logged(
        self, caplog
    ) -> None:
        """A text-only reply is a *typed* boundary failure, not an
        AttributeError from inside the SDK."""
        text_only = SimpleNamespace(
            candidates=[
                SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="hi")]))
            ]
        )
        client = FakeGenAiTtsClient(response=text_only)
        narrator = GeminiTtsNarrator(client=client)

        with (
            caplog.at_level(logging.ERROR, logger="telegram_documentaries.narrator"),
            pytest.raises(NarrationError),
        ):
            narrator.synthesise("A script about a fox.")

        assert any("event=narrator_error" in r.message for r in caplog.records)

    def test_no_partial_bytes_escape_on_a_client_failure(self) -> None:
        """The contract is all-or-nothing: either verified OGG/Opus bytes or
        a typed exception — never half-converted output."""
        client = FakeGenAiTtsClient(error=RuntimeError("gemini 503"))
        narrator = GeminiTtsNarrator(client=client)

        with pytest.raises(NarrationError):
            narrator.synthesise("A script about a fox.")


class _FlakyTtsClient:
    """Fake TTS client: times out once, then returns a valid audio response."""

    def __init__(self, response: object) -> None:
        self.calls: list[dict] = []
        self.models = self
        self._response = response

    def generate_content(self, **kwargs) -> object:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise TimeoutError("gemini tts timed out")
        return self._response


class _AlwaysTimingOutTtsClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.models = self

    def generate_content(self, **kwargs) -> object:
        self.calls.append(kwargs)
        raise TimeoutError("gemini tts timed out")


class TestGeminiTtsNarratorRetries:
    """ROADMAP §7: a transient Gemini TTS timeout is retried with backoff;
    exhaustion surfaces as the existing typed NarrationError for the
    gateway's narrator fallback — never an unhandled exception."""

    def test_transient_timeout_is_retried_then_verified_ogg_returns(self) -> None:
        sleeper = RecordingSleeper()
        client = _FlakyTtsClient(
            _FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client, sleeper=sleeper)

        ogg = narrator.synthesise("In the quiet hours, a fox-like soul stirs.")

        verify_ogg_opus(ogg)  # same verified-bytes contract as a first-try success
        assert len(client.calls) == 2, "one retry after the timeout"
        assert sleeper.delays == [0.5]

    def test_a_first_try_success_sleeps_nothing(self) -> None:
        sleeper = RecordingSleeper()
        client = FakeGenAiTtsClient(
            response=_FakeTtsResponse.one_part(_fake_pcm(), VALID_MIME)
        )
        narrator = GeminiTtsNarrator(client=client, sleeper=sleeper)

        ogg = narrator.synthesise("In the quiet hours, a fox-like soul stirs.")

        assert ogg[:4] == b"OggS"
        assert sleeper.delays == []

    def test_exhaustion_raises_a_typed_error_carrying_the_timeout(self) -> None:
        sleeper = RecordingSleeper()
        client = _AlwaysTimingOutTtsClient()
        narrator = GeminiTtsNarrator(client=client, sleeper=sleeper)

        with pytest.raises(NarrationError) as exc_info:
            narrator.synthesise("A script about a fox.")

        assert isinstance(exc_info.value.__cause__, TimeoutError), (
            "the underlying timeout must survive for the logs and the fallback"
        )
        assert len(client.calls) == 3, "bounded: at most max_attempts calls"

    def test_a_deterministic_error_is_not_retried(self) -> None:
        sleeper = RecordingSleeper()
        attempts = {"n": 0}

        class _Client:
            def __init__(self) -> None:
                self.models = self

            def generate_content(self, **kwargs) -> object:
                attempts["n"] += 1
                raise ValueError("bad request")

        narrator = GeminiTtsNarrator(client=_Client(), sleeper=sleeper)

        with pytest.raises(NarrationError):
            narrator.synthesise("A script about a fox.")

        assert attempts["n"] == 1
        assert sleeper.delays == []

