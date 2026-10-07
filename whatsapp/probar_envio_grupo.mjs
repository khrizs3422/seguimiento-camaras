/**
 * Envia un mensaje de prueba a un grupo y muestra el resultado REAL que devuelve
 * WhatsApp (id del mensaje o el error), para distinguir "aceptado por la libreria"
 * de "entregado de verdad".
 *
 * Uso: node probar_envio_grupo.mjs <carpeta_sesion> <jid> <texto>
 */

import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import pino from 'pino';

const SESSION = process.argv[2];
const JID = process.argv[3];
const TEXTO = process.argv[4] || 'PRUEBA de envio al grupo';

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

    try {
        const enviado = await sock.sendMessage(JID, { text: TEXTO });
        console.log('ENVIADO id:', enviado?.key?.id, '| a:', enviado?.key?.remoteJid);
        console.log('estado del envio:', enviado?.status);
    } catch (error) {
        console.log('ERROR AL ENVIAR:', error?.message || error);
    }

    setTimeout(() => process.exit(0), 4000);
});

setTimeout(() => { console.log('TIMEOUT'); process.exit(1); }, 60000);
