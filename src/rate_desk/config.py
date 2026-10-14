"""Settings, read from the environment so the same build runs on a laptop and on a server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    # Where the app is reachable from outside, e.g. https://rate-desk.fly.dev.
    # The MCP endpoint only answers to this host (and localhost), which stops
    # DNS-rebinding attacks from a browser on someone's network.
    public_url: str | None = None
    # Seed demo data into an empty database on start (used by the hosted demo).
    demo: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        public_url = os.environ.get("RATE_DESK_PUBLIC_URL") or None
        return cls(
            db_path=Path(os.environ.get("RATE_DESK_DB", "rate-desk.db")),
            public_url=public_url.rstrip("/") if public_url else None,
            demo=os.environ.get("RATE_DESK_DEMO", "") == "1",
        )

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
        if self.public_url:
            host = urlsplit(self.public_url).netloc
            hosts += [host, f"{host}:*"]
        return hosts

    @property
    def allowed_origins(self) -> list[str]:
        origins = ["http://localhost:*", "http://127.0.0.1:*"]
        if self.public_url:
            origins.append(self.public_url)
        return origins
