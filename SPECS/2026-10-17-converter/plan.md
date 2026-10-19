# Plan — Converter (ROADMAP Phase 4)

Red/Green TDD. Run `./scripts/test` + `./scripts/hooks` after each group. All changes on feature branch `feature/2026-10-17-converter`.

## 1. Models and session fields
- [ ] 1.1 Red: tests for `OutboundPhoto` (validation, extra forbid, defaults).
- [ ] 1.2 Red: tests asserting `Session` has original photo fields (`original_photo_bytes`, `original_photo_mime`, `original_photo_file_id`) and schema/version unchanged by shape addition (v2 remains).
- [ ] 1.3 Green: add `OutboundPhoto` to `app/models.py`; add fields to `Session` in `app/state.py`.
- [ ] 1.4 Run `./scripts/test` + `./scripts/hooks`.

## 2. Converter module
- [ ] 2.1 Red: unit tests for `ConverterResult`, `dossier_to_summary`/flattening, `_extract_image` parsing, and `GenAiConverter` wiring (monkeypatch genai client).
- [ ] 2.2 Green: implement `app/converter.py` with `CONVERTER_MODEL="gemini-3.1-flash-image"`, `Converter` protocol, `GenAiConverter`, defensive response parsing.
- [ ] 2.3 Run `./scripts/test` + `./scripts/hooks`.

## 3. Store original photo on admission
- [ ] 3.1 Red: gateway test that admitted photo stores bytes/mime/file_id on session (using FakePhotoFetcher).
- [ ] 3.2 Red: gateway test that photo fetch failure on store is swallowed and admission still proceeds.
- [ ] 3.3 Green: in gateway admission path, fetch largest photo and stash on session; log on failure.
- [ ] 3.4 Run `./scripts/test` + `./scripts/hooks`.

## 4. SendPhoto support
- [ ] 4.1 Red: `TelegramSender.send_photo` tests (ok true/false, multipart content).
- [ ] 4.2 Green: add `send_photo` to `MessageSender` protocol and implement in `TelegramSender` (multipart, no raise_for_status, secret-safe logging).
- [ ] 4.3 Run `./scripts/test` + `./scripts/hooks`.

## 5. Gateway routing for DONE → Converter
- [ ] 5.1 Red: when DONE and converter wired, sends photo directly, returns None; caption includes animal; all three inputs passed.
- [ ] 5.2 Red: converter failure → graceful reply, session unchanged.
- [ ] 5.3 Red: missing original photo/dossier → falls back to portrait tease.
- [ ] 5.4 Green: wire converter param through `poll_once`, `handle_update`, `run()`; implement `_run_converter`; call on DONE.
- [ ] 5.5 Run `./scripts/test` + `./scripts/hooks`.

## 6. Integration & verification
- [ ] 6.1 All unit tests green (including new converter tests).
- [ ] 6.2 `./scripts/hooks` clean.
- [ ] 6.3 No live tests required (user opted out). Offline suite only.
