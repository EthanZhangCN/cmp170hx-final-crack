# CMP 170HX -> A100 80GB PCIe (device-ID FF override + VBIOS flash)

Convert the card to a genuine A100 80GB PCIe identity so the stock A100 VBIOS
runs its native Gen4 DevInit and the driver trains/reports PCIe normally --
no closed-GSP SET_PCIE_SPEED wall, no stale-cache reporting hack.

Target identity: device 10de:20b5, subsystem 10de:1533, "NVIDIA A100 80GB PCIe".
Target ROM: A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom
  (verified: 1 MiB full SPI, NVGI magic, device 0x20B5 in PciAt @0x5e76,
   RFRD MAC range 0x2200-0x44600, InfoROM name "NVIDIA A100 80GB PCIe").
  NOT bundled: NVIDIA proprietary firmware. Source it yourself (e.g. the
  TechPowerUp VBIOS collection: A100 80GB PCIe, P1001/SKU230, 92.00.A0.00.03)
  and save it under that filename. The copy used for this record had SHA-256
  4f0e65452a5ebf5ad8c3c5f27c683ccdc52fa8bfd1330883959bbf81d6a0602c --
  match it or run every check in cmp_a100_conversion.md before flashing.

## Why this works (and why the wiki "dead end" does not apply)

The boot gate checks the VBIOS device ID against the FUSED device ID. The
wiki's failed A100 cross-flashes left the fuse at 0x20C2, so a 0x20B5 A100 ROM
was rejected at boot. Here we FF-override the fused device ID to 0x20B5 FIRST
(the same fuseless-fuse lever used for MAGIC_D / Gen3 / Gen4), so the genuine
signed A100 ROM matches the gate. The A100 ROM is validly signed by NVIDIA, so
its MAC passes; we are NOT editing any MAC-protected byte.

Empirically on this card, after the FF override the CMP ROM still POSTs and
enumerates as 10de:20b5 (the PCI ID is strapped from the fuse), but the card
is **NOT functional** with the wrong BIOS -- it is **flashable**. That is what
makes the in-band nvflash path below possible: nvflash accepts the A100 image
because the card's (fused) ID matches it. **A HW SPI flasher is recommended,
not required:** the forward conversion works fully in-band.

## CRITICAL: recovery nuance (read before burning)

The FF device-ID burn is PERMANENT (reversible only by appending restore
records in a fresh burn window). Once block37/38 = 0x416A (fused 0x20B5):
  - The A100 ROM (0x20B5) boots natively and the card is fully functional.
  - The CMP ROM (0x20C2) still POSTs, but the card identifies as 20b5 and is
    NOT functional -- only flashable.
To return to a functional CMP you must reverse the FF burn (block37/38 back
to 0x4184) in a burn window; if the A100 ROM was already flashed, reflash the
CMP backup as well. Keep the CMP ROM backup AND the reverse-burn commands
below. Do not lose either. The HW flasher is the clean escape hatch (no ID
checks, works even when the host cannot POST the A100 VBIOS).

## Prerequisites

- A way to reach the FF burn window (DSOV=0, SBFE=0, EFP=1, PLMs open): your
  own VFI rig, or the bundled `ga100vfi/` / `pico-ga100vfi/` toolchains.
- nvflash64 (Linux) for the in-band flash. **HW SPI flasher (CH341A + SOP8
  clip) recommended** for a clean backup and recovery; not mandatory.
- The sourced, verified A100 ROM (above) on the host.
- A CMP stock ROM backup (Phase 0). The card currently runs 92.00.6D.00.0A.
- A host that can allocate the 128GB 64-bit prefetchable BAR at POST for the
  A100 VBIOS. Measured: i3-12100/ASUS H610M-C = NO POST; R5 8500G/Gigabyte
  B850M Force = POST but card not detected; R9 9950X3D/ASUS X870E Apex and
  R5 9600X/ASUS B850 ProArt = fully working. Full list + BIOS settings:
  cmp_a100_conversion.md ("Host requirement").
- Card cooled; cooler access for the clip if using the HW flasher.

## Phase 0 -- back up the current VBIOS

Preferred: HW flasher read of the SPI chip (two reads, compare):
  flashrom -p ch341a_spi -r cmp_backup_1.rom
  flashrom -p ch341a_spi -r cmp_backup_2.rom
  cmp cmp_backup_1.rom cmp_backup_2.rom && echo "clip contact good"
Keep this file safe. It is your CMP image for recovery.

Alternative (no HW flasher): nvflash64 --save cmp_backup.rom with the driver
bound. It consumes the Falcon window and needs the driver, so the HW read is
cleaner when available -- but the save is a valid backup.

## Phase 1 -- FF-override the device ID (burn window)

Establish the burn window with your VFI setup (bundled ga100vfi/ or
pico-ga100vfi/, or your own rig), then:
  DSOV=0 -> sw_override.py (EN_SW_OVERRIDE 0x820040=1)
         -> open_wdata_plm.py (0x82048C=0)

Read-only sanity (current fused device ID shadows):
  sudo python3 -c "import mmap,os,struct; m=mmap.mmap(os.open('/sys/bus/pci/devices/0000:01:00.0/resource0',os.O_RDONLY),0x822000,prot=mmap.PROT_READ); print('DEVIDA=0x%08X'%struct.unpack_from('<I',m,0x8204d8)[0]); print('DEVIDB=0x%08X'%struct.unpack_from('<I',m,0x82056c)[0])"
  # expect 0x000020C2 for both before the burn

