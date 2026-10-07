# Camera monitoring — local AI person detection

A small home system that watches an IP camera and **sends a WhatsApp alert when it detects a person**,
with the photo inside the message. Video never leaves the computer: detection runs locally with an
ONNX model and the only thing that goes out is the alert with its picture.

It was built to watch a house in Puerto Montt with an ordinary RTSP camera and a PC that was on
anyway. No cloud, no accounts, no cards required.

- Author: **Christopher Aguila** ([@khrizs3422](https://github.com/khrizs3422))
- License: MIT

## What's inside

| File | What it does |
| --- | --- |
| `camera_watch.py` | Detector: reads the RTSP stream, runs YOLO11n locally, applies interest zones and day/night thresholds, stores the event and queues the alert. |
| `alertas_whatsapp.py` | Alert queue: retries what failed and reports when the WhatsApp service is down. |
| `whatsapp/whatsapp_alerts.js` | Local Node service that links to WhatsApp as a device and sends the photo with the date. Listens on `127.0.0.1:8780`. |
| `camera_watchdog.py` | Supervisor: restarts the detector if the process dies or its heartbeat goes stale. |
| `camera_health.py` | Health check every 10 minutes (detector, supervisor, queue, WhatsApp service). Silent while everything is fine; it only speaks on problems or recoveries. |
| `puente_camara_puertomontt.py` | Serves the RTSP stream as HTTP video (`:8088`) to watch the camera from a browser on the LAN or through a tunnel. |
| `mms_android_bridge/` | Android app that reads incoming SMS/MMS on the phone and forwards them to a service of your own. Used when alerts had to arrive by SMS. |
| `run_camera_hidden.vbs` | Starts the supervisor without a window (for Windows Task Scheduler). |

## How it works

1. The detector grabs frames from the RTSP stream and first checks for motion (a cheap prefilter).
   No motion, no model.
2. On motion it runs person detection with **YOLO11n** (`onnxruntime`) and verifies the person falls
   inside one of the **interest zones** drawn as polygons in the config.
3. To avoid false alarms it requires several hits in a row (`confirm_hits` within a time window) and
   applies a **cooldown** between alerts.
4. At night it uses different thresholds, because a dark image lowers model confidence.
5. The event is stored with several `.webp` images and the alert is queued; the WhatsApp service
   takes it and sends the picture with the date.
6. The supervisor watches the detector's heartbeat and the health check watches the whole set.

## Requirements

- Windows 10/11 (the launchers and scheduled tasks are Windows-only; the rest is portable).
- Python 3.11 or newer.
- An IP camera with RTSP.
- Node.js 20 or newer, only for the WhatsApp service.
- The detection model: **it is not shipped in the repository** (~11 MB). Export or download
  `yolo11n.onnx` (Ultralytics YOLO11 nano in ONNX format) and place it at `models/yolo11n.onnx`.

## Install

```bash
git clone https://github.com/khrizs3422/seguimiento-camaras.git
cd seguimiento-camaras
pip install -r requirements.txt
mkdir -p models && cp /path/to/your/yolo11n.onnx models/yolo11n.onnx
cp .env.example .env          # optional: every value has a sensible default
```

Set the camera in `camera_ai.json` (`rtsp_url`) or through `CAMARA_RTSP`, and adjust the interest
zones to your yard.

Link WhatsApp once:

```bash
cd whatsapp
npm install
node whatsapp_alerts.js       # prints a QR code: scan it from your phone
```

The session is stored in `whatsapp/sesion/`, so you scan only once. Then set the recipient in
`whatsapp/config.json` (a number in international format or a group id ending in `@g.us`); if it is
empty the service sends nothing and reports it as an error.

## Usage

```bash
python camera_watch.py --once --no-notify   # single test pass, no alerts
python camera_watch.py                      # run the detector
python alertas_whatsapp.py --estado         # alert queue status
python alertas_whatsapp.py --texto "test"   # send a short test message
python camera_health.py                     # check the whole set and alert on problems
python puente_camara_puertomontt.py         # viewer: http://localhost:8088/video
```

To leave it running on its own, in Windows Task Scheduler: `run_camera_hidden.vbs` with restart on
failure, plus `camera_health.py` every 10 minutes.

## Main settings (`camera_ai.json`)

| Key | Purpose |
| --- | --- |
| `rtsp_url` | The camera. Can also come from `CAMARA_RTSP`. |
| `interest_zones` | Polygons (in pixels) where a person actually matters: yard, gate, hallway. |
| `confidence_threshold` / `night_confidence_threshold` | Minimum confidence by day and by night. |
| `confirm_hits` / `confirm_window_seconds` | How many consecutive hits are required and in how long. |
| `alert_cooldown_seconds` | Minimum time between alerts. |
| `enable_motion_prefilter` | Skips the model when nothing moves (saves CPU). |
| `event_images`, `keep_event_days` | How many images per event and how many days they are kept. |

You can also tune it with `.env`: `CAMARA_RTSP`, `WHATSAPP_ALERTS_URL`, `WHATSAPP_PORT`,
`ALERTA_VIGENCIA_HORAS`, `CLOUDFLARED` (path to the executable, only for the remote viewer).

## Privacy

Analysis is 100% local. Video is never uploaded anywhere and no temporary public links are created
for alerts: what goes out is the event photo through the owner's own WhatsApp. The `events/`,
`logs/`, `models/` and `whatsapp/sesion/` folders stay out of the repository.

## Known limitations

- **One camera per instance.** For several cameras, run several copies with their own port and event
  folder.
- The model is **YOLO11n** (the lightest one): it detects people well by day and reasonably at night,
  but it does not tell animals from people at a distance and does not read plates.
- The WhatsApp service uses an **unofficial** library (Baileys): if WhatsApp changes something the
  library has to be updated, and if the device gets unlinked you must scan the QR again.
- The tunnel viewer is for watching, not for recording: recording would be the camera's own RTSP
  stream.
- Built and tested on Windows; launchers and scheduled tasks are Windows-only.

## License

MIT — see [LICENSE](LICENSE).
