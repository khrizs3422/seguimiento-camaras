/**
 * Alertas de la camara por WhatsApp
 * ---------------------------------------------------------------------------
 * Envia las fotos y clips que detecta el sistema de camaras como mensajes de
 * WhatsApp, sin tuneles publicos ni enlaces temporales.
 *
 * QUE HACE
 *   - Se conecta a WhatsApp como dispositivo vinculado (igual que WhatsApp Web).
 *   - Expone un servicio local en 127.0.0.1:8780 para que el sistema de camaras
 *     (Python) le pida enviar una alerta.
 *   - Envia la FOTO dentro del mensaje (no un enlace) con la fecha en el texto.
 *
 * POR QUE ASI
 *   Sin enlaces no hace falta un tunel de Cloudflare, que consumia RAM y dejaba
 *   procesos huerfanos con direcciones publicas antiguas sirviendo las fotos.
 *
 * VINCULACION (una sola vez)
 *   Al primer arranque muestra un codigo QR. Se escanea desde el telefono:
 *   WhatsApp -> Dispositivos vinculados -> Vincular un dispositivo.
 *   La sesion queda guardada en la carpeta "sesion", asi que no hay que repetirlo.
 *   Si WhatsApp desvincula el equipo, hay que volver a escanear el QR.
 *
 * USO
 *   POST http://127.0.0.1:8780/enviar
 *     { "imagen": "D:\\...\\foto.webp", "texto": "Persona detectada - 13 de septiembre" }
 *   POST http://127.0.0.1:8780/texto
 *     { "texto": "Aviso corto sin imagen" }
 *   GET  http://127.0.0.1:8780/estado   -> estado de la conexion
 *
 * NOTA DE PUERTOS
 *   El servicio escucha en 8780 por defecto (se puede cambiar con la variable
 *   de entorno WHATSAPP_PORT). Solo atiende en 127.0.0.1: no queda expuesto a
 *   la red.
 */

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');

// ---------------------------------------------------------------------------
// Registro de la libreria
//
// IMPORTANTE: la libreria trae su propio registro y por defecto escribe en la
// salida estandar. Cuando el servicio lo lanza el Programador de tareas, esa
// salida no existe y cada escritura falla con EPIPE. Eso provocaba un bucle de
// miles de errores por segundo. Por eso aqui se redirige TODO a un archivo.
// ---------------------------------------------------------------------------
function crearRegistroLibreria() {
    try {
        const pino = require('pino');
        const destino = pino.destination({ dest: ARCHIVO_LOG_LIBRERIA, mkdir: true, sync: false });
        // Se crea con pino(...) para que tenga .child(), que es lo que la
        // libreria usa internamente por cada subsistema.
        return pino({ level: 'warn' }, destino);
    } catch (error) {
        // Sin registrador propio no se rompe nada: la libreria se queda callada.
        registrar(`No pude preparar el registro de la libreria: ${error.message}`);
        return { level: 'silent' };
    }
}

const PUERTO = Number(process.env.WHATSAPP_PORT || 8780);
const BASE = __dirname;
const CARPETA_SESION = path.join(BASE, 'sesion');
const ARCHIVO_CONFIG = path.join(BASE, 'config.json');
const ARCHIVO_LOG = path.join(BASE, 'whatsapp_alerts.log');
const ARCHIVO_LOG_LIBRERIA = path.join(BASE, 'baileys.log');
const ARCHIVO_QR = path.join(BASE, 'ultimo_qr.txt');
const ARCHIVO_QR_PNG = path.join(BASE, 'vincular_whatsapp.png');

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

function registrar(mensaje) {
    const linea = `${new Date().toISOString()} ${mensaje}`;
    console.log(linea);
    try {
        fs.appendFileSync(ARCHIVO_LOG, linea + '\n', 'utf8');
    } catch (_) { /* el registro nunca debe tumbar el servicio */ }
}

function leerConfig() {
    const porDefecto = { destino: '', pais: '56' };
    try {
        if (fs.existsSync(ARCHIVO_CONFIG)) {
            return { ...porDefecto, ...JSON.parse(fs.readFileSync(ARCHIVO_CONFIG, 'utf8')) };
        }
    } catch (error) {
        registrar(`No pude leer config.json: ${error.message}`);
    }
    return porDefecto;
}

