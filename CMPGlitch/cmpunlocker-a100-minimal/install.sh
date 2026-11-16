#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/driver/VERSION")
SUPPORTED_VERSIONS_CSV="$(IFS=', '; echo "${SUPPORTED_VERSIONS[*]}")"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/install_$(date +%Y%m%d_%H%M%S).log"

MCLK_NDIV=""
SM_UNLOCK=""
for arg in "$@"; do
    case "${arg}" in
        --mclk-ndiv=*) MCLK_NDIV="${arg#*=}" ;;
        --sm-unlock) SM_UNLOCK=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: sudo ./install.sh [--sm-unlock] [--mclk-ndiv=N]

Minimal cmpunlocker for the converted A100 80GB PCIe (10de:20b5).
Everything is native A100 except the two optional unlocks:

  --sm-unlock     SM count unlock: all 128 SMs (TPC reconfig in the SEC2
                  POSTBL window). WITHOUT this flag the card keeps the
                  native A100 GSP floorsweep: 124 SMs.
                  The SM_SPEED FMA-throttle lift is ALWAYS applied, with or
                  without this flag: full FMA speed even at 124 SMs.
  --mclk-ndiv=N   HBM memory clock: PLL multiplier (30-80), N * 27 MHz.
                  Compiled into the patched modules. Without this flag the
                  overclock is not applied at all. A100 80GB stock HBM clock
                  on this card is 1512 MHz = NDIV 56. If a value wedges the
                  GPU at boot, use the escape hatch
                  nvidia.NVreg_RegistryDwords=cmpSafe=1 and step back down.

The two flags are independent (e.g. OC with native 124 SMs is fine).
All unlocks are VOLATILE register writes applied at driver init. No fuses
are touched. sudo ./remove.sh --yes + reboot returns the card to native
A100 behavior.
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            echo "Try: sudo ./install.sh --help" >&2
            exit 1
            ;;
    esac
done

exec > >(tee -a "${LOG_FILE}") 2>&1

source "${SCRIPT_DIR}/common/lib.sh"

banner
step_init 5

