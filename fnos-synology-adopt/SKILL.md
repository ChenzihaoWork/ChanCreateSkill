---
name: fnos-synology-adopt
display_name: "群晖硬盘接入飞牛 fnOS"
display_name_en: "Adopt Synology Disks into fnOS"
description: "把从群晖(Synology)拆下来的硬盘无损接入飞牛 fnOS：打内核 btrfs 兼容补丁、LVM 改名注册为原生存储空间、把群晖共享文件夹挂进「我的文件」、飞牛升级后自动恢复挂载。当用户提到群晖盘迁移到飞牛、群晖硬盘挂不上飞牛、invalid root flags、飞牛找不到群晖文件时使用。"
description_zh: "群晖硬盘无损迁移到飞牛 fnOS：内核补丁、注册存储空间、共享文件夹挂载、升级自愈"
description_en: "Adopt Synology disks into fnOS losslessly: kernel patch, storage pool registration, shared folder bind-mounts, post-upgrade self-healing"
category: system
version: 1.0.0
author: "WorkBuddy"
metadata:
  tags: ["fnos", "synology", "btrfs", "nas", "disk-migration", "lvm"]
  requires: ["root on fnOS", "python3", "xz", "zstd", "paramiko(可选)"]
---

# 群晖硬盘接入飞牛 fnOS

把群晖拆下来的硬盘**不格式化、不丢数据**接入飞牛，成为飞牛「原生存储空间」，
并把群晖原有的共享文件夹挂到「我的文件」里。飞牛升级后自动恢复。

## 什么时候用这个 Skill

用户说这些话时：

- 「群晖的硬盘插到飞牛上了，怎么挂载 / 读不出来」
- 「飞牛里看不到群晖的文件」
- 「invalid root flags」「can't read superblock」「invalid incompat flags」
- 「群晖盘迁到飞牛」「群晖硬盘搬家」
- 「升级飞牛后群晖盘挂不上了」
- 「飞牛的存储空间管理里没有那块盘」

## 先决条件（务必先确认）

1. **SSH 能登录飞牛且能 sudo/root**
2. 群晖盘已物理装好，且 `mdadm` 阵列已组装、LVM 已激活：
   ```bash
   cat /proc/mdstat          # 应能看到 mdX
   pvs; vgs; lvs             # 应能看到 vg1/volume_1 之类
   ```
   没组装就 `mdadm --assemble --scan && vgchange -ay`
3. 有足够时间做**只读验证**，一旦数据确认无误再往下走

> 先跑 `sudo python3 scripts/verify_env.py --verbose` 一次性看全所有前置条件。

## 适用边界（重要，先确认你的盘属于哪种）

本 skill 的脚本默认针对**最常见的一种布局**，其他布局需要手工调整：

| 群晖存储类型 | 支持度 | 说明 |
|---|---|---|
| **Basic（单盘）** | ✅ 完整支持 | 分区 → mdadm(单成员) → LVM → btrfs，脚本直接可用 |
| **RAID 1** | ✅ 支持 | mdadm 双成员，LVM 层与 Basic 相同 |
| **RAID 5 / 6 / SHR** | ⚠️ 需手工调整 | mdadm 多成员；LVM 之后完全相同，只需确认 PV 全部就绪 |
| **JBOD / 多盘拼接** | ⚠️ 需手工调整 | 同 RAID5 |
| **ext4 卷** | ❌ 不支持 | 补丁只针对 btrfs；ext4 无需补丁但要另写挂载逻辑 |
| **加密共享文件夹** | ❌ 不支持 | 需要群晖的密钥，无法在飞牛解密 |
| **iSCSI LUN / VMM 虚拟盘** | ❌ 不支持 | 不是文件系统层的东西 |

