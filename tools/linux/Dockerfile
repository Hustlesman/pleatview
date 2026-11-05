# Runs and packages PleatView for Linux without installing anything on this machine:
# Node, Electron's libraries, a virtual screen (Xvfb) for tests, and tools for screenshots.
FROM node:22-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgtk-3-0 libnss3 libxss1 libxtst6 libasound2 libgbm1 libdrm2 libxshmfence1 \
      xvfb xauth imagemagick fonts-dejavu-core fonts-noto-color-emoji dbus-x11 fakeroot dpkg-dev \
    && rm -rf /var/lib/apt/lists/*
USER node
WORKDIR /app
