"""JSON for HTTP and WebSocket responses that never fails on lone surrogates.

LLM output is kept verbatim (agent histories, traces, events), and a model can emit an unpaired surrogate
such as "\\ud800" in a JSON tool argument. Such a string cannot be encoded as UTF-8, so the stock Starlette
JSONResponse / WebSocket.send_json would fail (HTTP 500, a dead live-update stream). Here the JSON text falls
back to ASCII escapes ("\\ud800"), which every JSON parser accepts."""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import JSONResponse as _StarletteJSONResponse


def json_text(content: Any, *, indent: int | None = None, default: Any = None) -> str:
    """json.dumps with ensure_ascii=False, or escaped ASCII when the text is not valid UTF-8."""
    separators = (",", ":") if indent is None else None
    text = json.dumps(content, ensure_ascii=False, allow_nan=False, indent=indent, separators=separators,
                      default=default)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(content, ensure_ascii=True, allow_nan=False, indent=indent, separators=separators,
                          default=default)
    return text


class JSONResponse(_StarletteJSONResponse):
    def render(self, content: Any) -> bytes:
        return json_text(content).encode("utf-8")


__all__ = ["JSONResponse", "json_text"]
