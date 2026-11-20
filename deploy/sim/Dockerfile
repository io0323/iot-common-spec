# 共通シミュレータ（iotspec-sim）のイメージ
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY schemas ./schemas
COPY profiles ./profiles
COPY sim ./sim
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["iotspec-sim"]
CMD ["run", "normal_day"]
