# fnos-synology-adopt —— 群晖硬盘接入飞牛 fnOS

> 把群晖(Synology)拆下来的硬盘**不格式化、不丢数据**接入飞牛 fnOS，
> 成为飞牛「原生存储空间」，共享文件夹出现在「我的文件」里，飞牛升级后自动恢复。

---

## 一、它解决什么问题

从群晖退役下来的硬盘，装进飞牛 fnOS 之后会遇到一连串看起来无解的问题：

| 现象 | 真正的原因 |
|------|-----------|
| `mount: can't read superblock` | 盘没坏，是内核太严 —— 群晖的 btrfs 带私有 flag，主线内核拒绝挂载 |
| 补丁打了也没用 | initrd 里内嵌了一份原版 btrfs.ko，会抢先加载，补丁等于白打 |
| 挂上了但「存储空间管理」里没有它 | 飞牛靠自己的 mount 表 + LVM 命名约定识别存储空间 |
| 卷挂上了但「我的文件」是空的 | 飞牛的「我的文件」只显示 `/volN/1000/` 下的内容 |
| 飞牛一升级就全丢 | 升级换了内核，补丁失效 |

网上能找到的教程要么只解决其中一步，要么教你**重新格式化重做 RAID**（数据没了），
要么让你**用上游源码重编译 btrfs 模块**（飞牛系统盘挂不上，直接变砖）。

这个 Skill 把整条链路打通，并且把上面五个坑全部显式处理掉。

---

## 二、优势

### 1. 不重编译内核 —— 不会变砖

飞牛的 btrfs.ko 带私有挂载选项 `trimacl`，上游内核源码里根本没有。
用上游源码重编译 → 飞牛系统盘 `/vol1` 挂不上 → 系统起不来。

本 Skill 走**二进制补丁**路线：直接对飞牛自己的 `.ko` 打补丁，只放宽群晖私有 flag 的校验位。

### 2. 失败也不会变砖

补丁只放宽群晖私有 flag 的校验，**不影响标准 btrfs 功能**。
飞牛系统盘用的是标准 flags，照常挂载、照常启动。

最坏情况：升级换了内核 → 群晖盘暂时挂不上 → **飞牛系统完全正常**。

### 3. 自适应，不写死

所有补丁点按**特征值扫描**定位（不硬编码地址/偏移）：

- 内核版本不同 → 自动适配
- initrd 压缩格式不同 → 支持 zstd / gzip / xz / lz4
- initrd 层间 padding 长度不同 → 自动跳过前导 0
- 已有存储空间数量不同 → mount 表 id 自动取下一个空闲号
- 群晖 VG/LV 名称任意 → 按 `fstype=btrfs` 探测，不看名字
- 没有 `sudo` → 自动回退到 `su postgres -c`

**换一台 NAS、换一个内核版本都不用改代码。**

### 4. 探测不到就明确失败，绝不猜

脚本的设计原则是**自动探测 + 显式失败**。探测不到预期值时告诉你去查什么，
而不是硬跑下去 —— 写错 mount 表或改错 LVM 名的代价，远高于停一下问人。

### 5. 全程可回滚

- 先只读验证，确认数据在再写
- 危险操作前**自动备份** LVM 元数据与数据库
- 改名后立刻只读试挂验证，失败自动回滚
- 绝不 `mkfs` / `mdadm --stop` / `dd` 写数据区

### 6. 升级自愈

装上 `install.py` 部署的 systemd 服务后，飞牛升级换内核时自动重新打补丁并同步 initrd，
**无需人工干预**。

---

## 三、大致实现方式

### 整体链路

```
群晖硬盘
  └─ 分区 (linux_raid_member)
      └─ mdadm 阵列 (Basic / RAID1 / RAID5 ...)
          └─ LVM (vg1 / volume_1)
              └─ btrfs 卷（带群晖私有 flag）
                    │
                    │ ① 打内核补丁
                    │ ② 同步 initrd 内嵌模块
                    ▼
              ③ LVM 改名 trim_<uuid>/0
                 + 设默认子卷 @syno
                 + 写飞牛 mount 表
                    ▼
              飞牛原生存储空间  /vol2, /vol3 ...
                    │
                    │ ④ 共享文件夹 bind 挂载到 /volN/1000/
                    ▼
              飞牛「我的文件」中可见
                    │
                    │ ⑤ systemd 定时自愈
                    ▼
              飞牛升级后自动恢复
```

