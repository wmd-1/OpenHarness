# Implementation Tasks: 同步 HyperFrames latest 并重新应用 OpenHarness 补丁

**Change ID:** `sync-hyperframes-latest-patches`

---

## Phase 1: 基线同步（mirror latest → patched）

- [x] 1.1 备份 `hyperframes_github_skills/` 到 `hyperframes_github_skills.bak.20260708_170149`
- [x] 1.2 用 `hyperframes_github_skills_latest/` 镜像覆盖 patched（`cp -a` + 先删后拷），技能集合与 latest 一致
- [x] 1.3 校验技能集合：figma/hyperframes-keyframes 存在；hyperframes-media/graphic-overlays 不存在

**Quality Gate:**
- [x] 镜像后 patched 与 latest 的顶层技能目录集合一致
- [x] 备份目录完整可回滚

---

## Phase 2: 重新应用 QwenTTS 补丁（适配 media-use）

- [x] 2.1 `media-use/audio/scripts/lib/tts.mjs` 顶部 provider chain 注释加 QwenTTS（注入点①）
- [x] 2.2 加 `qwenttsAvailable()` 检测函数（注入点②）
- [x] 2.3 `pickProvider()` 白名单+校验+链首（注入点③）
- [x] 2.4 `resolveVoiceId()` 加 qwentts 分支（注入点④）
- [x] 2.5 `synthesizeOne()` 加 qwentts 分发（注入点⑤）
- [x] 2.6 加 `QWENTTS_LANG_FULL_NAME` 常量 + `synthesizeQwenTTS()` 实现（注入点⑥）
- [x] 2.7 `media-use/audio/scripts/audio.mjs` 2 处注释
- [x] 2.8 `media-use/SKILL.md` provider 文档（description/QwenTTS 例外/provider 表首行）
- [x] 2.9 `media-use/audio/references/tts.md` QwenTTS 参考节

**Quality Gate:**
- [x] `node --check media-use/audio/scripts/lib/tts.mjs` 通过
- [x] `node --check media-use/audio/scripts/audio.mjs` 通过
- [x] grep -ci qwentts tts.mjs = 20

---

## Phase 3: 重新应用 Chrome 路径补丁

- [x] 3.1 `hyperframes-cli/SKILL.md` Render 步骤插入 OpenHarness runtime callout（§4.3）
- [x] 3.2 `hyperframes-cli/references/doctor-browser.md` 顶部 callout（§4.4①）
- [x] 3.3 `doctor-browser.md` 新增 `## Using a specific Chrome for render` 段（§4.4②）
- [x] 3.4 `doctor-browser.md` Common issues 加 caveat（§4.4③）

**Quality Gate:**
- [x] grep "OpenHarness runtime note" doctor-browser.md = 1
- [x] grep "OpenHarness runtime" hyperframes-cli/SKILL.md ≥ 1

---

## Phase 4: 验证与构建配置确认

- [x] 4.1 静态校验（§6.1，路径改 media-use）：node --check + grep 计数全部通过
- [x] 4.2 确认 `Dockerfile.fix` 含 `npx hyperframes browser ensure`（已在位）
- [x] 4.3 确认 `.env.example` 与 `docker-compose.yml` 版本标签 `v0.1.9_v0.7.20_v1.3_v2.0`（已在位）

**Quality Gate:**
- [x] 全部 grep/node --check 通过
- [x] 构建配置标签一致

---

## Completion Checklist

- [x] 四个 Phase 完成
- [x] 所有 Quality Gate 通过
- [x] 备份目录保留供回滚
- [x] 执行 `/openspec-archive sync-hyperframes-latest-patches`
