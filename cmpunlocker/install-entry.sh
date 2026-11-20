#!/usr/bin/env bash
# install-entry.sh - 自解压安装包入口(烧完 FF 冷启后一键装到验证)
#
# 阶段:彻底清理旧驱动 → 检查用户态驱动 → install.sh(含 pcie-gen3.patch 模块)
#      → 编 cmp-pcie-speed → 写 cmp-gen3.conf → 逐卡 Gen3 分阶训练
#      → Gen4(E1+setpci,默认执行)→ verify + 带宽
# NVLink 解锁在 FF 层已完成(烧 NVLINK_DISABLE := DEFECTIVE);驱动层无操作,
#      U11 硬改前仅解锁层生效(README 5.3)。
#
# 用法: sudo ./cmp170hx-ff-install.elf [--bdf 0000:xx:00.0] [install.sh 参数...]
#       默认全功能(Gen3 训练 + Gen4 retrain 都执行);--no-gen4 只到 Gen3。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED_DRIVER=615.71.09
GEN3_KEYS="RmForceEnableGen2=1;RMPcieLinkSpeed=0x5;CmpGen3=1"

DO_GEN4=1
BDF_ONLY=""
PASS_ARGS=()
prev_bdf=0
for arg in "$@"; do
    if (( prev_bdf )); then BDF_ONLY="${arg}"; prev_bdf=0; continue; fi
    case "${arg}" in
        --self-test)  SELF_TEST=1 ;;
        --no-gen4)    DO_GEN4=0 ;;
        --bdf=*)      BDF_ONLY="${arg#--bdf=}" ;;
        --bdf)        prev_bdf=1 ;;
        *)            PASS_ARGS+=("${arg}") ;;
    esac
done

ok()   { printf '  [ OK ] %s\n' "$*"; }
info() { printf '  [ .. ] %s\n' "$*"; }
err()  { printf '  [FAIL] %s\n' "$*" >&2; }

# ---------------------------------------------------------------- 阶段 0/自检
if [[ "${SELF_TEST:-0}" == 1 ]]; then
    echo "== self-test: 校验 payload 完整性(不改动系统)=="
    mapfile -t NEED \
        < <(printf '%s\n' \
            install.sh verify.sh driver/build.sh \
            driver/patches/pcie-gen3.patch \
            driver/.build/open-gpu-kernel-modules-615.71.09.tar.gz \
            tools/cmp-gen3.sh tools/set_pcie_speed.c \
            tools/pcie_gen4_probe.py tools/pcie_bandwidth.py)
    fail=0
    for f in "${NEED[@]}"; do
        if [[ -f "${SCRIPT_DIR}/${f}" ]]; then ok "${f}"; else err "缺失 ${f}"; fail=1; fi
    done
    grep -q 'pcie-gen3.patch' "${SCRIPT_DIR}/driver/build.sh" \
        && ok "build.sh PATCH_ORDER 已含 pcie-gen3.patch" \
        || { err "build.sh 未注入 pcie-gen3.patch"; fail=1; }
    (( fail == 0 )) && { echo "== self-test OK =="; exit 0; } || { echo "== self-test FAILED =="; exit 1; }
fi

[[ "${EUID}" -eq 0 ]] || { err "需要 root"; exit 1; }

# ------------------------------------------------------ 阶段 0 彻底清理旧驱动
echo "== [0/7] 彻底清理既有 nvidia 驱动(内核模块/服务/黑名单 nouveau)=="
purge_info() { printf '  [ .. ] %s\n' "$*"; }

purge_info "停用 nvidia 相关服务"
for svc in nvidia-persistenced nvidia-fabricmanager nvidia-suspend \
           nvidia-resume nvidia-hibernate nvidia-powerd docker libvirt-bin; do
    systemctl disable --now "${svc}" 2>/dev/null || true
done

purge_info "卸载内存中的 nvidia 内核模块(依赖序)"
for i in 1 2 3; do
    if lsmod | grep -q '^nvidia'; then
        modprobe -r nvidia_drm nvidia_uvm nvidia_modeset nvidia 2>/dev/null || true
        sleep 1
    fi
