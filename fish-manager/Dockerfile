FROM rust:1.95-bookworm AS builder

WORKDIR /app
COPY Cargo.toml ./
COPY Cargo.lock ./
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim

WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/fish-manager /usr/local/bin/fish-manager

ENV MANAGER_BIND_ADDR=0.0.0.0:8080
EXPOSE 8080
CMD ["fish-manager"]
