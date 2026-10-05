# v60_vowifi changelog

## v0.3
- Fix: `com.android.qns` crash-looped ("Only system can set hide fields in
  SignalThresholdInfo"). `CellularQualityMonitor` set three SignalThresholdInfo fields only
  a system/phone caller may set; our re-signed QNS runs under an app UID. QNS decides which
  network the IMS connection uses, so a boot where it died before its first report never
  brought up IMS at all (no VoLTE until a reboot). The three fields are left at their
  defaults now (the platform enables the thresholds itself), with a try/catch around
  `setSignalStrengthUpdateRequest` as a safety net.
- Magisk in-app updates (`updateJson`).
- Version aligned with v60_ims_volte.

## v0.2
- Second release.

## v0.1
- First release.
