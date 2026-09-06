#!/usr/bin/env python3
"""Build the two Magisk modules: v60_ims_volte and v60_vowifi.

Inputs (from extract_blobs.py + docs/PATCH-RECIPES.md):
  staging/lg-app/           LG app-layer blobs (jars, libs, permission xml,
                            plat_mac_permissions.xml, ike/mapcon conf)
  staging/patched/          Ims6.apk, lgdataservice.apk,
                            QualifiedNetworksService.apk, Iwlan.apk,
                            privapp-permissions-v60-aosp-iwlan.xml
  staging/native/           stroke, ipsecd  (ABI-fixed / HAL-patched)
  tools/andsf.xml           PLMN -> WLAN routing (edit its PLMN for your carrier)
  --keystore                key that signs the re-packed APKs; its certificate is
                            injected into plat_mac_permissions.xml as seinfo=platform
                            (default: tools/v60ims.jks, store/key/alias v60ims)

Usage:  python3 build_consolidated_modules.py --staging staging/ --out out/
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMS_VOLTE_VER = VOWIFI_VER = "v0.1"

# ---------------------------------------------------------------- module.prop
IMS_VOLTE_PROP = f"""id=v60_ims_volte
name=V60 IMS + VoLTE
version={IMS_VOLTE_VER}
versionCode=1
author=greatluke
description=LG IMS application layer for LineageOS on the LG V60: com.lge.ims with a status-bar VoLTE indicator fix, the LG data service with a com.lge.os.PropertyUtils stub, LG framework jars/libs, platform seinfo for the signing key, seapp/property_contexts overlays rebuilt from the live ROM at install, and a boot service that enables the LG data service and selects com.lge.ims. Requires the LG substrate system_ext image. Gives VoLTE; VoWiFi is a separate module (v60_vowifi).
"""

VOWIFI_PROP = f"""id=v60_vowifi
name=V60 VoWiFi (LG ePDG / IPsec)
version={VOWIFI_VER}
versionCode=1
author=greatluke
description=Adds Wi-Fi Calling on top of v60_ims_volte: the ABI-fixed strongSwan stroke client, an ipsecd HAL null-fix, the ipsecd/charon SELinux grants, AOSP com.android.qns + com.google.android.iwlan (WifiQualityMonitor crash fixes + MODIFY_PHONE_STATE), andsf.xml, and the CarrierConfig WLAN-service override applied at runtime via cmd phone cc. REQUIRES v60_ims_volte and the LG substrate image. Then enable Wi-Fi Calling in Settings.
"""

# ---------------------------------------------------------------- v60_ims_volte scripts
IMS_SEPOLICY_RULE = """\
allow vendor_qtelephony imsipsecstarter unix_dgram_socket { sendto }
typeattribute imsipsecstarter mlstrustedsubject
allow imsipsecstarter property_socket sock_file { write }
allow imsipsecstarter init unix_stream_socket { connectto }
allow imsipsecstarter ctl_start_prop property_service { set }
allow imsipsecstarter ctl_stop_prop property_service { set }
allow imsipsecclient vendor_qtelephony unix_dgram_socket { sendto }
typeattribute imsipsecclient mlstrustedsubject
allow vendor_qtelephony imsipsecclient unix_dgram_socket { sendto }
allow vendor_qtelephony vendor_lge_misc_prop file { read getattr map open }
allow vendor_qtelephony lge_ims_data_prop file { read getattr map open }
allow vendor_qtelephony system_data_file dir { getattr search }
allow vendor_dataservice_app vendor_lge_misc_prop file { read getattr map open }
allow vendor_dataservice_app hal_lgdata_hwservice hwservice_manager { find }
allow vendor_dataservice_app hal_lgdata_default binder { call transfer }
allow hal_lgdata_default vendor_dataservice_app binder { call transfer }
allow vendor_dataservice_app hal_lgdata_default fd { use }
allow hal_lgdata_default vendor_dataservice_app fd { use }
allow hal_lgdata_default hal_lgdata_default qipcrtr_socket { getattr read write setopt }
"""

IMS_POST_FS_DATA = """\
#!/system/bin/sh
if command -v resetprop >/dev/null 2>&1; then
    resetprop persist.product.lge.ims.volte_open 1
elif [ -x /data/adb/magisk/resetprop ]; then
    /data/adb/magisk/resetprop persist.product.lge.ims.volte_open 1
fi

[ "$(getprop init.svc.ipsecd)" = "running" ] || start ipsecd >/dev/null 2>&1

