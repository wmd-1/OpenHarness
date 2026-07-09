# Delta: HyperFrames media-use 音频引擎 — QwenTTS 最高优先级 provider

**Change ID:** `sync-hyperframes-latest-patches`
**Affects:** `hyperframes_github_skills/media-use/audio/scripts/lib/tts.mjs`, `audio.mjs`, `SKILL.md`, `audio/references/tts.md`

---

## ADDED

### Requirement: QwenTTS 作为最高优先级 TTS provider

当环境变量 `QWENTTS_URL` 设置时，共享音频引擎必须把 QwenTTS（本地 vLLM-Omni OpenAI 兼容 `/v1/audio/speech`）作为 provider 选择链的**首位**，优先级高于 HeyGen / ElevenLabs / Kokoro。

#### Scenario: QWENTTS_URL 已设置时自动选中 QwenTTS
- GIVEN 进程环境变量 `QWENTTS_URL=http://localhost:8091`
- WHEN 调用 `pickProvider()` 未指定 provider
- THEN 返回 `"qwentts"`

#### Scenario: 用户显式指定 qwentts 但未设 URL 时校验失败
- GIVEN 未设置 `QWENTTS_URL`
- WHEN 调用 `pickProvider("qwentts")`
- THEN 抛出错误 `provider=qwentts but $QWENTTS_URL is not set`

#### Scenario: synthesizeOne 分发到 QwenTTS
- GIVEN provider 为 `qwentts`
- WHEN 调用 `synthesizeOne({ provider:"qwentts", ... })`
- THEN 调用 `synthesizeQwenTTS()` 并返回其结果

#### Scenario: QwenTTS 不可达时优雅降级
- GIVEN `QWENTTS_URL` 指向不可达服务
- WHEN `synthesizeQwenTTS()` 执行
- THEN 返回 `{ ok:false, words:null }` 且不抛异常、不写半成品文件（避免静默失败连锁回退 Kokoro）

#### Scenario: resolveVoiceId 解析 qwentts 音色
- GIVEN provider 为 `qwentts`
- WHEN 调用 `resolveVoiceId("qwentts", ...)`
- THEN 返回 `process.env.QWENTTS_VOICE || "vivian"`

---

## MODIFIED

### Requirement: TTS provider 文档（media-use/SKILL.md 与 references/tts.md）

provider 链文档与表格须把 QwenTTS 列为第 1 顺位，并说明"QwenTTS 在 `$QWENTTS_URL` 设置时无视 HeyGen 开关直接胜出"的例外。

#### Scenario: provider 表首行为 QwenTTS
- GIVEN 阅读 `media-use/SKILL.md` 的 TTS provider 表
- WHEN 查看 Order=1 行
- THEN 显示 `QwenTTS (local) | $QWENTTS_URL set | No word timestamps`

---

## REMOVED

- 旧路径 `hyperframes-media/scripts/lib/tts.mjs` 上的 QwenTTS 注入（随 hyperframes-media 退休移除，迁移至 `media-use/audio/scripts/lib/tts.mjs`）。
