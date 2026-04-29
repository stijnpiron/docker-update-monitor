FROM python:3.13-slim

LABEL org.opencontainers.image.title="Docker Update Monitor"
LABEL org.opencontainers.image.description="Monitors Docker containers for image updates and notifies a webhook"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# Run as non-root but still needs socket access → add to group via docker-compose
USER nobody

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["python", "-u", "-m", "app.main"]