/** Deja el numero en formato internacional sin simbolos, como lo pide WhatsApp. */
function normalizarNumero(entrada, pais) {
    let soloDigitos = String(entrada || '').replace(/[^0-9]/g, '');
    if (!soloDigitos) return '';
    // Numero escrito como 9XXXXXXXX -> agregar el codigo de pais.
    if (soloDigitos.length === 9 && soloDigitos.startsWith('9')) {
        soloDigitos = pais + soloDigitos;
    }
    return soloDigitos;
}

/**
 * Arma el destinatario de WhatsApp a partir de config.json.
 *
 * Acepta dos formas:
 *   - un numero de telefono (ejemplo: 56912345678) -> se normaliza
 *   - un identificador ya listo (ejemplo: 120363000000000000@g.us para un grupo)
 *
 * IMPORTANTE: los grupos usan un identificador terminado en "@g.us". Si se le
 * aplica la normalizacion de numeros se le borra el sufijo y queda como si fuera
 * un telefono, con lo que los mensajes se envian a un numero inexistente y no
 * llegan a ninguna parte (sin error visible).
 */
function jidDeDestino(entrada, pais) {
    const texto = String(entrada || '').trim();
    if (texto.includes('@')) {
        return texto;
    }
    const numero = normalizarNumero(texto, pais);
    return numero ? `${numero}@s.whatsapp.net` : '';
}

const MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'];

/** Fecha en palabras, en la zona horaria de Chile. */
function fechaEnPalabras(fecha) {
    const partes = new Intl.DateTimeFormat('es-CL', {
        timeZone: 'America/Santiago',
        day: 'numeric',
        month: 'numeric',
    }).formatToParts(fecha);
    const dia = partes.find((p) => p.type === 'day').value;
    const mes = Number(partes.find((p) => p.type === 'month').value);
    return `${dia} de ${MESES[mes - 1]}`;
}

// ---------------------------------------------------------------------------
// Estado de la conexion
// ---------------------------------------------------------------------------

let socket = null;
let conectado = false;
let ultimoQr = null;
let ultimoError = null;
let desdeCuando = null;

// ---------------------------------------------------------------------------
// Conexion con WhatsApp
// ---------------------------------------------------------------------------

