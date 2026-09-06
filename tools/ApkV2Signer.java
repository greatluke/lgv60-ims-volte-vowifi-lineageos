import com.android.apksig.ApkSigner;

import java.io.File;
import java.io.FileInputStream;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Minimal apksig front end used by build_lg_probe.py. */
public final class ApkV2Signer {
    public static void main(String[] args) throws Exception {
        if (args.length != 6) {
            throw new IllegalArgumentException(
                    "usage: ApkV2Signer INPUT OUTPUT KEYSTORE STOREPASS KEYPASS ALIAS");
        }

        char[] storePassword = args[3].toCharArray();
        char[] keyPassword = args[4].toCharArray();
        KeyStore keyStore = KeyStore.getInstance(KeyStore.getDefaultType());
        try (FileInputStream input = new FileInputStream(args[2])) {
            keyStore.load(input, storePassword);
        }

        PrivateKey key = (PrivateKey) keyStore.getKey(args[5], keyPassword);
        List<X509Certificate> certificates = new ArrayList<>();
        for (Certificate certificate : keyStore.getCertificateChain(args[5])) {
            certificates.add((X509Certificate) certificate);
        }
        ApkSigner.SignerConfig signer = new ApkSigner.SignerConfig.Builder(
                args[5], key, certificates).build();

        new ApkSigner.Builder(Collections.singletonList(signer))
                .setInputApk(new File(args[0]))
                .setOutputApk(new File(args[1]))
                // Android 16's system scan explicitly requires a v2 block for
                // this transplanted package. Keep the probe deliberately v2.
                .setMinSdkVersion(24)
                .setV1SigningEnabled(true)
                .setV2SigningEnabled(true)
                .setV3SigningEnabled(false)
                .setV4SigningEnabled(false)
                .build()
                .sign();
    }
}
