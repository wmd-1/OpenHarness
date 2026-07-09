# media-use TTS Specification

Source of truth for the HyperFrames `media-use` shared audio engine TTS provider behavior in OpenHarness.

## Requirements

### QwenTTS as highest-priority TTS provider

When `QWENTTS_URL` is set, the shared audio engine MUST select QwenTTS (local vLLM-Omni OpenAI-compatible `/v1/audio/speech`) as the first provider in the selection chain, above HeyGen / ElevenLabs / Kokoro.

#### Scenario: QWENTTS_URL set → auto-select QwenTTS
- GIVEN `QWENTTS_URL=http://localhost:8091`
- WHEN `pickProvider()` called with no explicit provider
- THEN returns `"qwentts"`

#### Scenario: explicit qwentts without URL → validation error
- GIVEN `QWENTTS_URL` unset
- WHEN `pickProvider("qwentts")`
- THEN throws `provider=qwentts but $QWENTTS_URL is not set`

#### Scenario: synthesizeOne dispatches to QwenTTS
- GIVEN provider `"qwentts"`
- WHEN `synthesizeOne({ provider:"qwentts", ... })`
- THEN calls `synthesizeQwenTTS()` and returns its result

#### Scenario: QwenTTS unreachable → graceful degrade
- GIVEN `QWENTTS_URL` points to an unreachable service
- WHEN `synthesizeQwenTTS()` executes
- THEN returns `{ ok:false, words:null }` and does NOT throw or write a partial file

#### Scenario: resolveVoiceId resolves qwentts voice
- GIVEN provider `"qwentts"`
- WHEN `resolveVoiceId("qwentts", ...)`
- THEN returns `process.env.QWENTTS_VOICE || "vivian"`

### TTS provider documentation

The provider-chain docs and tables in `media-use/SKILL.md` and `media-use/audio/references/tts.md` MUST list QwenTTS as the first entry and note the exception "QwenTTS wins regardless of the HeyGen switch when `$QWENTTS_URL` is set".

#### Scenario: provider table first row is QwenTTS
- GIVEN reading `media-use/SKILL.md` TTS provider table
- WHEN viewing the Order=1 row
- THEN shows `QwenTTS (local) | $QWENTTS_URL set | No word timestamps`

## Deprecated

### QwenTTS injected at hyperframes-media/scripts/lib/tts.mjs (Removed: 2026-07-08)
Reason: `hyperframes-media` retired and renamed to `media-use`; the QwenTTS injection was migrated to `media-use/audio/scripts/lib/tts.mjs` (see change `sync-hyperframes-latest-patches`).
