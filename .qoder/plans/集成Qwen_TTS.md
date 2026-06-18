# 集成 QwenTTS 作为 HyperFrames TTS Provider

## 设计决策

- **环境变量**: `QWENTTS_URL`（如 `http://localhost:8091`），设置后自动启用 qwentts provider
- **双模式**: 通过 `QWENTTS_MODE` 环境变量切换，默认 `speech`（`/v1/audio/speech`），可选 `chat`（`/v1/chat/completions`）
- **优先级**: qwentts 排在 HeyGen 之前（本地部署优先于云端）
- **voice 映射**: 直接用 QwenTTS 的 voice 名称（如 `vivian`），通过 `--voice` 参数传递；chat 模式下 voice 由模型 system prompt 控制，不传 voice 字段
- **默认 voice**: `QWENTTS_VOICE` 环境变量，或默认 `vivian`
- **音频归一化**: 所有模式输出统一经 ffmpeg 转码为 WAV 44.1kHz mono，与 HeyGen 路径保持一致，确保下游 whisper/ffprobe 正常工作

## 改动文件清单

共涉及 **3 个 audio.mjs** + **3 个 guide.md** + **2 个 SKILL.md/reference** = 8 个文件。

---

## Task 1: 修改 `hyperframes_github_skills/product-launch-video/scripts/audio.mjs`

### 1.1 更新文件头部 usage 注释

找到 `--provider heygen|elevenlabs|kokoro`，改为 `--provider qwentts|heygen|elevenlabs|kokoro`。

### 1.2 添加 `qwenttsAvailable()` 检测函数

在 `heygenAvailable()` 前插入：
```js
function qwenttsAvailable() {
  return !!process.env.QWENTTS_URL;
}
```

### 1.3 更新 provider 检测链

```js
// 原：provider = heygenAvailable() ? "heygen" : elevenlabsAvailable() ? "elevenlabs" : "kokoro";
// 改：
provider = qwenttsAvailable() ? "qwentts" : heygenAvailable() ? "heygen" : elevenlabsAvailable() ? "elevenlabs" : "kokoro";
```

### 1.4 更新 provider 验证列表 + 添加 qwentts 可用性校验

```js
if (!["qwentts", "heygen", "elevenlabs", "kokoro"].includes(provider))
  die(`invalid --provider "${provider}" (must be qwentts | heygen | elevenlabs | kokoro)`);
if (provider === "qwentts" && !qwenttsAvailable())
  die("provider=qwentts but $QWENTTS_URL is not set");
```
（保留后续 heygen/elevenlabs 的已有校验不变）

### 1.5 更新 voice 默认值逻辑

在 voiceId 三元表达式中，在 kokoro 分支之后、heygen default（`: null`）之前插入：
```js
let voiceId =
  userVoice ||
  (provider === "elevenlabs"
    ? "21m00Tcm4TlvDq8ikWAM" // Rachel (ElevenLabs default)
    : provider === "kokoro"
      ? lang === "en"
        ? "am_michael"
        : die(
            "Kokoro non-English path requires explicit --voice (see /hyperframes-media references/tts.md)",
          )
      : provider === "qwentts"
        ? (process.env.QWENTTS_VOICE || "vivian")
        : null); // heygen default resolved below — needs a starfish voice_id
```

### 1.6 添加 `synthesizeQwenTTS(s)` 函数

在 `synthesizeHeygen()` 之后、`ttsScene()` 之前插入。**关键点**：
- 先写到 tmpFile，再用 ffmpeg 归一化为 44.1kHz mono WAV（与 HeyGen 路径一致）
- chat 模式不传 voice（Qwen3-Omni 由 system prompt 控制音色）
- catch 块打印错误信息便于调试

```js
async function synthesizeQwenTTS(s) {
  const baseUrl = process.env.QWENTTS_URL.replace(/\/+$/, "");
  const mode = (process.env.QWENTTS_MODE || "speech").toLowerCase();
  const wavAbs = join(hyperframesDir, `assets/voice/${s.sceneId}.wav`);
  const text = readFileSync(scratchPath(`${s.sceneId}.txt`), "utf8");
  const model = process.env.QWENTTS_MODEL || "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice";
  const instructions = process.env.QWENTTS_INSTRUCTIONS || undefined;
  const td = mkdtempSync(join(tmpdir(), `hf-qwentts-${s.sceneId}-`));
  const tmpRaw = join(td, "raw_audio");

  try {
    if (mode === "chat") {
      // Qwen3-Omni chat completions 模式
      // 注意：不传 voice 参数，Qwen3-Omni 通过 system prompt 控制音色
      const res = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model,
          messages: [{ role: "user", content: text }],
          modalities: ["audio"],
        }),
      });
      if (!res.ok) {
        console.error(`QwenTTS chat API HTTP ${res.status}`);
        return { status: -1 };
      }
      const payload = await res.json();
      const b64 = payload?.choices?.[0]?.message?.audio?.data;
      if (!b64) {
        console.error("QwenTTS chat API: no audio.data in response");
        return { status: -1 };
      }
      writeFileSync(tmpRaw, Buffer.from(b64, "base64"));
    } else {
      // 默认 speech 模式：直接返回二进制音频流
      const res = await fetch(`${baseUrl}/v1/audio/speech`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model,
          input: text,
          voice: voiceId,
          response_format: "wav",
          ...(lang !== "en" && { language: lang }),
          ...(instructions && { instructions }),
        }),
      });
      if (!res.ok) {
        console.error(`QwenTTS speech API HTTP ${res.status}`);
        return { status: -1 };
      }
      const buf = Buffer.from(await res.arrayBuffer());
      writeFileSync(tmpRaw, buf);
    }

    // 音频归一化：统一转码为 WAV 44.1kHz mono（与 HeyGen 路径一致）
    const ff = spawnSync(
      "ffmpeg",
      ["-y", "-loglevel", "error", "-i", tmpRaw, "-ar", "44100", "-ac", "1", wavAbs],
      { stdio: "ignore" },
    );
    rmSync(td, { recursive: true, force: true });
    if (ff.status !== 0 || !existsSync(wavAbs)) {
      console.error("QwenTTS: ffmpeg normalization failed");
      return { status: -1 };
    }
    return { status: 0 };
  } catch (err) {
    console.error(`QwenTTS synthesis failed: ${err?.message || err}`);
    rmSync(td, { recursive: true, force: true });
    return { status: -1 };
  }
}
```

