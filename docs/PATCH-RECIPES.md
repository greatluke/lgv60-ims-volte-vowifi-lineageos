# Patch recipes

The two Magisk modules bundle patched binaries that cannot be redistributed (LG / Google
proprietary code), so `build_consolidated_modules.py` and `build_lg_substrate_image.py` consume
copies you produce yourself and drop into the staging tree:

| File | Where it goes | This doc |
|---|---|---|
| `Ims6.apk` (bytecode patch) | `staging/patched/` | §1 |
| `lgdataservice.apk` (add a stub class) | `staging/patched/` | §2 |
| `QualifiedNetworksService.apk` (`com.android.qns`, 3 edits) | `staging/patched/` | §3 |
| `Iwlan.apk` (`com.google.android.iwlan`, **unmodified**, just obtained) | `staging/patched/` | §3 |
| `stroke` (strongSwan client, source patch + NDK build) | `staging/native/` | §4 |
| `ipsecd` (2 byte patches) | `staging/native/` | §5 |

`privapp-permissions-v60-aosp-iwlan.xml` is **not** staged; the base allowlist ships as
`tools/privapp-permissions-v60-aosp-iwlan.xml` and the build script adds `MODIFY_PHONE_STATE`
to it (§3c).

## Toolchain

- `smali`/`baksmali` **3.0.9** (`com.android.tools.smali.*`). The v2.5.2 in most distros
  rejects modern dex. Run with `guava` and `jcommander` **1.64** on the classpath.
  Assemble with `--api 23` (produces dex `035`, which these APKs use).
- Android build‑tools: `aapt2`, `zipalign`, `apksigner`.
- `tools/axml_patch.py` for binary‑AndroidManifest edits.
- `tools/ApkV2Signer.java`: compile against `apksigner.jar`; signs v1+v2, no v3/v4:
  `java -cp <classes>:<apksigner.jar> ApkV2Signer IN OUT KEYSTORE STOREPASS KEYPASS ALIAS`

For every APK below: unpack, patch `classes.dex` and/or `AndroidManifest.xml`, repack **without**
`META-INF/`, `zipalign -p 4`, then re‑sign with the project key (`tools/v60ims.jks`,
store/key/alias `v60ims`), the same key whose certificate is in
`plat_mac_permissions.xml`.

---

## 1. `Ims6.apk`: status‑bar VoLTE indicator

Source: `system_ext/priv-app/Ims6/Ims6.apk` from LG stock.

`NotificationService.showVoLTEIndicator()` looks the indicator drawable up by a name that lives
in LG's SystemUI (absent on LineageOS); the lookup returns `0` and the status‑bar slot stays
blank. The APK already bundles `drawable/regi_volte` (`0x7f04000b`). Patch: in
`showVoLTEIndicator`, right after the `getResourceId(...)` `move-result v1`, insert

```smali
if-gtz v1, :v60_icon_ok
const v1, 0x7f04000b
:v60_icon_ok
```

Only `classes.dex` changes; keep `classes2.dex`.

---

## 2. `lgdataservice.apk`: `PropertyUtils` stub

Source: `system_ext/priv-app/lgdataservice/lgdataservice.apk` from LG stock.

`IwlanDataService.setupDataCall()` → `getIpv6GlobalIIDAddress` references
`com.lge.os.PropertyUtils`, which is not on LineageOS →
`NoClassDefFoundError` and the ePDG data call never starts. Add a trivial pass‑through stub as a
new class in the APK's secondary dex:

```smali
.class public final Lcom/lge/os/PropertyUtils;
.super Ljava/lang/Object;
.field private static final INSTANCE:Lcom/lge/os/PropertyUtils;
.method static constructor <clinit>()V
    .registers 1
    new-instance v0, Lcom/lge/os/PropertyUtils;
    invoke-direct {v0}, Lcom/lge/os/PropertyUtils;-><init>()V
    sput-object v0, Lcom/lge/os/PropertyUtils;->INSTANCE:Lcom/lge/os/PropertyUtils;
    return-void
.end method
.method private constructor <init>()V
    .registers 1
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V
    return-void
.end method
.method public static getInstance()Lcom/lge/os/PropertyUtils;
    .registers 1
    sget-object v0, Lcom/lge/os/PropertyUtils;->INSTANCE:Lcom/lge/os/PropertyUtils;
    return-object v0
.end method
.method public get(ILjava/lang/String;)Ljava/lang/String;
    .registers 3
    return-object p2
.end method
.method public getBoolean(IZ)Z
    .registers 3
    return p2
.end method
.method public getInt(II)I
    .registers 3
    return p2
.end method
.method public set(ILjava/lang/String;)V
    .registers 3
    return-void
.end method
```

