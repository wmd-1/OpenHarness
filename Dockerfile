# ============================================================================
# OpenHarness Dockerfile — UV + pip install openharness-ai
# ============================================================================
# 基于 README Quick Start 安装方式，使用 UV 加速依赖安装
#
# 构建命令：
#   docker build -t openharness:latest .
#
# docker compose 使用：
#   docker compose up
# ============================================================================
ARG PYTHON_VERSION=3.11

# ---- 阶段 1：安装 UV ----
FROM python:${PYTHON_VERSION}-slim AS uv-installer

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# ---- 阶段 2：构建应用镜像 ----
FROM python:${PYTHON_VERSION}-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ---- 从 uv-installer 复制 UV ----
COPY --from=uv-installer /bin/uv /bin/uv /usr/local/bin/

# ---- apt 安装重试辅助函数 ----
RUN printf '#!/bin/bash\nset -e\nfor i in 1 2 3; do\n  apt-get install -y --no-install-recommends --fix-missing "$@" && exit 0\n  echo "Retry $i: apt-get install failed, updating lists..."\n  apt-get update\n  sleep 2\ndone\nexit 1\n' > /usr/local/bin/apt-retry && chmod +x /usr/local/bin/apt-retry

# ---- 全部系统依赖（一次 apt-get update，一次清理）----
# 合并：基础工具 + Chrome 运行时 + Node.js + FFmpeg + TTS/STT 编译 + unzip + supervisord
RUN apt-get update && apt-retry \
        ca-certificates curl git bash \
        build-essential cmake \
        openssh-client vim-tiny htop locales \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 \
        fonts-noto-cjk ripgrep \
        ffmpeg unzip \
        espeak-ng libsndfile1 \
        supervisor \
    && sed -i '/C.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-retry nodejs \
    && npm install -g npm@latest \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ---- Hyperframes（AI 视频生成）+ Chrome Headless Shell ----
# 锁定版本，禁用自动更新
ENV HYPERFRAMES_AUTO_UPDATE=false \
    HYPERFRAMES_NO_AUTO_INSTALL=1 \
    HYPERFRAMES_NO_UPDATE_CHECK=1
RUN npm install -g hyperframes@0.6.102 \
    && npx skills add heygen-com/hyperframes
# 预装 hyperframes pinned bundled chrome：运行时 `browser ensure`/`doctor` 只认 bundled
# chrome（不读 PRODUCER_HEADLESS_SHELL_PATH），空缓存会在第一次跑 skill 时触发 ~150MB
# 下载并卡住。build 时一次性下载烧进镜像，运行时 ensure 即 find 到、no-op。
# 临时 HYPERFRAMES_NO_AUTO_INSTALL=0 确保显式 ensure 能下载（运行时 ENV 的 =1 不动）。
RUN HYPERFRAMES_NO_AUTO_INSTALL=0 npx hyperframes browser ensure
# Chrome Headless Shell（从本地 docker/chrome/ 安装）
# 预先下载：https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json
COPY docker/chrome/chrome-headless-shell-linux64.zip /tmp/chrome-headless-shell-linux64.zip
RUN unzip /tmp/chrome-headless-shell-linux64.zip -d /opt/ \
    && ln -s /opt/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chrome-headless-shell \
    && rm /tmp/chrome-headless-shell-linux64.zip

# ---- Python 虚拟环境 + 全部 pip 依赖（一次安装）----
# 合并：openharness-ai + Kokoro TTS + FastAPI 视频服务
RUN python -m venv /root/.openharness-venv \
    && /root/.openharness-venv/bin/pip install --upgrade pip \
    && uv pip install --python /root/.openharness-venv/bin/python \
        openharness-ai \
        kokoro-onnx soundfile \
        fastapi==0.115.* uvicorn[standard]==0.32.* \
        sqlalchemy[asyncio]==2.0.* asyncpg==0.30.* psycopg[binary]==3.2.* \
        alembic==1.14.* \
        celery[redis]==5.4.* redis==5.2.* \
        pydantic-settings==2.6.* sse-starlette==2.1.* python-multipart
# ElevenLabs 云端 TTS（可选，需 API Key，按需取消注释）：
# RUN uv pip install --python /root/.openharness-venv/bin/python elevenlabs

