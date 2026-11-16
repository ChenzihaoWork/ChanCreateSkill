# 原理详解

这篇解释**为什么**需要这些步骤，以及每一步背后的机制。如果你只是想照着做，
看 [step-by-step.md](step-by-step.md) 就够了。

---

## 0. 适用边界（先确认你的盘属于哪种）

本 skill 的脚本默认针对**最常见的一种布局**。不同布局需要不同处理，
先用这张表确认你在哪一栏：

| 群晖存储类型 | 是否支持 | 说明 |
|---|---|---|
| **Basic（单盘）** | ✅ 完整支持 | 分区 → mdadm(raid1 单成员) → LVM → btrfs。脚本直接可用 |
| **RAID 1（两块盘镜像）** | ✅ 支持 | mdadm raid1 双成员，LVM 层与 Basic 相同 |
| **RAID 5 / 6 / SHR** | ⚠️ 需手动调整 | mdadm 多成员，脚本的 PV 探测可能要补；LVM 层相同 |
| **SHR（Synology Hybrid RAID）** | ⚠️ 需手动调整 | 底层是 mdadm + LVM，但可能多层 md，需先 `mdadm --examine` 看清 |
| **JBOD / 多盘拼接** | ⚠️ 需手动调整 | 同上 |
| **ext4 卷**（老 DSM / 部分套件卷） | ❌ 不支持 | 本 skill 的补丁只针对 btrfs；ext4 无需补丁但要另写挂载逻辑 |
| **加密卷（加密共享文件夹）** | ❌ 不支持 | 需要群晖的密钥，无法在飞牛解密 |
| **iSCSI LUN / VMM 虚拟盘** | ❌ 不支持 | 不是文件系统层的东西 |

### 怎么判断自己属于哪种

```bash
# 1. 看盘和分区
lsblk -o NAME,SIZE,FSTYPE,LABEL

# 2. 看 mdadm 阵列有几个成员
cat /proc/mdstat
#   raid1 sdd5[0]        → 单成员（Basic 的本质）
#   raid1 sdd5[0] sde5[1] → 双成员（RAID1）
#   raid5 sdd5[0] sde5[1] sdf5[2] → RAID5/SHR

# 3. 看 LVM 卷上有几个 LV
lvs -o vg_name,lv_name,lv_size
#   volume_1 一个数据 LV          → 标准
#   volume_1 + volume_2 两个      → 群晖多卷（每卷独立 VG，脚本会各自处理）
```

### 不适配时怎么办

脚本的设计是**自动探测 + 显式失败**，不会静默出错。遇到不支持的布局时：

- `adopt_synology.py --scan` 会列出它识别到的卷，也可能一个都不列
  （比如 ext4 卷，`fstype != btrfs` 就被跳过）
- 此时先按上表手工确认结构，再参照
  [step-by-step.md 附录 A](step-by-step.md) 手工执行对应步骤

**关键判断点**：只要最终能构造出
`/dev/mapper/<VG>-<LV>` 且它是 btrfs，后面的「改名为 `trim_<uuid>` / `0` + 写 mount 表」
就是通用的，与底层是 Basic 还是 RAID5 无关。

---

## 1. 群晖和飞牛的存储结构差异

### 群晖 Basic 卷（单盘）

```
/dev/sdX
├─ sdX1  → mdadm 阵列 → ext4     「系统分区」（DSM、/etc/passwd 等）
├─ sdX2  → mdadm 阵列 → swap
└─ sdX5  → mdadm 阵列 → LVM(vg1/volume_1) → btrfs    ← 数据在这里
```

注意群晖的分区编号是 1/2/5（**没有 3/4**，那是给 SHR/RAID 预留的）。数据在
最里层的 btrfs，外面套了两层（LVM + mdadm）。要读到数据必须把三层一节点亮：

```bash
mdadm --assemble --scan    # 点亮 mdadm
vgchange -ay               # 点亮 LVM
mount ...                  # 才是 btrfs
```

### 飞牛 fnOS 的存储空间约定

飞牛对「存储空间」的约定很具体：

| 项目 | 约定值 |
|---|---|
| LVM 卷组名 | `trim_<uuid>`（如 `trim_98f928b9_779d_487b_b898_00cedaec5b0d`，UUID 是随机生成的） |
| 逻辑卷名 | `0` |
| 设备路径 | `/dev/mapper/trim_<uuid>-0` |
| 数据库记录 | PostgreSQL `trim` 库的 `mount` 表一行 |
| `id` 字段 | 对应挂载点 `/vol<id>` |
| `pool_type` | `0` = btrfs 单盘 |
| 挂载选项 | `rw,relatime,trimacl,space_cache=v2` |

