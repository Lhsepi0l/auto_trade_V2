from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class Notifier:
    enabled: bool = False
    provider: str = "none"
    webhook_url: str | None = None
    timeout_sec: float = 5.0

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        provider = str(self.provider or "none").strip().lower()
        if provider == "discord":
            try:
                self._send_discord(message)
            except httpx.HTTPError:
                print(f"[notify] discord_send_failed: {message}")
            return
        print(f"[notify] {message}")

    def _send_discord(self, message: str) -> None:
        url = str(self.webhook_url or "").strip()
        if not url:
            return
        with httpx.Client(timeout=httpx.Timeout(self.timeout_sec)) as client:
            response = client.post(url, json={"content": message})
            response.raise_for_status()
