import importlib.util, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("check", os.path.join(HERE, "check.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def base_values():
    v = {}
    for name, off, width in m.REGISTERS:
        v[name] = 0
    return v

# Case 1: arca "108 SM" example -> GPC0 whole-defective, GPC1 TPC6, GPC2 TPC1;
# FBPA defective {12,13,22,23} (=0xC03000), FBP defective {6,11} (=0x840).
v = base_values()
v["OPT_GPC_DEFECTIVE"] = 0x01
v["OPT_TPC_GPC0_DEFECTIVE"] = 0xFF
v["OPT_TPC_GPC1_DEFECTIVE"] = 0x40
v["OPT_TPC_GPC2_DEFECTIVE"] = 0x02
v["OPT_FBPA_DEFECTIVE"] = 0xC03000
v["OPT_FBP_DEFECTIVE"] = 0x840
v["OPT_FBPA_DISABLE"] = 0xC0330C   # disabled {2,3,8,9,12,13,22,23}
v["STATUS_OPT_FBPA"] = 0xC0330C
ce = m.estimate_ceiling(v, 0x20C2)
assert ce["sm"]["ceiling_sm"] == 108, ce["sm"]
assert ce["sm"]["healthy_tpc"] == 54, ce["sm"]
assert ce["hbm"]["ceiling_gib"] == 80, ce["hbm"]
assert ce["hbm"]["healthy_fbpa"] == 20, ce["hbm"]
assert ce["hbm"]["defective_fbpa_indices"] == [12,13,22,23], ce["hbm"]
assert ce["hbm"]["reclaimable_fbpa_indices"] == [2,3,8,9], ce["hbm"]
assert ce["all_defect_flags_zero"] is False
print("Case1 (108 SM / 80 GiB, reclaimable 2,3,8,9): PASS")

# Case 2: arca "122 SM" example -> 3 defective TPCs in otherwise-clean GPCs.
v = base_values()
v["OPT_TPC_GPC3_DEFECTIVE"] = 0x20
v["OPT_TPC_GPC5_DEFECTIVE"] = 0x10
v["OPT_TPC_GPC6_DEFECTIVE"] = 0x04
v["OPT_FBPA_DEFECTIVE"] = 0xC03000
ce = m.estimate_ceiling(v, 0x20C2)
assert ce["sm"]["ceiling_sm"] == 122, ce["sm"]
assert ce["hbm"]["ceiling_gib"] == 80, ce["hbm"]
print("Case2 (122 SM / 80 GiB): PASS")

# Case 3: golden die, all defect flags zero -> 128 SM / 96 GiB candidate.
v = base_values()
ce = m.estimate_ceiling(v, 0x20C2)
assert ce["sm"]["ceiling_sm"] == 128, ce["sm"]
assert ce["hbm"]["ceiling_gib"] == 96, ce["hbm"]
assert ce["all_defect_flags_zero"] is True
print("Case3 (128 SM / 96 GiB golden candidate): PASS")

# Case 4: 10GB SKU density (2 GiB/FBPA), 4 defective FBPA -> 40 GiB.
v = base_values()
v["OPT_FBPA_DEFECTIVE"] = 0x00000F
ce = m.estimate_ceiling(v, 0x2082)
assert ce["hbm"]["ceiling_gib"] == 40, ce["hbm"]
assert ce["hbm"]["gib_per_fbpa_assumed"] == 2
print("Case4 (10GB SKU 40 GiB): PASS")

# Sanity: allowlist has no command/data ports (FUSECTRL/FUSEADDR/FUSEDATA/FUSEWDATA).
for forbidden in (0x820000, 0x820004, 0x820008, 0x82000C, 0x820060):
    assert forbidden not in m.ALLOWED_OFFSETS, hex(forbidden)
print("Allowlist excludes all fuse command/data/debug ports: PASS")
print("ALL OFFLINE TESTS PASSED")