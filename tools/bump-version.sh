#!/usr/bin/env bash
# Sets the version for the next release in the Android app, the integration and the Linux app, and
# increments the app's versionCode (which Google Play requires to grow every upload).
#
#   tools/bump-version.sh 0.2.0
set -euo pipefail

version="${1:?usage: tools/bump-version.sh <version>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
gradle="$root/android/app/build.gradle.kts"

code=$(sed -nE 's/^ *versionCode = ([0-9]+).*/\1/p' "$gradle")
sed -i -E "s/^( *versionCode = )[0-9]+/\1$((code + 1))/; s/^( *versionName = )\"[^\"]+\"/\1\"$version\"/" "$gradle"

python3 - "$root" "$version" <<'PY'
import json
import sys

root, version = sys.argv[1:]


def update(path, change):
    with open(path) as f:
        data = json.load(f)
    change(data)
    with open(path, "w") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


update(f"{root}/custom_components/pleatview/manifest.json", lambda m: m.update(version=version))
update(f"{root}/linux/package.json", lambda p: p.update(version=version))
update(f"{root}/linux/package-lock.json",
       lambda lock: (lock.update(version=version), lock["packages"][""].update(version=version)))
PY

echo "Version $version (versionCode $((code + 1))). After merging, tag the commit: git tag v$version"
