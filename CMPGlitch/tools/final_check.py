#!/usr/bin/env python3
"""Final feature confirmation for the unlocked CMP 170HX. Run on the GPU host.

Checks the permanent fuse state (BAR0), runtime features (nvidia-smi / CUDA),
and stability (Xid errors), then prints a PASS/FAIL feature report.

  sudo python3 final_check.py --bdf 0000:01:00.0

Run this after the burns and a power-cycle, before returning the card to
service, to confirm the unlock.
"""
import argparse
import mmap
import os
import re
import struct
import subprocess
import sys

# (label, offset, expected, note)  -- expected None = informational only
FUSE_CHECKS = [
    ("GPC disable",        0x820350, 0x00000000, "all 8 GPC enabled"),
    ("FBP disable",        0x820364, 0x00000840, "10 FBP (6,11 defective)"),
    ("FBPA disable",       0x820368, 0x00C03000, "20 FBPA (12,13,22,23 defective)"),
    ("FBIO disable",       0x82036C, 0x00C03000, "20 FBIO"),
    ("PES disable",        0x820690, 0x00000000, "all PES enabled (SM requirement)"),
    ("ROP_L2 disable",     0x8202C4, 0x00C03000, "ROP/L2 defect-only"),
    ("Gen3 speed disable", 0x820250, 0x00000000, "Gen3 allowed"),
    ("Boot Gen2/3 disable",0x82057C, 0x00000000, "Gen2/3 boot allowed"),
    ("Boot Gen3 disable",  0x820580, 0x00000000, "Gen3 boot allowed"),
    ("NVLink disable",     0x820684, 0x00000000, "NVLink unmasked"),
]
TPC_BASE = 0x820760
MAGIC_D = 0x820520
ECC_EN = 0x820228
LANE_DIS = 0x820394


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def sm_count():
    try:
        import torch
        return torch.cuda.get_device_properties(0).multi_processor_count, "torch"
    except Exception:
        pass
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        return pynvml.nvmlDeviceGetNumGpuCores(h), "pynvml"
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bdf", default="0000:01:00.0")
    args = ap.parse_args()
    if sys.platform != "linux":
        print("Run on the Linux GPU host"); return 2

    print("=== CMP 170HX Final Feature Confirmation ===")
    print("Card: %s\n" % args.bdf)
    passed = failed = 0

    def report(ok, label, detail):
        nonlocal passed, failed
        tag = "PASS" if ok else "FAIL"
        if ok: passed += 1
        else: failed += 1
        print("  [%s] %-22s %s" % (tag, label, detail))

    # ---- fuse state ----
    print("FUSE STATE (permanent burns):")
    try:
        fd = os.open("/sys/bus/pci/devices/%s/resource0" % args.bdf, os.O_RDONLY)
        m = mmap.mmap(fd, 0x822000, prot=mmap.PROT_READ)
        rd = lambda o: struct.unpack_from("<I", m, o)[0]
        for label, off, exp, note in FUSE_CHECKS:
            v = rd(off)
            report(v == exp, label, "0x%08X (%s)" % (v, note))
        for g in range(8):
            v = rd(TPC_BASE + 4 * g)
            report(v == 0, "TPC GPC%d disable" % g, "0x%02X" % v)
        md = rd(MAGIC_D)
        report(((md >> 25) & 1) == 0, "Gen4 flag (bit25)", "0x%08X bit25=%d" % (md, (md >> 25) & 1))
        ecc = rd(ECC_EN)
        print("  [INFO] %-22s 0x%X (fuse; never burned -- ECC was software-only pre-conversion)" % ("ECC_EN fuse", ecc))
        lane = rd(LANE_DIS)
        print("  [INFO] %-22s 0x%08X" % ("PCIe lane disable", lane))
        m.close(); os.close(fd)
    except Exception as e:
        print("  BAR0 read failed: %s" % e)

    # ---- runtime ----
    print("\nRUNTIME (nvidia-smi / CUDA):")
    q = sh("nvidia-smi --query-gpu=memory.total,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,ecc.mode.current --format=csv,noheader,nounits")
    if q.strip():
        parts = [p.strip() for p in q.strip().split(",")]
        try:
            mem, gcur, gmax, wcur, wmax, ecc = parts[:6]
            report(int(mem) >= 80000, "Memory", "%s MiB (~80 GB)" % mem)
            report(int(gcur) >= 3, "PCIe gen", "current Gen%s / max Gen%s" % (gcur, gmax))
            report(int(wcur) >= 4, "PCIe width", "current x%s / max x%s" % (wcur, wmax))
            print("  [INFO] %-22s %s (no ECC fuse burn; completely unvalidated/untested post-conversion)" % ("ECC mode", ecc))
        except Exception:
            print("  parse error: %s" % q)
    else:
        print("  nvidia-smi query failed (driver loaded?)")
    n, src = sm_count()
    if n is not None:
        report(n >= 120, "SM count", "%d (%s)" % (n, src))
    else:
        print("  [INFO] SM count: install torch or pynvml to query")

    # ---- stability ----
    print("\nSTABILITY:")
    dmesg = sh("dmesg")
    xids = re.findall(r"Xid.*?: (\d+)", dmesg)
    report(len(xids) == 0, "Xid errors", "none" if not xids else "Xids: %s" % sorted(set(xids)))

    print("\n=== RESULT: %d passed, %d failed ===" % (passed, failed))
    print("NVLink note: unmasked but needs U11 mod + ROM-check bypass to train.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