飞牛的 `triminit` 启动时会读 `mount` 表，逐个把 `/dev/mapper/<uuid>` 挂到 `/volN`。

**所以「接入」的本质** = 把群晖的 LVM 改成飞牛命名 + 往 `mount` 表补一行。
飞牛并没有魔法，它只是照着约定行事。

---

## 2. 为什么直接 mount 会失败

群晖 DSM 7.x 的 btrfs 用了一些**私有 flag**，而主线内核（5.4+）的
tree-checker 会做严格校验，遇到不认识的 flag 就判定为「文件系统损坏」：

```
BTRFS critical (device dm-3): corrupt leaf: root=1 block=337494016 slot=5,
  invalid root flags, have 0x400000000 expect mask 0x1000000000001
BTRFS error (device dm-3): read time tree block corruption detected
BTRFS error (device dm-3): open_ctree failed: -5
```

用户看到的是 `mount: can't read superblock`，很容易误判成「盘坏了」——
其实盘完好无损，只是内核太严格。

实测见过的几种私有 flag 组合（**你的盘可能是别的值**）：

| root flags 值 | 含义 |
|---|---|
| `0x400000000` | bit34 |
| `0x600000000` | bit33 + bit34 |
| `0xC00000000` | bit34 + bit35 |

不同 DSM 版本、不同卷类型（是否开过快照、是否装了特定套件）会导致位不同。
所以补丁**不是只放行某一位，而是放宽整个 bit33~bit39 区间**（`0xfe00000000`），
这样各种组合都能覆盖。

> ⚠️ **bit 编号极易算错**：`0x400000000` 是 **bit34** 不是 bit33。
> `0x4000000000` 是 bit38，`0x8000000000` 是 bit39。
> 有人就在这里算错，导致部分卷挂不上。

> 💡 **怎么查你自己的盘用哪几位**：
> ```bash
> mount -o ro /dev/mapper/vg1-volume_1 /tmp/t 2>&1
> dmesg | tail -3 | grep -o "have 0x[0-9a-f]*"
> ```
> 报错里的 `have 0x...` 就是实际值。

另外文件 inode 上也有私有 flag：

```
BTRFS critical: corrupt leaf: root=256 block=189038592 slot=0 ino=7608141,
  unknown incompat flags detected: 0x40000000
```

---

## 3. 三处补丁点的由来

### 补丁点 1 & 2：`check_root_item()` 的 root flags 掩码

上游源码 `fs/btrfs/tree-checker.c`：

```c
if (unlikely(flags & BTRFS_ROOT_ITEM_FLAG_MASK)) {   // 有效位之外的位
    generic_err(leaf, slot, "invalid root flags, have 0x%llx expect mask 0x%llx",
                flags, BTRFS_ROOT_ITEM_FLAG_MASK);
    return -EUCLEAN;
}
```

编译成汇编是：

```asm
movabs $0xfffefffffffffffe, %rdx    ; = ~(bit0 | bit32)
test   %rdx, %rcx                   ; flags & ~mask
jne    <报错分支>
```

`0xfffefffffffffffe` 取反是 `0x1000000000001` = bit0（`BTRFS_ROOT_SUBVOL_RDONLY`）
+ bit32。群晖的 bit34/35 不在允许范围 → 报错。

**补丁**：把掩码改成也允许 bit33~bit39：

```python
SYN_ROOT_BITS = 0
for b in range(33, 40):
    SYN_ROOT_BITS |= (1 << b)          # 0xfe00000000

NEW_ROOT_MASK = (~(0x1000000000001 | SYN_ROOT_BITS)) & 0xFFFFFFFFFFFFFFFF
# = 0xfffeff01fffffffe
```

### 补丁点 3：`check_inode_item()` 的 inode flags 掩码

上游 `fs/btrfs/tree-checker.c`：

```c
btrfs_inode_split_flags(btrfs_inode_flags(leaf, iitem), &flags, &ro_flags);
if (unlikely(flags & ~BTRFS_INODE_FLAG_MASK)) {
    inode_item_err(leaf, slot, "unknown incompat flags detected: 0x%x", flags);
    return -EUCLEAN;
}
```

`BTRFS_INODE_FLAG_MASK`（来自 `include/uapi/linux/btrfs_tree.h`）=

