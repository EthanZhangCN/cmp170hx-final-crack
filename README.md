# CMP 170HX (GA100) 从 0 到全解锁 · 完整实操手册

本项目综合了社区和QQ群各个大佬已经实操破解的资料进行了汇总，尽可能梳理出了一个图文教学的教程，由于本人还未开始实操，仅供参考。  
欢迎各位实操过的大佬查漏补缺。


> 打包/部署见 [`部署清单.md`](部署清单.md)。

## 目录结构

| 路径 | 作用 |
|---|---|
| `README.md`(本文件) / `部署清单.md` | 实操手册 + 部署清单 |
| `utils/ga100_ff.py` | FF 解锁一体工具(plan/burn/verify,内置安全护栏) |
| `utils/ffcalc-fix-issue1.patch` | ffcalc issue#1 修复补丁(cmp170hx-ffcalc 目录内副本已应用) |
| `images/` | 手册配图 + spec 图源 + `render_spec.py` 渲染脚本 |
| `pico-ga100vfi/` | RP2350 打毛刺固件 + host CLI |
| `ga100vfi/` | 寄存器地图 + gpu0-capture |
| `cmp170hx-ffcalc/` | 逐卡 FF 计算(交叉验证用) |
| `cmp-170hx-rir-burn-util/` | CMD_WRITE 写机制参考 |
| `cmpunlocker/` | 80GB 显存几何 + PCIe 补丁(须配 nvidia-open 615.71.09) |
| `CMPGlitch/` | 完整记录快照:Gen4 跑通方案 + 备份/回滚工具链(驱动 610.43.03) |

> 上游仓库保留各自的 `.git`,可独立 pull 更新;外层仓库将它们视为嵌套仓库,不重复跟踪其内容。
> `open-gpu-kernel-modules-615.71.09.tar.gz`(25M)不入库,克隆后手动放回根目录与 `cmpunlocker/driver/.build/`。

---

> 对象:CMP 170HX(8GB 版实际板载 ~80GB HBM,70 SM,PCIe Gen1)。
> 目标:**81920 MiB 显存 / 126 SM / PCIe Gen4 能力 / NVLink**。
> 本手册 = 实物接线 + 打毛刺 + 逐卡计算 + 烧 FF + 驱动层,端到端已在真实卡跑通(2026-11-15)。
> ⛔ **FF 烧写物理不可逆,烧错即报废。每个阶段都有"验证关",不过关不进下一阶段。**
> 阅读方式:按阶段顺序做;每阶段末有"✅ 达成判据",满足才继续。

---

## 阶段 −1 · 准备(硬件/软件/环境)

### 目的
把"打毛刺 + 读卡 + 写 fuse + 驱动"所需的软硬件全部备齐。

### 硬件清单
| 项 | 说明 |
|---|---|
| CMP 170HX 显卡 | 8GB 版才有 80GB 潜力(10GB 版目标 40GB) |
| Raspberry Pi Pico 2 / 2 W / Waveshare Plus-W | RP2350,打毛刺用 |
| 漆包线/跳线 ×4 | 18–20AWG,镀锡头 |
| 烙铁 + 助焊剂 + 放大镜 | 焊卡背面 4 个点 |
| Linux 主机 | 装这张卡、能 sudo;**根端口决定 PCIe 上限**(要 Gen4 需主板/插槽支持) |

