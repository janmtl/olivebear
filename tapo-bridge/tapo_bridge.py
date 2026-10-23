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

Optionally also sends a plain "motion detected" ntfy push on every trigger
(NOTIFY_ON_MOTION=true), independent of whether a bear is then confirmed.
"""

import asyncio
import logging
import os
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

WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "180"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "1.0"))

NOTIFY_ON_MOTION = os.environ.get("NOTIFY_ON_MOTION", "false").lower() == "true"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")


def push_motion_notice() -> None:
    if not (NOTIFY_ON_MOTION and NTFY_TOPIC):
        return
    headers = {"Title": f"Motion on {FRIGATE_CAMERA}", "Tags": "eyes", "Priority": "default"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data="PIR trigger — waking camera for bear check",
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        log.warning("motion notice push failed: %s", exc)


class WakeWindow:
    """State machine: PIR triggers open/extend a detection window; expiry closes it."""

    def __init__(self, set_camera_enabled, window_seconds: int):
        self._set_camera_enabled = set_camera_enabled
        self._window_seconds = window_seconds
        self._window_end = 0.0
        self.camera_awake = False

    def on_trigger(self, now: float) -> None:
        self._window_end = now + self._window_seconds
        if not self.camera_awake:
            log.info("PIR trigger: waking %s for %ss", FRIGATE_CAMERA, self._window_seconds)
            self._set_camera_enabled(True)
            self.camera_awake = True
        else:
            log.info("PIR re-trigger: extending window")

    def tick(self, now: float) -> None:
        if self.camera_awake and now >= self._window_end:
            log.info("window expired: putting %s back to sleep", FRIGATE_CAMERA)
            self._set_camera_enabled(False)
            self.camera_awake = False


def make_mqtt_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


async def run() -> None:
    if not PLUG_HOST:
        log.error("PLUG_HOST is not set — see .env.example (TAPO_PLUG_HOST)")
        sys.exit(1)

    client = make_mqtt_client()

    def set_camera_enabled(enabled: bool) -> None:
        client.publish(f"frigate/{FRIGATE_CAMERA}/enabled/set", "ON" if enabled else "OFF")

    # Start from a known state: camera asleep until the first trigger.
    # (Frigate persists the runtime toggle across restarts, so don't rely on defaults.)
    set_camera_enabled(False)
    window = WakeWindow(set_camera_enabled, WINDOW_SECONDS)

    plug = None
    while True:
        try:
            if plug is None:
                plug = await Discover.discover_single(
                    PLUG_HOST,
                    username=TAPO_USERNAME or None,
                    password=TAPO_PASSWORD or None,
                )
                log.info("connected to plug %s (%s)", plug.alias, PLUG_HOST)

            await plug.update()
            if plug.is_on:
                await plug.turn_off()  # re-arm the tripwire immediately
                window.on_trigger(time.time())
                push_motion_notice()
        except Exception as exc:
            log.warning("plug poll failed (%s), reconnecting", exc)
            plug = None
            await asyncio.sleep(5)

        window.tick(time.time())
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
