# OpenHarness 对 HyperFrames skill 的定制补丁同步指南

> 用途：本文档记录 OpenHarness 在**上游 HyperFrames skill** 基础上做的两类定制（**QwenTTS**、**Chrome 路径**），供以后从 hyperframes 的 github 拉取最新版 skill 后，照此重新应用补丁。
>
> 对应提交：
> - `de72011` — v1.3：升级到 HyperFrames v0.7.2 技能集 + 将 QwenTTS 接入共享音频引擎
> - `4feb2ff` — 在 skill 文档中添加 OpenHarness 运行时的 Chrome 配置说明

---

## 1. 背景与目录约定

仓库里有三套 skill 目录，角色不同，**不要混淆**：

| 目录 | 角色 | 处理方式 |
| --- | --- | --- |
| `hyperframes_container_skills/` | 旧版（过期） | **忽略**，不再维护 |
| `hyperframes_github_skills_latest/` | 从 hyperframes github 同步的**上游原版最新** skill（当前为空，拉取目标） | 拉新版时填充，作为基线 |
| `hyperframes_github_skills/` | **实际使用**的、已打 OpenHarness 补丁的版本 | Docker 构建时 `COPY` 进镜像；补丁打在这里 |

镜像构建链路（[Dockerfile:98](../Dockerfile#L98)、[Dockerfile.fix:36](../Dockerfile.fix#L36)）：

```
hyperframes_github_skills/   ──Docker COPY──▶  /opt/oh-skills-builtin/  ──wrapper cp -a──▶  /root/.openharness/skills/  ──oh CLI 加载
```

api 服务（docker-compose `api`）`extends: openharness`，与交互式 CLI **共用同一镜像、同一份 skill**，无独立副本。

---

## 2. 同步工作流

每次 hyperframes 上游发布新版 skill 时：

1. **拉取上游最新** → 填充 `hyperframes_github_skills_latest/`（`npx skills add heygen-com/hyperframes` 或直接 clone github 仓库的 skills 目录）。
2. **用 latest 覆盖实际使用目录**：把 `hyperframes_github_skills_latest/` 的内容覆盖到 `hyperframes_github_skills/`。
3. **重新应用 OpenHarness 补丁**：按本文档第 3、4 节，在 `hyperframes_github_skills/` 上逐文件打回 QwenTTS + Chrome 定制。
4. **同步构建配置**：按第 5 节更新 `Dockerfile.fix` / `.env.example` 的版本标签。
5. **重建镜像**：`docker build -f Dockerfile.fix --build-arg BASE_IMAGE=<旧tag> -t <新tag> .`（见第 5 节）。
6. **验证**：按第 6 节确认补丁生效。

> ⚠ 关键原则：只把 **OpenHarness 注入的部分**手动打回。上游 v0.7.2 自带的结构变化（工作流 `audio.mjs` 改薄适配器、faceless-explainer 重构等）拉新版即得，**不要手动重复**（见第 7 节）。

---

## 3. 补丁一：QwenTTS（本地 TTS，最高优先级 provider）

### 3.1 意图与根因

把本地 QwenTTS 服务集成为**最高优先级** TTS provider，修复"容器只会回退 Kokoro"的问题。

**根因**（来自 `de72011` 提交说明）：
1. 旧版 QwenTTS 仅 vendored 在 `product-launch-video` / `pr-to-video` / `faceless-explainer` 三个 per-skill `audio.mjs` 中；`general-video` 等走 `npx hyperframes tts`（Kokoro-only CLI）的工作流**从不查询 QwenTTS**。
2. `QWENTTS_URL=http://localhost:8091` 是容器自身 loopback，GPU 机器上的 QwenTTS 服务不可达，导致 QwenTTS 感知的技能也静默失败、回退 Kokoro。

**解法**：在**唯一共享 TTS 库** `hyperframes-media/scripts/lib/tts.mjs` 中加一处 QwenTTS 分支，即覆盖全部视频工作流；设 `QWENTTS_URL` 时优先于 HeyGen / ElevenLabs / Kokoro。

### 3.2 涉及文件

| 文件 | 补丁性质 |
| --- | --- |
| `hyperframes-media/scripts/lib/tts.mjs` | **核心**：注入 QwenTTS provider（检测/选择/voice/合成） |
| `hyperframes-media/scripts/audio.mjs` | 注释标注 QwenTTS 优先级（代码靠 import tts.mjs 间接支持） |
| `hyperframes-media/SKILL.md` | provider 文档 |
| `hyperframes-media/references/tts.md` | QwenTTS 详细参考节 |

### 3.3 `scripts/lib/tts.mjs` — 注入 QwenTTS provider（6 处）

> 上游 v0.7.2 的 `tts.mjs` 自带 HeyGen / ElevenLabs / Kokoro / transcribe 等基础设施。OpenHarness 在其上插入下面 6 处 QwenTTS 片段。若上游新版函数名/结构变化，按"意图"在对应位置适配。

**注入点 ① — 文件顶部 provider chain 注释**：在 provider 列表最前面加 QwenTTS 第 1 条（原上游第 1 条 HeyGen 顺延为第 2）：

```js
//   1. QwenTTS (local)    — $QWENTTS_URL (highest priority when set). OpenAI-
//        compatible /v1/audio/speech (speech mode) or /v1/chat/completions
//        (chat mode) served by vLLM-Omni. No word timings → caller transcribes.
```

**注入点 ② — `qwenttsAvailable()` 检测函数**（与 `heygenAvailable` 等并列）：

```js
export function qwenttsAvailable() {
  return !!process.env.QWENTTS_URL;
}
```

**注入点 ③ — `pickProvider()` 把 QwenTTS 设为链首**：
- 校验白名单加 `"qwentts"`；
- 加 `provider=qwentts` 但未设 `QWENTTS_URL` 的校验；
- 自动选择链首加 `qwenttsAvailable() ? "qwentts" :`。

```js
// First available provider wins; an explicit choice is honored (and validated).
// Chain: QwenTTS (local, $QWENTTS_URL) → HeyGen → ElevenLabs → Kokoro (always).
export function pickProvider(userProvider) {
  if (userProvider) {
    if (!["qwentts", "heygen", "elevenlabs", "kokoro"].includes(userProvider))
      throw new Error(`invalid provider "${userProvider}" (qwentts | heygen | elevenlabs | kokoro)`);
    if (userProvider === "qwentts" && !qwenttsAvailable())
      throw new Error("provider=qwentts but $QWENTTS_URL is not set");
    if (userProvider === "heygen" && !heygenAvailable())
      throw new Error(
        "provider=heygen but no HeyGen credentials (set $HEYGEN_API_KEY or run `hyperframes auth login`)",
      );
    if (userProvider === "elevenlabs" && !process.env.ELEVENLABS_API_KEY)
      throw new Error("provider=elevenlabs but $ELEVENLABS_API_KEY is not set");
    return userProvider;
  }
  return qwenttsAvailable()
    ? "qwentts"
    : heygenAvailable()
      ? "heygen"
      : elevenlabsAvailable()
        ? "elevenlabs"
        : "kokoro";
}
```

**注入点 ④ — `resolveVoiceId()` 加 qwentts 分支**（返回 `QWENTTS_VOICE` 或默认 `vivian`）：

```js
  if (provider === "qwentts") return process.env.QWENTTS_VOICE || "vivian";
```

**注入点 ⑤ — `synthesizeOne()` 加 qwentts 分发**（在 heygen 分支之前）：

```js
  if (provider === "qwentts") return synthesizeQwenTTS({ text, voiceId, lang, wavAbs });
```

**注入点 ⑥ — `synthesizeQwenTTS()` 实现 + `QWENTTS_LANG_FULL_NAME` 常量**：
- `speech` 模式（默认）`POST /v1/audio/speech`，二进制流；
- `chat` 模式 `POST /v1/chat/completions`，`choices[0].message.audio.data` 取 base64；
- 均经 `transcodeToWav` 归一化为 44.1k 单声道 wav；
- **不可达时优雅返回 `{ok:false}`，不抛异常、不写半成品**（这是修复根因 2 的关键——避免静默失败连锁回退）。

```js
// QwenTTS (local, vLLM-Omni OpenAI-compatible /v1/audio/speech) — highest-priority
// provider when $QWENTTS_URL is set. speech mode → binary stream; chat mode →
// base64 in choices[0].message.audio.data. Both normalized to 44.1k mono wav via
// transcodeToWav (same path as the HeyGen mp3). No word timestamps → caller
// transcribes. Never throws; failures return { ok:false }.
const QWENTTS_LANG_FULL_NAME = {
  en: "English", zh: "Chinese", ja: "Japanese", ko: "Korean", de: "German",
  fr: "French", ru: "Russian", pt: "Portuguese", es: "Spanish", it: "Italian",
};

async function synthesizeQwenTTS({ text, voiceId, lang, wavAbs }) {
  const baseUrl = (process.env.QWENTTS_URL || "").replace(/\/+$/, "");
  const mode = (process.env.QWENTTS_MODE || "speech").toLowerCase();
  const instructions = process.env.QWENTTS_INSTRUCTIONS || undefined;
  try {
    let bytes;
    if (mode === "chat") {
      const res = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: [{ role: "user", content: text }],
          modalities: ["audio"],
        }),
      });
      if (!res.ok) return { ok: false, words: null };
      const payload = await res.json();
      const b64 = payload?.choices?.[0]?.message?.audio?.data;
      if (!b64) return { ok: false, words: null };
      bytes = Buffer.from(b64, "base64");
    } else {
      // language omitted for en (server Auto-detects); non-en mapped to full name.
      const language = QWENTTS_LANG_FULL_NAME[lang] || (lang !== "en" ? lang : undefined);
      const body = { input: text, voice: voiceId };
      if (language) body.language = language;
      if (instructions) body.instructions = instructions;
      const res = await fetch(`${baseUrl}/v1/audio/speech`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) return { ok: false, words: null };
      bytes = Buffer.from(await res.arrayBuffer());
    }
    if (!transcodeToWav(bytes, wavAbs)) return { ok: false, words: null };
    return { ok: true, words: null };
  } catch {
    return { ok: false, words: null };
  }
}
```

> `synthesizeQwenTTS` 依赖同文件已有的 `transcodeToWav`（上游基础设施，把任意音频字节 ffmpeg 成 44.1k 单声道 wav）。无需新增。

### 3.4 `scripts/audio.mjs` — 注释标注（2 处）

`audio.mjs` 是共享音频引擎，本身不直接写 QwenTTS，靠 `import { pickProvider, resolveVoiceId, synthesizeOne, ... } from "./lib/tts.mjs"` 间接支持。只需在**顶部注释**里把 QwenTTS 标进 provider chain：

注入点 ① — switch 说明里加 TTS exception：
```js
// The three capabilities degrade on ONE switch — whether HeyGen is configured
// (credential present, NOT the CLI). This mirrors the table in ../SKILL.md:
// (TTS exception: QwenTTS, when $QWENTTS_URL is set, wins regardless of the switch.)
```

注入点 ② — TTS chain 注释把 QwenTTS 放首位：
```js
//   TTS : QwenTTS → HeyGen REST → ElevenLabs → Kokoro (CLI)
```

### 3.5 `SKILL.md` — provider 文档

在 `hyperframes-media/SKILL.md` 里确保以下 QwenTTS 文档点存在（v1.2 起就有，v1.3 架构重写时保留）：

- `description` frontmatter 含 `QwenTTS local`：
  > `... multi-provider TTS (QwenTTS local / HeyGen / ElevenLabs / Kokoro) ...`

- "audio engine" 节说明 QwenTTS 优先级例外：
  > TTS has one exception: **QwenTTS, when `$QWENTTS_URL` is set, wins regardless of the switch** (it sits above HeyGen in `pickProvider`).

- TTS provider 表格第 1 行：

  | Order | Provider | Detected when | Word timestamps |
  | --- | --- | --- | --- |
  | 1 | QwenTTS (local) | `$QWENTTS_URL` set | No — chain `transcribe` after |
  | 2 | HeyGen (Starfish) | ... | ... |

### 3.6 `references/tts.md` — QwenTTS 参考节（完整）

上游 `tts.md` 不会有 QwenTTS 节（QwenTTS 是 OpenHarness 本地服务）。需在 `tts.md` 里加回以下内容：

**(a) Provider chain 表加 QwenTTS 第 1 行**：

```markdown
| 1     | QwenTTS (local)   | `$QWENTTS_URL` set                          | QwenTTS voice names (e.g. `vivian`)         | No                                        | ffmpeg → wav 44.1k   |
```

**(b) 整节 `## QwenTTS (local deployment)`**（插入位置：在 HeyGen 节之后、`## When to use which provider` 之前）：

```markdown
## QwenTTS (local deployment)

When `$QWENTTS_URL` is set (e.g. `http://localhost:8091`), QwenTTS becomes the highest-priority provider. Served via vLLM-Omni with the OpenAI-compatible `/v1/audio/speech` API.

### Model variants

Each task type requires a matching model checkpoint:

| Task Type     | Model                                    | Description                                        |
| ------------- | ---------------------------------------- | -------------------------------------------------- |
| `CustomVoice` | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`  | Predefined speaker voices + optional style/emotion  |
| `VoiceDesign` | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`  | Generate speech from natural language voice description |
| `Base`        | `Qwen/Qwen3-TTS-12Hz-1.7B-Base`         | Voice cloning from reference audio + transcript     |

Default: `CustomVoice` (predefined speakers like `vivian`).

### Modes

| Mode      | Env var            | Endpoint                | Response format            |
| --------- | ------------------ | ----------------------- | -------------------------- |
| `speech`  | `QWENTTS_MODE=speech` (default) | `/v1/audio/speech`       | Binary WAV stream          |
| `chat`    | `QWENTTS_MODE=chat`             | `/v1/chat/completions`   | JSON with base64 audio     |

### Environment variables

| Variable              | Required | Default                                    | Description                                     |
| --------------------- | -------- | ------------------------------------------ | ----------------------------------------------- |
| `QWENTTS_URL`         | Yes      | —                                          | Service base URL (e.g. `http://localhost:8091`)  |
| `QWENTTS_MODE`        | No       | `speech`                                   | `speech` (binary stream) or `chat` (base64 JSON)|
| `QWENTTS_VOICE`       | No       | `vivian`                                   | Voice name (speech mode only; list via `/v1/audio/voices`) |
| `QWENTTS_INSTRUCTIONS`| No       | —                                          | Style/emotion instruction (e.g. `"Speak with great enthusiasm"`, CustomVoice model only) |

### Notes

- All output is normalized to WAV 44.1kHz mono via ffmpeg (QwenTTS may output 24kHz PCM natively).
- `model` and `response_format` are omitted from the request (server defaults to loaded model + wav format).
- `language` is omitted by default (server Auto-detects); when `--lang` is non-English, mapped to full name (e.g. `zh` → `"Chinese"`). Supported: Auto, Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, Italian.
- QwenTTS does not return word timestamps — chain `transcribe` after for caption data.
- Voice names are QwenTTS-specific and not interchangeable with Kokoro/HeyGen/ElevenLabs.
- When `QWENTTS_URL` is unset, the provider chain falls through to HeyGen → ElevenLabs → Kokoro.
- The server serves one model variant at a time; switching task types requires a server restart.
```

**(c) `## When to use which provider` 表加 QwenTTS 行**：

```markdown
| Self-hosted / local-first TTS, no cloud dependency         | **QwenTTS** (`$QWENTTS_URL`)                        |
```

---

## 4. 补丁二：Chrome 路径（OpenHarness 运行时已预配置）

### 4.1 意图

OpenHarness Docker 运行时已把 Chrome headless shell 预配置好（`PRODUCER_HEADLESS_SHELL_PATH` / `CHROME_HEADLESS_BIN` 均指向 `/opt/chrome-headless-shell-linux64/chrome-headless-shell`）。需在 skill 文档里告诉模型：**直接 `render`，别自己设 Chrome 路径、别跑 `browser ensure`、别给 `render` 传 `--browser-path`**，避免模型纠结于 Chrome 诊断而跑偏。

### 4.2 涉及文件

| 文件 | 补丁内容 |
| --- | --- |
| `hyperframes-cli/SKILL.md` | render 步骤加 OpenHarness runtime callout |
| `hyperframes-cli/references/doctor-browser.md` | 顶部 callout + "Using a specific Chrome for render" 段 + doctor 误报 caveat |

### 4.3 `hyperframes-cli/SKILL.md` — render 步骤 callout

在 Render 步骤（`7. **Render** — pick the variant:`）下、变体列表前，插入：

```markdown
   > **OpenHarness runtime:** Chrome is **already configured** via `PRODUCER_HEADLESS_SHELL_PATH` (`/opt/chrome-headless-shell-linux64/chrome-headless-shell`, injected by the runtime). **Just run `render` — don't set a chrome path, don't run `browser ensure`, and don't pass `--browser-path`** to `render` (that flag is ignored by `render`; it's `preview`/`play` only). Read `references/doctor-browser.md` only if `render` actually fails with a Chrome error.
```

### 4.4 `hyperframes-cli/references/doctor-browser.md` — 3 处插入

**注入点 ① — 文件顶部 callout**（在 `Environment diagnosis...` 行之后）：

```markdown
> **⚠ OpenHarness runtime note** — Chrome is **already configured for you** in the OpenHarness Docker runtime: `PRODUCER_HEADLESS_SHELL_PATH` and `CHROME_HEADLESS_BIN` are both pre-set to `/opt/chrome-headless-shell-linux64/chrome-headless-shell` (injected by `service/app/workers/runner.py` and `docker-compose.yml`). **Do not set the Chrome path yourself, do not run `browser ensure`, and do not pass `--browser-path` to `render`. Just run `npx hyperframes render`.** Only read the rest of this file if `render` actually fails with a Chrome error.
```

**注入点 ② — 新增 `## Using a specific Chrome for render` 段落**（紧接顶部 callout 之后）：

```markdown
## Using a specific Chrome for `render`

`render` does **not** accept `--browser-path` — that flag is `preview`/`play` only (see `preview-render.md`). To point `render` at a specific Chrome / chrome-headless-shell binary, set the **`PRODUCER_HEADLESS_SHELL_PATH`** environment variable:

```bash
PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome-headless-shell-linux64/chrome-headless-shell \
  npx hyperframes render --quality draft --output out.mp4
```

- `npx hyperframes browser ensure` downloads the **pinned bundled** Chrome (for reproducible pixel output across machines) — it does **not** adopt an existing binary, so it is the wrong tool when a Chrome path is already supplied by the environment.
- `--browser-path` / `--user-data-dir` / `--remote-debugging-port` are `preview`/`play` flags and are ignored by `render`.
```

**注入点 ③ — Common issues 里给 "Missing bundled Chrome" 加 caveat**：

```markdown
- **Missing bundled Chrome** — run `npx hyperframes browser ensure`. **Caveat:** doctor's `Chrome` check only inspects the **bundled** build — it does **not** read `PRODUCER_HEADLESS_SHELL_PATH`. If you point `render` at a binary via that env var, doctor will still report Chrome as "not found"; that is **expected**. Gate on whether `render` actually succeeds, not on doctor's Chrome line.
```

---

## 5. 构建配置同步

### 5.1 `Dockerfile.fix` — BASE_IMAGE 标签

`Dockerfile.fix` 的 `BASE_IMAGE` 默认值与示例命令需指向带 QwenTTS 的镜像 tag（`openharness_hyperframes_qwen-tts:...`，而非旧的 `openharness_hyperframes:...`）：

```dockerfile
ARG BASE_IMAGE=openharness_hyperframes_qwen-tts:v0.1.9_v0.6.102_v1.2
FROM ${BASE_IMAGE}
```

示例命令（注释里）：
```bash
# 仅更新 skills
docker build -f Dockerfile.fix \
  --build-arg BASE_IMAGE=openharness_hyperframes_qwen-tts:v0.1.9_v0.6.102_v1.2 \
  -t openharness_hyperframes_qwen-tts:v0.1.9_v0.6.102_v1.2 .

# 同时升级 Hyperframes 版本
docker build -f Dockerfile.fix \
  --build-arg BASE_IMAGE=openharness_hyperframes_qwen-tts:v0.1.9_v0.6.102_v1.2 \
  --build-arg HYPERFRAMES_VERSION=0.7.2 \
  -t openharness_hyperframes_qwen-tts:v0.1.9_v0.7.2_v1.3 .
```

### 5.2 `.env.example` — 版本标签

```bash
# ---- 镜像版本标签 ----
OH_VERSION_HYPERFRAMES_VERSION=v0.1.9_v0.7.2_v1.3
```

> `.env` 被 `.gitignore` 忽略，`QWENTTS_URL` 占位符与镜像 tag v1.3 不入库，需在构建/运行环境单独配置。

### 5.3 `docker-compose.yml` — QwenTTS 环境变量

`api` 与 `openharness` 服务都需透传 QwenTTS 环境变量（已有，同步新版时保留）：

```yaml
environment:
  - QWENTTS_URL=${QWENTTS_URL:-}
  - QWENTTS_MODE=${QWENTTS_MODE:-speech}
  - QWENTTS_MODEL=${QWENTTS_MODEL:-}
  - QWENTTS_VOICE=${QWENTTS_VOICE:-}
  - QWENTTS_INSTRUCTIONS=${QWENTTS_INSTRUCTIONS:-}
  - PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome-headless-shell-linux64/chrome-headless-shell
  - CHROME_HEADLESS_BIN=/opt/chrome-headless-shell-linux64/chrome-headless-shell
```

---

## 6. 验证

### 6.1 静态（源码侧）

```bash
# tts.mjs 语法
node --check hyperframes_github_skills/hyperframes-media/scripts/lib/tts.mjs
node --check hyperframes_github_skills/hyperframes-media/scripts/audio.mjs

# QwenTTS 注入点计数（tts.mjs 应 ≈ 20 处 qwentts）
grep -c -i qwentts hyperframes_github_skills/hyperframes-media/scripts/lib/tts.mjs

# Chrome callout 在
grep -c "OpenHarness runtime note" hyperframes_github_skills/hyperframes-cli/references/doctor-browser.md
```

### 6.2 容器侧（确认 api 服务加载的就是改过的 skill）

```bash
# api 容器跑的是 v1.3 镜像
docker inspect openharness-api --format '{{.Config.Image}}'
# 期望: openharness_hyperframes_qwen-tts:v0.1.9_v0.7.2_v1.3

# 镜像内置 skill 含 QwenTTS
docker exec openharness-api grep -c qwentts /opt/oh-skills-builtin/hyperframes-media/scripts/lib/tts.mjs

# 运行时加载的 skill 也含 QwenTTS（证明已同步到卷）
docker exec openharness-api grep -c qwentts /root/.openharness/skills/hyperframes-media/scripts/lib/tts.mjs

# Chrome callout 在
docker exec openharness-api grep -c "OpenHarness runtime note" /root/.openharness/skills/hyperframes-cli/references/doctor-browser.md
```

> 命名卷 `openharness-config` 挂在 `/root/.openharness`。wrapper 用 `cp -a`（覆盖式，不删除旧文件）——重建镜像后新内容会覆盖生效，但 v1.3 删除的旧文件可能残留在卷里。若要彻底一致，把 wrapper 改为先清空再拷：`rm -rf /root/.openharness/skills 2>/dev/null; cp -a /opt/oh-skills-builtin/. /root/.openharness/skills/`（改 [Dockerfile:102-104](../Dockerfile#L102-L104) 与 [Dockerfile.fix:36-38](../Dockerfile.fix#L36-L38)）。

---

## 7. 上游 v0.7.2 自带变化（拉新版即得，**勿手动重复**）

`de72011` 里下面这些改动属于"整体替换为最新技能集"，上游新版自带，不需要手动打补丁：

- `faceless-explainer` 的 `agents/`、`phases/`、`style-presets/` 大量删除/重构（block-frame / capsule / claude / pin-and-paper / scatterbrain 等 preset）。
- 各工作流（`faceless-explainer` / `pr-to-video` / `product-launch-video`）的 per-skill `scripts/audio.mjs` 从"各自 vendored TTS 逻辑"改为"调用共享引擎的薄适配器"。
- 共享引擎组件：`hyperframes-media/scripts/audio.mjs`、`scripts/lib/{heygen,bgm,sfx}.mjs`、`scripts/heygen-tts.mjs`、`scripts/wait-bgm.mjs`（HeyGen/BGM/SFX 主体逻辑，**不含 QwenTTS**）。
- `references/bgm.md`、`references/sfx.md` 等 BGM/SFX 文档。

> 判据：文件内容含 `qwen` / `QWENTTS` 的才是 OpenHarness 定制（须手动打回）；其余 HeyGen/ElevenLabs/Kokoro/BGM/SFX 逻辑是上游自带。

---

## 8. pptx-to-html skill 适配（路径 + Python 依赖）

> pptx-to-html 不是 HyperFrames skill，但与 hyperframes 共用同一条镜像构建链路（`COPY` 进 `/opt/oh-skills-builtin/` → wrapper 同步到 `/root/.openharness/skills/` → `oh` 加载），适配模式同型，故一并记录在此。

### 8.1 意图

把上游 `cskwork/pptx-to-html` skill 接入 OpenHarness，使其能在 `oh` 里把 `.pptx` 转成 HTML（再交 hyperframes 渲染成视频）。上游 skill 面向 smithery 云环境，有三处与 OpenHarness 不匹配，须打补丁：

1. **Python 依赖缺失** — 主镜像 venv 未预装 `python-pptx` / `openpyxl` / `fonttools`，skill 跑转换会 `ModuleNotFoundError`。
2. **路径写死云环境** — SKILL.md 全程用 `/mnt/skills/user/pptx-to-html/...` 与 `/mnt/user-data/...`，oh 实际加载路径是 `/root/.openharness/skills/pptx-to-html/`。
3. **脚本名 / Phase 错位** — SKILL.md 引用 Phase 1 的 `convert_pptx_to_html.py`（仓库里已不存在），实际只有 `convert_pptx_to_html_v2.py`；且能力描述仍停留在 Phase 1（charts / SmartArt / animations 标"不支持"，v2 已实现）。

### 8.2 涉及文件

| 文件 | 补丁性质 |
| --- | --- |
| [Dockerfile.fix](../Dockerfile.fix) | 删无效的 `PPTX2HTML_VERSION` / `npx skills add --agent claude-code` 段（装到 `~/.claude/skills/`，oh 不读）；新增 `pip install -r requirements.txt` 到 `/root/.openharness-venv` |
| `pptx-to-html/SKILL.md` | 脚本名 → `_v2.py`；路径 → `/root/.openharness/skills/pptx-to-html/`；去掉 `/mnt/user-data` 写死与 `computer://`；能力描述同步到 Phase 2 |
| `pptx-to-html/README.md` | 删引用已移除的 Phase 1 脚本的两处（Basic Usage 的 legacy 示例 + 文件树 legacy 行） |

### 8.3 Dockerfile.fix — 删 smithery 段 + 装 venv 依赖

删除（对 oh 无效 —— `--agent claude-code` 装到 `~/.claude/skills/`，而 oh 只同步 `/opt/oh-skills-builtin/`）：

```dockerfile
# ---- 可选：升级 PPTX-TO-HTML 版本（不传则跳过）----
ARG PPTX2HTML_VERSION=""
RUN if [ -n "${PPTX2HTML_VERSION}" ]; then \
        npx -y skills add https://smithery.ai/skills/cskwork/pptx-to-html --agent claude-code; \
    fi
```

新增（放在两条 `COPY ... /opt/oh-skills-builtin/` 之后，跟着 skill 自带 `requirements.txt` 走）：

```dockerfile
# ---- 安装 pptx-to-html 的 Python 依赖到 OpenHarness venv ----
RUN /root/.openharness-venv/bin/pip install --no-cache-dir \
        -r /opt/oh-skills-builtin/pptx-to-html/requirements.txt
```

> 为何装到 venv：主 [Dockerfile](../Dockerfile#L87) 把 `/root/.openharness-venv/bin` 放在 `PATH` 最前，容器里 `python` / `python3` / `pip` 自动命中 venv，运行时无需 activate；安装时显式用 `/root/.openharness-venv/bin/pip` 最稳。

### 8.4 SKILL.md — 路径 + 脚本名 + 能力描述

**路径 / 脚本名替换**（4 处命令 + Workflow 叙述）：

| 旧 | 新 |
| --- | --- |
| `/mnt/skills/user/pptx-to-html/scripts/convert_pptx_to_html.py` | `/root/.openharness/skills/pptx-to-html/scripts/convert_pptx_to_html_v2.py` |
| `/mnt/user-data/uploads/<file>.pptx` | `<pptx-path>` / `/path/to/<file>.pptx`（不写死） |
| `/mnt/user-data/outputs` | `<output-dir>` / `/path/to/output-dir` |
| `computer:///mnt/user-data/outputs/<file>.html` | 直接给输出路径 |

**能力描述同步到 Phase 2**（参照 skill 自带 `CLAUDE.md` 的 ✅ 清单）：

- `What Gets Preserved` 补 Charts（Chart.js）/ Custom Shapes（SVG）/ SmartArt（文本层级）/ Animations / Shadows & Reflections。
- `Current Limitations` 删去 charts / smartart / animations / shadows / custom-shapes 的"不支持"（v2 已实现），改写为 CLAUDE.md 的 Known Limitations（SmartArt 仅文本、custom fonts fallback、3D 不保留、master 复杂继承、Macros/VBA 永不支持）。
- `Roadmap` 把上述项从 Phase 2/3 "In Progress / Future" 提升为 Phase 2 ✅ COMPLETED；Phase 3 仅留 embedded font extraction（FontManager，进行中）/ SmartArt 视觉布局 / 3D / master 继承。
- `Troubleshooting` 修正两条矛盾项（"custom shapes / SmartArt unsupported" → 改为 SmartArt 视觉简化；"Tables on Phase 2 roadmap" → 改为 SmartArt 已知限制）。

### 8.5 验证

```bash
# 依赖装到 venv
docker exec <容器> /root/.openharness-venv/bin/python -c "import pptx,openpyxl,fonttools;print('ok')"

# skill 同步到运行时目录 + 脚本存在
docker exec <容器> ls /root/.openharness/skills/pptx-to-html/scripts/convert_pptx_to_html_v2.py

# SKILL.md 路径已改、无云环境残留
docker exec <容器> grep -c "/root/.openharness/skills/pptx-to-html" /root/.openharness/skills/pptx-to-html/SKILL.md
docker exec <容器> grep -c "/mnt/skills/user\|/mnt/user-data" /root/.openharness/skills/pptx-to-html/SKILL.md  # 期望 0

# 跑一次真实转换
docker exec <容器> /root/.openharness-venv/bin/python \
  /root/.openharness/skills/pptx-to-html/scripts/convert_pptx_to_html_v2.py /path/to/test.pptx /tmp/out
```

---

## 9. 变更历史

| 日期 | 提交 | 内容 |
| --- | --- | --- |
| 2026-06-23 | `de72011` (v1.3) | 升级 HyperFrames skill 至 v0.7.2；QwenTTS 接入共享音频引擎 `tts.mjs`（最高优先级 provider） |
| 2026-06-24 | `4feb2ff` | skill 文档加 OpenHarness 运行时 Chrome 配置说明（`hyperframes-cli/SKILL.md` + `doctor-browser.md`） |
| 2026-06-25 | — | 接入 pptx-to-html skill：删 Dockerfile.fix 的 smithery 段、装 venv 依赖、SKILL.md 路径 / 脚本名 / Phase 2 能力描述适配（见第 8 节） |
