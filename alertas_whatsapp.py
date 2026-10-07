"""Alertas de la camara por WhatsApp.

COMO FUNCIONA
  El detector guarda la foto y llama a `enqueue()`. Este modulo apunta el evento
  en una cola propia (SQLite) y un hilo de fondo la envia por WhatsApp usando el
  servicio local que escucha en 127.0.0.1:8780 (whatsapp/whatsapp_alerts.js).

  La imagen viaja DENTRO del mensaje, con la fecha en el texto. No hay enlaces
  ni tuneles publicos.

GARANTIAS
  - La cola es persistente: si WhatsApp esta caido, el aviso se reintenta.
  - Cada evento tiene un ID fijo, asi que nunca se envia dos veces el mismo.
  - Los reintentos se espacian; tras `alerta_vigencia_horas` el aviso vence.
  - Un fallo del envio NUNCA detiene la camara.

ARCHIVOS
  alertas_whatsapp.sqlite3 : cola de eventos y su estado
  alertas_whatsapp.log     : registro del envio
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
EVENTOS = BASE / 'events'
DB = BASE / 'alertas_whatsapp.sqlite3'
REGISTRO = BASE / 'alertas_whatsapp.log'

# Servicio local de WhatsApp (whatsapp/whatsapp_alerts.js)
SERVICIO = os.environ.get('WHATSAPP_ALERTS_URL', 'http://127.0.0.1:8780')

# Ajustes de la cola
VIGENCIA_HORAS = float(os.environ.get('ALERTA_VIGENCIA_HORAS', 24))
ESPERA_ENTRE_REINTENTOS = 20.0     # segundos entre intentos del mismo aviso
ESPERA_SIN_CONEXION = 30.0         # espera cuando el servicio no responde
PAUSA_ENTRE_CICLOS = 5.0

MESES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
         'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')

DETENER = threading.Event()
_iniciado = False
_cerrojo = threading.Lock()


# ---------------------------------------------------------------------------
# Cola
# ---------------------------------------------------------------------------

@contextmanager
def conexion():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS avisos
        (id TEXT PRIMARY KEY, path TEXT NOT NULL,
         created REAL NOT NULL, state TEXT NOT NULL,
         text TEXT, detail TEXT, attempt REAL DEFAULT 0)''')
    try:
        with db:
            yield db
    finally:
        db.close()


def fecha_en_palabras(momento: float | None = None) -> str:
    """Fecha en palabras: '13 de septiembre'."""
    fecha = datetime.fromtimestamp(momento) if momento else datetime.now()
    return f'{fecha.day} de {MESES[fecha.month - 1]}'


def _texto_de(path: Path, created: float) -> str:
    if path.name.startswith('test_'):
        etiqueta = 'Prueba de camara'
    elif path.suffix.lower() == '.mp4':
        etiqueta = 'Clip de movimiento'
    else:
        etiqueta = 'Persona detectada'
    return f'{etiqueta} - {fecha_en_palabras(created)}'


def enqueue(path, created: float | None = None, custom_text: str | None = None) -> str:
    """Apunta una foto o video en la cola. Devuelve el ID del evento."""
    ruta = Path(path).resolve()
    try:
        relativa = ruta.relative_to(EVENTOS.resolve())
    except ValueError:
        raise ValueError('Solo se pueden enviar archivos de eventos de la camara')
    if not ruta.is_file() or ruta.suffix.lower() not in ('.jpg', '.jpeg', '.webp', '.mp4'):
        raise ValueError('Solo se pueden enviar imagenes o videos de eventos de la camara')

    momento = created if created is not None else time.time()
    texto_aviso = custom_text if custom_text else _texto_de(ruta, momento)
    # El ID depende de la ruta: el mismo evento nunca se encola dos veces.
    event_id = hashlib.sha256(relativa.as_posix().encode()).hexdigest()[:32]
    with conexion() as db:
        db.execute(
            'INSERT OR IGNORE INTO avisos (id,path,created,state,text) VALUES (?,?,?,?,?)',
            (event_id, str(ruta), momento, 'pendiente', texto_aviso))
    return event_id


def pendientes() -> int:
    """Cuantos avisos estan sin entregar."""
    with conexion() as db:
        return db.execute(
            "SELECT COUNT(*) FROM avisos WHERE state IN ('pendiente','enviando')").fetchone()[0]


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

def _servicio_responde() -> bool:
    try:
        with urllib.request.urlopen(f'{SERVICIO}/estado', timeout=8) as r:
            estado = json.loads(r.read().decode('utf-8'))
        return bool(estado.get('conectado'))
    except Exception:
        return False


def _enviar(archivo: Path, texto: str) -> None:
    cuerpo = json.dumps({'imagen': str(archivo), 'video': str(archivo), 'texto': texto}).encode('utf-8')
    peticion = urllib.request.Request(
        f'{SERVICIO}/enviar', data=cuerpo,
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(peticion, timeout=90) as r:
        respuesta = json.loads(r.read().decode('utf-8'))
    if not respuesta.get('ok'):
        raise RuntimeError(respuesta.get('error', 'envio rechazado'))


def enviar_texto(texto: str) -> bool:
    """Aviso corto sin imagen (fallos y recuperaciones). Devuelve si se pudo."""
    try:
        cuerpo = json.dumps({'texto': texto}).encode('utf-8')
        peticion = urllib.request.Request(
            f'{SERVICIO}/texto', data=cuerpo,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(peticion, timeout=30) as r:
            return bool(json.loads(r.read().decode('utf-8')).get('ok'))
    except Exception as exc:
        logging.warning('No pude enviar el aviso de texto (%s)', type(exc).__name__)
        return False


def _trabajador() -> None:
    """Hilo de fondo: entrega los avisos pendientes."""
    logging.info('Cola de alertas por WhatsApp iniciada')
    while not DETENER.is_set():
        ahora = time.time()
        try:
            with conexion() as db:
                # Lo que quedo demasiado viejo se marca vencido y no se reintenta.
                db.execute(
                    "UPDATE avisos SET state='vencido' WHERE created<? AND state IN ('pendiente','enviando')",
                    (ahora - VIGENCIA_HORAS * 3600,))
                fila = db.execute(
                    "SELECT * FROM avisos WHERE state IN ('pendiente','enviando') AND attempt<? "
                    "ORDER BY created LIMIT 1",
                    (ahora - ESPERA_ENTRE_REINTENTOS,)).fetchone()

            if fila is None:
                DETENER.wait(PAUSA_ENTRE_CICLOS)
                continue

            if not _servicio_responde():
                logging.warning('El servicio de WhatsApp no esta conectado; reintentare')
                DETENER.wait(ESPERA_SIN_CONEXION)
                continue

            with conexion() as db:
                db.execute("UPDATE avisos SET state='enviando', attempt=? WHERE id=?",
                           (time.time(), fila['id']))
            imagen = Path(fila['path'])
            try:
                _enviar(imagen, fila['text'])
                with conexion() as db:
                    db.execute("UPDATE avisos SET state='enviado', detail=? WHERE id=?",
                               (json.dumps({'enviado': time.time()}), fila['id']))
                logging.info('Alerta enviada: %s (%s)', fila['id'][:8], imagen.name)
            except Exception as exc:
                # Se devuelve a 'pendiente' para que el proximo ciclo reintente.
                with conexion() as db:
                    db.execute("UPDATE avisos SET state='pendiente', detail=? WHERE id=?",
                               (json.dumps({'error': str(exc)[:200]}), fila['id']))
                logging.warning('Reintento pendiente para %s (%s)', fila['id'][:8], type(exc).__name__)
                DETENER.wait(ESPERA_SIN_CONEXION)
        except Exception as exc:
            logging.warning('Error en la cola de alertas (%s)', type(exc).__name__)
            DETENER.wait(ESPERA_SIN_CONEXION)


def iniciar() -> None:
    """Arranca el hilo de envio una sola vez."""
    global _iniciado
    with _cerrojo:
        if _iniciado:
            return
        if not logging.getLogger().handlers:
            logging.basicConfig(
                filename=REGISTRO, level=logging.INFO,
                format='%(asctime)s %(levelname)s %(message)s')
        hilo = threading.Thread(target=_trabajador, daemon=True, name='alertas-whatsapp')
        hilo.start()
        _iniciado = True


def detener() -> None:
    DETENER.set()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Alertas de la camara por WhatsApp')
    parser.add_argument('--encolar', type=Path, help='encola una imagen de events')
    parser.add_argument('--texto', help='envia un aviso corto de texto')
    parser.add_argument('--estado', action='store_true', help='muestra la cola')
    args = parser.parse_args()

    if args.encolar:
        print(enqueue(args.encolar))
    elif args.texto:
        print('enviado' if enviar_texto(args.texto) else 'fallo')
    elif args.estado:
        with conexion() as db:
            print('conexion con WhatsApp:', 'OK' if _servicio_responde() else 'NO')
            print('avisos pendientes:', pendientes())
            for fila in db.execute('SELECT id,state,created,path FROM avisos ORDER BY created DESC LIMIT 10'):
                cuando = datetime.fromtimestamp(fila['created']).strftime('%d/%m %H:%M')
                print(f"  {fila['state']:<10} {cuando}  {Path(fila['path']).name}")
    else:
        parser.print_help()