### 1.7 在 `ttsScene()` 中添加 qwentts 分支

在 `if (provider === "heygen")` 之后加：
```js
if (provider === "qwentts") return synthesizeQwenTTS(s);
```

---

## Task 2: 同步修改 `hyperframes_github_skills/pr-to-video/scripts/audio.mjs`

与 Task 1 相同的 TTS 部分改动（1.1-1.7）。

**注意**：该文件的 BGM 部分（Lyria/MusicGen 逻辑）与 product-launch-video 不同，但 TTS 相关代码（Step 3 provider 检测 + Step 6 ttsScene）结构一致。插入位置相同，不要触碰 BGM 部分。

---

## Task 3: 同步修改 `hyperframes_github_skills/faceless-explainer/scripts/audio.mjs`

与 Task 1 完全相同的改动。

---

## Task 4: 更新 `hyperframes_github_skills/hyperframes-media/SKILL.md`

在 TTS provider 表格中添加 qwentts 行，并将所有行重新编号为 1-4（QwenTTS 优先级最高）：
```
| 1 | QwenTTS (local, vLLM-Omni) | `$QWENTTS_URL` set | No -- chain `transcribe` after |
| 2 | HeyGen (Starfish)          | ... (existing)     | ...                            |
| 3 | ElevenLabs                 | ... (existing)     | ...                            |
| 4 | Kokoro-82M (local)         | ... (existing)     | ...                            |
```

在 non-negotiable rules 中补充：
- QwenTTS voice 名称与 Kokoro/HeyGen/ElevenLabs 互不兼容，`--voice vivian` 只适用于 qwentts provider。

---

## Task 5: 更新 `hyperframes_github_skills/hyperframes-media/references/tts.md`

添加 QwenTTS provider 段落：
- 环境变量说明（`QWENTTS_URL`、`QWENTTS_MODE`、`QWENTTS_MODEL`、`QWENTTS_VOICE`）
- 两种模式说明：speech（推荐，直接返回二进制流）vs chat（Qwen3-Omni，base64 JSON）
- 与 Kokoro 的关系：qwentts 优先于 kokoro，设置 `QWENTTS_URL` 即自动切换

---

## Task 6: 更新 3 个 `phases/audio/guide.md`

- `hyperframes_github_skills/product-launch-video/phases/audio/guide.md`
- `hyperframes_github_skills/faceless-explainer/phases/audio/guide.md`
- `hyperframes_github_skills/pr-to-video/phases/audio/guide.md`（该文件比其他两个多一段 BGM backend selection，改动时注意只改 tts_provider 部分）

将 `tts_provider` 枚举从：
```json
"tts_provider": "heygen" | "elevenlabs" | "kokoro"
```
改为：
```json
"tts_provider": "qwentts" | "heygen" | "elevenlabs" | "kokoro"
```

---

## 环境变量速查

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `QWENTTS_URL` | 是 | 无 | 服务地址，如 `http://localhost:8091` |
| `QWENTTS_MODE` | 否 | `speech` | `speech`（二进制流）或 `chat`（base64 JSON） |
| `QWENTTS_MODEL` | 否 | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` | 模型名称 |
| `QWENTTS_VOICE` | 否 | `vivian` | 默认音色（仅 speech 模式；chat 模式由模型内部控制） |
| `QWENTTS_INSTRUCTIONS` | 否 | — | 情感/风格指令（如 `"Speak with great enthusiasm"`，仅 CustomVoice 模型） |

---

## Dockerfile 建议变更

无需改动源码。只需在 docker-compose.yml 或 docker run 中添加环境变量：
```yaml
environment:
  - QWENTTS_URL=http://qwentts-host:8091
  - QWENTTS_VOICE=vivian
```

Kokoro 模型预下载部分可保留（作为 fallback），也可删除以减小镜像体积。