### 软件/工具链(在主机上装)
```bash
# 固件编译
pico-sdk                                   # 编 Pico 固件
# 只读采集/计算
rust toolchain                             # 编 gpu0-capture(可选)
python3 + pip                              # ffcalc
gcc make linux-headers-$(uname -r)         # 编 ffprobe.ko 采集模块
```
- 仓库(已备齐):`pico-ga100vfi`(打毛刺)、`cmp170hx-ffcalc`(算计划,**用打过 issue#1 补丁的版本**)、`cmp-170hx-rir-burn-util`(写机制参考)、`ga100vfi`(寄存器地图)。
- **cmpunlocker**(软件层显存/PCIe)+ nvidia-open **615.71.09** 驱动(⚠️ 别用 615.78.08)。

### ✅ 达成判据
- 硬件在手;Pico 能进 BOOTSEL;主机能 `lspci | grep -i nvidia` 看到卡(PCI ID `10de:20c2`)。

---

## 阶段 0 · 接线 + 打毛刺进 Goldilocks

> 目的:**临时**让 fuse 控制器在这次 boot 把控制行读成全 0 → `DSOV=0`(software override 解锁)。
> ⚠️ 这是**易失**状态,断电/PEX_RST 即丢;只用于"读 fuse + 拿烧写权限",不是最终解锁。

### 0.1 接线(卡背面 4 根线,可逆)
| 信号 | 卡上焊点 | Pico 脚 |
|---|---|---|
| I2CC **SDA** | **Q808 上方的 pad** | **GP0**(物理 4 脚) |
| I2CC **SCL** | **Q808 左下的 pad** | **GP1**(物理 5 脚) |
| **PEX_RST** | **U809/U810 中间 pad** | **GP22**(物理 30 脚) |
| **GND** | 任意 GND 过孔 | **GND**(物理 3 脚) |

**实物接线总览**(Pico 平放卡面,USB-C 朝下;4 根线连到卡背面):

![实物接线总览](pico-ga100vfi/docs/images/board-wiring-overview.jpg)

**焊点特写**(Q808 的 SDA/SCL,U809/U810 的 PEX_RST):

![焊点特写1](pico-ga100vfi/docs/images/tap-points-closeup.jpg)

![焊点特写2](images/position.jpg)


**接线示意**(信号流向):

![接线示意](pico-ga100vfi/docs/images/wiring-diagram.jpg)

**接线与时序框图**:

![Pico 接线与时序](images/pico_flow.png)

**红线:** PEX_RST **直连禁串电阻**(1.8V push-pull);固件只拉低不驱高;每焊点 ≤2 秒;不焊 NVVDD(正常编译用不到);全程可逆。

### 0.2 主机准备(关键,否则读不到卡)
```bash
# 按依赖顺序卸驱动(lightdm/lactd 会抢回,需先停)
sudo systemctl stop lightdm lactd; sudo pkill -x Xorg
sudo modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia
# 禁用会强装驱动的 install 规则(若有)
sudo mv /etc/modprobe.d/cmp-preflight.conf{,.disabled} 2>/dev/null
# 拉黑(aa- 前缀排最前)
sudo tee /etc/modprobe.d/aa-blacklist-nvidia-campaign.conf <<EOF
blacklist nvidia
blacklist nvidia_drm
blacklist nvidia_modeset
blacklist nvidia_uvm
install nvidia /bin/false
EOF
```

### 0.3 打金窗
```bash
pip install -e pico-ga100vfi/host/
sudo env PYTHONPATH=$PWD/host python3 -m ga100vfi campaign \
  --bdf 0000:2f:00.0 --adaptive --max-attempts 0 --restore-wait 0.4
# --bdf 换成你的卡地址
```
工具自动:在 PEX_RST 释放沿打浅三脉冲 → 读 BAR0 判 5 项 → 没过按 6.25mV 调 `v_dip` 重打。实测中位 ~5–16 发、几秒即中。

**打毛刺→判定流程**(软件自动闭环,无需示波器):

![判定流程](images/judge_flow.png)

**实测三脉冲波形**(命令→电压滞后约 46µs,已用 delay 补偿;来自 Kate 单卡 100 发实测):

![实测三脉冲波形](pico-ga100vfi/docs/images/nvvdd-waveform.png)

### ✅ 达成判据(Goldilocks)
终端打出 `"passed": true`,且:
```
DSOV_STATUS  0x00820084 == 0        ← 金窗核心
CRC_STATUS   0x00820070 == 0x55
PRIV_MASK    == 0xffffffff           ← PLM 全开
FPF 行可读 / DSOV 可写回
```
> 只到这一步**断电就回到原样**,不产生任何永久改变。

---

## 阶段 1 · 只读采集 + 逐卡算 FF 计划

> 目的:读出**这张卡自己**的 fuse 现状和缺陷掩码,算出"该烧哪几行、烧什么值"。**只读,不改卡。**
> 用本目录一体工具 `utils/ga100_ff.py`(内置 issue#1 防呆 + 按本卡缺陷算目标)。

```bash
# 设 PYTHONPATH 指向 pico-ga100vfi 的 host(工具靠它读卡)
export PYTHONPATH=$PWD/pico-ga100vfi/host
sudo python3 utils/ga100_ff.py plan --bdf 0000:2f:00.0 -o plan.json
```
工具做:读金窗状态 + 全部 FF 物理行(222–487) + 本卡 `_DEFECTIVE` 缺陷掩码 → 按"**目标=缺陷(只禁真坏的)**"算出每条 FF 记录,从已用行之后追加空白行。**只读,不写。**

> ⚠️ 若 `plan.json` 里 `ff_rows` 为空(没枚举到物理行),工具会**拒绝出计划**(防止像 ffcalc issue#1 那样从行 222 起排、覆盖已有记录)。

**目标值公式:`XXX_DISABLE := XXX_DEFECTIVE`**。例:`FBP_DISABLE 0x852→0x840`、`FBPA/FBIO 0xC0330C→0xC03000`、`GPC 0x13→0`。

### ✅ 达成判据
- `plan.json` 生成,`records` 非空;每条 `row` 从已用行之后追加(**不是 222**);打印的 `old->new` 合理(只放开非缺陷位)。
- (可选交叉验证)用 `cmp170hx-ffcalc` 再算一遍对比:`ffcalc report/gen/sim`,结果应一致。

---

## 阶段 2 · 烧前预演(强制 dry-run)

> 目的:真烧前,先 dry-run 确认金窗在、每条目标行当前确为空白。

```bash
sudo python3 utils/ga100_ff.py burn --plan plan.json        # 默认 dry-run,不写
```
打印每行 `row <- record  当前 0x00000000`;若有 `★非空!` 则**停**,回阶段 1 重算。

### ✅ 达成判据
- dry-run 无 `★非空!`;`DSOV=0 / CRC=0x55`。

---

## 阶段 3 · 拿烧写权限(arm)+ 物理烧 FF(永久!)

> 目的:在金窗内依次打开烧写门,把计划逐条写进空白 OTP 行。**这是唯一永久、烧错即砖的步骤。**
> 用 `utils/ga100_ff.py burn --commit`,它内置下面的 arm 序列 + 逐条校验(每步带断言,失败即停)。

### 3.0 烧前备份(强烈建议,CMPGlitch 经验)
烧前先把整张卡的 fuse 现状导出存档,并准备一份"恢复计划"(用各 block 的 old_data 生成),万一烧错还能靠"同 block 后写覆盖"补一条恢复记录:
```bash
# 备份全行 + 影子寄存器(只读)
sudo python3 CMPGlitch/tools/ff_dump.py --bdf <bdf> -o ff_backup.json
```
> FF 是"后写赢":烧错不至于彻底无救,可用恢复记录把有效值改回去(但物理行已消耗,不可逆的是"行被用掉",不是"值改不回")。

```bash
sudo python3 utils/ga100_ff.py burn --plan plan.json --commit     # 真烧(永久!)
```

### 3.1 arm 序列(工具自动执行,每步带断言)
```
① 断言金窗:DSOV==0 且 CRC==0x55
② FUSECTRL=3 (SENSE_CTRL) → wait_idle → 断言 EFP==1 且 DSOV 仍==0
③ EN_SW_OVERRIDE(0x00820040)=1 → 读回==1
④ SBFE(0x00821108)=0 → 读回==0
⑤ WDATA_WR_SECURE(0x0082048C)=0 → 读回==0
```
armed 自检表(全对才动手):
`FUSECTRL=0xE0040000  CRC=0x55  EFP=1  DSOV=0  SBFE=0  WDATA_SEC=0  EN_SW=1`

### 3.2 烧写(工具逐条执行)
```
读行(空行检查==0) → 写 FUSEWDATA(0x0082000C) → FUSECTRL=CMD_WRITE → 回读校验
```
- FF 区 row 222–487;**控制行 0–3 永远禁写**;空行=读回 `0x0`。
- **逐条:空行检查 → 写 → CMD_WRITE → 回读**;每条后复读 `CRC/DSOV/EFP` 确认没被带坏。
- **先单行试烧**:计划里挑一条(如 fbp)单独烧 → 断电冷启 → `CRC==0x55` → 再烧全量。
- **PCIe 记录最危险**(写错可能掉总线),放最后、一次烧齐一次验证。

### 3.3 血泪红线(实测踩坑)
1. **写口是 `0x0082000C`,不是 `0x00820008`**(读口)。写错端口会"静默成功、回读还是 0、记录丢失"。
2. **绝不直接写 PLM 寄存器 `0x008200F4/F8/FC`**。注意区分两种做法:
   - ❌ **直接写 PLM=0xFFFFFFFF**(社区 `ffburn.py` 的做法)→ 把熔丝控制器写死(FUSECTRL 读 0、CRC=0、影子全 0),只能 PEX_RST 救。
   - ✅ **清 `WDATA_WR_SECURE(0x0082048C)=0`** → PLM **自动**全开为 0xFFFFFFFF(这是正确路径,CMPGlitch 与 zhiyu 都这么做)。PLM 是"结果",不是"写入对象"。
3. **FF 烧写必须 `DEBUGCTRL.RWL=0`**(主阵列)。RWL=1 会把写路由到 **RIR macro**,可能卡死 die;CMPGlitch 的 ff_burn 写前会主动清 RWL。⚠️ 与 zhiyu 记录"RWL 不用置"一致——主阵列烧写**不要碰 RWL**。
4. **金窗易失**:任何 PEX_RST/驱动加载都会丢;一个金窗内烧完。
5. 某些机器 `reboot` 会挂死 → 一律**物理断电冷启**。
6. **烧不全=PES/ROP_L2 漏烧会 GSP 启动失败**(见阶段 4 红线)。

### ✅ 达成判据
- 每条记录 `readback == 写入值`;全程 `CRC=0x55 DSOV=0 EFP=1`;计划行全部烧完无报错。

---

## 阶段 4 · 断电冷启 + 功能验收

> 目的:让 fuse 控制器重放新 FF 链,把解锁体现在派生寄存器和功能上。

**物理断电冷启**(不是 reboot),然后:
```bash
lspci -vv -s <bdf> | grep -E "LnkCap:|LnkCap2:|LnkSta:"
nvidia-smi --query-gpu=memory.total,pcie.link.gen.current --format=csv
# CUDA: multiProcessorCount / cudaMemGetInfo
# BAR0 影子寄存器(root,只读 mmap resource0)
```

**验收对照(烧后期望):**
```
FBP_DISABLE=0x840  FBPA/FBIO=0xC03000  GPC=0  NVLINK=0
GEN3/Gen4/BOOT_DIS=0  TPC 仅坏核保留
LnkCap2: 2.5-16GT/s      ← Gen4 能力释放
memory.total: 81920 MiB  ← 需阶段 5 显存几何后才满血
```
**显存功能验证**(证明不是 L2 别名幻觉):写个 kernel 分配 ~76GiB,`a[i]=i^常量` 全量写读比对,`mismatches=0`。

> ⛔ **漏烧 PES/ROP_L2 = GSP 启动失败**(CMPGlitch Pass1 实测):只烧 GPC/TPC/FB* 而不烧 PES/ROP_L2,会 `failed to wait for GFW boot complete: 0x55`。PES/ROP_L2 是每个 SM 必需的块,同为 chain-1 floorsweep,**必须与 GPC/TPC 同批烧齐**再上电验证。

### ✅ 达成判据
- 影子寄存器 `XXX_DISABLE == XXX_DEFECTIVE`;`LnkCap2` 到 16GT/s;SM 数上升;显存全量写读 0 错误;**GSP 正常启动**(无 Xid、dmesg 干净)。

---

## 阶段 5 · 软件层:满血显存 + 实链提速

> 目的:FF 只解开"禁用层";**满血 80GB 显存**和**实际链速**都要驱动层配合(每次开机重做)。

### 5.1 满血 80GB 显存
- FF 解开后,显存几何(`FBPA_CFG`/`SM_SPEED_SELECT` 等)默认仍只用部分 HBM → 需 **cmpunlocker** 写更 permissive 的几何。
- 装 nvidia-open **615.71.09** + cmpunlocker;每次驱动加载时写入。

### 5.2 PCIe 实链(Gen3/Gen4)

熔丝只"允许"(`LnkCap2` 变 16GT/s),实链被固件钉死。限制分三层:FF 熔丝 / IFF+DevInit PHY / VBIOS 的 Gen4 抑制块。

**Gen3(CMPGlitch 实测跑通,LnkSta 0x1083,H2D/D2H ~6.6 GB/s,约 1.97×):**
- 三层叠加:FF 熔丝(清 `DISABLE_GEN3_SPEED`、boot 策略)+ 驱动 post-GSP 补丁(`CmpGen3=1`,插在 `kgspBootstrap_TU102` 的 BooterLoad 成功**之后**——固件会把更早的写 re-clamp 掉)+ 经 RM 分阶训练 Gen1→2→3。
- **大坑**:`LINK_CONFIG_0[19:18]` 是**反转编码**(1=Gen3),与 config-space LnkCtl2(3=Gen3)**相反**,写反会坠链。

**Gen4(CMPGlitch 实测跑通 Gen4 x16,LnkSta 0x1104,带宽 24.7/26.3 GB/s):**
- 在 Gen3 基础上追加 FF:把 `PCIE_MAGIC_BITS_D` 清到与 A100 逐位一致;
- **关键**:Gen4 不是靠 RM 驱动补丁(RM `SET_PCIE_SPEED(4)` 被闭源固件拒),而是 **LTSSM 硬件 retrain**,绕开 GSP-RM:
  ```bash
  setpci -s <bridge> CAP_EXP+30.w=0x4   # 桥 Target=Gen4
  setpci -s <gpu>    CAP_EXP+30.w=0x4
  setpci -s <bridge> CAP_EXP+10.w=0x20  # 触发 Retrain Link
  ```
- **权威判据**:`setpci` 读 LnkSta、sysfs `current_link_speed`、实测带宽。**别信 `nvidia-smi` 的 gen 显示**(那是 boot 时缓存的 linkCap,不刷新)。
- **硬件前提**:Gen4 需 24×0402 0.22µF 去耦电容 mod + 直插 x16 + 主板/插槽支持 Gen4。

**血泪:**
- 运行中绝不从用户态写 XP 寄存器(`0x8C040`/`0x880A8` → BAR0 全 `0xFFFFFFFF` 变砖态)。
- `0x881C0 SYSTEM_MAX_SPEED` 的 Booter 写是 once-gating,会 wedge GSP 启动(FECS_PRI_TIMEOUT)——这条路线已死,勿试。
- **上限看根端口**:主板/插槽只到 8GT/s 则 Gen4 物理不可能。

### 5.3 NVLink(可选)
- 清 `NVLINK_DISABLE`(FF 层)只是其一;**还需补焊 U11**(170HX 缺这颗 IC,不起参考时钟)+ 主机侧绕 ROM check。无 U11 时先只做"解锁层验证"。

### ✅ 达成判据
- `nvidia-smi` 显存 81920 MiB;`pcie.link.gen.current` 到主板支持的最高代;要 NVLink 则 `nvidia-smi topo -m` 见链路。

---

## 附录 A · 持久性速查
| 层 | 持久性 | 以后要不要重做 |
|---|---|---|
| 打毛刺(Goldilocks) | ❌ 仅当前上电 | 没烧 FF 前每次上电要重打 |
| **烧 FF(OTP)** | ✅ **永久** | **烧一次永久,以后插上即解锁,免毛刺免 Pico** |
| 显存几何 / 链速(软件) | ❌ 每次开机 | 做成驱动补丁自动打 |

## 附录 B · 坑速查
| 现象 | 原因 | 处理 |
|---|---|---|
| 烧写"成功"但回读 0 | 写到读口 0x00820008 | 改写口 0x0082000C |
| 控制器全 0、CRC=0 | 写了 PLM 0xF4/F8/FC | 重打 campaign;别碰 PLM |
| RWL latch 不住 | FF 不需要 RWL | 放弃 DEBUGCTRL 路线 |
| 卸不掉 nvidia | lightdm 抢回 / install 规则强装 | 停服务+禁 conf+逐个卸 |
| 金窗后 DSOV 又变 1 | PEX_RST/驱动加载 | 卸驱动再打;一窗烧完 |
| 重启机器不回 | 本机 reboot 病 | 物理断电冷启 |
| 烧完还 5GT/s | 驱动回锁(post-GSP re-clamp) | Gen3:post-GSP 补丁+分阶训练;Gen4:LTSSM 硬件 retrain |
| GSP 启动失败 0x55 | 漏烧 PES/ROP_L2 | 与 GPC/TPC 同批烧齐再冷启 |
| Gen4 训不上 | RM SET_PCIE_SPEED 被固件拒 | 用 setpci 硬件 retrain,别走 RM |
| 烧写路由错/卡 die | DEBUGCTRL.RWL=1 | 主阵列烧写保持 RWL=0 |
| 显示 80GiB 一碰就崩 | 只改 FBP 没改 FBPA/FBIO,或 L2 幻觉 | 全量 fill/verify 实测 |
| 计划从行 222 起排 | ffcalc ff_rows 空(issue#1) | 用 utils/ga100_ff.py(已防呆),确认 ff_rows 非空 |

## 附录 C · 权威来源
- 端到端真实记录(2026-11-15 跑通,81920MiB/126SM/NVLink0/Gen4 能力)
- **CMPGlitch 记录**(本目录 `CMPGlitch/`,Gen4 x16 跑通 + 烧前备份/回滚 + PES/ROP_L2 红线 + 9 项烧前检查)
- Kate 博客(原理) / nvidia-open `dev_fuse.h`(寄存器权威定义)
- 仓库:`pico-ga100vfi` / `cmp170hx-ffcalc`(打 issue#1 补丁) / `cmp-170hx-rir-burn-util` / `ga100vfi`;一体工具 `utils/ga100_ff.py`
