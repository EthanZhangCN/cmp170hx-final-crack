#!/usr/bin/env python3
"""Empirical MAGIC_D <- FF block47 bit derivation (chain 2). IRREVERSIBLE burns.

Goal: map PCIE_MAGIC_BITS_D bits {19,22,26,28} (the Gen4 capability bits, A100
MAGIC_D=0x00200000) to their FF block47 bit locations. Only bit25->block47 bit3
is documented; the rest of the MAGIC_D<->block mapping is scrambled. This tool
clears ONE candidate block47 bit per burn so you can observe which MAGIC_D bit
clears after a power cycle, building the map empirically.

Method (per cycle):
  1. Glitch to burn window (DSOV=0).
  2. python3 ff_magic_derive.py --bdf <BDF> --bit N          # dry-run
  3. python3 ff_magic_derive.py --bdf <BDF> --bit N --confirm # burn (type BURN)
  4. Power cycle, read MAGIC_D:
       python3 -c "import mmap,os,struct; m=mmap.mmap(os.open('/sys/bus/pci/devices/<BDF>/resource0',os.O_RDONLY),0x822000,prot=mmap.PROT_READ); print('MAGIC_D=0x%08X'%struct.unpack_from('<I',m,0x820520)[0])"
  5. Compare to previous MAGIC_D: the cleared bit reveals block47 bit N's mapping.
     If it is a Gen4 target bit (19/22/26/28) keep it; else restore (append the
     old block47 value at a new blank row).

block47 effective value is read live (last-record-wins over rows 222-487).
--all clears every set block47 bit at once (faster, but maps them together).
Guards identical to ff_burn.py; NO retry on failure.
"""
import argparse
import mmap
import os
import struct
import sys
import time

FUSECTRL = 0x00820000
FUSEADDR = 0x00820004
FUSERDATA = 0x00820008
FUSEWDATA = 0x0082000C
DEBUGCTRL = 0x00820060
RWL_BIT = 1 << 1
CRC_STATUS = 0x00820070
EFP = 0x00820078
DSOV = 0x00820084
FUSEWDATA_PLM = 0x008200F4
FUSECTRL_PLM = 0x008200F8
SBFE = 0x00821108
MAGIC_D = 0x00820520
CMD_READ = 0x1
CMD_WRITE = 0x2
STATE_MASK = 0x1F << 16
STATE_IDLE = 4 << 16
MAP_BYTES = 0x822000
FF_START, FF_END = 222, 487
CHAIN = 2
FF_START, FF_END = 222, 487


class Bar0:
    def __init__(self, bdf):
        self.fd = os.open("/sys/bus/pci/devices/%s/resource0" % bdf,
                          os.O_RDWR | os.O_SYNC | os.O_CLOEXEC)
        self.mm = mmap.mmap(self.fd, MAP_BYTES, flags=mmap.MAP_SHARED,
                            prot=mmap.PROT_READ | mmap.PROT_WRITE)

    def rd(self, o):
        return struct.unpack_from("<I", self.mm, o)[0]

    def wr(self, o, v):
        struct.pack_into("<I", self.mm, o, v & 0xFFFFFFFF)

    def close(self):
        self.mm.close()
        os.close(self.fd)


