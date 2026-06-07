FROM lmsysorg/sglang-omni:dev

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml ./
COPY uv.lock ./
COPY fish_worker ./fish_worker
COPY scripts ./scripts
COPY configs ./configs

RUN chmod +x scripts/*.sh && uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

CMD ["./scripts/start.sh"]
