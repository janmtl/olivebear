"""Logic tests for the bridge (no network needed).

Run:  PLUG_HOST=1.2.3.4 python3 test_tapo_bridge.py
"""

import time

import tapo_bridge as tb


def test_wake_window():
    calls = []
    w = tb.WakeWindow(lambda on: calls.append(on), window_seconds=180)
    w.tick(999)
    assert calls == [], "idle tick must do nothing"
    w.on_trigger(1000)
    assert calls == [True] and w.camera_awake and w.opened_at == 1000
    w.on_trigger(1150)
    assert calls == [True], "re-trigger extends, no duplicate wake"
    w.tick(1300)
    assert calls == [True], "inside extended window"
    w.tick(1331)
    assert calls == [True, False] and not w.camera_awake, "expiry sleeps camera"
    w.on_trigger(2000)
    assert calls == [True, False, True] and w.opened_at == 2000


def test_edge_detector():
    e = tb.EdgeDetector()
    assert e.rising(False) is False
    assert e.rising(True) is True, "OFF->ON fires"
    assert e.rising(True) is False, "stuck ON must not re-fire"
    assert e.rising(False) is False
    assert e.rising(True) is True, "re-armed plug fires again"


def test_notifier_rate_limit():
    sent = []
    n = tb.Notifier()
    n.push = lambda title, msg, tags="warning", priority="default": sent.append(title)
    assert n.warn("k", "T", "m", min_interval=3600, now=1000) is True, "first warning always fires"
    assert n.warn("k", "T", "m", min_interval=3600, now=2000) is False, "inside interval"
    assert n.warn("k", "T", "m", min_interval=3600, now=4601) is True, "after interval"
    assert n.clear("k") is True, "recovery notice due once"
    assert n.clear("k") is False
    assert sent == ["T", "T"]


class _FakeInfo:
    rc = 0


class _FakeClient:
    def __init__(self, pubs):
        self._pubs = pubs

    def publish(self, topic, payload, qos=0):
        self._pubs.append(payload)
        return _FakeInfo()


def test_frigate_link_reconciliation():
    pubs = []
    fl = tb.FrigateLink.__new__(tb.FrigateLink)
    fl.desired_enabled = False
    fl._reported = None
    fl._last_assert = time.time()
    fl._client = _FakeClient(pubs)

    fl.assert_state()
    assert pubs == [], "no mismatch, not due: no publish"
    fl._reported = "ON"
    fl.assert_state()
    assert pubs == ["OFF"], "mismatch with frigate state: reconcile"
    fl._reported = "OFF"
    fl._last_assert = 0
    fl.assert_state()
    assert pubs == ["OFF", "OFF"], "periodic blind re-assert"
    fl.set_desired(True)
    assert pubs[-1] == "ON", "desired-state change publishes immediately"


if __name__ == "__main__":
    test_wake_window()
    test_edge_detector()
    test_notifier_rate_limit()
    test_frigate_link_reconciliation()
    print("ALL TESTS PASS")