DIR=/data/user_de/0/product.lge.data.server
if [ -d "$DIR" ]; then
    L=$(ls -Zd "$DIR" 2>/dev/null | awk '{print $1}' | sed 's/:app_data_file:/:radio_data_file:/')
    case "$L" in *:radio_data_file:*) chcon -R "$L" "$DIR" 2>/dev/null ;; esac
fi

# imsipsecclient <-> com.lge.ims IPsec bridge: the MLS-attribute grant fails the
# boot-time secilc compile if placed in the substrate CIL, so apply it live here.
magiskpolicy --live "typeattribute imsipsecclient mlstrustedsubject" 2>/dev/null
magiskpolicy --live "allow imsipsecclient vendor_qtelephony unix_dgram_socket { sendto }" 2>/dev/null
magiskpolicy --live "allow vendor_qtelephony imsipsecclient unix_dgram_socket { sendto }" 2>/dev/null
"""

IMS_SERVICE = """\
#!/system/bin/sh
pm grant --user 0 com.lge.ims android.permission.ACCESS_COARSE_LOCATION 2>/dev/null
pm grant --user 0 com.lge.ims android.permission.ACCESS_FINE_LOCATION 2>/dev/null
pm grant --user 0 com.lge.ims android.permission.ACCESS_BACKGROUND_LOCATION 2>/dev/null

( i=0; while [ "$(getprop sys.boot_completed)" != "1" ] && [ "$i" -lt 90 ]; do sleep 2; i=$((i+1)); done
  # LG ships product.lge.data.server as android:enabled=false; and PackageWatchdog
  # can disable com.lge.ims if it crash-loops during boot churn. Re-enable both
  # every boot (idempotent) so IMS self-heals.
  pm enable product.lge.data.server >/dev/null 2>&1
  pm enable com.lge.ims >/dev/null 2>&1
  # IMS-service selection is runtime state on this build
  n=0; while [ "$n" -lt 60 ]; do
    su -c 'cmd phone ims set-ims-service -s 0 -c -f 1 com.lge.ims' >/dev/null 2>&1 && break
    sleep 2; n=$((n+1))
  done ) &
"""

# customize.sh: rebuild the seapp/property context overlays from the ROM's *live*
# files (so this stays correct across LineageOS updates without duplicate entries)
IMS_CUSTOMIZE = """\
#!/system/bin/sh
ui_print "- V60 IMS + VoLTE"

SEAPP=""
for c in /system/etc/selinux/plat_seapp_contexts \\
         /system_ext/etc/selinux/system_ext_seapp_contexts \\
         /vendor/etc/selinux/vendor_seapp_contexts; do
    [ -f "$c" ] && grep -q 'name=org.codeaurora.ims' "$c" && { SEAPP="$c"; break; }
