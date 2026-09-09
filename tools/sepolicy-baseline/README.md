# `system_ext` sepolicy baseline

The four files here are **stock LineageOS 23.2 `timelm` (nightly 20260830)** `system_ext`
SELinux policy, extracted verbatim from that release's `system_ext.img`
(`/etc/selinux/`). Nothing LG, nothing modified — pure LineageOS build output.

`build_lg_substrate_image.py` uses them as the reference for an **append** graft: it computes
`donor_policy − this_baseline` (the LG-only statements) and appends that delta to the *target
ROM's own* `system_ext` policy, instead of overwriting the target's policy with the donor's.
That keeps a derivative ROM's own `system_ext` types/rules intact (crDroid, EvolutionX,
AlphaDroid, … add their own here, and a wholesale overwrite drops types their `product`
sepolicy still references → boot-time `secilc` fails → bootloop).

## Keep it paired with the donor

The delta is only "LG-only" if this baseline matches the LineageOS version the **donor image**
was cut from. The shipped donor is built on 20260830, so this baseline is 20260830. If you
refresh the donor from a newer nightly, re-extract these four files from that same nightly:

```sh
unzip -o lineage-23.2-<date>-nightly-timelm-signed.zip payload.bin
payload_dumper --partitions system_ext --out /tmp/los payload.bin
for f in system_ext_sepolicy.cil system_ext_file_contexts \
         system_ext_service_contexts system_ext_property_contexts; do
  debugfs -R "dump -p /etc/selinux/$f tools/sepolicy-baseline/$f" /tmp/los/system_ext.img
done
```