async function conectar() {
    fs.mkdirSync(CARPETA_SESION, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(CARPETA_SESION);
    let version;
    try {
        ({ version } = await fetchLatestBaileysVersion());
    } catch (_) {
        version = undefined; // si no hay internet, Baileys usa su version incluida
    }

    socket = makeWASocket({
        version,
        auth: state,
        logger: crearRegistroLibreria(),
        printQRInTerminal: false,
        browser: ['Seguimiento Camaras', 'Chrome', '1.0.0'],
        syncFullHistory: false,
        markOnlineOnConnect: false,
    });

    socket.ev.on('creds.update', saveCreds);

    socket.ev.on('connection.update', (actualizacion) => {
        const { connection, lastDisconnect, qr } = actualizacion;

        if (qr) {
            ultimoQr = qr;
            // Se guarda para poder volver a mostrarlo cuando haga falta vincular.
            try { fs.writeFileSync(ARCHIVO_QR, qr, 'utf8'); } catch (_) { /* opcional */ }
            // Imagen PNG: mucho mas facil de escanear que el dibujo de consola.
            try {
                require('qrcode').toFile(ARCHIVO_QR_PNG, qr, { width: 420, margin: 2 });
                registrar(`QR generado. Escanealo desde: ${ARCHIVO_QR_PNG}`);
            } catch (error) {
                registrar(`QR generado (no pude crear el PNG: ${error.message})`);
            }
            try {
                require('qrcode-terminal').generate(qr, { small: true });
            } catch (_) { /* el PNG ya sirve */ }
            // La salida estandar puede no existir si lo lanzo el Programador de
            // tareas; el QR en imagen es la via confiable.
        }

        if (connection === 'open') {
            conectado = true;
            ultimoQr = null;
            ultimoError = null;
            desdeCuando = new Date();
            try { if (fs.existsSync(ARCHIVO_QR)) fs.unlinkSync(ARCHIVO_QR); } catch (_) { /* opcional */ }
            registrar('Conectado a WhatsApp. Listo para enviar alertas.');
        }

        if (connection === 'close') {
            conectado = false;
            const codigo = lastDisconnect?.error?.output?.statusCode;
            const cerroSesion = codigo === DisconnectReason.loggedOut;
            ultimoError = `conexion cerrada (codigo ${codigo ?? 'desconocido'})`;
            registrar(`Desconectado: ${ultimoError}`);

            if (cerroSesion) {
                registrar('WhatsApp cerro la sesion. Hay que borrar la carpeta "sesion" y escanear el QR otra vez.');
                // No se reintenta: reintentar con credenciales invalidas no sirve.
                return;
            }
            // Cualquier otra caida (red, reinicio de WhatsApp) se reintenta sola.
            setTimeout(() => {
                registrar('Reintentando conexion...');
                conectar().catch((error) => registrar(`Fallo al reconectar: ${error.message}`));
            }, 5000);
        }
    });

    return socket;
}

// ---------------------------------------------------------------------------
// Preparacion de la imagen
//
// POR QUE EXISTE ESTO
//   La camara guarda las detecciones en WebP. WhatsApp NO muestra WebP como
//   imagen: el mensaje llega al telefono, pero queda pegado en "Esperando
//   mensaje. Esto puede tomar tiempo" porque el contenido nunca se puede
//   descargar. Convertir a JPEG antes de enviar resuelve el problema.
// ---------------------------------------------------------------------------

const FORMATOS_QUE_WHATSAPP_MUESTRA = new Set(['.jpg', '.jpeg', '.png']);

// Rutas candidatas de ffmpeg. La primera que funcione se recuerda. Se puede
// fijar una ruta propia en config.json con la clave "ffmpeg".
const RUTAS_FFMPEG = [
    'ffmpeg',
];

let rutaFfmpegRecordada = null;

/** Primera ruta de ffmpeg que realmente funcione. Se recuerda para no repetir la prueba. */
function buscarFfmpeg(config) {
    if (rutaFfmpegRecordada) return rutaFfmpegRecordada;
    const candidatos = config.ffmpeg ? [config.ffmpeg, ...RUTAS_FFMPEG] : RUTAS_FFMPEG;
    for (const candidato of candidatos) {
        try {
            execFileSync(candidato, ['-version'], { stdio: 'ignore', timeout: 15000 });
            rutaFfmpegRecordada = candidato;
            return candidato;
        } catch (_) { /* se prueba el siguiente */ }
    }
    return '';
}

/**
 * Deja la imagen en un formato que WhatsApp si muestre.
 * Devuelve { ruta, temporal }: si hubo conversion, hay que borrar el temporal.
 */
function prepararImagen(rutaOriginal, config) {
    const extension = path.extname(rutaOriginal).toLowerCase();
    if (FORMATOS_QUE_WHATSAPP_MUESTRA.has(extension)) {
        return { ruta: rutaOriginal, temporal: false };
    }

    const ffmpeg = buscarFfmpeg(config);
    if (!ffmpeg) {
        throw new Error(`no encontre ffmpeg para convertir ${extension || 'la imagen'} a JPEG`);
    }

    const temporal = path.join(
        os.tmpdir(),
        `alerta_${Date.now()}_${Math.random().toString(16).slice(2)}.jpg`,
    );

    // La camara guarda varias fotos seguidas y el antivirus tambien puede estar
    // leyendo el archivo, asi que la conversion puede fallar de forma pasajera
    // (archivo bloqueado o a medio escribir). Se reintenta antes de darse por
    // vencido, porque el respaldo (mandarlo como archivo) se ve peor.
    let ultimoError;
    for (let intento = 1; intento <= 3; intento += 1) {
        try {
            execFileSync(ffmpeg, [
                '-y', '-loglevel', 'error',
                '-i', rutaOriginal,
                '-frames:v', '1',
                '-q:v', '4',
                temporal,
            ], { stdio: 'ignore', timeout: 30000 });

            if (fs.existsSync(temporal) && fs.statSync(temporal).size > 0) {
                return { ruta: temporal, temporal: true };
            }
            throw new Error('ffmpeg no genero la imagen JPEG');
        } catch (error) {
            ultimoError = error;
            if (intento < 3) {
                // Espera corta y bloqueante (no hay nada mas que hacer mientras tanto).
                Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1500);
            }
        }
    }
    throw ultimoError;
}

// ---------------------------------------------------------------------------
// Envio de imagenes a grupos por un puente externo opcional
//
// POR QUE EXISTE
//   Esta version de Baileys deja las imagenes enviadas a un grupo en estado
//   "pendiente": el mensaje nunca aparece (el texto si llega, la imagen no).
//   Si dispones de otro gateway de WhatsApp que si las entregue como imagen
//   incrustada, se le puede delegar el envio a grupos aqui. Si ese puente no
//   esta disponible, se cae al envio propio como archivo, que es la unica
//   forma que este servicio logra por si solo.
// ---------------------------------------------------------------------------

const PUERTO_PUENTE_EXTERNO = Number(process.env.PUENTE_EXTERNO_PORT || 3000);