# ---- 环境变量 + 目录结构 ----
ENV PATH="/root/.local/bin:/root/.openharness-venv/bin:${PATH}" \
    CHROME_HEADLESS_BIN=/opt/chrome-headless-shell-linux64/chrome-headless-shell \
    PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome-headless-shell-linux64/chrome-headless-shell \
    PYTHONPATH=/app/src \
    OPENHARNESS_PERMISSION_MODE=full_auto
WORKDIR /app
RUN mkdir -p /root/.openharness/skills /root/.openharness/plugins \
    /root/.local/bin /var/openharness/videos /workspaces

# ---- 复制 hyperframes_github_skills 到镜像内置路径（不受命名卷遮蔽）----
# 运行时由 wrapper 脚本同步到 /root/.openharness/skills/（卷挂载点）
COPY hyperframes_github_skills/ /opt/oh-skills-builtin/

# ---- 命令 Wrapper（强制注入 full_auto 最高权限 + skills 同步）+ oh-serve ----
# 每次启动时 cp -a 将镜像内置 skills 同步到命名卷，确保重建镜像后 skills 自动更新
RUN printf '#!/bin/bash\ncp -a /opt/oh-skills-builtin/. /root/.openharness/skills/ 2>/dev/null || true\nexec /root/.openharness-venv/bin/oh --permission-mode full_auto "$@"\n' > /root/.local/bin/oh \
    && printf '#!/bin/bash\ncp -a /opt/oh-skills-builtin/. /root/.openharness/skills/ 2>/dev/null || true\nexec /root/.openharness-venv/bin/ohmo --permission-mode full_auto "$@"\n' > /root/.local/bin/ohmo \
    && printf '#!/bin/bash\ncp -a /opt/oh-skills-builtin/. /root/.openharness/skills/ 2>/dev/null || true\nexec /root/.openharness-venv/bin/openharness --permission-mode full_auto "$@"\n' > /root/.local/bin/openharness \
    && printf '#!/bin/bash\nexec /usr/bin/supervisord -c /etc/supervisor/conf.d/oh-service.conf\n' > /usr/local/bin/oh-serve \
    && chmod +x /root/.local/bin/oh /root/.local/bin/ohmo /root/.local/bin/openharness /usr/local/bin/oh-serve
EXPOSE 8000

# ---- [模块 D] 预下载 Kokoro TTS 模型（离线必需，~338 MB）----
RUN mkdir -p /root/.cache/hyperframes/tts/models \
             /root/.cache/hyperframes/tts/voices \
    && curl -Lo /root/.cache/hyperframes/tts/models/kokoro-v1.0.onnx \
       "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" \
    && curl -Lo /root/.cache/hyperframes/tts/voices/voices-v1.0.bin \
       "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

# ---- [模块 E] 预构建 whisper.cpp + 下载模型 + 清理编译残留 ----
RUN mkdir -p /root/.cache/hyperframes/whisper \
    && cd /root/.cache/hyperframes/whisper \
    && git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git \
    && cd whisper.cpp \
    && cmake -B build \
    && cmake --build build --config Release -j$(nproc) \
    && ln -s /root/.cache/hyperframes/whisper/whisper.cpp/build/bin/whisper-cli \
             /usr/local/bin/whisper-cli \
    && cd /root/.cache/hyperframes/whisper \
    && rm -rf whisper.cpp/.git whisper.cpp/examples whisper.cpp/tests \
              whisper.cpp/models whisper.cpp/build/CMakeFiles \
    && mkdir -p /root/.cache/hyperframes/whisper/models \
    && curl -Lo /root/.cache/hyperframes/whisper/models/ggml-small.en.bin \
       "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
# 背景移除模型（可选，~168 MB，按需取消注释）：
# RUN mkdir -p /root/.cache/hyperframes/background-removal/models \
#     && curl -Lo /root/.cache/hyperframes/background-removal/models/u2net_human_seg.onnx \
#        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net_human_seg.onnx"

# ---- FastAPI 服务 + supervisord 配置 ----
COPY service /opt/oh-service
ENV PYTHONPATH=/app/src:/opt/oh-service
COPY docker/supervisord.conf /etc/supervisor/conf.d/oh-service.conf

ENTRYPOINT ["oh"]
CMD ["--help"]
