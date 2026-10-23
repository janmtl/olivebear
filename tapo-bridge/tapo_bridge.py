#!/usr/bin/env python3
"""Smart-plug-as-tripwire bridge for battery cameras that lack RTSP (e.g. Tapo C403).

Event chain:
  camera's onboard PIR fires
    -> Tapo app Smart Action (or Alexa routine) flips a smart plug ON
    -> this bridge, polling the plug locally, sees it within ~1s
    -> resets the plug to OFF and wakes the matching Frigate camera via MQTT
       (frigate/<camera>/enabled/set ON) so bear detection runs on the
       camera's proprietary tapo:// stream for a window
    -> after WINDOW_SECONDS with no new triggers, puts the camera back to
       sleep so the battery camera stops streaming.

Hardening / fail-safe design (the camera must never be left streaming):
  - MQTT Last Will puts the camera to sleep if the bridge dies uncleanly,
    and SIGTERM/SIGINT publish an explicit OFF on clean shutdown.
  - Desired camera state is continuously reconciled: commands use QoS 1, the
    bridge subscribes to frigate/<camera>/enabled/state and re-asserts on any
    mismatch, and blindly re-asserts every RECONCILE_SECONDS regardless (this
    also corrects Frigate's persisted runtime state after a Frigate restart,
    and covers commands published before Frigate finished starting).
  - Plug triggers are edge-detected (OFF->ON), so a plug stuck ON (failed
    reset, repeated turn_off retries) cannot re-trigger or spam notices.
  - WAKE_CHECK_SECONDS into each window the bridge asks Frigate's stats API
    whether frames are actually flowing; if not, you get a "camera failed to
    wake" push instead of silence.
  - A plug that is unreachable for PLUG_OFFLINE_WARN_SECONDS triggers a
    rate-limited warning push (and a recovery notice), since a dead plug
    means a disarmed tripwire.
  - Optional canary: with STALE_TRIGGER_HOURS > 0, a push is sent when no
    PIR trigger has been seen for that long — on a hill with nightly
    wildlife, prolonged silence usually means the Tapo automation chain broke.
  - A heartbeat file backs the container HEALTHCHECK, so a wedged loop shows
    up as "unhealthy" in Container Manager instead of looking fine.
"""

import asyncio
import logging
import os
import signal
import sys
import time

import paho.mqtt.client as mqtt
import requests
from kasa import Discover

log = logging.getLogger("tapo-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PLUG_HOST = os.environ.get("PLUG_HOST", "")
# Tapo-branded plugs (P110/P115...) need your TP-Link account credentials for
# the local KLAP protocol. Kasa-branded plugs need none.
TAPO_USERNAME = os.environ.get("TAPO_USERNAME", "")
TAPO_PASSWORD = os.environ.get("TAPO_PASSWORD", "")

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
FRIGATE_CAMERA = os.environ.get("FRIGATE_CAMERA", "hillside_tapo")
FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")

WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "180"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "1.0"))
RECONCILE_SECONDS = int(os.environ.get("RECONCILE_SECONDS", "60"))
WAKE_CHECK_SECONDS = int(os.environ.get("WAKE_CHECK_SECONDS", "30"))
PLUG_OFFLINE_WARN_SECONDS = int(os.environ.get("PLUG_OFFLINE_WARN_SECONDS", "300"))
STALE_TRIGGER_HOURS = float(os.environ.get("STALE_TRIGGER_HOURS", "0"))  # 0 = off

NOTIFY_ON_MOTION = os.environ.get("NOTIFY_ON_MOTION", "false").lower() == "true"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

HEARTBEAT_FILE = "/tmp/heartbeat"

ENABLED_SET_TOPIC = f"frigate/{FRIGATE_CAMERA}/enabled/set"
ENABLED_STATE_TOPIC = f"frigate/{FRIGATE_CAMERA}/enabled/state"


