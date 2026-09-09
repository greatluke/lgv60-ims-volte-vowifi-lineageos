# Substrate build service (GitHub Actions)

Lets people get a prebuilt `lg_substrate.img` for a `timelm` ROM build that isn't in the
releases, without running the toolchain themselves. The compute, storage, and downloads are
GitHub's; there is no server to run.

## How a request flows

1. Someone opens an issue with the **Substrate build request** form (ROM name, build ID, a
   URL to the flashable ROM zip: SourceForge direct link, GitHub release asset, pixeldrain
   `/u/<id>`, or a Google Drive `Anyone with the link` share).
2. A maintainer sanity-checks it and adds the **`build`** label.
3. `.github/workflows/build-substrate.yml` runs:
   - downloads the zip (200 MB – 3 GB; `gdown --fuzzy` for Drive links, else `curl`), pulls
     `system_ext.img` out of it
     (`tools/ci/extract_system_ext.sh`, handles A/B `payload.bin`, plain image zips, sparse, `.br`)
   - rejects it if that `system_ext` already contains `/bin/ipsecd` (already grafted)
   - downloads the private **LG donor image**, writes `staging/paths.json`, runs
     `tools/build_lg_substrate_image.py`
   - `xz -9` the result, publishes it to the rolling **`substrates`** release, comments the
     download link + sha256 + flash steps on the issue, and closes it.
4. A manual `workflow_dispatch` (paste a URL + a short tag) does the same without an issue.

## One-time setup

### 1. Private donor repo

Create a **private** repo (e.g. `greatluke/lgv60-ims-donors`). It holds one file: a prepared LG
`system_ext.img` the builder reads via `debugfs`: LG stock `/bin`, `/lib64`, `/etc/init`,
`/etc/ipsec`, and the **corrected** `system_ext_sepolicy.cil` + `*_contexts` (the invalid
`property_service { find }` rule stripped, seapp `levelFrom=all`). The
`los-system_ext-0830-vowifi-folded-v2` image already satisfies this.

Attach it as a release asset so the workflow can `gh release download` it:

```sh
gh release create donor lg_system_ext.img --repo greatluke/lgv60-ims-donors \
  --title "LG donor" --notes "prepared LG V60 system_ext for the substrate builder"
```

Keeping it in a private repo (not this one) is what keeps LG's proprietary blobs out of the
public tree, the same reason LineageOS keeps device blobs in `TheMuppets`, not the main org.

### 2. Secret + variable on this repo

- **Secret** `DONORS_PAT`: a fine-grained PAT with *Contents: read* on the donor repo only.
- **Variable** `DONORS_REPO`: `greatluke/lgv60-ims-donors`.

Settings → Secrets and variables → Actions.

### 3. Label

Create a `build` label. The workflow only runs when that label is added to an issue that already
carries `substrate-request` (the form applies that automatically), so drive-by issues can't burn
Actions minutes, a maintainer is always in the loop.

## Limits (set expectations in the reply)

- The CI **composes the policy; it cannot test-boot**. The builder *appends* the LG policy delta
  to the ROM's own `system_ext` sepolicy (it does not overwrite it), so a derivative's own extra
  types survive. It can still bootloop if that ROM ships a `system_ext` CIL its **on-device**
  `secilc` rejects once the forced recompile kicks in (a latent bug the ROM's precompiled policy
  was hiding), or if a grafted LG rule trips one of that ROM's `neverallow`s. The requester
  power-cycles and reports back; most LineageOS-based `timelm` derivatives are fine.
- **GApps-shipping ROMs**: flashing the substrate wipes `GoogleServicesFramework` (the only GApps
  file in `system_ext`). They must re-flash GApps after, or Play Services crash-loops.
- The **module zips** are the fragile per-ROM part, not the substrate. `v60_ims_volte` bundles a
  `telephony-common.jar` overlay keyed to LineageOS's exact jar; on a ROM with a modified
  framework it must be rebuilt from *that* ROM's stock jar
  (`tools/build_incoming_reject_fix.py`; if the `.line` marker offset differs the script bails
  and the offset needs adjusting). `Ims6` seinfo also depends on their signing setup.
- Every asset carries LG proprietary blobs, the same redistribution posture as publishing a
  substrate yourself. If you don't want substrate distribution to be self-serve, don't create
  the `build` label; the scripts still work for anyone running them against their own firmware.
