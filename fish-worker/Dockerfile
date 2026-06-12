FROM lmsysorg/sglang-omni:dev

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG UV_HTTP_TIMEOUT=300
ARG FLASHINFER_VERSION=0.6.1
ARG FLASHINFER_CUDA_SUFFIX=cu129
ARG GIT_HTTP_PROXY
ARG GIT_HTTPS_PROXY

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir -i "${PIP_INDEX_URL}" uv

COPY pyproject.toml ./
COPY uv.lock ./
COPY fish_worker ./fish_worker
COPY scripts ./scripts
COPY configs ./configs

RUN chmod +x scripts/*.sh \
    && uv venv --system-site-packages /app/.venv \
    && UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" UV_INDEX_URL="${UV_INDEX_URL}" uv sync --no-dev \
    && if [ -n "${GIT_HTTPS_PROXY:-}${GIT_HTTP_PROXY:-}" ]; then git -c https.proxy="${GIT_HTTPS_PROXY:-}" -c http.proxy="${GIT_HTTP_PROXY:-}" clone --depth 1 --filter=blob:none https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni; else git clone --depth 1 --filter=blob:none https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni; fi \
    && printf 'protobuf>=6.31.1,<7.0.0\n' >/tmp/sglang-omni-overrides.txt \
    && UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" UV_INDEX_URL="${UV_INDEX_URL}" uv pip install --no-config --python /app/.venv/bin/python --overrides /tmp/sglang-omni-overrides.txt -v -e /opt/sglang-omni \
    && UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" UV_INDEX_URL="${UV_INDEX_URL}" uv pip install --no-config --python /app/.venv/bin/python --no-deps "flashinfer-python==${FLASHINFER_VERSION}" "flashinfer-cubin==${FLASHINFER_VERSION}" \
    && UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT}" uv pip install --no-config --python /app/.venv/bin/python --no-deps --index-url "https://flashinfer.ai/whl/${FLASHINFER_CUDA_SUFFIX}" "flashinfer-jit-cache==${FLASHINFER_VERSION}+${FLASHINFER_CUDA_SUFFIX}" \
    && rm -f /tmp/sglang-omni-overrides.txt

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

CMD ["./scripts/start.sh"]
