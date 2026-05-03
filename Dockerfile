FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml ./

RUN pip install --no-cache-dir -e .

# Copy application
COPY app/ ./app/

# Non-root user
RUN groupadd -g 1000 appuser && \
    useradd --create-home --shell /bin/bash --gid 1000 --uid 1000 appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