step "Verifying root privileges"
[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./install.sh"
ok "Running as root"

step "Detecting converted A100 GPU(s) (10de:20b5)"
mapfile -t PCI_LINES < <(lspci -nn 2>/dev/null | grep -iE '10de:20b5' || true)
[[ ${#PCI_LINES[@]} -gt 0 ]] || die "No 10de:20b5 GPU found. This tree only targets the converted A100 (fused device ID 0x20B5)."
GPU_BDFS=()
for PCI_LINE in "${PCI_LINES[@]}"; do
    PCI="$(echo "${PCI_LINE}" | awk '{print $1}')"
    PCI_FULL="$(normalize_bus_id "${PCI}")"
    GPU_BDFS+=("${PCI_FULL}")
    ok "GPU ${PCI_FULL} (10de:20b5)"
done
if lspci -nn 2>/dev/null | grep -qiE '10de:20c2|10de:2082'; then
    warn "CMP-ID card(s) also present: this tree does NOT unlock 0x20C2/0x2082 (use the original cmpunlocker for those)"
fi

step "Verifying nvidia-open (${SUPPORTED_VERSIONS_CSV})"
[[ ${#SUPPORTED_VERSIONS[@]} -gt 0 ]] || die "No supported versions listed in driver/VERSION"
if [[ -d /sys/firmware/efi ]] && command -v mokutil &>/dev/null; then
    if mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; then
        die "Secure Boot is enabled. Disable it before installing unsigned patched modules."
    fi
fi

version_supported() {
    local v="$1"
    local s
    for s in "${SUPPORTED_VERSIONS[@]}"; do
        [[ "${v}" == "${s}" ]] && return 0
    done
    return 1
}

detected=""
if [[ -r /proc/driver/nvidia/version ]]; then
    detected="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' /proc/driver/nvidia/version | head -1 || true)"
fi
if [[ -z "${detected}" ]] && command -v nvidia-smi &>/dev/null; then
    smi_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]' || true)"
    [[ "${smi_version}" =~ ^[0-9]+(\.[0-9]+)+$ ]] && detected="${smi_version}"
fi
if [[ -z "${detected}" ]]; then
    for cand in "${SUPPORTED_VERSIONS[@]}"; do
        if [[ -d "/lib/firmware/nvidia/${cand}" ]]; then
            detected="${cand}"
            break
        fi
    done
    if [[ -z "${detected}" ]]; then
        fw="$(ls -d /lib/firmware/nvidia/*/ 2>/dev/null | sed 's|.*/nvidia/||;s|/||' | sort -rV | head -1 || true)"
        detected="${fw}"
    fi
fi

[[ -n "${detected}" ]] || die "Could not detect an installed NVIDIA driver. Install nvidia-open ${SUPPORTED_VERSIONS_CSV} first."
version_supported "${detected}" || die "Installed driver is ${detected}, but this tree requires one of: ${SUPPORTED_VERSIONS_CSV}."
ok "NVIDIA driver ${detected} is supported"

[[ -d "/lib/modules/$(uname -r)/build" ]] || die "Kernel headers missing for $(uname -r). Install linux-headers-$(uname -r) or kernel-devel."
ok "Kernel headers present for $(uname -r)"

info "Removing conflicting NVIDIA DKMS modules (not all systems have any)"
for ver in "${SUPPORTED_VERSIONS[@]}"; do
    dkms remove nvidia/"${ver}" --all 2>/dev/null || true
done
depmod -a "$(uname -r)"
ok "DKMS conflicting modules resolution complete"

if [[ -n "${MCLK_NDIV}" ]]; then
    if ! [[ "${MCLK_NDIV}" =~ ^[0-9]+$ ]] || (( MCLK_NDIV < 30 || MCLK_NDIV > 80 )); then
        die "--mclk-ndiv must be between 30 and 80 (got: ${MCLK_NDIV})"
    fi
    ok "MCLK: NDIV=${MCLK_NDIV} ($(( MCLK_NDIV * 27 )) MHz) will be compiled in"
    info "A100 80GB stock reference: NDIV 59 = 1593 MHz"
else
    info "MCLK: stock (no --mclk-ndiv given)"
fi

if [[ -n "${SM_UNLOCK}" ]]; then
    ok "SM: count unlock ON (128 SMs) will be compiled in"
else
    info "SM: native count (no --sm-unlock given; expect 124 SMs)"
fi
ok "FMA: full speed (SM_SPEED override always compiled in)"

step "Building and installing patched modules"
chmod +x "${SCRIPT_DIR}/driver/build.sh"
CMPUNLOCKER_DRIVER_VERSION="${detected}" \
CMPUNLOCKER_CARD_PROFILE="a100" \
CMPUNLOCKER_SM_UNLOCK="${SM_UNLOCK}" \
CMPUNLOCKER_MCLK_NDIV="${MCLK_NDIV}" \
    "${SCRIPT_DIR}/driver/build.sh"
ok "Patched modules installed"

step "Cleaning up legacy cmpunlocker leftovers (if any)"
# Old cmpunlocker installs forced PCIe registry keys / Gen2 retrain services.
# None of that applies to the native A100 path; remove leftovers so they
# cannot interfere with native Gen4 training.
rm -f /etc/modprobe.d/cmp-gen3.conf /etc/modprobe.d/cmp-pcie-gen2.conf
for legacy_unit in gen2.service cmpretrain.service cmp-gen2-retrain.service; do
    systemctl disable --now "${legacy_unit}" 2>/dev/null || true
    systemctl reset-failed "${legacy_unit}" 2>/dev/null || true
done
rm -f /etc/systemd/system/gen2.service \
      /etc/systemd/system/cmpretrain.service \
      /etc/systemd/system/cmp-gen2-retrain.service \
      /usr/local/sbin/gen2-hammer \
      /usr/local/sbin/retrain.sh \
      /usr/local/sbin/cmp-gen2-retrain.sh \
      /usr/local/sbin/cmp-pcie-speed
systemctl daemon-reload 2>/dev/null || true
ok "Legacy leftovers cleaned"

echo ""
banner
echo "cmpunlocker-a100-minimal install finished!"
echo "GPUs:    ${#GPU_BDFS[@]} x 10de:20b5 (${GPU_BDFS[*]})"
if [[ -n "${SM_UNLOCK}" ]]; then
    echo "SM:      count unlock ON (expect 128 SMs)"
else
    echo "SM:      native count (expect 124 SMs; use --sm-unlock for 128)"
fi
echo "FMA:     full speed (always on)"
if [[ -n "${MCLK_NDIV}" ]]; then
    echo "MCLK:    NDIV=${MCLK_NDIV} ($(( MCLK_NDIV * 27 )) MHz)"
else
    echo "MCLK:    stock (overclock not requested)"
fi
echo ""
echo "Next:"
echo -e "  1. Cold reboot: ${CYAN}sudo shutdown -h now${NC}  (then power on)"
if [[ -n "${SM_UNLOCK}" ]]; then
    echo -e "  2. SM count: ${CYAN}python3 smcheck.py${NC}  (expect SMs=128)"
    echo -e "  3. Unlock logs: ${CYAN}sudo dmesg | grep SEC2_DEBUG${NC}  (SM-RECONFIG GPC0..7)"
else
    echo -e "  2. SM count: ${CYAN}python3 smcheck.py${NC}  (expect SMs=124, native)"
    echo -e "  3. Init logs: ${CYAN}sudo dmesg | grep SEC2_DEBUG${NC}  (PLMs only, no SM-RECONFIG)"
fi
if [[ -n "${MCLK_NDIV}" ]]; then
    echo -e "  4. HBM OC: ${CYAN}sudo dmesg | grep HBMPLL_OC${NC}  (expect 'done, N FBPAs at NDIV=${MCLK_NDIV}')"
fi
echo -e "  5. TPC state probe: ${CYAN}sudo python3 tools/smprobe.py --unbind${NC}"
echo ""
echo "Escape hatch (OC wedges init): nvidia.NVreg_RegistryDwords=cmpSafe=1"
echo "Full rollback: sudo ./remove.sh --yes  (native A100, nothing permanent)"
echo "Re-run this script after each kernel upgrade."
echo "Log saved to: ${LOG_FILE}"
echo ""
