from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class Notifier:
    enabled: bool = False
    provider: str = "none"
    webhook_url: str | None = None
    timeout_sec: float = 5.0

    @dataclass(frozen=True)
    class SendResult:
        sent: bool
        error: str | None = None

    def send_with_result(self, message: str) -> SendResult:
        if not self.enabled:
            return Notifier.SendResult(sent=False, error="disabled")

        provider = str(self.provider or "none").strip().lower()
        if provider == "discord":
            try:
                self._send_discord(message)
                return Notifier.SendResult(sent=True, error=None)
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"[notify] discord_send_failed: {message}")
                return Notifier.SendResult(sent=False, error=error)

        print(f"[notify] {message}")
        return Notifier.SendResult(sent=True, error=None)

    def send(self, message: str) -> None:
        _ = self.send_with_result(message)

    def _send_discord(self, message: str) -> None:
        url = str(self.webhook_url or "").strip()
        if not url:
            return
        with httpx.Client(timeout=httpx.Timeout(self.timeout_sec)) as client:
            response = client.post(url, json={"content": message})
            response.raise_for_status()
