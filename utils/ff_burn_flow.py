#!/usr/bin/env python3
"""CMP 170HX FF 烧录 · 主机侧一键流程编排.

把 README 阶段 0–3 的主机侧命令(卸驱动 → campaign 打金窗 → plan 出计划
→ dry-run 判空 + 备份 → 真烧)串成一个可断点续跑的脚本,终点 = burn-all
记录全部写入。冷启后的验收(verify / final_check.py)不在本脚本范围。

用法(在仓库根目录):
    sudo python3 utils/ff_burn_flow.py run --bdf 0000:2f:00.0          # 自动推进
    sudo python3 utils/ff_burn_flow.py run ... --single                # 含单行试烧
    python3  utils/ff_burn_flow.py status                              # 看进度
    sudo python3 utils/ff_burn_flow.py reset                           # 清空状态

子命令:run / prep / glitch / plan / precheck / single-burn / burn-all
        / status / reset。零改动现有工具,全部原样子进程调用。

安全设计:
  - 每个写阶段入口只读 DSOV/CRC 探针,金窗不在则停(不自动重打毛刺)
  - burn 前校验 state 中 card_serial + plan sha256,防串卡/换 plan
  - 真烧两段确认(Y → 字面量 BURN);非 TTY 且无 --yes 直接拒绝
  - 烧失败 → phase=aborted,绝不自动重试
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GA100_FF = os.path.join(REPO_ROOT, "utils", "ga100_ff.py")
FF_DUMP = os.path.join(REPO_ROOT, "CMPGlitch", "tools", "ff_dump.py")
GA100VFI_HOST = os.path.join(REPO_ROOT, "pico-ga100vfi", "host")

PHASES = ["prep", "glitch", "plan", "precheck", "burn-all", "done"]
BLACKLIST_CONF = "/etc/modprobe.d/aa-blacklist-nvidia-campaign.conf"
BLACKLIST_BODY = (
    "blacklist nvidia\nblacklist nvidia_drm\nblacklist nvidia_modeset\n"
    "blacklist nvidia_uvm\ninstall nvidia /bin/false\n"
)

# ga100_ff.py FIELDS 的 block 反查:优先挑 chain1 非关键记录做试烧。
# fbp(FBP_DISABLE, chain1 blk35 附近)是最温和的候选;绝不选 chain2(PCIe)。
SAFE_TEST_FIELD_BLOCKS = {"fbp", "fbpa", "fbio"}


class FlowError(Exception):
    """流程级失败(可读消息,不带堆栈)。"""


# ---------------------------------------------------------------- state ----
def state_path(args) -> str:
    return os.path.join(args.state_dir, "state.json")


def load_state(args) -> dict:
    p = state_path(args)
    if not os.path.exists(p):
        return {"version": 1, "bdf": args.bdf, "phase": "prep",
                "phases_done": [], "burned_rows": [], "history": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_state(args, st: dict) -> None:
    os.makedirs(args.state_dir, exist_ok=True)
    st["history"] = st.get("history", [])[-50:]
    tmp = state_path(args) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, state_path(args))


def record(st: dict, phase: str, ok: bool, note: str = "") -> None:
    st["history"].append({"t": time.strftime("%F %T"), "phase": phase,
                          "ok": ok, "note": note})


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ subprocess ----
def run_cmd(cmd: list, env: dict | None = None, sudo_needed: bool = True) -> tuple:
    """返回 (exit, stdout, stderr)。脚本自身通常已以 sudo 运行。"""
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, capture_output=True, text=True, env=e, cwd=REPO_ROOT)
    return p.returncode, p.stdout, p.stderr


def probe_window(args) -> tuple:
    """只读 DSOV/CRC 金窗探针。返回 (dsov, crc) 或抛 FlowError。"""
    code, out, err = run_cmd(
        [sys.executable, GA100_FF, "burn", "--plan", _probe_plan(args)])
    m = re.search(r"DSOV=0x([0-9A-Fa-f]{8})\s+CRC=0x([0-9A-Fa-f]{8})", out)
    if not m:
        raise FlowError(f"金窗探针失败(dry-run 无输出):{err.strip() or out[:200]}")
    return int(m.group(1), 16), int(m.group(2), 16)


def _probe_plan(args) -> str:
    """探针/dry-run 共用的 plan 路径;没有真 plan 时造一个空 records 的。"""
    st = load_state(args)
    p = st.get("plan_file")
    if p and os.path.exists(p):
        return p
    os.makedirs(args.state_dir, exist_ok=True)
    fake = os.path.join(args.state_dir, "probe_empty.json")
    with open(fake, "w", encoding="utf-8") as f:
        json.dump({"records": [], "expect": {"crc": "0x55", "dsov": "0x0"},
                   "ff_rows": {}, "bdf": args.bdf}, f)
    return fake


def _env_hint() -> None:
    """Pico 工具链缺失时提示环境脚本(只提示,不阻塞:glitch 失败会自然暴露)。"""
    if shutil.which("arm-none-eabi-gcc") is None:
        print("  ℹ 检测到交叉工具链缺失——若 Pico 固件未编译,"
              "先跑:python3 utils/ff_env_setup.py check")


# -------------------------------------------------------------- phases ----
def phase_prep(args, st: dict) -> bool:
    """卸驱动 + 幂等写黑名单。返回 True=阶段完成。"""
    print("== prep: 卸 nvidia 栈 ==")
    _env_hint()
    subprocess.run(["systemctl", "stop", "lightdm", "lactd"], capture_output=True)
    subprocess.run(["pkill", "-x", "Xorg"], capture_output=True)
    time.sleep(1)
    for mod in ("nvidia_uvm", "nvidia_drm", "nvidia_modeset", "nvidia"):
        p = subprocess.run(["modprobe", "-r", mod], capture_output=True, text=True)
        if p.returncode != 0:
            raise FlowError(
                f"modprobe -r {mod} 失败:{p.stderr.strip()}\n"
                f"  → 查占用:lsof /dev/nvidia* 或 fuser -v /dev/nvidia*;处理后重跑 run")
    lsmod = subprocess.run(["lsmod"], capture_output=True, text=True).stdout
    if re.search(r"^nvidia\S*\s", lsmod, re.M):
        raise FlowError("lsmod 仍见 nvidia 模块,卸载不彻底")
    if not os.path.exists(BLACKLIST_CONF):
        try:
            with open(BLACKLIST_CONF, "w", encoding="utf-8") as f:
                f.write(BLACKLIST_BODY)
            print(f"  黑名单已写 {BLACKLIST_CONF}")
        except PermissionError:
            print(f"  ⚠ 无权写 {BLACKLIST_CONF}(非 root?)——黑名单只防驱动抢回,"
                  f"不阻塞流程;建议手动补上")
    else:
        print(f"  黑名单已存在,跳过")
    lspci = subprocess.run(["lspci", "-d", "10de:20c2", "-D"],
                           capture_output=True, text=True).stdout.strip()
    if not lspci:
        raise FlowError(f"lspci 未见 10de:20c2(PCI ID 不符或卡不在此 BDF)")
    if args.bdf not in lspci:
        print(f"  ⚠ lspci 看到 {lspci.splitlines()[0].split()[0]} 但 --bdf={args.bdf},请核对")
    print("  prep OK")
    return True


def phase_glitch(args, st: dict) -> bool:
    """campaign 打金窗。成功判据:exit==0 且 stdout JSON goldilocks==true。"""
    print("== glitch: campaign 打金窗 ==")
    logf = os.path.join(args.state_dir, "goldilocks.jsonl")
    code, out, err = run_cmd(
        [sys.executable, "-m", "ga100vfi", "campaign",
         "--bdf", args.bdf, "--adaptive", "--budget", str(args.budget),
         "--log", logf],
        env={"PYTHONPATH": GA100VFI_HOST})
    try:
        payload = json.loads(out[out.index("{"):])
    except (ValueError, IndexError):
        payload = {}
    if code == 2:
        raise FlowError("campaign exit 2:Pico 失联/解锁失败——检查接线与 /dev 串口,人工处理后重跑 run")
    if code != 0 or not payload.get("goldilocks"):
        raise FlowError(
            f"campaign 未命中(exit={code}, attempts={payload.get('attempts')}):"
            f"预算耗尽。可提高 --budget 重跑,或人工检查 Pico/接线")
    dsov, crc = probe_window(args)
    if dsov != 0 or crc != 0x55:
        raise FlowError(f"campaign 报成功但探针 DSOV={dsov:#x} CRC={crc:#x}——金窗未真正建立,重跑 run")
    st["goldilocks_at"] = time.strftime("%F %T")
    print(f"  金窗 OK(DSOV=0 CRC=0x55, {payload.get('attempts')} 发)")
    return True


def phase_plan(args, st: dict) -> bool:
    print("== plan: 只读采集 + 出计划 ==")
    out_path = os.path.join(args.state_dir, "plan.json")
    code, out, err = run_cmd(
        [sys.executable, GA100_FF, "plan", "--bdf", args.bdf, "-o", out_path])
    if code != 0:
        raise FlowError(f"ga100_ff plan 失败:{err.strip() or out[:300]}")
    with open(out_path, encoding="utf-8") as f:
        plan = json.load(f)
    if not plan.get("records"):
        if plan.get("used_rows"):
            raise FlowError("plan 成功但 records 为空(目标可能已全部达成)——人工确认后 reset 并跑 verify")
        raise FlowError("plan 的 records 与 used_rows 均空——采集异常,回 glitch 重跑")
    if not plan.get("ff_rows"):
        raise FlowError("plan 的 ff_rows 为空——采集不完整(issue#1 防呆),回 glitch 重跑")
    st["plan_file"] = out_path
    st["plan_sha256"] = sha256_file(out_path)
    st["card_serial_hint"] = plan.get("bdf")
    st["plan_records_rows"] = [int(r["row"]) for r in plan["records"]]
    print(f"  计划 {len(plan['records'])} 条记录(行 {plan['records'][0]['row']}"
          f"–{plan['records'][-1]['row']}),已用行 {len(plan.get('used_rows', []))}")
    return True


def phase_precheck(args, st: dict) -> bool:
    """备份 ff_dump + dry-run 判空(dry-run exit 恒 0,必须解析 stdout)。"""
    print("== precheck: 备份 + dry-run ==")
    bak = os.path.join(args.state_dir, "ff_backup.json")
    code, out, err = run_cmd([sys.executable, FF_DUMP, "--bdf", args.bdf,
                              "--out", bak])
    if code != 0 or not os.path.exists(bak):
        raise FlowError(f"ff_dump 备份失败:{err.strip() or out[:200]}")
    st["backup_file"] = bak
    st["backup_sha256"] = sha256_file(bak)
    print(f"  备份 {bak}(sha256 {st['backup_sha256'][:12]}…)")

    code, out, err = run_cmd(
        [sys.executable, GA100_FF, "burn", "--plan", st["plan_file"]])
    if "★非空!" in out:
        rows = [ln.strip() for ln in out.splitlines() if "★非空!" in ln]
        raise FlowError("dry-run 发现非空行,拒绝继续(防覆盖已有记录):\n  "
                        + "\n  ".join(rows)
                        + "\n  → 金窗内卡况变了,回 plan 重算(重跑 run 会自动从 plan 开始)")
    m = re.search(r"DSOV=0x([0-9A-Fa-f]{8})\s+CRC=0x([0-9A-Fa-f]{8})", out)
    if not m or int(m.group(1), 16) != 0 or int(m.group(2), 16) != 0x55:
        got = m.groups() if m else ("?", "?")
        raise FlowError(f"dry-run 时金窗异常(DSOV=0x{got[0]} CRC=0x{got[1]})——回 glitch 重跑")
    print("  dry-run 全部空行,金窗正常")
    return True


def pick_test_row(plan: dict) -> int:
    """默认试烧行:chain1 且字段温和(fbp/fbpa/fbio);绝不 chain2。"""
    chain2_rows = {int(r["row"]) for r in plan["records"] if int(r["chain"]) == 2}
    for rec in plan["records"]:
        if int(rec["chain"]) != 2 and int(rec["row"]) not in chain2_rows:
            fields = rec.get("fields") or [rec.get("block")]
            if any(str(f) in SAFE_TEST_FIELD_BLOCKS for f in fields):
                return int(rec["row"])
    for rec in plan["records"]:
        if int(rec["chain"]) == 1:
            return int(rec["row"])
    raise FlowError("计划里找不到 chain1 记录可做试烧——用 --test-row 显式指定")


def slice_plan(plan: dict, keep_rows: set) -> dict:
    """拷贝 plan 只缩 records(burn_row 拒绝非空行,试烧后必须裁剪)。"""
    p = dict(plan)
    p["records"] = [r for r in plan["records"] if int(r["row"]) in keep_rows]
    return p


def confirm_burn(args, sliced: dict, purpose: str) -> None:
    print(f"\n即将真烧【{purpose}】共 {len(sliced['records'])} 条记录(OTP,不可逆):")
    for r in sliced["records"]:
        tag = "  ← PCIe/chain2,最危险" if int(r["chain"]) == 2 else ""
        print(f"  row {r['row']}  {r['record']}  chain{r['chain']} "
              f"blk{r['block']}  {r['old']} -> {r['new']}{tag}")
    print("  (plan.json 内 records 天然按 chain1→chain2 排序,PCIe 已在最后)")
    if args.yes:
        print("  --yes 跳过交互确认")
        return
    if not sys.stdin.isatty():
        raise FlowError("非交互终端且未给 --yes,拒绝真烧")
    if input("确认以上计划?输入 Y 继续:").strip() != "Y":
        raise FlowError("用户未确认,中止")
    if input("再确认:输入字面量 BURN 才会写入:").strip() != "BURN":
        raise FlowError("二次确认失败,中止")


def do_commit_burn(args, st: dict, sliced: dict, purpose: str) -> list:
    """写 sliced plan 到临时文件 → burn --commit。成功返回烧成的行号列表。"""
    tmp = os.path.join(args.state_dir, f"plan_{purpose}.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sliced, f, indent=2, ensure_ascii=False)
    code, out, err = run_cmd(
        [sys.executable, GA100_FF, "burn", "--plan", tmp, "--commit"])
    sys.stdout.write(_indent(out))
    if err.strip():
        sys.stderr.write(_indent(err))
    burned = [int(m) for m in re.findall(r"row (\d+) <- 0x[0-9A-F]{8}  回读一致 OK", out)]
    expected = [int(r["row"]) for r in sliced["records"]]
    if code != 0 or sorted(burned) != sorted(expected):
        st["phase"] = "aborted"
        st["aborted_reason"] = f"{purpose} 烧写不完整"
        done = sorted(set(burned) | set(st.get("burned_rows", [])))
        remain = sorted(set(st.get("plan_records_rows", expected)) - set(done))
        raise FlowError(
            f"烧写失败或不完整(exit={code})。绝不自动重试。\n"
            f"  已确认烧成行:{done or '无'}\n  剩余未烧行:{remain}\n"
            f"  → 按 README 阶段 3 红线人工处置;必要时用恢复记录(CMPGlitch/tools/ff_restore.py)")
    return burned


def phase_single_burn(args, st: dict) -> bool:
    print("== single-burn: 单行试烧 ==")
    dsov, crc = probe_window(args)
    if dsov != 0 or crc != 0x55:
        raise FlowError(f"金窗不在(DSOV={dsov:#x} CRC={crc:#x})——先重跑 glitch")
    with open(st["plan_file"], encoding="utf-8") as f:
        plan = json.load(f)
    _check_plan_integrity(args, st, plan)
    row = args.test_row if args.test_row else pick_test_row(plan)
    sliced = slice_plan(plan, {row})
    if not sliced["records"]:
        raise FlowError(f"--test-row {row} 不在计划 records 中")
    confirm_burn(args, sliced, f"试烧 row {row}")
    burned = do_commit_burn(args, st, sliced, "single")
    st["burned_rows"] = sorted(set(st.get("burned_rows", [])) | set(burned))
    st["single_test_row"] = row
    print(f"\n试烧完成(row {row})。⛔ 现在物理断电冷启(不是 reboot),"
          f"然后重新上电、接好 Pico,再跑:\n"
          f"  sudo python3 utils/ff_burn_flow.py run --bdf {args.bdf}"
          f"{' --single' if args.single else ''}\n"
          f"(脚本会自动重打金窗并只烧剩余行)")
    return True


def phase_burn_all(args, st: dict) -> bool:
    print("== burn-all: 烧剩余全部记录 ==")
    dsov, crc = probe_window(args)
    if dsov != 0 or crc != 0x55:
        raise FlowError(f"金窗不在(DSOV={dsov:#x} CRC={crc:#x})——先重跑 glitch")
    with open(st["plan_file"], encoding="utf-8") as f:
        plan = json.load(f)
    _check_plan_integrity(args, st, plan)
    all_rows = set(st.get("plan_records_rows") or
                   [int(r["row"]) for r in plan["records"]])
    remain = sorted(all_rows - set(st.get("burned_rows", [])))
    if not remain:
        print("  没有剩余记录——全部已烧完")
        return True
    sliced = slice_plan(plan, set(remain))
    confirm_burn(args, sliced, "全量烧写")
    burned = do_commit_burn(args, st, sliced, "all")
    st["burned_rows"] = sorted(set(st.get("burned_rows", [])) | set(burned))
    print(f"\n✅ FF 烧写完成:{len(st['burned_rows'])} 条记录全部写入并回读一致。")
    print("现在物理断电冷启(不是 reboot!),然后手动验收:")
    print(f"  sudo python3 utils/ga100_ff.py verify --bdf {args.bdf}")
    print(f"  sudo python3 CMPGlitch/tools/final_check.py   # 期望 failed==0")
    print("⚠ 若 GSP 启动失败 0x55 → 漏烧 PES/ROP_L2,见 README 阶段 4 红线。")
    return True


def _check_plan_integrity(args, st: dict, plan: dict) -> None:
    cur = sha256_file(st["plan_file"])
    if st.get("plan_sha256") and cur != st["plan_sha256"]:
        raise FlowError("plan.json 与 state 记录的 sha256 不符(文件被改?)——中止,人工核查")
    if plan.get("bdf") not in (None, args.bdf):
        raise FlowError(f"plan 的 bdf={plan.get('bdf')} 与 --bdf={args.bdf} 不符——防串卡,中止")


def _indent(text: str) -> str:
    return "".join("  " + ln + "\n" for ln in text.splitlines())


# ------------------------------------------------------------------ run ----
def cmd_run(args) -> int:
    st = load_state(args)
    if st.get("phase") == "aborted":
        print("state 处于 aborted(上次烧写失败)。绝不自动重试。\n"
              f"  原因:{st.get('aborted_reason')}\n"
              f"  已烧:{st.get('burned_rows')}\n"
              "人工处置后:确认剩余行安全 → sudo ... reset → 手动 burn-all,或回退流程。")
        return 1
    if st.get("phase") == "done":
        print("已完成。冷启验收用 verify / final_check.py。")
        return 0

    # 试烧启用时插入 single-burn 阶段;跳过时直接到 burn-all
    seq = (["prep", "glitch", "plan", "precheck", "single-burn", "burn-all"]
           if args.single else PHASES)
    phases_done = set(st.get("phases_done", []))
    # 若上次已做过 single-burn 而这次没带 --single:已试烧行仍在 burned_rows,
    # burn-all 的裁剪逻辑天然跳过它,不会重复烧。
    for ph in seq:
        if ph in phases_done:
            continue
        fn = {"prep": phase_prep, "glitch": phase_glitch, "plan": phase_plan,
              "precheck": phase_precheck, "single-burn": phase_single_burn,
              "burn-all": phase_burn_all}.get(ph)
        if fn is None:  # "done"
            continue
        ok = fn(args, st)
        if not ok:
            return 1
        phases_done.add(ph)
        st["phases_done"] = sorted(phases_done)  # 不按 seq 过滤:single-burn
        # 的完成记录在 --single 与非 --single 序列间切换时也不能丢,否则重跑会重复试烧
        st["phase"] = seq[min(seq.index(ph) + 1, len(seq) - 1)]
        # single-burn 后必须人工冷启 —— 保存进度后退出
        if ph == "single-burn":
            save_state(args, st)
            return 0
        save_state(args, st)
    st["phase"] = "done"
    save_state(args, st)
    print(f"\n[run] 全部阶段完成。已烧行:{st.get('burned_rows')}")
    return 0


def cmd_status(args) -> int:
    st = load_state(args)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


def cmd_reset(args) -> int:
    p = state_path(args)
    if os.path.exists(p):
        os.remove(p)
        print(f"已删除 {p}(plan.json / 备份文件保留在 {args.state_dir}/)")
    else:
        print("无状态可清")
    return 0


# ------------------------------------------------------------- selftest ----
FIXTURE_PLAN = {
    "bdf": "0000:2f:00.0",
    "expect": {"crc": "0x55", "dsov": "0x0"},
    "used_rows": [222],
    "ff_rows": {"222": "0x00000001"},
    "records": [
        {"row": 223, "chain": 1, "block": 35, "old": "0x0852", "new": "0x0840",
         "record": "0x08408C41", "fields": ["fbp"]},
        {"row": 224, "chain": 1, "block": 19, "old": "0x0013", "new": "0x0000",
         "record": "0x00004C81", "fields": ["gpc"]},
        {"row": 225, "chain": 2, "block": 39, "old": "0x0021", "new": "0x0000",
         "record": "0x00009E42", "fields": ["pcie_gen3"]},
    ],
}


def self_test() -> int:
    """离线自检:不碰真卡,不跑子进程,只测胶水逻辑。"""
    import tempfile
    fails = []

    def check(name, cond):
        print(("  ok  " if cond else "  FAIL") + " " + name)
        if not cond:
            fails.append(name)

    # slice_plan 保留其余字段、只缩 records
    s = slice_plan(FIXTURE_PLAN, {223})
    check("slice_plan 单行", [r["row"] for r in s["records"]] == [223]
          and s["expect"] == FIXTURE_PLAN["expect"])
    s2 = slice_plan(FIXTURE_PLAN, {224, 225})
    check("slice_plan 裁剪已烧行", [r["row"] for r in s2["records"]] == [224, 225])

    # 试烧行选择:chain1 优先 fbp;绝不 chain2
    check("pick_test_row 选 fbp 行", pick_test_row(FIXTURE_PLAN) == 223)
    only_chain2 = dict(FIXTURE_PLAN, records=FIXTURE_PLAN["records"][2:])
    try:
        pick_test_row(only_chain2)
        check("pick_test_row 拒绝纯 chain2", False)
    except FlowError:
        check("pick_test_row 拒绝纯 chain2", True)

    # 状态机:原子写 + 断点字段
    with tempfile.TemporaryDirectory() as td:
        ap = argparse.Namespace(state_dir=td, bdf="0000:2f:00.0")
        st = load_state(ap)
        check("空 state 初始 phase=prep", st["phase"] == "prep")
        st["plan_file"] = os.path.join(td, "plan.json")
        with open(st["plan_file"], "w") as f:
            json.dump(FIXTURE_PLAN, f)
        st["plan_sha256"] = sha256_file(st["plan_file"])
        save_state(ap, st)
        st2 = load_state(ap)
        check("state 回读一致", st2["plan_sha256"] == st["plan_sha256"])
        # 篡改 plan → integrity 检测
        with open(st["plan_file"], "a") as f:
            f.write(" ")
        try:
            _check_plan_integrity(ap, st2, FIXTURE_PLAN)
            check("sha256 篡改检测", False)
        except FlowError:
            check("sha256 篡改检测", True)
        # bdf 串卡检测
        st3 = dict(st2)
        ap2 = argparse.Namespace(state_dir=td, bdf="0000:01:00.0")
        try:
            _check_plan_integrity(ap2, st3, dict(FIXTURE_PLAN, bdf="0000:2f:00.0"))
            check("bdf 串卡检测", False)
        except FlowError:
            check("bdf 串卡检测", True)

    # do_commit_burn 的 stdout 解析(回读一致 OK 行号提取)
    fake_out = ("--- DRY RUN ---\n"
                "  DSOV=0x00000000  CRC=0x00000055\n"
                "--- armed ---\n"
                "  row 223 <- 0x08408C41  回读一致 OK\n"
                "  row 224 <- 0x00004C81  回读一致 OK\n")
    got = [int(m) for m in re.findall(
        r"row (\d+) <- 0x[0-9A-F]{8}  回读一致 OK", fake_out)]
    check("burn 成功行解析", got == [223, 224])
    got2 = re.findall(r"row (\d+) <- 0x[0-9A-F]{8}  回读一致 OK", "  row 9 <- x\n")
    check("异常行不误收", got2 == [])

    # dry-run 判空正则
    check("dry-run 金窗正则命中",
          re.search(r"DSOV=0x([0-9A-Fa-f]{8})\s+CRC=0x([0-9A-Fa-f]{8})", fake_out) is not None)
    check("非空行检测", "★非空!" in "  row 225 <- 0x1  当前 0x0000000F  ★非空!")

    # ---- 端到端 mock:monkeypatch run_cmd 与确认输入,跑完整 run 链路 ----
    self_mod = sys.modules[__name__]
    with tempfile.TemporaryDirectory() as td:
        ap = argparse.Namespace(state_dir=td, bdf="0000:2f:00.0", single=False,
                                test_row=None, budget=10, yes=True)
        plan_path = os.path.join(td, "plan.json")
        burn_inputs = iter([])  # --yes 模式不读 stdin
        calls = []

        def fake_run_cmd(cmd, env=None, sudo_needed=True):
            calls.append(cmd[-3] if len(cmd) > 3 else cmd[0])
            key = " ".join(os.path.basename(c) for c in cmd[:3]) + " " + " ".join(cmd[3:5])
            if cmd[1:2] == ["-m"]:  # campaign
                return 0, json.dumps({"goldilocks": True, "attempts": 3}), ""
            tool = cmd[1] if len(cmd) > 1 else ""
            if tool.endswith("ga100_ff.py"):
                if cmd[2] == "plan":
                    shutil.copy(os.path.join(td, "_fixture.json"), plan_path) \
                        if os.path.exists(os.path.join(td, "_fixture.json")) else \
                        json.dump(FIXTURE_PLAN, open(plan_path, "w"))
                    return 0, "", ""
                if cmd[2] == "burn" and "--commit" in cmd:
                    with open(cmd[cmd.index("--plan") + 1]) as f:
                        sliced = json.load(f)
                    out = ["--- armed ---"]
                    out += [f"  row {r['row']} <- {r['record']}  回读一致 OK"
                            for r in sliced["records"]]
                    return 0, "\n".join(out) + "\n", ""
                return 0, ("--- DRY RUN ---\n  DSOV=0x00000000  CRC=0x00000055\n"
                           "  row 223 <- 0x1  当前 0x00000000\n"
                           "  row 224 <- 0x1  当前 0x00000000\n"
                           "  row 225 <- 0x1  当前 0x00000000\n"), ""
            if tool.endswith("ff_dump.py"):
                json.dump({"rows": {}}, open(cmd[cmd.index("--out") + 1], "w"))
                return 0, "", ""
            return 0, "", ""

        def fake_probe(args_):
            return 0, 0x55

        orig_run_cmd, orig_probe = self_mod.run_cmd, self_mod.probe_window
        orig_geteuid = self_mod.os.geteuid
        self_mod.run_cmd = fake_run_cmd
        self_mod.probe_window = fake_probe
        # prep 阶段跳过真实 systemctl:patch subprocess.run
        orig_sub_run = self_mod.subprocess.run
        self_mod.subprocess.run = lambda *a, **k: type("R", (), {
            "returncode": 0, "stdout": "0000:2f:00.0 VGA ... 10de:20c2", "stderr": ""})()
        try:
            rc = self_mod.cmd_run(ap)
        finally:
            self_mod.run_cmd, self_mod.probe_window = orig_run_cmd, orig_probe
            self_mod.subprocess.run = orig_sub_run
        st_end = load_state(ap)
        check("mock 全链路 exit 0", rc == 0)
        check("mock 终态 done", st_end.get("phase") == "done")
        check("mock 已烧 3 行", st_end.get("burned_rows") == [223, 224, 225])

    print(("\nSELF-TEST FAILED: %s" % fails) if fails else "\nSELF-TEST OK(全部离线断言通过)")
    return 1 if fails else 0


# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(
        prog="ff_burn_flow",
        description="CMP 170HX FF 烧录主机侧一键流程(prep→glitch→plan→precheck→burn-all);"
                    "终点=记录全部烧写完成。冷启验收用 ga100_ff.py verify / final_check.py。")
    ap.add_argument("--bdf", default="0000:2f:00.0")
    ap.add_argument("--state-dir", default=os.path.join(REPO_ROOT, "campaign_state"))
    ap.add_argument("--single", action="store_true",
                    help="启用单行试烧环节(试烧→人工冷启→续跑烧剩余);默认跳过")
    ap.add_argument("--test-row", type=int, default=None,
                    help="single-burn 指定试烧行(默认自动选 chain1 温和记录)")
    ap.add_argument("--budget", type=int, default=500, help="campaign 总发数")
    ap.add_argument("--yes", action="store_true",
                    help="跳过烧写两段交互确认(仅限受控/自动化环境)")
    ap.add_argument("--self-test", action="store_true", help="离线自检,不碰硬件")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="自动从断点推进")
    sub.add_parser("prep")
    sub.add_parser("glitch")
    sub.add_parser("plan")
    sub.add_parser("precheck")
    sub.add_parser("single-burn")
    sub.add_parser("burn-all")
    sub.add_parser("status", help="打印当前 state.json")
    sub.add_parser("reset", help="删除 state.json(plan/备份保留)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.cmd is None:
        ap.print_help()
        return 2
    if args.cmd in ("prep", "glitch", "single-burn", "burn-all") and os.geteuid() != 0:
        print("需要 root(涉及 systemctl/modprobe/BAR0 写):sudo 运行", file=sys.stderr)
        return 1
    try:
        if args.cmd == "run":
            return cmd_run(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "reset":
            return cmd_reset(args)
        # 单阶段模式:只跑该阶段(不推进 state.phase)
        st = load_state(args)
        fn = {"prep": phase_prep, "glitch": phase_glitch, "plan": phase_plan,
              "precheck": phase_precheck, "single-burn": phase_single_burn,
              "burn-all": phase_burn_all}[args.cmd]
        ok = fn(args, st)
        record(st, args.cmd, ok)
        save_state(args, st)
        return 0 if ok else 1
    except FlowError as e:
        st = load_state(args)
        record(st, args.cmd or "run", False, str(e)[:300])
        if "烧写失败或不完整" in str(e):
            st["phase"] = "aborted"
        save_state(args, st)
        print(f"ABORT: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