### ① 打内核补丁 —— 放宽三处 flag 掩码

群晖的 btrfs 使用了私有 flag，主线内核的校验掩码不认，于是报：

```
BTRFS critical: corrupt leaf: invalid root flags,
                have 0x400000000 expect mask 0x1000000000001
```

`gen_btrfs_patch.py` 通过**特征值扫描**在 `.ko` 里定位这三处常量并改写：

| 位置 | 原始值 | 补丁后 |
|------|--------|--------|
| root flags 掩码 | `0xfffefffffffffffe` | `0xfffeff01fffffffe` |
| 错误显示掩码 | `0x1000000000001` | `0x100fe00000001` |
| inode flags 掩码 | `0x7ffff000` | `0x3ffff000` |

> 注意第三处：`0x7ffff000` 本身已含 bit30，改 inode 掩码要用 `& ~bit` 清除，用 `| bit` 是空操作。

脚本同时处理模块签名问题：带签名的模块会报 `Key was rejected by service`，脚本会自动剥离签名尾。

### ② 同步 initrd —— 容易被忽略的关键一步

**只替换 `/lib/modules/<ver>/btrfs.ko.xz` 是不够的。**

飞牛的 initrd 里也内嵌了一份（未压缩的）btrfs.ko，initramfs 阶段就会被 modprobe 加载，
之后模块再也换不掉 —— 补丁等于白打。

`ird_tool.py` 负责解包 initrd（支持 zstd/gzip/xz/lz4）、替换内嵌模块、再原样打包回去。

### ③ 注册为飞牛原生存储空间

`adopt_synology.py` 做三件事：

1. **LVM 改名**：`vg1/volume_1` → `trim_<uuid>/0`（飞牛的命名约定）
2. **设默认子卷**为 `@syno`，这样 `/volN` 根下直接就是共享文件夹，不多一层
3. **写 mount 表**：往 PostgreSQL `trim` 库的 `mount` 表插一行，
   `id` 对应 `/volN`，`uuid` 是 `trim_<uuid>-0`，`pool_type=0`（btrfs 单盘）

> **为什么不自己写 systemd 挂载服务**：飞牛的 `triminit` 会根据 mount 表自动挂载所有存储空间。
> 自建挂载服务如果 `ExecStart=/bin/mount`，在 md/LVM 未就绪时会**无限阻塞**，卡死整个启动流程。

### ④ 共享文件夹挂进「我的文件」

飞牛 `share_service` 里的 **TrimAFS** 会把 `/volN/1000/` 下的每个目录自动注册为共享文件夹。
所以 `bind_shares.py` 把群晖共享文件夹 bind 挂过去即可。

> **必须用 bind 挂载，不能用软链接** —— 飞牛 UI 不认软链接。

### ⑤ 升级自愈

`install.py` 部署工具链 + systemd 服务 + 30 分钟定时检查。
`patch_watchdog.sh` 检测到内核版本变化时，自动重新执行 ① ② 两步。

---

## 四、怎么用

### 快速开始

```bash
# 0. 先自检，看环境缺什么
sudo python3 scripts/verify_env.py --verbose

# 1. 打内核补丁 + 同步 initrd
sudo python3 scripts/gen_btrfs_patch.py --kernel "$(uname -r)"
sudo python3 scripts/ird_tool.py \
    --initrd /boot/initrd.img-$(uname -r) \
    --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko
sudo reboot

# 2. 注册为飞牛原生存储空间（先 --dry-run 演练）
sudo python3 scripts/adopt_synology.py --scan
sudo python3 scripts/adopt_synology.py --adopt --dry-run
sudo python3 scripts/adopt_synology.py --adopt --yes
sudo reboot

# 3. 共享文件夹挂进「我的文件」
sudo python3 scripts/bind_shares.py --generate --apply --install-service

# 4. 装升级自愈机制
sudo python3 scripts/install.py --source-dir ./scripts
```

### 前置条件

