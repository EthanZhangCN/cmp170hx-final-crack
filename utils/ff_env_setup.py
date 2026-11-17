#!/usr/bin/env python3
"""CMP 170HX 主机侧环境准备 · 一键脚本.

覆盖 README 阶段 −1 的软件侧(工具链安装 → pico-sdk → 固件编译 →
BOOTSEL 烧写 uf2 → host CLI 安装),全部幂等可重跑。毛刺到烧录的流程
由 utils/ff_burn_flow.py 负责,两脚本零耦合。

用法(在仓库根目录):
    python3  utils/ff_env_setup.py check                  # 只检测,不安装,无需 root
    sudo python3 utils/ff_env_setup.py all --board pico2_w # 全流程(板型必填)
    sudo python3 utils/ff_env_setup.py tools               # 单步:装 apt 工具链
    sudo python3 utils/ff_env_setup.py sdk                 # 单步:clone pico-sdk
    python3  utils/ff_env_setup.py build --board pico2     # 单步:编固件
    python3  utils/ff_env_setup.py flash --board pico2_w   # 单步:BOOTSEL 烧写
    python3  utils/ff_env_setup.py hostcli                 # 单步:pip 装 host CLI

板型(--board):pico2_w = Pico 2 W / Waveshare Plus-W(RP2350B,默认验证板);
pico2 = Pico 2(RP2350A)。必须显式指定,防止烧错板型固件。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE_DIR = os.path.join(REPO_ROOT, "pico-ga100vfi", "firmware")
HOST_DIR = os.path.join(REPO_ROOT, "pico-ga100vfi", "host")
PICO_SDK_URL = "https://github.com/raspberrypi/pico-sdk.git"
RP2350_MOUNT = "RP2350"

APT_PACKAGES = ["cmake", "ninja-build", "gcc-arm-none-eabi"]
TOOL_PROBES = {"cmake": "cmake", "ninja": "ninja", "arm-none-eabi-gcc": "gcc-arm-none-eabi"}
BOARD_OPTS = {"pico2_w": ("pico2_w", "BOARD_RP2350B"),
              "pico2": ("pico2", "BOARD_RP2350A")}


class EnvError(Exception):
    pass


def sh(cmd: list, env_extra: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    if env_extra:
        e.update(env_extra)
    p = subprocess.run(cmd, cwd=REPO_ROOT, env=e)
    if check and p.returncode != 0:
        raise EnvError(f"命令失败({p.returncode}):{' '.join(cmd)}")
    return p


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# ------------------------------------------------------------------ check ----
def check_status(sdk_dir: str) -> tuple:
    """返回 (缺失工具列表, 缺失项的可读报告)。只读,不安装。"""
    missing_tools = [t for t in TOOL_PROBES if not have(t)]
    sdk_ok = os.path.isfile(os.path.join(sdk_dir, "pico_sdk_init.cmake"))
    try:
        import serial  # noqa: F401
        pyserial_ok = True
    except ImportError:
        pyserial_ok = False
    return missing_tools, {"sdk": sdk_ok, "pyserial": pyserial_ok}


def print_check(sdk_dir: str) -> int:
    missing, state = check_status(sdk_dir)
    print("== 环境检测(不安装)==")
    for t in TOOL_PROBES:
        print(f"  [{'OK' if have(t) else '缺失'}] {t}")
    print(f"  [{'OK' if state['sdk'] else '缺失'}] pico-sdk @ {sdk_dir}"
          f"(或设 PICO_SDK_PATH)")
    print(f"  [{'OK' if state['pyserial'] else '缺失'}] pyserial(host CLI 依赖)")
    if missing or not state["sdk"] or not state["pyserial"]:
        print("\n修复:sudo python3 utils/ff_env_setup.py all --board <pico2_w|pico2>")
        print("  (或单步:tools / sdk / hostcli;build 前须装齐)")
        return 1
    print("\n环境就绪。下一步:烧固件 flash,或直接跑 ff_burn_flow.py run")
    return 0


# ------------------------------------------------------------------ steps ----
def step_tools() -> None:
    print("== tools: apt 安装缺失工具链 ==")
    if os.geteuid() != 0:
        raise EnvError("需要 root:sudo 运行,或先用 check 查看")
    missing_pkgs = [pkg for tool, pkg in TOOL_PROBES.items() if not have(tool)]
    if not missing_pkgs:
        print("  工具链已齐,跳过")
        return
    print(f"  安装:{' '.join(missing_pkgs)}")
    sh(["apt-get", "update", "-qq"])
    sh(["apt-get", "install", "-y"] + missing_pkgs)
    still = [t for t in TOOL_PROBES if not have(t)]
    if still:
        raise EnvError(f"安装后仍找不到:{still}(PATH 问题?重开终端再试)")
    print("  工具链 OK")


def step_sdk(sdk_dir: str) -> None:
    print(f"== sdk: pico-sdk @ {sdk_dir} ==")
    if os.path.isfile(os.path.join(sdk_dir, "pico_sdk_init.cmake")):
        print("  已存在,跳过")
        return
    if os.path.exists(sdk_dir):
        raise EnvError(
            f"{sdk_dir} 已存在但缺少 pico_sdk_init.cmake(不完整的 clone?)。\n"
            f"  → 确认无自定义内容后删除该目录,重跑本步")
    parent = os.path.dirname(sdk_dir.rstrip("/"))
    os.makedirs(parent, exist_ok=True)
    try:
        sh(["git", "clone", "--depth", "1", PICO_SDK_URL, sdk_dir])
    except EnvError:
        raise EnvError(
            "clone pico-sdk 失败(网络?)。手动:\n"
            f"  git clone --depth 1 {PICO_SDK_URL} {sdk_dir}")
    print("  clone OK(RP2350 标准 board 无需 submodules)")


def step_build(board: str, sdk_dir: str, build_dir: str) -> str:
    print(f"== build: 固件编译(board={board})==")
    if board not in BOARD_OPTS:
        raise EnvError(f"未知板型 {board!r};可选:{' / '.join(BOARD_OPTS)}")
    pico_board, board_opt = BOARD_OPTS[board]
    if not os.path.isfile(os.path.join(sdk_dir, "pico_sdk_init.cmake")):
        raise EnvError(f"pico-sdk 不在 {sdk_dir} —— 先跑 sdk 步(或设 PICO_SDK_PATH)")
    if not all(have(t) for t in TOOL_PROBES):
        raise EnvError("工具链缺失(cmake/ninja/arm-gcc)—— 先跑 tools 步")
    build_dir = os.path.abspath(os.path.join(REPO_ROOT, build_dir))
    sh(["cmake", "-S", FIRMWARE_DIR, "-B", build_dir,
        "-DPICO_BOARD=" + pico_board, f"-D{board_opt}=ON", "-G", "Ninja"],
       env_extra={"PICO_SDK_PATH": sdk_dir})
    sh(["ninja", "-C", build_dir], env_extra={"PICO_SDK_PATH": sdk_dir})
    uf2 = os.path.join(build_dir, "pwmvid.uf2")
    if not os.path.isfile(uf2):
        raise EnvError(f"编译完成但未见 {uf2} —— 看上方 ninja 输出")
    print(f"  产物:{uf2}(detect.uf2 同目录)")
    return uf2


def _wait_mount(timeout: int) -> str:
    base = os.path.join("/media", os.environ.get("SUDO_USER") or os.environ.get("USER") or "")
    print(f"  等待 {RP2350_MOUNT} 盘出现(按住 BOOTSEL 插 USB)…")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for user_dir in (os.listdir("/media") if os.path.isdir("/media") else []):
            mnt = os.path.join("/media", user_dir, RP2350_MOUNT)
            if os.path.ismount(mnt) or os.path.isdir(mnt):
                return mnt
        if not base:
            break
        time.sleep(2)
    raise EnvError(f"{timeout}s 内未见 {RP2350_MOUNT} 挂载——手动 cp 或检查 udev/udisks")


def step_flash(board: str, build_dir: str, timeout: int, with_detect: bool = True) -> None:
    print(f"== flash: BOOTSEL 烧写(board={board})==")
    uf2 = step_flash_find(build_dir)
    mnt = _wait_mount(timeout)
    dst = os.path.join(mnt, os.path.basename(uf2))
    print(f"  拷 {uf2} → {dst}")
    shutil.copy(uf2, dst)
    # 拷完固件自重启,盘会消失
    for _ in range(30):
        if not os.path.exists(dst):
            print("  烧写完成(盘已消失,固件已运行)")
            break
        time.sleep(1)
    else:
        print("  ⚠ 盘未消失——可能没烧进去,重插 BOOTSEL 检查")
    if with_detect:
        detect = os.path.join(os.path.dirname(uf2), "detect.uf2")
        if os.path.isfile(detect):
            print("\n  (可选)detect.uf2 是独立 I2C 探针固件,需要时:")
            print(f"    重新 BOOTSEL 后手动:cp {detect} /media/$USER/{RP2350_MOUNT}/")
    print("  验证:接好 4 根线后跑 ff_burn_flow.py run —— glitch 阶段的"
          " unlock MATCH 即固件握手成功的判据(见上游 docs/BUILD.md)")


def step_flash_find(build_dir: str) -> str:
    uf2 = os.path.abspath(os.path.join(REPO_ROOT, build_dir, "pwmvid.uf2"))
    if not os.path.isfile(uf2):
        raise EnvError(f"未找到 {uf2} —— 先跑 build 步")
    return uf2


def step_hostcli() -> None:
    print("== hostcli: pip 安装 ga100vfi host CLI ==")
    sh([sys.executable, "-m", "pip", "install", "-e", HOST_DIR], check=False)
    probe = subprocess.run([sys.executable, "-c", "import serial, ga100vfi"],
                           capture_output=True)
    if probe.returncode != 0:
        # pico-ga100vfi/host 未进 sys.path 时用 PYTHONPATH 探测(文档要求的方式)
        env = dict(os.environ, PYTHONPATH=HOST_DIR)
        probe2 = subprocess.run([sys.executable, "-c", "import serial, ga100vfi"],
                                capture_output=True, env=env)
        if probe2.returncode != 0:
            raise EnvError(
                "host CLI 不可 import。手动:\n"
                f"  pip install -e {HOST_DIR}\n"
                f"  或每次运行前 export PYTHONPATH={HOST_DIR}")
        print("  OK(需 PYTHONPATH 指向 host/,burn 脚本已自动处理)")
        return
    print("  OK")


def step_all(args) -> None:
    step_tools()
    step_sdk(args.sdk_dir)
    uf2 = step_build(args.board, args.sdk_dir, args.build_dir)
    step_flash(args.board, args.build_dir, args.flash_timeout,
               with_detect=not args.no_detect)
    step_hostcli()
    print(f"""
