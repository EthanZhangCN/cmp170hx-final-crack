# cmpunlocker-a100-minimal -- command reference

All commands for the converted A100 80GB PCIe (10de:20b5). Run from the tree
root unless noted. Everything the driver does is volatile (re-applied at
driver init); nothing here touches fuses or the VBIOS.

Current validated config on this card: `--sm-unlock --mclk-ndiv=71`
(128 SMs, full FMA, HBM 1917 MHz). See "Measured results" at the bottom.

---

## 1. Install

```bash
sudo ./install.sh --sm-unlock --mclk-ndiv=71    # full unlock + 1917 MHz HBM
sudo shutdown -h now                            # COLD reboot, then power on
```

Flag combinations (FMA speed is ALWAYS full, with or without flags):

| Command | SMs | FMA | HBM clock |
|---|---|---|---|
| `sudo ./install.sh` | 124 (native) | full | stock 1512 MHz (NDIV 56) |
| `sudo ./install.sh --sm-unlock` | 128 | full | stock |
| `sudo ./install.sh --mclk-ndiv=N` | 124 (native) | full | N * 27 MHz |
| `sudo ./install.sh --sm-unlock --mclk-ndiv=N` | 128 | full | N * 27 MHz |

- `--mclk-ndiv=N`: N in 30-80 (810-2160 MHz). Build-time constant.
- `--sm-unlock`: build-time; controls ONLY the 128-SM TPC reconfig.
- Changing flags re-patches/rebuilds automatically (build stamp).
- Re-run the same install command after every kernel upgrade.
- Help: `./install.sh --help`

## 2. Verify after boot

```bash
nvidia-smi                          # A100 80GB PCIe, 81920 MiB, Gen4 x16
python3 tools/smcheck.py            # SMs=128 with --sm-unlock, 124 without
sudo dmesg | grep SEC2_DEBUG        # unlock trail (see markers below)
sudo dmesg | grep HBMPLL_OC         # OC trail (only if --mclk-ndiv given)
```

Expected dmesg markers:

```
SEC2_DEBUG: PLM[0..14] ... reg=0xffffffff     all PLMs opened (WPR_CFG 0xfffff0ff)
SEC2_DEBUG: POST-WRITE SS0=0x88888888 SS1=0x00000008    FMA lift (ALWAYS present)
SEC2_DEBUG: SM-RECONFIG GPC0..7 OVR<-0xff STATUS ...    ONLY with --sm-unlock
SEC2_DEBUG: normal BooterLoad status=0x0                GSP booted clean
HBMPLL_OC: start NDIV 56->71 (1512->1917 MHz)
HBMPLL_OC: done, 12 FBPAs at NDIV=71 (1917 MHz)
HBMPLL_OC: post-GSP POST: COEFF=...(NDIV=71) CFG=... lock=1
```

Live clocks / temps / power:

```bash
nvidia-smi --query-gpu=temperature.gpu,temperature.memory,power.draw,clocks.sm,clocks.mem --format=csv -l 5
nvidia-smi -q -d CLOCK,POWER,ECC
```

NOTE: `clocks.current.memory` in nvidia-smi shows the LIVE PLL (1917 MHz).
CUDA device attribute 36 (memory clock) shows the VBIOS table value
(1512 MHz) and does NOT reflect the OC -- ignore it.

TPC plane probe (read-only, pure Python, never writes to the GPU):

```bash
sudo python3 tools/smprobe.py             # works directly with the driver bound (VM passthrough)
sudo python3 tools/smprobe.py --unbind    # elsewhere: unbind -> read -> rebind
```

With the driver loaded and --sm-unlock active: RECONFIG(a40)=0xff on all
8 GPCs, STATUS_TPC=0x00, SM_SPEED_OVR_0=0x88888888. With --unbind the GPU
resets, so it always shows the underlying fuse state (124-SM baseline),
not the live override.

Theoretical HBM peak (bus width is live; clock may read VBIOS stock):

```bash
python3 - <<'EOF'
import ctypes
c = ctypes.CDLL('libcuda.so.1'); c.cuInit(0)
d = ctypes.c_int(); c.cuDeviceGet(ctypes.byref(d), 0)
def attr(n):
    v = ctypes.c_int(); c.cuDeviceGetAttribute(ctypes.byref(v), n, d); return v.value
bw, mclk = attr(37), attr(36)   # bus width (bits), memory clock (kHz)
print(f'bus={bw}-bit  memclk={mclk/1000:.0f} MHz  theoretical peak={bw/8*mclk*1000*2/1e12:.2f} TB/s')
EOF
```

This card: 5120-bit. Peak = 640 bytes * clock * 2: 1.94 TB/s at 1512 MHz,
2.45 TB/s at 1917 MHz. For the real clock use the HBMPLL_OC dmesg line or
nvidia-smi, then recompute.

## 3. Stability tests

