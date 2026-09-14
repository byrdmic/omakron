"""Framing for the client/service protocol: one bounded JSON object per line.

A connection carries one request and one response. Requests are JSON objects
with ``id`` (client-chosen, echoed back), ``op``, and ``params``. Responses
carry ``id``, ``ok``, and either ``result`` or ``error`` with ``code`` and
``message``. Anything larger than :data:`MAX_MESSAGE_BYTES` is refused, so a
runaway client cannot make the service buffer without bound.
"""

from __future__ import annotations

import json
import socket
from typing import Any

MAX_MESSAGE_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
READ_CHUNK = 4096


class ProtocolError(Exception):
    """The peer sent something that is not one bounded JSON object on a line."""


def encode(message: dict[str, Any], *, max_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
    data = (json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise ProtocolError(f"message of {len(data)} bytes exceeds {max_bytes}")
    return data


def read_message(sock: socket.socket, *, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    """Read up to one newline; refuse oversize, non-object, or truncated input."""
    buffer = bytearray()
    while b"\n" not in buffer:
        chunk = sock.recv(READ_CHUNK)
        if not chunk:
            if not buffer:
                raise ProtocolError("peer closed before sending a message")
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ProtocolError(f"message exceeds {max_bytes} bytes")
    line, _, _ = bytes(buffer).partition(b"\n")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"message is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message is not a JSON object")
    return message


def error_response(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def ok_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}
