package cl.aura.mmsbridge;
import android.content.Context;
import android.security.keystore.*;
import java.io.FileOutputStream;
import java.math.BigInteger;
import java.security.*;
import java.util.Date;
import javax.net.ssl.*;
import javax.security.auth.x500.X500Principal;

public final class BridgeTls {
    public static SSLServerSocket server(Context c) throws Exception {
        KeyStore ks=KeyStore.getInstance("AndroidKeyStore"); ks.load(null);
        String alias="aura-wifi-ec";
        if(!ks.containsAlias(alias)) {
            KeyPairGenerator gen=KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC,"AndroidKeyStore");
            gen.initialize(new KeyGenParameterSpec.Builder(alias,KeyProperties.PURPOSE_SIGN|KeyProperties.PURPOSE_VERIFY)
                .setAlgorithmParameterSpec(new java.security.spec.ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_NONE,KeyProperties.DIGEST_SHA256,KeyProperties.DIGEST_SHA512)
                .setCertificateSubject(new X500Principal("CN=Aura SMS"))
                .setCertificateSerialNumber(BigInteger.ONE)
                .setCertificateNotBefore(new Date(System.currentTimeMillis()-86400000L))
                .setCertificateNotAfter(new Date(System.currentTimeMillis()+315360000000L)).build());
            gen.generateKeyPair();
        }
        try(FileOutputStream f=c.openFileOutput("bridge-cert.der",0)) { f.write(ks.getCertificate(alias).getEncoded()); }
        KeyManagerFactory km=KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()); km.init(ks,null);
        SSLContext ctx=SSLContext.getInstance("TLS"); ctx.init(km.getKeyManagers(),null,new SecureRandom());
        return (SSLServerSocket)ctx.getServerSocketFactory().createServerSocket(8768,8);
    }
}
