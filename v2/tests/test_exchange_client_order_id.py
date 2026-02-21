from __future__ import annotations

import re

from v2.exchange.client_order_id import generate_client_order_id, sanitize_client_order_id


def test_client_order_id_generator_matches_binance_constraints() -> None:
    cid = generate_client_order_id(prefix="V2-Order")
    assert len(cid) < 32
    assert re.fullmatch(r"[A-Za-z0-9\-_]+", cid) is not None


def test_client_order_id_sanitize_replaces_invalid_chars() -> None:
    out = sanitize_client_order_id("@@ bad id !!")
    assert out
    assert len(out) < 32
    assert re.fullmatch(r"[A-Za-z0-9\-_]+", out) is not None
