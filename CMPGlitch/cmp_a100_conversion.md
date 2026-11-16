# CMP 170HX -> A100 80GB PCIe: Native Gen4 Conversion Record

Final, working recipe that turns the unlocked CMP 170HX into a genuine A100
80GB PCIe with **native PCIe Gen4 x16 and correct driver reporting** -- no
cmpunlocker PCIe patches, no config-space retrain hack, no boot service, no
closed-GSP SET_PCIE_SPEED wall. Supersedes the Gen4-retrain approach in
`cmp_gen4.md` / `cmp_gen4_retrain_analysis.md`.

Companion step-by-step: `a100-flash/README.md`. Fuse history: `cmp_burn.md`.

## Result (measured on a server-class host, card in a VM via PCI passthrough)

| Field | Before (CMP) | After (A100) |
|---|---|---|
| PCI ID | 10de:20c2 / subsys 1585 | **10de:20b5 / subsys 1533** |
| Name | NVIDIA CMP 170HX | **NVIDIA A100 80GB PCIe** |
| VBIOS | 92.00.6D.00.0A (CMP 300W) | **92.00.A0.00.03 (A100 80GB PCIe)** |
| Memory | 81920 MiB (cmpunlocker) | **81920 MiB (native)** |
| PCIe Max / Current | 3 / 3 (stale RM cache) | **4 / 4 (native, correct)** |
| Device Max / Current | 4 / 2 | **4 / 4** |
| Link width | 16x | **16x** |
| Power cap | 250/300W | **300W** |
| Reporting fix needed | yes (kernel_gsp.c:4574 hack) | **none -- native** |

`nvidia-smi -q` PCIe Generation: Max 4, Current 4, Device Current 4, Device
Max 4. `Host Max: N/A` is expected under VM passthrough (guest cannot see the
physical root port). The A100 VBIOS runs its native Gen4 DevInit; the stock
driver trains and reports Gen4 x16 directly.

Measured bandwidth (`tools/pcie_bandwidth.py`, 256 MiB pinned):
H2D 26.76 GB/s, D2H 26.67 GB/s.

## Why this works (and why the wiki "A100 cross-flash dead end" does NOT apply)

