#!/bin/sh
# Builds pleatview-light_all.deb from this folder, with dpkg-deb only.
#   light/build-deb.sh [OUTPUT_DIR]
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/dist}
version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$here/pleatview_light/__init__.py")
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT
chmod 755 "$root"
mkdir -p "$root/DEBIAN" "$root/usr/share/pleatview-light/pleatview_light" "$root/usr/bin" "$root/usr/share/doc/pleatview-light"
cp "$here"/pleatview_light/*.py "$root/usr/share/pleatview-light/pleatview_light/"
install -m 755 "$here/build/pleatview-light" "$root/usr/bin/pleatview-light"
cp "$here/README.md" "$root/usr/share/doc/pleatview-light/"
cp "$here/../LICENSE" "$root/usr/share/doc/pleatview-light/copyright"
cat >"$root/DEBIAN/control" <<CONTROL
Package: pleatview-light
Version: $version
Architecture: all
Maintainer: Hustlesman <privacy@hustlesman.com>
Depends: python3 (>= 3.11), mpv
Section: video
Priority: optional
Homepage: https://pleatview.hustlesman.com
Description: Home Assistant dashboards on the smallest screens
 Plays the dashboards Home Assistant renders (PleatView Renderer) full screen
 with mpv and the board's video decoder, for computers too small for a browser,
 such as a Raspberry Pi Zero 2 W. Discovered, paired and controlled like any
 PleatView screen.
CONTROL
mkdir -p "$out"
dpkg-deb --root-owner-group --build "$root" "$out/pleatview-light_${version}_all.deb" >/dev/null
echo "$out/pleatview-light_${version}_all.deb"
