#!/usr/bin/env python3
"""Gen4 fuse hypothesis test (VOLATILE, glitch window only).

Tests whether clearing PCIE_MAGIC_BITS_D bits {19,22,26,28} (MAGIC_D ->
0x00200000, the A100 reference value) enables Gen4. Hypothesis: those four
bits are the remaining Gen4 fuses (Amogh: "Gen4 needs four fuses"; only bit25
was burned). The A100 value is community-measured (JRex table / Consensus wiki);
the bit->Gen4 mapping is a HYPOTHESIS this test verifies.

REQUIRES the glitch window (DSOV=0). Refuses to run on a normal boot (DSOV=1,
where MAGIC_D writes are gated). Everything here is VOLATILE -- a power cycle
restores the burned fuse values. Dry-run by default; --confirm to write.

Procedure after glitching to the burn-ready state:
  python3 pcie_gen4_fuse_test.py --bdf 0000:01:00.0            # dry-run, check DSOV
  python3 pcie_gen4_fuse_test.py --bdf 0000:01:00.0 --confirm  # write MAGIC_D
  # then lift the cap + try RM Gen4:
  python3 pcie_gen4_probe.py --bdf 0000:01:00.0 --confirm
  /usr/local/sbin/cmp-pcie-speed 4 0 01
  setpci -s 01:00.0 CAP_EXP+12.w                               # 0x1104 = Gen4 x16
"""
import argparse
import mmap
import os
import struct

MAGIC_D = 0x820520
DSOV = 0x820084
EN_SW = 0x820040
A100_MAGIC_D = 0x00200000


def rd(m, o):
    return struct.unpack_from("<I", m, o)[0]


def wr(m, o, v):
    struct.pack_into("<I", m, o, v & 0xFFFFFFFF)


def main():
    ap = argparse.ArgumentParser(description="Gen4 MAGIC_D fuse test (glitch window, volatile)")
    ap.add_argument("--bdf", default="0000:01:00.0")
    ap.add_argument("--confirm", action="store_true", help="actually write (default dry-run)")
    args = ap.parse_args()

    res = "/sys/bus/pci/devices/%s/resource0" % args.bdf
    fd = os.open(res, os.O_RDWR if args.confirm else os.O_RDONLY)
    m = mmap.mmap(fd, 0x822000, prot=mmap.PROT_READ | (mmap.PROT_WRITE if args.confirm else 0))

    dsov = rd(m, DSOV) & 1
    magic = rd(m, MAGIC_D)
    print("=== Gen4 MAGIC_D fuse test (VOLATILE) ===")
    print("  DSOV(0x820084) bit0 = %d   %s" % (dsov, "GLITCH WINDOW OK" if dsov == 0 else "NOT glitched"))
    print("  MAGIC_D(0x820520)   = 0x%08X" % magic)
    print("  A100 target         = 0x%08X  (clears bits %s)"
          % (A100_MAGIC_D, ",".join(str(b) for b in range(32) if (magic >> b) & 1 and not (A100_MAGIC_D >> b) & 1)))

    if dsov != 0:
        print("\n  REFUSING: DSOV=1 (normal boot). MAGIC_D writes are gated here.")
        print("  Glitch to the burn-ready state (DSOV=0) first, then re-run.")
        m.close(); os.close(fd)
        return 1

    if not args.confirm:
        print("\n  DRY-RUN: nothing written. Re-run with --confirm in the glitch window.")
        m.close(); os.close(fd)
        return 0

    print("\n  enabling SW override + writing MAGIC_D:")
    wr(m, EN_SW, 1)
    print("    EN_SW_OVERRIDE(0x820040) = 0x%08X" % rd(m, EN_SW))
    wr(m, MAGIC_D, A100_MAGIC_D)
    after = rd(m, MAGIC_D)
    print("    MAGIC_D 0x%08X -> 0x%08X  %s"
          % (magic, after, "STUCK (write landed)" if after == A100_MAGIC_D else "GATED (write ignored)"))
    print("    LnkCap(0x88084) = 0x%08X (nibble %d)" % (rd(m, 0x88084), rd(m, 0x88084) & 0xF))
    m.close(); os.close(fd)

    if after == A100_MAGIC_D:
        print("\n  MAGIC_D write landed. Next: lift cap + try RM Gen4:")
        print("    python3 pcie_gen4_probe.py --bdf %s --confirm" % args.bdf)
        print("    /usr/local/sbin/cmp-pcie-speed 4 0 01")
        print("    setpci -s %s CAP_EXP+12.w   # 0x1104 = Gen4 x16" % args.bdf)
        print("  If RM Gen4 trains -> the 4 bits are Gen4 fuses; derive permanent FF burns.")
        print("  If still 0x56 -> RM Gen4 is firmware-hardcoded (GSP patch territory).")
    else:
        print("\n  MAGIC_D write ignored even at DSOV=0 -> needs the _CP shadow path.")
    print("\n  VOLATILE: power cycle restores burned values. Re-glitch each session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
