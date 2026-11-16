# CMP 170HX -> A100 80GB PCIe: fuseless-fuse unlock + conversion

Record and tooling for unlocking the NVIDIA CMP 170HX (GA100 die) to its
full silicon -- **128 SM / 80 GB HBM / PCIe Gen4** -- via fuseless-fuse
(FF) burns, and converting it to a genuine **A100 80GB PCIe** identity
(device-ID FF override + genuine NVIDIA-signed A100 VBIOS). The converted
card runs native PCIe Gen4 x16 with correct driver reporting, 300 W, and no
driver hacks for PCIe/memory.

Measured on the converted card: `nvidia-smi` = NVIDIA A100 80GB PCIe,
81920 MiB, PCIe Max/Current 4/4, x16; bandwidth H2D 26.76 / D2H 26.67 GB/s
(256 MiB pinned). Benchmark proof: [`tests.png`](tests.png).
ECC was never fuse-burned: pre-conversion ECC was a volatile cmpunlocker
software override, and post-conversion ECC is completely unvalidated and
untested (`cmp_burn.md`, Remaining work / known limitations).

## Scope: bring your own burn window

The FF burns require the GA100 **VFI burn window**: DSOV=0, SBFE=0, EFP=1,
FUSECTRL_PLM open (see `cmp_burn.md`). Reaching it is a prerequisite, not
part of the burn tools. The burns in this record were performed on a
self-built RP2040 PWMVID glitcher; that firmware is **not shipped here**.
Two public VFI toolchains are bundled for this step:

- [`ga100vfi/`](ga100vfi/README.md) -- kate-land's STM32 PWMVID toolchain
  (upstream: github.com/kate-land/ga100vfi)
- [`pico-ga100vfi/`](pico-ga100vfi/README.md) -- dalzyu's RP2350 I2C +
  PEX_RST port (upstream: github.com/dalzyu/pico-ga100vfi)

Use either, or your own rig. The burn tools only check the register state.

> **WARNING -- irreversible hardware modification.** FF burns are 0->1 OTP
> writes: permanent, and reversible only by appending restore records in a
> fresh burn window. A wrong record can permanently disable units or wedge
> the die. Never retry a failed burn. The A100 VBIOS needs a host that can
> allocate a 128 GB BAR at POST (compatibility list in
> `cmp_a100_conversion.md`). A HW SPI flasher is recommended as the recovery
> escape hatch. Use at your own risk; no warranty, no liability.

## Known limitations -- not everything is validated

- **Fuse-set completeness is not proven.** Passes 1-6 (`cmp_burn.md`) are
  what this card needed for the observed results; we are NOT 100% confident
  these are all the fuses that need burning. Other option words (NVDEC,
  MIG, ...) are untested. Treat the record as a working subset, not a
  complete fuse map.
- **Full FP32 speed needs the driver, even with the A100 BIOS.** The
  SM_SPEED_SELECT FMA throttle is a die fuse the conversion does not change.
  Without `cmpunlocker-a100-minimal`'s always-on SM_SPEED override, FP32 FMA
  throughput stays throttled (roughly half).
- **128 SM needs the driver too.** The A100 GSP applies its own floorsweep
  at init: 124 SM native. The FF burns enable all TPCs, but going 124 -> 128
  requires `cmpunlocker-a100-minimal --sm-unlock` (volatile pre-boot
  override). Without it the converted card is a 124-SM A100.
- **NVLink is untested.** The NVLINK_DISABLE burn (row 308) is done, but
  bring-up was never attempted: it needs the U11 hardware mod plus an
  undocumented ROM-check bypass. The fuse alone is necessary, not sufficient.
- **ECC is completely unvalidated and untested** post-conversion. No ECC
  fuse was burned; pre-conversion ECC was a volatile cmpunlocker software
  override. Do not rely on ECC on this card.

## Layout