done
[ -n "$SEAPP" ] || abort "! could not find the org.codeaurora.ims seapp mapping"
case "$SEAPP" in
    /system/*)     DST="$MODPATH/system/${SEAPP#/system/}" ;;
    /system_ext/*) DST="$MODPATH/system/system_ext/${SEAPP#/system_ext/}" ;;
    /vendor/*)     DST="$MODPATH/system/vendor/${SEAPP#/vendor/}" ;;
esac
mkdir -p "${DST%/*}"
sed -e '/name=com[.]lge[.]ims/d' -e '/name=product[.]lge[.]data[.]server/d' \\
    -e '/name=[.]lgedataservice/d' "$SEAPP" > "$DST"
grep 'name=org.codeaurora.ims' "$SEAPP" | head -n1 \\
  | sed -e 's/name=org.codeaurora.ims/name=com.lge.ims/' -e 's/levelFrom=none$/levelFrom=all/' >> "$DST"
printf '%s\\n' 'user=_app seinfo=platform name=.lgedataservice domain=vendor_dataservice_app type=radio_data_file levelFrom=all' >> "$DST"

PSRC=/system_ext/etc/selinux/system_ext_property_contexts
PDST="$MODPATH/system/system_ext/etc/selinux/system_ext_property_contexts"
[ -f "$PSRC" ] || abort "! could not find system_ext_property_contexts"
mkdir -p "${PDST%/*}"
sed -e '/^persist[.]net[.]wo[.]/d' -e '/^persist[.]product[.]lge[.]data[.]/d' \\
    -e '/^persist[.]product[.]lge[.]sar[.]ratpreference[[:space:]]/d' \\
    -e '/^net[.]iwlan_possible[[:space:]]/d' -e '/^product[.]lge[.]data[.]iwlan[.]/d' \\
    -e '/^product[.]lge[.]ims[.]reg[[:space:]]/d' \\
    -e '/^net[.]wo[.]apntypes_in_epdgonlyapn[[:space:]]/d' \\
    -e '/^product[.]lge[.]data[.]iwlan[.]hosupport[.]ims[[:space:]]/d' \\
    -e '/^product[.]lge[.]data[.]iwlan[.]hosupport[.]roam[.]ims[[:space:]]/d' \\
    -e '/^product[.]lge[.]data[.]imscalltype[[:space:]]/d' "$PSRC" > "$PDST"
cat >> "$PDST" <<'EOF'
persist.net.wo.                         u:object_r:lge_ims_wo_prop:s0
persist.product.lge.data.             u:object_r:lge_ims_data_prop:s0
persist.product.lge.sar.ratpreference     u:object_r:lge_ims_sar_ratpreference_prop:s0 exact int
net.iwlan_possible                     u:object_r:lge_ims_iwlan_possible_prop:s0 exact int
product.lge.data.iwlan.                 u:object_r:lge_ims_data_prop:s0
product.lge.ims.reg                 u:object_r:lge_ims_reg_prop:s0 exact int
net.wo.apntypes_in_epdgonlyapn       u:object_r:lge_wo_apntypes_prop:s0 exact string
product.lge.data.iwlan.hosupport.ims u:object_r:lge_iwlan_hosupport_prop:s0 exact int
product.lge.data.iwlan.hosupport.roam.ims u:object_r:lge_iwlan_hosupport_roam_prop:s0 exact int
product.lge.data.imscalltype     u:object_r:lge_ims_calltype_prop:s0 exact int
EOF

set_perm_recursive "$MODPATH/system" 0 0 0755 0644
ui_print "- LG IMS overlays + property/seapp contexts installed"
"""

# ---------------------------------------------------------------- v60_vowifi scripts
VOWIFI_SEPOLICY = """\
allow ipsecd lge_ims_wo_prop file { read getattr map open }
allow ipsecd lge_ims_data_prop file { read getattr map open }
allow ipsecd vendor_default_prop file { read getattr map open }
allow ipsecd vendor_lge_misc_prop file { read getattr map open }
allow ipsecd hwservicemanager_prop file { read getattr map open }
allow ipsecd default_android_hwservice hwservice_manager find
allow ipsecd ipsecd_socket unix_stream_socket connectto
"""

VOWIFI_POST_FS_DATA = """\
#!/system/bin/sh
MODDIR=${0%/*}
for p in /system_ext/bin/ipsecd "$MODDIR/system/system_ext/bin/ipsecd"; do
  [ -e "$p" ] && chcon u:object_r:ipsecd_exec:s0 "$p" 2>/dev/null
done
magiskpolicy --live "allow ipsecd system_prop property_service set" 2>/dev/null
magiskpolicy --live "allow ipsecd hwservicemanager binder { call transfer }" 2>/dev/null
magiskpolicy --live "allow ipsecd servicemanager binder { call transfer }" 2>/dev/null
# PackageManager caches its APK parse in /data/system/package_cache keyed on
# path+size+mtime; a re-signed priv-app APK that keeps the same versionCode and
# size (very common here) is NOT re-parsed on the next boot, so newly added
# manifest permissions are ignored. Bust the cache whenever this module's
# QualifiedNetworksService.apk changes (md5), not just once.
QNS_APK="$MODDIR/system/system_ext/priv-app/QualifiedNetworksService/QualifiedNetworksService.apk"
QNS_MD5=$(md5sum "$QNS_APK" 2>/dev/null | cut -d' ' -f1)
if [ "$QNS_MD5" != "$(cat "$MODDIR/.qns_cache_stamp" 2>/dev/null)" ]; then
  rm -rf /data/system/package_cache/* 2>/dev/null
  rm -f /data/user_de/0/com.android.phone/files/carrierconfig-*.xml 2>/dev/null
  echo "$QNS_MD5" > "$MODDIR/.qns_cache_stamp"
fi
setprop ctl.restart ipsecd 2>/dev/null
"""

VOWIFI_SERVICE = """\
#!/system/bin/sh
MODDIR=${0%/*}
magiskpolicy --live "allow ipsecd system_prop property_service set" 2>/dev/null
magiskpolicy --live "allow ipsecd hwservicemanager binder { call transfer }" 2>/dev/null
magiskpolicy --live "allow ipsecd servicemanager binder { call transfer }" 2>/dev/null
( i=0; while [ "$(getprop sys.boot_completed)" != "1" ] && [ "$i" -lt 60 ]; do sleep 2; i=$((i+1)); done
  [ "$(getprop sys.boot_completed)" = "1" ] || exit 0
  sleep 8
  if ! cmd phone cc set-values-from-xml -p < "$MODDIR/carrier_wlan_override.xml" 2>/dev/null; then
    cmd phone cc set-value -p carrier_data_service_wlan_package_override_string product.lge.data.server
    cmd phone cc set-value -p carrier_network_service_wlan_package_override_string product.lge.data.server
    cmd phone cc set-value -p carrier_qualified_networks_service_package_override_string com.android.qns
    cmd phone cc set-value -p carrier_volte_available_bool true
    cmd phone cc set-value -p carrier_wfc_ims_available_bool true
    cmd phone cc set-value -p iwlan.supported_integrity_algorithms_int_array 2 12 13 14
  fi
  [ "$(settings get global wfc_ims_enabled)" = "null" ] && settings put global wfc_ims_enabled 1
  [ "$(settings get global wfc_ims_mode)"    = "null" ] && settings put global wfc_ims_mode 2
  [ "$(settings get global volte_vt_enabled)" = "null" ] && settings put global volte_vt_enabled 1 ) &
"""

VOWIFI_CC_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<carrier_config>
    <string name="carrier_data_service_wlan_package_override_string">product.lge.data.server</string>
    <string name="carrier_network_service_wlan_package_override_string">product.lge.data.server</string>
    <string name="carrier_qualified_networks_service_package_override_string">com.android.qns</string>
    <boolean name="carrier_volte_available_bool" value="true"/>
    <boolean name="carrier_wfc_ims_available_bool" value="true"/>
    <int-array name="iwlan.supported_integrity_algorithms_int_array" num="4">
        <item value="2"/><item value="12"/><item value="13"/><item value="14"/>
    </int-array>
</carrier_config>
"""


def zipdir(tree: Path, dest: Path) -> None:
    dest.unlink(missing_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(tree.rglob("*")):
            if f.is_file():
                zi = zipfile.ZipInfo(str(f.relative_to(tree)), (2026, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = (0o755 if f.suffix == ".sh" else 0o644) << 16
                z.writestr(zi, f.read_bytes())
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")


def cert_der_hex(keystore: Path, storepass: str, alias: str) -> str:
    p = subprocess.run(["keytool", "-exportcert", "-keystore", str(keystore),
                        "-storepass", storepass, "-alias", alias, "-rfc"],
                       capture_output=True, text=True)
    if p.returncode:
        # fall back to DER
        p = subprocess.run(["keytool", "-exportcert", "-keystore", str(keystore),
                            "-storepass", storepass, "-alias", alias],
                           capture_output=True)
        return p.stdout.hex()
    import base64
    b = "".join(l for l in p.stdout.splitlines() if "CERTIFICATE" not in l)
    return base64.b64decode(b).hex()


def inject_signer(xml: str, der_hex: str) -> str:
    if f'signature="{der_hex}"' in xml:
        return xml
    block = f'    <signer signature="{der_hex}"><seinfo value="platform"/></signer>\n'
    return xml.replace("</policy>", block + "</policy>", 1)


def need(p: Path) -> Path:
    if not p.exists():
        sys.exit(f"missing: {p}\n  run extract_blobs.py and follow docs/PATCH-RECIPES.md")
    return p


def build_ims_volte(staging: Path, out: Path, der_hex: str) -> None:
    la = staging / "lg-app"
    with tempfile.TemporaryDirectory() as t:
        tr = Path(t) / "v60_ims_volte"
        (tr / "system/system_ext/priv-app/Ims6").mkdir(parents=True)
        (tr / "system/system_ext/priv-app/lgdataservice").mkdir(parents=True)
        (tr / "system/system_ext/framework").mkdir(parents=True)
        (tr / "system/system_ext/lib64").mkdir(parents=True)
        (tr / "system/system_ext/etc/permissions").mkdir(parents=True)
        (tr / "system/system_ext/etc/selinux").mkdir(parents=True)
        (tr / "system/etc/selinux").mkdir(parents=True)
        (tr / "system/etc").mkdir(parents=True, exist_ok=True)

        import shutil
        shutil.copy(need(staging / "patched/Ims6.apk"),
                    tr / "system/system_ext/priv-app/Ims6/Ims6.apk")
        shutil.copy(need(staging / "patched/lgdataservice.apk"),
                    tr / "system/system_ext/priv-app/lgdataservice/lgdataservice.apk")
        for j in (la / "framework").glob("*.jar"):
            shutil.copy(j, tr / "system/system_ext/framework" / j.name)
        for so in (la / "lib64").glob("*.so"):
            shutil.copy(so, tr / "system/system_ext/lib64" / so.name)
        for x in (la / "etc/permissions").glob("*.xml"):
            shutil.copy(x, tr / "system/system_ext/etc/permissions" / x.name)
        for c in ("ike_conf.xml", "mapcon_conf.xml"):
            if (la / "etc" / c).exists():
                shutil.copy(la / "etc" / c, tr / "system/etc" / c)

        mac = need(la / "etc/selinux/plat_mac_permissions.xml").read_text()
        (tr / "system/etc/selinux/plat_mac_permissions.xml").write_text(inject_signer(mac, der_hex))

        (tr / "module.prop").write_text(IMS_VOLTE_PROP)
        (tr / "sepolicy.rule").write_text(IMS_SEPOLICY_RULE)
        (tr / "post-fs-data.sh").write_text(IMS_POST_FS_DATA)
        (tr / "service.sh").write_text(IMS_SERVICE)
        (tr / "customize.sh").write_text(IMS_CUSTOMIZE)
        zipdir(tr, out / "v60_ims_volte.zip")


def build_vowifi(staging: Path, out: Path) -> None:
    import shutil
    with tempfile.TemporaryDirectory() as t:
        tr = Path(t) / "v60_vowifi"
        (tr / "system/system_ext/bin").mkdir(parents=True)
        (tr / "system/system_ext/priv-app/QualifiedNetworksService").mkdir(parents=True)
        (tr / "system/system_ext/priv-app/Iwlan").mkdir(parents=True)
        (tr / "system/system_ext/etc/permissions").mkdir(parents=True)
        (tr / "system/etc").mkdir(parents=True)

        shutil.copy(need(staging / "native/stroke"), tr / "system/system_ext/bin/stroke")
        shutil.copy(need(staging / "native/ipsecd"), tr / "system/system_ext/bin/ipsecd")
        shutil.copy(need(staging / "patched/QualifiedNetworksService.apk"),
                    tr / "system/system_ext/priv-app/QualifiedNetworksService/QualifiedNetworksService.apk")
        shutil.copy(need(staging / "patched/Iwlan.apk"),
                    tr / "system/system_ext/priv-app/Iwlan/Iwlan.apk")

        # base allowlist ships in tools/; add MODIFY_PHONE_STATE to the qns block
        # (also added to the QNS manifest itself, see PATCH-RECIPES.md 3c)
        xml = need(HERE / "privapp-permissions-v60-aosp-iwlan.xml").read_text()
        perm = '        <permission name="android.permission.MODIFY_PHONE_STATE"/>\n'
        if "MODIFY_PHONE_STATE" not in xml:
            marker = '<privapp-permissions package="com.android.qns">\n'
            if marker not in xml:
                sys.exit("tools/privapp-permissions-v60-aosp-iwlan.xml: no com.android.qns block")
            xml = xml.replace(marker, marker + perm, 1)
        (tr / "system/system_ext/etc/permissions/privapp-permissions-v60-aosp-iwlan.xml").write_text(xml)

        shutil.copy(need(HERE / "andsf.xml"), tr / "system/etc/andsf.xml")

        (tr / "module.prop").write_text(VOWIFI_PROP)
        (tr / "sepolicy.rule").write_text(VOWIFI_SEPOLICY)
        (tr / "post-fs-data.sh").write_text(VOWIFI_POST_FS_DATA)
        (tr / "service.sh").write_text(VOWIFI_SERVICE)
        (tr / "carrier_wlan_override.xml").write_text(VOWIFI_CC_XML)
        zipdir(tr, out / "v60_vowifi.zip")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", type=Path, default=Path("staging"))
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--keystore", type=Path, default=HERE / "v60ims.jks")
    ap.add_argument("--storepass", default="v60ims")
    ap.add_argument("--alias", default="v60ims")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    der = cert_der_hex(need(a.keystore), a.storepass, a.alias)
    print(f"signer cert DER: {len(der)//2} bytes")
    build_ims_volte(a.staging, a.out, der)
    build_vowifi(a.staging, a.out)


if __name__ == "__main__":
    main()
