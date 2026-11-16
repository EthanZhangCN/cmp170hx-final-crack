# Sources, upstream credits and licenses

Where every vendored component in this release came from, the exact upstream
snapshot it was taken at, and the community work this record builds on.
Clone metadata (`.git`) was removed from all vendored trees; the commits
below identify each snapshot. Vendored trees keep their own LICENSE and
credit files -- those terms apply to their contents.

## Vendored snapshots (bundled in this release)

| Local path | Upstream | Snapshot commit | License |
|---|---|---|---|
| `ga100vfi/` | https://github.com/kate-land/ga100vfi | `69220fcd4faaa1adee0a4ba80bcfeb5e4fd73691` | MIT (ST CMSIS device components Apache-2.0; see `ga100vfi/LICENSING.md`) |
| `pico-ga100vfi/` | https://github.com/dalzyu/pico-ga100vfi | `8e06391415d6d4abe595c0e15f7a11bc101cea9e` | MIT |
| `cmpunlocker-v0.5-oc-80g/` | https://github.com/amoghmunikote/cmpunlocker + https://github.com/asm64-hooligan/cmpunlocker (NDIV fork) | `17535a0ab1e8d8a9797e096ac5a2495f3da16d2c` | GPL-2.0 |
| `cmpunlocker-v0.5-oc-80g-gen3/` | same lineage as above | `17535a0ab1e8d8a9797e096ac5a2495f3da16d2c` | GPL-2.0 |
| `cmprir/ga100-fuse-re/` | https://github.com/ahnguyen17/ga100-fuse-re | `7a101722bfd8ebe0672026362162ff9e1c5ae2d3` | no license file in snapshot -- check upstream |
| `cmprir/gpcprobe/` | https://github.com/matstatman/gpcprobe | `048c8f21d6c077f27066101a67a1e5ecb2da1df8` | no license file in snapshot -- check upstream |
| `cmprir/170hx-rir-tool/` | https://github.com/lixiangwuxian/170hx-rir-tool | `bba9dbd3d03e6e348a15e5e799449470eeab191f` | MIT |

## Local work on top of the snapshots

- `cmpunlocker-v0.5-oc-80g/`: upstream v0.5 + the asm64-hooligan HBM NDIV OC
  port, plus local additions: post-GSP PCIe Gen2/Gen3/Gen4 patches
  (`driver/patches/pcie-gen*.patch`), staged training scripts
  (`tools/cmp-gen3.sh`, `tools/cmp-gen4.sh`, `tools/set_pcie_speed.c`),
  CRLF normalization in `driver/build.sh`. Mechanism and results:
  `cmp_gen3.md`, `cmp_gen4.md`, `MCLK_NDIV.md`.
- `cmpunlocker-v0.5-oc-80g-gen3/`: same lineage minus the Gen4 patch
  (Gen3-only variant).
- `cmpunlocker-a100-minimal/`: **original work**, trimmed from
  `cmpunlocker-v0.5-oc-80g` for the converted A100 (device gate 0x20B5;
  FMA override always on, opt-in `--sm-unlock` and `--mclk-ndiv=N`).
  Derivative of GPL-2.0 cmpunlocker; no LICENSE file in the tree yet.
- `tools/`, `host_scripts/`, `a100-flash/`, the `cmp_*.md` records and this
  release's README: **original work** of this project (no upstream).
- `snapshots/registers_20261113_153657.json`: own-card measurement
  (defect-mask baseline for `tools/ff_derive.py`; card UUID redacted).
- `tests.png`: own benchmark screenshots (OpenCL-Benchmark, see below).

## Community references (not vendored)

- Kate Land -- the GA100 VFI method, fuse controller map and original
  tooling: https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100
- Consensus-Protocol CMP 170HX wiki -- boot gate (Board ID + fused device
  ID), VBIOS MAC ranges, the A100 cross-flash dead end, PCIe Gen3 notes:
  https://github.com/Consensus-Protocol/cmp170hx and https://cmp.wiki
- lixiangwuxian -- proven 10 GB Gen3 guide:
  https://github.com/lixiangwuxian/170hx_tips (`Gen3_unlock_en.md`)
- evanrodgers -- RIR burn wrapper:
  https://github.com/evanrodgers/cmp-170hx-rir-burn-util
- JRex -- GA100 fuse/register table:
  https://gist.github.com/JRex286/0480d2b2b35ad594e57b6543952be307
- amoghmunikote -- GA100 VBIOS Comparison Table gist (ROM inventory, MAC
  ranges, device IDs; "cross-flash fails on device ID mismatch") and
  cmpunlocker: https://github.com/amoghmunikote/cmpunlocker
- mellojello_ -- concurrent 8 GB CMP 170HX write-up; first public proof of
  the I2C + `PEX_RST` actuation path (credited in `pico-ga100vfi/README.md`)
- NVIDIA -- open-gpu-kernel-modules, the driver source the cmpunlocker
  trees build against (610.43.03 / 610.43.02):
  https://github.com/NVIDIA/open-gpu-kernel-modules
- Validation tools used in the records: ProjectPhysX/OpenCL-Benchmark
  (https://github.com/ProjectPhysX/OpenCL-Benchmark, `tests.png`) and
  wilicc/gpu-burn (https://github.com/wilicc/gpu-burn)

## A100 VBIOS sourcing (not bundled)

The A100 80GB PCIe ROM is NVIDIA proprietary firmware and is **not**
redistributed here. Source it yourself, e.g. from the TechPowerUp VBIOS
collection: https://www.techpowerup.com/vgabios/ -- look for A100 80GB PCIe,
board P1001 / SKU230, version 92.00.A0.00.03, 1 MiB full SPI. Verify the
SHA-256 and the byte-level checklist in `cmp_a100_conversion.md`
("The A100 ROM") before flashing.

Warning: TechPowerUp 283106 (https://www.techpowerup.com/vgabios/283106) is
the **Drive-A100 PG199** image the wiki warns against -- NOT the retail
A100 80GB PCIe image this procedure requires.

## Licensing notes / open items

- GPL-2.0 trees (`cmpunlocker-v0.5-oc-80g*`): this release distributes their
  modified source in full, as the license requires.
- `cmpunlocker-a100-minimal/` is a GPL-2.0 derivative but carries no LICENSE
  file yet; add one before publishing.
- The original work (`tools/`, `host_scripts/`, records, README) has no
  top-level license yet; choose one before publishing.
- `cmprir/ga100-fuse-re/` and `cmprir/gpcprobe/` snapshots contain no license
  file; confirm upstream terms before redistributing them.