Assemble it into a `classes2.dex` and keep the original `classes.dex` unchanged (so the modem/
ISIM/APN behaviour is untouched). `get(int, String)` returning its String argument is the
identity the caller expects (it passes the default in).

---

## 3. `QualifiedNetworksService.apk` (`com.android.qns`) + `Iwlan.apk`

### Where to get them

Both are AOSP packages, not LG's:

- `com.android.qns` → AOSP `packages/services/QualifiedNetworks` (module `QualifiedNetworksService`)
- `com.google.android.iwlan` → AOSP `packages/services/Iwlan` (module `Iwlan`)

Two ways:

1. **Pull prebuilts from an Android 16 system image**: a Pixel factory image or a GSI. They
   live at `system_ext/priv-app/QualifiedNetworksService/QualifiedNetworksService.apk` and
   `system_ext/priv-app/Iwlan/Iwlan.apk` (unpack `system_ext.img` with `debugfs` /
   `simg2img` + `mount`). This project was validated with `com.android.qns` `versionCode` 36.
2. **Build from AOSP**: sync the `android-16.0.0_rXX` branch (or a matching AOSP tag), then
   `m QualifiedNetworksService Iwlan`; the APKs land in
   `out/target/product/*/system_ext/priv-app/`.

`Iwlan.apk` is used **as‑is** (no patch). Only `QualifiedNetworksService.apk` gets the three
edits below.

### The three edits

**a. `WifiQualityMonitor.registerCallback`, try/catch.** It builds a `NetworkRequest` with an
RSSI threshold; `ConnectivityService.ensureSufficientPermissionsForRequest` rejects it
(`SecurityException`, needs `NETWORK_SIGNAL_STRENGTH_WAKEUP`). Wrap the
`registerNetworkCallback` invoke:

```smali
:try_start_pv
invoke-virtual {v0, v1, v2}, Landroid/net/ConnectivityManager;->registerNetworkCallback(Landroid/net/NetworkRequest;Landroid/net/ConnectivityManager$NetworkCallback;)V
:try_end_pv
.catch Ljava/lang/Throwable; {:try_start_pv .. :try_end_pv} :catch_pv
:goto_after_pv
    # ... fall through to initWifiQualityNetworkCallback + registerSystemDefaultNetworkCallback
:catch_pv
    move-exception v0
    goto :goto_after_pv
```

**b. `WifiQualityMonitor.unregisterCallback`, try/catch.** After (a), the threshold callback
was never registered, so `unregisterNetworkCallback` throws
`IllegalArgumentException: NetworkCallback was not registered`. Wrap the two
`unregisterNetworkCallback` invokes in one `try/catch(Throwable)` that falls through to the
`mIsRegistered = false` cleanup.

**c. Add `android.permission.MODIFY_PHONE_STATE`.** Required for
`QnsProvisioningListener.registerProvisioningCallback`; without it `iwlanEnable` stays false and
QNS never reports IWLAN qualified. It is not an appop, so it must be in the manifest:

```python
from axml_patch import AXML
a = AXML(bytearray(open("AndroidManifest.xml","rb").read()))
idx = a.append_string("android.permission.MODIFY_PHONE_STATE")
a.duplicate_uses_permission("uses-permission", idx)
open("AndroidManifest.xml","wb").write(a.data)
```

The allowlist side is handled for you: `build_consolidated_modules.py` reads
`tools/privapp-permissions-v60-aosp-iwlan.xml` (the base list of privileged permissions the two
packages declare) and inserts `<permission name="android.permission.MODIFY_PHONE_STATE"/>` into
the `com.android.qns` block as it builds the module.

> `com.android.qns` in some builds also references a Pixel‑only RIL wrapper
> (`com.google.android.gril.*`) in `GoogleQnsManager`'s constructor, throwing
> `NoClassDefFoundError`. If your copy does, wrap the `new GoogleQnsManager(...)` site in
> `QnsComponents.createQnsComponents()` in a narrow `try/catch(Throwable)` that stores `null`.

---

## 4. `stroke`: strongSwan client ABI

