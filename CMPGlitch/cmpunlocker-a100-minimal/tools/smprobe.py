#!/usr/bin/env python3
"""Read-only GA100 SM/TPC state probe. Pure Python, no kernel module to build.

Reads BAR0 fuse shadows and the TPC override planes to show exactly which
TPCs are disabled and by which plane. NEVER writes to the GPU.

If the nvidia driver owns BAR0 (mmap fails with EINVAL/EBUSY), run with
--unbind: the script temporarily unbinds the driver, reads, and rebinds it
(guaranteed by try/finally). Unbind resets the GPU and drops CUDA contexts,
so run it with the GPU idle. Fuses are permanent, so the reset does not
change the fused state being probed.

Usage:
  sudo python3 smprobe.py                      # direct read (driver unbound)
  sudo python3 smprobe.py --unbind             # auto unbind -> read -> rebind
  sudo python3 smprobe.py --bdf 0000:01:00.0 --unbind

Register map (cmprir/gpcprobe + cmpunlocker sec2 patch):
  0x820350 OPT_GPC_DISABLE        fuse: bit per GPC, 1 = disabled
  0x820c1c STATUS_OPT_GPC         read-only shadow of the above
  0x820c38 + g*4  FUSE_STATUS_OPT_TPC_GPC   effective TPC disable mask
  0x820838 + g*4  FUSE_CTRL_OPT_TPC_GPC     CTRL override plane (remove-only)
  0x820a40 + g*4  RECONFIG TPC override     what cmpunlocker SM-RECONFIG writes
  0x82381c / 0x823820  FEATURE_OVERRIDE_SM_SPEED (FMA throttle lift)
"""
import argparse
import mmap
import os
import struct
import sys
import time

MAP_LEN = 0x824000  # covers fuse region + feature override registers


def map_bar0(bdf):
    path = "/sys/bus/pci/devices/%s/resource0" % bdf
    fd = os.open(path, os.O_RDONLY)
    try:
        return mmap.mmap(fd, MAP_LEN, prot=mmap.PROT_READ)
    finally:
        os.close(fd)


def rd(m, off):
    return struct.unpack_from("<I", m, off)[0]


def bound_driver(bdf):
    p = "/sys/bus/pci/devices/%s/driver" % bdf
    if os.path.islink(p):
        return os.path.basename(os.readlink(p))
    return None


def sysfs_write(path, value):
    with open(path, "w") as f:
        f.write(value)


def popcount(x):
    return bin(x).count("1")


def probe(m):
    def show(name, off):
        v = rd(m, off)
        flag = "  <-- PRI ERROR" if (v & 0xFFF00000) == 0xBAD00000 else ""
        print("  %-28s (0x%06x) = 0x%08x%s" % (name, off, v, flag))
        return v

    print("identity / sanity:")
    show("DEVIDA", 0x8204D8)
    show("DEVIDB", 0x82056C)
    show("MAGIC_D", 0x820520)

    print("GPC fuses:")
    gpc_dis = show("OPT_GPC_DISABLE", 0x820350)
    show("STATUS_OPT_GPC", 0x820C1C)

    print("SM speed feature override (0 = no override):")
    show("SM_SPEED_OVR_0", 0x82381C)
    show("SM_SPEED_OVR_1", 0x823820)

    print("TPC planes (STATUS = effective disable mask, 1 bit per TPC):")
    print("  GPC  STATUS_TPC  CTRL(838)  RECONFIG(a40)  TPCs enabled")
    total_tpc = 0
    disabled = []
    for g in range(8):
        stat = rd(m, 0x820C38 + g * 4) & 0xFF
        ctrl = rd(m, 0x820838 + g * 4) & 0xFF
        reconf = rd(m, 0x820A40 + g * 4) & 0xFF
        if (gpc_dis >> g) & 1:
            en = 0
            note = "GPC fused off"
        else:
            en = 8 - popcount(stat)
            note = ""
        total_tpc += en
        for t in range(8):
            if not ((gpc_dis >> g) & 1) and (stat >> t) & 1:
                disabled.append("GPC%d/TPC%d" % (g, t))
        print("  %d    0x%02x        0x%02x       0x%02x         %d/8  %s"
              % (g, stat, ctrl, reconf, en, note))

    print("total: %d TPCs = %d SMs (2 SM per TPC)" % (total_tpc, total_tpc * 2))
    if disabled:
        print("disabled TPCs: %s" % ", ".join(disabled))
    print("")
    print("reading: RECONFIG(a40)=0 everywhere means no driver override is")
    print("active; the disabled TPCs above come from fuses / VBIOS / GSP.")
    print("cmpunlocker-a100-minimal writes 0xff to all 8 RECONFIG dwords in")
    print("the SEC2 POSTBL window before GSP boots (SM-RECONFIG).")


def main():
    ap = argparse.ArgumentParser(description="Read-only GA100 TPC/SM probe")
    ap.add_argument("--bdf", default="0000:01:00.0")
    ap.add_argument("--unbind", action="store_true",
                    help="temporarily unbind/rebind the bound driver if it owns BAR0")
    args = ap.parse_args()
    bdf = args.bdf

    if not os.path.isdir("/sys/bus/pci/devices/%s" % bdf):
        sys.exit("no PCI device %s" % bdf)

    drv = bound_driver(bdf)
    m = None
    unbound = False
    try:
        try:
            m = map_bar0(bdf)
        except OSError as e:
            if not args.unbind:
                sys.exit("BAR0 mmap failed (%s). Driver '%s' owns the BAR.\n"
                         "Re-run with --unbind to temporarily unbind/read/rebind "
                         "(GPU idle, CUDA contexts will be dropped)." % (e, drv))
            if drv is None:
                sys.exit("BAR0 mmap failed (%s) and no driver is bound" % e)
            if os.geteuid() != 0:
                sys.exit("--unbind needs root")
            print("unbinding %s from %s ..." % (bdf, drv))
            sysfs_write("/sys/bus/pci/drivers/%s/unbind" % drv, bdf)
            unbound = True
            time.sleep(1.0)
            m = map_bar0(bdf)
        probe(m)
    finally:
        if m is not None:
            m.close()
        if unbound:
            print("rebinding %s to %s ..." % (bdf, drv))
            try:
                sysfs_write("/sys/bus/pci/drivers/%s/bind" % drv, bdf)
            except OSError as e:
                sys.exit("REBIND FAILED (%s): run 'echo %s > "
                         "/sys/bus/pci/drivers/%s/bind' manually" % (e, bdf, drv))
            time.sleep(2.0)
            print("rebound; driver reinitializes the GPU (nvidia-smi may take a moment)")


if __name__ == "__main__":
    main()
