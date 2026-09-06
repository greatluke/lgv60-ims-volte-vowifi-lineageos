# Building from source

Produces `lg_substrate.img`, `v60_ims_volte.zip`, `v60_vowifi.zip` from your own LG V60 stock
firmware and a stock LineageOS `system_ext.img`. Nothing proprietary ships in this repo, so the
one‑time patched‑binary step (§3 below) is on you; see
[`PATCH-RECIPES.md`](PATCH-RECIPES.md).

## Build host

- Linux with `python3`, `openjdk`, `e2fsprogs` (`debugfs`, `e2fsck`).
- Android **platform‑tools** (`adb`, `fastboot`) and **build‑tools** (`aapt2`, `zipalign`,
  `apksigner`).
- `payload_dumper` (`pip install payload_dumper`) to pull `system_ext.img` from a LineageOS
  `payload.bin`.
- One‑time, for the patched binaries: **`smali`/`baksmali` 3.0.9** (APK bytecode edits) and an
  **Android NDK** r25+ (the `stroke` client). `tools/patch_ipsecd.py` needs only `python3`.

## Inputs you provide

- **A stock LineageOS `system_ext.img`** for the exact build you run.
- **Your LG V60 stock firmware**: a KDZ for your model (unpack with kdztools), or a `dd` of
  your own device's `system_ext` partition. Used only for local extraction; nothing from it is
  redistributed.

## Steps

```sh
git clone https://github.com/greatluke/lgv60-ims-volte-vowifi-lineageos
cd lgv60-ims-volte-vowifi-lineageos
```

### 0. Signing key (one‑time)

The re‑packed system APKs must be signed with the key whose certificate lands in
`v60_ims_volte`'s `plat_mac_permissions.xml` (that is what grants `com.lge.ims` its
`seinfo=platform`). A project key is provided at `tools/v60ims.jks` (store / key / alias =
`v60ims`). To use your own instead:

```sh
keytool -genkeypair -keystore tools/mykey.jks -storepass p -keypass p \
    -alias k -keyalg RSA -keysize 2048 -validity 10950 -dname "CN=v60 ims, O=you"
```

then pass `--keystore tools/mykey.jks --storepass p --alias k` to
`build_consolidated_modules.py` (it re‑injects the cert).

### 1. Stock LineageOS `system_ext.img`

```sh
python3 -m payload_dumper --partitions system_ext \
    --out out/los lineage-23.x-*-timelm-signed/payload.bin
```

### 2. Extract the LG blobs

```sh
python3 tools/extract_blobs.py \
    --lg-system-ext /path/to/lg-stock/system_ext.img \
    --los-system-ext out/los/system_ext.img \
    --out staging/
```

### 3. Produce the patched binaries (one‑time)

See [`PATCH-RECIPES.md`](PATCH-RECIPES.md) for each:

| Output | Recipe |
|---|---|
| `staging/patched/Ims6.apk` | §1, status‑bar VoLTE indicator, one bytecode patch |
| `staging/patched/lgdataservice.apk` | §2, add a `com.lge.os.PropertyUtils` stub class |
| `staging/patched/QualifiedNetworksService.apk` | §3, `com.android.qns`, three edits |
| `staging/patched/Iwlan.apk` | §3, AOSP `com.google.android.iwlan`, **unmodified**, just obtained |
| `staging/native/stroke` | §4, strongSwan 5.7.1 + `tools/strongswan-5.7.1-lg-stroke.patch`, NDK build |

`ipsecd` is scripted:

```sh
python3 tools/patch_ipsecd.py staging/lg-src/ipsecd staging/native/ipsecd
```

### 4. Build

```sh
python3 tools/build_lg_substrate_image.py  --staging staging/ --out out/lg_substrate.img
python3 tools/build_consolidated_modules.py --staging staging/ --out out/
```

Output: `out/lg_substrate.img`, `out/v60_ims_volte.zip`, `out/v60_vowifi.zip`. Install per the
README.

## After a LineageOS update

The LG substrate is tied to the LineageOS `system_ext` version; the two Magisk modules are not.
For a newer nightly on the same branch, re‑run steps **1 → 2 → 4** against the new
`system_ext.img` and re‑flash `lg_substrate.img`. The modules carry across untouched (the LG
blobs are version‑independent, only the stock base changes). A major LineageOS / Android jump
can also need `v60_ims_volte`'s framework‑coupled pieces (the `Ims6` bytecode patch, LG
`.so`/jars vs the new `framework.jar`) re‑derived.
