# Findings

Why this is one image plus two modules, and the non‑obvious blockers behind each fix.

## Why a `system_ext` image and not three modules

The LG IMS/ePDG stack needs several things that `init` and `secilc` consume **before Magisk
mounts its overlays**, so they cannot be delivered by a Magisk module and must be real files in
the `system_ext` partition:

- **`system_ext_sepolicy.cil`**: the LG domain closure (`ipsecd`, `imsipsecclient`,
  `imsipsecstarter`, `lge_ims_phone_provider` context, `vendor_dataservice_app`, `hal_lgdata*`,
  the `lge_ims_*_prop` property types, and all their transitions/allows). `magiskpolicy` can add
  individual rules to the *loaded* policy but cannot define a domain closure with type
  transitions. Removing `system_ext_sepolicy_and_mapping.sha256` forces `secilc` to recompile
  the CIL at boot so the grafted policy is actually merged; keeping the stale hash makes the
  loader use the precompiled policy and silently ignore the new CIL.
- **`system_ext_property_contexts`**: maps `persist.net.wo.*`, `product.lge.data.*`,
  `net.iwlan_possible`, … to the LG property types. Read by `init` at second stage. Without it
  those properties fall to `default_prop`, `vendor_dataservice_app` cannot set them, and the LG
  data service crashes on start (`failed to set system property "persist.net.wo.key.display"`).
- **`system_ext_service_contexts`**: must contain `com.lge.ims.phone u:object_r:ims_service:s0`
  so `lge_ims_phone_provider` (running as `radio`) can register its binder service; otherwise it
  hits `avc: denied { add } … default_android_service` and crash‑loops, which then trips
  RescueParty into disabling the Magisk module.
- **`system_ext_file_contexts`**: labels the LG `/bin` daemons (`ipsecd_exec`,
  `imsipsecstarter_exec`, …).
- **`etc/init/*.rc`**: the LG service definitions (`ipsecd`, `imsipsec*`,
  `lge_ims_phone_provider`) with their declared sockets and `seclabel`. `init` parses partition
  `.rc` files at second stage. A module‑overlaid `.rc` (even via `overlay.d`) is fragile;
  `class core`/`class main` services also start before Magisk magic‑mount makes their binaries
  visible.

The **native daemons and libs** referenced by those `.rc` files (`lge_ims_phone_provider`,
`imsipsecclient`, `imsipsecstarter`, `ipsecd`, `starter`, `charon`, `ipsec`; `libcharon.so`,
`libstrongswan.so`, `libsimaka.so`, `LGDataFeature.so`, `libLgeProductProperties.so`,
`vendor.lge.hardware.property@2.0.so`, `libpatchcodeid.so`) and `/etc/ipsec/` are grafted into
the same image simply because that is where `init` expects them. They are LG‑stock and do not
change between LineageOS builds, only the stock base of the image does.

Everything at the **application layer** (`Ims6.apk`, the LG data service, LG framework jars, the
`seinfo` mapping, `seapp`/`property` context *overlays* re‑derived from the ROM at install, the
patched AOSP QNS/iwlan APKs, `andsf.xml`, and the runtime CarrierConfig override) *is* delivered
by the two Magisk modules and can be iterated without re‑flashing.

## Blockers behind the two modules

### VoLTE (`v60_ims_volte`)

- **`product.lge.data.server` ships `android:enabled="false"`.** On a fresh `/data` it stays
  disabled, so no IMS PDN is brought up, LG's registration engine never becomes ready, and
  MmTel never registers. The module's `service.sh` runs `pm enable product.lge.data.server` on
  every boot (idempotent).
- **IMS‑AKA IPsec fails: `Activating IPSecClient failed - SPADD`.** `imsipsecclient` sets the
  Gm‑interface security policy and replies to `com.lge.ims` over a unix datagram socket. The
  grafted CIL grants that path for `imsipsecstarter` but not for `imsipsecclient`, and
  `imsipsecclient` (label `s0`) writing to `com.lge.ims` (label `s0:c…`) also needs
  `mlstrustedsubject`. Adding `(typeattributeset mlstrustedsubject (imsipsecclient))` to the CIL
  **fails the boot‑time `secilc` compile** (a `neverallow`), causing a boot loop. The module
  instead applies the three rules with `magiskpolicy --live` from `post-fs-data.sh`, which
  patches the loaded binary policy and skips the `neverallow` check.
- **`com.lge.ims` needs `seinfo=platform`** to land in `vendor_qtelephony`. That mapping (in
  `plat_mac_permissions.xml`) is keyed on the certificate of the key used to re‑sign the
  re‑packed system APKs, hence the single signing key.
- The LG data service also references a class (`com.lge.os.PropertyUtils`) that is not present
  on LineageOS; a tiny pass‑through stub is grafted into its secondary dex.