```
NODATASUM | NODATACOW | READONLY | NOCOMPRESS | PREALLOC | SYNC |
IMMUTABLE | APPEND | NODUMP | NOATIME | DIRSYNC | COMPRESS | ROOT_ITEM_INIT
= bit0..bit11 | bit31
= 0x80000FFF
```

取反得 `0x7FFFF000`，汇编是：

```asm
test $0x7ffff000, %eax
jne  <报错分支>
```

**这里有个大坑**：

```
0x7ffff000 = 0111 1111 1111 1111 1111 0000 0000 0000
              ↑
              bit30 = 1（0x40000000 已包含在内）
```

所以 `0x7ffff000` **本身就已经允许 bit30** —— 而群晖的私有 inode flag 正是 bit30。
但报错依然出现，说明这个 `test` 检查的是**除 bit30 之外**的位被置位的情况……

实际观测是：把掩码从 `0x7ffff000` 改成 `0x3ffff000`（**清除** bit30）后，
挂载才成功。原因是 `~BTRFS_INODE_FLAG_MASK` 在 C 里是 32 位无符号运算，
而编译器生成的立即数含义需要按实际反汇编确认。以实测定论：

**补丁**：`0x7ffff000` → `0x3ffff000`

```python
# ⚠ 必须用 & ~bit 清除，用 | bit 是空操作（因为 bit30 已经是 1）
NEW_INODE_MASK = ((~0x80000FFF) & 0xFFFFFFFF) & (~(1 << 30) & 0xFFFFFFFF)
# = 0x3ffff000
```

> 我第一次写工具时用了 `| (1 << 30)`，结果掩码没变（空操作），
> 检测逻辑一直误报"未打补丁"。这个 bug 排查了很久。

---

## 4. 为什么不能用上游源码重编译

飞牛的 `btrfs.ko` 是世界独有的定制版，带私有挂载选项：

```
Opt_trimacl                              # 挂载选项 trimacl
btrfs_xattr_trimacl_access_handler       # 对应的 xattr handler
```

飞牛系统盘 `/vol1` 就是用 `trimacl` 选项挂的：

```
/dev/mapper/trim_...-0 on /vol1 type btrfs (rw,relatime,ssd,discard=async,
  trimacl,space_cache=v2,subvolid=5,subvol=/)
```

上游内核**没有**这个选项，而且 `btrfs_parse_options()` 里有
`unrecognized mount option` 检查 —— 用上游模块挂 `/vol1` 会直接失败，
**系统起不来**。

而且飞牛不公开内核源码（仓库里只有 headers/image/modules）。

**所以唯一可行的路**：给飞牛自己的 `.ko` 打二进制补丁，保留 `trimacl`。

---

## 5. 为什么必须同步 initrd

这是最容易被忽略、也最致命的一步。

### initrd 里有什么

飞牛的 `/boot/initrd.img-<ver>` 是 **microcode cpio + zstd 压缩的 cpio** 两层结构：

```
offset 0         : layer0 = microcode cpio（newc 格式，未压缩，6 个条目）
offset 13126444  : padding（212 字节 0）
offset 13126656  : zstd 魔数 28 b5 2f fd → layer1（真正的根 fs + 内核模块）
```

`layer1` 里包含 `usr/lib/modules/<ver>/kernel/fs/btrfs/btrfs.ko` ——
**未压缩、未打补丁**。

### 它为什么能压过 /lib/modules

dracut 生成的 initrd 里有 `scripts/local-premount/btrfs` 这个 hook，
在 initramfs 阶段就会 `modprobe btrfs` 去扫描 btrfs 设备。此时加载的是
**initrd 里那份**。

而 Linux 的模块机制是**一个模块只能加载一次**：一旦 `btrfs` 已加载，
`/lib/modules` 里再新的版本也不会被加载（`modprobe` 直接返回"已在运行"）。

结果：`/lib/modules/.../btrfs.ko.xz` 打了补丁也**完全无效**。

### 解法

用 `ird_tool.py` 把 initrd 里那份也换成补丁版：

```python
# 解析 layer0 → 保留
# 解压 layer1（zstd）→ 替换 btrfs.ko → 重新压缩
# 拼接：layer0 + padding + 新 layer1
```

---

## 6. 为什么补丁模块要剥离签名

