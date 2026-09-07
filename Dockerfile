FROM python:3.14-slim

LABEL org.opencontainers.image.title="Docker Update Monitor"
LABEL org.opencontainers.image.description="Monitors Docker containers for image updates and notifies a webhook"

WORKDIR /app

# Install the OpenSSH client. DockerClient(use_ssh_client=True) shells out to
# the system `ssh` binary to reach the remote daemons listed in DOCKER_HOSTS.
# The slim base image ships no SSH client, so add it here. The install is
# harmless (simply unused) when DOCKER_HOSTS is unset, so this one image serves
# both single-host and multi-host deployments.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

# OpenSSH resolves the *user* config file (host aliases -> per-host keys) from the
# running user's passwd home directory, NOT the $HOME env var, and docker-py's
# use_ssh_client=True just invokes `ssh` with no -F flag. The container runs as
# `nobody`, whose default home is /nonexistent, so a mounted /home/ssh/.ssh/config
# would never be found by default. Point nobody's home at /home/ssh (the directory
# the compose mounts the config into) so the client picks it up. Pure metadata,
# harmless when multi-host SSH is disabled.
RUN usermod -d /home/ssh nobody

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# Run as non-root but still needs socket access → add to group via docker-compose.
# With multi-host SSH the container also reads a shared read-only ~/.ssh/config
# (mounted at /home/ssh/.ssh, with HOME=/home/ssh) plus one Docker-secret key
# per host at /run/secrets/... — all of those files must be readable by this
# user (see docker-compose.yml).
USER nobody

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
  CMD python -m app.healthcheck

ENTRYPOINT ["python", "-u", "-m", "app.main"]