done
if lsmod | grep -q '^nvidia'; then
    err "nvidia 模块仍在内存(有进程占用)。lsof /dev/nvidia* 查占用后重跑;"
    err "仍不行则物理断电冷启再跑本包。"
    exit 1
fi

purge_info "移除 DKMS nvidia 模块(全部版本)"
if command -v dkms &>/dev/null; then
    for m in $(dkms status 2>/dev/null | awk -F'[,:/]' '{print $1}' | sort -u); do
        [[ "${m}" == nvidia* ]] && dkms remove "${m}/all" --all 2>/dev/null || true
    done
fi
depmod -a 2>/dev/null || true

purge_info "卸载 .run 安装的旧内核模块(如有)"
if command -v nvidia-uninstall &>/dev/null; then
    nvidia-uninstall -s 2>/dev/null || true
fi

purge_info "删除旧 cmpunlocker 模块目录(将被本包重建)"
rm -rf "/lib/modules/$(uname -r)/updates/cmpunlocker" 2>/dev/null || true

purge_info "清理 cmpunlocker/gen2/gen3 旧配置(防旧键覆盖)"
rm -f /etc/modprobe.d/cmp-pcie-gen2.conf /etc/modprobe.d/cmp-gen3.conf \
      /etc/modprobe.d/cmpretrain.conf 2>/dev/null || true
# 烧 FF 期间 campaign 写的 nvidia 黑名单(install nvidia /bin/false 会挡死
# 驱动加载)——装机的本意就是加载驱动,必须删。
rm -f /etc/modprobe.d/aa-blacklist-nvidia-campaign.conf \
      /etc/modprobe.d/cmp-preflight.conf.disabled 2>/dev/null || true
systemctl disable --now gen2.service cmpretrain.service cmp-gen2-retrain.service 2>/dev/null || true
rm -f /etc/systemd/system/gen2.service /etc/systemd/system/cmpretrain.service \
      /etc/systemd/system/cmp-gen2-retrain.service 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

purge_info "确保 nouveau 黑名单(与 nvidia-open 并存要求)"
mkdir -p /etc/modprobe.d
if ! grep -rq '^blacklist nouveau' /etc/modprobe.d/ 2>/dev/null; then
    printf 'blacklist nouveau\noptions nouveau modeset=0\n' > /etc/modprobe.d/aa-blacklist-nouveau.conf
    initramfs_cmd=""
    command -v update-initramfs &>/dev/null && initramfs_cmd="update-initramfs -u"
    command -v dracut &>/dev/null && initramfs_cmd="dracut --force"
    [[ -n "${initramfs_cmd}" ]] && ${initramfs_cmd} 2>/dev/null || true
fi
printf '  [ OK ] 旧驱动清理完成\n'

# ------------------------------------------------------------ 阶段 1 用户态驱动
echo "== [1/7] 检查 nvidia-open 用户态(${EXPECTED_DRIVER})=="
RUNNING_DRIVER="$(modinfo -F version nvidia 2>/dev/null || true)"
if [[ -z "${RUNNING_DRIVER}" ]]; then
    RUNNING_DRIVER="$(ls -d /lib/firmware/nvidia/*/ 2>/dev/null | sed 's|.*/nvidia/||;s|/||' | sort -rV | head -1 || true)"
fi
if [[ "${RUNNING_DRIVER}" != "${EXPECTED_DRIVER}" ]]; then
    err "检测到 '${RUNNING_DRIVER:-none}',需要 nvidia-open ${EXPECTED_DRIVER} 用户态(libs+firmware)。"
    err "  本包不含官方 .run(体积/目标机匹配);请先用发行版包管理器安装 nvidia-open ${EXPECTED_DRIVER},"
    err "  例如 Ubuntu: sudo apt install nvidia-open-${EXPECTED_DRIVER//./-}"
    err "  装好后重跑本安装包。"
    exit 1
fi
ok "nvidia-open ${EXPECTED_DRIVER} 用户态在位"

