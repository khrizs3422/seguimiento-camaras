#!/usr/bin/env python3
"""Detector local de personas para una cámara IP RTSP.

Uso seguro:
  python camera_watch.py --once --no-notify
  python camera_watch.py

Procesa localmente. No sube video a la nube. Las alertas se encolan en
alertas_whatsapp.py y se envian por WhatsApp con la foto y la fecha.
No usa tuneles ni enlaces publicos temporales.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import onnxruntime as ort

# Los datos del detector viven junto a este archivo. El proyecto es autonomo:
# no depende de ningun framework externo. Las alertas salen por WhatsApp con
# alertas_whatsapp.py, que habla con el servicio local del puerto 8780.
BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "models" / "yolo11n.onnx"
EVENT_DIR = BASE_DIR / "events"
CONFIG_PATH = BASE_DIR / "camera_ai.json"
STATE_PATH = BASE_DIR / "camera_ai_state.json"
LOG_PATH = BASE_DIR / "logs" / "camera_ai.log"
CHILE = ZoneInfo("America/Santiago")

COCO_CLASSES = {
    0: "persona",
}


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    """Write operational state without exposing a partially written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_state(status: str, **details: Any) -> None:
    """Best-effort heartbeat consumed by the independent watchdog."""
    try:
        atomic_json(STATE_PATH, {
            "updated": time.time(),
            "pid": os.getpid(),
            "status": status,
            **details,
        })
    except OSError:
        # Losing the health file must never stop camera processing.
        pass

DEFAULT_CONFIG = {
    "rtsp_url": "rtsp://usuario:clave@IP_DE_TU_CAMARA:554/",
    "notification_channel": "whatsapp",
    "sample_interval_seconds": 0.2,
    "confidence_threshold": 0.62,
    "night_brightness_threshold": 85.0,
    "night_confidence_threshold": 0.45,
    "night_confirm_hits": 2,
    "nms_threshold": 0.45,
    "confirm_hits": 3,
    "confirm_window_seconds": 5.0,
    "alert_cooldown_seconds": 180,
    "gate_cooldown_seconds": 60,
    "street_cooldown_seconds": 180,
    "fence_confidence_threshold": 0.45,
    "fence_confirm_hits": 2,
    "loitering_seconds": 15.0,
    "vehicle_stop_seconds": 25.0,
    "send_clip": True,
    "enable_motion_prefilter": True,
    "presence_reset_seconds": 180.0,
    "motion_mean_threshold": 5.0,
    "motion_fraction_threshold": 0.015,
    "night_motion_mean_threshold": 2.5,
    "night_motion_fraction_threshold": 0.008,
    "reconnect_delay_seconds": 1.0,
    "stream_fps": 2.0,
    "stream_width": 1920,
    "zone_reference_width": 1280,
    "zone_reference_height": 720,
    "top_exclusion_pixels": 24,
    "send_timeout_seconds": 15,
    "interest_zones": [
        {"name": "propiedad_y_accesos_completos", "polygon": [[0, 70], [1280, 70], [1280, 720], [0, 720]]}
    ],
    "input_size": 640,
    "webp_quality": 82,
    "event_images": 5,
    "keep_event_days": 60,
}


