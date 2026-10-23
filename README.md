# olivebear — bear detection on a Synology NAS with a battery Tapo camera

A self-hosted bear detection stack for a Tapo C403 (or similar battery
camera) pointed at the wooded hill behind your deck. Everything runs natively
on the NAS in Docker; detection is local, day or night, with push
notifications to your phone.

Battery Tapo cameras have **no RTSP/ONVIF** (TP-Link disables both to protect
battery life) and no local motion-event push, so this stack uses a two-part
trick:

- **Smart-plug tripwire:** a Tapo app automation flips a cheap smart plug ON
  when the camera's PIR detects motion. A plug *is* locally pollable — it
  turns a phone-only notification into a LAN-visible signal.
- **On-demand streaming:** go2rtc (embedded in Frigate) speaks TP-Link's
  proprietary `tapo://` protocol, the same one the Tapo app uses, and only
  connects while needed.

```
C403 PIR fires ──Tapo automation──> smart plug ON
                                       │ (tapo-bridge polls the plug ~1s, LAN-only)
                                       ▼
          bridge resets plug, wakes the camera in Frigate over MQTT
                                       ▼
   Frigate pulls the tapo:// stream, runs bear detection on the GPU (3 min)
                                       ▼
 bear found ──mqtt──> bear-notifier ──> ntfy app on your phone
                                       ▼
                        window ends -> camera back to sleep
```

