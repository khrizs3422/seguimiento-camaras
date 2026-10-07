"""Monitor de salud del sistema de camaras.

Revisa que el sistema completo siga vivo y avisa por WhatsApp SOLO cuando hay
un problema o cuando se recupera. Es silencioso mientras todo esta bien.

Capas que revisa:
  1. Detector (camera_watch.py): proceso vivo y latido reciente
  2. Supervisor (camera_watchdog.py): proceso vivo y sin fallos repetidos
  3. Cola de alertas (alertas_whatsapp.py): avisos sin entregar
  4. Servicio de WhatsApp (puerto 8780): conectado y listo

Reparaciones que intenta por su cuenta:
  - Relanzar las tareas programadas del detector y del servicio de fotos

Se penso para ejecutarse desde el Programador de tareas de Windows cada
10 minutos. No usa SMS ni Telegram.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

REGISTRO = BASE / 'camera_health.log'
ESTADO_DETECTOR = BASE / 'camera_ai_state.json'
ESTADO_SUPERVISOR = BASE / 'camera_watchdog_state.json'

LATIDO_MAXIMO = 90          # segundos sin latido antes de considerarlo caido
PAUSA_ENTRE_AVISOS = 3600   # no repetir el mismo aviso antes de una hora
ARCHIVO_ULTIMO_AVISO = BASE / 'camera_health_ultimo_aviso.json'

# Nombres de las tareas programadas que el monitor intenta relanzar al reparar.
TAREA_DETECTOR = os.environ.get('CAMARA_TAREA_DETECTOR', 'SeguimientoCamaras_Detector')
TAREA_FOTOS = os.environ.get('CAMARA_TAREA_FOTOS', 'SeguimientoCamaras_Fotos')

logging.basicConfig(filename=REGISTRO, level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

BANDERA_SIN_VENTANA = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def leer_json(ruta: Path) -> dict:
    try:
        return json.loads(ruta.read_text(encoding='utf-8-sig'))
    except Exception:
        return {}


def proceso_vivo(nombre_script: str) -> bool:
    """True si hay un proceso de python ejecutando ese script."""
    try:
        consulta = ("@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' "
                    f"-and $_.CommandLine -like '*{nombre_script}*' }}).Count")
        salida = subprocess.run(['powershell', '-NoProfile', '-Command', consulta],
                                capture_output=True, text=True, timeout=25,
                                creationflags=BANDERA_SIN_VENTANA)
        return int((salida.stdout or '0').strip() or 0) > 0
    except Exception:
        return False


def puerto_escucha(puerto: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        return s.connect_ex(('127.0.0.1', puerto)) == 0


def servicio_whatsapp_conectado() -> bool:
    """Pregunta al servicio local si esta enlazado con WhatsApp."""
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:8780/estado', timeout=8) as r:
            return bool(json.loads(r.read().decode('utf-8')).get('conectado'))
    except Exception:
        return False


def avisos_pendientes() -> int:
    try:
        import alertas_whatsapp
        return alertas_whatsapp.pendientes()
    except Exception:
        return -1


def avisar(texto: str, clave: str) -> None:
    """Envia un aviso por WhatsApp, sin repetirlo mas de una vez por hora."""
    ultimos = leer_json(ARCHIVO_ULTIMO_AVISO)
    ahora = time.time()
    if ahora - float(ultimos.get(clave) or 0) < PAUSA_ENTRE_AVISOS:
        logging.info('Aviso "%s" omitido: ya se envio hace poco', clave)
        return
    try:
        import alertas_whatsapp
        if alertas_whatsapp.enviar_texto(texto):
            ultimos[clave] = ahora
            ARCHIVO_ULTIMO_AVISO.write_text(json.dumps(ultimos), encoding='utf-8')
            logging.info('Aviso enviado: %s', clave)
        else:
            logging.warning('No se pudo enviar el aviso "%s"', clave)
    except Exception as exc:
        logging.warning('Fallo al avisar "%s" (%s)', clave, type(exc).__name__)


def relanzar_tarea(nombre: str) -> None:
    try:
        subprocess.run(['schtasks', '/Run', '/TN', nombre],
                       capture_output=True, timeout=25, creationflags=BANDERA_SIN_VENTANA)
        logging.info('Tarea relanzada: %s', nombre)
    except Exception as exc:
        logging.warning('No pude relanzar %s (%s)', nombre, type(exc).__name__)


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------

def revisar() -> list[str]:
    """Devuelve la lista de problemas encontrados (vacia si todo esta bien)."""
    problemas: list[str] = []

    # 1. Detector
    detector_vivo = proceso_vivo('camera_watch.py')
    estado = leer_json(ESTADO_DETECTOR)
    ultimo_latido = float(estado.get('updated') or 0)
    sin_latido = time.time() - ultimo_latido if ultimo_latido else 99999
    if not detector_vivo:
        problemas.append('el detector no esta corriendo')
    elif sin_latido > LATIDO_MAXIMO:
        problemas.append(f'el detector lleva {int(sin_latido)}s sin latido')

    # 2. Supervisor
    if not proceso_vivo('camera_watchdog.py'):
        problemas.append('el supervisor no esta corriendo')
    else:
        sup = leer_json(ESTADO_SUPERVISOR)
        if not sup.get('worker_healthy', True) and int(sup.get('failures') or 0) >= 3:
            problemas.append(f"el supervisor reporta {sup.get('failures')} fallos ({sup.get('reason')})")

    # 3. Cola de alertas
    pendientes = avisos_pendientes()
    if pendientes > 20:
        problemas.append(f'{pendientes} avisos sin entregar en la cola')

    # 4. Servicio de WhatsApp
    if not puerto_escucha(8780):
        problemas.append('el servicio de WhatsApp esta apagado (puerto 8780)')
    elif not servicio_whatsapp_conectado():
        problemas.append('WhatsApp no esta enlazado (hay que reescanear el QR)')

    return problemas


def main() -> int:
    problemas = revisar()
    if not problemas:
        logging.info('Revision correcta')
        return 0

    logging.warning('Problemas detectados: %s', '; '.join(problemas))

    # Intento de reparacion: relanzar las tareas del sistema.
    relanzar_tarea(TAREA_DETECTOR)
    relanzar_tarea(TAREA_FOTOS)
    time.sleep(10)

    siguen = revisar()
    if siguen:
        avisar('Camara con problemas: ' + '; '.join(siguen) +
               '. El monitor seguira intentando repararlo.', 'problema')
        return 1

    avisar('Camara: se resolvio solo (' + '; '.join(problemas) +
           '). Todo volvio a la normalidad.', 'recuperado')
    return 0


if __name__ == '__main__':
    sys.exit(main())