function enviarPorPuenteExterno(jid, rutaArchivo, texto, mediaType = 'image') {
    return new Promise((resolve) => {
        const cuerpo = JSON.stringify({
            chatId: jid,
            filePath: rutaArchivo,
            mediaType: mediaType,
            caption: texto || '',
        });
        const peticion = http.request({
            host: '127.0.0.1',
            port: PUERTO_PUENTE_EXTERNO,
            path: '/send-media',
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(cuerpo),
            },
            timeout: 60000,
        }, (respuesta) => {
            respuesta.resume();
            const ok = respuesta.statusCode >= 200 && respuesta.statusCode < 300;
            if (!ok) registrar(`El puente externo respondio HTTP ${respuesta.statusCode}`);
            resolve(ok);
        });
        peticion.on('error', (error) => {
            registrar(`No pude usar el puente externo: ${error.message}`);
            resolve(false);
        });
        peticion.on('timeout', () => { peticion.destroy(); resolve(false); });
        peticion.write(cuerpo);
        peticion.end();
    });
}

// ---------------------------------------------------------------------------
// Envio de una alerta
// ---------------------------------------------------------------------------

async function enviarAlerta(imagen, texto) {
    if (!conectado || !socket) {
        throw new Error('WhatsApp no esta conectado');
    }

    const config = leerConfig();
    const jid = jidDeDestino(config.destino, config.pais || '56');
    if (!jid) {
        throw new Error('Falta el numero de destino en config.json');
    }

    if (!imagen || !fs.existsSync(imagen)) {
        throw new Error(`No existe la imagen: ${imagen}`);
    }

    const esVideo = imagen.toLowerCase().endsWith('.mp4');
    const esGrupo = jid.endsWith('@g.us');

    if (esVideo) {
        const datos = fs.readFileSync(imagen);
        const kb = Math.round(datos.length / 1024);

        // En grupos: primero por el puente externo (si entrega video nativo a grupos)
        if (esGrupo && await enviarPorPuenteExterno(jid, imagen, texto, 'video')) {
            registrar(`Clip enviado a ${jid} por puente externo (video incrustado ${path.basename(imagen)}, ${kb} KB MP4)`);
            return { ok: true, destino: jid, bytes: datos.length, formato: 'video' };
        }

        // Camino directo de Baileys (o fallback)
        try {
            const mensaje = {
                video: datos,
                mimetype: 'video/mp4',
                caption: texto || '',
                gifPlayback: true,
            };
            await socket.sendMessage(jid, mensaje);
            registrar(`Clip enviado a ${jid} (video/gif ${path.basename(imagen)}, ${kb} KB MP4)`);
            return { ok: true, destino: jid, bytes: datos.length, formato: 'video' };
        } catch (err) {
            registrar(`Fallo envio directo de video a ${jid}: ${err.message}`);
            throw err;
        }
    }

    let preparada;
    try {
        preparada = prepararImagen(imagen, config);
    } catch (error) {
        // Sin conversion la foto no se podria ver. Se manda el archivo original
        // como documento: eso WhatsApp si lo entrega, aunque no lo previsualice.
        const originales = fs.readFileSync(imagen);
        await socket.sendMessage(jid, {
            document: originales,
            mimetype: 'image/webp',
            fileName: path.basename(imagen),
            caption: texto || '',
        });
        registrar(`Alerta enviada como archivo (no pude convertir a JPEG: ${error.message})`);
        return { ok: true, destino: jid, bytes: originales.length, formato: 'documento' };
    }

    try {
        const datos = fs.readFileSync(preparada.ruta);
        const esGrupo = jid.endsWith('@g.us');
        const kb = Math.round(datos.length / 1024);

        // En grupos: primero el puente externo (imagen incrustada de verdad).
        if (esGrupo && await enviarPorPuenteExterno(jid, preparada.ruta, texto)) {
            registrar(`Alerta enviada a ${jid} (imagen incrustada, ${kb} KB)`);
            return { ok: true, destino: jid, bytes: datos.length, formato: 'imagen' };
        }

        // Camino propio: en chats personales imagen; en grupos archivo, porque
        // este servicio no logra entregar imagenes a grupos.
        const mensaje = esGrupo
            ? {
                document: datos,
                mimetype: 'image/jpeg',
                fileName: `${path.basename(imagen, path.extname(imagen))}.jpg`,
                caption: texto || '',
            }
            : { image: datos, mimetype: 'image/jpeg', caption: texto || '' };

        await socket.sendMessage(jid, mensaje);
        registrar(`Alerta enviada a ${jid} (${esGrupo ? 'archivo' : 'imagen'} ${path.basename(imagen)}, ${kb} KB JPEG)`);
        return { ok: true, destino: jid, bytes: datos.length, formato: esGrupo ? 'documento' : 'imagen' };
    } finally {
        if (preparada.temporal) {
            try { fs.unlinkSync(preparada.ruta); } catch (_) { /* da igual */ }
        }
    }
}

