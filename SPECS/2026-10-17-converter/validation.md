# Validation — Converter (ROADMAP Phase 4)

## 1. Automated checks
| Check | Command | Expected |
| --- | --- | --- |
| Unit + integration | `./scripts/test` | All pass; live tests skipped by default (no live converter tests). |
| Lint + types | `./scripts/hooks` | ruff clean, mypy clean |

## 2. Contract checks
- [ ] `ConverterResult` validates and carries animal/image bytes.
- [ ] All three inputs (original photo, dossier summary, suggested animal) passed in one multimodal request.
- [ ] Pydantic models used at boundaries (`OutboundPhoto`, `ConverterResult`).
- [ ] `TelegramSender.send_photo` posts multipart to `sendPhoto`; no `raise_for_status()` that leaks token.

## 3. Behaviour checks
- [ ] Original photo stored at admission (bytes/mime/file_id) on session.
- [ ] When interview completes to DONE and converter wired, portrait is sent directly; no text reply returned.
- [ ] Caption names the suggested animal.
- [ ] Converter failure degrades gracefully without killing session.
- [ ] Missing original photo/dossier → falls back to portrait pending tease.
- [ ] Photo fetch failure at admission is swallowed; interview can still proceed.
- [ ] No raw answers logged verbatim.
- [ ] Session isolation preserved (unchanged).

## 4. Merge gate
- [ ] `./scripts/test` green (offline)
- [ ] `./scripts/hooks` clean
- [ ] Docs updated if needed (TECH.md/ROADMAP.md/README.md)
- [ ] No secrets in tracked files/logs
