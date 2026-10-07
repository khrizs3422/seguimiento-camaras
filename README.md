# Seguimiento de cámaras — detección de personas con IA, todo local

Sistema casero que vigila una cámara IP y **avisa por WhatsApp cuando detecta a una persona**, con
la foto dentro del propio mensaje. El video nunca sale del computador: el análisis corre local con
un modelo ONNX y lo único que se envía es la alerta con su imagen.

Nació para vigilar una casa en Puerto Montt con una cámara RTSP común y un PC que ya estaba
encendido. No hace falta nube, ni cuentas, ni tarjetas.

- Autor: **Christopher Aguila** ([@khrizs3422](https://github.com/khrizs3422))
- Licencia: MIT

## Qué trae

| Archivo | Qué hace |
| --- | --- |
| `camera_watch.py` | Detector: lee el stream RTSP, corre YOLO11n en local, aplica zonas de interés y umbrales de día/noche, guarda el evento y encola la alerta. |
| `alertas_whatsapp.py` | Cola de alertas: reintenta lo que no se pudo enviar y avisa si el servicio de WhatsApp está caído. |
| `whatsapp/whatsapp_alerts.js` | Servicio local (Node) que se vincula a WhatsApp como dispositivo y manda la foto con la fecha. Escucha en `127.0.0.1:8780`. |
| `camera_watchdog.py` | Supervisor: relanza el detector si el proceso se cae o si el latido se queda pegado. |
| `camera_health.py` | Chequeo de salud cada 10 minutos (detector, supervisor, cola y servicio de WhatsApp). Es silencioso mientras todo está bien y avisa solo cuando hay un problema o cuando se recupera. |
| `puente_camara_puertomontt.py` | Sirve el stream RTSP como video HTTP (`:8088`) para ver la cámara desde el navegador en la red local o por un túnel. |
| `mms_android_bridge/` | App Android que lee los SMS/MMS entrantes del teléfono y los reenvía a un servicio propio. Se usó cuando el aviso tenía que llegar por SMS. |
| `run_camera_hidden.vbs` | Lanza el supervisor sin ventana (para el Programador de tareas de Windows). |

## Cómo funciona

1. El detector toma fotogramas del stream RTSP cada cierto intervalo y primero mira si hay movimiento
   (prefiltro barato). Si no lo hay, no gasta modelo.
2. Cuando hay movimiento corre la detección de personas con **YOLO11n** (`onnxruntime`) y comprueba
   que la persona caiga dentro de alguna **zona de interés** dibujada como polígono en la
   configuración.
3. Para no avisar por cualquier cosa exige varios aciertos seguidos (`confirm_hits` en una ventana de
   segundos) y aplica un **tiempo de espera** entre avisos del mismo tipo.
4. De noche usa umbrales propios, porque la imagen oscura baja la confianza del modelo.
5. Guarda el evento con varias imágenes (`.webp`) y encola la alerta; el servicio de WhatsApp la
   aparta de la cola y la envía con la foto y la fecha.
6. El supervisor vigila el latido del detector y el chequeo de salud vigila todo el conjunto.

## Requisitos

- Windows 10/11 (los lanzadores y el Programador de tareas son de Windows; el resto es portable).
- Python 3.11 o superior.
- Una cámara IP con RTSP.
- Node.js 20 o superior, solo para el servicio de WhatsApp.
- El modelo de detección: **no viene en el repositorio** (pesa ~11 MB). Exporta o descarga
  `yolo11n.onnx` (Ultralytics YOLO11 nano en formato ONNX) y déjalo en `models/yolo11n.onnx`.

## Instalación

```bash
git clone https://github.com/khrizs3422/seguimiento-camaras.git
cd seguimiento-camaras
pip install -r requirements.txt
mkdir -p models && cp /ruta/a/tu/yolo11n.onnx models/yolo11n.onnx
cp .env.example .env          # opcional: todo tiene valores por defecto
```

Configura la cámara en `camera_ai.json` (`rtsp_url`) o con la variable `CAMARA_RTSP`, y ajusta las
zonas de interés a tu patio.

Vincula el WhatsApp (una sola vez):

```bash
cd whatsapp
npm install
node whatsapp_alerts.js       # muestra un QR: escanéalo desde el teléfono
```

La sesión queda guardada en `whatsapp/sesion/`, así que no hay que repetir el escaneo. Después pon
el destinatario en `whatsapp/config.json` (un número en formato internacional o el identificador de un
grupo terminado en `@g.us`); si queda vacío, el servicio no envía nada y lo reporta como error.

## Uso

```bash
python camera_watch.py --once --no-notify   # una pasada de prueba, sin avisar
python camera_watch.py                      # el detector en marcha
python alertas_whatsapp.py --estado         # estado de la cola de alertas
python alertas_whatsapp.py --texto "prueba" # manda un aviso corto de prueba
python camera_health.py                     # revisa todo el conjunto y avisa si algo falla
python puente_camara_puertomontt.py         # visor: http://localhost:8088/video
```

Para dejarlo funcionando solo, en el Programador de tareas de Windows: `run_camera_hidden.vbs` con
reinicio en caso de fallo, y `camera_health.py` cada 10 minutos.

## Configuración principal (`camera_ai.json`)

| Clave | Para qué sirve |
| --- | --- |
| `rtsp_url` | La cámara. También se puede pasar por `CAMARA_RTSP`. |
| `interest_zones` | Polígonos (en píxeles) donde sí importa una persona: patio, portón, pasillo. |
| `confidence_threshold` / `night_confidence_threshold` | Confianza mínima de día y de noche. |
| `confirm_hits` / `confirm_window_seconds` | Cuántos aciertos seguidos se exigen y en cuánto tiempo. |
| `alert_cooldown_seconds` | Tiempo mínimo entre avisos. |
| `enable_motion_prefilter` | Salta el modelo cuando no hay movimiento (ahorra CPU). |
| `event_images`, `keep_event_days` | Cuántas imágenes guarda por evento y cuántos días las conserva. |

También se puede ajustar con `.env`: `CAMARA_RTSP`, `WHATSAPP_ALERTS_URL`, `WHATSAPP_PORT`,
`ALERTA_VIGENCIA_HORAS`, `CLOUDFLARED` (ruta al ejecutable, solo para el visor remoto).

## Privacidad

El análisis es 100% local. El video no se sube a ningún servicio y no se abren enlaces públicos
temporales para las alertas: lo que se envía es la foto del evento por el propio WhatsApp del dueño.
Las carpetas `events/`, `logs/`, `models/` y `whatsapp/sesion/` quedan fuera del repositorio.

## Limitaciones conocidas

- **Una cámara por instancia.** Para varias cámaras hay que correr varias copias con su propio
  puerto y su propia carpeta de eventos.
- El modelo es **YOLO11n** (el más liviano): detecta personas bien de día y razonablemente de noche,
  pero no distingue animales de personas a distancia ni lee patentes.
- El servicio de WhatsApp usa una biblioteca **no oficial** (Baileys): si WhatsApp cambia algo, hay
  que actualizarla, y si desvincula el equipo hay que volver a escanear el QR.
- El puente de video por túnel es para mirar, no para grabar: la grabación sería el propio stream
  RTSP de la cámara.
- Pensado y probado en Windows; los lanzadores y las tareas programadas son de Windows.

## Licencia

MIT — ver [LICENSE](LICENSE).