def ensure_config() -> dict[str, Any]:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise RuntimeError(f"Configuración inválida en {CONFIG_PATH}: {exc}") from exc
    else:
        cfg = DEFAULT_CONFIG.copy()
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)
    return merged


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("camera_ai")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s [CAMERA-AI] %(levelname)s: %(message)s")
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def load_model(logger: logging.Logger, input_size: int):
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"No existe el modelo ONNX: {MODEL_PATH}")
    options = ort.SessionOptions()
    options.intra_op_num_threads = min(2, os.cpu_count() or 1)
    options.inter_op_num_threads = 1
    options.add_session_config_entry('session.intra_op.allow_spinning','0')
    cv2.setNumThreads(1)
    session = ort.InferenceSession(str(MODEL_PATH), sess_options=options,
                                  providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    logger.info("Modelo cargado: %s; proveedor=%s", MODEL_PATH.name, session.get_providers()[0])
    return session, input_name, input_size


def letterbox(image: np.ndarray, size: int):
    h, w = image.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    left, top = (size - nw) // 2, (size - nh) // 2
    canvas[top:top + nh, left:left + nw] = resized
    blob = canvas[:, :, ::-1].astype(np.float32) / 255.0
    blob = np.transpose(blob, (2, 0, 1))[None, ...]
    return blob, scale, left, top


def check_scene_motion(frame: np.ndarray, bg_gray: np.ndarray | None) -> tuple[bool, np.ndarray, float]:
    """Pre-filtro liviano (<1ms) para omitir inferencia YOLO en escenas completamente estáticas."""
    small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    if bg_gray is None:
        return True, gray, 100.0
    diff = cv2.absdiff(bg_gray, gray)
    diff_mean = float(np.mean(diff))
    changed_pixels = int(np.count_nonzero(diff > 18))
    has_motion = changed_pixels >= 120 or diff_mean >= 1.5
    return has_motion, gray, diff_mean


def is_fence_area(box: list[int], frame_shape: tuple[int, int]) -> bool:
    """Verifica si la detección está en el área cercana a la reja o acceso interior."""
    h_f, w_f = frame_shape[:2]
    x, y, w, h = box
    foot_x = (x + w / 2.0) / max(1, w_f)
    foot_y = (y + h) / max(1, h_f)
    return (foot_y >= 0.50) or (foot_x >= 0.45 and foot_y >= 0.38)


def detect_objects(session, input_name: str, frame: np.ndarray, cfg: dict[str, Any]):
    """Detección exclusiva de personas con YOLO11n y umbrales adaptativos para reja."""
    size = int(cfg["input_size"])
    blob, scale, pad_x, pad_y = letterbox(frame, size)
    outputs = session.run(None, {input_name: blob})
    pred = np.asarray(outputs[0])[0]
    if pred.ndim != 2:
        raise RuntimeError(f"Salida ONNX inesperada: {pred.shape}")
    if pred.shape[0] < pred.shape[1]:
        pred = pred.T
    class_scores = pred[:, 4:]
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(len(class_scores)), class_ids]

    # Exclusivamente personas (clase 0). Todo lo demás se ignora.
    valid_mask = (class_ids == 0)
    selected = pred[valid_mask]
    selected_conf = confidences[valid_mask]

    if len(selected) == 0:
        return []

    h, w = frame.shape[:2]
    boxes, scores, labels = [], [], []
    default_person_thresh = float(cfg.get("confidence_threshold", 0.62))
    fence_person_thresh = float(cfg.get("fence_confidence_threshold", 0.45))

    for row, score in zip(selected, selected_conf):
        cx, cy, bw, bh = [float(x) for x in row[:4]]
        x1 = (cx - bw / 2 - pad_x) / scale
        y1 = (cy - bh / 2 - pad_y) / scale
        x2 = (cx + bw / 2 - pad_x) / scale
        y2 = (cy + bh / 2 - pad_y) / scale
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(w - 1, int(x2)), min(h - 1, int(y2))
        bw_box, bh_box = x2 - x1, y2 - y1
        if bw_box <= 0 or bh_box <= 0:
            continue

        box = [x1, y1, bw_box, bh_box]
        label = "persona"

        # Umbral adaptativo: persona cerca de la reja usa umbral más sensible contra barrotes
        req_thresh = fence_person_thresh if is_fence_area(box, (h, w)) else default_person_thresh

        if float(score) >= req_thresh:
            boxes.append(box)
            scores.append(float(score))
            labels.append(label)

    if not boxes:
        return []
    indices = cv2.dnn.NMSBoxes(boxes, scores, fence_person_thresh, float(cfg["nms_threshold"]))
    if len(indices) == 0:
        return []
    return [{"box": boxes[int(i)], "confidence": scores[int(i)], "label": labels[int(i)]} for i in np.array(indices).reshape(-1)]


def detect_persons(session, input_name: str, frame: np.ndarray, cfg: dict[str, Any]):
    """Alias compatible hacia detect_objects."""
    return detect_objects(session, input_name, frame, cfg)


