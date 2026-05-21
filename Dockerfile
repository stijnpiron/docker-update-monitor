# ── Build stage ───────────────────────────────────────────────────────────────
FROM rust:1.95-slim AS builder

ARG TARGETARCH
ARG TARGETVARIANT

# musl-tools provides the native musl-gcc for each platform (x86_64, aarch64, armhf).
# rusqlite's "bundled" feature compiles SQLite's C code, so a C compiler is required.
RUN apt-get update && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Resolve the musl target once so both dependency and source build steps share
# the same TARGET value without repeating the case statement.
RUN set -ex; \
    case "${TARGETARCH}${TARGETVARIANT}" in \
    "amd64")   echo x86_64-unknown-linux-musl ;; \
    "arm64")   echo aarch64-unknown-linux-musl ;; \
    "armv7")   echo armv7-unknown-linux-musleabihf ;; \
    *)         echo x86_64-unknown-linux-musl ;; \
    esac > /TARGET && rustup target add "$(cat /TARGET)"

# Copy manifests first and build a stub binary to compile all dependencies.
# This layer is cached as long as Cargo.toml / Cargo.lock don't change, so
# only your source code recompiles on typical code changes.
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
RUN set -ex; \
    TARGET="$(cat /TARGET)"; \
    mkdir -p src && echo 'fn main(){}' > src/main.rs; \
    cargo build --release --target "${TARGET}"; \
    rm -rf src target/*/release/.fingerprint/docker-update-monitor-*

# Now copy the real source and build — only your code is compiled.
COPY src/ src/
COPY tests/ tests/
COPY static/ static/
COPY templates/ templates/

RUN set -ex; \
    TARGET="$(cat /TARGET)"; \
    cargo build --release --target "${TARGET}"; \
    cp "target/${TARGET}/release/docker-update-monitor" /docker-update-monitor

# ── Runtime stage ─────────────────────────────────────────────────────────────
# gcr.io/distroless/static:nonroot runs as UID 65532 (nonroot) with no shell.
# The binary must be fully static (musl) — no glibc dependency.
FROM gcr.io/distroless/static:nonroot

LABEL org.opencontainers.image.title="Docker Update Monitor"
LABEL org.opencontainers.image.description="Monitors Docker containers for image updates and notifies via webhook or email"

WORKDIR /app

COPY --from=builder /docker-update-monitor /app/monitor
# Static assets (CSS/JS) served at runtime via tower-http ServeDir.
COPY --from=builder /build/static/ /app/static/

EXPOSE 8080

ENTRYPOINT ["/app/monitor"]
