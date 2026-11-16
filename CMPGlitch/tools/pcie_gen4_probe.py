#!/usr/bin/env python3
"""E1 Gen4 probe (research: cmp_gen4.md / Consensus-Protocol wiki).

Applies the A100 Gen4 PHY init state from a working Gen3 base, then optionally
RM-trains to Gen4. The key untried lever is the Gen4 DevInit config registers
0x88CE0 / 0x88CE4 (the A100 'Physical Layer 16.0 GT/s' init block, suppressed on
CMP). Also sets CYA_0 to the A100 value (clears bits 0,2,13,23 -- NOT bit 4),
PL_LINK_RATE Gen4 (0x00040036), and LINK_CONFIG_0 MAX_RATE=0 (reversed: Gen4).

ALL WRITES VOLATILE -- cold boot restores the Gen3 baseline. Dry-run by default.
Single-pass training, no retry loop. If the GPU drops off the bus, STOP and cold
boot; do not re-run in a loop.

Run from a trained Gen3 x16 state (cmp-gen3.sh first):
  python3 pcie_gen4_probe.py --bdf 0000:01:00.0                 # dry-run
  python3 pcie_gen4_probe.py --bdf 0000:01:00.0 --confirm       # write + readback
  python3 pcie_gen4_probe.py --bdf 0000:01:00.0 --confirm --train  # + RM Gen4
"""
import argparse
import mmap
import os
import re
import struct
import subprocess
import time

A100_CYA0 = 0x060711B2
A100_PL_RATE = 0x00040036
A100_88CE4 = 0x00000014


def rd(m, o):
    return struct.unpack_from("<I", m, o)[0]


def wr(m, o, v):
    struct.pack_into("<I", m, o, v & 0xFFFFFFFF)


def setpci(bdf, reg, val=None):
    cmd = ["setpci", "-s", bdf, reg]
    if val is not None:
        cmd[-1] = "%s=%s" % (reg, val)
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
    except Exception as e:
        return "ERR:%s" % e


def wverify(m, name, off, val):
    before = rd(m, off)
    wr(m, off, val)
    after = rd(m, off)
    ok = after == (val & 0xFFFFFFFF)
    print("  %-16s 0x%05X: 0x%08X -> 0x%08X (want 0x%08X) %s"
          % (name, off, before, after, val, "OK" if ok else "GATED/NO-OP"))
    return ok


def detect_minor_bus(bdf):
    minor, bus = None, bdf[5:7]
    try:
        info = open("/proc/driver/nvidia/gpus/%s/information" % bdf).read()
        mm = re.search(r"Device Minor\s*:\s*(\d+)", info)
        if mm:
            minor = mm.group(1)
    except Exception:
        pass
    return minor, bus


def main():
    ap = argparse.ArgumentParser(description="E1 Gen4 probe (volatile)")
    ap.add_argument("--bdf", default="0000:01:00.0")
    ap.add_argument("--confirm", action="store_true", help="actually write (default dry-run)")
    ap.add_argument("--train", action="store_true", help="after writes, RM staged train to Gen4")
    args = ap.parse_args()

    res = "/sys/bus/pci/devices/%s/resource0" % args.bdf
    fd = os.open(res, os.O_RDWR if args.confirm else os.O_RDONLY)
    m = mmap.mmap(fd, 0x90000, prot=mmap.PROT_READ | (mmap.PROT_WRITE if args.confirm else 0))

    print("=== E1 Gen4 probe: bdf=%s  %s ===" % (args.bdf, "WRITING" if args.confirm else "DRY-RUN"))
    print("VOLATILE -- cold boot restores Gen3. Single-pass; stop if GPU drops.\n")

    cya = rd(m, 0x8C2C0)
    lc0 = rd(m, 0x8C040)
    ce0 = rd(m, 0x88CE0)
    print("  current: CYA_0=0x%08X LC0=0x%08X 88CE0=0x%08X 88CE4=0x%08X"
          % (cya, lc0, ce0, rd(m, 0x88CE4)))
    print("           PL_RATE=0x%08X LnkCap=0x%08X LnkCap2=0x%08X"
          % (rd(m, 0x8C1C0), rd(m, 0x88084), rd(m, 0x880A4)))
    print("           LnkSta(config)=%s\n" % setpci(args.bdf, "CAP_EXP+12.w"))

    new_ce0 = (ce0 & ~0x3F) | 0x06
    new_lc0 = lc0 & ~0x000C0000  # MAX_RATE=0 (Gen4)
    print("  planned A100 Gen4 writes:")
    print("    0x88CE4 = 0x%08X  (Gen4 init, A100)" % A100_88CE4)
    print("    0x88CE0 = 0x%08X  (Gen4 init low6=0x06)" % new_ce0)
    print("    CYA_0   = 0x%08X  (A100; clear bits 0,2,13,23)" % A100_CYA0)
    print("    PL_RATE = 0x%08X  (Gen4)" % A100_PL_RATE)
    print("    LC0     = 0x%08X  (MAX_RATE=0/Gen4)" % new_lc0)
    print("    VSEC    = 0x00001001")

    if not args.confirm:
        print("\n  DRY-RUN: nothing written. Re-run with --confirm (and --train).")
        m.close(); os.close(fd)
        return 0

    print("\n  writing (readback verified):")
    wverify(m, "88CE4", 0x88CE4, A100_88CE4)
    wverify(m, "88CE0", 0x88CE0, new_ce0)
    wverify(m, "CYA_0", 0x8C2C0, A100_CYA0)
    wverify(m, "PL_RATE", 0x8C1C0, A100_PL_RATE)
    wverify(m, "LC0", 0x8C040, new_lc0)
    wverify(m, "VSEC", 0x88610, 0x00001001)

    cap_after = rd(m, 0x88084)
    cap2_after = rd(m, 0x880A4)
    print("\n  after writes: LnkCap=0x%08X (nibble %d)  LnkCap2=0x%08X"
          % (cap_after, cap_after & 0xF, cap2_after))
    print("  (LnkCap2 vector: 0x6=Gen1_2, 0xE=Gen1_2_3, 0xF=Gen1_2_3_4)")
    m.close(); os.close(fd)

    if not args.train:
        print("\n  --train not set; registers written only. Check LnkCap2 above.")
        return 0

    minor, bus = detect_minor_bus(args.bdf)
    rm = "/usr/local/sbin/cmp-pcie-speed"
    if minor is None or not os.path.exists(rm):
        print("\n  cannot train: minor=%s rm=%s (need cmp-pcie-speed + driver)" % (minor, rm))
        return 1
    print("\n  RM staged training Gen1->2->3->4 (single pass):")
    for gen in (1, 2, 3, 4):
        r = subprocess.run([rm, str(gen), minor, bus], capture_output=True, text=True)
        tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()
        print("    Gen%d: %s" % (gen, tail))
        if r.returncode != 0:
            print("    STOP: Gen%d failed (status above). Cold boot if GPU dropped." % gen)
            break
        time.sleep(1)
    print("\n  final LnkSta=%s  (0x1104 = Gen4 x16)" % setpci(args.bdf, "CAP_EXP+12.w"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