async function enviarTexto(texto) {
    if (!conectado || !socket) throw new Error('WhatsApp no esta conectado');
    const config = leerConfig();
    const jid = jidDeDestino(config.destino, config.pais || '56');
    if (!jid) throw new Error('Falta el numero de destino en config.json');
    await socket.sendMessage(jid, { text: texto });
    registrar(`Aviso enviado a ${jid}: ${texto}`);
    return { ok: true, destino: jid };
}

// ---------------------------------------------------------------------------
// Servicio local
// ---------------------------------------------------------------------------

function responder(res, codigo, cuerpo) {
    const datos = JSON.stringify(cuerpo);
    res.writeHead(codigo, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(datos);
}

function leerCuerpo(req) {
    return new Promise((resolve, reject) => {
        let acumulado = '';
        req.on('data', (trozo) => {
            acumulado += trozo;
            if (acumulado.length > 1e6) reject(new Error('cuerpo demasiado grande'));
        });
        req.on('end', () => {
            try { resolve(acumulado ? JSON.parse(acumulado) : {}); }
            catch (error) { reject(new Error('JSON invalido')); }
        });
        req.on('error', reject);
    });
}

const servidor = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${PUERTO}`);

    if (req.method === 'GET' && url.pathname === '/estado') {
        const config = leerConfig();
        return responder(res, 200, {
            conectado,
            desde: desdeCuando,
            ultimo_error: ultimoError,
            destino_configurado: Boolean(config.destino),
            necesita_qr: Boolean(ultimoQr),
        });
    }

    if (req.method === 'POST' && (url.pathname === '/enviar' || url.pathname === '/texto')) {
        try {
            const cuerpo = await leerCuerpo(req);
            if (url.pathname === '/texto') {
                return responder(res, 200, await enviarTexto(cuerpo.texto));
            }
            return responder(res, 200, await enviarAlerta(cuerpo.imagen, cuerpo.texto));
        } catch (error) {
            registrar(`Error al enviar: ${error.message}`);
            return responder(res, 503, { ok: false, error: error.message });
        }
    }

    return responder(res, 404, { error: 'ruta desconocida' });
});

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

// Los errores se registran con limite: si algo falla en bucle, no debe llenar el
// disco ni tumbar el servicio. En ese caso se anota un resumen cada minuto.
let erroresEnRafaga = 0;
let ultimoResumen = 0;
function registrarError(mensaje) {
    erroresEnRafaga += 1;
    const ahora = Date.now();
    if (ahora - ultimoResumen > 60000) {
        const extra = erroresEnRafaga > 1 ? ` (${erroresEnRafaga} errores en el ultimo minuto)` : '';
        registrar(`ERROR: ${mensaje}${extra}`);
        erroresEnRafaga = 0;
        ultimoResumen = ahora;
    }
}

process.on('unhandledRejection', (error) => registrarError(`promesa rechazada: ${error?.message || error}`));
process.on('uncaughtException', (error) => registrarError(error.message));

servidor.on('error', (error) => {
    if (error.code === 'EADDRINUSE') {
        // Ya hay otro proceso atendiendo el puerto: este no debe quedarse vivo
        // dando vueltas. Se cierra para no consumir memoria sin hacer nada.
        registrar(`El puerto ${PUERTO} ya esta en uso; este proceso se cierra.`);
        process.exit(1);
    }
    registrarError(`Error del servidor: ${error.message}`);
});

servidor.listen(PUERTO, '127.0.0.1', () => {
    registrar(`Servicio de alertas por WhatsApp escuchando en 127.0.0.1:${PUERTO}`);
    conectar().catch((error) => registrar(`No pude conectar a WhatsApp: ${error.message}`));
});

// Cierre ordenado: la tarea programada puede detenerlo sin dejar la sesion a medias.
for (const senal of ['SIGINT', 'SIGTERM']) {
    process.on(senal, () => {
        registrar(`Recibida senal ${senal}; cerrando.`);
        try { socket?.end(undefined); } catch (_) { /* ya estaba cerrado */ }
        servidor.close(() => process.exit(0));
        setTimeout(() => process.exit(0), 3000);
    });
}