1. SSH 能登录飞牛且能 sudo/root
2. 群晖盘已物理装好，且 mdadm 阵列已组装、LVM 已激活：
   ```bash
   cat /proc/mdstat      # 应能看到 mdX
   pvs; vgs; lvs         # 应能看到 vg1/volume_1 之类
   ```
   没组装就 `mdadm --assemble --scan && vgchange -ay`
3. 有足够时间做**只读验证**，确认数据无误再往下走

---

## 五、适用边界

| 群晖存储类型 | 支持度 | 说明 |
|---|---|---|
| **Basic（单盘）** | ✅ 完整支持 | 分区 → mdadm(单成员) → LVM → btrfs，脚本直接可用 |
| **RAID 1** | ✅ 支持 | mdadm 双成员，LVM 层与 Basic 相同 |
| **RAID 5 / 6 / SHR** | ⚠️ 需手工调整 | mdadm 多成员；LVM 之后完全相同，只需确认 PV 全部就绪 |
| **JBOD / 多盘拼接** | ⚠️ 需手工调整 | 同 RAID5 |
| **ext4 卷** | ❌ 不支持 | 补丁只针对 btrfs；ext4 无需补丁但要另写挂载逻辑 |
| **加密共享文件夹** | ❌ 不支持 | 需要群晖的密钥，无法在飞牛解密 |
| **iSCSI LUN / VMM 虚拟盘** | ❌ 不支持 | 不是文件系统层的东西 |

**关键认知**：不管哪种布局，只要最终能构造出 `/dev/mapper/<VG>-<LV>` 且它是 btrfs，
后面「改名 + 写 mount 表」就是**通用的**，与底层是 Basic 还是 RAID5 无关。

判断自己属于哪种：

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL
cat /proc/mdstat        # raid1 sdd5[0] = 单成员(Basic)；带 sde5[1] = 双成员(RAID1)
lvs -o vg_name,lv_name,lv_size
```

---

## 六、脚本清单

| 脚本 | 作用 |
|---|---|
| `verify_env.py` | 环境自检，一次性看全所有前置条件 |
| `gen_btrfs_patch.py` | 生成群晖兼容的 btrfs 内核模块补丁（自适应内核版本，按特征值扫描） |
| `ird_tool.py` | 替换 initrd 内嵌的 btrfs.ko（不换则补丁无效） |
| `adopt_synology.py` | 扫描 / LVM 改名 / 注册 mount 表 / 回滚 |
| `bind_shares.py` | 共享文件夹 bind 到 `/volN/1000/` + 装开机服务 |
| `patch_watchdog.sh` | 升级后自愈主脚本 |
| `install.py` | 一键部署工具链 + systemd 服务 |
| `nas_exec.py` | SSH 远程执行助手（Agent 用） |

---

## 七、文档

- [SKILL.md](./SKILL.md) — Skill 定义与完整操作手册
- [原理详解](./references/how-it-works.md) — 为什么打补丁、位运算细节、initrd 结构
- [完整操作手册](./references/step-by-step.md) — 从零到可用，含手工操作与回滚
- [故障排查](./references/troubleshooting.md) — 按现象索引

---

## 八、关键教训（踩过的坑）

1. **飞牛不走 `multi-user.target`** —— 它永远 inactive，自建服务必须挂
   `graphical.target`，否则永不触发。

2. **不要写 `After=trim_main.service`** —— `trim_main` 是 `Type=simple` +
   `Restart=always` 的常驻守护进程，systemd 认为它永不"启动完成"，
   所有依赖它的 oneshot 服务会被无限推迟、永不执行。

3. **不要自建挂载服务** —— `ExecStart=/bin/mount` 在设备未就绪时会无限阻塞，
   卡死整个启动流程。交给飞牛的 `triminit` 读 `mount` 表自己挂。

4. **`0x7ffff000` 本身已含 bit30** —— 改 inode 掩码要用 `& ~bit` 清除，
   用 `| bit` 是空操作。

5. **bind 挂载而非软链接** —— 飞牛 UI 不认软链接。

---

## 九、验收标准

```bash
mount | grep ' /vol'                              # 四个存储空间
grep -c '/1000/' /proc/self/mountinfo             # ≥14 个 bind
dmesg | grep -c "BTRFS critical"                  # 0
python3 scripts/gen_btrfs_patch.py --check --kernel "$(uname -r)"   # 已打补丁
```

四项都符合 → 重启一次再验，确认开机自动恢复。
