ARG CUDA_VER=12.8.1
ARG UBUNTU_VER=22.04
FROM nvidia/cuda:${CUDA_VER}-cudnn-runtime-ubuntu${UBUNTU_VER}

ARG APT_UBUNTU_MIRROR=https://mirrors.aliyun.com/ubuntu
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple
ARG UV_HTTP_TIMEOUT=300
ARG UV_EXTRA=cu128

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' >/etc/apt/apt.conf.d/keep-cache \
    && sed -i -E "s#https?://(archive|security).ubuntu.com/ubuntu#${APT_UBUNTU_MIRROR}#g" /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        aria2 \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        ffmpeg \
        git \
        libasound-dev \
        libportaudio2 \
        libportaudiocpp0 \
        libsndfile1 \
        libsox-dev \
        libssl-dev \
        pkg-config \
        portaudio19-dev \
        python3-dev \
        python3-pip \
        python3-venv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir -i "${PIP_INDEX_URL}" uv

COPY pyproject.toml uv.lock README.md .project-root ./

RUN --mount=type=cache,target=/root/.cache/uv \
    UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" UV_INDEX_URL="${UV_INDEX_URL}" UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX}" \
    uv sync --extra "${UV_EXTRA}" --frozen --no-dev --no-install-project

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    chmod +x fish-worker/scripts/*.sh \
    && UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" UV_INDEX_URL="${UV_INDEX_URL}" UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX}" \
       uv sync --extra "${UV_EXTRA}" --frozen --no-dev \
    && UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" UV_INDEX_URL="${UV_INDEX_URL}" UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX}" \
       uv pip install --python /app/.venv/bin/python -e /app/fish-worker

WORKDIR /app/fish-worker

CMD ["./scripts/start.sh"]
