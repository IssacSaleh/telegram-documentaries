"""Unit tests for the Converter (ROADMAP Phase 4).

The Converter fuses three inputs — the original photo, the interview
dossier/personality summary, and the suggested animal — into one hybrid
portrait via a single multimodal request (SPECS/2026-10-17-converter). These
tests cover the model constants, the dossier summary builder, defensive
response parsing, and the Pydantic-validated result.
"""

from __future__ import annotations

import pytest

from app.converter import (
    CONVERTER_MODEL,
    ConverterResult,
    _extract_image,
    dossier_to_summary,
)
from app.models import Dossier, DossierAnswer


class TestConverterConstants:
    """The pinned model is part of the contract (SPECS/TECH.md)."""

    def test_pinned_model_id(self) -> None:
        assert CONVERTER_MODEL == "gemini-3.1-flash-image"


class TestDossierSummary:
    """The personality summary must carry every answer and the rationale."""

    def test_includes_every_answer(self) -> None:
        dossier = Dossier(
            answers=[
                DossierAnswer(question="What do you do?", answer="I teach"),
                DossierAnswer(question="Where do you live?", answer="By the sea"),
            ]
        )

        summary = dossier_to_summary(dossier)

        assert "What do you do?" in summary
        assert "I teach" in summary
        assert "Where do you live?" in summary
        assert "By the sea" in summary

    def test_includes_animal_rationale(self) -> None:
        dossier = Dossier(
            answers=[DossierAnswer(question="Q", answer="A")],
            suggested_animal="Arctic Fox",
            animal_rationale="solitary and clever",
        )

        summary = dossier_to_summary(dossier)

        assert "solitary and clever" in summary

    def test_empty_dossier_is_empty_string(self) -> None:
        assert dossier_to_summary(Dossier()) == ""


class TestConverterResult:
    """The result is a validated model carrying the animal and image data."""

    def test_carries_animal_and_image(self) -> None:
        result = ConverterResult(
            suggested_animal="Red Fox",
            image_bytes=b"\x89PNG",
            image_mime_type="image/png",
        )

        assert result.suggested_animal == "Red Fox"
        assert result.image_bytes == b"\x89PNG"
        assert result.image_mime_type == "image/png"

    def test_defaults_to_png(self) -> None:
        assert ConverterResult().image_mime_type == "image/png"

    def test_ignores_unknown_fields(self) -> None:
        result = ConverterResult.model_validate(
            {"image_bytes": b"x", "unexpected": "value"}
        )

        assert result.image_bytes == b"x"


class _FakeInlineData:
    def __init__(self, data: bytes | None, mime_type: str | None) -> None:
        self.data = data
        self.mime_type = mime_type


class _FakePart:
    def __init__(self, inline_data: object | None = None) -> None:
        self.inline_data = inline_data


class _FakeContent:
    def __init__(self, parts: list) -> None:
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts: list) -> None:
        self.content = _FakeContent(parts)


class _FakeResponse:
    def __init__(self, candidates: list | None) -> None:
        self.candidates = candidates


class TestExtractImage:
    """Response parsing is defensive: the SDK shape varies by version."""

    def test_extracts_first_inline_image(self) -> None:
        response = _FakeResponse(
            [
                _FakeCandidate(
                    [
                        _FakePart(),
                        _FakePart(_FakeInlineData(b"IMGDATA", "image/png")),
                    ]
                )
            ]
        )

        data, mime = _extract_image(response)

        assert data == b"IMGDATA"
        assert mime == "image/png"

    def test_defaults_mime_when_missing(self) -> None:
        response = _FakeResponse(
            [_FakeCandidate([_FakePart(_FakeInlineData(b"X", None))])]
        )

        data, mime = _extract_image(response)

        assert data == b"X"
        assert mime == "image/png"

    def test_no_candidates_returns_none(self) -> None:
        assert _extract_image(_FakeResponse(None)) == (None, "image/png")
        assert _extract_image(_FakeResponse([])) == (None, "image/png")

    def test_text_only_returns_none(self) -> None:
        response = _FakeResponse([_FakeCandidate([_FakePart()])])

        assert _extract_image(response) == (None, "image/png")

    def test_empty_image_data_is_skipped(self) -> None:
        response = _FakeResponse(
            [_FakeCandidate([_FakePart(_FakeInlineData(b"", "image/png"))])]
        )

        assert _extract_image(response) == (None, "image/png")


class TestGenAiConverter:
    """The concrete converter wires the genai SDK; behavior via monkeypatch."""

    def test_generate_sends_all_three_inputs_and_returns_image(self) -> None:
        from app.converter import GenAiConverter

        captured: dict = {}

        class _Models:
            def generate_content(self, *, model, contents):
                captured["model"] = model
                captured["contents"] = contents
                return _FakeResponse(
                    [
                        _FakeCandidate(
                            [_FakePart(_FakeInlineData(b"PORTRAIT", "image/png"))]
                        )
                    ]
                )

        class _Client:
            models = _Models()

        # Bypass the real genai.Client() construction (no API key needed).
        converter = GenAiConverter.__new__(GenAiConverter)
        converter._client = _Client()

        result = converter.generate(
            original_photo_bytes=b"PHOTOBYTES",
            original_photo_mime="image/jpeg",
            dossier_text="Q1: hi\nA1: there",
            suggested_animal="Red Fox",
        )

        assert captured["model"] == CONVERTER_MODEL
        assert result.image_bytes == b"PORTRAIT"
        assert result.suggested_animal == "Red Fox"
        # All three inputs reached the request: photo bytes + a prompt that
        # names the animal and embeds the dossier summary.
        parts = captured["contents"][0].parts
        assert any(getattr(p, "inline_data", None) for p in parts)
        prompt = " ".join(
            p.text for p in parts if getattr(p, "text", None) is not None
        )
        assert "Red Fox" in prompt
        assert "there" in prompt

    def test_generate_raises_when_no_image_returned(self) -> None:
        from app.converter import GenAiConverter

        class _Models:
            def generate_content(self, *, model, contents):
                return _FakeResponse([_FakeCandidate([_FakePart()])])

        class _Client:
            models = _Models()

        converter = GenAiConverter.__new__(GenAiConverter)
        converter._client = _Client()

        with pytest.raises(RuntimeError, match="no image"):
            converter.generate(
                original_photo_bytes=b"P",
                original_photo_mime="image/jpeg",
                dossier_text="d",
                suggested_animal="Red Fox",
            )
