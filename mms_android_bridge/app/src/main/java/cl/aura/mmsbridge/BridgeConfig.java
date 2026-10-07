package cl.aura.mmsbridge;

import android.content.Context;
import android.content.SharedPreferences;

import java.security.SecureRandom;
import java.util.Base64;

public final class BridgeConfig {
    private static final String PREFS = "aura_mms_bridge";
    private static final String TOKEN = "token";

    private BridgeConfig() {}

    public static String getToken(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String existing = prefs.getString(TOKEN, null);
        if (existing != null && !existing.isEmpty()) return existing;
        byte[] bytes = new byte[24];
        new SecureRandom().nextBytes(bytes);
        String generated = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        prefs.edit().putString(TOKEN, generated).apply();
        return generated;
    }
}