== 环境就绪 ==
固件:{uf2}
下一步(接好 Pico 4 根线后):
  sudo python3 utils/ff_burn_flow.py run --bdf <bdf>
  (首次烧卡建议加 --single 走单行试烧)""")


# ------------------------------------------------------------------ main ----
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ff_env_setup",
        description="CMP 170HX 主机侧环境准备(工具链/pico-sdk/固件/uf2 烧写/host CLI);"
                    "毛刺到烧录用 utils/ff_burn_flow.py。幂等可重跑。")
    ap.add_argument("--board", choices=list(BOARD_OPTS), default=None,
                    help="板型,build/flash/all 必填:pico2_w(Pico 2 W/Waveshare Plus-W)"
                         " 或 pico2(Pico 2)。无默认值,防烧错板")
    ap.add_argument("--sdk-dir", default=os.path.expanduser("~/pico-sdk"))
    ap.add_argument("--build-dir", default=os.path.join("pico-ga100vfi", "build"),
                    help="相对仓库根的构建目录")
    ap.add_argument("--flash-timeout", type=int, default=120,
                    help="等 BOOTSEL 挂载的秒数")
    ap.add_argument("--no-detect", action="store_true",
                    help="flash 后不提示 detect.uf2")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", help="只检测不安装(无需 root)")
    sub.add_parser("tools", help="apt 安装缺失工具链")
    sub.add_parser("sdk", help="clone pico-sdk(浅克隆)")
    sub.add_parser("build", help="编译固件(需 --board)")
    sub.add_parser("flash", help="BOOTSEL 烧写 pwmvid.uf2(需 --board)")
    sub.add_parser("hostcli", help="pip 安装 host CLI")
    sub.add_parser("all", help="全流程:tools→sdk→build→flash→hostcli")
    args = ap.parse_args()

    if args.cmd is None:
        ap.print_help()
        return 2
    if args.cmd == "check":
        return print_check(args.sdk_dir)
    if args.cmd in ("build", "flash", "all") and not args.board:
        print("错误:build/flash/all 必须显式 --board pico2_w 或 pico2"
              "(防止烧错板型固件)", file=sys.stderr)
        return 1
    try:
        if args.cmd == "tools":
            step_tools()
        elif args.cmd == "sdk":
            step_sdk(args.sdk_dir)
        elif args.cmd == "build":
            step_build(args.board, args.sdk_dir, args.build_dir)
        elif args.cmd == "flash":
            step_flash(args.board, args.build_dir, args.flash_timeout,
                       with_detect=not args.no_detect)
        elif args.cmd == "hostcli":
            step_hostcli()
        elif args.cmd == "all":
            step_all(args)
    except EnvError as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
