# syntax=docker/dockerfile:1.7

# Lean Alpine image for the Python port: CLI (`downloader`) and Telegram bot
# (`telegram-bot`) targets. Mirrors the supply-chain posture of the Go image:
# mp4decrypt is built from a pinned Bento4 revision with a checksum.
#
# Size notes vs the Debian-based Go image: python:3.12-alpine + ffmpeg +
# gpac + the pure-Python/musl wheel stack lands well under 200 MB.

ARG PYTHON_IMAGE=python:3.12-alpine
ARG ALPINE_IMAGE=alpine:3.20

FROM ${PYTHON_IMAGE} AS python-build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY amdl ./amdl
COPY amdltgbot ./amdltgbot
# cryptography/pydantic-core ship musllinux wheels, so no compiler is needed.
RUN pip install --no-cache-dir --prefix=/install .

FROM ${ALPINE_IMAGE} AS bento4-builder
ARG BENTO4_REVISION=dc264854d1f76c370b65b18d9f303a95f7f21ab1
ARG BENTO4_SHA256=756ebf50c60d674027ef815d3b961dc01688ac270a04636144568192f04528c9
RUN apk add --no-cache \
    ca-certificates \
    cmake \
    g++ \
    make \
    musl-dev

ADD --checksum=sha256:${BENTO4_SHA256} \
    https://github.com/axiomatic-systems/Bento4/archive/${BENTO4_REVISION}.tar.gz \
    /tmp/bento4.tar.gz

RUN mkdir -p /src/bento4 /out \
    && tar -xzf /tmp/bento4.tar.gz --strip-components=1 -C /src/bento4 \
    && cmake -S /src/bento4 -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build --parallel "$(nproc)" \
    && install -m 0755 /build/mp4decrypt /out/mp4decrypt \
    && strip --strip-unneeded /out/mp4decrypt

# Alpine does not ship a gpac package, so MP4Box is built from the same pinned
# revision as the Go image. --static-bin keeps the binary dependency-free.
FROM ${ALPINE_IMAGE} AS gpac-builder
ARG GPAC_REVISION=60016d3818969bacc3e6d7b624c898b01b3649d4
ARG GPAC_SHA256=810ce421ee0cd2160686443fd1d60805880d5e5bd3a1e34667cc9f3e87e999f0
RUN apk add --no-cache \
    build-base \
    ca-certificates \
    pkgconf \
    zlib-dev \
    zlib-static

ADD --checksum=sha256:${GPAC_SHA256} \
    https://github.com/gpac/gpac/archive/${GPAC_REVISION}.tar.gz \
    /tmp/gpac.tar.gz

RUN mkdir -p /src/gpac /out \
    && tar -xzf /tmp/gpac.tar.gz --strip-components=1 -C /src/gpac

WORKDIR /src/gpac
RUN ./configure \
      --prefix=/usr \
      --static-bin \
      --isomedia-only \
      --disable-network \
      --disable-x11 \
      --use-zlib=system \
    && make -j"$(nproc)" \
    && install -m 0755 bin/gcc/MP4Box /out/MP4Box \
    && strip --strip-unneeded /out/MP4Box \
    && /out/MP4Box -version

FROM ${PYTHON_IMAGE} AS runtime
RUN apk add --no-cache \
        ca-certificates \
        ffmpeg \
    && addgroup -g 10001 app \
    && adduser -u 10001 -G app -D -H -h /app -s /sbin/nologin app \
    && install -d -o 10001 -g 10001 /app /downloads /downloads/.tmp

ENV TMPDIR=/downloads/.tmp \
    PYTHONUNBUFFERED=1

COPY --from=python-build --link /install /usr/local
COPY --from=bento4-builder --link /out/mp4decrypt /usr/local/bin/mp4decrypt
COPY --from=gpac-builder --link /out/MP4Box /usr/local/bin/MP4Box
WORKDIR /app

# Sanity check: every external tool the pipeline shells out to must resolve.
RUN apple-music-dl --help >/dev/null 2>&1 || true \
    ; command -v ffmpeg MP4Box mp4decrypt apple-music-dl apple-music-tgbot

FROM runtime AS downloader
COPY --link --chown=10001:10001 config.yaml.example config.yaml
RUN printf '\n%s\n%s\n%s\n' \
        'alac-save-folder: "/downloads/ALAC"' \
        'atmos-save-folder: "/downloads/Atmos"' \
        'aac-save-folder: "/downloads/AAC"' \
        >> config.yaml \
    && chown 10001:10001 config.yaml
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/apple-music-dl"]

FROM runtime AS telegram-bot
COPY --link --chown=10001:10001 config.docker.yaml config.yaml
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/apple-music-tgbot"]