- **Rejecting a ringing incoming IMS call leaves it stuck in Telecom.** LG's IMS reports a
  locally rejected *incoming* call through `ImsCall.Listener.onCallStartFailed()`, the callback
  AOSP uses for a failed *outgoing* call. `ImsPhoneCallTracker.onCallStartFailed` only cleans up
  a pending MO connection, so the incoming `ImsPhoneConnection` never disconnects, the ringing
  screen sticks, and `VerifyCallStateChangeTransaction` times out. `v60_ims_volte` overlays a
  `telephony-common.jar` that adds the incoming-connection cleanup (`docs/PATCH-RECIPES.md` §6).
  Answered-then-hung-up and outgoing calls were always fine; this is reject-while-ringing only,
  and it affects VoLTE and VoWiFi identically (same callback).

### VoWiFi (`v60_vowifi`)

- **`com.android.qns` decides IWLAN eligibility, not LG.** LG's own `QualifiedNetworksProvider`
  binds but never calls `reportQualifiedNetworks` (its adaptor throws
  `Unrecognized alarm listener`). So `com.android.qns` must be the bound QNS, wired via the
  CarrierConfig `carrier_qualified_networks_service_package_override_string` override.
- **`com.android.qns` needs `android.permission.MODIFY_PHONE_STATE`.** Without it,
  `QnsProvisioningListener.registerProvisioningCallback` throws `SecurityException`, so
  `IwlanNetworkStatusTracker` never sets `iwlanEnable=true`, so QNS never reports IWLAN
  qualified, so the framework never triggers the ePDG handover. The permission is added to the
  APK manifest (binary AXML insertion) and to a `privapp-permissions` allowlist. It is not an
  appop, so it cannot be granted at runtime.
- **`com.android.qns`'s `WifiQualityMonitor` and the cell↔Wi-Fi handover speed.**
  `registerCallback` builds a `NetworkRequest` with an RSSI threshold that needs
  `NETWORK_SIGNAL_STRENGTH_WAKEUP`; without it `registerNetworkCallback` throws
  `SecurityException` and the matching `unregisterCallback` then throws
  `IllegalArgumentException`. Wrapping both in `try/catch` stops the crash but leaves QNS
  reading Wi-Fi quality only from the throttled `RSSI_CHANGED` path, so a cell→Wi-Fi handover
  waits minutes for a coarse `WIFI_QUALITY_CHANGED` tick. Granting
  `NETWORK_SIGNAL_STRENGTH_WAKEUP` (manifest + privapp allowlist) lets the threshold callback
  register; QNS then commits the handover ~1-2 s after IWLAN becomes available. The try/catch
  stays as a safety net. The residual few seconds on cell→Wi-Fi is ePDG tunnel bring-up, not
  QNS. QNS-internal timers (`qns.*` carrier-config keys) can't be tuned at runtime
  (`cmd phone cc` rejects unregistered keys), so they'd need a CarrierConfig APK or a QNS
  bytecode change, which isn't worth it.
- **`ipsecd` null‑derefs a missing HAL.** At `IPSEC_CONNECTED` it calls into
  `vendor.lge.hardware.property::IProperty`, whose implementation is absent on LineageOS (only
  the interface stub ships). Two byte patches (`docs/PATCH-RECIPES.md` §5, scripted in
  `tools/patch_ipsecd.py`): the getter's prologue becomes a tail‑call to libcutils
  `property_get`, the setter's becomes `ret`. Reusable pattern: **when a LineageOS vendor‑blob
  port is missing a vendor HAL, binary‑patch the client to fall back to a libcutils
  equivalent** rather than rebuilding the HAL.
- **The `stroke` client's wire ABI is patched by LG.** Upstream `stroke_msg_t` / `stroke_end_t`
  don't match; `charon` rejects the message with `invalid stroke message length`. The fix is a
  small source patch appending the LG fields (see
  `tools/strongswan-5.7.1-lg-stroke.patch`), rebuilt with the NDK.
- **The CarrierConfig WLAN override is applied at runtime**, not by re‑signing
  `CarrierConfig.apk`. A re‑signed system APK whose signature differs from `packages.xml` is not
  re‑parsed without wiping caches, and it collides with the shared‑UID group. `cmd phone cc
  set-values-from-xml -p` sets the same keys persistently with no signature exposure.

## Reusable notes

- `debugfs` image edits must preserve `security.selinux`: `ea_get -f` → `rm` → `write` →
  `set_inode_field mode/uid/gid` → `ea_set -f` → verify by byte compare.
- Magisk's boot‑time `sepolicy.rule` loader silently drops `allow` lines that duplicate an
  existing `{domain,type,class}` triple *and everything after them*. Apply fragile rules via
  `magiskpolicy --live` from `post-fs-data.sh` instead.
- LG's `init` pets a hardware watchdog on a deadline; a slow first boot (fsck + policy
  recompile) can miss it and freeze at the logo. The next boot is fine. Shipping a matching
  precompiled policy would remove the recompile entirely.
