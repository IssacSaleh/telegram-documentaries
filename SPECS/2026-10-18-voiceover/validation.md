# Validation — Voice-over stage / Narrator (ROADMAP Phase 6)

Every box must be checked with **evidence** (command output, test name, log
line) — "should work" is not a check. The verifier checks these in order and
records what was actually verified vs. not verifiable in this environment.

## Automated (must be green)

- [x] `./scripts/test` passes — full suite, offline default (unit tests make
      no network calls; live tests skipped without `RUN_LIVE_TESTS=1`).
      **Evidence:** `278 passed, 8 skipped, 1 warning in 7.11s`; the 8 skips
      are exactly the live tests (3 Bouncer + 3 Interviewer + 1 Converter +
      1 Narrator), each gated on `RUN_LIVE_TESTS=1`.
- [x] `./scripts/hooks` passes — `ruff check app tests` + `mypy app` clean.
      **Evidence:** `All checks passed!` (ruff);
      `Success: no issues found in 13 source files` (mypy).
- [x] **Format contract:** the verifier accepts a valid `OggS`+`OpusHead`
      byte string and rejects empty bytes, raw PCM, MP3, and truncated
      headers. **Evidence:** `tests/unit/test_narrator.py` —
      `test_accepts_a_valid_ogg_opus_stream`,
      `test_accepts_a_second_page_after_a_plain_first_page_body`,
      `test_rejects_empty_bytes`, `test_rejects_raw_pcm`,
      `test_rejects_mp3`, `test_rejects_truncated_headers`,
      `test_rejects_an_opus_head_outside_the_first_page`.
- [x] **TTS edge contract:** valid `audio/l16; rate=24000; channels=1`
      payload parses; missing part / wrong mime / wrong rate / empty data
      each raise a typed, logged failure. **Evidence:**
      `tests/unit/test_narrator.py` — `test_valid_audio_part_parses_to_the_payload_bytes`,
      `test_parameter_order_does_not_break_the_contract`,
      `test_missing_audio_part_raises_a_typed_error` (4 shapes: no_candidates,
      no_content, no_parts, text_only), `test_unexpected_mime_raises_a_typed_error`
      (wrong rate 16000, stereo, no-params, mp3/ogg mime, None),
      `test_empty_audio_data_raises_a_typed_error`,
      `test_more_than_one_audio_part_is_rejected`,
      `test_every_failure_is_a_narration_error`,
      `test_client_failure_raises_a_typed_error_and_is_logged`,
      `test_malformed_response_raises_a_typed_error_and_is_logged`.
- [x] **Conversion:** fake 24 kHz s16le PCM → output bytes pass the
      OGG/Opus verifier (`OggS` at offset 0, `OpusHead` in first page).
      **Evidence:** `tests/unit/test_narrator.py` —
      `test_fake_pcm_becomes_verified_ogg_opus`,
      `test_output_is_never_an_mp3`; also exercised by the live test
      (see below).
- [x] **Narrator request shape:** the fake client captures the outgoing
      request: pinned model `gemini-3.1-flash-tts-preview`,
      `response_modalities=["AUDIO"]`, deep-male voice preset + style
      instruction wrapping the script (D4). **Evidence:**
      `tests/unit/test_narrator.py` — `test_request_pins_the_tts_model`,
      `test_request_asks_for_the_audio_modality`,
      `test_request_carries_the_deep_male_voice_preset`,
      `test_style_instruction_wraps_the_script`, `test_pinned_model`,
      `test_deep_male_voice_preset`,
      `test_style_instruction_is_a_dramatic_posh_british_narrator`.
- [x] **Transport:** `send_voice` POSTs multipart to `…/sendVoice` with the
      correct `chat_id` and a `voice` part named `voice.ogg`
      (`audio/ogg`); `ok:false`/non-JSON raise `RuntimeError` with
      Telegram's description; secret-hygiene tests still pass (no token in
      logs/exceptions). **Evidence:** `tests/unit/test_gateway_voice.py` —
      `test_posts_multipart_to_send_voice`,
      `test_voice_part_is_named_voice_dot_ogg_with_audio_ogg`,
      `test_ok_true_returns_without_raising`,
      `test_ok_false_raises_with_telegrams_description`,
      `test_non_json_body_raises_runtime_error_and_is_logged`,
      `test_send_voice_failure_never_leaks_the_token`; plus
      `python3 -m pytest tests/unit/test_secret_hygiene.py -q` →
      `7 passed in 0.31s`.
- [x] **Integration order:** with a fake narrator, the update flow yields
      portrait → script text → voice note, all to the **same chat_id**, no
      extra user message required (D2 keeps the text message). **Evidence:**
      `tests/unit/test_gateway_narrator.py` —
      `test_portrait_script_and_voice_arrive_in_order_to_the_same_chat`,
      `test_narrator_receives_exactly_the_stored_script`.
