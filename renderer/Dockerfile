# PleatView Renderer: Chromium, a virtual display and ffmpeg, controlled by a small Python server.
# Only free software from Debian's main archive; /app-packages.txt (and GET /api/packages) lists
# every installed package and version, for audits and vulnerability scans.
# Not BUILD_FROM: Home Assistant's Supervisor sets that to its own Alpine base image.
ARG DEBIAN=debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
FROM ${DEBIAN}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium xvfb ffmpeg python3 python3-aiohttp python3-cryptography tini ca-certificates \
        fonts-dejavu-core fonts-noto-color-emoji mesa-va-drivers \
        $([ "$(dpkg --print-architecture)" = amd64 ] && echo intel-media-va-driver i965-va-driver) \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && useradd --create-home --shell /usr/sbin/nologin render \
    && dpkg-query -W -f '${Package} ${Version} ${Architecture}\n' | sort > /app-packages.txt

COPY pleatview_renderer /app/pleatview_renderer
WORKDIR /app
ENV PYTHONUNBUFFERED=1
EXPOSE 8780
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "pleatview_renderer"]
