#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/driver/VERSION" 2>/dev/null || true)

source "${SCRIPT_DIR}/common/lib.sh"

banner

if [[ "${1:-}" != "--yes" && "${1:-}" != "-y" ]]; then
    warn "This removes the cmpunlocker-a100-minimal patched kernel modules:"
    echo "  - Removes /lib/modules/*/updates/cmpunlocker/"
    echo "  - Removes legacy cmpunlocker modprobe confs / services (if present)"
    echo "  - Rebuilds the stock nvidia DKMS modules that install.sh removed"
    echo "  - Reloads stock NVIDIA modules"
    echo ""
    echo "All unlocks are volatile: after removal the card is a native A100."
    echo ""
    echo "Run: sudo ./remove.sh --yes"
    exit 1
fi

step_init 4

step "Verifying root privileges"
[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./remove.sh --yes"
ok "Running as root"

LOG_DIR="${SCRIPT_DIR}/logs"
if ! mkdir -p "${LOG_DIR}" 2>/dev/null || [[ ! -w "${LOG_DIR}" ]]; then
    LOG_DIR="/tmp"
fi
LOG_FILE="${LOG_DIR}/remove_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "${LOG_FILE}") 2>&1

step "Removing legacy cmpunlocker helpers (if any)"
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
ok "Legacy helpers removed"

step "Removing patched modules and restoring stock NVIDIA modules"
restore_stock_modules() {
    local kernel="$1" ver
    if modprobe -n -q -S "${kernel}" nvidia 2>/dev/null; then
        ok "Stock nvidia module present for kernel ${kernel}: $(modinfo -n -k "${kernel}" nvidia 2>/dev/null || true)"
        return 0
    fi
    if ! command -v dkms &>/dev/null; then
        warn "No nvidia module for kernel ${kernel} and dkms is not installed - reinstall your distro's nvidia driver package"
        return 0
    fi
    for ver in "${SUPPORTED_VERSIONS[@]}"; do
        [[ -f "/usr/src/nvidia-${ver}/dkms.conf" ]] || continue
        info "Rebuilding stock nvidia ${ver} DKMS modules for kernel ${kernel} (install.sh removed them)..."
        if dkms install "nvidia/${ver}" -k "${kernel}"; then
            ok "Stock nvidia ${ver} modules restored for kernel ${kernel}"
            return 0
        fi
        warn "dkms install nvidia/${ver} failed for kernel ${kernel}"
    done
    warn "No stock nvidia DKMS source found for kernel ${kernel} - reinstall your distro's nvidia driver package"
    return 0
}

mod_removed=0
kernels=("$(uname -r)")
shopt -s nullglob
for mod_dir in /lib/modules/*/updates/cmpunlocker; do
    if [[ -d "${mod_dir}" ]]; then
        kernel="$(basename "$(dirname "$(dirname "${mod_dir}")")")"
        rm -rf "${mod_dir}"
        ok "Removed patched modules for kernel ${kernel}"
        mod_removed=$((mod_removed + 1))
        [[ " ${kernels[*]} " == *" ${kernel} "* ]] || kernels+=("${kernel}")
    fi
done
[[ "${mod_removed}" -gt 0 ]] || warn "No patched kernel modules found"

for kernel in "${kernels[@]}"; do
    depmod -a "${kernel}" 2>/dev/null || true
    restore_stock_modules "${kernel}"
done

info "Rebuilding initramfs so stock modules are packed again..."
for kernel in "${kernels[@]}"; do
    if command -v update-initramfs &>/dev/null; then
        update-initramfs -u -k "${kernel}" 2>/dev/null || true
    elif command -v dracut &>/dev/null; then
        dracut --force --kver "${kernel}" 2>/dev/null || true
    fi
done
if command -v mkinitcpio &>/dev/null && ! command -v update-initramfs &>/dev/null && ! command -v dracut &>/dev/null; then
    mkinitcpio -P 2>/dev/null || true
fi
ok "initramfs rebuild attempted"

step "Reloading stock NVIDIA driver"
if grep -q '^nvidia' /proc/modules; then
    warn "Unloading NVIDIA modules"
    systemctl stop nvidia-persistenced 2>/dev/null || true
    systemctl stop nvidia-fabricmanager 2>/dev/null || true
    sleep 1
    for mod in nvidia_drm nvidia_uvm nvidia_modeset nvidia; do
        modprobe -r "${mod}" 2>/dev/null || true
    done
    sleep 1
    if grep -q '^nvidia' /proc/modules; then
        for mod in nvidia_uvm nvidia_drm nvidia_modeset nvidia; do
            rmmod -f "${mod}" 2>/dev/null || true
        done
    fi
else
    warn "NVIDIA modules not loaded"
fi

if modprobe nvidia 2>/dev/null; then
    modprobe nvidia-modeset 2>/dev/null || true
    modprobe nvidia-uvm 2>/dev/null || true
    modprobe nvidia-drm 2>/dev/null || true
    ok "Stock NVIDIA driver loaded: $(modinfo -n nvidia 2>/dev/null || true)"
else
    warn "Could not load NVIDIA driver - reboot to finish cleanup"
fi

step "Done"
banner
echo "cmpunlocker-a100-minimal removed. Card is back to native A100 behavior."
echo "Log saved to: ${LOG_FILE}"
echo ""
echo "If the GPU is not working, reboot once:"
echo -e "  ${CYAN}sudo reboot${NC}"
echo ""
