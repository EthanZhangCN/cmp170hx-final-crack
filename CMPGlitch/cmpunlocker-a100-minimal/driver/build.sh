#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/VERSION")
DEFAULT_VERSION="${SUPPORTED_VERSIONS[0]:-}"
VERSION="${CMPUNLOCKER_DRIVER_VERSION:-${DEFAULT_VERSION}}"
PATCH_DIR="${SCRIPT_DIR}/patches"
BUILD_ROOT="${CMPUNLOCKER_BUILD_DIR:-${SCRIPT_DIR}/.build}"
SRC_NAME="open-gpu-kernel-modules-${VERSION}"
SRC_DIR="${BUILD_ROOT}/${SRC_NAME}"
TARBALL="${BUILD_ROOT}/${SRC_NAME}.tar.gz"
TARBALL_URL="https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/${VERSION}.tar.gz"
KVER="$(uname -r)"
KSRC="/lib/modules/${KVER}/build"
INSTALL_MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
fi

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

version_supported() {
    local v="$1"
    local s
    for s in "${SUPPORTED_VERSIONS[@]}"; do
        [[ "${v}" == "${s}" ]] && return 0
    done
    return 1
}

[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ${SCRIPT_DIR}/build.sh"
[[ -n "${VERSION}" ]] || die "No driver version set (driver/VERSION empty and CMPUNLOCKER_DRIVER_VERSION unset)"
version_supported "${VERSION}" || die "Unsupported driver version '${VERSION}' (supported: ${SUPPORTED_VERSIONS[*]})"
[[ -d "${PATCH_DIR}" ]] || die "Missing patches directory: ${PATCH_DIR}"
[[ -d "${KSRC}" ]] || die "Kernel headers not found at ${KSRC}. Install linux-headers-${KVER} (or kernel-devel)."
command -v python3 &>/dev/null || die "python3 is required"
python3 -c "import yaml" 2>/dev/null || die "python3 PyYAML is required to read common/constants.yaml (apt install python3-yaml)"
command -v sha256sum &>/dev/null || die "sha256sum is required"
info "Building against open-gpu-kernel-modules ${VERSION} (A100 minimal: SM reconfig + HBM NDIV)"

# Minimal patch set:
#   sec2-postbl-plm-ss-cfg : SEC2 POSTBL window, PLM opening, SM_SPEED override,
#                            SM-RECONFIG (TPC override), stock signature rebuild
#   booter-verify          : boot-path hardening + diagnostics (NDIV anchor)
#   hbm-ndiv               : HBM PLL NDIV overclock (applied via apply-ndiv.py)
PATCH_ORDER=(
    sec2-postbl-plm-ss-cfg.patch
    booter-verify.patch
    hbm-ndiv.patch
)
PATCH_FILES=()
for name in "${PATCH_ORDER[@]}"; do
    p="${PATCH_DIR}/${name}"
    [[ -f "${p}" ]] || die "Missing patch: ${p}"
    PATCH_FILES+=("${p}")
done
# Normalize patch line endings to LF (CRLF patches break context matching).
for p in "${PATCH_FILES[@]}"; do
    sed -i 's/\r$//' "$p"
done
PATCH_HASH="$(cat "${PATCH_FILES[@]}" | sha256sum | cut -d' ' -f1)"

PROFILE="a100"
CONSTANTS="${SCRIPT_DIR}/../common/constants.yaml"
[[ -r "${CONSTANTS}" ]] || die "Missing ${CONSTANTS}"
CONSTANTS_ENV="$(python3 "${SCRIPT_DIR}/../tools/read-constants.py" "${CONSTANTS}" "${PATCH_DIR}" "${SCRIPT_DIR}/build.sh" "${PROFILE}")" || die "common/constants.yaml rejected (see error above)"
eval "${CONSTANTS_ENV}"

#
# HBM memory clock (NDIV) - optional, from install.sh --mclk-ndiv=N.
# clock = NDIV * 27 MHz. Valid range 30-80 (810-2160 MHz).
# A100 80GB stock HBM clock is 1593 MHz = NDIV 59.
#
MCLK_NDIV="${CMPUNLOCKER_MCLK_NDIV:-}"
if [[ -n "${MCLK_NDIV}" ]]; then
    if ! [[ "${MCLK_NDIV}" =~ ^[0-9]+$ ]] || (( MCLK_NDIV < 30 || MCLK_NDIV > 80 )); then
        die "CMPUNLOCKER_MCLK_NDIV must be an integer between 30 and 80 (got: '${MCLK_NDIV}')"
    fi
    ok "HBM NDIV set to ${MCLK_NDIV} ($(( MCLK_NDIV * 27 )) MHz)"
else
    info "No CMPUNLOCKER_MCLK_NDIV: HBM overclock compiled out"
fi

#
# SM unlock - optional, from install.sh --sm-unlock.
# With it: SM-RECONFIG (all 128 SMs) compiled in.
# Without it: native A100 GSP floorsweep (124 SMs).
# The SM_SPEED FMA-throttle lift is ALWAYS applied, either way.
#
SM_UNLOCK="${CMPUNLOCKER_SM_UNLOCK:-}"
if [[ -n "${SM_UNLOCK}" ]]; then
    [[ "${SM_UNLOCK}" == "1" ]] || die "CMPUNLOCKER_SM_UNLOCK must be 1 or empty (got: '${SM_UNLOCK}')"
    ok "SM unlock ON: 128 SMs (TPC reconfig)"
else
    info "No CMPUNLOCKER_SM_UNLOCK: native SM count (124, A100 GSP floorsweep)"
fi
info "SM_SPEED FMA lift: always on (full FMA speed even at 124 SMs)"

BUILD_STAMP="${VERSION}:${KVER}:${PROFILE}:${MCLK_NDIV:-stock}:${SM_UNLOCK:-nosm}:${PATCH_HASH}:$(sha256sum "${SCRIPT_DIR}/build.sh" | cut -d' ' -f1)"

mkdir -p "${BUILD_ROOT}"

if [[ ! -f "${TARBALL}" ]]; then
    info "Downloading open-gpu-kernel-modules ${VERSION}..."
    curl -L --fail -o "${TARBALL}.partial" "${TARBALL_URL}"
    mv "${TARBALL}.partial" "${TARBALL}"
    ok "Downloaded ${TARBALL}"
else
    ok "Using cached tarball ${TARBALL}"
fi

STAMP_FILE="${SRC_DIR}/.cmpunlocker-stamp"
if [[ -d "${SRC_DIR}" ]] && [[ "$(cat "${STAMP_FILE}" 2>/dev/null || true)" == "${BUILD_STAMP}" ]]; then
    SKIP_PREP=1
    ok "Source tree already extracted and patched for this exact build; reusing it"
else
    SKIP_PREP=0
    info "Extracting sources..."
    rm -rf "${SRC_DIR}"
    tar -xzf "${TARBALL}" -C "${BUILD_ROOT}"
    if [[ ! -d "${SRC_DIR}" ]]; then
        extracted="$(find "${BUILD_ROOT}" -maxdepth 1 -type d -name "${SRC_NAME}*" | head -1)"
        [[ -n "${extracted}" ]] || die "Extracted source tree not found"
        mv "${extracted}" "${SRC_DIR}"
    fi
    ok "Sources ready: ${SRC_DIR}"

    info "Applying unlock patches..."
    cd "${SRC_DIR}"
    for i in "${!PATCH_ORDER[@]}"; do
        name="${PATCH_ORDER[$i]}"
        info "  ${name}"
        if [[ "${name}" == "hbm-ndiv.patch" ]]; then
            # Applied marker-based (unique anchors), not via patch(1).
            # patches/hbm-ndiv.patch stays the register-map source of truth.
            python3 "${SCRIPT_DIR}/apply-ndiv.py" "${SRC_DIR}"
        else
            patch -p1 < "${PATCH_FILES[$i]}"
        fi
    done
    ok "All patches applied"

    GSP_C="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
    [[ -f "${GSP_C}" ]] || die "Missing ${GSP_C} after patching"
    info "Native A100 geometry: no CFG1/LMR/FB rewrite (80GB is native)"

    printf '%s\n' "${BUILD_STAMP}" > "${STAMP_FILE}"
fi

cd "${SRC_DIR}"
mkdir -p "${INSTALL_MOD_DIR}"
printf '%s\n' "${VERSION}" > "${INSTALL_MOD_DIR}/driver_version"
printf '%s\n' "${PROFILE}" > "${INSTALL_MOD_DIR}/card_profile"
printf '%s\n' "${UNLOCK_LABEL}" > "${INSTALL_MOD_DIR}/unlock_geometry"

info "Generating unlock config header (SM / NDIV)..."
CFG_HDR="${SRC_DIR}/src/nvidia/inc/kernel/gpu/cmp_hbm_ndiv_cfg.h"
mkdir -p "$(dirname "${CFG_HDR}")"
{
    echo "/* Generated by driver/build.sh. Do not edit. */"
    echo "#ifndef CMP_HBM_NDIV_CFG_H"
    echo "#define CMP_HBM_NDIV_CFG_H"
    echo ""
    echo "struct OBJGPU;"
    echo "struct KernelGsp;"
    if [[ -n "${SM_UNLOCK}" ]]; then
        echo "#define CMPUNLOCK_SM_UNLOCK 1"
    fi
    if [[ -n "${MCLK_NDIV}" ]]; then
        echo "#define CMPUNLOCK_MCLK_NDIV ${MCLK_NDIV}"
        echo "void kgspCmpHbmNdivPostBooterLoad(struct OBJGPU *pGpu, struct KernelGsp *pKernelGsp);"
        echo "void kgspCmpHbmNdivPostGsp(struct OBJGPU *pGpu, struct KernelGsp *pKernelGsp);"
    fi
    echo "#endif"
} > "${CFG_HDR}"
ok "Wrote ${CFG_HDR}"

info "Installing HBM NDIV implementation header..."
IMPL_TMPL="${SCRIPT_DIR}/inc/cmp_hbm_ndiv_impl.h"
[[ -f "${IMPL_TMPL}" ]] || die "Missing ${IMPL_TMPL}"
IMPL_HDR="${SRC_DIR}/src/nvidia/inc/kernel/gpu/cmp_hbm_ndiv_impl.h"
mkdir -p "$(dirname "${IMPL_HDR}")"
cp "${IMPL_TMPL}" "${IMPL_HDR}"
ok "Wrote ${IMPL_HDR}"

info "Building modules for kernel ${KVER}..."
find . -name "*.sh" -exec chmod +x {} + 2>/dev/null || true
if [[ "${SKIP_PREP}" -eq 0 ]]; then
    rm -rf src/nvidia/_out src/nvidia-modeset/_out kernel-open/conftest 2>/dev/null || true
else
    info "Reusing prior build output - incremental rebuild"
fi

JOBS="$(nproc)"
CC_CMD="gcc"
if command -v ccache &>/dev/null; then
    CC_CMD="ccache gcc"
    info "ccache detected - compiler output will be cached for faster rebuilds"
fi
make -j"${JOBS}" modules SYSSRC="${KSRC}" CC="${CC_CMD}"
ok "Modules built"

if command -v ccache &>/dev/null; then
    ccache -s 2>/dev/null | sed 's/^/  /' || true
fi
info "Installing modules to ${INSTALL_MOD_DIR}..."
mkdir -p "${INSTALL_MOD_DIR}"

mapfile -t KO_FILES < <(find "${SRC_DIR}" -type f \( \
    -name 'nvidia.ko' -o -name 'nvidia-modeset.ko' -o -name 'nvidia-uvm.ko' \
    -o -name 'nvidia-drm.ko' -o -name 'nvidia-peermem.ko' \) \
    ! -path '*/conftest/*' | sort -u)
[[ ${#KO_FILES[@]} -gt 0 ]] || die "No built nvidia*.ko found"

for ko in "${KO_FILES[@]}"; do
    base="$(basename "${ko}")"
    install -m 0644 "${ko}" "${INSTALL_MOD_DIR}/${base}"
    ok "Installed ${base}"
done

depmod -a "${KVER}"
ok "depmod complete"
rebuild_initramfs() {
    if command -v update-initramfs &>/dev/null; then
        info "Rebuilding initramfs (update-initramfs)..."
        update-initramfs -u -k "${KVER}"
        ok "initramfs rebuilt"
        return 0
    fi
    if command -v dracut &>/dev/null; then
        info "Rebuilding initramfs (dracut)..."
        dracut --force --kver "${KVER}"
        ok "initramfs rebuilt"
        return 0
    fi
    if command -v mkinitcpio &>/dev/null; then
        info "Rebuilding initramfs (mkinitcpio)..."
        mkinitcpio -P
        ok "initramfs rebuilt"
        return 0
    fi
    warn "No initramfs tool found - rebuild manually before rebooting"
    return 1
}

rebuild_initramfs || true
resolved="$(modprobe -n -v nvidia 2>/dev/null | awk '/insmod/ {print $2; exit}' || true)"
if [[ -n "${resolved}" ]]; then
    info "modprobe will load: ${resolved}"
    if [[ "${resolved}" != *"/updates/cmpunlocker/"* ]]; then
        warn "Resolved nvidia.ko is not under updates/cmpunlocker/"
    fi
fi
info "Attempting to unload NVIDIA modules..."
systemctl stop nvidia-persistenced 2>/dev/null || true
systemctl stop nvidia-fabricmanager 2>/dev/null || true
reload_ok=0
if grep -q '^nvidia' /proc/modules; then
    for mod in nvidia_drm nvidia_uvm nvidia_modeset nvidia; do
        modprobe -r "${mod}" 2>/dev/null || true
    done
    sleep 1
fi

if ! grep -q '^nvidia ' /proc/modules; then
    if modprobe nvidia && modprobe nvidia-modeset; then
        modprobe nvidia-uvm 2>/dev/null || true
        modprobe nvidia-drm 2>/dev/null || true
        reload_ok=1
        ok "Patched NVIDIA modules loaded"
        running_src="$(cat /sys/module/nvidia/srcversion 2>/dev/null || true)"
        patched_src="$(modinfo -F srcversion "${INSTALL_MOD_DIR}/nvidia.ko" 2>/dev/null || true)"
        if [[ -n "${running_src}" && -n "${patched_src}" && "${running_src}" != "${patched_src}" ]]; then
            warn "Loaded nvidia srcversion (${running_src}) != patched (${patched_src})"
            reload_ok=0
        fi
    else
        warn "modprobe failed"
    fi
else
    warn "Could not unload nvidia modules"
fi
echo ""
if [[ "${reload_ok}" -eq 1 ]]; then
    ok "Build and install finished. Verify with: nvidia-smi"
    info "SM reconfig + NDIV run at driver init; a cold reboot is the clean path."
else
    warn "Modules installed but running driver is still stock."
    info "Perform cold reboot: shutdown -h now"
fi
echo ""
