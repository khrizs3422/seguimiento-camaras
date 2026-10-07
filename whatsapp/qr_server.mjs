/**
 * Muestra un QR (que otro proceso deja en un archivo de texto) como imagen en el
 * navegador y lo renueva solo cada pocos segundos.
 *
 * Sirve para vincular WhatsApp cuando el QR no se puede mostrar en una consola:
 * la pagina se refresca y siempre muestra el codigo vigente.
 *
 * Uso: node qr_server.mjs <archivo_qr.txt> [puerto]
 */

import http from 'http';
import { existsSync, readFileSync } from 'fs';
import QRCode from 'qrcode';

const QR_FILE = process.argv[2];
const PORT = Number(process.argv[3] || 8790);

const pagina = `<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Vincular WhatsApp</title>
<style>
  body{background:#0f1115;color:#e8e8e8;font-family:system-ui,Segoe UI,sans-serif;margin:0;padding:28px;text-align:center}
  h1{font-size:22px;margin:0 0 6px}
  p{color:#9aa4b2;font-size:15px;margin:6px 0 18px}
  img{background:#fff;padding:18px;border-radius:14px;width:min(70vh,620px);height:auto}
  .aviso{margin-top:14px;color:#7dd3fc;font-size:14px}
</style></head>
<body>
  <h1>Vincular WhatsApp</h1>
  <p>En el telefono: WhatsApp &rarr; Ajustes &rarr; Dispositivos vinculados &rarr; Vincular un dispositivo</p>
  <img id="qr" src="/qr.png" alt="QR">
  <div class="aviso">El código se renueva solo. Escanéalo con tu teléfono y espera la confirmación.</div>
  <script>setInterval(() => { document.getElementById('qr').src = '/qr.png?' + Date.now(); }, 7000);</script>
</body></html>`;

const servidor = http.createServer(async (req, res) => {
    if (req.url.startsWith('/qr.png')) {
        try {
            const texto = existsSync(QR_FILE) ? readFileSync(QR_FILE, 'utf8').trim() : '';
            if (!texto) {
                res.writeHead(404, { 'Content-Type': 'text/plain' });
                return res.end('todavia sin qr');
            }
            const png = await QRCode.toBuffer(texto, { width: 620, margin: 1 });
            res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' });
            return res.end(png);
        } catch (error) {
            res.writeHead(500, { 'Content-Type': 'text/plain' });
            return res.end(String(error));
        }
    }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(pagina);
});

servidor.listen(PORT, '0.0.0.0', () => {
    console.log(`Listo en http://127.0.0.1:${PORT}`);
});

