"""Narrator (Phase 6): finished script -> Gemini TTS -> verified OGG/Opus.

**Not an ADK agent** (SPECS/TECH.md): the Scripter's output is routed
straight to Gemini TTS through the plain ``google-genai`` client, with no
intermediate text hop and no runner. This module owns the audio boundary
contracts:

  * ``OutboundVoice``     — the typed voice-note request (OGG/Opus only;
    Telegram voice notes require OGG/Opus, never MP3 — a hard requirement).
  * ``parse_tts_response`` — parses the untrusted TTS response at the edge:
    exactly one inline audio part, mime ``audio/l16; rate=24000;
    channels=1``, non-empty data. Any other shape raises a typed
    ``TtsResponseError`` — never a bare ``KeyError``/``IndexError``.
  * ``verify_ogg_opus``   — magic-byte verification (``OggS`` at offset 0,
    ``OpusHead`` inside the first page) run on our own output before
    anything is handed to Telegram (decision D5).
  * ``pcm_to_ogg_opus``   — the PCM -> OGG/Opus encoder (decision D1,
    ``soundfile``/libsndfile), because the TTS model returns raw
    little-endian 16-bit PCM, not an Ogg container.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.logging_utils import get_logger

logger = get_logger("narrator")

#: The TTS model (SPECS/TECH.md, model table) — pinned, one place to change.
NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"

#: Prebuilt deep-male voice preset (decision D4). The dramatic posh-British
#: character comes from the style instruction; the preset supplies the timbre.
NARRATOR_VOICE = "Charon"

#: The TTS model returns raw PCM at 24 kHz, mono, little-endian signed
#: 16-bit — pinned at the edge and asserted by the response parser.
NARRATOR_SAMPLE_RATE = 24000

#: Style instruction wrapping the script (decision D4): dramatic, posh
#: British wildlife-documentary narrator.
NARRATOR_STYLE = (
    "Speak in a deep, posh British accent with the dramatic, unhurried "
    "gravitas of a classic wildlife-documentary narrator. Every word lands "
    "with theatrical reverence. Narrate exactly the script below — no "
    "asides, no explanations, no stage directions."
)


class NarrationError(RuntimeError):
    """Typed failure at the Narrator's audio boundary."""


class TtsResponseError(NarrationError):
    """The TTS response did not carry the contracted audio payload."""


class AudioFormatError(NarrationError):
    """Bytes failed the OGG/Opus format contract (or could not be encoded)."""


class OutboundVoice(BaseModel):
    """A voice note the gateway will send. Never a raw dict (TECH rule).

    Constrained to OGG/Opus: Telegram renders ``sendVoice`` with
    ``audio/ogg`` as a playable voice note; MP3 is not a valid voice note.
    """

    model_config = ConfigDict(extra="forbid")

    chat_id: int
    audio_bytes: bytes
    filename: str = "voice.ogg"
    mime_type: Literal["audio/ogg"] = "audio/ogg"


class Narrator(Protocol):
    """What the gateway needs from the voice stage — narrow for testing."""

    def synthesise(self, script: str) -> bytes: ...


def verify_ogg_opus(data: bytes) -> None:
    """Raise ``AudioFormatError`` unless ``data`` is a valid Ogg/Opus stream.

    Checks the Ogg capture pattern at offset 0 and an ``OpusHead`` marker
    inside the *first* page (whose bounds are computed from the page
    header). Everything else — empty bytes, raw PCM, MP3, truncated
    headers — is rejected before it can reach Telegram (decision D5).
    """
    if len(data) < 4 or data[:4] != b"OggS":
        raise AudioFormatError(
            f"not an Ogg stream: starts with {bytes(data[:4])!r}"
        )
    if len(data) < 27:
        # Capture pattern only, or a partial 27-byte page header.
        raise AudioFormatError(f"truncated Ogg page header ({len(data)} bytes)")

    segment_count = data[26]
    if len(data) < 27 + segment_count:
        raise AudioFormatError("truncated Ogg segment table")

    page_end = 27 + segment_count + sum(data[27 : 27 + segment_count])
    if page_end > len(data):
        raise AudioFormatError("truncated Ogg page body")

    if b"OpusHead" not in data[:page_end]:
        raise AudioFormatError("no OpusHead marker in the first Ogg page")


def _is_expected_tts_mime(mime: object) -> bool:
    """True for ``audio/l16`` declaring ``rate=24000`` and ``channels=1``.

    Parameter order and casing are ignored (``audio/l16; channels=1;
    rate=24000`` is equivalent); a missing or wrong parameter is not.
    """
    if not isinstance(mime, str):
        return False
    pieces = [piece.strip().lower() for piece in mime.split(";")]
    if pieces[0] != "audio/l16":
        return False
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        key, sep, value = piece.partition("=")
        if sep:
            params[key.strip()] = value.strip()
    return params.get("rate") == "24000" and params.get("channels") == "1"


