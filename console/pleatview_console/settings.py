"""Settings from the environment, with defaults for the Docker image."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data: Path
    host: str
    port: int
    # "auto": HTTPS with a certificate the console makes; "off": plain HTTP, for a reverse proxy
    # or ZTNA connector in front on the same machine.
    tls: str
    tls_cert: Path | None
    tls_key: Path | None
    master_key: Path
    discovery: bool

    @property
    def https(self) -> bool:
        return self.tls != "off"


def from_env(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    data = Path(env.get("DATA_DIR", "/data"))
    tls = env.get("TLS", "auto").lower()
    if tls not in ("auto", "off"):
        raise ValueError("TLS must be auto or off")
    host = env.get("HOST") or ("127.0.0.1" if tls == "off" else "0.0.0.0")
    return Settings(
        data=data,
        host=host,
        port=int(env.get("PORT", "8790")),
        tls=tls,
        tls_cert=Path(env["TLS_CERT"]) if env.get("TLS_CERT") else None,
        tls_key=Path(env["TLS_KEY"]) if env.get("TLS_KEY") else None,
        master_key=Path(env.get("MASTER_KEY_FILE") or data / "master.key"),
        discovery=env.get("DISCOVERY", "1") != "0",
    )