| Path | Contents |
|---|---|
| `tools/` | FF toolchain: read-only dump/decode, per-die plan derivation, guarded single-record burn, restore derivation, volatile fuse tests, PCIe PHY probes, bandwidth test, final feature check |
| `cmp_burn.md` | The fuse burn record: passes 1-6, rows 293-312, defect masks, safety rules |
| `cmp_a100_conversion.md` | Conversion record: device-ID FF override, ROM verification, host compatibility, recovery |
| `a100-flash/` | Step-by-step conversion + recovery procedure |
| `host_scripts/` | Read-only SM/HBM ceiling checker (`check.py`) + its offline tests |
| `snapshots/` | Baseline defect-mask snapshot (input to `tools/ff_derive.py`) |
| `cmpunlocker-v0.5-oc-80g/` | cmpunlocker v0.5 + HBM NDIV OC + Gen2/3/4 patches (CMP-stage driver) |
| `cmpunlocker-v0.5-oc-80g-gen3/` | Gen3-only variant of the above |
| `cmpunlocker-a100-minimal/` | Post-conversion driver: full FMA speed always, opt-in `--sm-unlock` (128 SM) and `--mclk-ndiv=N` (HBM OC) |
| `ga100vfi/`, `pico-ga100vfi/` | Vendored public VFI toolchains (the burn-window step) |
| `cmprir/` | Fuse reverse-engineering research: FF instruction decode, RIR macro verification, burn-list erratum, field data |
| `cmp_gen3.md`, `cmp_gen4.md`, `cmp_gen4_retrain_analysis.md`, `gen4-debug/` | Superseded PCIe retrain research (kept as record; the A100 conversion replaces it) |
| `sources.md` | Upstream repos, snapshot commits, licenses, community references, ROM sourcing |

## Workflow

1. **Reach the burn window** with a VFI toolchain (above), on a Linux host
   as root with the nvidia driver unloaded/blacklisted for the whole
   glitch + burn session. Verify DSOV=0, CRC=0x55, EFP=1,
   FUSECTRL_PLM=0xFFFFFFFF.
2. **Enable the write path** (each session; volatile):
   `sudo python3 tools/sw_override.py --bdf 0000:01:00.0` then
   `sudo python3 tools/open_wdata_plm.py --bdf 0000:01:00.0`.
3. **Read the die** -- on a stock boot with the driver bound, BEFORE the
   burn session (`check.py` cross-checks identity via nvidia-smi):
   `tools/ff_dump.py` (FF rows + shadows, no driver needed),
   `host_scripts/check.py` (defect masks / ceiling). Defect masks are
   per-die -- never reuse another card's plan. The bundled `ff.json` is
   this card's pre-burn reference dump; plan JSONs are generated per-die
   by the derive tools, not shipped.
4. **Derive plans** (read-only): `tools/ff_derive.py` (SM/HBM floorsweep),
   `ff_derive_pes.py`, `ff_derive_pcie.py`, `ff_derive_nvlink.py`,
   `ff_magic_derive.py` (MAGIC_D / device-ID, empirical per-die map).
5. **Burn guarded**: `tools/ff_burn.py --plan <plan> --index N` (dry-run by
   default; `--confirm` + type `BURN` to commit). All prerequisites and the
   read-back are checked; it aborts on any failure and never retries.
6. **Power-cycle** so the fuse controller replays the FF chain; confirm with
   `tools/final_check.py`.
7. **Optional -- convert to A100**: follow `a100-flash/README.md`
   (device-ID FF burn -> full power off / cold boot -> card POSTs as 20b5,
   not functional but flashable -> in-band nvflash of the verified A100 ROM
   -> cold boot -> native A100, Gen4 x16).
8. **Post-conversion driver**: `cmpunlocker-a100-minimal/` for full FMA
   throughput, optional 128 SM and HBM OC (all volatile, removable).
9. **Reverting**: `tools/ff_restore.py` / `ff_magic_derive.py --restore`
   append restore records in a fresh burn window (last-record-wins).

## The A100 ROM is not bundled

NVIDIA proprietary firmware. Source the A100 80GB PCIe image (P1001/SKU230,
92.00.A0.00.03, 1 MiB full SPI) yourself and verify it before flashing --
SHA-256 of the copy used here plus the full byte-level checklist are in
`cmp_a100_conversion.md` ("The A100 ROM").

## Credits / references

Upstream provenance, snapshot commits and licenses: [`sources.md`](sources.md).

- Kate Land: GA100 VFI method, fuse controller map --
  https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100
  and github.com/kate-land/ga100vfi
- dalzyu: pico-ga100vfi (RP2350 I2C/PEX_RST port); mellojello_ for the
  concurrent 8 GB write-up and the I2C + PEX_RST path
- amoghmunikote: cmpunlocker (github.com/amoghmunikote/cmpunlocker);
  asm64-hooligan: HBM NDIV OC fork
- Consensus-Protocol/cmp170hx wiki; JRex GA100 fuse/register table;
  lixiangwuxian/170hx_tips Gen3 guide
- Vendored trees retain their own licenses and credits (see each README /
  LICENSE / CREDITS.md).