# -------------------------------------------------- 阶段 2 install.sh(含 gen3 补丁)
echo "== [2/7] 编译安装解锁模块(离线源码缓存,含 pcie-gen3.patch;禁 Gen2 服务)=="
CMPUNLOCKER_DRIVER_VERSION="${EXPECTED_DRIVER}" \
    bash "${SCRIPT_DIR}/install.sh" --no-gen2-service "${PASS_ARGS[@]}"
ok "模块安装完成"

# ------------------------------------------------------- 阶段 3 编 cmp-pcie-speed
echo "== [3/7] 编译 cmp-pcie-speed(RM 分阶训练工具)=="
SRC_TREE="${SCRIPT_DIR}/driver/.build/open-gpu-kernel-modules-${EXPECTED_DRIVER}"
[[ -d "${SRC_TREE}" ]] || { err "源码树 ${SRC_TREE} 不存在(构建后应保留)"; exit 1; }
cc -std=gnu11 -O2 -I"${SRC_TREE}/src/common/sdk/nvidia/inc" \
   "${SCRIPT_DIR}/tools/set_pcie_speed.c" -o /usr/local/sbin/cmp-pcie-speed
chmod 755 /usr/local/sbin/cmp-pcie-speed
ok "/usr/local/sbin/cmp-pcie-speed 就位"

