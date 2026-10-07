package cl.aura.mmsbridge;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public class MainActivity extends Activity {
    protected void attachBaseContext(android.content.Context base) { super.attachBaseContext(BootStorage.context(base)); }
    private static final int REQUEST_PERMISSIONS = 70;
    private TextView live;
    private final android.os.Handler refresh = new android.os.Handler();
    private final Runnable update = new Runnable() {
        public void run() {
            android.content.SharedPreferences state=getSharedPreferences("operational",0);
            long age=System.currentTimeMillis()-state.getLong("heartbeat",0);
            if(live!=null)live.setText(age>90000 ? "Servicio sin latido reciente. Pulsa Recuperar servicio." : state.getString("status","Iniciando"));
            refresh.postDelayed(this,3000);
        }
    };
    protected void onResume(){super.onResume(); refresh.post(update);}
    protected void onPause(){refresh.removeCallbacks(update); super.onPause();}

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        showUi();
        requestNeededPermissions();
        startBridge();
    }

    private void showUi() {
        BridgeConfig.getToken(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(40, 44, 40, 40);
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("Vigilancia del hogar");
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        TextView info = new TextView(this);
        info.setText("\nCentro de mensajes · v0.4\n\nConexion cifrada por Wi-Fi o USB. Recuperacion automatica de los servidores y arranque al reiniciar y desbloquear Android.\n\nMantener conectado al cargador y permitir actividad en segundo plano.\n\nSMS automaticos a tu numero autorizado. RCS de Google Mensajes no dispone de envio automatico desde esta app.");
        info.setTextSize(16);
        root.addView(info, new LinearLayout.LayoutParams(-1, -2));
        live=new TextView(this); live.setTextSize(18); root.addView(live);

        Button restart = new Button(this);
        restart.setText("Recuperar servicio");
        restart.setOnClickListener(v -> { stopService(new Intent(this,MmsBridgeService.class)); startBridge(); });
        root.addView(restart, new LinearLayout.LayoutParams(-1, -2));
        setContentView(root);
    }

    private void requestNeededPermissions() {
        java.util.ArrayList<String> permissions = new java.util.ArrayList<>();
        if (checkSelfPermission(Manifest.permission.RECEIVE_SMS) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.RECEIVE_SMS);
        }
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.SEND_SMS) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.SEND_SMS);
        }
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.READ_PHONE_STATE) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.READ_PHONE_STATE);
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        if (!permissions.isEmpty()) {
            requestPermissions(permissions.toArray(new String[0]), REQUEST_PERMISSIONS);
        }
    }

    private void startBridge() {
        Intent intent = new Intent(this, MmsBridgeService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }
}


