# ── Base: install cargo-chef + musl toolchain ────────────────────────────────
FROM rust:1.95-slim AS base

ARG TARGETARCH
ARG TARGETVARIANT

# musl-tools provides the native musl-gcc for each platform (x86_64, aarch64, armhf).
# rusqlite's "bundled" feature compiles SQLite's C code, so a C compiler is required.
RUN apt-get update && apt-get install -y --no-install-recommends musl-tools \
    && rm -rf /var/lib/apt/lists/* \
    && cargo install cargo-chef --locked

WORKDIR /build

# Resolve and persist the musl target triple for use in later stages.
RUN set -ex; \
    case "${TARGETARCH}${TARGETVARIANT}" in \
    "amd64")   echo x86_64-unknown-linux-musl ;; \
    "arm64")   echo aarch64-unknown-linux-musl ;; \
    "armv7")   echo armv7-unknown-linux-musleabihf ;; \
    *)         echo x86_64-unknown-linux-musl ;; \
    esac > /TARGET && rustup target add "$(cat /TARGET)"

# ── Planner: compute the dependency recipe ───────────────────────────────────
FROM base AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

# ── Builder: cook deps (cached layer) then compile source ────────────────────
FROM base AS builder
COPY --from=planner /build/recipe.json recipe.json

# Cook dependencies — this layer is only invalidated when Cargo.lock changes.
RUN cargo chef cook --release --target "$(cat /TARGET)" --recipe-path recipe.json

# Now compile the real source — only your code, deps already built above.
COPY . .
RUN set -ex; \
    TARGET="$(cat /TARGET)"; \
    cargo build --release --target "${TARGET}"; \
    cp "target/${TARGET}/release/docker-update-monitor" /docker-update-monitor

# ── Runtime stage ─────────────────────────────────────────────────────────────
# gcr.io/distroless/static has no shell or package manager — minimal attack surface.
# Runs as root so bind-mounted data volumes work without manual chown on the host.
# (Running nonroot here is security theatre: Docker socket access already implies
# full host root access, and nonroot just causes write failures on bind mounts.)
FROM gcr.io/distroless/static

LABEL org.opencontainers.image.title="Docker Update Monitor"
LABEL org.opencontainers.image.description="Monitors Docker containers for image updates and notifies via webhook or email"

WORKDIR /app

COPY --from=builder /docker-update-monitor /app/monitor
COPY --from=builder /build/static/ /app/static/

EXPOSE 8080

ENTRYPOINT ["/app/monitor"]
