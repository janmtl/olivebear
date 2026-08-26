#!/usr/bin/env python3
"""MQTT -> ntfy bridge that turns Frigate detections into bear alerts.

Subscribes to Frigate's `events` topic, filters for bears (and optionally
"bear proxies": large animals the free COCO model tends to confuse bears
with), and pushes a notification with the event snapshot attached.
"""

import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt
import requests

log = logging.getLogger("bear-notifier")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TOPIC = os.environ.get("MQTT_TOPIC", "frigate/events")

FRIGATE_URL = os.environ.get("FRIGATE_URL", "http://frigate:5000").rstrip("/")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

ALERT_LABELS = {s.strip() for s in os.environ.get("ALERT_LABELS", "bear").split(",") if s.strip()}
PROXY_LABELS = {s.strip() for s in os.environ.get("PROXY_LABELS", "").split(",") if s.strip()}
PROXY_MIN_AREA = int(os.environ.get("PROXY_MIN_AREA", "30000"))
REQUIRED_ZONES = {s.strip() for s in os.environ.get("REQUIRED_ZONES", "").split(",") if s.strip()}
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "120"))

_last_sent: dict[str, float] = {}  # camera -> unix time of last push
_notified_events: set[str] = set()


def box_area(after: dict) -> int:
    box = after.get("box")
    if not box or len(box) != 4:
        return 0
    return max(0, (box[2] - box[0]) * (box[3] - box[1]))


def should_alert(after: dict) -> str | None:
    """Return a human title if this detection warrants a push, else None."""
    label = after.get("label", "")
    score = after.get("top_score") or after.get("score") or 0

    if REQUIRED_ZONES and not (REQUIRED_ZONES & set(after.get("entered_zones", []))):
        return None

    if label in ALERT_LABELS:
        return f"Bear on {after.get('camera', 'camera')} ({score:.0%})"

    if label in PROXY_LABELS and box_area(after) >= PROXY_MIN_AREA:
        return (
            f"Possible bear on {after.get('camera', 'camera')} "
            f"(detected as large {label}, {score:.0%})"
        )
    return None


def fetch_snapshot(event_id: str) -> bytes | None:
    try:
        r = requests.get(
            f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg",
            params={"bbox": 1, "quality": 90},
            timeout=10,
        )
        if r.ok and r.content:
            return r.content
    except requests.RequestException as exc:
        log.warning("snapshot fetch failed for %s: %s", event_id, exc)
    return None


def push(title: str, message: str, image: bytes | None = None) -> None:
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "bear",
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"

    if image:
        headers["Message"] = message
        headers["Filename"] = "bear.jpg"
        body: bytes | str = image
    else:
        body = message

    r = requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body, headers=headers, timeout=15)
    r.raise_for_status()
    log.info("pushed: %s", title)


def handle_event(payload: dict) -> None:
    event_type = payload.get("type")
    after = payload.get("after", {})
    event_id = after.get("id", "")
    camera = after.get("camera", "unknown")

    # Alert on "new" and on "update" (an event may only enter the zone or
    # gain a qualifying score mid-life), but never twice for the same event.
    if event_type not in ("new", "update") or event_id in _notified_events:
        return

    title = should_alert(after)
    if not title:
        return

    now = time.time()
    if now - _last_sent.get(camera, 0) < COOLDOWN_SECONDS:
        log.info("cooldown active for %s, skipping %s", camera, event_id)
        _notified_events.add(event_id)  # still one bear, don't retry later
        return

    snapshot = fetch_snapshot(event_id) if after.get("has_snapshot") else None
    try:
        push(title, f"Event {event_id} at {time.strftime('%H:%M:%S')}", snapshot)
        _last_sent[camera] = now
        _notified_events.add(event_id)
    except requests.RequestException as exc:
        log.error("ntfy push failed (will retry on next update): %s", exc)


def on_connect(client, userdata, flags, reason_code, properties=None):
    log.info("connected to mqtt (%s), subscribing to %s", reason_code, TOPIC)
    client.subscribe(TOPIC)


def on_message(client, userdata, msg):
    try:
        handle_event(json.loads(msg.payload))
    except Exception:
        log.exception("failed to process message")


def main() -> None:
    if "--test" in sys.argv:
        push("Test: bear-notifier is alive", "If you can read this, notifications work.")
        return

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