def detection_frame(frame: np.ndarray, cfg: dict[str, Any]):
    """Mejora contraste local CLAHE en visión infrarroja/nocturna."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness >= float(cfg.get("night_brightness_threshold", 85.0)):
        return frame, cfg, False, brightness
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    light, channel_a, channel_b = cv2.split(lab)
    light = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(light)
    enhanced = cv2.cvtColor(cv2.merge((light, channel_a, channel_b)), cv2.COLOR_LAB2BGR)
    night_cfg = dict(cfg)
    night_cfg["confidence_threshold"] = float(cfg.get("night_confidence_threshold", 0.45))
    night_cfg["fence_confidence_threshold"] = min(0.40, float(cfg.get("fence_confidence_threshold", 0.45)))
    night_cfg["motion_mean_threshold"] = float(cfg.get("night_motion_mean_threshold", 2.5))
    night_cfg["motion_fraction_threshold"] = float(cfg.get("night_motion_fraction_threshold", 0.008))
    return enhanced, night_cfg, True, brightness


def detection_in_interest_zone(box: list[int], cfg: dict[str, Any], frame_shape=None) -> bool:
    """Usa el punto de apoyo de la caja (pies) para priorizar cerco y accesos."""
    zones = cfg.get("interest_zones", [])
    if not zones:
        return True
    x, y, w, h = box
    foot = (float(x + w / 2), float(y + h))
    if frame_shape is not None:
        frame_h, frame_w = frame_shape[:2]
        reference_w = float(cfg.get("zone_reference_width", 1280))
        reference_h = float(cfg.get("zone_reference_height", 720))
        foot = (foot[0] * reference_w / frame_w, foot[1] * reference_h / frame_h)
    for zone in zones:
        polygon = np.asarray(zone.get("polygon", []), dtype=np.int32)
        if len(polygon) >= 3 and cv2.pointPolygonTest(polygon, foot, False) >= 0:
            return True
    return False


def motion_metrics(current_gray: np.ndarray, previous_gray: np.ndarray | None, box: list[int]) -> tuple[float, float]:
    """Mide cambios dentro de una detección entre dos frames consecutivos."""
    if previous_gray is None:
        return 0.0, 0.0
    x, y, w, h = box
    pad = max(4, int(min(w, h) * 0.08))
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2 = min(current_gray.shape[1], x + w + pad)
    y2 = min(current_gray.shape[0], y + h + pad)
    current = current_gray[y1:y2, x1:x2]
    previous = previous_gray[y1:y2, x1:x2]
    if current.size == 0 or previous.size == 0:
        return 0.0, 0.0
    diff = cv2.GaussianBlur(cv2.absdiff(current, previous), (3, 3), 0)
    return float(np.mean(diff)), float(np.mean(diff >= 12))


class TrackedObject:
    """Rastrea la trayectoria de un objeto a través del tiempo para clasificar su comportamiento."""

    def __init__(self, track_id: int, label: str, box: list[int], confidence: float, timestamp: float, reference_shape: tuple[int, int]):
        self.track_id = track_id
        self.label = label
        self.box = box
        self.confidence = confidence
        self.created_at = timestamp
        self.last_seen = timestamp
        self.hits = 1
        self.reference_shape = reference_shape
        foot = self._norm_foot(box, reference_shape)
        self.positions: list[tuple[float, tuple[float, float]]] = [(timestamp, foot)]
        self.alert_dispatched = False
        self.alert_type: str | None = None

    @staticmethod
    def _norm_foot(box: list[int], shape: tuple[int, int]) -> tuple[float, float]:
        x, y, w, h = box
        h_f, w_f = shape[:2]
        return ((x + w / 2.0) / max(1, w_f), (y + h) / max(1, h_f))

    def update(self, box: list[int], confidence: float, timestamp: float):
        self.box = box
        self.confidence = max(self.confidence, confidence)
        self.last_seen = timestamp
        self.hits += 1
        foot = self._norm_foot(box, self.reference_shape)
        self.positions.append((timestamp, foot))
        if len(self.positions) > 50:
            self.positions.pop(0)

    @property
    def duration(self) -> float:
        return self.last_seen - self.created_at

    def motion_vector(self) -> tuple[float, float]:
        if len(self.positions) < 2:
            return 0.0, 0.0
        start = self.positions[0][1]
        end = self.positions[-1][1]
        return (end[0] - start[0], end[1] - start[1])

    def total_displacement(self) -> float:
        dx, dy = self.motion_vector()
        return float(np.hypot(dx, dy))

    def classify_behavior(self, is_night: bool) -> str:
        dur = self.duration
        disp = self.total_displacement()
        start_y = self.positions[0][1][1]
        current_y = self.positions[-1][1][1]

        # Persona:
        # En la perspectiva de la cámara:
        # start_y >= 0.52 indica inicio en patio o pasillo interior
        if start_y >= 0.52 and (current_y <= start_y - 0.06 or current_y < 0.48):
            return "salida_propiedad"

        # Si partió en la calle y camina hacia la reja o ingresa al pasillo:
        if start_y < 0.58 and (current_y >= start_y + 0.06 or current_y >= 0.54):
            return "ingreso_propiedad"

        # Si permanece detenida frente a la reja o vereda por más de 12 segundos:
        if dur >= 12.0 and disp < 0.12 and current_y >= 0.40:
            return "merodeo_reja"

        # De noche cualquier presencia cerca de la reja es prioritaria:
        if is_night and current_y >= 0.45:
            return "presencia_nocturna"

        # Peatón desplazándose por la calle sin interactuar con la reja:
        if disp > 0.18:
            return "transito_calle"

        return "presencia"


class SimpleTracker:
    """Asocia detecciones cuadro a cuadro para mantener persistencia e historial."""

    def __init__(self, max_missing_seconds: float = 3.5):
        self.next_id = 1
        self.tracks: dict[int, TrackedObject] = {}
        self.max_missing = max_missing_seconds

    def has_active_tracks(self) -> bool:
        return len(self.tracks) > 0

    def update(self, detections: list[dict[str, Any]], timestamp: float, frame_shape: tuple[int, int]) -> list[TrackedObject]:
        unmatched_dets = list(range(len(detections)))
        matched_tracks = set()

        for track_id, track in list(self.tracks.items()):
            tx, ty, tw, th = track.box
            tcx, tcy = tx + tw / 2.0, ty + th / 2.0

            best_det_idx = -1
            best_dist = float("inf")

            for det_idx in unmatched_dets:
                det = detections[det_idx]
                if det.get("label") != track.label:
                    continue
                dx, dy, dw, dh = det["box"]
                dcx, dcy = dx + dw / 2.0, dy + dh / 2.0
                dist = np.hypot(dcx - tcx, dcy - tcy)

                if dist < 220 and dist < best_dist:
                    best_dist = dist
                    best_det_idx = det_idx

            if best_det_idx >= 0:
                det = detections[best_det_idx]
                track.update(det["box"], det["confidence"], timestamp)
                matched_tracks.add(track_id)
                unmatched_dets.remove(best_det_idx)
            else:
                if timestamp - track.last_seen > self.max_missing:
                    del self.tracks[track_id]

        for det_idx in unmatched_dets:
            det = detections[det_idx]
            new_track = TrackedObject(self.next_id, det["label"], det["box"], det["confidence"], timestamp, frame_shape)
            self.tracks[self.next_id] = new_track
            self.next_id += 1

        return list(self.tracks.values())


def filter_detections(frame: np.ndarray, previous_gray: np.ndarray | None,
                      detections: list[dict[str, Any]], cfg: dict[str, Any],
                      tracker: SimpleTracker,
                      logger: logging.Logger) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Descarta detecciones fuera de zonas prioritarias, estructuras fijas y borde superior."""
    current_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    accepted: list[dict[str, Any]] = []
    reference_h = float(cfg.get("zone_reference_height", 720))
    top_limit = int(round(float(cfg.get("top_exclusion_pixels", 24)) * frame.shape[0] / reference_h))
    mean_limit = float(cfg.get("motion_mean_threshold", 5.0))
    fraction_limit = float(cfg.get("motion_fraction_threshold", 0.015))

    for item in detections:
        x, y, w, h = item["box"]
        label = item.get("label", "persona")
        if not detection_in_interest_zone(item["box"], cfg, frame.shape):
            logger.info("Detección descartada fuera de zonas prioritarias: %s x=%d y=%d w=%d h=%d conf=%.0f%%", label, x, y, w, h, item["confidence"] * 100)
            continue
        if y <= top_limit:
            logger.info("Detección descartada por tocar el borde superior: %s x=%d y=%d w=%d h=%d conf=%.0f%%", label, x, y, w, h, item["confidence"] * 100)
            continue

        # Si el objeto ya está en seguimiento (el tracker ya lo reconoció en frames previos),
        # no lo descartamos aunque se quede quieto un momento.
        is_already_tracked = False
        tx_c, ty_c = x + w / 2.0, y + h / 2.0
        for tr in tracker.tracks.values():
            if tr.label == label and tr.hits >= 2:
                otx, oty, otw, oth = tr.box
                if np.hypot(tx_c - (otx + otw / 2.0), ty_c - (oty + oth / 2.0)) < 150:
                    is_already_tracked = True
                    break

        mean_diff, changed_fraction = motion_metrics(current_gray, previous_gray, item["box"])
        item["motion_mean"] = mean_diff
        item["motion_fraction"] = changed_fraction

        if not is_already_tracked and (mean_diff < mean_limit or changed_fraction < fraction_limit):
            logger.info("Detección descartada por objeto estático: %s x=%d y=%d w=%d h=%d conf=%.0f%% mov=%.2f/%.2f", label, x, y, w, h, item["confidence"] * 100, mean_diff, changed_fraction)
            continue

        accepted.append(item)
    return accepted, current_gray