class Notifier:
    """ntfy pushes with per-key rate limiting so health warnings can't spam."""

    def __init__(self):
        self._last: dict[str, float] = {}

    def push(self, title: str, message: str, tags: str = "warning", priority: str = "default"):
        if not NTFY_TOPIC:
            return
        headers = {"Title": title, "Tags": tags, "Priority": priority}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        try:
            requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=message, headers=headers, timeout=10)
        except requests.RequestException as exc:
            log.warning("ntfy push failed: %s", exc)

    def warn(self, key: str, title: str, message: str, min_interval: float = 3600, now: float | None = None) -> bool:
        """Rate-limited push; returns True if actually sent."""
        now = time.time() if now is None else now
        if key in self._last and now - self._last[key] < min_interval:
            return False
        self._last[key] = now
        self.push(title, message)
        return True

    def clear(self, key: str) -> bool:
        """Forget a warning key; returns True if it had fired (recovery notice due)."""
        return self._last.pop(key, None) is not None


class EdgeDetector:
    """Trigger on OFF->ON transitions only, so a plug stuck ON fires once."""

    def __init__(self):
        self._prev = False

    def rising(self, current: bool) -> bool:
        is_rising = current and not self._prev
        self._prev = current
        return is_rising


class WakeWindow:
    """State machine: PIR triggers open/extend a detection window; expiry closes it."""

    def __init__(self, set_camera_enabled, window_seconds: int):
        self._set_camera_enabled = set_camera_enabled
        self._window_seconds = window_seconds
        self._window_end = 0.0
        self.opened_at = 0.0
        self.camera_awake = False

    def on_trigger(self, now: float) -> None:
        self._window_end = now + self._window_seconds
        if not self.camera_awake:
            log.info("PIR trigger: waking %s for %ss", FRIGATE_CAMERA, self._window_seconds)
            self.opened_at = now
            self._set_camera_enabled(True)
            self.camera_awake = True
        else:
            log.info("PIR re-trigger: extending window")

    def tick(self, now: float) -> None:
        if self.camera_awake and now >= self._window_end:
            log.info("window expired: putting %s back to sleep", FRIGATE_CAMERA)
            self._set_camera_enabled(False)
            self.camera_awake = False


class FrigateLink:
    """MQTT link to Frigate with QoS 1, LWT fail-safe, and state reconciliation."""

    def __init__(self):
        self.desired_enabled = False
        self._reported: str | None = None  # last enabled/state payload seen
        self._last_assert = 0.0
        self._connected = False

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        # Fail-safe: if the bridge dies without a clean disconnect, the broker
        # tells Frigate to put the camera to sleep.
        self._client.will_set(ENABLED_SET_TOPIC, "OFF", qos=1)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self):
        self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        self._client.loop_start()

    def stop(self):
        # Clean disconnects don't fire the LWT, so sleep the camera explicitly.
        self.desired_enabled = False
        self.assert_state(force=True)
        time.sleep(1)  # give the in-flight QoS 1 publish a moment
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = True
        log.info("mqtt connected (%s)", reason_code)
        client.subscribe(ENABLED_STATE_TOPIC, qos=1)
        self.assert_state(force=True)  # re-assert after broker outages

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False
        log.warning("mqtt disconnected (%s), auto-reconnecting", reason_code)

    def _on_message(self, client, userdata, msg):
        self._reported = msg.payload.decode(errors="replace")

    @property
    def connected(self) -> bool:
        return self._connected

    def set_desired(self, enabled: bool) -> None:
        self.desired_enabled = enabled
        self.assert_state(force=True)

    def assert_state(self, force: bool = False) -> None:
        now = time.time()
        mismatch = self._reported is not None and self._reported != (
            "ON" if self.desired_enabled else "OFF"
        )
        due = now - self._last_assert >= RECONCILE_SECONDS
        if not (force or mismatch or due):
            return
        payload = "ON" if self.desired_enabled else "OFF"
        info = self._client.publish(ENABLED_SET_TOPIC, payload, qos=1)
        self._last_assert = now
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish %s=%s failed rc=%s (queued for reconnect)", ENABLED_SET_TOPIC, payload, info.rc)
        elif mismatch:
            log.info("reconciled camera state to %s (frigate reported %s)", payload, self._reported)


