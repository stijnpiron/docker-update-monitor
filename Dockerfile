# ── Build stage ───────────────────────────────────────────────────────────────
FROM rust:1.82-slim AS builder

ARG TARGETARCH
ARG TARGETVARIANT

# musl-tools provides the native musl-gcc for each platform (x86_64, aarch64, armhf).
# rusqlite's "bundled" feature compiles SQLite's C code, so a C compiler is required.
RUN apt-get update && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY . .

RUN set -ex; \
    case "${TARGETARCH}${TARGETVARIANT}" in \
    "amd64")   TARGET=x86_64-unknown-linux-musl ;; \
    "arm64")   TARGET=aarch64-unknown-linux-musl ;; \
    "armv7")   TARGET=armv7-unknown-linux-musleabihf ;; \
    *)         TARGET=x86_64-unknown-linux-musl ;; \
    esac; \
    rustup target add "${TARGET}"; \
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
