#!/usr/bin/env python3
"""Experimental PCIe PHY enable for GA100/CMP 170HX. MANUAL host tool.

The FF burns clear the option/advertisement plane (LnkCap Gen3/4) but IFF
re-writes the PHY training plane (CYA_0, via IFF row 490 = 0x008C2C3C) at
every boot, so the link never trains above Gen1. This tool applies the
host-writable subset of the proven Gen2 PHY recipe (cmprir/cmpunlocker-
v0.5-oc/driver/patches/pcie-gen2.patch), adapted for Gen3, then retrains.

ALL WRITES ARE VOLATILE (lost on power cycle). A bad value can hang the
link -- recover with a cold boot. Run manually; nothing here is automatic.

Strategy:
  1. --gen2  : apply the known Gen2 recipe subset, retrain to Gen2.
               If this trains Gen2, the PHY-enable path works on this die.
  2. --gen3  : MAX_RATE=3, XVE=0xA, retrain to Gen3. Iterate CYA_0 Gen3
               candidate bits with --cya-clear (bit numbers undocumented).

The privileged XP3G / PRIV_MISC_1 / VSEC writes from the full Gen2 recipe
need the SEC2 booter (or opened PLMs); this tool reports which host writes
stick so we can see what is PLM-gated. Readback is verified after each write.

Usage:
  sudo python3 pcie_phy_enable.py --bdf 0000:01:00.0 --gen2            # dry-run
  sudo python3 pcie_phy_enable.py --bdf 0000:01:00.0 --gen2 --confirm  # write+retrain
  sudo python3 pcie_phy_enable.py --bdf 0000:01:00.0 --gen3 --confirm
  sudo python3 pcie_phy_enable.py --bdf 0000:01:00.0 --gen3 --cya-clear 12,13 --confirm
"""
import argparse
import mmap
import os
import struct
import subprocess
import time

XVE_FUSE_OVERRIDE = 0x8872C
LINK_CONFIG_0 = 0x8C040
PL_CYA_0 = 0x8C2C0
PL_LINK_RATE = 0x8C1C0
LINK_CTRL_2 = 0x880A8


def rd(m, off):
    return struct.unpack_from("<I", m, off)[0]


def wr(m, off, val):
    struct.pack_into("<I", m, off, val & 0xFFFFFFFF)


def setpci(bdf, reg, val=None):
    cmd = ["setpci", "-s", bdf, reg]
    if val is not None:
        cmd[-1] = "%s=%s" % (reg, val)
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception as e:
        return "ERR:%s" % e


def bridge_of(bdf):
    try:
        return os.path.basename(os.path.dirname(os.path.realpath(
            "/sys/bus/pci/devices/%s" % bdf)))
    except Exception:
        return None


def write_verify(m, name, off, val):
    before = rd(m, off)
    wr(m, off, val)
    after = rd(m, off)
    ok = (after == (val & 0xFFFFFFFF))
    print("  %-18s 0x%05X: 0x%08X -> 0x%08X (want 0x%08X) %s"
          % (name, off, before, after, val, "OK" if ok else "GATED/NO-OP"))
    return ok


