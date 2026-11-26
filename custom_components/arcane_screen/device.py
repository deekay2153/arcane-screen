"""Talking to one screen: PiPup's HTTP API, ADB (install, permissions, nightly restart) and, on Hikvision indoor
stations, the ISAPI call that switches ADB on.

Hard-won rules from the screens this was built on:
- Shell commands are sent as ONE string through adb-shell, never through a local shell, so quoting can't split them
  into extra commands (a stray word once started a second copy of the intercom's own core process).
- PiPup grows by several hundred MB a day on these 2 GB screens; above ~90% memory Hikvision's software restarts
  itself every few seconds. So PiPup is restarted every night, and only when no call is in progress.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from typing import Any

import httpx

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client

from .const import (
    PIPUP_APK_SHA256,
    PIPUP_APK_URL,
    PIPUP_PACKAGE,
    PIPUP_PORT,
    PIPUP_VERSION,
    POPUP_ID,
)

_LOGGER = logging.getLogger(__name__)

MIN_SDK = 23            # PiPup 0.31 needs Android 6.0 or newer
SHELL_WAIT = 30.0       # seconds; slow screens can take well over 9 s to answer "dumpsys" or "pm install"

RESTART_CMDS = (
    f"am force-stop {PIPUP_PACKAGE}",
    # QUICKBOOT_POWERON is not a protected broadcast; flag 0x20 includes stopped packages. Starts PiPup's service.
    f"am broadcast -f 0x20 -a android.intent.action.QUICKBOOT_POWERON -n {PIPUP_PACKAGE}/.Receiver",
)


class ScreenError(Exception):
    """Something on the screen didn't answer as expected (message is safe to show)."""


