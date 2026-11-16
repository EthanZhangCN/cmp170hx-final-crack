#!/usr/bin/env python3
"""Derive FF restore records to revert the unlock burns. READ-ONLY derivation.

Reads ff_plan.json (the unlock records, which carry old_data) and ff.json
(the pre-burn chains/shadow), and produces append records that restore each
changed block to its ORIGINAL value. Because the FF chain is last-record-wins,
these restore records (placed at higher blank rows) override the unlock
records and return the card to its pre-burn, bootable configuration.

WRITES NOTHING to the GPU. Review the plan, re-glitch for the burn window,
then burn with: ff_burn.py --plan ff_plan_restore.json --index N --confirm
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

TARGETS = [
    ("OPT_FBIO_DISABLE", 1, 83, 106),
    ("OPT_FBPA_DISABLE", 1, 107, 130),
    ("OPT_GPC_DISABLE", 1, 131, 138),
    ("OPT_FBP_DISABLE", 1, 307, 318),
]
for _i in range(8):
    TARGETS.append(("OPT_TPC_DISABLE_GPC%d" % _i, 1, 187 + 8 * _i, 194 + 8 * _i))


def extract(chains, c, lo, hi):
    v = 0
    for b, d in chains.get(c, {}).items():
        v |= (d & 0xFFFF) << (16 * b)
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def main():
    ap = argparse.ArgumentParser(description="Derive FF restore records (read-only)")
    ap.add_argument("--plan", default=os.path.join(HERE, "ff_plan.json"))
    ap.add_argument("--ff", default=os.path.join(HERE, "ff.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "ff_plan_restore.json"))
    args = ap.parse_args()

    with open(args.plan, encoding="ascii") as f:
        plan = json.load(f)
    with open(args.ff, encoding="ascii") as f:
        ff = json.load(f)
    unlock = plan["records"]
    orig = {int(c): {int(b): int(d, 16) for b, d in bm.items()}
            for c, bm in ff["chains"].items()}
    shadow = {n: int(v, 16) for n, v in ff["shadow"].items()}

    used = [r["row"] for r in unlock if r.get("row") is not None]
    blanks = [r for r in ff["blank_rows"] if r > max(used)]
    if len(blanks) < len(unlock):
        print("ERROR: not enough blank rows for restore"); return 2

    restore = []
    for i, r in enumerate(unlock):
        c, b = r["chain"], r["block"]
        old = int(r["old_data"], 16)
        restore.append({"row": blanks[i], "chain": c, "block": b,
                        "new_data": r["old_data"],
                        "encoded": "0x%08X" % ((old << 16) | (b << 6) | c),
                        "note": "restore block %d to original %s" % (b, r["old_data"])})

    # verify: original + unlock + restore == original shadow masks
    merged = {c: dict(bm) for c, bm in orig.items()}
    for r in unlock:
        merged.setdefault(r["chain"], {})[r["block"]] = int(r["new_data"], 16)
    for r in restore:
        merged.setdefault(r["chain"], {})[r["block"]] = int(r["new_data"], 16)
    ok = True
    print("=== FF RESTORE plan (READ-ONLY, nothing written) ===")
    print("Reverts %d unlock records; restore rows %d-%d" %
          (len(restore), restore[0]["row"], restore[-1]["row"]))
    print("\n  #  row  chain block restore-to  encoded")
    for i, r in enumerate(restore):
        print("  %-2d %-4d %-5d %-5d %s    %s" %
              (i, r["row"], r["chain"], r["block"], r["new_data"], r["encoded"]))
    print("\n  verification (restored masks == pre-burn shadow):")
    for name, c, lo, hi in TARGETS:
        got = extract(merged, c, lo, hi)
        want = shadow.get(name)
        if want is None:
            continue
        match = "OK" if got == want else "MISMATCH"
        if got != want:
            ok = False
        print("    %-26s 0x%-8X %s" % (name, got, match))
    print("\n  overall: %s" % ("PASS" if ok else "FAIL"))

    with open(args.out, "w", encoding="ascii") as f:
        json.dump({"mode": "restore", "verified": ok, "records": restore}, f, indent=2)
    print("\nsaved: %s" % args.out)
    print("To apply: re-glitch for burn window, then ff_burn.py --plan %s" % args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
