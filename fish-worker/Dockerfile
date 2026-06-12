FROM lmsysorg/sglang-omni:dev

ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG no_proxy
ARG NO_PROXY

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml ./
COPY uv.lock ./
COPY fish_worker ./fish_worker
COPY scripts ./scripts
COPY configs ./configs

RUN chmod +x scripts/*.sh \
    && if [ -n "${https_proxy:-${HTTPS_PROXY:-}}" ]; then git config --global https.proxy "${https_proxy:-${HTTPS_PROXY}}"; fi \
    && if [ -n "${http_proxy:-${HTTP_PROXY:-}}" ]; then git config --global http.proxy "${http_proxy:-${HTTP_PROXY}}"; fi \
    && uv venv --system-site-packages /app/.venv \
    && uv sync --no-dev \
    && git clone --depth 1 --filter=blob:none https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni \
    && printf 'protobuf>=6.31.1,<7.0.0\n' >/tmp/sglang-omni-overrides.txt \
    && uv pip install --no-config --python /app/.venv/bin/python --overrides /tmp/sglang-omni-overrides.txt -v -e /opt/sglang-omni \
    && (git config --global --unset-all https.proxy || true) \
    && (git config --global --unset-all http.proxy || true) \
    && rm -f /tmp/sglang-omni-overrides.txt

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

CMD ["./scripts/start.sh"]
