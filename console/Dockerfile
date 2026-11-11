# PleatView Console: a small Python server and its web app (docs/console.md).
# Only free software from Debian's main archive, approved on 11 Nov 2026; /app-packages.txt (and
# GET /api/packages, for administrators) lists every installed package and version, for audits
# and vulnerability scans.
ARG DEBIAN=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
FROM ${DEBIAN}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-aiohttp python3-cryptography ffmpeg tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --home-dir /data --shell /usr/sbin/nologin console \
    && mkdir -p /data && chown console:console /data && chmod 700 /data \
    && dpkg-query -W -f '${Package} ${Version} ${Architecture}\n' | sort > /app-packages.txt

COPY pleatview_console /app/pleatview_console
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
USER console
VOLUME /data
EXPOSE 8790
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "pleatview_console"]
