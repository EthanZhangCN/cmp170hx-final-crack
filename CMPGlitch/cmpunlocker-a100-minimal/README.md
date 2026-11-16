# cmpunlocker-a100-minimal

**Command reference: [COMMANDS.md](COMMANDS.md)** -- install flags, verify,
stability tests, NDIV changes, escape hatch, rollback, troubleshooting.

Stripped-down cmpunlocker for the **converted A100 80GB PCIe** (fused device
ID `10de:20b5`, genuine A100 VBIOS `92.00.A0.00.03`). The conversion already
gives native 80GB, 128GB BAR1 and PCIe Gen4 x16 -- this driver adds back
only the three things the A100 VBIOS does not:

1. **Full FMA speed (always on).** The SM_SPEED feature override
   (`0x82381c <- 0x88888888`, `0x823820 <- 0x8`) lifts the fused FMA
   throttle -- that fuse is on the die and is NOT changed by the A100
   VBIOS. Applied on every install, with or without any flag: full FP32
   FMA throughput even at native 124 SMs.
2. **Full SM count (128), opt-in via `--sm-unlock`.** Without the flag the
   card keeps the native A100 GSP floorsweep (124 SMs). With it, in the
   SEC2 POSTBL window (before GSP boots) the driver writes the TPC reconfig
   override (`0x820a40 + gpc*4 <- 0xff`, GPC 0-7) -- the A100 GSP applies
   its own floorsweep during init, and this pre-boot override blocks it
   (verified: 124 -> 128).
3. **HBM overclock (optional, `--mclk-ndiv=N`).** PLL NDIV reprogram,
   `clock = N * 27 MHz`, range 30-80. This card's A100-stock HBM clock is
   1512 MHz = NDIV 56 (measured). Applied in two halves: post-Booter-Load
   (PLL cycle per active FBPA + DDLL cal, before GSP runs) and post-GSP
   (multicast COEFF re-write in case GSP reprogrammed the PLL).

The flags are independent: `--mclk-ndiv=64` alone overclocks HBM while
keeping native 124 SMs; `--sm-unlock` alone gives 128 SMs at stock HBM
clock. FMA speed is full in every combination.

Everything else from the original cmpunlocker is removed: no PCIe
gen2/3/4 forcing, no BAR1 resize, no memory geometry rewrite, no ECC
override, no name/SKU patches, no passthrough/IOMMU plumbing.

**Nothing here is permanent.** All unlocks are volatile register writes
applied at every driver init. No fuses are touched. `sudo ./remove.sh --yes`
plus a reboot returns the card to native A100 behavior.

## Requirements

- Converted card enumerating as `10de:20b5` (see `../cmp_a100_conversion.md`).
- Host that can map the 128GB BAR at POST (see ../cmp_a100_conversion.md).
- nvidia-open **610.43.03** or **610.43.02** installed (GSP firmware present).
- Kernel headers for the running kernel, gcc, make, python3 + PyYAML.
- Secure Boot off (unsigned modules).

## Install

```bash
sudo ./install.sh --sm-unlock --mclk-ndiv=64   # 128 SMs + 1728 MHz HBM
sudo shutdown -h now                           # cold reboot (booter/PLL work happens at cold boot)
```

Flag combinations:

| Command | SMs | FMA speed | HBM clock |
|---|---|---|---|
| `sudo ./install.sh` | 124 (native) | full | stock (1512 MHz) |
| `sudo ./install.sh --sm-unlock` | 128 | full | stock |
| `sudo ./install.sh --mclk-ndiv=64` | 124 (native) | full | 1728 MHz |
| `sudo ./install.sh --sm-unlock --mclk-ndiv=64` | 128 | full | 1728 MHz |

Both flags are build-time: changing them re-runs the build (the source tree
is re-patched automatically when the flags change). Re-run after every
kernel upgrade.

## Verify

