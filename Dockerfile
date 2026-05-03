# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build tools only in this stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment and install dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy source and install into the venv (non-editable)
COPY . .
RUN pip install --no-cache-dir .

# ── Stage 2: Runtime ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy only the virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user with explicit group creation
RUN groupadd -g 1000 appuser && \
    useradd --create-home --shell /bin/bash --gid 1000 --uid 1000 appuser
USER appuser

EXPOSE 8080

# Use --proxy-headers if running behind a reverse proxy (Traefik, Nginx)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
