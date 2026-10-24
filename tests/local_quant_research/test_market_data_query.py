from __future__ import annotations


def test_normalized_digest_is_independent_of_input_order() -> None:
    from scripts.research.market_data.query import normalized_digest

    rows = [
        {"date": "2026-01-06", "security": "000001.XSHG", "close": 10.2},
        {"date": "2026-01-05", "security": "000001.XSHG", "close": 10.1},
    ]

    assert normalized_digest(rows) == normalized_digest(reversed(rows))
