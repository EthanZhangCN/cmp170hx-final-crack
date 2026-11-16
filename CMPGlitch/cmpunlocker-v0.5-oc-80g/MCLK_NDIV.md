# Adding HBM NDIV memory-clock overclock to stock cmpunlocker (v0.5)

This document is the complete recipe for taking a **stock `cmpunlocker` v0.5 tree**
and turning it into `cmpunlocker-v0.5-oc` — v0.5 plus the HBM NDIV
memory-clock overclock (`--mclk-ndiv=N`), and nothing else.

Everything here is reproducible: the OC delta is **9 files** (3 added, 6
modified; this document is a 10th, added purely for reference). All other
files are byte-identical to stock v0.5.

---

## 1. What the OC does

Sets the HBM2e memory clock during driver init:

```
mem_clock = NDIV × 27 MHz
```

| NDIV | clock  | notes                          |
|------|--------|--------------------------------|
| 64   | 1728   | stock, 8 GB 300 W part         |
| 54   | 1458   | stock, 8 GB 250 W part         |
| 45   | 1215   | stock, 10 GB part              |
| 70   | 1890   | our target (`--mclk-ndiv=70`)  |

Valid range enforced by `driver/build.sh`: **30–80** (810–2160 MHz).

Mechanics (two halves, both compiled out unless `CMPUNLOCK_MCLK_NDIV` is set):

1. **Post-Booter-Load** (`kgspCmpHbmNdivPostBooterLoad`, called from
   `kgspBootstrap_TU102` in `kernel_gsp_tu102.c`): asserts
   MEMCLK_CHANGE_ALERT, enters HBM self-refresh, PLL-cycles each active
   FBPA (12 slots, base `0x900000`, stride `0x4000`) to the new NDIV,
   waits for PLL lock, runs DDLL calibration, exits self-refresh.
2. **Post-GSP** (`kgspCmpHbmNdivPostGsp`, called from `kgspInitRm_IMPL` in
   `kernel_gsp.c`): re-writes the COEFF via the multicast register in case
   GSP reprogrammed the PLL during its own memory init, then verifies lock.

Gated on the v0.5 target check (`_kgspSec2PostblTimingEnabled`, i.e. only
CMP 170HX 8 GB/10 GB devices) so non-target GPUs are untouched.

### Registers

Declared in `common/constants.yaml` (`hbm_ndiv:` unlock, 13 entries) and as
`#define`s in the hunk-1 block of `driver/patches/hbm-ndiv.patch` (the
manifest gate in `tools/read-constants.py` cross-checks these two — keep
them in sync):

| register            | address    | use                          |
|---------------------|------------|------------------------------|
| fbpa_pll_plm        | 0x00903c7c | PLL gate / status (bit4 = open) |
| fbpa_pll_cfg        | 0x00903c90 | PLL config (lock bit 0x20)   |
| fbpa_pll_coeff      | 0x00903c98 | NDIV lives in bits 15:8      |
| fbpa_pll_coeff_mc   | 0x0098bc98 | multicast COEFF (post-GSP write) |
| pri_fence           | 0x001211fc | PRI read fence               |
| fbio_broadcast      | 0x009a0590 | MEMCLK_CHANGE_ALERT (bit31)  |
| hbm_self_refresh    | 0x009a031c | self-refresh enter/exit      |
| ddll_cal            | 0x009a11dc | DDLL calibration trigger (bit6) |
| ddll_cal_status_0   | 0x009a0674 | cal status                   |
| ddll_cal_status_1   | 0x009a0678 | cal status                   |
| plm_gate_pll0/1/2   | 0x009a3c7c / 80 / 84 | Booter PLM table gates (value 0xffffffff) |

The last three are added to v0.5's `plmTable[]` and the table loop bound is
changed from a fixed `12` to `NV_ARRAY_ELEMENTS(plmTable)` so the new gates
actually get opened.

### Log markers (`dmesg | grep HBMPLL_OC`)

```
HBMPLL_OC: start NDIV 64->70 (1728->1890 MHz) devid=0x20c2 vbios=... PLM=0x...
HBMPLL_OC: done, N FBPAs at NDIV=70 (1890 MHz)          <- success
HBMPLL_OC: DDLL cal status 0x... 0x...
HBMPLL_OC: post-GSP PRE:  COEFF=0x...(NDIV=...) ...
HBMPLL_OC: post-GSP POST: COEFF=0x...(NDIV=...) CFG=0x... lock=1 iter=...  <- success
```

Failure shapes: `aborted, PLM=... (bit4 closed)`, `FBPA<n> PLL lock timeout`,
`no active FBPAs found`, `<n>/<m> FBPAs failed to lock`,
`cmpSafe=1 - skipping HBM PLL overclock` (escape hatch active).

