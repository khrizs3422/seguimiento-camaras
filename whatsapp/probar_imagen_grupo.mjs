/**
 * Prueba de envio de imagen a un grupo, en dos formatos, para ver cual acepta
 * WhatsApp cuando la foto como "imagen" no aparece.
 *
 * Uso: node probar_imagen_grupo.mjs <carpeta_sesion> <jid> <ruta_imagen>
 */

import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import { readFileSync, existsSync } from 'fs';
import pino from 'pino';
import path from 'path';

const SESSION = process.argv[2];
const JID = process.argv[3];
const IMAGEN = process.argv[4];

if (!existsSync(IMAGEN)) {
    console.log('ERROR: no existe la imagen', IMAGEN);
    process.exit(2);
}

const { state, saveCreds } = await useMultiFileAuthState(SESSION);
const { version } = await fetchLatestBaileysVersion();

const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: 'warn' }),
    syncFullHistory: false,
    markOnlineOnConnect: false,
});

sock.ev.on('creds.update', saveCreds);

sock.ev.on('connection.update', async (update) => {
    if (update.connection !== 'open') return;

    const datos = readFileSync(IMAGEN);
    const nombre = path.basename(IMAGEN);

    // 1) como documento
    try {
        const r = await sock.sendMessage(JID, {
            document: datos,
            mimetype: 'image/webp',
            fileName: nombre,
            caption: 'PRUEBA A: enviada como ARCHIVO (documento)',
        });
        console.log('DOCUMENTO enviado id:', r?.key?.id);
    } catch (error) {
        console.log('DOCUMENTO ERROR:', error?.message || error);
    }

    await new Promise((r) => setTimeout(r, 4000));

    // 2) como imagen sin texto
    try {
        const r2 = await sock.sendMessage(JID, { image: datos, mimetype: 'image/webp' });
        console.log('IMAGEN sin texto enviada id:', r2?.key?.id);
    } catch (error) {
        console.log('IMAGEN sin texto ERROR:', error?.message || error);
    }

    setTimeout(() => process.exit(0), 5000);
});

setTimeout(() => { console.log('TIMEOUT'); process.exit(1); }, 90000);