The GA100 boot gate checks the **VBIOS device ID against the FUSED device ID**.
The Consensus-Protocol wiki lists "full A100 VBIOS cross-flash" as a dead end
because every one of those attempts left the fuse at 0x20C2, so a 0x20B5 A100
ROM was rejected at the boot gate ("every cross-flash failed at that gate, not
at the write").

This recipe removes that blocker by **FF-overriding the fused device ID to
0x20B5 first** -- the same fuseless-fuse lever already used for MAGIC_D and the
Gen3/Gen4 fuses. Then the genuine, NVIDIA-signed A100 ROM matches the gate:
- The A100 ROM is validly signed (symmetric MAC, csecret(2)); we edit NO
  MAC-protected byte. We only change the fuse, not the ROM.
- Fused ID 0x20B5 == A100 ROM ID 0x20B5 -> boot gate passes.
- A100 DevInit programs the Gen4 PHY natively -> stock driver trains Gen4 x16
  and reports it correctly (the stale-cache / SET_PCIE_SPEED problems vanish
  because the A100 path never hits the CMP restrictions).

The PCIe Gen4 lock on the CMP is 5 DevInit bytes inside the MAC range; the
A100 ROM has the Gen4 version of those bytes, validly signed. Flashing the
whole A100 ROM (with the fuse matched) is how you get them without csecret(2).

Empirically on this card, after the FF override the CMP ROM still POSTs and
enumerates as 10de:20b5 (the PCI ID is strapped from the fuse), but the card
is **NOT functional** with the wrong BIOS -- it is **flashable**. That is what
enables the in-band path: normal nvflash accepts the genuine A100 ROM because
the fused ID matches it. A HW SPI flasher is recommended, not required.

## Prerequisites

- The card already unlocked via glitch + FF burns: 128 SM, 80GB HBM,
  MAGIC_D = 0x00200000 (Gen4 fuses, A100-exact). See cmp_burn.md.
  (No ECC fuse was burned; post-conversion ECC is completely unvalidated
  and untested.)
- A way to reach the FF burn window (DSOV=0, SBFE=0, EFP=1, PLMs open): your
  own VFI rig, or the bundled `ga100vfi/` / `pico-ga100vfi/` toolchains.
- nvflash64 for the in-band flash. **HW SPI flasher (CH341A + SOP8 clip)
  recommended** for a clean backup and as the recovery escape hatch -- not
  required for the conversion itself.
- The A100 ROM, sourced and verified (below).
- **A host that can allocate a 128GB 64-bit prefetchable BAR at POST** (see
  the host requirement section -- this is the main gotcha).

## The device-ID FF override (exact records, PERMANENT)

Device-ID fuses: NV_FUSE_OPT_PCIE_DEVIDA 0x8204d8 (FF chain2 block37),
NV_FUSE_OPT_PCIE_DEVIDB 0x82056c (block38). The FF block stores device_id << 1:
0x20C2 << 1 = 0x4184 (current); 0x20B5 << 1 = 0x416A (target).

FF record encoding: (data << 16) | (block << 6) | chain, chain=2,
last-record-wins per block.

| Fuse | Block | Old block data | New block data | Record burned | Row |
|---|---|---|---|---|---|
| DEVIDA | 37 | 0x4184 | 0x416A | **0x416A0942** | 311 |
| DEVIDB | 38 | 0x4184 | 0x416A | **0x416A0982** | 312 |

Burn commands (burn window: DSOV=0 -> sw_override.py -> open_wdata_plm.py):
```
sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 37 --restore 0x416A --confirm
sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 38 --restore 0x416A --confirm
```
Both printed VERIFY OK. (The tool's MAGIC_D banner lines are cosmetic; the
record low16 0x0942/0x0982 = block37/38, NOT block47/MAGIC_D.)

Block37/38 were confirmed FF-backed before burning (effective 0x4184, latest
rows 285/286). NV_FUSE_OPT_DEVID_SW_OVERRIDE_DIS (0x820584) gates software
override only; the FF burn edits the fuse sense upstream of it, so it does not
block this.

## The A100 ROM (source it yourself, then verify)

The ROM is NVIDIA proprietary firmware and is **NOT bundled** with this
release. Source the A100 80GB PCIe image (P1001 / SKU230, version
92.00.A0.00.03, 1 MiB full SPI) yourself -- e.g. the TechPowerUp VBIOS
collection -- and save it as
A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom. The copy used for
this record had SHA-256
4f0e65452a5ebf5ad8c3c5f27c683ccdc52fa8bfd1330883959bbf81d6a0602c;
match it, or run every check below before flashing. Do NOT flash an
unverified image.

Checks (all must pass):
- 1,048,576 bytes (1 MiB full SPI), magic "NVGI".
- RFRD manifest @0x2000: ver 3, pci_option_rom_offset 0x5E00,
  pci_option_rom_size 0x42400 -> MAC range 0x2200-0x44600.
- PciAt config header @0x5E74: vendor/device = 10de:20b5 (device ID @0x5e76).
  NO 0x20BB (Drive A100) or 0x20C2 anywhere -- this is the real A100 80GB PCIe,
  NOT the Drive-A100 PG199 image the wiki warns against (TPU 283106).
- InfoROM name string @0xC131C: "NVIDIA A100 80GB PCIe".
- Subsystem 0x1533 (NVGI header @0x000E). P1001 = A100 80GB PCIe board, SKU230.

## Procedure (summary; full detail in a100-flash/README.md)

0. Back up the CMP VBIOS (HW flasher read x2 compare, or nvflash64 --save).
   Keep it safe.
1. Burn window -> FF-override block37/38 to 0x416A (records above). VERIFY OK
   on both.
2. FULL power off, cold boot. The card POSTs and enumerates as 10de:20b5
   (A100) with the CMP VBIOS still on it: NOT functional, but flashable.
3. Flash the A100 ROM: normal in-band nvflash64 (the fused ID now matches the
   image), or HW flasher with the host off. Read back, verify byte-identical.
4. Cold boot in a host that can map the 128GB BAR. Verify:
   lspci -nn -d 10de:  -> 10de:20b5
   nvidia-smi          -> NVIDIA A100 80GB PCIe, 81920 MiB
   nvidia-smi -q       -> PCIe Generation Max/Current 4, width 16x

## Host requirement: the 128GB BAR gotcha (IMPORTANT)

The A100 80GB VBIOS declares a **128GB 64-bit prefetchable BAR1 at POST**. The
host BIOS must allocate it during POST. This is why cmpunlocker worked on weak
hosts but the A100 VBIOS does not: cmpunlocker resizes BAR1 at RUNTIME (driver
probe, bar1-resize-unlock.patch) from a small initial BAR, so the host only
allocates a small BAR at POST. The A100 VBIOS needs the full 128GB up front.

Observed host compatibility with the A100 VBIOS:

| Host (CPU / board) | Result |
|---|---|
| Intel i3-12100 / ASUS H610M-C | NO POST, no image |
| AMD R5 8500G / Gigabyte B850M Force | POST, card not detected |
| AMD R9 9950X3D / ASUS X870E Apex | POST, card fully working |
| AMD R5 9600X / ASUS B850 ProArt | POST, card fully working |

Required host BIOS settings:
- Above 4G Decoding = Enabled (mandatory).
- CSM = Disabled (pure UEFI; large 64-bit BARs need UEFI).
- Re-Size BAR Support = Enabled (if present).
- MMIO High Base / MMIO High Size >= 128GB (if the board exposes them).
A board with a small fixed MMIO window cannot host the A100 80GB VBIOS, period.

## Recovery (no brick path)

The FF device-ID burn is permanent (reversible only by appending restore
records in a fresh burn window). With the CMP ROM still on the card the host
POSTs (card identifies as 20b5, not functional), so the way back is staged:

1. A100 ROM not flashed yet: reverse the FF burn in a burn window (commands
   below), cold boot -> fused 0x20C2 + CMP ROM -> functional stock CMP.
   No flasher needed.
2. A100 ROM flashed, card boots: write the CMP backup back (HW flasher is
   cleanest -- no ID checks; in-band nvflash may need an ID override while the
   fuse is still 0x20B5), then reverse the FF burn, cold boot.
3. A100 ROM flashed, host fails to POST (128GB BAR limit): HW flasher write
   the CMP backup -- its SMALL BAR POSTs even with the fused ID at 0x20B5 --
   then reverse the FF burn, cold boot. This is the case the HW flasher is
   recommended for.

Reverse-burn commands:
```
sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 37 --restore 0x4184 --confirm
sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 38 --restore 0x4184 --confirm
```

Keep the CMP ROM backup AND these reverse-burn commands; together they are the
way back. The card was never bricked during this work -- the POST failures were
host BAR-allocation limits, not card damage.

## Post-conversion notes

- PCIe is fully native; no cmpunlocker PCIe patches or retrain boot service
  needed. Those approaches (cmp_gen4.md, cmp_gen4_retrain_analysis.md,
  gen4-debug/) are superseded.
- Compute throughput: the A100 VBIOS does NOT change the SM_SPEED_SELECT FMA
  throttle fuse. Full FP32 FMA speed REQUIRES cmpunlocker-a100-minimal's
  always-on SM_SPEED override even with the A100 BIOS; without it FP32 stays
  throttled (~half). Independent of PCIe.
- SM count: native A100 GSP floorsweep = 124 SM. The FF burns enable all
  TPCs, but 124 -> 128 requires cmpunlocker-a100-minimal --sm-unlock
  (volatile pre-boot override). Without the driver flag the card is 124 SM.
- ECC: no ECC fuse was burned. Pre-conversion ECC was the cmpunlocker volatile
  software override; post-conversion ECC is COMPLETELY UNVALIDATED AND
  UNTESTED (the A100 InfoROM carries the ECC object, but nothing proves it
  works). Do not rely on it.
- Fuse-set completeness: passes 1-6 are a working subset, not a proven-
  complete fuse map -- see cmp_burn.md "Remaining work / known limitations".
- The conversion is permanent (fuse + flash) and survives reboots with no
  maintenance. Validate sustained load + AER/Xid watch before production trust.

## Files

| File | Purpose |
|---|---|
| a100-flash/README.md | full conversion + recovery procedure |
| A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom | target filename for the sourced + verified A100 image (NOT bundled) |
| cmp_burn.md | prior fuse burns (SM/HBM/Gen4/NVLink/DEVID) |
| cmp_gen4.md / cmp_gen4_retrain_analysis.md | superseded Gen4-retrain research |

## References

- Consensus-Protocol/cmp170hx wiki: docs/hardware/vbios.md (boot gate = Board ID
  + fused device ID; MAC range; the A100 cross-flash dead end this recipe avoids
  by FF-overriding the fuse).
- amoghmunikote GA100 VBIOS Comparison Table gist (ROM inventory, MAC, device
  IDs; "cross-flash fails on device ID mismatch").
- Community reports: device-ID FF override + genuine A100 ROM yields native
  PCIe (the method validated here).
- Local: tools/ff_magic_derive.py, tools/ff_burn.py (FF burn tooling).
