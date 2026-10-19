"""Scripter agent: turns personality summary into a British wildlife documentary narration."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.logging_utils import get_logger
from app.resilience import call_with_retries

if TYPE_CHECKING:
    from app.models import Dossier

logger = get_logger("scripter")

SCRIPTER_MODEL = "gemini-3.1-flash-lite"

_SCRIPTER_INSTRUCTION = (
    "You are a revered, dramatic British wildlife documentary narrator. "
    "Frame the subject's quirks, bedtime habits, and snacking patterns as funny "
    "animal behaviours, intentionally making it sound like a nature documentary "
    "about humans. Output exactly one paragraph (max 90 words), no markdown "
    "formatting, ready for voice synthesis. Use the personality summary to "
    "weave concrete details into your narration. Return JSON with field 'text'."
)


class Script(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(min_length=1, max_length=600)


class Scripter(Protocol):
    def generate_script(self, dossier: Dossier) -> str: ...


def dossier_to_personality_summary(dossier: Dossier) -> str:
    parts: list[str] = []
    for index, item in enumerate(dossier.answers, start=1):
        parts.append(f"Q{index}: {item.question}\nA{index}: {item.answer}")
    if dossier.animal_rationale:
        parts.append(f"Why this animal fits: {dossier.animal_rationale}")
    if dossier.suggested_animal:
        parts.append(f"Suggested animal: {dossier.suggested_animal}")
    return "\n\n".join(parts)


class AdkScripter:
    """ADK-wired Scripter. A transient Gemini timeout is retried with capped
    backoff (ROADMAP §7); exhaustion re-raises for the gateway's degrade."""

    def __init__(self, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        try:
            from google.adk.agents import LlmAgent
        except Exception as exc:
            raise RuntimeError("google.adk not available") from exc

        self._sleeper = sleeper
        self._agent = LlmAgent(
            name="scripter",
            model=SCRIPTER_MODEL,
            instruction=_SCRIPTER_INSTRUCTION,
            output_schema=Script,
        )

    def generate_script(self, dossier: Dossier) -> str:
        def _generate() -> str:
            import asyncio

            from app.adk_utils import extract_json

            summary = dossier_to_personality_summary(dossier)
            prompt = (
                f"Personality summary:\n\n{summary}\n\n"
                "Generate the documentary narration."
            )

            async def _run() -> str:
                from google.adk.runners import InMemoryRunner
                from google.genai import types

                runner = InMemoryRunner(agent=self._agent)
                session = await runner.session_service.create_session(
                    app_name=runner.app_name, user_id="scripter"
                )
                raw = None
                async for event in runner.run_async(
                    user_id="scripter",
                    session_id=session.id,
                    new_message=types.Content(
                        role="user", parts=[types.Part.from_text(text=prompt)]
                    ),
                ):
                    if event.is_final_response() and event.content is not None:
                        raw = (
                            event.content.parts[0].text if event.content.parts else ""
                        )
                        break
                if not raw:
                    raise RuntimeError("scripter returned no content")
                script = Script.model_validate_json(extract_json(raw))
                return script.text

            try:
                return asyncio.run(_run())
            except Exception:
                logger.exception("event=scripter_error")
                raise

        return call_with_retries(_generate, sleeper=self._sleeper)
