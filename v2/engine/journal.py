from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from v2.storage import RuntimeStorage


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


@dataclass
class JournalWriter:
    storage: RuntimeStorage

    def write(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        reason: str | None,
        event_id: str | None = None,
    ) -> bool:
        payload_json = _canonical_json(payload)
        if event_id is None:
            digest = hashlib.sha256(f"{event_type}|{reason or ''}|{payload_json}".encode("utf-8")).hexdigest()
            event_id = f"auto-{digest}"
        return self.storage.append_journal_event(
            event_id=event_id,
            event_type=event_type,
            reason=reason,
            payload_json=payload_json,
        )
