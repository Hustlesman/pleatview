#!/usr/bin/env bash
# Builds the PleatView APK inside Docker, so no local Android SDK is needed.
#
#   tools/build-apk.sh                 # debug APK + unit tests
#   tools/build-apk.sh assembleRelease # any Gradle tasks
#
# Release signing: set PLEATVIEW_KEYSTORE (path to the keystore, anywhere on disk),
# PLEATVIEW_KEYSTORE_PASSWORD, PLEATVIEW_KEY_ALIAS and PLEATVIEW_KEY_PASSWORD.
#
# Output: android/app/build/outputs/apk/<variant>/
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
image="pleatview-android-build"
tasks=("$@")
[ ${#tasks[@]} -eq 0 ] && tasks=(testDebugUnitTest assembleDebug)

docker build -q -t "$image" "$root/tools/android-build" > /dev/null
# ~/.android holds the debug signing key; keeping it means new builds can update an
# installed APK (a different key forces an uninstall, which wipes pairing and settings).
mkdir -p "$HOME/.gradle" "$HOME/.android"

# The whole repository is mounted: unit tests read examples shared with the other apps
# (tests/fixtures). The keystore lives outside the repository; mount its folder read-only.
keystore=()
if [ -n "${PLEATVIEW_KEYSTORE:-}" ]; then
  dir="$(cd "$(dirname "$PLEATVIEW_KEYSTORE")" && pwd)"
  keystore=(-v "$dir:/keystore:ro" -e "PLEATVIEW_KEYSTORE=/keystore/$(basename "$PLEATVIEW_KEYSTORE")")
fi

exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/home -e GRADLE_USER_HOME=/gradle -e ANDROID_USER_HOME=/tmp/home/.android \
  -e PLEATVIEW_KEYSTORE_PASSWORD -e PLEATVIEW_KEY_ALIAS -e PLEATVIEW_KEY_PASSWORD \
  "${keystore[@]}" \
  -v "$HOME/.gradle:/gradle" \
  -v "$HOME/.android:/tmp/home/.android" \
  -v "$root:/repo" \
  -w /repo/android \
  "$image" ./gradlew --no-daemon --console=plain "${tasks[@]}"