### 判断自己属于哪种

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL
cat /proc/mdstat        # raid1 sdd5[0] = 单成员(Basic)；带 sde5[1] = 双成员(RAID1)
lvs -o vg_name,lv_name,lv_size
```

### 关键认知：不管哪种布局，接入动作都是一样的

只要最终能构造出 `/dev/mapper/<VG>-<LV>` 且它是 btrfs，
后面「改名为 `trim_<uuid>` / `0` + 写 mount 表」就是**通用的**，
与底层是 Basic 还是 RAID5 无关。

所以脚本的设计是：**自动探测 + 显式失败**，不会静默出错。
探测不到时会告诉你去查什么，而不是硬跑。

## 环境差异适配（不同飞牛版本）

脚本已经做了这些自适应，遇到不同环境时先看这里：

| 差异点 | 脚本怎么处理 | 你需要做什么 |
|---|---|---|
| 内核版本不同 | 补丁按**特征值扫描**定位，不写死地址 | 无需操作 |
| 内核 btrfs 结构大改 | 报「未能定位到可补丁的掩码」并中止 | 见 [troubleshooting E2](references/troubleshooting.md)，需人工反汇编 |
| initrd 压缩格式不同 | 支持 zstd / gzip / xz / lz4 | 无需操作 |
| initrd 层间 padding 长度不同 | 自动跳过前导 0 | 无需操作 |
| 已有存储空间数量不同 | `id` 自动取下一个空闲号 | 无需操作 |
| mount 表少列/多列 | 启动时检查必需列，缺了明确报错 | 按提示人工确认 |
| 没有 `sudo` 命令 | 自动回退到 `su postgres -c` | 无需操作 |
| 群晖 VG/LV 名称不同 | 按 fstype=btrfs 探测，不看名字 | 无需操作 |
| `@syno` 子卷不存在 | 用默认子卷，只提示不报错 | 见 [troubleshooting B3](references/troubleshooting.md) |
| `share_service` 不存在 | 只警告，不中断 | 挂载后手动重启相关服务 |

**铁律**：任何一步探测不到预期值，宁可**明确失败**也不要猜着往下走。
写错 mount 表或改错 LVM 名的代价远高于停一下问人。

## 核心认知：这活儿有三个隐藏门槛

大多数人卡在这里，README 式的教程都没写：

### 门槛 1：群晖的 btrfs 有私有 flag，主线内核拒绝挂载

直接 `mount` 会失败：

```
BTRFS critical: corrupt leaf: invalid root flags,
                have 0x400000000 expect mask 0x1000000000001
BTRFS critical: corrupt leaf: unknown incompat flags detected: 0x40000000
```

必须给 btrfs 内核模块打**三处**补丁（放宽 flag 校验）：

| 位置 | 原始值 | 补丁后 |
|---|---|---|
| root flags 掩码 | `0xfffefffffffffffe` | `0xfffeff01fffffffe` |
| 错误显示掩码 | `0x1000000000001` | `0x100fe00000001` |
| inode flags 掩码 | `0x7ffff000` | `0x3ffff000` |

→ 用 `scripts/gen_btrfs_patch.py`

### 门槛 2：initrd 里内嵌了一份原版 btrfs.ko，会抢先加载

**只替换 `/lib/modules/.../btrfs.ko.xz` 是不够的。**
飞牛的 initrd 里也有一份（未压缩），initramfs 阶段就会 modprobe 加载，
之后模块再也换不掉 —— 补丁等于白打。

必须**同时**替换 initrd 内嵌的那份。

→ 用 `scripts/ird_tool.py`

### 门槛 3：不能用上游源码重编译内核模块

飞牛的 btrfs.ko 有私有挂载选项 `trimacl`，上游没有。用上游源码重编译会
导致**飞牛系统盘 `/vol1` 挂不上**，系统起不来。

必须给飞牛自己的 `.ko` 做二进制补丁。

## 标准流程

### 阶段 A：环境检查与补丁

```bash
# 1. 确认系统信息
uname -r            # 记下内核版本
ls /lib/modules/    # 看有几个内核版本（可能有旧残留）

# 2. 检查补丁状态
python3 scripts/gen_btrfs_patch.py --check --kernel "$(uname -r)"

# 3. 打补丁（会自动备份原模块为 .orig-backup）
python3 scripts/gen_btrfs_patch.py --kernel "$(uname -r)"

