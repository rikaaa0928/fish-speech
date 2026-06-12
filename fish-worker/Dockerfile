FROM lmsysorg/sglang-omni:dev

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
    && uv venv --system-site-packages /app/.venv \
    && uv sync --no-dev \
    && git clone --depth 1 --filter=blob:none https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni \
    && printf 'protobuf>=6.31.1,<7.0.0\n' >/tmp/sglang-omni-overrides.txt \
    && uv pip install --no-config --python /app/.venv/bin/python --overrides /tmp/sglang-omni-overrides.txt -v -e /opt/sglang-omni \
    && rm -f /tmp/sglang-omni-overrides.txt

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

CMD ["./scripts/start.sh"]
