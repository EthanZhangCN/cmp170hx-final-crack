#!/usr/bin/env python3
"""Derive additional PES/ROP_L2 FF burns to complete the SM unlock. READ-ONLY.

The first burn pass cleared GPC/TPC/FBP/FBPA/FBIO but left PES and ROP_L2
disabled. PES/ROP are required for SMs to function (blog + chain-1 TOP_FS
floorsweep), so the GPU fails GSP boot with SMs enabled but PES/ROP disabled.

Derives the extra records to clear PES (defective=0 -> target 0) and ROP_L2
(defective=0xC03000) on top of the already-burned state (ff_plan.json).

WRITES NOTHING. Re-glitch for the burn window, then:
  ff_burn.py --plan ff_plan_pes.json --index N --confirm
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# (name, chain, lo_bit, hi_bit, target_value)  -- targets = defect masks
TARGETS = [
    ("OPT_PES_DISABLE", 1, 251, 274, 0x0),
    ("OPT_ROP_L2_DISABLE", 1, 319, 342, 0xC03000),
]


def extract(chains, c, lo, hi):
    v = 0
    for b, d in chains.get(c, {}).items():
        v |= (d & 0xFFFF) << (16 * b)
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def main():
    ap = argparse.ArgumentParser(description="Derive PES/ROP_L2 completion burns (read-only)")
    ap.add_argument("--ff", default=os.path.join(HERE, "ff.json"))
    ap.add_argument("--burned", default=os.path.join(HERE, "ff_plan.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "ff_plan_pes.json"))
    args = ap.parse_args()
    with open(args.ff, encoding="ascii") as f:
        ff = json.load(f)
    with open(args.burned, encoding="ascii") as f:
        burned = json.load(f)

    # current chains = original + already-burned records
    chains = {int(c): {int(b): int(d, 16) for b, d in bm.items()}
              for c, bm in ff["chains"].items()}
    for r in burned["records"]:
        chains.setdefault(r["chain"], {})[r["block"]] = int(r["new_data"], 16)
    used = [r["row"] for r in burned["records"] if r.get("row") is not None]
    blanks = [r for r in ff["blank_rows"] if r > max(used)]

    desired = {c: dict(bm) for c, bm in chains.items()}
    for name, c, lo, hi, target in TARGETS:
        for p in range(lo, hi + 1):
            block, bit = p // 16, p % 16
            tb = (target >> (p - lo)) & 1
            d = desired.setdefault(c, {})
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
    if len(blanks) < len(records):
        print("ERROR: not enough blank rows"); return 2
    for i, r in enumerate(records):
        r["row"] = blanks[i]

    merged = {c: dict(bm) for c, bm in chains.items()}
    for r in records:
        merged.setdefault(r["chain"], {})[r["block"]] = int(r["new_data"], 16)
    ok = True
    print("=== PES/ROP_L2 completion burns (READ-ONLY, nothing written) ===")
    for name, c, lo, hi, target in TARGETS:
        cur = extract(chains, c, lo, hi)
        got = extract(merged, c, lo, hi)
        match = "OK" if got == target else "FAIL"
        if got != target:
            ok = False
        print("  %-22s current=0x%-8X -> 0x%-8X (target 0x%X) %s" %
              (name, cur, got, target, match))
    print("\n  #  row  chain block old    new    encoded")
    for i, r in enumerate(records):
        print("  %-2d %-4d %-5d %-5d %s %s %s" %
              (i, r["row"], r["chain"], r["block"], r["old_data"],
               r["new_data"], r["encoded"]))
    print("\n  verification: %s" % ("PASS" if ok else "FAIL"))
    with open(args.out, "w", encoding="ascii") as f:
        json.dump({"mode": "pes_rop_completion", "verified": ok, "records": records},
                  f, indent=2)
    print("saved: %s" % args.out)
    print("Apply: re-glitch for window, then ff_burn.py --plan %s" % args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