Burn DEVIDA (block37) then DEVIDB (block38). DRY-RUN first; auto-picks a blank
row; guarded; type BURN to commit. Each is IRREVERSIBLE.

  sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 37 --restore 0x416A
  sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 37 --restore 0x416A --confirm
  sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 38 --restore 0x416A
  sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 38 --restore 0x416A --confirm

  (The tool's MAGIC_D banner lines are irrelevant here; the meaningful lines are
   "block37 effective", "planned: block37 ... -> 0x416A", and
   "record : row N = 0x416A0942" / "0x416A0982". VERIFY OK must print for each.)

Fixed-row equivalent (rows 311/312 are THIS card's record; on your die pick
blank rows yourself -- ff_burn aborts if the target row is not blank):
  sudo python3 tools/ff_burn.py --bdf 0000:01:00.0 --row <blank> --value 0x416A0942 --confirm
  sudo python3 tools/ff_burn.py --bdf 0000:01:00.0 --row <blank2> --value 0x416A0982 --confirm

After both VERIFY OK: FULL power off, then cold boot. The card POSTs and
enumerates as 10de:20b5 (A100) with the CMP VBIOS still on it. It is NOT
functional in this state (wrong BIOS for the fused ID) -- but it IS flashable.
Expected, not a failure.

## Phase 2 -- flash the A100 ROM

Option A -- in-band nvflash (no HW flasher needed). The fused ID now matches
the A100 image, so normal nvflash accepts it:
  sudo nvflash64 A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom
  # read back and verify:
  sudo nvflash64 --save a100_readback.rom
  cmp a100_readback.rom A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom && echo "flash verified"

Option B -- HW flasher (host powered off):
  flashrom -p ch341a_spi -w A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom
  # read back and verify:
  flashrom -p ch341a_spi -r a100_readback.rom
  cmp a100_readback.rom A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom && echo "flash verified"
  Re-enable SPI write protection before power-on (per the wiki: leaving it off
  causes 0xBADF3000 / RM init failure).

## Phase 3 -- cold boot and verify

Power on. The fused ID (0x20B5) now matches the A100 VBIOS (0x20B5).
  lspci -nn -d 10de:                      # expect 10de:20b5 (A100 80GB PCIe)
  nvidia-smi                              # expect "NVIDIA A100 80GB PCIe", 81920 MiB
  nvidia-smi -q | grep -A5 "PCIe Generation"   # expect Max/Current 4 (native)
  setpci -s 01:00.0 CAP_EXP+12.w          # LnkSta 0x1104 = Gen4 x16
  cat /sys/bus/pci/devices/0000:01:00.0/current_link_speed   # 16.0 GT/s
  python3 tools/pcie_bandwidth.py         # ~26.7 GB/s H2D/D2H (256 MiB pinned)

If it enumerates as A100 and trains Gen4 natively, the conversion succeeded and
the reporting is correct (the A100 DevInit + stock driver path, no RM hack).

## Recovery (if Phase 3 fails to boot / wrong ID / want CMP back)

1. A100 ROM NOT flashed yet (fuse 0x20B5, CMP ROM on card, POSTs but not
   functional): reverse the FF burn in a burn window, cold boot -> fused
   0x20C2 + CMP ROM -> functional stock CMP. No flasher needed:
     sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 37 --restore 0x4184 --confirm
     sudo python3 tools/ff_magic_derive.py --bdf 0000:01:00.0 --block 38 --restore 0x4184 --confirm
2. A100 ROM flashed, card boots: write the CMP backup back (HW flasher is
   cleanest -- no ID checks; in-band nvflash may need an ID override while the
   fuse is still 0x20B5), THEN reverse the FF burn (commands above), cold boot.
3. A100 ROM flashed, host fails to POST (128GB BAR limit): HW flasher write
   the CMP backup -- its SMALL BAR POSTs even with the fused ID at 0x20B5 --
   then reverse the FF burn, cold boot. This is the case the HW flasher is
   recommended for.

If the burn window cannot be opened on a non-booting card, the HW flasher can
still restore the CMP ROM, but the fused ID remains 0x20B5 until an FF reverse
burn is possible. This is why getting Phase 1 right (VERIFY OK on both) matters.

## Post-conversion notes

- The stock NVIDIA driver supports A100 (0x20B5); no driver hacks are needed
  for PCIe/memory. But full FP32 FMA speed REQUIRES cmpunlocker-a100-minimal's
  always-on SM_SPEED override even with the A100 BIOS (the SM_SPEED_SELECT
  throttle fuse is unchanged by the conversion), and 128 SM requires its
  --sm-unlock flag: the A100 GSP floorsweeps to 124 SM natively.
- ECC: no ECC fuse was burned on this card. Post-conversion ECC is COMPLETELY
  UNVALIDATED AND UNTESTED (pre-conversion ECC was a volatile cmpunlocker
  software override). Do not rely on it.
- NVLink: fuse burned, bring-up never tested (needs U11 mod + ROM-check
  bypass). The FF burn set is a working subset, not a proven-complete fuse
  map -- see cmp_burn.md "Remaining work / known limitations".
- Power: A100 80GB PCIe TDP 300W; the card already runs the 300W power wall.
  Keep cooling proven before sustained load.

## Files

| File | Purpose |
|---|---|
| A100_80GB_PCIe_P1001_SKU230_92.00.A0.00.03_full_spi.rom | target filename for the sourced + verified A100 image (NOT bundled) |
| this README | full conversion + recovery procedure |
