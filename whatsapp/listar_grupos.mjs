/**
 * Lista los grupos de WhatsApp a los que pertenece la cuenta, y
 * opcionalmente resuelve un link de invitacion.
 *
 * Se usa para averiguar el identificador (JID) del grupo "Alerta camaras", que es
 * lo que necesita whatsapp_alerts.js en config.json para mandar las fotos ahi en
 * vez de al chat personal.
 *
 * IMPORTANTE: usa la MISMA sesion que el servicio de alertas, asi que el servicio
 * debe estar detenido mientras corre (una sesion de Baileys no se comparte).
 *
 * Uso: node listar_grupos.mjs <carpeta_sesion> [codigo_de_invitacion]
 */

import { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import pino from 'pino';

const SESSION = process.argv[2];
const INVITE = (process.argv[3] || '').trim();

if (!SESSION) {
    console.error('falta la carpeta de sesion');
    process.exit(2);
}

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
        const grupos = await sock.groupFetchAllParticipating();
        const entradas = Object.entries(grupos);
        console.log(`GRUPOS (${entradas.length}):`);
        for (const [jid, grupo] of entradas) {
            console.log(`  ${jid} :: ${grupo.subject}`);
        }
    } catch (error) {
        console.log('ERROR listando grupos:', error.message);
    }

    if (INVITE) {
        try {
            const info = await sock.groupGetInviteInfo(INVITE);
            console.log(`INVITACION: ${info.id} :: ${info.subject}`);
        } catch (error) {
            console.log('ERROR con la invitacion:', error.message);
        }
    }

    setTimeout(() => process.exit(0), 1500);
});

setTimeout(() => {
    console.log('TIMEOUT sin conectar');
    process.exit(1);
}, 50000);