Components: **[Frigate NVR](https://frigate.video)** (GPU decode + detection,
`bear` is a class it already knows), **Mosquitto** (MQTT), **tapo-bridge**
(plug poller / camera waker), **bear-notifier** (bear-filtered pushes via
[ntfy](https://ntfy.sh), no cloud account required).

## 1. First: figure out which GPU your NAS actually has

Docker containers **can** access the GPU on a Synology, but *how* depends on
the model. SSH into the NAS (`Control Panel → Terminal & SNMP → Enable SSH`)
and run:

```bash
ls -la /dev/dri        # Intel iGPU check
cat /proc/cpuinfo | grep "model name" | head -1
```

**Case A — `/dev/dri/renderD128` exists (Intel-based `+` models: DS920+, DS918+, DS423+, DS224+, DS720+, DS1019+…).**
You have an Intel CPU with QuickSync. This is the easy path: the iGPU is
passed into the container as a plain device (`/dev/dri`), and Frigate uses it
for **both** video decoding (VAAPI) and object detection (OpenVINO). No
drivers to install. This repo's `docker-compose.yml` is set up for this case
by default.

> Note: AMD Ryzen-based models (DS923+, DS1522+, DS1621+…) have **no iGPU**
> and no `/dev/dri`. See Case C.

**Case B — DVA series (DVA3221 with its NVIDIA GTX 1650).**
Synology only exposes the NVIDIA GPU to its own Surveillance Station; there
is no official Docker GPU runtime. The community-maintained
[pdbear/syno_nvidia_gpu_driver](https://github.com/pdbear/syno_nvidia_gpu_driver)
package (driver 535 / CUDA 12.2) adds `nvidia-smi` and the `--runtime=nvidia`
Docker runtime. Once installed, uncomment the NVIDIA block in
`docker-compose.yml` and switch to the `-tensorrt` Frigate image. It works,
but you're off Synology's supported path — DSM updates can break it.

**Case C — no usable GPU.**
Frigate's OpenVINO detector also runs on the CPU. With the small default
model and a single camera that only streams during motion windows, a Celeron
copes fine — inference is slower (~50–100 ms vs ~15–20 ms) but detection only
runs where motion happens. Remove the `devices:` block from the compose file
and set `device: CPU` in `frigate/config/config.yml`.

## 2. Detection model

The default model bundled with Frigate is trained on the COCO dataset, which
includes `bear` (class 21) — so bear detection works out of the box with zero
training. Honest caveat: COCO's bear photos are wildlife-photography style
(plus some teddy bears), not grainy security-camera footage, so expect
real-world confidence around 50–70% and the occasional bear labeled `dog` or
`cow`. The config therefore:

1. Tracks `bear` with a deliberately low `min_score` — the fixed camera's
   zone and size filters absorb the false-positive risk that a low score
   normally carries.
2. Also tracks `dog`, `cat` and `cow` as *bear proxies* — the notifier treats
   bear-sized proxies as bears (a large "dog" on a wooded hill at 2 AM is
   worth a notification; a house-pet-sized one is suppressed).

**If you want it noticeably better:** a [Frigate+](https://frigate.video/plus)
subscription (~$50/yr) gets you a YOLOv9 model fine-tuned on real security-cam
footage — its base model explicitly lists `bear` among the labels with
dedicated training data, including night/IR imagery, and you can fine-tune it
further on snapshots of *your* actual hill. It drops into this config with a
two-line change. (You do **not** need a Google Coral — its driver is
unmaintained and Frigate no longer recommends it for new setups.)

## 3. Install on the NAS

Prereqs: **Container Manager** from Package Center (DSM 7.2+), the C403 set
up in the Tapo app, and any Tapo (P110/P115) or Kasa smart plug (~$10 — it
needs no load plugged into it; it's purely a signal flag).

1. **Folder layout:** copy this repo to e.g. `/volume1/docker/olivebear` and
   create `/volume1/frigate_media` for recordings.

2. **iGPU access** (Case A): find the `videodriver` group id for `.env`:

   ```bash
   grep videodriver /etc/group     # e.g. videodriver:x:937:
   ```

3. **Tapo app automation:** *Smart Actions → New automation →* **when** the
   C403 detects motion (or person/animal if your firmware offers it) →
   **then** turn on the plug. Add an auto-off after a minute as a
   belt-and-braces reset (the bridge also resets it immediately). If your app
   version doesn't offer the camera as a trigger, an Alexa/Google routine
   ("When C403 detects motion → turn on plug") does the same thing.

4. **Configure:** `cp .env.example .env` and fill it in (Tapo cloud password,
   plug IP + TP-Link credentials, ntfy topic — pick something unguessable —
   and the `videodriver` GID). Put the camera's LAN IP in
   `frigate/config/config.yml` under `go2rtc:`.

5. **Launch:** Container Manager → *Project* → *Create* → point it at the
   folder, or via SSH:

   ```bash
   cd /volume1/docker/olivebear && sudo docker compose up -d
   ```

6. **Draw the zone:** open `https://<nas-ip>:8971` → *Settings → Zones* and
   redraw `wooded_hill` over the actual hill (it ships covering the full
   frame). Paint out perpetually-swaying treetops under *Motion Masks*.

7. **Phone notifications:** install the **ntfy** app (iOS/Android), subscribe
   to your topic. Test end-to-end:

   ```bash
   sudo docker exec bear-notifier python /app/bear_notifier.py --test
   ```

## 4. Fail-safe behavior

The tripwire chain is hardened around one rule: when anything breaks, the
camera must end up *asleep* (battery protected), and you must find out.

- If the bridge crashes, an MQTT Last Will puts the camera to sleep; clean
  shutdowns (`docker compose down`) do the same explicitly.
- The bridge continuously reconciles the camera's actual state in Frigate
  against what it should be (QoS 1 commands + state subscription + periodic
  re-assert), which also corrects things after Frigate or broker restarts.
- Plug triggers are edge-detected, so a plug stuck ON can't re-trigger or
  spam; the reset is retried every poll.
- 30 s into each wake window the bridge verifies frames are actually flowing
  from the camera; if not, you get a "camera failed to wake — motion was
  real" push instead of silent nothing.
- If the plug goes unreachable for 5+ minutes you get a "tripwire is down"
  push (rate-limited, with a recovery notice) — a dead plug means a disarmed
  tripwire, which is otherwise invisible.
- Optional canary (`STALE_TRIGGER_HOURS`): no PIR triggers for N hours → one
  reminder push, in case the Tapo automation silently broke. Enable it once
  you know the hill's normal wildlife rhythm.
- The bridge container has a HEALTHCHECK tied to the poll loop's heartbeat,
  so a wedged bridge shows as *unhealthy* in Container Manager.

## 5. Night, battery, and known limits

- **Night:** detection works on the C403's IR/spotlight imagery as-is — the
  model doesn't care whether frames are color or grayscale, only whether the
  bear is visible. If the hill is beyond the built-in IR's reach, a cheap
  standalone 850 nm IR illuminator aimed at it helps a lot (avoid 940 nm
  "no-glow" — cameras see it far more weakly).
- **Battery:** streaming during motion windows costs battery beyond normal
  app use. If the hill gets a lot of nightly traffic, pair the camera with
  its solar panel — or set `NOTIFY_ON_MOTION: "true"` and skip the Frigate
  wake entirely for plain motion pushes at zero extra battery cost.
- **Latency:** the PIR→plug→bridge→stream chain takes a few seconds, so
  Frigate joins ~3–8 s after motion starts. Bears amble — in practice they're
  still in frame. The Tapo automation step routes through TP-Link's cloud, so
  the *trigger* isn't fully local (detection still is).
- **Wake flakiness:** sleeping Tapo cams occasionally need a retry to wake;
  Frigate's ffmpeg watchdog handles it, but expect the odd missed window —
  the bridge's wake-check push tells you when that happens.

## 6. Tuning (expect a 1–2 week shake-out)

- Frigate's **Explore** view shows every detection with its score. After a
  few days, raise `threshold` if shadows sneak through, lower `min_score` if
  a real animal was missed.
- The notifier has a **cooldown** (default 120 s per camera) so one bear
  ambling across the hill is one notification, not thirty.
- If bears keep getting labeled `dog`/`cow`, that's the free COCO model's
  ceiling — switch to Frigate+ rather than fighting thresholds. Deer are the
  main confusable; Frigate+ has `deer` as its own label.

## Repo layout

```
docker-compose.yml          # Frigate + Mosquitto + tapo-bridge + bear-notifier
.env.example                # secrets & knobs — copy to .env
frigate/config/config.yml   # camera, GPU detector, bear tracking, zone
mosquitto/mosquitto.conf    # internal-only MQTT broker config
tapo-bridge/tapo_bridge.py  # smart-plug tripwire poller + camera wake/sleep
notifier/bear_notifier.py   # MQTT → ntfy bridge, bear-filtered, with snapshots
```
