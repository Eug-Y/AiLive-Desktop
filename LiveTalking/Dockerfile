# LiveTalking Docker Image
# 数字人实时交互流式引擎
#
# 构建: docker build -t livetalking .
# 运行: docker run --gpus all -p 8010:8010 -p 1-65536:1-65536/udp livetalking

ARG BASE_IMAGE=nvidia/cuda:12.4.0-cudnn-devel-ubuntu22.04
FROM $BASE_IMAGE

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# ── 系统依赖 ──────────────────────────────────────────────────────────
RUN apt-get update -yq --fix-missing \
    && apt-get install -yq --no-install-recommends \
        pkg-config wget cmake curl git vim \
        ffmpeg libsndfile1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python 环境 ────────────────────────────────────────────────────────
RUN wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh \
    && sh Miniconda3-latest-Linux-x86_64.sh -b -u -p /opt/miniconda3 \
    && rm Miniconda3-latest-Linux-x86_64.sh \
    && /opt/miniconda3/bin/conda init bash

ENV PATH="/opt/miniconda3/bin:$PATH"

# ── 项目依赖 ──────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── 项目代码 ──────────────────────────────────────────────────────────
COPY . .

# ── 健康检查 ──────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -sf http://127.0.0.1:8010/health || exit 1

# ── 启动 ──────────────────────────────────────────────────────────────
EXPOSE 8010
CMD ["python3", "app.py", "--transport", "webrtc", "--model", "wav2lip", \
     "--avatar_id", "wav2lip256_avatar1", "--tts", "edgetts"]