# 4. 同步 initrd（关键！拿上一步生成的未压缩补丁模块）
python3 scripts/ird_tool.py \
    --initrd /boot/initrd.img-$(uname -r) \
    --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko

# 5. 重启使新模块生效（运行中的模块无法热替换，/vol1 挂着卸不掉）
reboot
```

> 重启是必须的：`/lib/modules` 里的模块文件换了，但**运行中的内核仍用旧模块**。
> 而且飞牛系统盘 `/vol1` 用 btrfs，卸载不掉、`rmmod` 会失败。
> 好消息：补丁只放宽 flag 校验，**即使补丁无效系统也能正常启动**，不会变砖。

### 阶段 B：注册为飞牛原生存储空间

```bash
# 1. 扫描可接入的群晖卷（只读，安全）
python3 scripts/adopt_synology.py --scan

# 2. 演练：看会做什么，不改任何东西
python3 scripts/adopt_synology.py --adopt --dry-run

# 3. 真正执行（自动备份 LVM 元数据 + mount 表）
python3 scripts/adopt_synology.py --adopt --yes

# 4. 重启，让飞牛 triminit 自动挂载
reboot

# 5. 验证
mount | grep ' /vol'
```

这一步做了三件事：

1. **LVM 改名**：`vg1/volume_1` → `trim_<uuid>/0`（飞牛的命名约定）
2. **设默认子卷**为 `@syno`（这样 `/volN` 根下直接是共享文件夹，不多一层）
3. **写 mount 表**：往 PostgreSQL `trim` 库的 `mount` 表插一行，
   `id` 对应 `/volN`，`uuid` 是 `trim_<uuid>-0`，`pool_type=0`（btrfs 单盘）

> **为什么不自己写 systemd 挂载服务**：飞牛的 `triminit` 会根据 `mount` 表
> 自动挂载所有存储空间。自建挂载服务如果 `ExecStart=/bin/mount`，
> 在 md/LVM 未就绪时会**无限阻塞**，卡死整个启动流程（教训惨痛）。
> 交给飞牛自己管最稳。

### 阶段 C：把共享文件夹挂进「我的文件」

此时卷已挂在 `/vol2`、`/vol3`…，但**「文件管理 → 我的文件」里看不到** ——
因为飞牛那个视图只显示各卷 `/volN/1000/` 目录下的内容。

```bash
# 1. 列出各卷可挂载的共享文件夹
python3 scripts/bind_shares.py --list

# 2. 生成清单
python3 scripts/bind_shares.py --generate

# 3. 执行 bind 挂载（会自动重启 share_service 刷新索引）
python3 scripts/bind_shares.py --apply

# 4. 装开机自动恢复
python3 scripts/bind_shares.py --install-service
```

原理：飞牛 `share_service` 里的 **TrimAFS** 会把 `/volN/1000/` 下的每个目录
自动注册为共享文件夹。所以把群晖共享文件夹 bind 挂过去即可。

> **必须用 bind 挂载，不能用软链接** —— 飞牛 UI 不认软链接。

### 阶段 D：装升级自愈机制（强烈建议）

```bash
# 一键部署（含 systemd 服务 + 30 分钟定时检查）
sudo python3 scripts/install.py --source-dir ./scripts

# 或者部署并立刻给当前内核打补丁 + 同步 initrd
sudo python3 scripts/install.py --source-dir ./scripts --patch-now

