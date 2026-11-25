"""The open line from Home Assistant to each screen's page (server-sent events), and the page's way back (taps).

The page is loaded ONCE and keeps this line open; Home Assistant pushes new data down it, so the page is never
re-sent (re-sending made PiPup rebuild its window: white flashes and memory growth).

Who can use either address: only a device on the local network, and only with that screen's key. Everyone else gets
a plain 404.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util.network import is_local

from .const import ACT_URL, CAM_URL, DOMAIN, FEED_URL

if TYPE_CHECKING:
    from .manager import ScreenManager

_LOGGER = logging.getLogger(__name__)
MAX_LISTENERS = 4
KEEPALIVE = 25


class Line:
    """Listeners and the latest data for one screen."""

    def __init__(self) -> None:
        self.latest: bytes | None = None
        self.queues: set[asyncio.Queue] = set()
        # The last banner and when it ends: a screen that (re)connects while it is still up gets it too,
        # so a banner sent while the page was loading (e.g. right after a restart) isn't lost.
        self._banner: dict[str, Any] | None = None
        self._banner_until = 0.0

    @callback
    def send(self, frame: bytes | None) -> None:
        for q in self.queues:
            if q.full():
                q.get_nowait()
            q.put_nowait(frame)

    @callback
    def publish(self, data: dict[str, Any]) -> None:
        self.latest = b"data: " + json.dumps(data, separators=(",", ":"), default=str).encode() + b"\n\n"
        self.send(self.latest)

    @callback
    def event(self, name: str, data: dict[str, Any] | None = None) -> None:
        if name == "banner" and data:
            self._banner, self._banner_until = dict(data), time.monotonic() + float(data.get("seconds", 15))
        self.send(_frame(name, data))

    def pending_banner(self) -> bytes | None:
        left = self._banner_until - time.monotonic()
        if not self._banner or left < 2:
            return None
        return _frame("banner", {**self._banner, "seconds": int(left)})


def _frame(name: str, data: dict[str, Any] | None) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data or {}, separators=(',', ':'))}\n\n".encode()


def _managers(request: web.Request) -> dict[str, ScreenManager]:
    return request.app[KEY_HASS].data.get(DOMAIN, {})


def _find(request: web.Request, key: str, screen: str) -> ScreenManager | None:
    # Local network only. A request through Home Assistant Cloud (remote UI) can carry a local address, so those are
    # refused too, as core does for local-only webhooks.
    try:
        if not is_local(ip_address(request.remote)) or is_cloud_connection(request.app[KEY_HASS]):
            return None
    except ValueError:
        return None
    mgr = _managers(request).get(screen)
    if mgr is None or not hmac.compare_digest(key.encode(), mgr.key.encode()):
        return None
    return mgr


class FeedView(HomeAssistantView):
    url = FEED_URL
    name = "api:arcane_screen:feed"
    requires_auth = False

    async def get(self, request: web.Request, key: str, screen: str) -> web.StreamResponse:
        mgr = _find(request, key, screen)
        if mgr is None or len(mgr.line.queues) >= MAX_LISTENERS:
            raise web.HTTPNotFound
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        mgr.line.queues.add(q)
        mgr.listeners_changed()
        try:
            await resp.write(b"retry: 5000\n\n")
            if mgr.line.latest:
                await resp.write(mgr.line.latest)
            if pending := mgr.line.pending_banner():
                await resp.write(pending)
            while True:
                try:
                    frame = await asyncio.wait_for(q.get(), KEEPALIVE)
                except asyncio.TimeoutError:
                    await resp.write(b": keep\n\n")
                    continue
                if frame is None:
                    break
                await resp.write(frame)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            mgr.line.queues.discard(q)
            mgr.listeners_changed()
        return resp


class ActView(HomeAssistantView):
    url = ACT_URL
    name = "api:arcane_screen:act"
    requires_auth = False

    async def post(self, request: web.Request, key: str, screen: str) -> web.Response:
        mgr = _find(request, key, screen)
        if mgr is None:
            raise web.HTTPNotFound
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest
        ok = await mgr.async_page_action(body)
        return web.json_response({"ok": ok}, status=200 if ok else 409)


class CamView(HomeAssistantView):
    """A welcome-screen camera picture (the page asks again for each frame), only while the welcome screen is up."""

    url = CAM_URL
    name = "api:arcane_screen:cam"
    requires_auth = False

    async def get(self, request: web.Request, key: str, screen: str, i: str) -> web.StreamResponse:
        from homeassistant.components import camera  # noqa: PLC0415 - only needed for this view

        mgr = _find(request, key, screen)
        try:
            eid = mgr.welcome_camera(int(i)) if mgr else None
        except ValueError:
            eid = None
        if eid is None:
            raise web.HTTPNotFound
        hass = request.app[KEY_HASS]
        # Small pictures, a few a second: these screens decode JPEGs slowly. The big tile asks for 640 px, the small ones 320.
        try:
            width = max(160, min(1280, int(request.query.get("w", "640"))))
        except ValueError:
            width = 640

        async def frame() -> bytes | None:
            if mgr.welcome_camera(int(i)) != eid:
                return None
            try:
                return (await camera.async_get_image(hass, eid, timeout=10, width=width)).content
            except HomeAssistantError as err:
                _LOGGER.debug("Camera %s for the welcome screen: %s", eid, err)
                return None

        body = await frame()
        if body is None:
            raise web.HTTPNotFound
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})
