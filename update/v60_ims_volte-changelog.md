# v60_ims_volte changelog

## v0.3
- Vo5G status-bar indicator (shown when voice is on NR) and VoNR enabled: LG's own VoNR
  gate is opened early and CarrierConfig's `vonr_enabled_bool` / `vonr_on_by_default_bool`
  are set at boot.
- Fix: an outgoing call cancelled while it was ringing (ALERTING) never disconnected in
  Telecom, so the call screen stuck. Once a call is past DIALING, AOSP clears `mPendingMO`;
  the start-failed callback that follows a user cancel then fell through a dead end in
  `ImsPhoneCallTracker$8.onCallStartFailed` with no disconnect at all. It now calls
  `sendCallStartFailedDisconnect`.
- Fix: `Ims6.apk` is zipaligned again. Android refuses to parse a priv-app whose
  `resources.arsc` isn't 4-byte aligned; the misalignment stayed hidden on an existing
  install (PackageManager reused its cached parse) and surfaced as a missing `com.lge.ims`
  after a LineageOS update.
- `telephony-common.jar` is zipaligned (ART was extracting `classes.dex` to a temp file on
  every load), and the boot image's stale `boot-telephony-common.{oat,vdex,art}` are hidden
  so processes no longer fall back to "imageless running".
- Magisk in-app updates (`updateJson`).

## v0.2.1
- Persist `editable_enhanced_4g_lte_bool=true` so the Enhanced 4G/VoLTE Settings switch
  is user-editable.

## v0.2
- Fix a rejected-while-ringing incoming VoLTE/VoWiFi call sticking in Telecom
  (`telephony-common.jar` / `ImsPhoneCallTracker` cleanup gap).

## v0.1
- First release.