- [x] **Phase machine:** `SCRIPTING` after the portrait, `NARRATING` after
      the script, `DONE` after the voice note (TECH.md table); every
      failure branch (TTS raise, not-wired, send failure) lands `DONE` with
      a friendly reply logged, and the session stays usable. **Evidence:**
      `tests/unit/test_gateway_narrator.py` —
      `test_phase_is_scripting_when_the_scripter_runs`,
      `test_phase_is_narrating_when_the_narrator_runs`,
      `test_phase_is_done_after_the_voice_note_and_session_survives`,
      `test_tts_failure_sends_a_friendly_reply_lands_done_and_logs`,
      `test_voice_send_failure_sends_a_friendly_reply_lands_done_and_logs`,
      `test_narrator_not_wired_still_delivers_the_script_and_logs`,
      `test_tts_failure_never_escapes_the_poll_loop`;
      `tests/unit/test_gateway_narrator.py::TestRunNarratorWiring` —
      `test_gateway_started_reports_the_narrator_model_and_wiring`,
      `test_run_forwards_the_narrator_into_poll_once`,
      `test_narrator_init_failure_reports_not_wired_and_keeps_polling`.
- [x] **Regression:** pre-existing tests (Bouncer, Interviewer, Converter,
      Scripter, secret hygiene) pass unchanged — the pipeline is extended,
      not rewritten. **Evidence:** full suite green (`278 passed`) with
      all pre-existing test modules passing; secret hygiene `7 passed`.
- [x] **Live (gated):** `RUN_LIVE_TESTS=1` runs `test_live_narrator`:
      real TTS synthesis → non-empty bytes that pass the OGG/Opus
      contract. **Evidence:**
      `RUN_LIVE_TESTS=1 python3 -m pytest tests/integration/test_live_narrator.py -q`
      → `1 passed in 7.10s`
      (`test_live_narrator_synthesises_verified_ogg_opus` — real Gemini
      TTS, asserts `OggS` prefix and runs `verify_ogg_opus`; never touches
      Telegram).

## End-to-end on the live bot (manual, reported honestly)

- [x] **Verified by the orchestrator after the verifier pass (11:00, PID
      38860):** server restarted cleanly on the feature branch; the live
      `gateway_started` log line reads
      `… converter_model=gemini-3.1-flash-image converter_wired=True
      scripter_wired=True narrator_model=gemini-3.1-flash-tts-preview
      narrator_wired=True`; long-poll socket ESTAB to Telegram's DC,
      `getMe` ok, and a delivery to the active chat succeeded
      (`sendMessage` → `message_id 259`). Also covered offline by
      `test_gateway_started_reports_the_narrator_model_and_wiring`.
- [x] **Full happy path — verified by the orchestrator via a real-pipeline
      E2E drive (2026-10-18, ~11:30).** All five real stages ran against
      the live APIs exactly as `run()` wires them: fixture portrait
      (`tests/fixtures/person.jpg`, injected fetcher — the only synthetic
      step; file-id download is covered by Bouncer unit/live tests) →
      Bouncer admission (real vision call) → YES → 5 adaptive interview
      questions (real ADK calls) → "You're a House Cat" → hybrid portrait
      (`sendPhoto` ok) → 84-word script text (`sendMessage` ok) → voice
      note (`sendVoice` ok, 55 s). All 10 outbound API calls delivered to
      chat `6682234839` only; session ended `DONE` with `session.script`
      set; the completing answer's single `handle_update` call ran the
      whole auto-trigger chain synchronously (44 s) with no exception.
- [x] **Voice note format/delivery — verified by round-trip (same E2E
      run).** Telegram's `sendVoice` response contained a `voice` object
      with `mime_type=audio/ogg`, `duration=55`, `file_size=244519`;
      `getFile` → download of the stored file
      (`voice/file_10.oga`, 244 519 bytes) passes the same
      `verify_ogg_opus` magic-byte contract (`OggS` + `OpusHead`). A
      `sendVoice` delivery with `audio/ogg` renders as a voice note (not a
      file/audio attachment) in official clients. The only remaining human
      act is the ear itself: the voice note is in the chat, ready to tap.
- [x] **Partial-run report (this box checked):** verified here — offline
      suite (`278 passed, 8 skipped`), `./scripts/hooks` clean, real Gemini
      TTS synthesis via the gated live test, transport and phase-machine
      behaviour via mocks, secret hygiene. The three manual boxes above —
      which the verifier could not complete — were subsequently driven to
      completion by the orchestrator (live-server restart; real-pipeline
      E2E into the real chat; voice-note round-trip via `getFile`), with
      evidence recorded in each box. The only act left to a human is
      listening to the delivered voice note. No unverified claims.

## Spec-drift review (verifier)

- [x] Differences between this spec and the implementation are surfaced;
      spec/TECH/MISSION/ROADMAP/README updated to what was **actually**
      implemented. Drift found and fixed by the verifier:
      (a) `requirements.md` Logging section listed `chat_id` inside the
      `narration_synthesised`/`narration_converted` events — in the
      implementation those events (emitted in `app/narrator.py`) carry
      `model`/`script_words`/`pcm_bytes` and `ogg_bytes`/`duration`;
      `chat_id` lives on the gateway-side events (`narrator_error`,
      `voice_sent`, `voice_send_failed`, `narrator_not_wired`). Requirements
      Logging section corrected to match. (b) `TECH.md` dependency note and
      "delivered" sections updated for the Narrator; `ROADMAP.md` Phase 5/6
      marked Delivered; `README.md` Project status updated. No contradictions
      found in `MISSION.md` (it already described the Narrator target) or in
      `TECH.md`'s state-machine table / architecture (implementation matches
      exactly). No app-code or test changes made by the verifier.