飞牛的 btrfs.ko 带 PKCS#7 签名（`sig_id: PKCS#7`，`signer: Build time
autogenerated kernel key`）。

内核**没有**启用 `CONFIG_MODULE_SIG_FORCE`（不强制签名），但有个坑：

> **带无效签名的模块会被内核拒绝加载**，报 `Key was rejected by service`。

因为签名覆盖了模块全文，改一个字节签名就失效了。内核检测到「有签名但验证失败」
→ 拒绝。

而**完全不带签名**的模块，内核只会在 `dmesg` 里打个 taint 标记，照常加载。

所以补丁后要截掉签名尾：

```python
marker = b"\x00\x00\x02\x00\x00\x00\x00\x00"   # module_signature 结构特征
idx = data.rfind(marker)
data = data[:idx]                              # 截断
```

（这个 8 字节特征值来自 `struct module_signature` 的固定前缀。）

---

## 7. 为什么不能用自建 systemd 挂载服务

我第一版方案是：写 `fnos-mount-volX.service`，`ExecStart=/bin/mount ...`。

**这个方案把系统搞挂过一次**（飞牛系统盘无法引导，最后重装了）。

原因：`/bin/mount` 是**同步阻塞调用**。如果开机时 md/LVM 还没就绪，
`mount` 会一直等待设备出现 —— 于是 systemd 卡在 `multi-user.target`，
整个系统（包括 SSH、Web）都起不来。

**正确做法**：交给飞牛的 `triminit`。它读 `mount` 表自己管理挂载，
时序问题它自己处理，我们只管把数据准备好。

---

## 8. 共享文件夹为什么进不了「我的文件」

飞牛有三个「看文件」的入口，数据来源不同：

| 入口 | 显示什么 |
|---|---|
| 系统设置 → 存储空间管理 | `mount` 表里的存储空间（卡片形式，带容量/类型） |
| 文件管理 → 管理员视角 → 设备全部文件 | 所有存储空间 + 各自的全部目录 |
| **文件管理 → 我的文件** | **只显示各卷 `/volN/1000/` 下的内容** |

`1000` 是飞牛的用户 UID 目录约定（`/volN/<uid>/`）。所以群晖的共享文件夹
（在卷根，如 `/volN/Data`）不会出现在「我的文件」里。

飞牛 `share_service` 里的 **TrimAFS** 组件会把 `/volN/1000/` 下每个目录
自动注册为共享文件夹：

```
[TrimAFS] [ftp] Need to create share folder '/volN/1000/Data', name 'Data'
```

所以只要 bind 挂载过去就行：

```
/volN/Data  ──bind──>  /volN/1000/Data
```

（`N` 是你的实际卷号，每台机器不同 —— 用 `mount | grep ' /vol'` 看。）

**必须用 bind 挂载而不是软链接** —— 飞牛的 UI 把软链接显示成一个坏掉的链接文件，
属主也不对。

### 启动时序坑

`share_service` 在系统启动时扫一次。如果那一刻群晖卷还没挂载完，
日志会报：

```
[webdav] AddUserFoldersByStorageId, path '/vol2/1000' not existed
```

修复：`systemctl restart share_service` 重新扫描（零风险）。

---

## 9. 飞牛升级后会发生什么

飞牛升级（尤其换内核）会动三样东西：

| 被动的文件 | 后果 |
|---|---|
| `/lib/modules/<新ver>/.../btrfs.ko.xz` | 换成未打补丁的原版 → 群晖卷挂不上 |
| `/boot/initrd.img-<新ver>` | 内嵌原版 btrfs.ko → 补丁被绕过 |
| `/lib/modules/` 下旧版本目录 | 变成死文件（磁盘上无对应 vmlinuz） |

**但不会变砖**：补丁只放宽群晖私有 flag 的校验，不影响标准 btrfs 功能。
飞牛系统盘 `/vol1` 用标准 flags，照常挂载。坏的只是群晖三卷。

自愈脚本（`patch_watchdog.sh`）会在开机时和每 30 分钟检查一次，
发现补丁丢失就自动重打 + 同步 initrd。

---

## 10. 关键常量速查

```
# root flags
ORIG  = 0xfffefffffffffffe      # 允许 bit0, bit32
NEW   = 0xfffeff01fffffffe      # 再放行 bit33~bit39
SYN   = 0xfe00000000            # bit33..bit39

# display（仅影响报错文案）
ORIG  = 0x1000000000001
NEW   = 0x100fe00000001

# inode flags
ORIG  = 0x7ffff000              # 已含 bit30
NEW   = 0x3ffff000              # 清除 bit30

# 飞牛挂载选项
rw,relatime,trimacl,space_cache=v2

# 群晖保留区（不是数据，12MB，可忽略）
syno_vg_reserved_area
```
