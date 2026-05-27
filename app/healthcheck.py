"""Health check script for Docker HEALTHCHECK directive."""
import sys
import urllib.request

try:
    urllib.request.urlopen("http://localhost:8080/health")
except Exception:
    sys.exit(1)
