#!/usr/bin/env python3
"""Patch telephony-common.jar so a rejected-while-ringing incoming IMS call is
cleaned up in Telecom.

LG's IMS reports a locally rejected INCOMING call through
`ImsCall.Listener.onCallStartFailed()` (the same callback AOSP uses for a failed
OUTGOING call). AOSP's cleanup there only covers a pending MO connection, so the
incoming `ImsPhoneConnection` is never disconnected and Telecom stays RINGING ->
the in-call/ringing screen sticks. This adds the incoming-connection cleanup
(find the tracked ringing connection -> `onDisconnect(3)` -> detach -> remove ->
`updatePhoneState`) right before AOSP's existing logic in
`ImsPhoneCallTracker$8.onCallStartFailed`. Transport-agnostic: fixes VoLTE and
VoWiFi. The overlay is bundled into v60_ims_volte by build_consolidated_modules.py.

Only the one class is disassembled/reassembled; the rest of classes.dex is kept
verbatim and merged back with dexlib2 (full baksmali of this jar trips a
hidden-API flag mismatch on unrelated classes).

Requires: `baksmali` and `smali` 3.0.9 on PATH, `javac`/`java`, and the smali
3.0.9 support jars (smali-dexlib2, smali-util, guava, jcommander) for
DexReplaceClass -- pass their directory with --smali-jars.

Input: a STOCK telephony-common.jar for your build
       (`adb pull /system/framework/telephony-common.jar`, or from system.img).

Usage:
  python3 build_incoming_reject_fix.py \\
      --input telephony-common.jar \\
      --smali-jars /path/to/smali-3.0.9/ \\
      --out staging/patched/telephony-common.jar
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET_CLASS = "Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker$8;"
TRACKER_SMALI = Path(
    "com/android/internal/telephony/imsphone/ImsPhoneCallTracker$8.smali"
)

# The `onCallStartFailed` body in ImsPhoneCallTracker$8 begins (on this
# LineageOS 23.x line) with a DomainSelectionResolver lookup. We insert the
# incoming-connection cleanup immediately before it.
MARKER = (
    "    .line 3559\n"
    "    invoke-static {}, Lcom/android/internal/telephony/domainselection/"
    "DomainSelectionResolver;->getInstance()Lcom/android/internal/telephony/"
    "domainselection/DomainSelectionResolver;\n"
)
INSERTION = (
    "    # LG IMS reports rejected incoming calls as start-failed. The callback\n"
    "    # may no longer carry the same ImsCall object, so use the tracked\n"
    "    # ringing connection as a fallback before normal cleanup.\n"
    "    iget-object v2, p0, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker$8;->this$0:Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;\n"
    "    invoke-static {v2, p1}, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;->-$$Nest$mfindConnection(Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;Lcom/android/ims/ImsCall;)Lcom/android/internal/telephony/imsphone/ImsPhoneConnection;\n"
    "    move-result-object v2\n"
    "    if-eqz v2, :v2_reject_ringing_fallback\n"
    "    invoke-virtual {v2}, Lcom/android/internal/telephony/Connection;->isIncoming()Z\n"
    "    move-result v3\n"
    "    if-eqz v3, :v2_reject_continue\n"
    "    goto :v2_reject_cleanup\n"
    "    :v2_reject_ringing_fallback\n"
    "    iget-object v2, p0, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker$8;->this$0:Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;\n"
    "    iget-object v2, v2, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;->mRingingCall:Lcom/android/internal/telephony/imsphone/ImsPhoneCall;\n"
    "    invoke-virtual {v2}, Lcom/android/internal/telephony/imsphone/ImsPhoneCall;->getFirstConnection()Lcom/android/internal/telephony/imsphone/ImsPhoneConnection;\n"
    "    move-result-object v2\n"
    "    if-eqz v2, :v2_reject_continue\n"
    "    invoke-virtual {v2}, Lcom/android/internal/telephony/Connection;->isIncoming()Z\n"
    "    move-result v3\n"
    "    if-eqz v3, :v2_reject_continue\n"
    "    :v2_reject_cleanup\n"
    "    iget-object v1, p0, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker$8;->this$0:Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;\n"
    "    const-string v3, \"onCallStartFailed: clearing incoming connection\"\n"
    "    invoke-virtual {v1, v3}, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;->log(Ljava/lang/String;)V\n"
    "    const/4 v3, 0x3\n"
    "    invoke-virtual {v2, v3}, Lcom/android/internal/telephony/imsphone/ImsPhoneConnection;->onDisconnect(I)Z\n"
    "    invoke-virtual {v2}, Lcom/android/internal/telephony/imsphone/ImsPhoneConnection;->getCall()Lcom/android/internal/telephony/imsphone/ImsPhoneCall;\n"
    "    move-result-object v3\n"
    "    if-eqz v3, :v2_reject_no_parent\n"
    "    invoke-virtual {v3, v2}, Lcom/android/internal/telephony/imsphone/ImsPhoneCall;->detach(Lcom/android/internal/telephony/imsphone/ImsPhoneConnection;)V\n"
    "    :v2_reject_no_parent\n"
    "    iget-object v1, p0, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker$8;->this$0:Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;\n"
    "    invoke-virtual {v1, v2}, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;->removeConnection(Lcom/android/internal/telephony/imsphone/ImsPhoneConnection;)V\n"
    "    iget-object v1, p0, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker$8;->this$0:Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;\n"
    "    invoke-static {v1}, Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;->-$$Nest$mupdatePhoneState(Lcom/android/internal/telephony/imsphone/ImsPhoneCallTracker;)V\n"
    "    return-void\n\n"
    "    :v2_reject_continue\n" + MARKER
)


def run(*a: str) -> None:
    print("+", " ".join(a))
    subprocess.run(a, check=True)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True,
                    help="stock telephony-common.jar for your build")
    ap.add_argument("--smali-jars", type=Path, required=True,
                    help="dir with smali-dexlib2/smali-util/guava/jcommander jars")
    ap.add_argument("--out", type=Path,
                    default=Path("staging/patched/telephony-common.jar"))
    a = ap.parse_args()
    if not a.input.is_file():
        raise SystemExit(f"missing input jar: {a.input}")
    cp = ":".join(str(j) for j in sorted(a.smali_jars.glob("*.jar")))
    if not cp:
        raise SystemExit(f"no jars in {a.smali_jars}")

    with tempfile.TemporaryDirectory(prefix="incoming-reject-") as t:
        tmp = Path(t)
        smali_root = tmp / "smali"
        run("baksmali", "disassemble", str(a.input), "-o", str(smali_root),
            "--api", "35", "--classes", TARGET_CLASS)

        f = smali_root / TRACKER_SMALI
        text = f.read_text()
        if text.count(MARKER) != 1:
            raise SystemExit(
                f"onCallStartFailed marker found {text.count(MARKER)}x "
                "(the .line offset differs on your build; adjust MARKER)")
        f.write_text(text.replace(MARKER, INSERTION, 1))

        repl_dex = tmp / "replacement.dex"
        run("smali", "assemble", "--api", "33", str(smali_root), "-o", str(repl_dex))

        classes_dir = tmp / "cls"
        classes_dir.mkdir()
        run("javac", "-cp", cp, "-d", str(classes_dir),
            str(HERE / "DexReplaceClass.java"))
        merged = tmp / "classes-patched.dex"
        run("java", "-cp", f"{classes_dir}:{cp}", "DexReplaceClass",
            str(a.input), str(repl_dex), TARGET_CLASS, str(merged))

        a.out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(a.input) as src, \
             zipfile.ZipFile(a.out, "w", allowZip64=True) as out:
            for info in src.infolist():
                if info.filename == "classes.dex":
                    out.write(merged, "classes.dex", compress_type=info.compress_type)
                else:
                    out.writestr(info, src.read(info.filename))

    print(f"input  sha256 {sha256(a.input)}")
    print(f"output sha256 {sha256(a.out)}  -> {a.out}")


if __name__ == "__main__":
    main()
