package cl.aura.mmsbridge;
import android.content.Context;
import android.os.UserManager;

/** Device-encrypted state permits the bridge to start before the first unlock. */
public final class BootStorage {
    public static synchronized Context context(Context source) {
        Context device=source.createDeviceProtectedStorageContext();
        if(!source.isDeviceProtectedStorage() && source.getSystemService(UserManager.class).isUserUnlocked()) {
            for(String name:new String[]{"aura_mms_bridge","status","operational"}) {
                if(!device.moveSharedPreferencesFrom(source,name))
                    throw new IllegalStateException("Cannot migrate "+name);
            }
            if(!device.moveDatabaseFrom(source,"inbox.db"))
                throw new IllegalStateException("Cannot migrate inbox");
        }
        return device;
    }
}