```bash
nvidia-smi                            # A100 80GB PCIe, 81920 MiB, Gen4 x16 (unchanged)
python3 smcheck.py                    # SMs=128 with --sm-unlock, SMs=124 without
sudo dmesg | grep SEC2_DEBUG          # POST-WRITE SS0 always; SM-RECONFIG only with --sm-unlock
sudo dmesg | grep HBMPLL_OC           # 'done, N FBPAs at NDIV=64 (1728 MHz)' + post-GSP lock=1
sudo python3 tools/smprobe.py         # read-only TPC plane dump (pure Python, no module)
```

`smprobe.py` never writes to the GPU. With `--unbind` it temporarily
unbinds/rebinds the nvidia driver to read BAR0 (GPU idle; CUDA contexts are
dropped). Without the driver bound it reads directly.

FP32 sanity: with 128 SMs at ~1.4 GHz and the FMA throttle lifted, an FMA
benchmark should approach ~23 TFLOPS; roughly half that means the throttle
is still active.

## Escape hatches / rollback

- **OC wedges GPU at init:** add `nvidia.NVreg_RegistryDwords=cmpSafe=1`
  (kernel cmdline or modprobe conf), cold reboot. OC is skipped, SM unlock
  stays. Then step NDIV down and reinstall.
- **Full rollback:** `sudo ./remove.sh --yes` (restores stock modules),
  reboot. Card is a native A100 again -- nothing to undo in fuses/ROM.
- **Bad NDIV value:** rebuild with a lower `--mclk-ndiv=N` (step down toward
  59 = stock).

## What the patches do

| Patch | Role |
|---|---|
| `sec2-postbl-plm-ss-cfg.patch` | SEC2 POSTBL window: swap booter signature for the register-write payload, open 12 PLMs (incl. OPT_PLM / RECONFIG_PLM), then the SM_SPEED override (always) and -- only if built with `--sm-unlock` (`CMPUNLOCK_SM_UNLOCK`) -- SM-RECONFIG (8x TPC override); rebuild stock signature, repopulate WPR meta. Gated on `0x20B5` only. |
| `booter-verify.patch` | Boot-path hardening (asserts -> explicit returns) + POST-BooterLoad diagnostics; also the anchor for the NDIV include. |
| `hbm-ndiv.patch` | Register map source of truth for the NDIV OC; applied marker-based by `driver/apply-ndiv.py` (not patch(1)). |

The sec2/booter-verify patches were regenerated against the real
`open-gpu-kernel-modules-610.43.03` source and round-trip verified
(fresh extract -> apply -> byte-identical), and `apply-ndiv.py` +
the `common/constants.yaml` manifest gate both pass on the result.

## Files

```
install.sh / remove.sh          install / rollback
COMMANDS.md                     full command reference
common/lib.sh                   shell helpers
common/constants.yaml           register manifest (validated at build)
tools/read-constants.py         manifest gate
tools/smcheck.py                SM count via libcuda
tools/smprobe.py                read-only TPC/SM probe (pure Python)
driver/VERSION                  610.43.03 / 610.43.02
driver/build.sh                 download source, patch, build, install modules
driver/apply-ndiv.py            marker-based NDIV hook applier
driver/inc/cmp_hbm_ndiv_impl.h  NDIV implementation (copied into source tree)
driver/patches/                 the 3 patches above
```

## Lineage

Trimmed from `cmpunlocker-v0.5-oc-80g` (itself cmpunlocker v0.5 +
asm64-hooligan's NDIV fork). The SM-RECONFIG / SEC2 POSTBL mechanism is
unchanged from the proven CMP unlock; only the device-ID gate changed
(`0x20C2/0x2082` -> `0x20B5`) and the now-native geometry/PCIe work was
removed; the volatile ECC override was dropped (ECC completely
unvalidated/untested post-conversion). Fuse history: `../cmp_burn.md`. Conversion:
`../cmp_a100_conversion.md`.
