/**
 * Muestra los ajustes de un grupo de WhatsApp y el rol de cada miembro.
 *
 * Sirve para diagnosticar por que un mensaje "enviado" no aparece: si el grupo
 * tiene activado "solo administradores pueden enviar" (announce) y la cuenta que
 * envia no es administradora, el servidor descarta el mensaje sin avisar.
 *
 * Uso: node estado_grupo.mjs <carpeta_sesion> <jid_del_grupo>
 */

import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import pino from 'pino';

const SESSION = process.argv[2];
const JID = process.argv[3];

const { state, saveCreds } = await useMultiFileAuthState(SESSION);
const { version } = await fetchLatestBaileysVersion();

const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    logger: pino({ level: 'silent' }),
    syncFullHistory: false,
    markOnlineOnConnect: false,
});

sock.ev.on('creds.update', saveCreds);

sock.ev.on('connection.update', async (update) => {
    if (update.connection !== 'open') return;

    try {
        const meta = await sock.groupMetadata(JID);
        console.log('GRUPO:', meta.subject, '|', meta.id);
        console.log('solo administradores pueden enviar (announce):', meta.announce);
        console.log('solo administradores editan (restrict):', meta.restrict);
        console.log('miembros:', meta.participants.length);
        for (const p of meta.participants) {
            console.log(`  ${p.id} | admin=${p.admin || 'no'} | lid=${p.lid || '-'}`);
        }
        console.log('YO SOY:', sock.user?.id);
    } catch (error) {
        console.log('ERROR:', error.message);
    }

    setTimeout(() => process.exit(0), 1500);
});

setTimeout(() => { console.log('TIMEOUT'); process.exit(1); }, 50000);