# 查看部署状态
python3 scripts/install.py --status
```

之后飞牛升级换内核时，自愈脚本会自动重新打补丁并同步 initrd，
**无需人工干预**。

## 飞牛升级后会不会失效？

| 情况 | 结果 |
|---|---|
| 升级不换内核 | ✅ 完全正常 |
| 换了内核 | ⚠️ 群晖卷挂不上，**飞牛系统正常**（不会变砖） |
| 覆盖了 initrd | ⚠️ 补丁完全失效 |

**为什么不会变砖**：补丁只放宽群晖私有 flag 的校验，不影响标准 btrfs 功能。
飞牛系统盘用的是标准 flags，所以它照常挂载。

装了阶段 D 的自愈机制后，上述情况都会被自动修复。

升级前建议先检查一次：

```bash
python3 scripts/gen_btrfs_patch.py --check --kernel "$(uname -r)"
# 若新内核提示"未能定位到可补丁的掩码"，说明结构大改，先别升级，需人工适配
```

## systemd 配置的两个致命坑

如果你要自己写服务，务必注意：

### 1. 必须挂 `graphical.target`

| 挂载目标 | 结果 |
|---|---|
| `multi-user.target` | ❌ 永不触发（飞牛的它永远 inactive） |
| `graphical.target` | ✅ 正确（飞牛实际启动的 target） |

### 2. 绝对不要写 `After=trim_main.service`

`trim_main` 是 `Type=simple` + `Restart=always` 的**常驻守护进程**。
systemd 认为它永远不会"进入启动完成状态"，于是所有
`After=trim_main.service` 的 oneshot 服务会被**无限推迟、永不执行**。

调试方法：

```bash
systemctl show <unit> -p WantedBy -p After -p ActiveState
# 若 WantedBy 出现 trim_main.service，说明被错误关联了
```

正确写法：

```ini
[Unit]
Description=...

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=600          # 放 [Service]，放 [Unit] 会报 Unknown key
ExecStart=/usr/local/sbin/xxx.sh

[Install]
WantedBy=graphical.target
```

## 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `can't read superblock` | btrfs 模块未打补丁 | 跑 `gen_btrfs_patch.py` + `ird_tool.py` + 重启 |
| 打补丁后仍报 `invalid root flags`，mask 没变 | initrd 抢先加载原版 | 同步 initrd 后重启 |
| `Key was rejected by service` | 模块带无效签名 | `gen_btrfs_patch.py` 会自动剥离签名尾 |
| `Module btrfs is in use` | `/vol1` 还挂着 | 停 `trim_file_monitor` + 停 NFS，完整 umount |
| `mount: /vol1: target is busy` | 有进程持有 | `fuser -vm /vol1` 找；常见是 `trim_file_monitor`、`nfsd` |
| 挂载成功但 UI 看不到 | 没注册 mount 表 | `adopt_synology.py --adopt` |
| 存储空间管理里没有盘 | LV 名不叫 `0` 或 VG 名不是 `trim_` | 同上，脚本会自动改名 |
| 「我的文件」里看不到共享文件夹 | 未 bind 到 `/volN/1000/` | `bind_shares.py --generate --apply` |
| 重启后 bind 挂载全丢 | 服务挂错 target | 改 `WantedBy=graphical.target` |
| 重启后服务不执行 | 写了 `After=trim_main` | 删掉这行 |

## 安全守则（不可违反）

- ✅ 全程**先只读验证**，确认数据在再写
- ✅ 危险操作前**自动备份** LVM 元数据和数据库
- ✅ 改名后立刻**只读试挂验证**，失败自动回滚
- ❌ 绝不 `mkfs` / `mdadm --stop` / `dd` 写数据区
- ❌ 绝不用上游源码重编译飞牛的 btrfs 模块
- ❌ 绝不为了"省事"跳过 initrd 同步

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `gen_btrfs_patch.py` | 生成群晖兼容的 btrfs 内核模块补丁（自适应内核版本） |
| `ird_tool.py` | 替换 initrd 内嵌的 btrfs.ko（不换则补丁无效） |
| `adopt_synology.py` | 扫描/LVM 改名/注册 mount 表/回滚 |
| `bind_shares.py` | 把共享文件夹 bind 到 `/volN/1000/` + 装开机服务 |
| `patch_watchdog.sh` | 升级后自愈主脚本 |
| `install.py` | 一键部署工具链 + systemd 服务 |
| `nas_exec.py` | SSH 远程执行助手（Agent 用） |

## 详细文档

- [原理详解](references/how-it-works.md) —— 为什么需要打补丁、位运算细节
- [完整操作手册](references/step-by-step.md) —— 从零到可用的每一步
- [故障排查](references/troubleshooting.md) —— 按现象索引的排错指南
