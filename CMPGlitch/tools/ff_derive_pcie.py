#!/usr/bin/env python3
"""Derive PCIe Gen3/Gen4 FF burns (chain 2). READ-ONLY derivation.

Clears the PCIe speed/boot gates per the blog FF map:
  block 39 bits 1+9 : DISABLE_GEN3_SPEED (bit identity unresolved -> clear both
                      candidates; both are currently set, one is the Gen3 gate)
  block 47 bit 3    : PCIE_MAGIC_BITS_D[25] Gen4 speed-disable flag
  block 49 bits 6+7 : PCIE_BOOT_GEN23_DISABLE + PCIE_BOOT_GEN3_DISABLE

Requires the PCIe decoupling-capacitor mod for Gen4 x16 (hardware, separate).
WRITES NOTHING. Re-glitch for the window, then ff_burn.py --plan ff_plan_pcie.json.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CHAIN = 2
# (block, bits_to_clear, description)
TARGETS = [
    (39, (1 << 1) | (1 << 9), "DISABLE_GEN3_SPEED (bit1+bit9 candidates)"),
    (47, (1 << 3), "PCIE_MAGIC_BITS_D[25] Gen4 flag"),
    (49, (1 << 6) | (1 << 7), "BOOT_GEN23 + BOOT_GEN3 disable"),
]


def main():
    ap = argparse.ArgumentParser(description="Derive PCIe Gen3/Gen4 burns (read-only)")
    ap.add_argument("--ff", default=os.path.join(HERE, "ff.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "ff_plan_pcie.json"))
    ap.add_argument("--first-row", type=int, default=305)
    args = ap.parse_args()
    with open(args.ff, encoding="ascii") as f:
        ff = json.load(f)
    chains = {int(c): {int(b): int(d, 16) for b, d in bm.items()}
              for c, bm in ff["chains"].items()}
    blanks = [r for r in ff["blank_rows"] if r >= args.first_row]

    records = []
    print("=== PCIe Gen3/Gen4 burns (READ-ONLY, nothing written) ===")
    for block, mask, desc in TARGETS:
        cur = chains.get(CHAIN, {}).get(block, 0)
        new = cur & ~mask
        enc = (new << 16) | (block << 6) | CHAIN
        print("  block %2d: 0x%04X -> 0x%04X  (clear 0x%04X)  %s" %
              (block, cur, new, mask, desc))
        if new != cur:
            records.append({"chain": CHAIN, "block": block,
                            "old_data": "0x%04X" % cur, "new_data": "0x%04X" % new,
                            "encoded": "0x%08X" % enc, "note": desc})
    for i, r in enumerate(records):
        r["row"] = blanks[i]
    print("\n  #  row  chain block old    new    encoded")
    for i, r in enumerate(records):
        print("  %-2d %-4d %-5d %-5d %s %s %s" %
              (i, r["row"], r["chain"], r["block"], r["old_data"],
               r["new_data"], r["encoded"]))
    with open(args.out, "w", encoding="ascii") as f:
        json.dump({"mode": "pcie_gen4", "records": records}, f, indent=2)
    print("\nsaved: %s" % args.out)
    print("NOTE: Gen3 bit identity unresolved; both block-39 candidates cleared.")
    print("      Requires PCIe decoupling-cap mod for Gen4 x16.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
