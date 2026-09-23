"""Unit tests for catalog artifact mapping (no Hermes gateway required)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> None:
    plugin_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("tcc_catalog_artifacts", plugin_dir / "__init__.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    events = mod.map_catalog_items(
        [
            {
                "title": "Show A",
                "start_at": "2026-10-01T12:00:00.000Z",
                "venue": "Impact",
                "product_id": 42,
                "id": "evt-42",
                "poster_url": "https://cdn.example/a.jpg",
                "artists": [{"display_name": "Band"}],
                "price_min": 1000,
                "price_max": 3000,
            },
            {
                "title": "Show B",
                "ticket_url": "https://ticket.example/b",
            },
        ]
    )
    assert events[0]["title"] == "Show A"
    assert events[0]["meta"] == "2026-10-01 · Impact"
    assert events[0]["date"] == "2026-10-01"
    assert events[0]["venue"] == "Impact"
    assert events[0]["url"] == "/concert/42"
    assert events[0]["id"] == "evt-42"
    assert events[0]["product_id"] == 42
    assert events[0]["image"] == "https://cdn.example/a.jpg"
    assert events[0]["artists"] == ["Band"]
    assert events[0]["price"] == "1000–3000"
    assert events[1]["url"] == "https://ticket.example/b"

    # Prefer absolute ticket_url from tool (TCC concert page) over rebuilding path.
    with_ticket = mod.map_catalog_item(
        {
            "title": "NCT",
            "product_id": 3000,
            "ticket_url": "http://localhost:3334/concert/3000",
            "poster_url": "https://cdn.example/n.jpg",
        }
    )
    assert with_ticket["url"] == "http://localhost:3334/concert/3000"

    one = mod.events_from_tool_payload(
        {
            "id": 42,
            "product_id": 42,
            "title": "Detail Show",
            "venue_name": "Impact",
            "start_at": "2026-11-01T00:00:00.000Z",
            "poster_url": "https://cdn.example/d.jpg",
        },
        layout="poster",
    )
    assert len(one) == 1
    assert one[0]["title"] == "Detail Show"
    assert one[0]["image"] == "https://cdn.example/d.jpg"
    assert one[0]["layout"] == "poster"
    assert one[0]["url"] == "/concert/42"
    assert one[0]["venue"] == "Impact"
    assert mod.layout_for_tool("mcp__tcc_api__get_event") == "poster"
    assert mod.layout_for_tool("mcp__tcc_api__find_events") == "card"

    listed = mod.events_from_tool_payload(
        {"total": 1, "items": [{"title": "List", "product_id": 1, "poster_url": "https://cdn.example/l.jpg"}]},
        layout="card",
    )
    assert listed[0]["layout"] == "card"
    assert listed[0]["venue"] == "" or "venue" in listed[0]

    assert mod.events_from_tool_payload({"notFound": True}) == []

    payload = mod.parse_tool_payload('{"total":1,"items":[{"title":"X","product_id":9}]}')
    assert payload["items"][0]["title"] == "X"

    wrapped = mod.parse_tool_payload(
        '{"result":{"total":1,"items":[{"title":"Wrapped","product_id":7,"poster_url":"https://cdn.example/w.jpg"}]}}'
    )
    assert wrapped["items"][0]["title"] == "Wrapped"
    cards = mod.events_from_tool_payload(wrapped)
    assert cards[0]["image"] == "https://cdn.example/w.jpg"

    nested = mod.parse_tool_payload(
        '{"result":"{\\"total\\":1,\\"items\\":[{\\"title\\":\\"Nested\\",\\"product_id\\":8}]}"}'
    )
    assert nested["items"][0]["title"] == "Nested"

    assert mod._TOOL_RE.match("mcp__tcc_api__find_events")
    assert mod._TOOL_RE.match("mcp__tcc_api__get_event")
    assert mod._TOOL_RE.match("mcp__tcc_api_stg__search_events")

    mod.store_events("sess-1", events)
    out = {"object": "chat.completion", "choices": []}
    mod.attach_catalog_to_payload(out, "sess-1")
    assert out["hermes"]["catalog"]["events"][0]["title"] == "Show A"
    assert mod.take_events("sess-1") == []

    zero = mod.map_catalog_item(
        {
            "title": "STARRY",
            "product_id": 5961,
            "price_min": 790,
            "price_max": 2990,
            "ticket_tier_count": 0,
        }
    )
    assert zero["ticket_tier_count"] == 0

    from_tiers = mod.map_catalog_item(
        {
            "title": "Echo",
            "product_id": 3080,
            "ticket_tiers": [
                {"zone": "GA", "price_min": 1350},
                {"zone": "VIP", "price_min": 2700},
            ],
        }
    )
    assert from_tiers["ticket_tier_count"] == 2

    posters = mod.events_from_tool_payload(
        {
            "title": "Echo",
            "product_id": 3080,
            "poster_url": "https://cdn.example/e.jpg",
            "ticket_tiers": [
                {"zone": "GA", "price_min": 1},
                {"zone": "VIP", "price_min": 2},
            ],
        },
        layout="poster",
    )
    assert posters[0]["layout"] == "poster"
    assert posters[0]["ticket_tier_count"] == 2

    print("tcc-catalog-artifacts ok")


if __name__ == "__main__":
    main()