# ------------------------------------------------------------- 阶段 4 Gen3 持久化
echo "== [4/7] 写 /etc/modprobe.d/cmp-gen3.conf(CmpGen3=1)=="
# 先删 install.sh 刚写的 gen2 conf(其 RMPcieLinkSpeed=0x1 会被下方合并逻辑保留并废掉 Gen3),
# 再从残余配置合并;GEN3_KEYS 键强制覆盖(旧值不保留)。
rm -f /etc/modprobe.d/cmp-pcie-gen2.conf   # 防 0x1 字典序覆盖 0x5;且防合并逻辑吃进 0x1
EXISTING_RD="$(modprobe -c 2>/dev/null | sed -n 's/^options nvidia .*NVreg_RegistryDwords="\([^"]*\)".*/\1/p' | head -1)"
if [[ -z "${EXISTING_RD}" ]]; then
    MERGED_RD="${GEN3_KEYS}"
else
    # 剔除与 GEN3_KEYS 冲突的旧键,再追加 GEN3_KEYS(强制覆盖语义)
    FILTERED_RD=""
    for oldkv in ${EXISTING_RD//;/ }; do
        okey="${oldkv%%=*}"
        skip=0
        for kv in ${GEN3_KEYS//;/ }; do
            [[ "${okey}" == "${kv%%=*}" ]] && skip=1 && break
        done
        (( skip )) || FILTERED_RD="${FILTERED_RD:+${FILTERED_RD};}${oldkv}"
    done
    MERGED_RD="${FILTERED_RD:+${FILTERED_RD};}${GEN3_KEYS}"
fi
printf 'options nvidia NVreg_RegistryDwords="%s"\n' "${MERGED_RD}" > /etc/modprobe.d/cmp-gen3.conf
rm -f /etc/modprobe.d/cmp-pcie-gen2.conf   # 防 RMPcieLinkSpeed=0x1 字典序覆盖 0x5
ok "cmp-gen3.conf 写入(RmForceEnableGen2=1;RMPcieLinkSpeed=0x5;CmpGen3=1)"
[[ -n "${EXISTING_RD}" ]] && info "已合并既有 NVreg_RegistryDwords;请确认无其他 conf 覆盖"

# ------------------------------------------------------------- 阶段 5 Gen3 训练
echo "== [5/7] 逐卡 Gen3 分阶训练(幂等;已是 Gen3 则跳过)=="
mapfile -t BDFS < <(lspci -D 2>/dev/null | awk '/10de:20c2|10de:2082/ {print $1}')
if [[ -n "${BDF_ONLY}" ]]; then BDFS=("${BDF_ONLY}"); fi
(( ${#BDFS[@]} > 0 )) || { err "未发现 CMP 170HX(10de:20c2/2082);用 --bdf= 指定"; exit 1; }
declare -A G3RESULT
G3FAIL=0
for bdf in "${BDFS[@]}"; do
    if bash "${SCRIPT_DIR}/tools/cmp-gen3.sh" "${bdf}"; then
        G3RESULT[${bdf}]="Gen3 OK"
    else
        G3RESULT[${bdf}]="FAILED"; G3FAIL=1
    fi
done

# ------------------------------------------------------------------ 阶段 6a Gen4
if (( DO_GEN4 == 1 )); then
    echo "== [6/7] Gen4(易失;每次冷启后需重做)=="
    cat <<'WARN'
  ┌─────────────────────────────────────────────────────────────────────┐
  │ ⛔ Gen4 风险须知                                                     │
  │  • E1 PHY init 写 0x88CE0/CE4、CYA_0、PL_RATE、0x8C040(清位)、VSEC,  │
  │    全部易失(冷启回 Gen3);0x8C040 仅允许本 probe 脚本写,           │
  │    正常运行态禁写 0x8C040/0x880A8(README 血泪红线)                │
  │  • setpci retrain 可能瞬断链路;掉卡即停,物理断电冷启,勿循环重试   │
  │  • 硬件前提:Gen4 根端口 + 24×0402 电容 mod + 直插 x16,缺一则停在 Gen3 │
  └─────────────────────────────────────────────────────────────────────┘
WARN
    read -r -p '  确认继续请键入 GEN4: ' ans
    [[ "${ans}" == "GEN4" ]] || { info "跳过 Gen4"; }
    if [[ "${ans}" == "GEN4" ]]; then
        for bdf in "${BDFS[@]}"; do
            [[ "${G3RESULT[${bdf}]}" == "Gen3 OK" ]] || { info "${bdf}: Gen3 未成,跳过 Gen4"; continue; }
            python3 "${SCRIPT_DIR}/tools/pcie_gen4_probe.py" --bdf "${bdf}" --confirm
            PARENT=$(basename "$(dirname "$(readlink -f "/sys/bus/pci/devices/${bdf}")")")
            setpci -s "${PARENT}" CAP_EXP+30.w=0004:000f
            setpci -s "${bdf}"     CAP_EXP+30.w=0004:000f
            setpci -s "${PARENT}" CAP_EXP+10.w=0020:0020
            sleep 2
            STA="$(setpci -s "${bdf}" CAP_EXP+12.w)"
            GEN=$(( 16#${STA} & 15 ))
            if (( GEN == 4 )); then G3RESULT[${bdf}]="Gen4 OK"; ok "${bdf}: LnkSta ${STA} → Gen4"
            else err "${bdf}: LnkSta ${STA} 仍 Gen${GEN} —— 停(勿重试;物理冷启回 Gen3)"; fi
        done
    fi
fi

# ------------------------------------------------------------------ 阶段 6b 验证
echo "== [7/7] 验收:verify.sh + 每卡链路状态与带宽 =="
VERIFY_SH="${SCRIPT_DIR}/verify.sh"
VOUT="$(bash "${VERIFY_SH}" 2>&1 || true)"
echo "${VOUT}"
if echo "${VOUT}" | grep -q "PCIe Gen2 is not active"; then
    info "verify.sh 报 Gen2 未活跃属预期(本装目标是 Gen3+)"
fi
echo "  ─── 每卡汇总 ───"
for bdf in "${BDFS[@]}"; do
    STA="$(setpci -s "${bdf}" CAP_EXP+12.w 2>/dev/null || echo 0000)"
    GEN=$(( 16#${STA:-0} & 15 ))
    BW="$(python3 "${SCRIPT_DIR}/tools/pcie_bandwidth.py" 2>/dev/null | tr '\n' ' ' || echo '带宽测试跳过')"
    printf '  %s  LnkSta=%s(Gen%d)  %s  %s\n' "${bdf}" "${STA}" "${GEN}" "${G3RESULT[${bdf}]:-?}" "${BW}"
done

echo ""
[[ ${G3FAIL} -eq 0 ]] && echo "✅ 安装完成。冷启后 Gen3 需重训:重跑本包(幂等)或 sudo bash /path/cmp-gen3.sh <bdf>" \
                    || echo "⚠ 部分卡 Gen3 未成;查上方输出。冷启后重跑本包即可重试。"
exit "${G3FAIL}"
