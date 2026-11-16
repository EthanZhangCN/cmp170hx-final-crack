# Debugging why SET_PCIE_SPEED(4) returns 0x56

Goal: find the exact gate that makes the RM control
`NV2080_CTRL_CMD_BUS_SET_PCIE_SPEED` (0x20801805) return `0x56`
(`NV_ERR_NOT_SUPPORTED`) for Gen4 while Gen1/2/3 succeed.

The handler `subdeviceCtrlCmdBusSetPcieSpeed_IMPL` has **no body in the open
tree** (only a declaration + export wrapper). It runs in **closed GSP-RM
firmware**. So we debug it as a black box: vary the inputs the open kernel
passes to GSP and observe when `0x56` flips, then (optionally) disassemble the
firmware for the definitive answer.

## Leading hypothesis

GSP-RM validates the requested speed against the **boot-cached LnkCap Max Link
Speed** it receives from the kernel at init:

- The kernel reads config-space LnkCap once at boot (`kbifStateLoad` ->
  `kbifGetGpuLinkCapabilities`, `kernel_bif.c:93-94,1253-1277`) and caches it in
  `pKernelBif->pcieConfigReg.linkCap`.
- `_kgspPrepareSystemInfo` ships that cache to GSP in the `GspSetSystemInfo`
  init RPC: `kernel_gsp.c:4574`
  `rpcInfo->pcieConfigReg.linkCap = pKernelBif->pcieConfigReg.linkCap;`
- `GSP_PCIE_CONFIG_REG` has **exactly one field, `linkCap`**
  (`gsp_static_config.h:63`). It is the only PCIe capability GSP gets from the
  kernel. Max Link Speed = `linkCap & 0xF`.
- On this card the boot LnkCap nibble is **3** (Gen3): the CMP VBIOS suppresses
  the DevInit Gen4 block, so the PHY latches Gen3 capability at boot. E1 lifts
  the *live* LnkCap to 4 later, but the **cache shipped to GSP stays 3**.

Prediction: GSP rejects `busSpeed=4` because `4 > (linkCap & 0xF) == 3`.
Gen3 passes (`3 <= 3`); Gen1/2 pass. This is consistent with every measurement:
- Gen3 staged training works (boot cap already 3).
- `RMPcieLinkSpeed=0x15` (Gen4 allow-mask) does not help (not the gate).
- GSP reports Device Max 4 (fuse capability) yet refuses speed 4 (the cached
  config-space cap, a different input).
- E1 lifting live LnkCap to 4 does not help (GSP uses the boot cache, not live).

## Experiment A (decisive): force the cached cap to Gen4

`pcie-gen4-capdbg.patch` inserts a gated block right after `kernel_gsp.c:4574`
that forces the shipped `linkCap` Max Link Speed nibble to 4. It changes ONE
variable (what GSP is told the max speed is). It does **not** train the link or
touch the PHY. Gated on registry key `CmpGen4Dbg=1`, device 0x20C2/0x2082.

### Build / install (standalone; does not modify the shared -80g tree)

Apply the patch to a fresh open-gpu-kernel-modules 610.43.03 build tree (or your
cmpunlocker build tree AFTER the existing patches), then build/install as usual.
With the cmpunlocker build flow, drop the patch into `driver/patches/` and add
`pcie-gen4-capdbg.patch` to `PATCH_ORDER` in `driver/build.sh` (after
`pcie-gen4.patch`). On a plain source tree:

```bash
cd open-gpu-kernel-modules-610.43.03
sed -i 's/\r$//' /path/to/pcie-gen4-capdbg.patch
patch -p1 < /path/to/pcie-gen4-capdbg.patch
make -j"$(nproc)" modules SYSSRC=/lib/modules/$(uname -r)/build
# install the resulting nvidia*.ko (cmpunlocker build.sh does this for you)
```

### Run the experiment

Boot with the **working Gen3 baseline** plus the debug key. Do NOT set
`CmpGen4=1` (that re-enables the abandoned 0x881C0 Booter write which wedges
GSP boot). Example modprobe conf:

```
options nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x5;CmpGen3=1;CmpGen4Dbg=1"
```

Cold boot, then:

```bash
# 1. Confirm the forced cap was shipped to GSP:
dmesg | grep "CMP Gen4 capdbg linkCap"
#    expect: linkCap 0x00456103 -> 0x00456104 (MaxSpeed=4)

# 2. Does the driver now REPORT Gen4? (tests the reporting path too)
nvidia-smi -q -d PCIE | grep -A6 "GPU Link Info"

# 3. The decisive call (minor + bus from `nvidia-smi -q | grep -E 'Minor|Bus'`):
sudo /usr/local/sbin/cmp-pcie-speed 4 <minor> <bus-hex>
#    e.g. sudo /usr/local/sbin/cmp-pcie-speed 4 0 01

# 4. Capture the link state + any GSP/RM log around the call:
setpci -s 01:00.0 CAP_EXP+12.w
dmesg | tail -n 60
```

### Interpret the result

| `cmp-pcie-speed 4` result | Meaning | Next step |
|---|---|---|
| **OK** (link trains Gen4) | Gate WAS the cached linkCap. Hypothesis confirmed AND solved. | Make it permanent: real Gen4 retrain in the post-GSP hook + live-read 4574 (the reporting fix). |
| **OK** but link stays Gen3 | Validation gate was the cache; GSP now *accepts* 4 but its training path needs the Gen4 PHY init (E1). | Add E1 init before the call; the 0x56 validation wall is gone. |
| **Different error** (not 0x56) | Cache is part of the gate; a second check remains. | Read the new status; capture GSP log (Experiment B). |
| **Still 0x56** | Gate is NOT the cache -> hardcoded Gen1/2/3 switch in closed firmware. | Go to Experiment C (disassemble) to locate the switch. |

Also note step 2: if `nvidia-smi` Max flips to 4 just from forcing the cache,
that **proves the reporting path is the same cached linkCap** — the reporting
fix is then confirmed independently of the retrain.

## Experiment B: capture the GSP/RM log at rejection

GSP-RM logs are decoded by the kernel (libos log decoder) into the kernel log.
Enable verbose RM prints and a larger GSP RM log buffer, reproduce, and read:

```
options nvidia NVreg_RegistryDwords="...;CmpGen4Dbg=1;RmGspLogBufferSizeTaskRm=256"
```

`RmMsg` (string regkey, `nvrm_registry.h:545`) enables RM `DBG_PRINTF` by
filename/function/line rule, e.g. `RmMsg="kernel_bif.c,kern_bus_ctrl.c"`. After
reproducing the `0x56`, capture the full kernel log and the GSP log:

```bash
sudo dmesg > /tmp/gen4-reject.dmesg
sudo nvidia-bug-report.sh   # bundles GSP/RM logs + regkeys
grep -iE "pcie|speed|0x56|not supported|linkCap" /tmp/gen4-reject.dmesg
```

If the closed handler prints a reason (e.g. "speed exceeds max" or a speed
switch default), it shows up here. This can reveal the gate without disassembly.

## Experiment C (definitive): disassemble the GSP firmware

The handler lives in the GSP-RM binary. For GA100 it is the **tu10x** family
(`nv-firmware.h:118-124`):

```
/lib/firmware/nvidia/610.43.03/gsp_tu10x.bin        # GSP-RM firmware
/lib/firmware/nvidia/610.43.03/gsp_log_tu10x.bin    # log decoder
```

Steps (frontier, higher effort):
1. Copy `gsp_tu10x.bin` off the host.
2. Decrypt: GSP is csecret-encrypted; the CMP wiki documents a shared GA100 key
   and a known-plaintext oracle for this driver family. (See
   `cmp_gen4.md` references / Consensus-Protocol wiki.)
3. Disassemble the Falcon/RISC-V ucode, locate the control dispatch for
   `0x20801805`, and read the speed validation. The `0x56` return site shows the
   exact comparison (cached linkCap nibble vs a hardcoded 1/2/3 switch).

This is the only way to see the closed logic directly; Experiments A/B usually
identify the gate without it.

## Safety

- Experiment A is **capability-only**: it edits the value handed to GSP at init.
  No PHY writes, no Booter runs, no link transition. Worst case the link stays
  Gen3; cold boot restores everything.
- Do **not** set `CmpGen4=1` for this debug (re-enables the wedging 0x881C0
  Booter write). Use `CmpGen3=1` for the working baseline.
- If you later test actual Gen4 training, do it single-pass; a failed Gen4
  transition can drop the GPU off the bus (cold boot recovers). No retry loops.
- Judge the trained link by config-space `LnkSta` (`setpci CAP_EXP+12.w`), never
  by LnkCap or nvidia-smi Max.

## Files

| File | Purpose |
|---|---|
| `pcie-gen4-capdbg.patch` | Force shipped linkCap MaxSpeed=4 (CmpGen4Dbg=1), capability-only |
| `README.md` | This debug plan |