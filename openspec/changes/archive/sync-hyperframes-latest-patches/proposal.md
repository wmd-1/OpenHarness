# Proposal: 同步 HyperFrames latest 并重新应用 OpenHarness 补丁

**Change ID:** `sync-hyperframes-latest-patches`
**Created:** 2026-07-08
**Status:** Draft

---

## Problem Statement

OpenHarness 在 HyperFrames 上游 skill 基础上做了两类定制（QwenTTS 本地 TTS 最高优先级 provider、Chrome 路径运行时说明），记录在 `docs/hyperframes-skill-openharness-patches.md`。上游近期发布新版 skill，关键变化是 **`hyperframes-media` 重命名为 `media-use`**，共享 TTS 库从 `hyperframes-media/scripts/lib/tts.mjs` 迁移到 `media-use/audio/scripts/lib/tts.mjs`。

当前 `hyperframes_github_skills/`（Docker 实际 COPY 的目录）的 QwenTTS 补丁仍打在**旧的** `hyperframes-media/scripts/lib/tts.mjs` 上，且含一个布局过时的 `media-use` 残留；而 `hyperframes_github_skills_latest/` 已是干净的新版基线（正确位置 `media-use/audio/scripts/lib/tts.mjs`，0 处 qwentts）。

若直接发布，QwenTTS 不会在新版音频引擎中生效，容器将回退 Kokoro。

## Proposed Solution

遵循 `docs/hyperframes-skill-openharness-patches.md` 第 2 节同步工作流：

1. 备份 `hyperframes_github_skills/` 到带时间戳的安全目录。
2. 用 `hyperframes_github_skills_latest/` **镜像覆盖** `hyperframes_github_skills/`（技能集合与 latest 一致：加入 `figma`/`hyperframes-keyframes`/新 `media-use`，移除已退休的 `hyperframes-media`/`graphic-overlays`/旧 `media-use`）。
3. 重新应用两类 OpenHarness 补丁，但 **QwenTTS 的目标路径适配 media-use 重命名**：
   - QwenTTS：`media-use/audio/scripts/lib/tts.mjs`（6 处注入）+ `media-use/audio/scripts/audio.mjs`（2 处注释）+ `media-use/SKILL.md` + `media-use/audio/references/tts.md`（文档）。
   - Chrome：`hyperframes-cli/SKILL.md` + `hyperframes-cli/references/doctor-browser.md`。
4. 验证补丁生效（§6 静态校验，路径改为 media-use）。
5. `Dockerfile.fix`/`docker-compose.yml`/`.env.example` 的 browser ensure 与版本标签 `v0.1.9_v0.7.20_v1.3_v2.0` 已在近期提交中就绪，仅做校验，不再改动。

## Scope

### In Scope
- `hyperframes_github_skills/` 整体镜像为 latest 基线。
- QwenTTS provider 在 `media-use/audio/scripts/lib/tts.mjs` 重新注入（最高优先级）。
- Chrome 路径说明在 `hyperframes-cli` 文档重新注入。
- 静态验证（node --check + grep 计数）。

### Out of Scope
- 实际 `docker build` 重建镜像（本环境无 Docker，且 §6.2 容器侧验证需运行中容器）。
- pptx-to-html 适配（文档第 8 节，独立目录 `pptx2html_github_skills/`，不在本次 latest 覆盖范围内）。
- 上游 v0.7.2+ 自带的结构性变化（faceless-explainer 重构、薄适配器音频引擎等）—— 拉新版即得，不手动重复（文档 §7）。

## Impact Analysis

| Component | Change Required | Details |
|-----------|-----------------|---------|
| hyperframes_github_skills/ | Yes | 整体镜像为 latest；随后重新注入 2 类补丁 |
| media-use/audio/scripts/lib/tts.mjs | Yes | 注入 QwenTTS（6 处） |
| media-use/audio/scripts/audio.mjs | Yes | 2 处注释 |
| media-use/SKILL.md | Yes | provider 文档 |
| media-use/audio/references/tts.md | Yes | QwenTTS 参考节 |
| hyperframes-cli/SKILL.md | Yes | render 步骤 callout |
| hyperframes-cli/references/doctor-browser.md | Yes | 3 处插入 |
| Dockerfile.fix / .env.example / docker-compose.yml | No (verify only) | 版本标签与 browser ensure 已在位 |

## Architecture Considerations

- 镜像构建链路不变：`hyperframes_github_skills/` → Docker COPY `/opt/oh-skills-builtin/` → wrapper `cp -a` → `/root/.openharness/skills/` → `oh` 加载。
- 补丁判定准则（文档 §7）：文件内容含 `qwen`/`QWENTTS` 才是 OpenHarness 定制，必须手动打回；其余 HeyGen/ElevenLabs/Kokoro/BGM/SFX 与技能集合差异均为上游自带，镜像覆盖即得。
- 文档 §3.2/§3.3 的路径（`hyperframes-media/...`）需整体映射到 `media-use/...`，代码块本身（QwenTTS 函数、常量）逐字保留。

## Success Criteria

- [ ] `hyperframes_github_skills/` 技能集合与 `hyperframes_github_skills_latest/` 一致（figma/hyperframes-keyframes 存在，hyperframes-media/graphic-overlays 不存在）。
- [ ] `media-use/audio/scripts/lib/tts.mjs` 含 QwenTTS（grep -ci qwentts ≈ 20），`node --check` 通过。
- [ ] `hyperframes-cli/references/doctor-browser.md` 含 "OpenHarness runtime note" callout。
- [ ] `Dockerfile.fix` 含 `npx hyperframes browser ensure`；`.env.example` 与 `docker-compose.yml` 版本标签为 `v0.1.9_v0.7.20_v1.3_v2.0`。
- [ ] 备份目录可随时用于回滚。

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| 镜像覆盖误删 OpenHarness 自定义技能 | Low | High | 备份整个 patched 目录到 `.bak.<ts>`；仅按文档判定准则删除上游已退休技能（无 qwen/OpenHarness 标记） |
| QwenTTS 注入点因上游结构微调而错位 | Med | High | 逐处比对 latest 的 tts.mjs 锚点（pickProvider/synthesizeOne/transcodeToWav 等均在位）；按"意图"适配 |

---

## Archive Information

**Archived:** 2026-07-08 17:30
**Outcome:** Successfully implemented

### Files Modified
- `hyperframes_github_skills/media-use/audio/scripts/lib/tts.mjs` — 6 处 QwenTTS 注入
- `hyperframes_github_skills/media-use/audio/scripts/audio.mjs` — 2 处注释
- `hyperframes_github_skills/media-use/SKILL.md` — provider 文档
- `hyperframes_github_skills/media-use/audio/references/tts.md` — QwenTTS 参考节
- `hyperframes_github_skills/hyperframes-cli/SKILL.md` — Render callout
- `hyperframes_github_skills/hyperframes-cli/references/doctor-browser.md` — 3 处 Chrome 说明
- （`hyperframes_github_skills/` 整体镜像自 `hyperframes_github_skills_latest/`）

### Specs Updated
- `openspec/specs/media-use-tts.md` — 新建，记录 QwenTTS 最高优先级 provider 需求

### Notes
- 备份目录：`hyperframes_github_skills.bak.20260708_170149`（回滚用）
- `Dockerfile.fix` / `.env.example` / `docker-compose.yml` 的 browser ensure 与版本标签已在近期提交就位，本次仅校验未改动
| media-use 重命名导致文档路径未同步 | Med | Med | 所有 §3/§4 目标路径统一映射到 media-use；验证时 grep 用新路径 |
