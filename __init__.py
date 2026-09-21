"""Capture catalog MCP tool results and attach them to chat completions.

Hermes already called ``find_events`` / ``get_event`` (and legacy search/recommend
names); this surfaces those items as ``hermes.catalog.events`` on the
OpenAI-compatible response so tcc-ai-assistant can forward them without a
second MCP round-trip.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

_log = logging.getLogger("hermes.plugin.tcc-catalog-artifacts")

# mcp__tcc_api__find_events / mcp__tcc_api_stg__get_event / …
_TOOL_RE = re.compile(
    r"^mcp__tcc_api(?:_[a-z0-9]+)?__"
    r"(find_events|get_event|recommend_events|search_events)$"
)

# Per-profile plugin imports create separate module objects. Keep the event bag
# and patch flag on a process-global singleton so store + attach share state.
def _shared_state() -> Dict[str, Any]:
    import sys

    state = sys.modules.setdefault(
        "_tcc_catalog_artifacts_shared",
        {"bag": {}, "lock": threading.Lock(), "patched": False},
    )
    return state  # type: ignore[return-value]


def _bag() -> Dict[str, Dict[str, Any]]:
    return _shared_state()["bag"]


def _lock() -> threading.Lock:
    return _shared_state()["lock"]


_TTL_SEC = 300
_MAX_EVENTS = 8
_PATCHED_ATTR = "_tcc_catalog_artifacts_patched"


def _purge_expired(now: Optional[float] = None) -> None:
    ts = time.time() if now is None else now
    bag = _bag()
    for key in [k for k, v in bag.items() if float(v.get("expires") or 0) < ts]:
        bag.pop(key, None)


def store_events(key: str, events: List[Dict[str, Any]]) -> None:
    if not key or not events:
        return
    with _lock():
        _purge_expired()
        _bag()[key] = {"events": events[:_MAX_EVENTS], "expires": time.time() + _TTL_SEC}


def take_events(*keys: str) -> List[Dict[str, Any]]:
    with _lock():
        _purge_expired()
        bag = _bag()
        found: List[Dict[str, Any]] = []
        matched: List[str] = []
        for raw in keys:
            key = str(raw or "").strip()
            if not key or key in matched:
                continue
            row = bag.get(key)
            if not row:
                continue
            events = row.get("events")
            if isinstance(events, list) and events:
                found = list(events)
                matched.append(key)
                break
        for key in matched:
            bag.pop(key, None)
        # Drop aliases that still hold the same turn (best-effort).
        if found:
            for raw in keys:
                key = str(raw or "").strip()
                if key:
                    bag.pop(key, None)
        return found


def _day_label(start_at: Any) -> str:
    raw = str(start_at or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else raw[:10]


def _artist_names(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    names: List[str] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        name = str(
            a.get("display_name") or a.get("name_en") or a.get("name_th") or ""
        ).strip()
        if name:
            names.append(name)
        if len(names) >= 5:
            break
    return names


def _price_label(raw: Dict[str, Any]) -> str:
    lo = raw.get("price_min")
    hi = raw.get("price_max")
    try:
        lo_n = float(lo) if lo is not None and lo != "" else None
    except (TypeError, ValueError):
        lo_n = None
    try:
        hi_n = float(hi) if hi is not None and hi != "" else None
    except (TypeError, ValueError):
        hi_n = None
    if lo_n is None and hi_n is None:
        return ""
    if lo_n is not None and hi_n is not None and lo_n != hi_n:
        return f"{int(lo_n)}–{int(hi_n)}"
    val = lo_n if lo_n is not None else hi_n
    return str(int(val)) if val is not None else ""


def map_catalog_item(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    if raw.get("notFound") is True:
        return None
    title = str(raw.get("title") or raw.get("title_th") or raw.get("title_en") or "").strip()
    if not title:
        return None
    day = _day_label(raw.get("start_at"))
    shown = str(raw.get("show_time") or "").strip()
    if shown:
        day = shown
    venue = str(
        raw.get("venue")
        or raw.get("venue_name")
        or raw.get("venue_name_as_listed")
        or raw.get("venue_name_th")
        or raw.get("venue_name_en")
        or ""
    ).strip()
    meta = " · ".join([p for p in (day, venue) if p])
    try:
        pid = int(raw.get("product_id")) if raw.get("product_id") is not None else 0
    except (TypeError, ValueError):
        pid = 0
    ticket = str(raw.get("ticket_url") or "").strip()
    if ticket:
        url = ticket
    elif pid > 0:
        url = f"/concert/{pid}"
    else:
        url = ""
    card: Dict[str, Any] = {
        "title": title,
        "meta": meta,
        "date": day,
        "venue": venue,
        "url": url,
        "image": str(raw.get("poster_url") or "").strip(),
        "artists": _artist_names(raw.get("artists")),
        "price": _price_label(raw),
    }
    desc = str(raw.get("description") or raw.get("description_th") or raw.get("description_en") or "").strip()
    if desc:
        card["description"] = desc[:1200]
    item_id = str(raw.get("id") or "").strip()
    if item_id:
        card["id"] = item_id
    if pid > 0:
        card["product_id"] = pid
    return card


def map_catalog_items(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in items:
        card = map_catalog_item(raw)
        if not card:
            continue
        out.append(card)
        if len(out) >= _MAX_EVENTS:
            break
    return out


def layout_for_tool(tool_name: str) -> str:
    """get_event → poster link; find/search/recommend → full card."""
    name = str(tool_name or "")
    if name.endswith("get_event"):
        return "poster"
    return "card"


def _with_layout(card: Dict[str, Any], layout: str) -> Dict[str, Any]:
    out = dict(card)
    out["layout"] = layout
    # Keep date/venue/artists/price on poster cards — AI ASK detail template needs them.
    return out


def events_from_tool_payload(
    payload: Any, *, layout: str = "card"
) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("items"), list):
        return [_with_layout(c, layout) for c in map_catalog_items(payload.get("items"))]
    # get_event returns one object (or { notFound: true }).
    if payload.get("title") or payload.get("title_th") or payload.get("title_en"):
        card = map_catalog_item(payload)
        return [_with_layout(card, layout)] if card else []
    return []


def _unwrap_result_envelope(payload: Any, *, depth: int = 0) -> Any:
    """Hermes MCP tool results often arrive as ``{"result": <payload|json-str>}``."""
    if depth > 4 or not isinstance(payload, dict):
        return payload
    if "items" in payload or "total" in payload:
        return payload
    if payload.get("title") or payload.get("title_th") or payload.get("notFound") is not None:
        return payload
    if "result" not in payload:
        return payload
    inner = payload.get("result")
    if isinstance(inner, str):
        text = inner.strip()
        if not text:
            return payload
        try:
            inner = json.loads(text)
        except Exception:
            return payload
    if isinstance(inner, dict):
        return _unwrap_result_envelope(inner, depth=depth + 1)
    return payload


def parse_tool_payload(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, dict):
        payload = result
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                try:
                    payload = json.loads(first["text"])
                except Exception:
                    return None
        elif isinstance(result.get("text"), str):
            try:
                payload = json.loads(result["text"])
            except Exception:
                payload = result
        return _unwrap_result_envelope(payload)
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(text[start : end + 1])
                except Exception:
                    return None
            else:
                return None
        return _unwrap_result_envelope(payload)
    return None


def attach_catalog_to_payload(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    key_list = [str(k).strip() for k in keys if str(k or "").strip()]
    events = take_events(*key_list)
    if not events:
        if key_list:
            with _lock():
                bag_keys = list(_bag().keys())
            _log.debug(
                "catalog artifacts: attach miss keys=%s bag=%s object=%s",
                key_list[:6],
                bag_keys[:8],
                payload.get("object"),
            )
        return payload
    hermes = payload.get("hermes")
    if not isinstance(hermes, dict):
        hermes = {}
        payload["hermes"] = hermes
    catalog = hermes.get("catalog")
    if not isinstance(catalog, dict):
        catalog = {}
        hermes["catalog"] = catalog
    catalog["events"] = events
    _log.info("catalog artifacts: attached %s events", len(events))
    return payload


def _session_key_aliases() -> List[str]:
    aliases: List[str] = []
    try:
        from tools.approval import get_current_session_key

        key = str(get_current_session_key(default="") or "").strip()
        if key:
            aliases.append(key)
    except Exception:
        pass
    try:
        from gateway.session_context import get_session_env

        for name in ("HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
            val = str(get_session_env(name, "") or "").strip()
            if val:
                aliases.append(val)
    except Exception:
        pass
    return aliases


def on_post_tool_call(
    *,
    tool_name: str = "",
    result: Any = None,
    session_id: str = "",
    task_id: str = "",
    api_request_id: str = "",
    **_: Any,
) -> None:
    # Gateway may bind routes before plugins finish; re-arm cheaply when already patched.
    try:
        _install_response_patch()
    except Exception:
        pass
    name = str(tool_name or "")
    if not _TOOL_RE.match(name):
        return
    payload = parse_tool_payload(result)
    if not isinstance(payload, dict):
        return
    events = events_from_tool_payload(payload, layout=layout_for_tool(name))
    if not events:
        return
    keys = [
        str(session_id or "").strip(),
        str(api_request_id or "").strip(),
        str(task_id or "").strip(),
        *_session_key_aliases(),
    ]
    stored = False
    for key in keys:
        if key:
            store_events(key, events)
            stored = True
    if stored:
        _log.info(
            "catalog artifacts: stored %s events from %s keys=%s",
            len(events),
            name,
            [k for k in keys if k][:6],
        )
    else:
        _log.warning("catalog artifacts: matched %s but no session/task key", name)


def _header_get(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    try:
        get = getattr(headers, "get", None)
        if callable(get):
            return str(get(name) or get(name.lower()) or "")
    except Exception:
        pass
    if isinstance(headers, dict):
        for key, val in headers.items():
            if str(key).lower() == name.lower():
                return str(val or "")
    return ""


def _is_our_patch(fn: Any) -> bool:
    return bool(fn is not None and getattr(fn, _PATCHED_ATTR, False))


def _install_response_patch() -> bool:
    """Attach catalog on the wire format Hermes already uses.

    Class-method monkeypatches are unreliable: the API server binds routes to
    the original handlers before plugins register. Patch the shared helpers
    those handlers call instead (``web.json_response`` / ``_sse_frame``).
    """
    try:
        from gateway.platforms import api_server as api_mod
        from aiohttp import web
    except Exception:
        _log.debug("api_server unavailable; catalog artifacts idle")
        return False

    armed = False

    if not _is_our_patch(getattr(web, "json_response", None)):
        original_json_response = web.json_response

        def json_response(body, *args, **kwargs):
            if isinstance(body, dict) and body.get("object") == "chat.completion":
                headers = kwargs.get("headers")
                sid = _header_get(headers, "X-Hermes-Session-Id")
                skey = _header_get(headers, "X-Hermes-Session-Key")
                attach_catalog_to_payload(body, sid, skey, *_session_key_aliases())
            return original_json_response(body, *args, **kwargs)

        setattr(json_response, _PATCHED_ATTR, True)
        web.json_response = json_response
        armed = True

    original_frame = getattr(api_mod, "_sse_frame", None)
    if original_frame is not None and not _is_our_patch(original_frame):

        def _sse_frame(data, event=None):
            if (
                isinstance(data, dict)
                and data.get("object") == "chat.completion.chunk"
            ):
                choices = data.get("choices") or []
                if choices and choices[0].get("finish_reason"):
                    attach_catalog_to_payload(data, *_session_key_aliases())
            if event is None:
                return original_frame(data)
            return original_frame(data, event=event)

        setattr(_sse_frame, _PATCHED_ATTR, True)
        api_mod._sse_frame = _sse_frame
        armed = True

    if armed:
        _log.info("catalog artifacts: chat completions attach armed")
        return True
    sse_frame = getattr(api_mod, "_sse_frame", None)
    if _is_our_patch(getattr(web, "json_response", None)) and (
        sse_frame is None or _is_our_patch(sse_frame)
    ):
        return True

    _log.error("catalog artifacts: could not patch response helpers")
    return False


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", on_post_tool_call)
    try:
        _install_response_patch()
    except Exception:
        _log.exception("catalog artifacts: response patch failed")
    _log.info("tcc-catalog-artifacts ready (%s)", _TOOL_RE.pattern)
