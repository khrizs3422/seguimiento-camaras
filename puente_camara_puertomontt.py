import cv2
import threading
import time
import subprocess
import os
import sys
import re
from http.server import HTTPServer, BaseHTTPRequestHandler

RTSP_URL = os.environ.get("CAMARA_RTSP", "rtsp://usuario:clave@IP_DE_TU_CAMARA:554/")
PORT = 8088

current_frame = None
lock = threading.Lock()

class CamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/video':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            while True:
                with lock:
                    frame = current_frame
                if frame:
                    try:
                        self.wfile.write(b"--frame\r\n")
                        self.send_header('Content-Type', 'image/jpeg')
                        self.send_header('Content-Length', str(len(frame)))
                        self.end_headers()
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    except Exception:
                        break
                time.sleep(0.04)
        elif self.path == '/snapshot':
            with lock:
                frame = current_frame
            if frame:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(frame)
            else:
                self.send_response(503)
                self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Puente de camara RTSP</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>body {{ background: #000; color: #fff; text-align: center; font-family: sans-serif; margin: 0; padding: 20px; }} img {{ max-width: 100%; border-radius: 8px; }}</style>
</head>
<body>
    <h2>📹 Camara en Vivo</h2>
    <img src="/video" alt="En vivo" />
    <p>URL para la App SecureCam: <code>/video</code></p>
</body>
</html>"""
            self.wfile.write(html.encode('utf-8'))

    def log_message(self, format, *args):
        pass # Silenciar logs HTTP repetitivos

def rtsp_worker():
    global current_frame
    print(f"[*] Conectando al stream RTSP de la camara: {RTSP_URL}")
    while True:
        cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            print("[-] No se pudo abrir el stream RTSP. Reintentando en 3s...")
            time.sleep(3)
            continue

        print("[+] ¡Stream RTSP conectado exitosamente!")
        fail_count = 0
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                fail_count += 1
                if fail_count > 30:
                    print("[-] Perdida de senal RTSP. Reconectando...")
                    break
                time.sleep(0.03)
                continue

            fail_count = 0
            # Redimensionar a 960x540 para transmision ultra-fluida y bajo consumo de datos 5G
            h, w = frame.shape[:2]
            target_w = 960
            target_h = int(h * (target_w / w))
            resized = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)

            _, jpeg = cv2.imencode('.jpg', resized, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            with lock:
                current_frame = jpeg.tobytes()

            time.sleep(0.035) # ~25 fps

        cap.release()
        time.sleep(2)

def main():
    t = threading.Thread(target=rtsp_worker, daemon=True)
    t.start()

    server = HTTPServer(('0.0.0.0', PORT), CamHandler)
    print(f"[+] Servidor local listo en http://localhost:{PORT}")
    print("[*] Iniciando tunel Cloudflare para acceso 5G mundial...")

    cloudflared_path = os.environ.get(
        "CLOUDFLARED", r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
    if os.path.exists(cloudflared_path):
        cmd = [cloudflared_path, "tunnel", "--url", f"http://localhost:{PORT}"]
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
        
        # Buscar la URL publica de Cloudflare en la salida
        found_url = False
        for line in proc.stderr:
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match and not found_url:
                public_url = match.group(0)
                found_url = True
                print("\n" + "="*60)
                print(">>> ¡ENLACE 5G MUNDIAL ACTIVO! <<<")
                print(f"URL para ingresar en SecureCam:")
                print(f"{public_url}/video")
                print("="*60 + "\n")
                # Guardar en archivo para consulta rápida
                with open("url_5g_activa.txt", "w") as f:
                    f.write(f"{public_url}/video\n")
    else:
        print("[-] cloudflared no encontrado en la ruta por defecto.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando puente...")

if __name__ == "__main__":
    main()