Not an APK. The unpatched client makes LG's `charon` reject every message with
`invalid stroke message length`, because LG extended `stroke_end_t` with extra trailing fields.

1. Fetch a clean **strongSwan 5.7.1** tree (`strongswan-5.7.1.tar.bz2` from strongswan.org).
2. `patch -p1 < tools/strongswan-5.7.1-lg-stroke.patch` (adds the LG fields to
   `src/stroke/stroke_msg.h`).
3. Cross-compile for `aarch64` with an Android NDK (r25+). A standalone build of just the
   client is enough, you only need `src/stroke/{stroke.c,stroke_msg.c,stroke_keywords.c}`
   linked against strongSwan's `library.c` bits, but the simplest reproducible route is a full
   configure of the tree with the NDK toolchain and then taking `src/stroke/.libs/stroke`:

   ```sh
   export NDK=/path/to/android-ndk
   export TC=$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin
   export CC=$TC/aarch64-linux-android28-clang
   ./configure --host=aarch64-linux-android --disable-defaults \
       --enable-stroke --enable-kernel-netlink --enable-socket-default \
       --enable-openssl CFLAGS="-Os -fPIE" LDFLAGS="-pie"
   make -C src/libstrongswan && make -C src/stroke
   cp src/stroke/.libs/stroke <out>
   ```

   (`stroke_keywords.c` is generated by `gperf` during the build.)
4. Place the binary at `staging/native/stroke`. Its SELinux label is Magisk's default
   `system_file`, which is what `charon` expects for a client on the unix socket.

---

## 5. `ipsecd`: missing `vendor.lge.hardware.property` HAL

Source: `system_ext/bin/ipsecd` from LG stock (the LineageOS‑era build; `file` reports
"for Android 33"). Reference binary this recipe was derived from:
md5 `52393430f8e5c3cac9fe04e1680117fe`.

LineageOS ships only the **interface** stub for `vendor.lge.hardware.property@2.0`, no
implementation. Inside `ipsecd`, `IProperty::getService()` therefore returns `null` and the
daemon segfaults at `IPSEC_CONNECTED`. `ipsecd` has exactly two functions that reach that HAL,
a property **getter** and a property **setter**, each identified by its single cross‑reference:
a `bl` to the `IProperty::getService@plt` thunk. `property_get` / `property_set` (libcutils) are
already imported by `ipsecd` (PLT entries exist), so no relocation work is needed.

- **Setter** → overwrite its first instruction (`paciasp`) with `ret`. Property writes from
  `ipsecd` become no‑ops (nothing on LineageOS consumes them).
- **Getter** → overwrite its prologue with a tail‑call to libcutils `property_get`:

  ```asm
  mov  x0, x2              ; e0 03 02 aa   (key was the 3rd arg)
  mov  x2, xzr             ; e2 03 1f aa   (default_value = NULL)
  b    property_get@plt    ; 14xxxxxx      (imm26 = (plt - here) >> 2)
  ```

  Distinguish getter from setter structurally: the getter's body calls
  `android::hardware::hidl_string::c_str()` and copies the result out (LG's own code even has a
  `property_get` fallback path inside it); the setter's body has neither.

### For the reference binary, the patch is two literal edits

| File offset | Stock bytes | Patched bytes | Meaning |
|---|---|---|---|
| `0x13854` | `3f 23 03 d5` | `c0 03 5f d6` | setter → `ret` |
| `0x13a00` | `3f 23 03 d5 ff 83 03 d1 fd 7b 0a a9` | `e0 03 02 aa e2 03 1f aa 4e 28 00 14` | getter → `mov x0,x2 / mov x2,xzr / b property_get@plt` |

`.text` file offset equals vaddr for this binary (`.text` at `0xc000`/`0xc000`), and
`property_get@plt` is at `0x1db40` (so `b` at `0x13a08`: `(0x1db40-0x13a08)>>2 = 0x284e`).

### Or run the helper

```sh
python3 tools/patch_ipsecd.py  staging/lg-src/ipsecd  staging/native/ipsecd
```

`tools/patch_ipsecd.py` walks the ELF, locates the two `IProperty::getService` call sites,
tells getter from setter by the markers above, and applies the same edits, recomputing offsets
so it also works on a differently‑built `ipsecd`. It bails out rather than guess if the shape
doesn't match.

> The substrate image keeps the **stock** `ipsecd` (it is what `init` starts first); the
> `v60_vowifi` module overlays this patched copy. Both must exist.
