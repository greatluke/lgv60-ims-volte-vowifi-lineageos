# v60_ims_volte changelog

## v0.3
- Fix: an outgoing call cancelled while it's still ringing (ALERTING) never disconnected
  in Telecom, leaving the call screen stuck forever. Root cause: once a call progresses
  past DIALING, AOSP's `ImsPhoneCallTracker` clears `mPendingMO`; the async start-failed
  callback that follows a user cancel then fell through a dead end in
  `ImsPhoneCallTracker$8.onCallStartFailed` with no disconnect and no Telecom
  notification at all. Now redirected to `sendCallStartFailedDisconnect`.
- Packaging fix: the repacked telephony-common.jar was never zipaligned, so ART logged
  "please zipalign to 4 bytes" and silently fell back to extracting classes.dex to a
  temp file on every load instead of mmapping it directly. Now zipaligned.
- Packaging fix: replacing telephony-common.jar via the systemless overlay left the
  boot image's stale, checksum-mismatched `boot-telephony-common.{oat,vdex,art}` in
  place, causing every process on the device to log and fall back from "imageless
  running" at startup. The module now hides those stale artifacts for a clean boot.

## v0.2.1
- Persist `editable_enhanced_4g_lte_bool=true` so the Enhanced 4G/VoLTE Settings switch
  is user-editable.

## v0.2
- Fix a rejected-while-ringing incoming VoLTE/VoWiFi call sticking in Telecom
  (`telephony-common.jar` / `ImsPhoneCallTracker` cleanup gap).

## v0.1
- First release.