def poll_idle(bar, ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        if (bar.rd(FUSECTRL) & STATE_MASK) == STATE_IDLE:
            return True
        time.sleep(0.001)
    return False


def read_row(bar, row):
    bar.wr(FUSECTRL, 0)
    bar.wr(FUSEADDR, row)
    bar.wr(FUSECTRL, CMD_READ)
    if not poll_idle(bar, 1000):
        return None
    return bar.rd(FUSERDATA)


def scan(bar, block_tag):
    """Return (effective block data, latest block row, sorted blank rows)."""
    eff, latest, blanks = 0, -1, []
    for row in range(FF_START, FF_END + 1):
        v = read_row(bar, row)
        if v is None:
            continue
        if v == 0:
            blanks.append(row)
        elif (v & 0xFFFF) == block_tag:
            eff = v >> 16
            latest = row
    return eff, latest, blanks


def check_prereqs(bar, row):
    checks = []
    fc = bar.rd(FUSECTRL)
    checks.append(("FUSECTRL IDLE", (fc & STATE_MASK) == STATE_IDLE, "0x%08X" % fc))
    crc = bar.rd(CRC_STATUS)
    checks.append(("CRC_STATUS=0x55", crc == 0x55, "0x%02X" % crc))
    checks.append(("DSOV=0", bar.rd(DSOV) == 0, "0x%X" % bar.rd(DSOV)))
    checks.append(("SBFE=0", bar.rd(SBFE) == 0, "0x%X" % bar.rd(SBFE)))
    checks.append(("EFP=1", bar.rd(EFP) == 1, "0x%X" % bar.rd(EFP)))
    checks.append(("FUSEWDATA_PLM open", bar.rd(FUSEWDATA_PLM) == 0xFFFFFFFF, "0x%08X" % bar.rd(FUSEWDATA_PLM)))
    checks.append(("FUSECTRL_PLM open", bar.rd(FUSECTRL_PLM) == 0xFFFFFFFF, "0x%08X" % bar.rd(FUSECTRL_PLM)))
    dbg = bar.rd(DEBUGCTRL)
    checks.append(("DEBUGCTRL.RWL=0", (dbg & RWL_BIT) == 0, "0x%08X" % dbg))
    blank = read_row(bar, row)
    checks.append(("target row blank", blank == 0, "0x%08X" % (blank if blank is not None else 0xFFFFFFFF)))
    return checks


def main():
    ap = argparse.ArgumentParser(description="MAGIC_D<-block47 empirical derivation (IRREVERSIBLE)")
    ap.add_argument("--bdf", required=True)
    ap.add_argument("--block", type=int, default=47, help="chain-2 block to derive (default 47)")
    ap.add_argument("--bit", type=int, help="block bit to clear (0-15)")
    ap.add_argument("--all", action="store_true", help="clear all set block bits")
    ap.add_argument("--restore", type=lambda x: int(x, 0), help="append this block data value (restore)")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        print("Requires Linux + sudo"); return 2
    if args.bit is None and not args.all and args.restore is None:
        print("need --bit N, --all, or --restore 0xVAL"); return 2
    blk = args.block
    block_tag = (blk << 6) | CHAIN

    bar = Bar0(args.bdf)
    try:
        magic = bar.rd(MAGIC_D)
        eff, latest, blanks = scan(bar, block_tag)
        print("=== MAGIC_D <- chain2 block%d derivation ===" % blk)
        print("  MAGIC_D(0x820520)      = 0x%08X  (bits %s)" %
              (magic, ",".join(str(b) for b in range(32) if (magic >> b) & 1)))
        print("  block%d effective      = 0x%04X  (latest row %d, bits %s)" %
              (blk, eff, latest, ",".join(str(b) for b in range(16) if (eff >> b) & 1)))
        print("  Gen4 target MAGIC_D bits to clear: 19,22,26,28  (A100=0x00200000)")
        if not blanks:
            print("ABORT: no blank FF rows"); return 1
        row = blanks[0]

        if args.restore is not None:
            new = args.restore & 0xFFFF
            desc = "restore block%d=0x%04X" % (blk, new)
        elif args.all:
            new = 0
            desc = "clear ALL block%d bits" % blk
        else:
            if not (0 <= args.bit <= 15):
                print("bit must be 0-15"); return 1
            new = eff & ~(1 << args.bit)
            desc = "clear block%d bit %d" % (blk, args.bit)
        if new == eff and args.restore is None:
            print("  block%d bit already clear; nothing to burn." % blk); return 0
        value = (new << 16) | block_tag
        print("  planned: block%d 0x%04X -> 0x%04X  (%s)" % (blk, eff, new, desc))
        print("  record : row %d = 0x%08X" % (row, value))

        checks = check_prereqs(bar, row)
        ok = True
        for name, passed, val in checks:
            print("  [%s] %-26s %s" % ("OK" if passed else "FAIL", name, val))
            ok = ok and passed
        if not ok:
            print("ABORT: prerequisites not met."); return 1
        if not args.confirm:
            print("\nDRY-RUN: would burn row %d = 0x%08X. Add --confirm (IRREVERSIBLE)." % (row, value))
            return 0

        print("\n*** IRREVERSIBLE FUSE BURN ***")
        try:
            resp = input("Type BURN to write row %d = 0x%08X: " % (row, value))
        except EOFError:
            resp = ""
        if resp.strip() != "BURN":
            print("ABORT: not confirmed."); return 1
        dbg = bar.rd(DEBUGCTRL)
        if dbg & RWL_BIT:
            bar.wr(DEBUGCTRL, dbg & ~RWL_BIT)
        bar.wr(FUSECTRL, 0)
        bar.wr(FUSEADDR, row)
        bar.wr(FUSEWDATA, value)
        bar.wr(FUSECTRL, CMD_WRITE)
        if not poll_idle(bar, 5000):
            print("ERROR: write incomplete. STOP. No retry."); return 1
        rb = read_row(bar, row)
        if rb == value:
            print("VERIFY OK: row %d = 0x%08X" % (row, rb))
            print("POWER CYCLE, then read MAGIC_D to see which bit cleared:")
            print("  python3 -c \"import mmap,os,struct; m=mmap.mmap(os.open('/sys/bus/pci/devices/%s/resource0',os.O_RDONLY),0x822000,prot=mmap.PROT_READ); print('MAGIC_D=0x%%08X'%%struct.unpack_from('<I',m,0x820520)[0])\"" % args.bdf)
            print("  prev MAGIC_D=0x%08X ; the cleared bit = block47 bit's mapping." % magic)
            return 0
        print("VERIFY FAIL: got 0x%08X. STOP. No retry." % (rb if rb is not None else 0xFFFFFFFF))
        return 1
    finally:
        bar.close()


if __name__ == "__main__":
    sys.exit(main())
