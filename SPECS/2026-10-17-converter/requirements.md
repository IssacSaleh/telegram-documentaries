# Requirements — Converter (ROADMAP Phase 4)

## Context

The Bouncer admits humans, the Interviewer guides an adaptive Q&A and produces a
suggested animal plus the per-chat dossier (personality/answers). The next stage
is **Phase 4 — the Converter**: fuse the original photo, the interview dossier,
and the suggested animal into a hybrid animal portrait, returning it directly to
Telegram (no intermediate text hop).

ROADMAP §4 acceptance: *"a portrait is returned to the chat after the interview
completes; the suggested animal demonstrably influences the output; it is a
Pydantic-validated model response; failure degrades gracefully without killing
the session."*

## Scope

**In scope**

- A `Converter` that takes (original photo bytes/mime, dossier summary text,
  suggested animal) and returns image bytes + metadata via Pydantic validation.
- Auto-trigger when the interview reaches `SessionPhase.DONE` in the gateway.
- One multimodal request to the image model that includes all three inputs
  together.
- Store original photo bytes at admission time for later use.
- Send the generated portrait directly to Telegram (`sendPhoto`), with a
  caption naming the animal.
- Graceful degradation: missing inputs or conversion failure log loudly and
  fall back to the "portrait pending" tease without crashing the session.
- Offline unit tests + extension of gateway routing tests; no live tests
  required for this pass (the user opted out).

**Out of scope**

- TTS/narration (Phases 5–6), video, editing tools. No re-upload to external
  storage. Keep everything in-memory.

## Decisions

| # | Decision | Rationale |
| --- | --- | --- |
| C1 | All three inputs go in one multimodal call: original image + dossier summary + suggested animal name. | Matches "no intermediate text hop" and ensures the animal/personality influence the generation. |
| C2 | Store original photo bytes/mime/file_id on the session at admission time (when admitted). | Photo bytes are needed later at DONE; fetching again may fail; storing once is simple and in-memory (SPECS/TECH.md). |
| C3 | On success, send the image directly to Telegram and do not send a text reply. | The portrait is the reply per the phase's deliverable. |
| C4 | On missing inputs or conversion failure, degrade by sending the existing "portrait pending" tease (or a friendly error) and do not crash; session stays in DONE. | Matches error-policy "never leave the user in silence" while respecting the planned fallback for missing inputs. |
| C5 | The model is `gemini-3.1-flash-image` (SPECS/TECH.md). | Pinned to the project's model table. |

## Contracts

`ConverterResult` (Pydantic):
- `suggested_animal: str | None`
- `image_mime_type: str = "image/png"`
- `image_base64: str | None` (optional)
- `image_bytes: bytes | None` (primary delivery)

`Converter` protocol: `generate(original_photo_bytes, original_photo_mime, dossier_text, suggested_animal) -> ConverterResult`

`OutboundPhoto` model for sending: `chat_id`, `image_bytes`, `caption`, `filename`, `mime_type`.

## Behaviour

- When a photo is admitted (Bouncer yes), fetch the largest photo bytes and store in `Session.original_photo_bytes`, `original_photo_mime`, `original_photo_file_id`.
- When `session.phase == DONE` and a text arrives, if a wired converter exists and all inputs present (original photo + dossier + suggested_animal), call `converter.generate(...)` with all three inputs, send `OutboundPhoto`, return None (no text). Otherwise fall back to the portrait pending tease.
- Dossier is flattened into a plain-text summary (Q/A pairs + rationale) for the prompt; never log raw answers verbatim.
- Failures are logged with `event=converter_error`/`converter_skipped` and handled gracefully.

## Acceptance criteria

1. After the interview completes (DONE), the gateway triggers conversion when wired and sends the portrait directly to Telegram.
2. The suggested animal demonstrably influences the output (the prompt includes it and the result carries it).
3. The response is validated by Pydantic (`ConverterResult`).
4. Failure degrades gracefully without killing the session.
5. All three inputs (original photo, dossier summary, suggested animal) are passed in one multimodal request.