---

## 2. The 9-file delta

### Added (3)

| file | purpose |
|------|---------|
| `driver/patches/hbm-ndiv.patch` | **Register map source of truth.** Not applied by `patch(1)` anymore — `apply-ndiv.py` reads the 10 `CMP_REG_*` `#define`s out of it, and the manifest gate reads the same addresses from it. Keep it as the single place the register list exists. |
| `driver/inc/cmp_hbm_ndiv_impl.h` | The two NDIV C functions (verbatim port of the fork's `driver/src/cmpunlock.c` NDIV sections, renamed to `kgspCmpHbmNdiv*` / `_kgspCmpHbmNdivSafeMode`). Self-guarded by `#ifdef CMPUNLOCK_MCLK_NDIV`. `build.sh` copies it into the NVIDIA source tree at `src/nvidia/inc/kernel/gpu/` where `kernel_gsp.c` includes it. |
| `driver/apply-ndiv.py` | Marker-based applier (see §3). |

### Modified (6)

| file | change vs stock |
|------|-----------------|
| `driver/build.sh` | `hbm-ndiv.patch` in `PATCH_ORDER`; NDIV validation (30–80); generates `cmp_hbm_ndiv_cfg.h` (macro + prototypes) when `CMPUNLOCKER_MCLK_NDIV` is set; installs the impl header; routes `hbm-ndiv.patch` through `apply-ndiv.py` instead of `patch(1)`; NDIV value in the build stamp. |
| `install.sh` | `--mclk-ndiv=N` flag → `CMPUNLOCKER_MCLK_NDIV` env; help text; summary line "MCLK: NDIV=N (...) will be compiled in". |
| `common/constants.yaml` | `hbm_ndiv:` unlock entry (13 registers, §1) so the manifest gate covers it. |
| `driver/VERSION` | Pinned to `610.43.03` / `610.43.02` — the only driver versions the delta is verified against. (Stock lists more; adding a version means re-verifying the anchor markers + patch sequence against it.) |
| `README.md` | "HBM memory overclock (NDIV)" section. |
| `CREDITS.md` | Fork credit for the NDIV code. |

---

## 3. Why markers instead of `patch(1)` for our hunks

History: the first two implementations of the NDIV insertion were plain
unified-diff hunks. On a Proxmox host (Debian, **GNU patch 2.8**) the
pure-insertion hunks failed with `Hunk #N FAILED` even though:

- the received patch file was byte-identical to the verified one (same SHA256),
- the source tarball was byte-identical (same SHA256),
- the required context lines provably existed in the file (byte-diffed with
  `cat -A`), 2–70 lines from the hunk hint,
- the *same* context had applied with `fuzz 2` on the same host in an
  earlier run, and
- the one hunk containing a real delete/replace line applied fine.

The v0.5 patches (written by their author, matching contexts) apply cleanly
on the same host. Conclusion: multi-line-context + guessed-line-number
matching is version- and host-fragile. `driver/apply-ndiv.py` therefore
anchors every edit to a **line that must occur exactly once**:

| # | edit | anchor (unique line) |
|---|------|----------------------|
| 1 | cfg include + register map + impl include (kernel_gsp.c) | `#include "crashcat/crashcat_report.h"` |
| 2 | 3 PLM table gates (kernel_gsp.c) | `"PJTAG_SEC_PLM"` |
| 3 | loop bound `12` → `NV_ARRAY_ELEMENTS(plmTable)` (kernel_gsp.c) | `plmIdx < 12` |
| 4 | post-GSP call (kernel_gsp.c) | `kgspStartLogPolling(pGpu, pKernelGsp), done` |
| 5 | cfg include (kernel_gsp_tu102.c) | `#define SEC2_DEBUG_PRI_MMU_LMR` |
| 6 | post-Booter-Load call (kernel_gsp_tu102.c) | `// Send init RPCs necessary for GSP-RM booting` |

It aborts with a clear error if any anchor is found 0 or 2+ times, and is
idempotent (re-running after a partial state is safe). This is why the
v0.5 patches keep using `patch(1)` and only the NDIV step uses the script.

---

## 4. Building the tree from stock (reproducible recipe)

Precondition: a stock `cmpunlocker` (v0.5) checkout and the 9 delta files
(e.g. from an existing `cmpunlocker-v0.5-oc` tree or this document's
source).

```bash
cd <dir containing both>

# a) set aside the 9 delta files
mkdir -p oc-files/driver/patches oc-files/driver/inc oc-files/common
cp <oc>/driver/patches/hbm-ndiv.patch   oc-files/driver/patches/
cp <oc>/driver/inc/cmp_hbm_ndiv_impl.h  oc-files/driver/inc/
cp <oc>/driver/apply-ndiv.py           oc-files/driver/
cp <oc>/driver/build.sh                oc-files/driver/
cp <oc>/driver/VERSION                 oc-files/driver/
cp <oc>/install.sh                     oc-files/
cp <oc>/common/constants.yaml          oc-files/common/
cp <oc>/README.md                      oc-files/
cp <oc>/CREDITS.md                     oc-files/

# b) fresh tree from stock
rm -rf cmpunlocker-v0.5-oc
cp -r cmpunlocker cmpunlocker-v0.5-oc

# c) apply the delta
mkdir -p cmpunlocker-v0.5-oc/driver/inc
cp oc-files/driver/patches/hbm-ndiv.patch   cmpunlocker-v0.5-oc/driver/patches/
cp oc-files/driver/inc/cmp_hbm_ndiv_impl.h  cmpunlocker-v0.5-oc/driver/inc/
cp oc-files/driver/apply-ndiv.py           cmpunlocker-v0.5-oc/driver/
cp oc-files/driver/build.sh                cmpunlocker-v0.5-oc/driver/build.sh
cp oc-files/driver/VERSION                 cmpunlocker-v0.5-oc/driver/VERSION
cp oc-files/install.sh                     cmpunlocker-v0.5-oc/install.sh
cp oc-files/common/constants.yaml          cmpunlocker-v0.5-oc/common/constants.yaml
cp oc-files/README.md                      cmpunlocker-v0.5-oc/README.md
cp oc-files/CREDITS.md                     cmpunlocker-v0.5-oc/CREDITS.md
rm -rf oc-files
```

### Pre-flight checks (run in the new tree)

```bash
# manifest gate: must print 17 unlocks and exit 0
python3 tools/read-constants.py common/constants.yaml driver/patches driver/build.sh 8gb

# script sanity
bash -n driver/build.sh install.sh
python3 -m py_compile driver/apply-ndiv.py

# line endings (required on Linux)
! grep -rlU $'\r' --include='*' . --exclude-dir=.git && echo 'LF clean'

# delta must be exactly the 9 files (run from the parent of both trees)
diff -rq cmpunlocker cmpunlocker-v0.5-oc | grep -v '\.git'
```

---

## 5. Install / verify / escape hatches

```bash
# CRLF safety pass (mandatory when files crossed Windows -> Linux):
find . -path ./.git -prune -o -type f -exec sed -i 's/\r$//' {} +

sudo ./install.sh --mclk-ndiv=70        # stock build: omit the flag
sudo shutdown -h now                    # COLD reboot — Booter/PLL work happens at cold boot
```

After boot:

```bash
sudo dmesg | grep HBMPLL_OC             # §1 success lines
nvidia-smi                              # expect 65536 MiB (was 8192 MiB)
nvidia-smi -q -d ECC | head -20         # v0.5 ECC still active
gpu-burn -d 300                         # stability gate: zero errors
```

- **Escape hatch (GPU wedges at init):** add
  `nvidia.NVreg_RegistryDwords=cmpSafe=1`, cold reboot → OC skipped, unlock
  still active, `dmesg` shows `cmpSafe=1 - skipping HBM PLL overclock`.
- **Rollback:** `cd <stock cmpunlocker> && sudo ./install.sh` (installs to
  the same `/lib/modules/$(uname -r)/updates/cmpunlocker/` slot), cold reboot.
- **PVE kernel upgrade:** re-run `sudo ./install.sh --mclk-ndiv=70`
  (DKMS/build is per-kernel), cold reboot.
- **Uninstall:** `sudo ./remove.sh --yes`.

---

## 6. If you change the NDIV value

`--mclk-ndiv=N` is a build-time constant (baked into the generated
`cmp_hbm_ndiv_cfg.h`). Change it → re-run install → cold reboot. No tree
changes needed. Range 30–80; step down if gpu-burn shows errors at a new
value (1890 MHz at NDIV=70 was the target; stock 1728 MHz at NDIV=64 is the
floor for "no overclock").

## 7. Known-good environment (baseline for this recipe)

- host: Proxmox, kernel `7.0.14-23-pve`
- driver: `nvidia-open 610.43.03` (only 610.xx supported by both upstream v0.5
  and the fork; the other stock-VERSION entries are unverified with this delta)
- GPU: CMP 170HX 8 GB (`10de:20c2`), target 64 GB geometry
- tooling: GNU patch 2.8 (v0.5 patches), python3 + PyYAML (gate + applier)
