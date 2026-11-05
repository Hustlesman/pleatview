#!/bin/sh
# Runs PleatView for Linux on a virtual 1920x1080 screen (Xvfb), for tests and screenshots.
# Chromium in a container has no GPU: it draws with SwiftShader.
set -e
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99  # left behind when the container restarts
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
for _ in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
npm run --silent assets
exec ./node_modules/.bin/electron . --no-sandbox --use-gl=angle --use-angle=swiftshader "$@"
