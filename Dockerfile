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
ARG UV_VERSION=0.7.3

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

# ---- 系统依赖（基础）----
RUN apt-get update && apt-retry \
        ca-certificates curl git \
        bash build-essential \
        openssh-client \
        vim-tiny htop \
        locales \
    && sed -i '/C.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ---- Chrome 运行时依赖 ----
RUN apt-get update && apt-retry \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 \
        fonts-noto-cjk \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ---- Node.js 22 + npm 最新版 ----
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-retry nodejs \
    && npm install -g npm@latest \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ---- FFmpeg ----
RUN apt-get update && apt-retry ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ---- Hyperframes（AI 视频生成）----
# 锁定版本 0.6.93，禁用自动更新
ENV HYPERFRAMES_AUTO_UPDATE=false \
    HYPERFRAMES_NO_AUTO_INSTALL=1 \
    HYPERFRAMES_NO_UPDATE_CHECK=1
RUN npm install -g hyperframes@0.6.93 \
    && npx skills add heygen-com/hyperframes
    
# ---- Chrome Headless Shell（从本地 docker/chrome/ 安装）----
# 预先下载 chrome-headless-shell-linux64.zip 放到 docker/chrome/ 目录：
#   curl -Lo docker/chrome/chrome-headless-shell-linux64.zip \
#     https://storage.googleapis.com/chrome-for-testing-public/<VERSION>/linux64/chrome-headless-shell-linux64.zip
# 版本查询：https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json
RUN apt-get update && apt-retry unzip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean
COPY docker/chrome/chrome-headless-shell-linux64.zip /tmp/chrome-headless-shell-linux64.zip
RUN unzip /tmp/chrome-headless-shell-linux64.zip -d /opt/ \
    && ln -s /opt/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chrome-headless-shell \
    && rm /tmp/chrome-headless-shell-linux64.zip

# ---- 创建虚拟环境并使用 UV pip 安装 openharness-ai ----
# 与 README "pip install openharness-ai" 安装方式一致，使用 UV 加速
RUN python -m venv /root/.openharness-venv \
    && /root/.openharness-venv/bin/pip install --upgrade pip \
    && uv pip install --python /root/.openharness-venv/bin/python \
        openharness-ai

# ---- 创建配置目录 ----
RUN mkdir -p /root/.openharness/skills \
    && mkdir -p /root/.openharness/plugins

# ---- 创建命令 Wrapper（强制注入 full_auto 最高权限）----
RUN mkdir -p /root/.local/bin \
    && printf '#!/bin/bash\nexec /root/.openharness-venv/bin/oh --permission-mode full_auto "$@"\n' > /root/.local/bin/oh \
    && printf '#!/bin/bash\nexec /root/.openharness-venv/bin/ohmo --permission-mode full_auto "$@"\n' > /root/.local/bin/ohmo \
    && printf '#!/bin/bash\nexec /root/.openharness-venv/bin/openharness --permission-mode full_auto "$@"\n' > /root/.local/bin/openharness \
    && chmod +x /root/.local/bin/oh /root/.local/bin/ohmo /root/.local/bin/openharness

# ---- 环境变量 ----
ENV PATH="/root/.local/bin:/root/.openharness-venv/bin:${PATH}" \
    CHROME_HEADLESS_BIN=/opt/chrome-headless-shell-linux64/chrome-headless-shell \
    PRODUCER_HEADLESS_SHELL_PATH=/opt/chrome-headless-shell-linux64/chrome-headless-shell

# ---- 源码挂载点 ----
WORKDIR /app

# PYTHONPATH 使挂载的源码优先于 pip 安装的包
# 容器启动时通过 -v ./src:/app/src 挂载，修改立即生效
ENV PYTHONPATH=/app/src

# ---- OpenHarness 最高权限 ----
# (环境变量保留以防后续版本支持)
ENV OPENHARNESS_PERMISSION_MODE=full_auto

ENTRYPOINT ["oh"]
CMD ["--help"]