def parse_tts_response(response: object) -> bytes:
    """Pull the contracted PCM payload out of an untrusted TTS response.

    The SDK response shape differs across versions and the model may reply
    with text only, several parts, the wrong mime, or empty data — each is
    a typed ``TtsResponseError`` at this boundary, never an ``AttributeError``
    or ``KeyError`` leaking from inside the SDK.
    """
    audio_parts: list[object] = []
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                audio_parts.append(inline)

    if not audio_parts:
        raise TtsResponseError("TTS response carried no audio part")
    if len(audio_parts) > 1:
        raise TtsResponseError(
            f"expected exactly one audio part, got {len(audio_parts)}"
        )

    inline = audio_parts[0]
    data = getattr(inline, "data", None)
    if not isinstance(data, bytes) or not data:
        raise TtsResponseError("TTS audio part carried empty data")

    mime = getattr(inline, "mime_type", None)
    if not _is_expected_tts_mime(mime):
        raise TtsResponseError(f"unexpected TTS mime type: {mime!r}")

    return data


def pcm_to_ogg_opus(pcm: bytes, sample_rate: int = NARRATOR_SAMPLE_RATE) -> bytes:
    """Encode raw 16-bit little-endian mono PCM as an OGG/Opus stream.

    The TTS model returns ``audio/l16`` bytes — not an Ogg container — so
    conversion is mandatory (SPECS/TECH.md: "Audio format is OGG/Opus, not
    MP3"). Encoded entirely in memory (no temp files) via
    ``soundfile``/libsndfile (decision D1), then magic-byte verified before
    returning, so unverified bytes can never reach Telegram (decision D5).

    ``numpy``/``soundfile`` import inside the function on purpose: a broken
    encoder install must degrade to a logged narrator failure, not kill the
    gateway at import time (decision D8 — one stage never takes down the loop).
    """
    import io

    import numpy as np
    import soundfile as sf  # type: ignore[import-untyped]  # no py.typed/stubs

    if not pcm or len(pcm) % 2 != 0:
        raise AudioFormatError(
            f"PCM payload must be non-empty and 16-bit aligned, got {len(pcm)} bytes"
        )

    # s16 samples are normalised to [-1, 1] floats for the encoder.
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

    buffer = io.BytesIO()
    try:
        sf.write(buffer, samples, sample_rate, format="OGG", subtype="OPUS")
    except sf.LibsndfileError as exc:
        raise AudioFormatError(f"OGG/Opus encoding failed (libsndfile): {exc}") from exc
    except Exception as exc:  # soundfile/libsndfile failure
        raise AudioFormatError(f"OGG/Opus encoding failed: {exc}") from exc

    ogg = buffer.getvalue()
    verify_ogg_opus(ogg)
    return ogg


class GeminiTtsNarrator:
    """Concrete Narrator backed by the plain ``google-genai`` client.

    Decision D3: no ``LlmAgent``, no runner — the Scripter's finished script
    goes straight to Gemini TTS, exactly like ``GenAiConverter``'s direct
    client style. The injected ``client`` exists purely for tests (faked at
    the module boundary); production wiring constructs the real client.
    """

    def __init__(self, client: Any = None) -> None:
        if client is None:
            from google import genai

            client = genai.Client()
        self._client = client

    def synthesise(self, script: str) -> bytes:
        """Render ``script`` to verified OGG/Opus bytes, or raise typed.

        All-or-nothing (decision D5): either bytes that pass
        ``verify_ogg_opus``, or a ``NarrationError`` — never partial output.
        Failures are logged loudly here and re-raised for the gateway to
        degrade gracefully with a friendly reply (SPECS/TECH.md error policy).
        """
        if not script or not script.strip():
            raise NarrationError("refusing to synthesise an empty script")

        contents = f"{NARRATOR_STYLE}\n\n{script}"

        try:
            from google.genai import types

            response = self._client.models.generate_content(
                model=NARRATOR_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=NARRATOR_VOICE
                            )
                        )
                    ),
                ),
            )

            pcm = parse_tts_response(response)
            logger.info(
                "event=narration_synthesised model=%s script_words=%d pcm_bytes=%d",
                NARRATOR_MODEL,
                len(script.split()),
                len(pcm),
            )

            ogg = pcm_to_ogg_opus(pcm)
            frames = len(pcm) // 2
            duration = frames / NARRATOR_SAMPLE_RATE
            logger.info(
                "event=narration_converted ogg_bytes=%d duration=%.2f",
                len(ogg),
                duration,
            )
            return ogg
        except NarrationError:
            # Typed boundary failure (bad response, bad format): already
            # precise — log loudly with context and hand it up unchanged.
            logger.exception(
                "event=narrator_error model=%s script_words=%d",
                NARRATOR_MODEL,
                len(script.split()),
            )
            raise
        except Exception as exc:
            # Client/network/SDK failure: wrap in the typed error so no
            # untyped exception can escape into the gateway's flow.
            logger.exception(
                "event=narrator_error model=%s script_words=%d",
                NARRATOR_MODEL,
                len(script.split()),
            )
            raise NarrationError("Gemini TTS synthesis failed") from exc