def main():
    ap = argparse.ArgumentParser(description="Experimental PCIe PHY enable (volatile)")
    ap.add_argument("--bdf", default="0000:01:00.0")
    ap.add_argument("--gen2", action="store_true", help="target Gen2 (known recipe)")
    ap.add_argument("--gen3", action="store_true", help="target Gen3 (experimental)")
    ap.add_argument("--max-rate", type=int, default=0, help="override MAX_RATE/target (1/2/3); decouples from --gen2/--gen3")
    ap.add_argument("--xve", default="", help="override XVE_FUSE_OVERRIDE value (e.g. 0x6 or 0xA)")
    ap.add_argument("--cya-clear", default="", help="comma list of CYA_0 bits to clear (Gen3 experiment)")
    ap.add_argument("--pl-rate", default="", help="optional PL_LINK_RATE value (e.g. 0x00240036)")
    ap.add_argument("--no-retrain", action="store_true", help="write regs only, skip retrain")
    ap.add_argument("--confirm", action="store_true", help="actually write (default dry-run)")
    args = ap.parse_args()

    gen = args.max_rate if args.max_rate else (3 if args.gen3 else 2)
    # NVIDIA LINK_CONFIG_0 MAX_LINK_RATE[19:18] is REVERSED: 0=Gen4,1=Gen3,2=Gen2,3=Gen1
    # (config-space LnkCtl2 target uses the NORMAL encoding 1..4). Source: cmp.wiki / ropuls.
    max_rate_enc = {1: 3, 2: 2, 3: 1, 4: 0}.get(gen, gen)
    xve_val = int(args.xve, 0) if args.xve else (0xA if gen >= 3 else 0x6)
    br = bridge_of(args.bdf)
    print("=== PCIe PHY enable: target Gen%d, bdf=%s bridge=%s ===" % (gen, args.bdf, br))
    print("VOLATILE writes; cold boot restores. %s" % ("DRY-RUN" if not args.confirm else "WRITING"))

    res = "/sys/bus/pci/devices/%s/resource0" % args.bdf
    fd = os.open(res, os.O_RDWR if args.confirm else os.O_RDONLY)
    m = mmap.mmap(fd, 0x90000, prot=mmap.PROT_READ | (mmap.PROT_WRITE if args.confirm else 0))

    cya = rd(m, PL_CYA_0)
    lc = rd(m, LINK_CONFIG_0)
    print("\n  current: CYA_0=0x%08X LINK_CONFIG_0=0x%08X XVE=0x%08X PL_RATE=0x%08X"
          % (cya, lc, rd(m, XVE_FUSE_OVERRIDE), rd(m, PL_LINK_RATE)))

    new_lc = (lc & ~0x000C0000) | (max_rate_enc << 18)
    new_cya = cya & ~(1 << 2)  # DIS_G2 (known)
    if args.cya_clear:
        if args.cya_clear.strip().lower() == "all":
            new_cya = 0
        else:
            for b in args.cya_clear.split(","):
                b = b.strip()
                if b:
                    new_cya &= ~(1 << int(b, 0))
    new_lc2 = (rd(m, LINK_CTRL_2) & ~0xF) | gen

    print("\n  planned writes:")
    print("    XVE_FUSE_OVERRIDE = 0x%X" % xve_val)
    print("    LINK_CONFIG_0     = 0x%08X (MAX_RATE enc=%d -> Gen%d)" % (new_lc, max_rate_enc, gen))
    print("    CYA_0             = 0x%08X (clear bit2%s)" % (new_cya,
          (" + bits " + args.cya_clear) if args.cya_clear else ""))
    print("    LINK_CTRL_2(int)  = 0x%08X (target=%d)" % (new_lc2, gen))
    if args.pl_rate:
        print("    PL_LINK_RATE      = %s" % args.pl_rate)

    if not args.confirm:
        print("\n  DRY-RUN: nothing written. Re-run with --confirm.")
        m.close(); os.close(fd)
        return 0

    print("\n  writing (readback verified):")
    write_verify(m, "XVE_FUSE_OVERRIDE", XVE_FUSE_OVERRIDE, xve_val)
    write_verify(m, "LINK_CONFIG_0", LINK_CONFIG_0, new_lc)
    write_verify(m, "CYA_0", PL_CYA_0, new_cya)
    write_verify(m, "LINK_CTRL_2", LINK_CTRL_2, new_lc2)
    if args.pl_rate:
        write_verify(m, "PL_LINK_RATE", PL_LINK_RATE, int(args.pl_rate, 0))
    m.close(); os.close(fd)

    if args.no_retrain:
        print("\n  --no-retrain: regs written, skipping link retrain.")
        return 0

    print("\n  retrain to Gen%d (config space, from bridge):" % gen)
    tgt = "0x%x" % gen
    print("    gpu  LnkCtl2 <- %s : %s" % (tgt, setpci(args.bdf, "CAP_EXP+30.w", tgt)))
    if br:
        print("    br   LnkCtl2 <- %s : %s" % (tgt, setpci(br, "CAP_EXP+30.w", tgt)))
        ctl = setpci(br, "CAP_EXP+10.w")
        try:
            newctl = "0x%04x" % (int(ctl, 16) | 0x20)
        except ValueError:
            newctl = "0x0020"
        print("    br   LnkCtl  <- %s : %s" % (newctl, setpci(br, "CAP_EXP+10.w", newctl)))
    lost = False
    for i in range(10):
        time.sleep(0.3)
        sta = setpci(args.bdf, "CAP_EXP+12.w")
        if sta.startswith("ERR:") or not sta:
            lost = True
            break
        try:
            speed = int(sta, 16) & 0xF
        except ValueError:
            speed = 0
        if speed >= gen:
            print("    LnkSta=%s -> Gen%d TRAINED" % (sta, speed))
            return 0
    if lost:
        print("    LnkSta UNREADABLE -- GPU fell off the bus during retrain.")
        print("    COLD BOOT (power cycle) to recover; writes were volatile.")
        return 1
    sta = setpci(args.bdf, "CAP_EXP+12.w")
    try:
        speed = int(sta, 16) & 0xF
    except ValueError:
        speed = 0
    print("    LnkSta=%s -> still Gen%d (retrain ineffective or PHY/channel block)" % (sta, speed))
    print("\n  If GATED/NO-OP above: those regs need the SEC2 booter / opened PLMs.")
    print("  If writes OK but no train: may need boot-window catch (firmware re-clamps),")
    print("  or the privileged XP3G/PRIV_MISC_1/VSEC writes, or Gen3 CYA bits.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
