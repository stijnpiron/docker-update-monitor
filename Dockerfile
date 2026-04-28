FROM python:3.13-slim

LABEL org.opencontainers.image.title="Docker Update Monitor"
LABEL org.opencontainers.image.description="Monitors Docker containers for image updates and notifies a webhook"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

# Run as non-root but still needs socket access → add to group via docker-compose
USER nobody

ENTRYPOINT ["python", "-u", "monitor.py"]
