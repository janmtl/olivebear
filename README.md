# olivebear — bear detection on a Synology NAS

A self-hosted bear detection stack for PoE cameras pointed at the wooded hill
behind your deck. Everything runs natively on the NAS in Docker:

- **[Frigate NVR](https://frigate.video)** decodes the camera streams and runs
  object detection on the GPU. `bear` is a class it already knows.
- **Mosquitto** (MQTT broker) carries detection events.
- **bear-notifier** (small Python service in this repo) filters events down to
  bears only and pushes a notification with a snapshot to your phone via
  [ntfy](https://ntfy.sh) — day or night, no cloud account required.

```
PoE cameras ──rtsp──> Frigate (GPU decode + detection) ──mqtt──> bear-notifier ──> ntfy app on your phone
                          └── 24/7 + event recordings on the NAS volume
```

---

## 1. First: figure out which GPU your NAS actually has

Docker containers **can** access the GPU on a Synology, but *how* depends on
the model. SSH into the NAS (`Control Panel → Terminal & SNMP → Enable SSH`)
and run:

```bash
ls -la /dev/dri        # Intel iGPU check
cat /proc/cpuinfo | grep "model name" | head -1
```

**Case A — `/dev/dri/renderD128` exists (Intel-based `+` models: DS920+, DS918+, DS423+, DS224+, DS720+, DS1019+…).**
You have an Intel CPU with QuickSync (e.g. Celeron J4125, J4025, Pentium).
This is the easy path: the iGPU is passed into the container as a plain
device (`/dev/dri`), and Frigate uses it for **both** video decoding (VAAPI)
and object detection (OpenVINO). No drivers to install. This repo's
`docker-compose.yml` is set up for this case by default.

> Note: AMD Ryzen-based models (DS923+, DS1522+, DS1621+…) have **no iGPU**
> and no `/dev/dri`. See "Case C" below.

**Case B — DVA series (DVA3221 with its NVIDIA GTX 1650).**
Synology only exposes the NVIDIA GPU to its own Surveillance Station DVA
tasks; there is no official Docker GPU runtime. The community-maintained
[pdbear/syno_nvidia_gpu_driver](https://github.com/pdbear/syno_nvidia_gpu_driver)
package (driver 535 / CUDA 12.2) adds `nvidia-smi` and the `--runtime=nvidia`
Docker runtime. Once installed, uncomment the NVIDIA block in
`docker-compose.yml` and switch to the `-tensorrt` Frigate image. It works,
but you're off Synology's supported path — DSM updates can break it. An
alternative on a DVA is to skip Docker detection entirely and let
Surveillance Station's own deep-learning tasks do generic "animal" intrusion
detection, but it cannot distinguish a bear from a deer, so this repo assumes
Frigate.

**Case C — no usable GPU.**
Frigate's OpenVINO detector also runs on the CPU. With the small default
model, one or two cameras at 720p/5fps detect fine on a Celeron — inference
is slower (~50–100 ms vs ~15–20 ms) but well within budget because detection
only runs where motion happens. Remove the `devices:` block and set
`device: CPU` in `frigate/config/config.yml`.

## 2. Why Frigate, and which model — the "stationary camera" advantage

Your instinct is right: fixed cameras make this much easier, and Frigate is
built around exactly that assumption:

- **Motion-gated detection.** Frigate runs background subtraction on the fixed
  scene first, and only sends motion regions to the neural net. A wooded hill
  swaying in the wind is handled with **motion masks** (paint out treetops that
  always move) so the GPU isn't burned on rustling leaves.
- **Zones.** You draw a polygon over the hill; alerts only fire for objects
  inside it. A bear on the hill notifies you, your dog on the deck doesn't.
- **Size filters.** On a fixed camera a bear at a known distance occupies a
  predictable pixel-area range. `min_area`/`max_area` filters kill
  squirrel-sized and house-sized false positives cheaply.

**Detection model:** the default model bundled with Frigate is trained on the
COCO dataset, which includes `bear` (class 21) — so bear detection works out
of the box with zero training. Honest caveat: COCO's bear photos are
wildlife-photography style (plus some teddy bears), not grainy security-camera
footage, so expect real-world confidence around 50–70% and the occasional bear
labeled `dog` or `cow`. This config therefore:

1. Tracks `bear` with a deliberately low `min_score` (stationary camera + zone
   + size filters absorb the false-positive risk that a low score normally carries).
2. Also tracks `dog`, `cat` and `cow` as *bear proxies* — a large "dog" or
   "cow" on a wooded hill at 2 AM is worth a notification. The notifier treats
   oversized proxies as bears (configurable).

**If you want it noticeably better:** a [Frigate+](https://frigate.video/plus)
subscription (~$50/yr) gets you a YOLOv9 model fine-tuned on real security-cam
footage — its base model explicitly lists `bear` among the labels with
dedicated training data, including night/IR imagery, and you can fine-tune it
further on snapshots of *your* actual hill. It drops into this same config
with a two-line change. Recommended once you've confirmed the pipeline works.
(You do **not** need a Google Coral — its driver is unmaintained and Frigate no
longer recommends it for new setups.)

## 3. Night detection

The model doesn't care whether the image is color or IR grayscale — it cares
whether the bear is *visible*. Night performance is decided by optics:

- **IR range is the spec that matters.** Built-in camera IR is usually rated
  ~30 m and effective at less. If the hill is farther, add a standalone
  850 nm IR illuminator aimed at it (12 V, cheap, mounts anywhere). Avoid
  940 nm ("no-glow") — cameras see it far more weakly.
- **Camera picks:** a 4MP+ turret from Dahua / Hikvision (or their OEMs:
  Loryta, EmpireTech, Amcrest) with a large sensor (1/1.8″ if budget allows —
  marketed as "ColorVu"/"Full-Color") and **H.264 dual streams over RTSP**.
  Turrets beat bullets outdoors (no spider-web IR flare). Reolink works but
  needs quirky stream settings in Frigate; Dahua/Hikvision are plug-and-play.
- **Camera settings:** cap shutter/exposure at 1/60s or faster at night —
  auto modes drift to 1/12s in the dark and a walking bear becomes an
  undetectable smear. Fixed shutter + more IR light beats bright-but-blurry.
- **Two cameras > one:** if you're buying more than one, overlap their views
  from different angles rather than tiling the hill — occlusion behind trees
  is the main miss-cause, and a second angle also confirms real events.

## 4. Install on the NAS

Prereq: **Container Manager** installed from Package Center (DSM 7.2+).

1. **Create the folder layout** (SSH or File Station), e.g. under
   `/volume1/docker/olivebear`, and copy this repo's contents there.
   Recordings go to a separate share so they can have their own quota:
   create `/volume1/frigate_media`.

2. **Grant the container access to the iGPU** (Case A). On Synology,
   `/dev/dri` is owned by root:`videodriver`, so containers can't open it by
   default. Find the group id and put it in `.env`:

   ```bash
   grep videodriver /etc/group     # e.g. videodriver:x:937:
   ```

3. **Configure:**
   - `cp .env.example .env` and fill it in (RTSP password, ntfy topic — pick
     something unguessable like `bears-<random-string>`, and the
     `videodriver` GID from step 2).
   - Edit `frigate/config/config.yml`: put your cameras' RTSP URLs in the
     `go2rtc:` section (IP, main stream + substream paths).

4. **Launch:** Container Manager → *Project* → *Create* → point it at the
   folder (it reads `docker-compose.yml`), or via SSH:

   ```bash
   cd /volume1/docker/olivebear && sudo docker compose up -d
   ```

5. **Draw your masks and zones:** open `https://<nas-ip>:8971`, go to
   *Settings → Motion Masks* and paint out perpetually-moving treetops, then
   *Settings → Zones* and draw a `wooded_hill` polygon over the hill. (The
   config ships with placeholder coordinates — the UI writes real ones back
   to the config for you.)

6. **Phone notifications:** install the **ntfy** app (iOS/Android),
   subscribe to the topic you chose in `.env`. Done — the notifier container
   is already publishing to it. Test it end-to-end:

   ```bash
   sudo docker exec bear-notifier python /app/bear_notifier.py --test
   ```

   *Alternative:* Frigate ≥0.16 also has native web-push notifications
   (Settings → Notifications) — those require HTTPS access to Frigate and
   notify on all review alerts, not just bears, which is why this repo ships
   the ntfy bridge instead. Home Assistant users can subscribe to the same
   MQTT broker and skip ntfy entirely.

## 5. Tuning (expect a 1–2 week shake-out)

- Frigate's **Explore** view shows every detection with its score. After a few
  days, review what fired: raise `threshold` if leaves/shadows sneak through,
  lower `min_score` if a real animal was missed.
- The notifier has a **cooldown** (default 120 s per camera) so one bear
  ambling across the hill is one notification, not thirty.
- If you get bears labeled as `dog`/`cow` frequently, that's the free COCO
  model's ceiling — switch to Frigate+ rather than fighting thresholds.
- Deer will be your main confusable. The size filter helps (deer are taller
  and thinner than a bear's bounding box at the same spot); Frigate+
  distinguishes `deer` as its own label.

## 6. Coexisting with Surveillance Station

Keep it. Frigate connects to the cameras' RTSP streams directly and in
parallel — cameras happily serve both. If a camera limits concurrent RTSP
clients, point Surveillance Station at Frigate's restream
(`rtsp://<nas-ip>:8554/<camera-name>`) so the camera only serves one client.
Mind CPU/RAM headroom: Frigate with GPU decode uses little CPU, but both
systems recording 24/7 doubles disk write load — recording *alerts only* in
Frigate (see the commented `record:` option) avoids that.

## Repo layout

```
docker-compose.yml          # Frigate + Mosquitto + bear-notifier (Synology-tuned)
.env.example                # secrets & knobs — copy to .env
frigate/config/config.yml   # cameras, GPU detector, bear tracking, zones
mosquitto/mosquitto.conf    # internal-only MQTT broker config
notifier/bear_notifier.py   # MQTT → ntfy bridge, bear-filtered, with snapshots
```
