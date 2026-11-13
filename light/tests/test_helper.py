"""The helper that restarts and updates Linux screens (linux/build/pleatview-helper), run with
stand-ins for curl, apt-get, systemctl and dpkg-query, a test signing key and a release in a
folder. It runs as root on screens, so these check what it refuses as much as what it does."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2] / "linux/build/pleatview-helper"
TOOLS = ("openssl", "dpkg", "dpkg-deb", "sha256sum", "find", "awk")

STUB_CURL = """#!/bin/sh
# Copies https://releases.test/<path> from $RELEASES_DIR, as curl -o would download it.
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; https://*) url=$1 ;; esac
  shift
done
echo "$url" >> "$LOG_DIR/fetched"
cp "$RELEASES_DIR/${url#https://releases.test/}" "$out" 2>/dev/null || exit 22
"""
STUB_RECORD = """#!/bin/sh
echo "$0 $*" >> "$LOG_DIR/ran"
"""
STUB_DPKG_QUERY = """#!/bin/sh
echo -n "$INSTALLED"
"""


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs Debian's tools and openssl")
class HelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = self.tmp / "run"
        (self.run_dir / "requests").mkdir(parents=True)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.releases = self.tmp / "releases"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("curl", STUB_CURL), ("apt-get", STUB_RECORD), ("systemctl", STUB_RECORD),
                           ("dpkg-query", STUB_DPKG_QUERY)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.key = self.tmp / "key.pem"
        self.cert = self.tmp / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.key),
                        "-out", str(self.cert), "-days", "2", "-subj", "/CN=PleatView test"],
                       check=True, capture_output=True)
        self.uptime = self.tmp / "uptime"
        self.uptime.write_text("500.00 900.00\n")

    def release(self, version: str, *, package: str = "pleatview-light", deb_version: str | None = None,
                sign_with: Path | None = None, tamper: bool = False) -> None:
        """Release `version` as release.yml does: the package, SHA256SUMS and its signature."""
        folder = self.releases / f"v{version}"
        folder.mkdir(parents=True)
        root = self.tmp / f"deb-{version}"
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN/control").write_text(
            f"Package: {package}\nVersion: {deb_version or version}\nArchitecture: all\n"
            "Maintainer: Test <test@example.com>\nDescription: test\n")
        deb = folder / "pleatview-light_all.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)], check=True, capture_output=True)
        digest = subprocess.run(["sha256sum", deb.name], cwd=folder, check=True, capture_output=True, text=True).stdout
        (folder / "SHA256SUMS").write_text(digest + "0" * 64 + "  pleatview.apk\n")
        if tamper:
            deb.write_bytes(deb.read_bytes() + b"\0")
        key = sign_with or self.key
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key), "-out", str(folder / "SHA256SUMS.sig"),
                        str(folder / "SHA256SUMS")], check=True, capture_output=True)

    def helper(self, *requests: str, installed: str = "1.0.19", package: str = "pleatview-light") -> dict | None:
        for name in requests:
            (self.run_dir / "requests" / name).touch()
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "LOG_DIR": str(self.logs), "RELEASES_DIR": str(self.releases),
               "INSTALLED": installed, "PLEATVIEW_HELPER_RUN": str(self.run_dir), "PLEATVIEW_HELPER_CERT": str(self.cert),
               "PLEATVIEW_HELPER_RELEASES": "https://releases.test", "PLEATVIEW_HELPER_UPTIME": str(self.uptime)}
        self.result = subprocess.run(["sh", str(HELPER), package], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(list((self.run_dir / "requests").iterdir()), [], "requests are always cleared")
        status = self.run_dir / "status.json"
        return json.loads(status.read_text()) if status.exists() else None

    def ran(self) -> str:
        path = self.logs / "ran"
        return path.read_text() if path.exists() else ""

    def test_installs_a_newer_signed_release(self) -> None:
        self.release("1.0.20")
        status = self.helper("update-1.0.20")
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.assertEqual((status["version"], status["state"], status["error"]), ("1.0.20", "installed", None))
        self.assertIn("apt-get install", self.ran())
        self.assertIn("pleatview-light_all.deb", self.ran())
        self.assertEqual(oct((self.run_dir / "status.json").stat().st_mode & 0o777), "0o640")

    def test_refuses_what_isnt_newer(self) -> None:
        self.release("1.0.19")
        status = self.helper("update-1.0.19")
        self.assertEqual(status["state"], "failed")
        self.assertIn("only newer", status["error"])
        self.assertNotIn("apt-get", self.ran())
        self.assertFalse((self.logs / "fetched").exists(), "nothing is downloaded")

    def test_refuses_a_signature_from_another_key(self) -> None:
        other = self.tmp / "other.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(other)],
                       check=True, capture_output=True)
        self.release("1.0.20", sign_with=other)
        status = self.helper("update-1.0.20")
        self.assertEqual((status["state"], status["error"]), ("failed", "the release's signature is wrong"))
        self.assertNotIn("apt-get", self.ran())

    def test_refuses_a_package_that_doesnt_match_its_hash(self) -> None:
        self.release("1.0.20", tamper=True)
        status = self.helper("update-1.0.20")
        self.assertEqual(status["state"], "failed")
        self.assertIn("hash", status["error"])
        self.assertNotIn("apt-get", self.ran())

    def test_refuses_another_package_or_version(self) -> None:
        self.release("1.0.20", package="something-else")
        self.assertIn("isn't pleatview-light", self.helper("update-1.0.20")["error"])
        self.release("1.0.21", deb_version="1.0.22")
        self.assertIn("isn't version 1.0.21", self.helper("update-1.0.21")["error"])
        self.assertNotIn("apt-get", self.ran())

    def test_a_missing_release(self) -> None:
        status = self.helper("update-1.0.30")
        self.assertEqual((status["state"], status["error"]), ("failed", "no release 1.0.30"))

    def test_ignores_requests_it_doesnt_know(self) -> None:
        self.release("1.0.20")
        for name in ("update-1.0", "update-1.0.20-rc1", "update-..", "update-1.0.20;reboot", "reboot", "update", "*"):
            self.assertIsNone(self.helper(name), name)
        self.assertEqual(self.ran(), "")
        self.assertFalse((self.logs / "fetched").exists())

    def test_restarts_the_computer(self) -> None:
        self.helper("restart")
        self.assertIn("systemctl reboot", self.ran())

    def test_not_again_just_after_it_started(self) -> None:
        self.uptime.write_text("45.12 80.00\n")
        self.helper("restart")
        self.assertEqual(self.ran(), "")
        self.assertIn("started 45s ago", self.result.stderr)

    def test_unknown_package(self) -> None:
        result = subprocess.run(["sh", str(HELPER), "bash"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
