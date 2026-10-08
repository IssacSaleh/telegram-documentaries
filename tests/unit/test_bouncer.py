"""The Bouncer: vision gate. ROADMAP Phase 2.

"A portrait passes through to the Interviewer; a non-human image gets a
rejection reply and state resets; rejects are logged."

These are unit tests — the classifier and photo fetcher are test doubles, so
no Gemini or Telegram I/O happens here. See tests/integration/ for the real
positive (person) and negative (object/landscape) verdicts.
"""

from __future__ import annotations

import logging

from app.bouncer import (
    BOUNCER_INSTRUCTION,
    BOUNCER_MODEL,
    REJECTION_REPLY,
    AdkHumanPresenceClassifier,
    BouncerDecision,
    bounce,
)
from app.models import BouncerVerdict, TelegramUpdate

# ---------------------------------------------------------------------------
# helpers / doubles
# ---------------------------------------------------------------------------

PHOTO_UPDATE = {
    "update_id": 42,
    "message": {
        "message_id": 7,
        "date": 1759750000,
        "chat": {"id": 6682234839, "type": "private"},
        "photo": [
            {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
            {"file_id": "largest", "file_unique_id": "u4", "width": 900, "height": 900},
        ],
    },
}


def photo_update(chat_id: int = 6682234839) -> TelegramUpdate:
    payload = {
        "update_id": 42,
        "message": {
            "message_id": 7,
            "date": 1759750000,
            "chat": {"id": chat_id, "type": "private"},
            "photo": PHOTO_UPDATE["message"]["photo"],
        },
    }
    return TelegramUpdate.model_validate(payload)


class FakeFetcher:
    """Returns canned image bytes; records which file_id was requested."""

    def __init__(self, data: bytes = b"\xff\xd8fakejpeg", mime: str = "image/jpeg") -> None:
        self.data = data
        self.mime = mime
        self.requested: list[str] = []

    def fetch(self, file_id: str) -> tuple[bytes, str]:
        self.requested.append(file_id)
        return self.data, self.mime


class FailingFetcher:
    def fetch(self, file_id: str) -> tuple[bytes, str]:
        raise RuntimeError("telegram getFile exploded")


class FakeClassifier:
    def __init__(self, verdict: BouncerVerdict) -> None:
        self.verdict = verdict
        self.received: list[tuple[bytes, str]] = []

    def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
        self.received.append((image_bytes, mime_type))
        return self.verdict


def human() -> BouncerVerdict:
    return BouncerVerdict(human_present=True, confidence=0.98, reason="clear portrait")


def no_human() -> BouncerVerdict:
    return BouncerVerdict(human_present=False, confidence=0.97, reason="a mountain range")


# ---------------------------------------------------------------------------
# rejection copy
# ---------------------------------------------------------------------------


class TestRejectionCopy:
    def test_rejection_says_the_submission_must_contain_a_human(self) -> None:
        assert "human" in REJECTION_REPLY.lower()

    def test_rejection_is_more_than_a_flat_no(self) -> None:
        # "humorous, cheeky" — must be a real sentence, not "no".
        assert len(REJECTION_REPLY.split()) >= 6

    def test_rejection_has_no_leading_or_trailing_whitespace(self) -> None:
        assert REJECTION_REPLY == REJECTION_REPLY.strip()


# ---------------------------------------------------------------------------
# negative test: object / landscape -> reject + reset + log
# ---------------------------------------------------------------------------


class TestNegativePath:
    def test_non_human_image_is_rejected(self) -> None:
        decision = bounce(
            photo_update(),
            fetcher=FakeFetcher(),
            classifier=FakeClassifier(no_human()),
        )

        assert decision.verdict.human_present is False
        assert decision.reply is not None
        assert decision.reply.text == REJECTION_REPLY
        assert decision.reply.chat_id == 6682234839

    def test_non_human_image_resets_ephemeral_state(self) -> None:
        from app.state import SessionPhase, get_store

        store = get_store()
        # pre-existing session in a non-initial phase, as if mid-interview
        store.get_or_create(6682234839).phase = SessionPhase.INTERVIEWING

        bounce(photo_update(), fetcher=FakeFetcher(), classifier=FakeClassifier(no_human()))

        # reset purges the chat entirely -> next run starts clean
        assert 6682234839 not in store.chat_ids

    def test_non_human_rejection_is_logged(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="telegram_documentaries.bouncer"):
            bounce(
                photo_update(),
                fetcher=FakeFetcher(),
                classifier=FakeClassifier(no_human()),
            )

        assert any(
            "event=rejected" in r.message and r.levelno >= logging.INFO
            for r in caplog.records
        ), "rejection must be logged at INFO (ROADMAP: 'rejects are logged')"

    def test_non_human_sends_bytes_to_the_classifier(self) -> None:
        fetcher = FakeFetcher(data=b"IMGDATA", mime="image/png")
        classifier = FakeClassifier(no_human())

        bounce(photo_update(), fetcher=fetcher, classifier=classifier)

        assert fetcher.requested == ["largest"]  # biggest size, not the first
        assert classifier.received == [(b"IMGDATA", "image/png")]


# ---------------------------------------------------------------------------
# positive test: person -> continue confirmation flow unchanged
# ---------------------------------------------------------------------------


class TestPositivePath:
    def test_human_image_is_not_rejected(self) -> None:
        decision = bounce(
            photo_update(),
            fetcher=FakeFetcher(),
            classifier=FakeClassifier(human()),
        )

        assert decision.verdict.human_present is True
        assert decision.reply is None, "a pass must not send a rejection"

    def test_human_image_leaves_state_intact(self) -> None:
        from app.state import SessionPhase, get_store

        store = get_store()
        store.get_or_create(6682234839).phase = SessionPhase.AWAITING_PHOTO

        bounce(photo_update(), fetcher=FakeFetcher(), classifier=FakeClassifier(human()))

        session = store.get(6682234839)
        assert session is not None, "passing must NOT purge the session"
        assert session.phase is SessionPhase.AWAITING_PHOTO

    def test_human_pass_is_logged(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="telegram_documentaries.bouncer"):
            bounce(photo_update(), fetcher=FakeFetcher(), classifier=FakeClassifier(human()))

        assert any("event=admitted" in r.message for r in caplog.records)

    def test_pass_returns_none_so_gateway_flow_continues_unchanged(self) -> None:
        decision = bounce(
            photo_update(),
            fetcher=FakeFetcher(),
            classifier=FakeClassifier(human()),
        )
        assert decision.reply is None
        assert decision.admitted is True


# ---------------------------------------------------------------------------
# cross-chat isolation (MISSION non-negotiable)
# ---------------------------------------------------------------------------


class TestCrossChatIsolation:
    def test_rejecting_one_chat_does_not_reset_another(self) -> None:
        from app.state import get_store

        store = get_store()
        other = 111111111
        store.get_or_create(other)

        bounce(photo_update(chat_id=6682234839),
               fetcher=FakeFetcher(),
               classifier=FakeClassifier(no_human()))

        assert other in store.chat_ids, "another user's session must survive"
        assert 6682234839 not in store.chat_ids

    def test_rejection_targets_its_own_chat_only(self) -> None:
        decision = bounce(
            photo_update(chat_id=4242),
            fetcher=FakeFetcher(),
            classifier=FakeClassifier(no_human()),
        )
        assert decision.reply is not None
        assert decision.reply.chat_id == 4242


# ---------------------------------------------------------------------------
# failure modes — never raise into the user's flow (TECH error policy)
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_download_failure_does_not_raise(self) -> None:
        decision = bounce(
            photo_update(),
            fetcher=FailingFetcher(),
            classifier=FakeClassifier(human()),
        )
        assert decision.reply is None
        assert decision.error is True

    def test_download_failure_is_logged(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="telegram_documentaries.bouncer"):
            bounce(photo_update(), fetcher=FailingFetcher(),
                   classifier=FakeClassifier(human()))
        assert any("event=photo_fetch_failed" in r.message for r in caplog.records)

    def test_classifier_failure_does_not_raise(self) -> None:
        class ExplodingClassifier:
            def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
                raise RuntimeError("gemini 500")

        decision = bounce(photo_update(), fetcher=FakeFetcher(),
                          classifier=ExplodingClassifier())
        assert decision.error is True

    def test_classifier_failure_is_logged_and_does_not_reject_the_user(
        self, caplog
    ) -> None:
        class ExplodingClassifier:
            def classify(self, image_bytes: bytes, mime_type: str) -> BouncerVerdict:
                raise RuntimeError("gemini 500")

        with caplog.at_level(logging.ERROR, logger="telegram_documentaries.bouncer"):
            decision = bounce(photo_update(), fetcher=FakeFetcher(),
                              classifier=ExplodingClassifier())

        # degrade gracefully: no rejection, no crash, error flagged
        assert decision.reply is None
        assert decision.error is True
        # ...and the failure is loud, not silent (SPECS/TECH.md error policy)
        assert any(
            "event=classification_failed" in r.message for r in caplog.records
        )

    def test_photo_update_with_empty_photo_array_is_handled(self) -> None:
        update = TelegramUpdate.model_validate(
            {"update_id": 1, "message": {"message_id": 1, "date": 1,
             "chat": {"id": 5, "type": "private"}, "photo": []}}
        )
        decision = bounce(update, fetcher=FakeFetcher(),
                          classifier=FakeClassifier(human()))
        assert decision.error is True
        assert decision.reply is None


# ---------------------------------------------------------------------------
# decision shape
# ---------------------------------------------------------------------------


class TestDecisionShape:
    def test_decision_is_a_typed_model_not_a_raw_dict(self) -> None:
        decision = bounce(photo_update(), fetcher=FakeFetcher(),
                          classifier=FakeClassifier(human()))
        assert isinstance(decision, BouncerDecision)

    def test_admitted_flag_reflects_the_verdict(self) -> None:
        yes = bounce(photo_update(), fetcher=FakeFetcher(),
                     classifier=FakeClassifier(human()))
        no = bounce(photo_update(), fetcher=FakeFetcher(),
                    classifier=FakeClassifier(no_human()))
        assert yes.admitted is True and no.admitted is False


class TestAdkWiring:
    """The Google ADK agent must be wired exactly as SPECS/TECH.md pins it.

    Building `AdkHumanPresenceClassifier` constructs the `LlmAgent` but makes
    no network call — Gemini is only reached inside `classify()`. So this
    asserts the wiring offline, while the live tests in `tests/integration/`
    prove the very same agent really classifies a person and a landscape.
    """

    def test_agent_is_wired_with_the_pinned_model_and_structured_output(
        self,
    ) -> None:
        agent = AdkHumanPresenceClassifier()._agent

        assert agent.name == "bouncer"
        # SPECS/TECH.md: vision gate model is `gemini-3.1-flash-lite`.
        assert BOUNCER_MODEL == "gemini-3.1-flash-lite"
        assert agent.model == BOUNCER_MODEL
        # Structured output, so a verdict can never cross the boundary raw.
        assert agent.output_schema is BouncerVerdict

    def test_instruction_limits_the_verdict_to_human_presence(self) -> None:
        instruction = BOUNCER_INSTRUCTION.lower()

        assert "human" in instruction
        assert "face" in instruction
        assert "body" in instruction
        # Classification only — no narration, no TTS, no video (MISSION.md).
        assert "video" not in instruction


class TestAdkResponseParsing:
    """How the ADK agent's response becomes a typed `BouncerVerdict`.

    Gemini itself is only reached in `tests/integration/`, but everything
    that runs AFTER the model answers must be proven offline: prose around
    the JSON, and streamed fragments before it, must not break the verdict —
    otherwise a photo would silently fall into the "degraded" path instead
    of being admitted or rejected.
    """

    @staticmethod
    def _event(text: str, *, final: bool):
        from google.genai import types

        class _Event:
            def __init__(self) -> None:
                self.content = types.Content(
                    role="model", parts=[types.Part.from_text(text=text)]
                )

            def is_final_response(self) -> bool:
                return final

        return _Event()

    @staticmethod
    def _classify_with(monkeypatch, events) -> BouncerVerdict:
        """Run the real classifier with the ADK runner stubbed out."""
        from google.adk import runners

        class _Runner:
            def __init__(self, agent, app_name) -> None:
                self.session_service = self

            async def create_session(self, **kwargs) -> None:
                return None

            async def run_async(self, **kwargs):
                for event in events:
                    yield event

        monkeypatch.setattr(runners, "InMemoryRunner", _Runner)
        return AdkHumanPresenceClassifier().classify(b"\xff\xd8fake", "image/jpeg")

    def test_a_plain_json_response_becomes_a_verdict(self, monkeypatch) -> None:
        verdict = self._classify_with(
            monkeypatch,
            [
                self._event(
                    '{"human_present": false, "confidence": 0.95, '
                    '"reason": "a mountain range"}',
                    final=True,
                )
            ],
        )

        assert verdict.human_present is False
        assert verdict.confidence == 0.95
        assert verdict.reason == "a mountain range"

    def test_prose_around_the_json_and_streamed_fragments_are_tolerated(
        self, monkeypatch
    ) -> None:
        """The final response wins, and the object is lifted out of any
        narration — an earlier partial fragment must not be concatenated."""
        verdict = self._classify_with(
            monkeypatch,
            [
                self._event('{"human_present": tru', final=False),
                self._event(
                    'Here is my verdict: {"human_present": true, '
                    '"confidence": 0.97, "reason": "a clear portrait"} — '
                    "a human is present.",
                    final=True,
                ),
            ],
        )

        assert verdict.human_present is True
        assert verdict.confidence == 0.97
        assert verdict.reason == "a clear portrait"
