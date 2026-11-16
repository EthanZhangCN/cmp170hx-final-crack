#!/usr/bin/env python3
"""GA100 (CMP 170HX) FF 解锁一体工具 — 合并三方所长。

整合来源:
  - ffburn_fixed.py (zhiyu): 修好的 arm/burn 序列 (不碰 PLM、不用 RWL、写口 0x0082000C)
  - ffplan.py (社区):       chain+block→option 映射 + 共享块表 + DEFECTIVE/DISABLE 地址表
  - cmp170hx-ffcalc:        按本卡 _DEFECTIVE 自动算目标值 + 逐卡指纹
  - rirburn.c:              CMD_WRITE/IDLE 判定实测机制

安全(全部内置,不可绕过):
  * 写口是 FUSEWDATA=0x0082000C,绝不写读口 0x00820008
  * 绝不写 PLM 0x008200F4/F8/FC(会把熔丝控制器写死)
  * FF 烧写不 latch DEBUGCTRL.RWL(不需要)
  * 只写 FF 区 row 222-487,且写入前实时判空(读回==0 才写)
  * 每条写完回读校验 + 复读 CRC/DSOV/EFP 确认金窗没被带坏
  * 目标值默认 = 本卡 _DEFECTIVE(只禁真坏的),不照抄别人的卡

用法(必须在 Goldilocks 窗口内,DSOV=0;先 campaign 打毛刺,中间不要断电):
  # 0. 采集(只读):读出本卡缺陷/现状/FF空行,生成计划(不写)
  sudo python3 ga100_ff.py plan --bdf 0000:2f:00.0 -o plan.json

  # 1. 预演:检查金窗 + 打印每条目标行是否空白(不写)
  sudo python3 ga100_ff.py burn --plan plan.json            # dry-run

  # 2. 真烧(永久!)
  sudo python3 ga100_ff.py burn --plan plan.json --commit

  # 3. 冷启后验证(读派生寄存器 + 指定行)
  sudo python3 ga100_ff.py verify --bdf 0000:2f:00.0

依赖:与 pico-ga100vfi 同一主机,需其 Bar0。设 PYTHONPATH 指向 pico-ga100vfi/host,
或把本文件放到该 host 目录旁。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# --- 引入 pico-ga100vfi 的 Bar0(读卡) ---------------------------------------
def _import_bar0():
    try:
        from ga100vfi.bar0 import Bar0, restore
        return Bar0, restore
    except ImportError:
        # 兼容把本文件放在 pico-ga100vfi/host 旁边的情况
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (here, os.path.join(here, "pico-ga100vfi", "host"),
                     os.path.join(here, "host")):
            if os.path.isdir(cand) and cand not in sys.path:
                sys.path.insert(0, cand)
        from ga100vfi.bar0 import Bar0, restore
        return Bar0, restore

# --- fuse 寄存器(权威:dev_fuse.h)-------------------------------------------
FUSECTRL = 0x00820000   # CMD[1:0] 1=READ 2=WRITE 3=SENSE_CTRL; IDLE=0xE0040000
FUSEADDR = 0x00820004   # 行号, VLDFLD 8:0
FUSERDATA = 0x00820008  # 读口(只读)
FUSEWDATA = 0x0082000C  # 写口(-W-4R)★ 不是 0x00820008
EN_SW_OVERRIDE = 0x00820040
CRC_STATUS = 0x00820070
EFP_STATUS = 0x00820078           # ENABLE_FUSE_PROGRAM
DSOV_STATUS = 0x00820084          # DISABLE_SW_OVERRIDE: 0 == 金窗
SBFE = 0x00821108                 # OPT_SECURED_BURN_FEATURE_ENABLE bit0
WDATA_WR_SECURE = 0x0082048C      # OPT_SECURE_FUSE_WDATA_WR_SECURE bit0

CMD_READ, CMD_WRITE, CMD_SENSE = 1, 2, 3
FF_FIRST, FF_LAST = 222, 487      # FF 区
CONTROL_ROWS_MAX = 3              # 控制行 0-3 永远禁写

# --- 映射表(来自 ffplan.py + ffcalc,社区差分逆向)---------------------------
# 每个 option: (chain, bit起, 位宽, defective键)
FIELDS = {
    "fbio_disable":      (1, 83, 24, "fbio_defective"),
    "fbpa_disable":      (1, 107, 24, "fbpa_defective"),
    "gpc_disable":       (1, 131, 8, "gpc_defective"),
    "pes_disable":       (1, 251, 24, "pes_defective"),
    "fbp_disable":       (1, 307, 12, "fbp_defective"),
    "rop_l2_disable":    (1, 319, 24, "rop_l2_defective"),
    "nvlink_disable":    (1, 368, 3, "nvlink_defective"),
    "sys_pipe_disable":  (1, 372, 8, "sys_pipe_defective"),
    # PCIe(chain 2), 无缺陷键,目标=0
    "disable_gen3_speed_bit1": (2, 39 * 16 + 1, 1, None),
    "disable_gen3_speed_bit9": (2, 39 * 16 + 9, 1, None),
    "pcie_magic_gen4_dis":     (2, 47 * 16 + 3, 1, None),
    "pcie_boot_gen23_dis":     (2, 49 * 16 + 6, 1, None),
    "pcie_boot_gen3_dis":      (2, 49 * 16 + 7, 1, None),
}
# TPC 每 GPC 一个 8-bit 掩码
TPC_FIELDS = {"tpc_gpc%d_disable" % i: (1, 187 + 8 * i, 8, "tpc_gpc%d_defective" % i)
              for i in range(8)}

# 读地址(effective _DISABLE 与 _DEFECTIVE)
DISABLE_ADDR = {
    "fbio_disable": 0x0082036C, "fbpa_disable": 0x00820368,
    "fbp_disable": 0x00820364, "gpc_disable": 0x00820350,
    "pes_disable": 0x00820690, "rop_l2_disable": 0x008202C4,
    "nvlink_disable": 0x00820684, "sys_pipe_disable": 0x008203A4,
}
DEFECTIVE_ADDR = {
    "fbio_defective": 0x008205D4, "fbpa_defective": 0x008205D0,
    "fbp_defective": 0x008205CC, "gpc_defective": 0x008205C4,
    "pes_defective": 0x00820698, "rop_l2_defective": 0x008205E8,
    "nvlink_defective": 0x0082068C, "sys_pipe_defective": 0x008203AC,
}
TPC_DEFECTIVE_ADDR = {"tpc_gpc%d_defective" % i: a for i, a in enumerate(
    [0x008205C8, 0x00820508, 0x0082050C, 0x00820514,
     0x00820564, 0x008205AC, 0x00820B84, 0x00820B80])}


class FfError(RuntimeError):
    pass


# --- 底层读卡 ---------------------------------------------------------------
def wait_idle(bar, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        c = bar.rd32(FUSECTRL)
        if ((c >> 16) & 31) == 4 and (c & 3) == 0:
            return c
        time.sleep(0.001)
    raise FfError(f"FUSECTRL 卡死: 0x{bar.rd32(FUSECTRL):08X}")


def read_row(bar, row):
    bar.wr32(FUSEADDR, row)
    bar.wr32(FUSECTRL, CMD_READ)
    wait_idle(bar)
    return bar.rd32(FUSERDATA)


def read_defective(bar):
    out = {k: bar.rd32(a) for k, a in DEFECTIVE_ADDR.items()}
    out.update({k: bar.rd32(a) for k, a in TPC_DEFECTIVE_ADDR.items()})
    return out


# --- FF 记录编码 -------------------------------------------------------------
def encode(chain, block, data):
    if not (0 <= chain < 32 and 0 <= block < 1024 and 0 <= data <= 0xFFFF):
        raise FfError(f"记录字段越界 chain={chain} block={block} data={data:#x}")
    return (data << 16) | (block << 6) | chain


def decode(word):
    return {"chain": word & 0x1F, "iff": (word >> 5) & 1,
            "block": (word >> 6) & 0x3FF, "data": (word >> 16) & 0xFFFF}


# --- 位读写(chain 内 bit 流)-------------------------------------------------
def get_bits(blocks, chain, bit, width):
    v = 0
    for i in range(width):
        b = bit + i
        blk = blocks.get(chain, {}).get(b // 16, 0)
        v |= ((blk >> (b % 16)) & 1) << i
    return v


def set_bits(blocks, chain, bit, width, value):
    blocks.setdefault(chain, {})
    for i in range(width):
        b = bit + i
        blk = blocks[chain].get(b // 16, 0)
        blk = (blk & ~(1 << (b % 16))) | (((value >> i) & 1) << (b % 16))
        blocks[chain][b // 16] = blk


def replay_blocks(ff_rows):
    """把已有 FF 行回放成 {chain: {block: data}}(后写赢)。"""
    blocks = {}
    for row in sorted(ff_rows):
        rec = ff_rows[row]
        if rec == 0:
            continue
        d = decode(rec)
        if d["iff"]:
            continue
        blocks.setdefault(d["chain"], {})[d["block"]] = d["data"]
    return blocks


# --- 采集 + 计划(只读)-------------------------------------------------------
def collect(bar):
    """读金窗状态 + 全部 FF 物理行 + 缺陷掩码。只读。"""
    dsov = bar.rd32(DSOV_STATUS)
    crc = bar.rd32(CRC_STATUS)
    ff_rows = {r: read_row(bar, r) for r in range(FF_FIRST, FF_LAST + 1)}
    defective = read_defective(bar)
    return {"dsov": dsov, "crc": crc, "ff_rows": ff_rows, "defective": defective}


def build_plan(col):
    """按本卡缺陷算目标值,生成 FF 记录清单。目标=DEFECTIVE(只禁真坏的)。"""
    if not col["ff_rows"]:
        raise FfError("未读到 FF 物理行(ff_rows 空)——采集不完整,拒绝出计划")
    used = sorted(r for r, v in col["ff_rows"].items() if v != 0)
    next_row = (max(used) + 1) if used else FF_FIRST
    if next_row < FF_FIRST:
        next_row = FF_FIRST

    # 基线 = 已有 FF 行回放的块值
    pending = {c: dict(b) for c, b in replay_blocks(col["ff_rows"]).items()}
    base = {c: dict(b) for c, b in pending.items()}

    defective = col["defective"]
    # 逐字段算目标值(默认 = 缺陷值;PCIe=0;缺陷缺失时按 0=全放行)
    targets = {}
    for key, (chain, bit, width, dkey) in {**FIELDS, **TPC_FIELDS}.items():
        if dkey and dkey in defective:
            targets[key] = defective[dkey]
        elif dkey is None:                      # PCIe 类
            targets[key] = 0
        else:                                   # 缺陷键缺失(如某些 TPC)→ 全放行
            targets[key] = 0
        # 应用目标到 pending 块
        set_bits(pending, chain, bit, width, targets[key])

    records = []
    for chain in sorted(pending):
        for blk in sorted(pending[chain]):
            old = base.get(chain, {}).get(blk, 0)
            new = pending[chain][blk]
            if old == new:
                continue
            if next_row > FF_LAST:
                raise FfError("FF 空行不足")
            records.append({"row": next_row, "chain": chain, "block": blk,
                            "old": "0x%04x" % old, "new": "0x%04x" % new,
                            "record": "0x%08x" % encode(chain, blk, new)})
            next_row += 1
    return {"used_rows": used, "ff_rows": {str(k): "0x%08x" % v
            for k, v in col["ff_rows"].items()},
            "defective": {k: "0x%08x" % v for k, v in defective.items()},
            "targets": {k: "0x%x" % v for k, v in targets.items()},
            "records": records}


# --- arm + burn(写,永久)-----------------------------------------------------
def arm(bar):
    if bar.rd32(DSOV_STATUS) != 0:
        raise FfError("DSOV != 0 — 不在金窗,先打毛刺(campaign)")
    if bar.rd32(CRC_STATUS) != 0x55:
        raise FfError(f"CRC != 0x55 (0x{bar.rd32(CRC_STATUS):08X}),状态可疑")
    # 1. SENSE_CTRL:控制行回熔丝值,DSOV 保持 0
    bar.wr32(FUSECTRL, CMD_SENSE)
    wait_idle(bar)
    if bar.rd32(EFP_STATUS) != 1:
        raise FfError("SENSE_CTRL 后 EFP != 1")
    if bar.rd32(DSOV_STATUS) != 0:
        raise FfError("SENSE_CTRL 把 DSOV 带回 1,金窗失效")
    # 2. 影子可写
    bar.wr32(EN_SW_OVERRIDE, 1)
    if bar.rd32(EN_SW_OVERRIDE) != 1:
        raise FfError("EN_SW_OVERRIDE 没锁住")
    # 3. 允许烧非 FPF
    bar.wr32(SBFE, 0)
    if bar.rd32(SBFE) != 0:
        raise FfError("SBFE 没清掉")
    # 4. 开 FUSEWDATA 写通路
    bar.wr32(WDATA_WR_SECURE, 0)
    if bar.rd32(WDATA_WR_SECURE) != 0:
        raise FfError("WDATA_WR_SECURE 没清掉")
    # 注意:不碰 PLM,不用 RWL
    return {"FUSECTRL": bar.rd32(FUSECTRL), "CRC": bar.rd32(CRC_STATUS),
            "EFP": bar.rd32(EFP_STATUS), "DSOV": bar.rd32(DSOV_STATUS),
            "SBFE": bar.rd32(SBFE), "WDATA_SEC": bar.rd32(WDATA_WR_SECURE),
            "EN_SW": bar.rd32(EN_SW_OVERRIDE)}


def burn_row(bar, row, word):
    if not (FF_FIRST <= row <= FF_LAST):
        raise FfError(f"行 {row} 超出 FF 区 {FF_FIRST}-{FF_LAST}")
    if row <= CONTROL_ROWS_MAX:
        raise FfError(f"行 {row} 是控制行,永远禁写")
    cur = read_row(bar, row)
    if cur != 0:
        raise FfError(f"行 {row} 非空(0x{cur:08X}),拒绝覆盖")
    bar.wr32(FUSEADDR, row)
    bar.wr32(FUSEWDATA, word)
    bar.wr32(FUSECTRL, CMD_WRITE)
    wait_idle(bar)
    back = read_row(bar, row)
    if back != word:
        raise FfError(f"行 {row} 回读 0x{back:08X} != 0x{word:08X}")
    # 确认金窗没被带坏
    if bar.rd32(CRC_STATUS) != 0x55 or bar.rd32(DSOV_STATUS) != 0 \
            or bar.rd32(EFP_STATUS) != 1:
        raise FfError(f"烧行 {row} 后金窗状态被带坏")
    print(f"  row {row} <- 0x{word:08X}  回读一致 OK")


# --- 命令 ---------------------------------------------------------------------
def cmd_plan(args, Bar0, restore):
    if not restore(args.bdf, wait_s=0.5):
        raise FfError(f"{args.bdf} 在 PCIe 配置空间无响应")
    with Bar0(args.bdf) as bar:
        col = collect(bar)
    if col["dsov"] != 0:
        print(f"⚠️ 当前 DSOV={col['dsov']:#x} 不在金窗;采集可读,但烧写需先打毛刺",
              file=sys.stderr)
    plan = build_plan(col)
    plan["bdf"] = args.bdf
    plan["expect"] = {"crc": "0x55", "dsov": "0x0"}
    with open(args.out, "w") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    used = plan["used_rows"]
    print(f"采集完成:FF 已用行 {len(used)} 条"
          f"({('末行 %d' % max(used)) if used else '无'}),"
          f"计划 {len(plan['records'])} 条新记录 → {args.out}")
    for r in plan["records"]:
        print(f"  row {r['row']}  {r['record']}  chain{r['chain']} blk{r['block']}"
              f"  {r['old']} -> {r['new']}")
    print("\n核对无误后再 burn。烧前确认每条 row 当前读回 == 0。")


def cmd_burn(args, Bar0, restore):
    with open(args.plan) as f:
        plan = json.load(f)
    records = plan.get("records") or []
    if not records:
        raise FfError("计划里没有 records")
    bdf = args.bdf or plan.get("bdf")
    if not restore(bdf, wait_s=0.5):
        raise FfError(f"{bdf} 在 PCIe 配置空间无响应")
    with Bar0(bdf) as bar:
        if not args.commit:
            # dry-run:查金窗 + 逐行判空
            print("--- DRY RUN ---")
            print(f"  DSOV=0x{bar.rd32(DSOV_STATUS):08X}  CRC=0x{bar.rd32(CRC_STATUS):08X}")
            for r in records:
                cur = read_row(bar, int(r["row"]))
                flag = "" if cur == 0 else "  ★非空!"
                print(f"  row {r['row']} <- {r['record']}  当前 0x{cur:08X}{flag}")
            print("\n什么都没写。确认后加 --commit 真烧。")
            return
        st = arm(bar)
        print("--- armed ---")
        for k, v in st.items():
            print(f"  {k:<10} 0x{v:08X}")
        for r in records:
            burn_row(bar, int(r["row"]), int(str(r["record"]), 0))
    print("\n烧完。物理断电冷启让 FF 链重放,再用 verify 验证。")


def cmd_verify(args, Bar0, restore):
    if not restore(args.bdf, wait_s=0.5):
        raise FfError(f"{args.bdf} 在 PCIe 配置空间无响应")
    with Bar0(args.bdf) as bar:
        print("--- 烧后派生状态 ---")
        for k, a in DISABLE_ADDR.items():
            print(f"  {k:<18} 0x{bar.rd32(a):08X}")
        print(f"  {'DSOV_STATUS':<18} 0x{bar.rd32(DSOV_STATUS):08X}  (普通启动=1 正常)")
        print(f"  {'CRC_STATUS':<18} 0x{bar.rd32(CRC_STATUS):08X}")
        if args.rows:
            print("--- 指定行 ---")
            for r in args.rows:
                print(f"  row {r}: 0x{read_row(bar, r):08X}")


def main():
    Bar0, restore = _import_bar0()
    ap = argparse.ArgumentParser(prog="ga100_ff",
                                 description="GA100 FF 解锁一体工具(只读 plan / 烧写 burn / 验证 verify)")
    ap.add_argument("--bdf", default=None, help="卡的 PCI 地址,如 0000:2f:00.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="只读采集 + 按本卡缺陷生成烧写计划")
    p.add_argument("--bdf", default="0000:2f:00.0")
    p.add_argument("-o", "--out", required=True)

    b = sub.add_parser("burn", help="烧写(默认 dry-run;--commit 真烧)")
    b.add_argument("--bdf", default=None)
    b.add_argument("--plan", required=True)
    b.add_argument("--commit", action="store_true")

    v = sub.add_parser("verify", help="冷启后读派生寄存器/指定行")
    v.add_argument("--bdf", default="0000:2f:00.0")
    v.add_argument("--rows", nargs="*", type=int)

    args = ap.parse_args()
    try:
        if args.cmd == "plan":
            cmd_plan(args, Bar0, restore)
        elif args.cmd == "burn":
            cmd_burn(args, Bar0, restore)
        elif args.cmd == "verify":
            cmd_verify(args, Bar0, restore)
    except FfError as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
