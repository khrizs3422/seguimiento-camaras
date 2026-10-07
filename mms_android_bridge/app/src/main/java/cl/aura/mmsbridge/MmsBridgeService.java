package cl.aura.mmsbridge;

import android.app.*;
import android.content.*;
import android.os.*;
import android.telephony.*;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;

/** Pinned TLS LAN/USB messaging bridge, one authorized recipient, no LLM. */
public class MmsBridgeService extends Service {
    protected void attachBaseContext(Context base) { super.attachBaseContext(BootStorage.context(base)); }
    // Numero autorizado que recibe los SMS. Editalo con tu propio numero.
    public static final String TO = "+56900000000";
    private ServerSocket server;
    private final ExecutorService workers = new ThreadPoolExecutor(3,3,0L,TimeUnit.SECONDS,
        new ArrayBlockingQueue<Runnable>(12),new ThreadPoolExecutor.AbortPolicy());
    private final ScheduledExecutorService monitor = Executors.newSingleThreadScheduledExecutor();
    private volatile boolean listening, discovering;
    private volatile long lastContact;
    private final long started = SystemClock.elapsedRealtime();
    private volatile boolean running;
    private PowerManager.WakeLock wake;
    private DatagramSocket discovery;
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("aura", "Alertas Aura", NotificationManager.IMPORTANCE_LOW));
        startForeground(1001, new Notification.Builder(this, "aura")
            .setContentTitle("Aura: puente SMS activo").setContentText("Avisos a " + TO)
            .setSmallIcon(android.R.drawable.stat_sys_upload).setOngoing(true).build());
        running = true;
        wake=getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"aura:sms");
        wake.acquire();
        new Thread(() -> {
          while(running) {
            try(DatagramSocket d=new DatagramSocket(null)) {
                d.setReuseAddress(true);
                d.bind(new InetSocketAddress(8766));
                discovery=d;
                discovering=true;
                while(running) {
                    DatagramPacket p=new DatagramPacket(new byte[128],128); d.receive(p);
                    String q=new String(p.getData(),0,p.getLength(),StandardCharsets.US_ASCII);
                    if(q.matches("AURA_DISCOVER [a-z0-9]{24}")) {
                        byte[] reply=("AURA_SMS "+q.substring(14)).getBytes(StandardCharsets.US_ASCII);
                        d.send(new DatagramPacket(reply,reply.length,p.getAddress(),p.getPort()));
                    }
                }
            } catch(Exception e) { android.util.Log.w("AuraBridge","Discovery retry",e); }
            finally { discovering=false; }
            retryPause();
          }
        },"aura-discovery").start();
        new Thread(() -> {
          while(running) {
            try (ServerSocket s = BridgeTls.server(this)) {
                server = s;
                listening=true;
                while (running) {
                    Socket c = s.accept();
                    try { workers.execute(() -> handle(c)); }
                    catch(RejectedExecutionException full) { c.close(); }
                }
            } catch (Exception e) { android.util.Log.w("AuraBridge","TLS listener retry",e); }
            finally { listening=false; }
            retryPause();
          }
        },"aura-tls-listener").start();
        monitor.scheduleWithFixedDelay(() -> {
            try {
                long age=lastContact==0 ? -1 : (SystemClock.elapsedRealtime()-lastContact)/1000;
                String status=!listening ? "Recuperando servidor de mensajes" :
                    age<0 ? "Esperando conexion del PC" : age>90 ? "Sin contacto del PC hace "+age+" s" : "PC conectado · SMS disponible";
                getSharedPreferences("operational",0).edit().putString("status",status)
                    .putLong("heartbeat",System.currentTimeMillis()).apply();
                nm.notify(1001,new Notification.Builder(this,"aura").setContentTitle("Vigilancia del hogar")
                    .setContentText(status).setSmallIcon(android.R.drawable.stat_sys_upload)
                    .setOngoing(true).build());
            } catch(Exception e) { android.util.Log.w("AuraBridge","Health update",e); }
        },0,30,TimeUnit.SECONDS);
    }
    private void retryPause() {
        if(running) try { Thread.sleep(3000); } catch(InterruptedException e) { Thread.currentThread().interrupt(); }
    }
    public int onStartCommand(Intent i, int flags, int id) { return START_STICKY; }
    public IBinder onBind(Intent i) { return null; }
    public void onDestroy() {
        running = false;
        if(discovery!=null)discovery.close();
        if(wake!=null && wake.isHeld())wake.release();
        try { if (server != null) server.close(); } catch (IOException ignored) {}
        monitor.shutdownNow(); workers.shutdownNow(); super.onDestroy();
    }
    static String line(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        for (int n; (n = in.read()) != -1;) {
            if (n == 10) return b.toString("US-ASCII").replace("\r", "");
            if (b.size() >= 4096) throw new IOException("header too long");
            b.write(n);
        }
        throw new EOFException();
    }
    private void handle(Socket c) {
        try (Socket socket = c) {
            socket.setSoTimeout(10000);
            InputStream in = socket.getInputStream();
            String[] request = line(in).split(" ");
            Map<String,String> headers = new HashMap<>();
            for (int count = 0; ; count++) {
                String h = line(in); if (h.isEmpty()) break;
                if (count > 30) throw new IOException("too many headers");
                int colon = h.indexOf(':');
                if (colon < 1) throw new IOException("bad header");
                headers.put(h.substring(0,colon).toLowerCase(Locale.ROOT),h.substring(colon+1).trim());
            }
            String token = headers.getOrDefault("x-aura-token", "");
            if (!MessageDigest.isEqual(token.getBytes(StandardCharsets.UTF_8), BridgeConfig.getToken(this).getBytes(StandardCharsets.UTF_8))) {
                respond(socket, 401, new JSONObject().put("error","unauthorized")); return;
            }
            if (request.length != 3) throw new IOException("bad request");
            lastContact=SystemClock.elapsedRealtime();
            if (request[0].equals("GET") && request[1].equals("/health")) {
                int sub = SubscriptionManager.getDefaultSmsSubscriptionId();
                respond(socket,200,new JSONObject().put("ok",true).put("service","aura-sms-bridge")
                    .put("recipient",TO).put("subscription_id",sub).put("inbound_sms",true)
                    .put("version","0.4.0-resilient").put("uptime_seconds",(SystemClock.elapsedRealtime()-started)/1000)
                    .put("tls_listening",listening).put("discovery_listening",discovering)
                    .put("automatic_rcs",false)
                    .put("carrier_mms_enabled",SmsManager.getSmsManagerForSubscriptionId(sub)
                        .getCarrierConfigValues().getBoolean("enabledMMS",false))); return;
            }
            if (request[0].equals("GET") && request[1].equals("/inbox")) {
                try(SmsInbox inbox=new SmsInbox(this)){respond(socket,200,new JSONObject().put("messages",inbox.pending()));} return;
            }
            if (request[0].equals("GET") && request[1].startsWith("/status/")) {
                String id = request[1].substring(8);
                respond(socket,200,new JSONObject(getSharedPreferences("status",0).getString(id,"{\"state\":\"unknown\"}"))); return;
            }
            if (!request[0].equals("POST") || !(request[1].equals("/send-sms")||request[1].equals("/ack"))) {
                respond(socket,404,new JSONObject().put("error","not found")); return;
            }
            int length = Integer.parseInt(headers.getOrDefault("content-length","0"));
            if (length < 1 || length > 4096) throw new IOException("invalid size");
            byte[] body = new byte[length]; int offset = 0;
            while (offset < length) { int n=in.read(body,offset,length-offset); if(n<0)throw new EOFException(); offset+=n; }
            JSONObject p = new JSONObject(new String(body,StandardCharsets.UTF_8));
            if(request[1].equals("/ack")){
                String id=p.getString("id");if(!id.matches("[0-9a-f]{64}"))throw new IOException("bad id");
                try(SmsInbox inbox=new SmsInbox(this)){inbox.ack(id);}
                respond(socket,200,new JSONObject().put("acked",true));return;
            }
            if (!TO.equals(p.optString("to"))) { respond(socket,403,new JSONObject().put("error","recipient not allowed")); return; }
            String id = p.getString("id"); String text = p.getString("text");
            if (!id.matches("[a-zA-Z0-9_-]{8,80}") || text.isEmpty() || text.length()>459) throw new IOException("invalid message");
            synchronized (this) {
                SharedPreferences state = getSharedPreferences("status",0);
                if (state.contains(id)) { respond(socket,202,new JSONObject(state.getString(id,"{}"))); return; }
                int sub = SubscriptionManager.getDefaultSmsSubscriptionId();
                if (sub < 0 || sub != p.getInt("subscription_id")) {
                    respond(socket,409,new JSONObject().put("error","SIM changed; verify sender")); return;
                }
                SmsManager sms = SmsManager.getSmsManagerForSubscriptionId(sub);
                ArrayList<String> parts=sms.divideMessage(text);
                if (parts.size()>3) { respond(socket,400,new JSONObject().put("error","three SMS parts maximum")); return; }
                long last = state.getLong("_last",0);
                if (System.currentTimeMillis()-last<10000) { respond(socket,429,new JSONObject().put("error","rate limit")); return; }
                JSONObject accepted = new JSONObject().put("id",id).put("accepted",true).put("state","submitted");
                if (!state.edit().putString(id,accepted.toString()).putLong("_last",System.currentTimeMillis()).commit()) throw new IOException("storage");
                try {
                    int piFlags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
                    ArrayList<PendingIntent> sentList=new ArrayList<>(),deliveredList=new ArrayList<>();
                    for(int part=0;part<parts.size();part++){
                        Intent sent=new Intent(this,SmsResultReceiver.class).setAction("sent."+id+"."+part).putExtra("id",id).putExtra("part",part).putExtra("total",parts.size());
                        Intent delivered=new Intent(this,SmsResultReceiver.class).setAction("delivered."+id+"."+part).putExtra("id",id).putExtra("part",part).putExtra("total",parts.size());
                        sentList.add(PendingIntent.getBroadcast(this,0,sent,piFlags));deliveredList.add(PendingIntent.getBroadcast(this,0,delivered,piFlags));
                    }
                    if(parts.size()==1)sms.sendTextMessage(TO,null,text,sentList.get(0),deliveredList.get(0));
                    else sms.sendMultipartTextMessage(TO,null,parts,sentList,deliveredList);
                } catch (Exception e) {
                    accepted.put("state","failed").put("error",e.getClass().getSimpleName());
                    state.edit().putString(id,accepted.toString()).commit();
                }
                respond(socket,202,accepted);
            }
        } catch (Exception ignored) { /* Timeout is unknown, never proof of non-delivery. */ }
    }
    private void respond(Socket s,int code,JSONObject body) throws IOException {
        byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);
        OutputStream out=s.getOutputStream();
        out.write(("HTTP/1.1 "+code+" Result\r\nContent-Type: application/json\r\nContent-Length: "+bytes.length+"\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
        out.write(bytes); out.flush();
    }
}
