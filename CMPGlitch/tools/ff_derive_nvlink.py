#!/usr/bin/env python3
"""Derive the NVLink FF burn (chain 1, block 23, bits 0-2). READ-ONLY.

Clears OPT_NVLINK_DISABLE (FF chain 1 bits 368-370, a 3-link mask) down to
the NVLINK_DEFECTIVE mask. Block 23 also holds SYS_PIPE_DISABLE (bits 4-11)
which is preserved.

IMPORTANT: confirm the real NVLINK_DEFECTIVE by reading register 0x82068C on
the card, then pass it via --defective. Default 0x0 assumes no defective links
(this die has no defects elsewhere) -- VERIFY before burning.

NOTE: NVLink also needs the U11 hardware mod AND a ROM-check bypass (host-side
commands) that is NOT documented in the references. The fuse burn alone will
not bring the links up.

WRITES NOTHING. Re-glitch for the window, then ff_burn.py --plan ff_plan_nvlink.json.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CHAIN, LO, HI = 1, 368, 370   # NVLINK_DISABLE field (3 bits)


def extract(chains, c, lo, hi):
    v = 0
    for b, d in chains.get(c, {}).items():
        v |= (d & 0xFFFF) << (16 * b)
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def main():
    ap = argparse.ArgumentParser(description="Derive NVLink FF burn (read-only)")
    ap.add_argument("--ff", default=os.path.join(HERE, "ff.json"))
    ap.add_argument("--defective", type=lambda x: int(x, 0), default=0x0,
                    help="NVLINK_DEFECTIVE mask from reg 0x82068C (VERIFY!)")
    ap.add_argument("--out", default=os.path.join(HERE, "ff_plan_nvlink.json"))
    ap.add_argument("--first-row", type=int, default=308)
    args = ap.parse_args()
    with open(args.ff, encoding="ascii") as f:
        ff = json.load(f)
    chains = {int(c): {int(b): int(d, 16) for b, d in bm.items()}
              for c, bm in ff["chains"].items()}
    target = args.defective & ((1 << (HI - LO + 1)) - 1)
    cur_field = extract(chains, CHAIN, LO, HI)

    desired = {c: dict(bm) for c, bm in chains.items()}
    for p in range(LO, HI + 1):
        block, bit = p // 16, p % 16
        tb = (target >> (p - LO)) & 1
        d = desired.setdefault(CHAIN, {})
        old = d.get(block, 0)
        d[block] = (old & ~(1 << bit)) | (tb << bit)

    records = []
    for c in sorted(desired):
        for b in sorted(desired[c]):
            new = desired[c][b]
            old = chains.get(c, {}).get(b, 0)
            if new != old:
                records.append({"chain": c, "block": b,
                                "old_data": "0x%04X" % old, "new_data": "0x%04X" % new,
                                "encoded": "0x%08X" % ((new << 16) | (b << 6) | c)})
    blanks = [r for r in ff["blank_rows"] if r >= args.first_row]
    for i, r in enumerate(records):
        r["row"] = blanks[i]

    merged = {c: dict(bm) for c, bm in chains.items()}
    for r in records:
        merged.setdefault(r["chain"], {})[r["block"]] = int(r["new_data"], 16)
    got = extract(merged, CHAIN, LO, HI)
    ok = got == target

    print("=== NVLink FF burn (READ-ONLY, nothing written) ===")
    print("  NVLINK_DISABLE current=0x%X -> target=0x%X (defective=0x%X)  %s" %
          (cur_field, got, target, "OK" if ok else "FAIL"))
    print("  (SYS_PIPE in block 23 bits 4-11 preserved)")
    print("\n  #  row  chain block old    new    encoded")
    for i, r in enumerate(records):
        print("  %-2d %-4d %-5d %-5d %s %s %s" %
              (i, r["row"], r["chain"], r["block"], r["old_data"],
               r["new_data"], r["encoded"]))
    with open(args.out, "w", encoding="ascii") as f:
        json.dump({"mode": "nvlink", "verified": ok,
                   "defective_assumed": "0x%X" % target, "records": records}, f, indent=2)
    print("\nsaved: %s" % args.out)
    print("VERIFY NVLINK_DEFECTIVE (reg 0x82068C) == 0x%X before burning!" % target)
    print("NVLink also needs U11 mod + undocumented ROM-check bypass.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