def annotate(frame: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
    out = frame.copy()
    for item in detections:
        x, y, w, h = item["box"]
        conf = item["confidence"]
        label = item.get("label", "persona")
        color = (0, 220, 0) if label == "persona" else (0, 180, 240)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        cv2.putText(out, f"{label} {conf:.0%}", (x, max(25, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)
    return out


def frame_quality(frame: np.ndarray, detections: list[dict[str, Any]]) -> tuple[float, ...]:
    """Prioriza rostro visible, tamaño de la persona/vehículo y nitidez."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = []
    cascade_factory = getattr(cv2, "CascadeClassifier", None)
    haar_data = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if cascade_factory is not None and haar_data:
        try:
            cascade = cascade_factory(haar_data + "haarcascade_frontalface_default.xml")
            if not cascade.empty():
                faces = cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
        except Exception:
            faces = []
    face_area = max((int(w) * int(h) for _, _, w, h in faces), default=0)
    frame_area = max(1, frame.shape[0] * frame.shape[1])
    person_score = max((d["confidence"] * d["box"][2] * d["box"][3] / frame_area
                        for d in detections), default=0.0)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return (1.0 if face_area else 0.0, face_area / frame_area, person_score, sharpness)


def compile_mp4_clip(frames: list[np.ndarray], output_mp4: Path, fps: float = 3.0) -> bool:
    """Compila una ráfaga de frames en un clip H.264 ligero (reproducible como GIF en bucle en WhatsApp)."""
    if not frames:
        return False
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    h, w = frames[0].shape[:2]
    out_w, out_h = w, h
    if out_w > 960:
        scale = 960 / out_w
        out_w = int(round(out_w * scale)) // 2 * 2
        out_h = int(round(out_h * scale)) // 2 * 2
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{w}x{h}", "-pix_fmt", "bgr24", "-r", f"{fps:g}",
        "-i", "-",
        "-vf", f"scale={out_w}:{out_h}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-crf", "28", "-movflags", "+faststart", "-an",
        str(output_mp4)
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for f in frames:
            proc.stdin.write(f.tobytes())
        proc.stdin.close()
        proc.wait(timeout=10)
        return output_mp4.exists() and output_mp4.stat().st_size > 0
    except Exception:
        return False


def save_person_sequence(cap, first_frame: np.ndarray, first_detections: list[dict[str, Any]],
                         cfg: dict[str, Any], session, input_name: str,
                         logger: logging.Logger,
                         recent_buffer: list[np.ndarray] | None = None) -> tuple[list[Path], Path, Path | None, np.ndarray]:
    """Guarda imágenes cronológicas WebP, compila el clip MP4 en bucle y retorna la mejor imagen."""
    wanted = max(5, int(cfg.get("event_images", 5)))
    clip_frames: list[np.ndarray] = []
    if recent_buffer:
        clip_frames.extend(list(recent_buffer))
    clip_frames.append(first_frame.copy())

    captures: list[tuple[datetime, np.ndarray, list[dict[str, Any]]]] = [
        (datetime.now(CHILE), first_frame.copy(), list(first_detections))]
    attempts = 0
    while len(captures) < wanted and attempts < wanted * 3:
        attempts += 1
        ok, frame = cap.read()
        if not ok or frame is None:
            logger.warning("Flujo RTSP interrumpido durante secuencia; imágenes=%d", len(captures))
            break
        clip_frames.append(frame.copy())
        inference, active_cfg, _, _ = detection_frame(frame, cfg)
        raw = detect_objects(session, input_name, inference, active_cfg)
        reference_h = float(active_cfg.get("zone_reference_height", 720))
        top_limit = int(round(float(active_cfg.get("top_exclusion_pixels", 24)) * frame.shape[0] / reference_h))
        relevant = [d for d in raw if detection_in_interest_zone(d["box"], active_cfg, frame.shape)
                    and d["box"][1] > top_limit]
        captures.append((datetime.now(CHILE), frame.copy(), relevant))

    started = captures[0][0]
    event_dir = EVENT_DIR / started.strftime("%Y-%m-%d")
    event_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, (stamp, frame, _) in enumerate(captures, 1):
        name = "event_" + stamp.strftime("%Y%m%d_%H%M%S_%f")[:-3] + f"_{index:02d}.webp"
        path = event_dir / name
        if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_WEBP_QUALITY, int(cfg.get("webp_quality", 82))]):
            raise RuntimeError(f"No se pudo guardar {path}")
        paths.append(path)

    try:
        best_index = max(range(len(captures)), key=lambda i: frame_quality(captures[i][1], captures[i][2]))
    except Exception:
        logger.exception("Falló la selección de la mejor imagen; se usará la primera")
        best_index = 0
    best = paths[best_index]

    # Generar clip MP4 (animación en bucle / modo GIF)
    clip_path = None
    if cfg.get("send_clip", True) and len(clip_frames) >= 4:
        mp4_name = best.stem + "_clip.mp4"
        candidate_clip = event_dir / mp4_name
        if compile_mp4_clip(clip_frames, candidate_clip, fps=float(cfg.get("stream_fps", 2.0)) * 1.5):
            clip_path = candidate_clip
            logger.info("Clip de movimiento compilado: %s (%d KB)", clip_path.name, round(clip_path.stat().st_size / 1024))

    try:
        atomic_json(best.with_suffix(".event.json"), {
            "created": captures[0][0].timestamp(),
            "alert_image": best.name,
            "alert_clip": clip_path.name if clip_path else None,
            "images": [p.name for p in paths],
            "detection_count": len(first_detections),
            "alert_enqueued": False,
        })
    except OSError:
        logger.exception("Las imágenes se guardaron, pero no se pudo escribir su manifiesto")
    return paths, best, clip_path, captures[-1][1]


def mark_event_enqueued(path: Path, event_id: str) -> None:
    manifest = path.with_suffix(".event.json")
    if not manifest.exists():
        return
    data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    data.update({"alert_enqueued": True, "event_id": event_id, "enqueued_at": time.time()})
    atomic_json(manifest, data)


def recover_unqueued_events(logger: logging.Logger) -> int:
    """Retry local queueing for evidence saved before a crash or database fault."""
    recovered = 0
    from alertas_whatsapp import enqueue
    for manifest in EVENT_DIR.rglob("*.event.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if data.get("alert_enqueued"):
                continue
            image = manifest.parent / str(data["alert_image"])
            if not image.is_file():
                continue
            event_id = enqueue(image)
            mark_event_enqueued(image, event_id)
            recovered += 1
            logger.info("Alerta recuperada desde evidencia local: event_id=%s", event_id)
        except Exception:
            logger.exception("No se pudo recuperar la alerta descrita por %s", manifest.name)
    return recovered


def send_alert(path: Path, clip_path: Path | None, custom_text: str, logger: logging.Logger) -> bool:
    """Encola la alerta (foto fija y clip) para que salga por WhatsApp."""
    from alertas_whatsapp import enqueue
    try:
        event_id = enqueue(path, custom_text=custom_text)
        mark_event_enqueued(path, event_id)
        logger.info("Alerta de foto en cola de WhatsApp: event_id=%s (%s)", event_id, custom_text)

        if clip_path and clip_path.exists():
            clip_text = f"{custom_text} (Clip en movimiento)"
            clip_event_id = enqueue(clip_path, custom_text=clip_text)
            logger.info("Alerta de clip en cola de WhatsApp: event_id=%s", clip_event_id)

        return True
    except Exception:
        logger.exception("No se pudo guardar la alerta de WhatsApp")
        return False


class FFmpegFrameReader:
    """Decodifica RTSP sin permitir que una lectura congele el detector."""

    def __init__(self, url: str, fps: float = 2.0, width: int = 1920,
                 read_timeout: float = 20.0):
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        self.read_timeout = max(5.0, read_timeout)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.eof = False
        self.proc = subprocess.Popen(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", url,
                "-vf", f"fps={fps:g},scale={width}:-2", "-an",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.buffer = bytearray()
        self.reader = threading.Thread(target=self._pump, name="aura-rtsp-reader", daemon=True)
        self.reader.start()

    def is_opened(self) -> bool:
        return self.proc.poll() is None and self.proc.stdout is not None

    def _publish(self, frame: np.ndarray) -> None:
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(frame)
            except queue.Full:
                pass

    def _pump(self) -> None:
        try:
            while not self.stop_event.is_set() and self.proc.stdout is not None:
                chunk = self.proc.stdout.read(4096)
                if not chunk:
                    break
                self.buffer.extend(chunk)
                while True:
                    start = self.buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(self.buffer) > 1:
                            del self.buffer[:-1]
                        break
                    end = self.buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start:
                            del self.buffer[:start]
                        break
                    jpeg = bytes(self.buffer[start:end + 2])
                    del self.buffer[:end + 2]
                    frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        self._publish(frame)
        finally:
            self.eof = True

    def read(self):
        if not self.is_opened() and self.frames.empty():
            return False, None
        try:
            return True, self.frames.get(timeout=self.read_timeout)
        except queue.Empty:
            return False, None

    def release(self):
        self.stop_event.set()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try: self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
        self.reader.join(timeout=2)
        if self.proc.stdout and not self.reader.is_alive():
            self.proc.stdout.close()


def capture_stream(url: str, cfg: dict[str, Any]):
    return FFmpegFrameReader(url, float(cfg.get("stream_fps", 2.0)),
                             int(cfg.get("stream_width", 1920)),
                             float(cfg.get("stream_read_timeout_seconds", 20.0)))


def cleanup_events(days: int, logger: logging.Logger):
    cutoff = time.time() - days * 86400
    for path in EVENT_DIR.rglob("*"):
        is_evidence = path.suffix.lower() in (".jpg", ".jpeg", ".webp")
        is_manifest = path.name.endswith(".event.json")
        if not path.is_file() or not (is_evidence or is_manifest):
            continue
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
        except OSError as exc:
            logger.warning("No pude borrar %s: %s", path, exc)
    for directory in sorted((p for p in EVENT_DIR.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def run(once: bool, no_notify: bool):
    cfg = ensure_config()
    logger = setup_logging()
    write_state("starting")
    logger.info("Alertas configuradas: cola de WhatsApp")
    # La cola de envio corre en su propio hilo dentro de este proceso.
    if not once:
        import alertas_whatsapp
        alertas_whatsapp.iniciar()
    session, input_name, _ = load_model(logger, int(cfg["input_size"]))
    cap = capture_stream(str(cfg["rtsp_url"]), cfg)
    if not cap.is_opened():
        raise RuntimeError(f"No pude abrir RTSP: {cfg['rtsp_url']}")
    logger.info("RTSP abierto: %s", cfg["rtsp_url"])
    recovered = 0 if no_notify else recover_unqueued_events(logger)
    write_state("running", recovered_alerts=recovered)

    tracker = SimpleTracker(max_missing_seconds=3.5)
    recent_buffer: deque[np.ndarray] = deque(maxlen=6)
    bg_motion_gray: np.ndarray | None = None

    last_gate_alert_time = 0.0
    last_street_alert_time = 0.0
    last_cleanup = 0.0
    previous_gray: np.ndarray | None = None
    low_light_state: bool | None = None
    last_heartbeat = 0.0
    last_recovery = time.time()
    frames_processed = 0
    last_frame_at = 0.0
    outage_started = 0.0

    gate_cooldown = float(cfg.get("gate_cooldown_seconds", 60))
    street_cooldown = float(cfg.get("street_cooldown_seconds", 180))

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                logger.warning("Se perdió un frame RTSP; reconectando")
                now = time.time()
                if not outage_started:
                    outage_started = now
                write_state("reconnecting", frames_processed=frames_processed,
                            last_frame_at=last_frame_at,
                            stream_outage_seconds=int(now - outage_started),
                            whatsapp_queue_independent=True)
                cap.release()
                previous_gray = None
                bg_motion_gray = None
                time.sleep(float(cfg.get("reconnect_delay_seconds", 1.0)))
                cap = capture_stream(str(cfg["rtsp_url"]), cfg)
                continue

            now = time.time()
            last_frame_at = now
            outage_started = 0.0
            frames_processed += 1
            recent_buffer.append(frame.copy())

            # Pre-filtro de movimiento liviano: ahorra CPU si el patio está desierto
            if not once and cfg.get("enable_motion_prefilter", True):
                has_motion, bg_motion_gray, _ = check_scene_motion(frame, bg_motion_gray)
                if not tracker.has_active_tracks() and not has_motion:
                    if now - last_heartbeat >= 10:
                        write_state("running", frames_processed=frames_processed,
                                    last_frame_at=now, whatsapp_queue_independent=True)
                        last_heartbeat = now
                    time.sleep(float(cfg.get("sample_interval_seconds", 0.2)))
                    continue

            inference_frame, active_cfg, low_light, brightness = detection_frame(frame, cfg)
            if low_light_state is None or low_light_state != low_light:
                logger.info("Modo de detección: %s; brillo medio=%.1f",
                            "nocturno" if low_light else "diurno", brightness)
                low_light_state = low_light

            raw_detections = detect_objects(session, input_name, inference_frame, active_cfg)
            detections, previous_gray = filter_detections(frame, previous_gray, raw_detections, active_cfg, tracker, logger)
            active_tracks = tracker.update(detections, now, frame.shape)

            if now - last_heartbeat >= 10:
                write_state("running", frames_processed=frames_processed,
                            last_frame_at=now, whatsapp_queue_independent=True)
                last_heartbeat = now

            if not no_notify and now - last_recovery >= 60:
                recover_unqueued_events(logger)
                last_recovery = now

            # Evaluar comportamiento de cada objeto rastreado
            for track in active_tracks:
                if track.alert_dispatched and not once:
                    continue

                behavior = track.classify_behavior(low_light)

                # Descartar peatones normales caminando por la calle de día
                if behavior == "transito_calle" and not low_light:
                    continue

                # Separar cooldown de la reja respecto a la calle
                is_gate_event = behavior in ("ingreso_propiedad", "salida_propiedad", "merodeo_reja", "presencia_nocturna")
                if is_gate_event:
                    cooldown_ok = once or (now - last_gate_alert_time >= gate_cooldown)
                    req_hits = int(cfg.get("fence_confirm_hits", 2))
                else:
                    cooldown_ok = once or (now - last_street_alert_time >= street_cooldown)
                    req_hits = int(cfg.get("confirm_hits", 3))

                if (track.hits >= req_hits and cooldown_ok) or once:
                    if behavior == "ingreso_propiedad":
                        alert_title = "🔴 Ingreso a la propiedad detectado"
                    elif behavior == "salida_propiedad":
                        alert_title = "🟢 Salida de la casa detectada"
                    elif behavior == "merodeo_reja":
                        alert_title = "🟠 Persona merodeando frente a la reja"
                    elif behavior == "presencia_nocturna":
                        alert_title = "🚨 Presencia nocturna en perímetro"
                    else:
                        alert_title = "Persona detectada"

                    logger.info("Activando alerta: %s (track_id=%d, hits=%d, dur=%.1fs)",
                                alert_title, track.track_id, track.hits, track.duration)

                    paths, best_image, clip_path, last_frame = save_person_sequence(
                        cap, frame, detections, cfg, session, input_name, logger, recent_buffer=list(recent_buffer))
                    previous_gray = cv2.cvtColor(last_frame, cv2.COLOR_BGR2GRAY)

                    queued = False
                    if not no_notify:
                        queued = send_alert(best_image, clip_path, alert_title, logger)

                    track.alert_dispatched = True
                    track.alert_type = behavior

                    if is_gate_event:
                        last_gate_alert_time = now
                    else:
                        last_street_alert_time = now

                    if once:
                        return {"detections": detections, "snapshot": str(best_image),
                                "clip": str(clip_path) if clip_path else None,
                                "images": [str(p) for p in paths], "notified": False, "queued": queued}
                    break

            if now - last_cleanup > 3600:
                cleanup_events(int(cfg["keep_event_days"]), logger)
                last_cleanup = now

            if once:
                return {"detections": detections, "snapshot": None, "notified": False}

            time.sleep(float(cfg["sample_interval_seconds"]))
    finally:
        cap.release()
        write_state("stopped")


INSTANCE_LOCK_PORT = 38742


def acquire_instance_lock():
    """Un solo detector a la vez.

    Los huérfanos de supervisiones anteriores siguen escribiendo
    camera_ai_state.json y le hacen creer al supervisor que su worker murió:
    de ahí salen los reinicios cada dos minutos y los avisos de caída/recuperación
    que llegaron el 13-09-2026. Un duplicado se retira aquí mismo.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
        sock.listen(1)
    except OSError:
        return None
    return sock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="captura y analiza un frame; termina")
    parser.add_argument("--no-notify", action="store_true", help="no encola alertas")
    args = parser.parse_args()
    lock = None
    if not args.once:
        lock = acquire_instance_lock()
        if lock is None:
            print("detector duplicado descartado")
            return
    try:
        result = run(args.once, args.no_notify)
    finally:
        if lock is not None:
            lock.close()
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

