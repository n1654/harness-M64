# syntax=docker/dockerfile:1.6

# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build && \
    pip wheel --no-deps --wheel-dir /wheels .

# ---- runtime ----
FROM python:3.12-slim AS runtime
WORKDIR /app

# Non-root user
RUN useradd --create-home --uid 1000 harness
USER harness

ENV PYTHONUNBUFFERED=1 \
    HARNESS_HOME=/app \
    HARNESS_MEMORY_DIR=/app/memory \
    HARNESS_TOOLS_DIR=/app/tools \
    HARNESS_STATE_DIR=/app/state

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --user /wheels/*.whl

# Ports (also documented in docker-compose)
EXPOSE 8080 9090 7000

# Mount points for operator content
VOLUME ["/app/memory", "/app/tools", "/app/state"]

ENTRYPOINT ["python", "-m", "harness"]
