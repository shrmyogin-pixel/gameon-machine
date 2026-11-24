"""Tiny stdlib HTTP helpers (run in a thread so they never block the event loop)."""
from __future__ import annotations

import asyncio
import json
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _request(url: str, body: dict | None, timeout: float, headers: dict[str, str] | None) -> str:
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9", "Accept-Language": "en-US,en;q=0.9"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def http_text(url: str, body: dict | None = None, timeout: float = 20, headers: dict[str, str] | None = None) -> str:
    return await asyncio.to_thread(_request, url, body, timeout, headers)


async def http_json(url: str, body: dict | None = None, timeout: float = 20, headers: dict[str, str] | None = None):
    return json.loads(await http_text(url, body, timeout, headers))
