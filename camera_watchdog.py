#!/usr/bin/env python3
"""Supervise the camera worker and recover it from exits or stale heartbeats."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
WORKER = BASE / "camera_watch.py"
STATE = BASE / "camera_ai_state.json"
WATCHDOG_STATE = BASE / "camera_watchdog_state.json"
LOG = BASE / "camera_watchdog.log"
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HEARTBEAT_TIMEOUT = 90
STARTUP_GRACE = 120
OUTAGE_NOTICE_AFTER = 180
# Python propio opcional: si existe una carpeta "python" junto a este archivo
# (con interpretes y dependencias incluidas) se usa ese; si no, se usa el
# Python en ejecucion. Asi el proyecto puede ser autonomo.
PYTHON_PROPIO = BASE / "python" / ("python.exe" if os.name == "nt" else "bin/python")


def worker_environment() -> dict[str, str]:
    """Entorno del detector. El Python del proyecto ya encuentra sus librerias."""
    return os.environ.copy()


def atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def terminate_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=15, check=False, creationflags=FLAGS)
    else:
        proc.terminate()


def kill_tree(pid: int) -> None:
    """Elimina un árbol de procesos ajeno a esta supervisión."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return
    if pid <= 0 or pid == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15, check=False, creationflags=FLAGS)
        else:
            os.kill(pid, signal.SIGTERM)
        logging.error("Huérfano del detector pid=%s eliminado", pid)
    except Exception as exc:
        logging.warning("No se pudo eliminar pid=%s (%s)", pid, type(exc).__name__)


def pid_alive(pid: int) -> bool:
    """Existencia de un PID. Nunca os.kill(pid, 0): en Windows mata el proceso."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def stragglers(keep: int | None = None) -> list[int]:
    """PIDs de otros camera_watch.py (huérfanos de supervisiones anteriores).

    Cuestan caro: escriben el mismo camera_ai_state.json y el supervisor, que
    exige que el pid del estado sea el de su worker, entra en bucle de reinicios.
    Devuelve [] si el escaneo no concluye: no poder mirar no autoriza a matar.
    """
    if os.name != "nt":
        return []
    query = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
             "-and $_.CommandLine -like '*camera_watch.py*' } "
             "| Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, encoding="cp850", errors="replace",
                             timeout=45, creationflags=FLAGS)
    except Exception as exc:
        logging.warning("Escaneo de huérfanos falló (%s)", type(exc).__name__)
        return []
    found = [int(tok) for tok in (out.stdout or "").split() if tok.isdigit()]
    return [p for p in found if p != keep and p != os.getpid()]


def notify_recovery(incident: int, downtime: int) -> None:
    """Aviso de recuperacion por WhatsApp; la supervision no depende de que se envie."""
    try:
        from alertas_whatsapp import enviar_texto
        minutos = max(1, round(downtime / 60))
        text = ("Camara: el detector se reinicio solo tras una falla de "
                f"{minutos} min. La vigilancia y el guardado local ya estan activos.")
        enviar_texto(text)
    except Exception as exc:
        logging.warning("No se pudo enviar aviso de recuperación (%s)", type(exc).__name__)


def notify_outage(incident: int) -> bool:
    """Aviso de caida prolongada por WhatsApp."""
    try:
        from alertas_whatsapp import enviar_texto
        return enviar_texto(
            "Camara: el detector lleva mas de 3 minutos sin recuperarse. "
            "El supervisor seguira intentando reiniciarlo.")
    except Exception as exc:
        logging.warning("No se pudo enviar aviso de caída (%s)", type(exc).__name__)
        return False


def main() -> None:
    logging.basicConfig(filename=LOG, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    instance_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        instance_lock.bind(("127.0.0.1", 38741))
        instance_lock.listen(1)
    except OSError:
        logging.info("Supervisor duplicado descartado")
        return
    failures = 0
    incident_started = 0
    incident_notified = False
    while True:
        started = time.time()
        for orphan in stragglers():
            kill_tree(orphan)
        # Se prefiere el Python del proyecto (autosuficiente); si faltara, el actual.
        python = PYTHON_PROPIO if PYTHON_PROPIO.exists() else Path(sys.executable)
        proc = subprocess.Popen([str(python), str(WORKER)], cwd=BASE,
                                env=worker_environment(), creationflags=FLAGS)
        logging.info("Detector iniciado pid=%s", proc.pid)
        healthy = False
        reason = "process_exit"
        while proc.poll() is None:
            now = time.time()
            state = read_state(STATE)
            heartbeat_updated = float(state.get("updated", 0))
            heartbeat_ok = (state.get("pid") == proc.pid and heartbeat_updated >= started and
                            now - heartbeat_updated <= HEARTBEAT_TIMEOUT and
                            state.get("status") in ("starting", "running", "reconnecting"))
            frame_at = float(state.get("last_frame_at") or 0)
            frames_ok = (heartbeat_ok and state.get("status") == "running" and
                         frame_at >= started and now - frame_at <= 30)
            if frames_ok:
                if not healthy:
                    logging.info("Detector saludable pid=%s", proc.pid)
                    healthy = True
                    if incident_started:
                        downtime = int(now - incident_started)
                        if incident_notified or downtime >= OUTAGE_NOTICE_AFTER:
                            notify_recovery(incident_started, downtime)
                        incident_started = 0
                        incident_notified = False
                failures = 0
            else:
                healthy = False
                if not incident_started:
                    incident_started = int(now)
                if now - incident_started >= OUTAGE_NOTICE_AFTER and not incident_notified:
                    incident_notified = notify_outage(incident_started)
            if not heartbeat_ok and now - started > STARTUP_GRACE:
                foreign = int(state.get("pid") or 0)
                if foreign and foreign != proc.pid and pid_alive(foreign):
                    # Un huérfano escribe el estado y tapa el latido del worker
                    # real. Se limpia el huérfano; reiniciar acá no arregla nada.
                    logging.error("Huérfano pid=%s escribiendo el estado; se elimina sin reiniciar",
                                  foreign)
                    kill_tree(foreign)
                    started = time.time()
                    time.sleep(2)
                    continue
                reason = "stale_heartbeat"
                logging.error("Heartbeat vencido; reiniciando detector pid=%s", proc.pid)
                terminate_tree(proc)
                break
            atomic_json(WATCHDOG_STATE, {
                "updated": now,
                "pid": os.getpid(),
                "worker_pid": proc.pid,
                "worker_healthy": frames_ok,
                "worker_responsive": heartbeat_ok,
                "failures": failures,
            })
            time.sleep(5)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            terminate_tree(proc)
        failures += 1
        if not incident_started:
            incident_started = int(time.time())
        if time.time() - incident_started >= OUTAGE_NOTICE_AFTER and not incident_notified:
            incident_notified = notify_outage(incident_started)
        logging.error("Detector detenido reason=%s exit=%s; fallo=%s",
                      reason, proc.returncode, failures)
        atomic_json(WATCHDOG_STATE, {
            "updated": time.time(), "pid": os.getpid(), "worker_pid": None,
            "worker_healthy": False, "failures": failures, "reason": reason,
        })
        time.sleep(min(30, 3 * failures))


if __name__ == "__main__":
    main()
