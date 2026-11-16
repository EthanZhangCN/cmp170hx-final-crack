#!/usr/bin/env python3
"""Read-only PCIe PHY probe for GA100/CMP 170HX. WRITES NOTHING.

Reads the physical-layer config registers that govern actual link training
(below the fuse-driven config-space capability). The FF burns clear the
OPTION-word gates (LnkCap / Device Max), but research documents a SECOND
Gen3 disable in PL_CYA_0 (0x8c2c0) set by IFF instructions at early boot:

  "Gen3 full unlock = FF permanent fix + XVE_FUSE_OVERRIDE for BOOT bits
   + IFF/CYA_0 second disable handling. Gen4 adds more."
  (cmprir/ga100-fuse-re/artifacts/external-fuse-intel-analysis-sep23.md)

This probe dumps those registers so we can see which PHY gates are still
set. Run on the host:  sudo python3 pcie_phy_probe.py [--bdf 0000:01:00.0]
"""
import argparse
import mmap
import os
import struct
import subprocess


def rd(m, off):
    return struct.unpack_from("<I", m, off)[0]


def setpci(bdf, reg):
    try:
        out = subprocess.check_output(["setpci", "-s", bdf, reg],
                                      stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return "?"


def main():
    ap = argparse.ArgumentParser(description="Read-only PCIe PHY probe")
    ap.add_argument("--bdf", default="0000:01:00.0")
    args = ap.parse_args()
    bdf = args.bdf

    res = "/sys/bus/pci/devices/%s/resource0" % bdf
    fd = os.open(res, os.O_RDONLY)
    m = mmap.mmap(fd, 0x90000, prot=mmap.PROT_READ)

    print("=== PCIe config space (setpci) ===")
    for name, reg in [("LnkCap ", "CAP_EXP+0C.w"), ("LnkCtl ", "CAP_EXP+10.w"),
                      ("LnkSta ", "CAP_EXP+12.w"), ("LnkCtl2", "CAP_EXP+30.w"),
                      ("LnkSta2", "CAP_EXP+32.w")]:
        print("  %s %s" % (name, setpci(bdf, reg)))
    try:
        parent = os.path.basename(os.path.dirname(os.path.realpath(
            "/sys/bus/pci/devices/%s" % bdf)))
        print("  upstream bridge: %s" % parent)
        for name, reg in [("LnkCap ", "CAP_EXP+0C.w"),
                          ("LnkCtl2", "CAP_EXP+30.w"),
                          ("LnkSta ", "CAP_EXP+12.w")]:
            print("    bridge %s %s" % (name, setpci(parent, reg)))
    except Exception:
        pass

    print("\n=== PHY / XVE registers (BAR0, read-only) ===")
    regs = [
        ("LINK_CAP (int)   ", 0x88084),
        ("LINK_CAP2        ", 0x880A4),
        ("LINK_CTRL_2      ", 0x880A8),
        ("LINK_CTRL_STATUS ", 0x88088),
        ("PRIV_MISC_1      ", 0x8841C),
        ("VSEC_DEVICE      ", 0x8860C),
        ("VSEC_HIERARCHY   ", 0x88610),
        ("XVE_FUSE_OVERRIDE", 0x8872C),
        ("LINK_CONFIG_0    ", 0x8C040),
        ("PL_LINK_RATE     ", 0x8C1C0),
        ("PL_CYA_0         ", 0x8C2C0),
        ("XP3G_STATUS_0    ", 0x8E100),
        ("XP3G_OVR_0       ", 0x8E110),
        ("XP3G_VAL_0       ", 0x8E120),
        ("XP3G_STATUS_3    ", 0x8E10C),
        ("XP3G_OVR_3       ", 0x8E11C),
        ("XP3G_VAL_3       ", 0x8E12C),
    ]
    vals = {}
    for name, off in regs:
        v = rd(m, off)
        vals[off] = v
        print("  %s 0x%05X = 0x%08X" % (name, off, v))

    print("\n=== decodes ===")
    lc = vals[0x8C040]
    mr = (lc >> 18) & 3
    print("  LINK_CONFIG_0 MAX_RATE (bits18-19) = %d   (REVERSED: 0=Gen4 1=Gen3 2=Gen2 3=Gen1)" % mr)
    cya = vals[0x8C2C0]
    print("  PL_CYA_0 bit2 DIS_G2 = %d" % ((cya >> 2) & 1))
    print("  PL_CYA_0 set bits: %s"
          % " ".join(str(b) for b in range(32) if (cya >> b) & 1))
    plr = vals[0x8C1C0]
    print("  PL_LINK_RATE low16 set bits: %s"
          % " ".join(str(b) for b in range(16) if (plr >> b) & 1))
    print("  PL_LINK_RATE hi16 set bits: %s"
          % " ".join(str(b + 16) for b in range(16) if (plr >> (b + 16)) & 1))
    xve = vals[0x8872C]
    print("  XVE_FUSE_OVERRIDE = 0x%X  (intel: 0xA spoofs BOOT_GEN23/GEN3)" % xve)
    m.close()
    os.close(fd)
    print("\nREAD-ONLY: nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
