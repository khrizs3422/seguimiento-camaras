package cl.aura.mmsbridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(intent.getAction()) || Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction()) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) {
            Intent service = new Intent(context, MmsBridgeService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(service);
            } else {
                context.startService(service);
            }
        }
    }
}
