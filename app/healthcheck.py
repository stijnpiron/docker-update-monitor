"""Health check script for Docker HEALTHCHECK directive."""
import sys
import urllib.request
import urllib.error

try:
    urllib.request.urlopen("http://localhost:8080/health")
except urllib.error.HTTPError:
    pass  # 503 is fine — server is up, just no scan completed yet
except Exception:
    sys.exit(1)