def camera_fps() -> float | None:
    """Frames flowing for the camera per Frigate's stats API, or None if unknown."""
    try:
        r = requests.get(f"{FRIGATE_URL}/api/stats", timeout=10)
        r.raise_for_status()
        return float(r.json()["cameras"][FRIGATE_CAMERA]["camera_fps"])
    except Exception as exc:
        log.warning("stats check failed: %s", exc)
        return None


async def run() -> None:
    if not PLUG_HOST:
        log.error("PLUG_HOST is not set — see .env.example (TAPO_PLUG_HOST)")
        sys.exit(1)

    notifier = Notifier()
    frigate = FrigateLink()
    frigate.start()
    frigate.set_desired(False)  # known state: asleep until the first trigger
    window = WakeWindow(frigate.set_desired, WINDOW_SECONDS)
    edge = EdgeDetector()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    plug = None
    plug_offline_since: float | None = None
    last_trigger = time.time()
    wake_checked = True  # no window open yet

    while not stop.is_set():
        now = time.time()

        # Heartbeat for the container HEALTHCHECK.
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(now))

        # --- poll the tripwire ------------------------------------------------
        try:
            if plug is None:
                plug = await Discover.discover_single(
                    PLUG_HOST,
                    username=TAPO_USERNAME or None,
                    password=TAPO_PASSWORD or None,
                )
                log.info("connected to plug %s (%s)", plug.alias, PLUG_HOST)
            await plug.update()

            if plug_offline_since is not None:
                plug_offline_since = None
                if notifier.clear("plug_offline"):
                    notifier.push("Tripwire restored", "Smart plug is reachable again.", tags="white_check_mark")

            plug_is_on = plug.is_on  # snapshot once; turn_off may mutate it
            if plug_is_on:
                try:
                    await plug.turn_off()  # re-arm; retried every poll if it fails
                except Exception as exc:
                    log.warning("plug reset failed (will retry): %s", exc)

            if edge.rising(plug_is_on):
                last_trigger = now
                was_awake = window.camera_awake
                window.on_trigger(now)
                if not was_awake:
                    wake_checked = False  # one wake-check per window, not per re-trigger
                if NOTIFY_ON_MOTION:
                    notifier.push(
                        f"Motion on {FRIGATE_CAMERA}",
                        "PIR trigger — waking camera for bear check",
                        tags="eyes",
                    )
        except Exception as exc:
            if plug_offline_since is None:
                plug_offline_since = now
                log.warning("plug poll failed (%s), reconnecting", exc)
            plug = None
            if now - plug_offline_since > PLUG_OFFLINE_WARN_SECONDS:
                notifier.warn(
                    "plug_offline",
                    "Bear tripwire is down",
                    f"Smart plug {PLUG_HOST} unreachable for "
                    f"{int((now - plug_offline_since) / 60)} min — camera motion "
                    "triggers are NOT being caught.",
                )
            await asyncio.sleep(5)

        # --- window lifecycle + fail-safes -----------------------------------
        window.tick(now)
        frigate.assert_state()

        if window.camera_awake and not wake_checked and now - window.opened_at >= WAKE_CHECK_SECONDS:
            wake_checked = True
            fps = camera_fps()
            if fps == 0.0:
                notifier.push(
                    "Camera failed to wake",
                    f"PIR fired but {FRIGATE_CAMERA} produced no frames "
                    f"{WAKE_CHECK_SECONDS}s into the window — check camera "
                    "battery/wifi. Motion was real; detection did not run.",
                )

        if STALE_TRIGGER_HOURS > 0 and now - last_trigger > STALE_TRIGGER_HOURS * 3600:
            if notifier.warn("stale", "No PIR triggers lately",
                             f"No motion trigger from the plug in {STALE_TRIGGER_HOURS:.0f}h. "
                             "If wildlife is normally active, check the Tapo automation, "
                             "plug, and camera.", min_interval=STALE_TRIGGER_HOURS * 3600):
                last_trigger = now  # re-arm the canary instead of re-warning hourly

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass

    log.info("shutting down: putting %s to sleep", FRIGATE_CAMERA)
    frigate.stop()


if __name__ == "__main__":
    asyncio.run(run())