```bash
# HBM error test (5 min standard + extended; Ctrl+C to stop):
./memtest_vulkan                        # pick the A100 (not llvmpipe)

# Bandwidth (OpenCL benchmark or CUDA D2D copy test):
#   opencl-benchmark-linux, or bandwidthTest --mode=range, or nvbandwidth

# Compute + thermal soak (10-15 min), watch for Xid/AER afterwards:
gpu-burn -d 900                         # or any sustained FP32 load
sudo dmesg | grep -iE "Xid|AER"         # must be empty
```

FP32 sanity: 128 SMs at ~1.4 GHz with the FMA lift should approach
~23 TFLOPS (FMA). Roughly half means the FMA lift is not active -- check
`POST-WRITE SS0=0x88888888` in dmesg.

## 4. Change the HBM clock (NDIV)

```bash
sudo ./install.sh --sm-unlock --mclk-ndiv=67   # any N in 30-80
sudo shutdown -h now
```

Step UP one value at a time, memtest after each. Step DOWN on any error.
Reference points: 56 = stock (1512), 64 = 1728, 67 = 1809, 71 = 1917
(validated), 80 = 2160 (build ceiling, not validated).

## 5. Toggle the SM count

```bash
sudo ./install.sh --sm-unlock --mclk-ndiv=71   # 128 SMs
sudo ./install.sh --mclk-ndiv=71               # 124 SMs (native), same OC
sudo shutdown -h now
```

FMA speed stays full in both cases.

## 6. Escape hatch: OC wedges the GPU at init

Skips the HBM OC only; SM unlock and FMA lift stay active.

```bash
echo 'options nvidia NVreg_RegistryDwords="cmpSafe=1"' | sudo tee /etc/modprobe.d/cmp-safe.conf
sudo shutdown -h now          # cold boot; dmesg shows "cmpSafe=1 - skipping"
# ... then reinstall with a lower --mclk-ndiv, and remove the hatch:
sudo rm /etc/modprobe.d/cmp-safe.conf
```

(Or append `nvidia.NVreg_RegistryDwords=cmpSafe=1` to the kernel cmdline.)

## 7. Rollback / uninstall

```bash
sudo ./remove.sh --yes        # removes patched modules, restores stock nvidia
sudo reboot
```

Card returns to native A100 behavior (124 SMs, stock HBM clock, fused FMA
throttle). Nothing permanent was ever written -- no fuse/ROM undo needed.
If the driver fails to load after an install, SSH in and run remove.sh.

## 8. Troubleshooting

| Symptom | Check / fix |
|---|---|
| SMs=124, expected 128 | installed without --sm-unlock; no SM-RECONFIG lines in dmesg |
| FP32 ~half speed | `POST-WRITE SS0` missing/zero in dmesg (should always be 0x88888888) |
| `HBMPLL_OC: aborted, PLM ... bit4 closed` | PLM opening failed; check SEC2_DEBUG PLM[n] lines |
| `FBPA<n> PLL lock timeout` / post-GSP lock=0 | NDIV too high; step down, reinstall |
| memtest errors | NDIV too high (or cooling); step down |
| GPU wedges at boot with OC | cmpSafe=1 hatch (section 6), then lower NDIV |
| Driver won't load at all | SSH + `remove.sh --yes`; grab `dmesg | grep -E "SEC2_DEBUG|NVRM"` |
| CUDA mem clock shows 1512 | normal: static VBIOS attribute; nvidia-smi shows the live PLL |
| Xid / AER under load | stop; check cooling/power; lower NDIV and/or SM count |

## 9. Measured results (this card, VM passthrough)

| Config | SMs | HBM | OpenCL read/write | Notes |
|---|---|---|---|---|
| native (no flags) | 124 | 1512 MHz (NDIV 56) | 1539 / 1805 GB/s | stock baseline |
| --sm-unlock --mclk-ndiv=64 | 128 | 1728 MHz | -- | first boot clean, all PLMs attempt 0 |
| --sm-unlock --mclk-ndiv=71 | 128 | 1917 MHz | 2072 / 2341 GB/s | write = 95% of 2.45 TB/s peak |

- memtest_vulkan at NDIV 71: standard 5-min PASS + extended, zero errors.
- No Xid/AER. Idle temps 34 C GPU / 49 C HBM. Bus 5120-bit (20 FBPA).
- PCIe Gen4 x16 native: H2D 26.76 / D2H 26.44 GB/s (see ../cmp_a100_conversion.md).
- SM-RECONFIG STATUS reads 0x00 before the write: the fuse burns already
  enable all 64 TPCs; the 124 limit is applied by A100 GSP during init,
  and the pre-boot override blocks it.

## Files

| Tool | Purpose |
|---|---|
| `install.sh` / `remove.sh` | install / rollback |
| `tools/smcheck.py` | SM count via libcuda |
| `tools/smprobe.py` | read-only TPC/fuse plane probe |
| `driver/build.sh` | build internals (called by install.sh) |
