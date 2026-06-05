FROM lmsysorg/sglang-omni:dev

WORKDIR /app

RUN python -m pip install --no-cache-dir uv

COPY pyproject.toml ./
COPY fish_worker ./fish_worker
COPY scripts ./scripts

RUN chmod +x scripts/*.sh && uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

CMD ["./scripts/start.sh"]
