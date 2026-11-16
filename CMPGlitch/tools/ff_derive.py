#!/usr/bin/env python3
"""Read-only FF burn-plan derivation + dry-run preview. WRITES NOTHING.

Reads the FF dump (ff.json) and the defect masks (baseline snapshot), then
computes the exact append records that would clear the RECLAIMABLE floorsweep
bits (leaving defective bits set). Changes are merged into existing block data
so unrelated option fields sharing a block are preserved. Prints a dry-run
preview and saves ff_plan.json. Does NOT touch the GPU.

Usage (offline, on the dev machine):
  python ff_derive.py                      # full unlock plan preview
  python ff_derive.py --single 0          # show only record #0 (first test)
  python ff_derive.py --noop              # a zero-risk mechanism-test record
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# option word -> (chain, lo_bit, hi_bit, defect register name)
TARGETS = [
    ("OPT_FBIO_DISABLE", 1, 83, 106, "OPT_FBIO_DEFECTIVE"),
    ("OPT_FBPA_DISABLE", 1, 107, 130, "OPT_FBPA_DEFECTIVE"),
    ("OPT_GPC_DISABLE", 1, 131, 138, "OPT_GPC_DEFECTIVE"),
    ("OPT_FBP_DISABLE", 1, 307, 318, "OPT_FBP_DEFECTIVE"),
]
for _i in range(8):
    TARGETS.append(("OPT_TPC_DISABLE_GPC%d" % _i, 1, 187 + 8 * _i, 194 + 8 * _i,
                    "OPT_TPC_GPC%d_DEFECTIVE" % _i))


def extract(chains, c, lo, hi):
    if c not in chains:
        return 0
    v = 0
    for b, d in chains[c].items():
        v |= (d & 0xFFFF) << (16 * b)
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def load_defects(path):
    with open(path, encoding="ascii") as f:
        rep = json.load(f)
    return {r["name"]: int(r["value"], 16) for r in rep.get("registers", [])}


def derive(ff, defects):
    chains = {int(c): {int(b): int(d, 16) for b, d in bm.items()}
              for c, bm in ff["chains"].items()}
    desired = {c: dict(bm) for c, bm in chains.items()}
    word_changes = []
    for name, c, lo, hi, defname in TARGETS:
        target = defects.get(defname, 0)
        cur = extract(chains, c, lo, hi)
        for p in range(lo, hi + 1):
            block, bit = p // 16, p % 16
            tb = (target >> (p - lo)) & 1
            d = desired.setdefault(c, {})
            old = d.get(block, 0)
            d[block] = (old & ~(1 << bit)) | (tb << bit)
        if cur != target:
            word_changes.append({"word": name, "chain": c, "bits": [lo, hi],
                                 "current": "0x%X" % cur, "target": "0x%X" % target})
    records = []
    for c in sorted(desired):
        for b in sorted(desired[c]):
            new = desired[c][b]
            old = chains.get(c, {}).get(b, 0)
            if new != old:
                records.append({"chain": c, "block": b,
                                "old_data": "0x%04X" % old, "new_data": "0x%04X" % new,
                                "encoded": "0x%08X" % ((new << 16) | (b << 6) | c)})
    return chains, desired, records, word_changes


def verify(chains, records, defects):
    """Apply records (last-wins) and confirm option words == defect masks."""
    merged = {c: dict(bm) for c, bm in chains.items()}
    for r in records:
        merged.setdefault(r["chain"], {})[r["block"]] = int(r["new_data"], 16)
    ok = True
    for name, c, lo, hi, defname in TARGETS:
        got = extract(merged, c, lo, hi)
        want = defects.get(defname, 0)
        if got != want:
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description="Read-only FF burn-plan derivation")
    ap.add_argument("--ff", default=os.path.join(HERE, "ff.json"))
    ap.add_argument("--defects", default=None, help="baseline snapshot JSON")
    ap.add_argument("--single", type=int, default=None, help="show only record N")
    ap.add_argument("--noop", action="store_true",
                    help="emit a zero-risk no-op test record (duplicate block)")
    ap.add_argument("--out", default=os.path.join(HERE, "ff_plan.json"))
    args = ap.parse_args()

    with open(args.ff, encoding="ascii") as f:
        ff = json.load(f)
    if args.defects:
        dpath = args.defects
    else:
        snaps = sorted(glob.glob(os.path.join(ROOT, "snapshots", "registers_*.json")))
        if not snaps:
            print("no baseline snapshot found; pass --defects")
            return 2
        dpath = snaps[-1]
    defects = load_defects(dpath)
    blank = ff.get("blank_rows", [])

    print("=== FF burn-plan derivation (READ-ONLY, nothing written) ===")
    print("FF dump: %s" % args.ff)
    print("Defects: %s" % dpath)
    print("Blank rows available: %d (first=%s)" % (len(blank), blank[0] if blank else "-"))

    if args.noop:
        # Zero-risk mechanism test: duplicate an existing block to a blank row.
        # Effective config UNCHANGED; the fresh row still gets programmed, so a
        # raw-row readback proves the analog burn landed.
        c, b = 1, 7
        data = ff["chains"][str(c)][str(b)]
        enc = (int(data, 16) << 16) | (b << 6) | c
        row = blank[0]
        print("\n-- NO-OP mechanism test record (zero functional change) --")
        print("  target blank row : %d" % row)
        print("  record           : chain=%d block=%d data=%s" % (c, b, data))
        print("  encoded word     : 0x%08X" % enc)
        print("  effect           : NONE (re-asserts existing block; last-wins same value)")
        print("  verify           : CMD_READ row %d should read 0x%08X (was 0x00000000)" % (row, enc))
        plan = {"mode": "noop", "records": [
            {"row": row, "chain": c, "block": b, "new_data": data,
             "encoded": "0x%08X" % enc, "noop": True}]}
        with open(args.out, "w", encoding="ascii") as f:
            json.dump(plan, f, indent=2)
        print("\nsaved: %s" % args.out)
        return 0

    chains, desired, records, word_changes = derive(ff, defects)
    print("\n-- option words to change (current -> defect-only target) --")
    for w in word_changes:
        print("  %-26s %s -> %s" % (w["word"], w["current"], w["target"]))

    # assign blank rows
    for i, r in enumerate(records):
        r["row"] = blank[i] if i < len(blank) else None
    ok = verify(chains, records, defects)

    show = records if args.single is None else [records[args.single]]
    print("\n-- append records (%d total)%s --" %
          (len(records), "" if args.single is None else " [showing #%d]" % args.single))
    print("  #  row  chain block old    new    encoded     fields")
    for i, r in enumerate(records):
        if args.single is not None and i != args.single:
            continue
        print("  %-2d %-4s %-5d %-5d %s %s %s" %
              (i, r["row"], r["chain"], r["block"], r["old_data"],
               r["new_data"], r["encoded"]))
    print("\n  verification (records reproduce defect-only masks): %s" %
          ("PASS" if ok else "FAIL"))
    if not ok:
        print("  WARNING: derivation did not verify - DO NOT BURN")

    plan = {"mode": "unlock", "verified": ok, "defects_source": dpath,
            "word_changes": word_changes, "records": records}
    with open(args.out, "w", encoding="ascii") as f:
        json.dump(plan, f, indent=2)
    print("\nsaved: %s" % args.out)
    print("\nNOTHING was written to the GPU. Review before any burn.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