class Screen:
    """One Android screen running (or about to run) PiPup."""

    _refused: dict[str, bool] = {}   # host -> the station refused our login (cleared by a Home Assistant restart or by typing the login again in setup)

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        adb_port: int,
        hik_user: str | None = None,
        hik_password: str | None = None,
    ) -> None:
        self.hass = hass
        self.host = host
        self.adb_port = adb_port
        self.hik_user = hik_user
        self.hik_password = hik_password
        self._adb_lock = asyncio.Lock()

    # ---------------------------------------------------------------- PiPup HTTP
    @property
    def _base(self) -> str:
        return f"http://{self.host}:{PIPUP_PORT}"

    async def pipup_state(self) -> dict[str, Any] | None:
        try:
            r = await get_async_client(self.hass).get(f"{self._base}/state", timeout=5)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def show(self, url: str, width: int, height: int) -> bool:
        body = {
            "id": POPUP_ID,
            "duration": 0,
            "fullscreen": True,
            "padding": 0,
            "borderWidth": 0,
            # dark, not white, while the page draws its first frame
            "backgroundColor": "#FF080C24",
            "media": {"web": {"uri": url, "width": width, "height": height}},
        }
        try:
            r = await get_async_client(self.hass).post(f"{self._base}/notify", json=body, timeout=15)
            return r.status_code < 300
        except httpx.HTTPError as err:
            _LOGGER.debug("show on %s failed: %s", self.host, err)
            return False

    async def dismiss(self) -> bool:
        try:
            r = await get_async_client(self.hass).post(f"{self._base}/cancel", params={"id": POPUP_ID}, timeout=10)
            return r.status_code < 300
        except httpx.HTTPError:
            return False

    async def pipup_settings(self, **params: str) -> None:
        try:
            await get_async_client(self.hass).post(f"{self._base}/settings", params=params, timeout=10)
        except httpx.HTTPError as err:
            _LOGGER.debug("PiPup settings on %s failed: %s", self.host, err)

    # ---------------------------------------------------------------- Hikvision ISAPI
    async def hik_adb_on(self) -> bool:
        """Ask a Hikvision indoor station to switch ADB on: one attempt per call, and never again once it has refused
        the login (a station locks its admin account after a few wrong passwords)."""
        if not (self.hik_user and self.hik_password) or Screen._refused.get(self.host):
            return False
        url = f"http://{self.host}/ISAPI/VideoIntercom/RemoteControl/adb?format=json"
        try:
            async with httpx.AsyncClient(
                auth=httpx.DigestAuth(self.hik_user, self.hik_password), timeout=10, verify=False
            ) as client:
                r = await client.put(url, json={"Adb": {"command": "start"}})
        except httpx.HTTPError as err:
            _LOGGER.debug("ISAPI adb start on %s failed: %s", self.host, err)
            return False
        if r.status_code == 401:
            Screen._refused[self.host] = True
            raise ScreenError("The screen refused the Hikvision username or password")
        return r.status_code < 300

    # ---------------------------------------------------------------- ADB
    async def _signer(self):
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner

        path = self.hass.config.path(".storage", "arcane_screen_adbkey")

        def load():
            if not os.path.isfile(path):
                keygen(path)
            with open(path, encoding="utf-8") as fh:
                priv = fh.read()
            with open(path + ".pub", encoding="utf-8") as fh:
                pub = fh.read()
            return PythonRSASigner(pub, priv)

        return await self.hass.async_add_executor_job(load)

    async def _connect(self):
        from adb_shell.adb_device_async import AdbDeviceTcpAsync

        signer = await self._signer()
        for attempt in (1, 2):
            dev = AdbDeviceTcpAsync(self.host, self.adb_port, default_transport_timeout_s=9.0)
            try:
                await dev.connect(rsa_keys=[signer], auth_timeout_s=20)
                return dev
            except Exception as err:  # noqa: BLE001 - adb-shell raises many kinds
                _LOGGER.debug("adb connect %s attempt %s: %s", self.host, attempt, err)
                try:
                    await dev.close()
                except Exception:  # noqa: BLE001
                    pass
                if attempt == 1 and await self.hik_adb_on():
                    await asyncio.sleep(3)
                    continue
                raise ScreenError(
                    "Couldn't reach ADB on the screen. On a Hikvision station check the admin login; on other "
                    "screens turn on network ADB (and accept the 'Allow USB debugging' prompt once)"
                ) from err
        raise ScreenError("Couldn't reach ADB on the screen")

    async def adb(self, *commands: str) -> list[str]:
        """Run shell commands, each passed as one string to the device. Returns their output."""
        async with self._adb_lock:
            dev = await self._connect()
            try:
                out = []
                for cmd in commands:
                    out.append(str(await dev.shell(cmd, transport_timeout_s=SHELL_WAIT, read_timeout_s=60) or "").strip())
                return out
            except Exception as err:  # noqa: BLE001 - adb-shell raises many kinds
                raise ScreenError(f"The screen stopped answering over ADB ({err})") from err
            finally:
                await dev.close()

    async def check_android(self) -> None:
        """Refuse early, with a clear message, on screens too old for PiPup (Android tells us only after the upload)."""
        sdk, release = await self.adb("getprop ro.build.version.sdk", "getprop ro.build.version.release")
        if sdk.isdigit() and int(sdk) < MIN_SDK:
            raise ScreenError(
                f"This screen runs Android {release or '?'} and PiPup needs Android 6.0 or newer, so it can't run here"
            )

    async def restart_pipup(self) -> None:
        await self.adb(*RESTART_CMDS)

    async def pipup_installed_version(self) -> str | None:
        (out,) = await self.adb(f"dumpsys package {PIPUP_PACKAGE} | grep versionName")
        out = out.strip()
        return out.split("=", 1)[1].strip() if "=" in out else None

    async def install_pipup(self) -> str:
        """Download the pinned PiPup release, check it, install it, grant the overlay permission and start it.

        Returns the installed version. PiPup's own update checks are switched off: updates come from this integration,
        one tested version at a time, so nobody has to tap Android's install prompt.
        """
        await self.check_android()
        current = await self.pipup_installed_version()
        if current != PIPUP_VERSION:
            r = await get_async_client(self.hass).get(PIPUP_APK_URL, follow_redirects=True, timeout=120)
            if r.status_code != 200:
                raise ScreenError(f"Couldn't download PiPup {PIPUP_VERSION} from GitHub (HTTP {r.status_code})")
            data = r.content
            if hashlib.sha256(data).hexdigest() != PIPUP_APK_SHA256:
                raise ScreenError("The downloaded PiPup file didn't match the expected checksum; nothing was installed")

            def save() -> str:
                fd, path = tempfile.mkstemp(suffix=".apk")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                return path

            local = await self.hass.async_add_executor_job(save)
            try:
                async with self._adb_lock:
                    dev = await self._connect()
                    try:
                        await dev.push(local, "/data/local/tmp/pipup.apk", transport_timeout_s=SHELL_WAIT, read_timeout_s=120)
                        res = str(await dev.shell("pm install -r /data/local/tmp/pipup.apk", transport_timeout_s=SHELL_WAIT, read_timeout_s=180))
                        await dev.shell("rm /data/local/tmp/pipup.apk", transport_timeout_s=SHELL_WAIT)
                    except Exception as err:  # noqa: BLE001
                        raise ScreenError(f"The screen stopped answering while PiPup was being installed ({err})") from err
                    finally:
                        await dev.close()
            finally:
                await self.hass.async_add_executor_job(os.remove, local)
            if "Success" not in res:
                raise ScreenError(f"Android didn't install PiPup: {res.strip()[:200]}")

        await self.adb(f"appops set {PIPUP_PACKAGE} SYSTEM_ALERT_WINDOW allow", *RESTART_CMDS)
        for _ in range(20):
            if await self.pipup_state() is not None:
                break
            await asyncio.sleep(1)
        else:
            raise ScreenError("PiPup was installed but didn't start; try a restart of the screen")
        await self.pipup_settings(updateChecks="false")
        return PIPUP_VERSION
