# 完整操作手册

从零开始，一步步把群晖硬盘接入飞牛 fnOS。每个阶段都有验证点，确认通过再往下走。

---

## 阶段 0：准备工作

### 0.1 确认硬件状态

群晖盘装进飞牛主机后，SSH 登录飞牛，切 root：

```bash
sudo -i
```

检查三层结构是否点亮：

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT
cat /proc/mdstat
pvs; vgs; lvs
```

**期望看到**（**盘符、容量、VG/LV 名都会不同，下面只是示意**）：

```
sdX                 1.8T
├─sdX1              2.4G  linux_raid_member
├─sdX2                2G  linux_raid_member
└─sdX5              1.8T  linux_raid_member
  └─mdN             1.8T  LVM2_member
    └─vg1-volume_1  1.8T  btrfs
```

关键特征（与具体名字无关）：
- 分区是 `linux_raid_member` 类型
- md 设备是 `LVM2_member`
- LVM 逻辑卷上是 `btrfs`

如果 `pvs/vgs/lvs` 什么都没有：

```bash
mdadm --assemble --scan
vgchange -ay
```

### 0.2 建立备份目录

```bash
mkdir -p /root/_stor_adopt_backup
```

---

## 阶段 1：内核 btrfs 补丁

### 1.1 先验证问题确实存在

```bash
# 把 vg1-volume_1 换成你实际的卷（先跑 pvs/vgs/lvs 确认）
mkdir -p /tmp/t && mount -o ro /dev/mapper/vg1-volume_1 /tmp/t
```

**期望**：报错

```
mount: /tmp/t: can't read superblock on /dev/mapper/vg1-volume_1.
```

```bash
dmesg | tail -20 | grep -i btrfs
```

**期望**：

```
BTRFS critical: invalid root flags, have 0x400000000 expect mask 0x1000000000001
```

看到这个就对了 —— 说明必须打补丁。如果直接挂上了，恭喜，你的内核可能已经兼容，
可以跳到阶段 2。

### 1.2 打补丁

```bash
# 复制 skill 脚本到 NAS（在你本地机器上执行）
scp scripts/*.py scripts/*.sh admin@<nas-ip>:/tmp/

# 以下在 NAS 上执行
cd /tmp
python3 gen_btrfs_patch.py --check --kernel "$(uname -r)"
# 期望输出: 未打补丁 ✗

python3 gen_btrfs_patch.py --kernel "$(uname -r)"
# 期望输出: 三处掩码的变更 + 已安装 + md5
```

**期望输出**（内核版本、md5、偏移都是示例）：

```
=== 内核 <你的内核版本> ===
    root_flags_mask    0xfffefffffffffffe -> 0xfffeff01fffffffe
    display_mask       0x1000000000001 -> 0x100fe00000001
    inode_flags_mask   0x7ffff000 -> 0x3ffff000
    已剥离 PKCS#7 签名尾 (@0x...)
    已备份原模块 -> /lib/modules/.../btrfs.ko.xz.orig-backup
    已安装 -> /lib/modules/.../btrfs.ko.xz
    md5=<新生成的 md5>
    已保存未压缩版 -> /usr/local/share/fnos-btrfs-patch/btrfs_patched_<内核>.ko

=== 汇总 ===
  <你的内核版本>    OK
```

要看的三点：
1. **三行掩码变更都在**（缺一行说明没定位到，后面会挂不上）
2. **`已剥离 PKCS#7 签名尾`** 出现（否则内核会拒绝加载）
3. 汇总里是 **OK** 不是 FAIL

### 1.3 同步 initrd（不能跳过！）

```bash
python3 ird_tool.py \
    --initrd /boot/initrd.img-$(uname -r) \
    --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko
```

**期望输出**（字节数、层偏移都会不同）：

```
initrd : /boot/initrd.img-<内核> (<大小> bytes)
目标模块: .../btrfs_patched_<内核>.ko (<大小> bytes, md5=...)
cpio 层 : 找到 N 个
  @0x0         <条目数> 条目
  [zstd 压缩层 @+0x...]
    → btrfs.ko: <大小> bytes, md5=<原 md5>
  检测到 zstd 压缩层 @+0x...（padding <字节数> 字节）
    zstd 内层: btrfs.ko 替换: ... 
    已重新压缩 (... -> ... bytes)
已备份原 initrd -> /boot/initrd.img-....orig-backup
已写入 ...
```

关键看 **`→ btrfs.ko:`** 这行 —— 能报出 md5 说明成功进到压缩层内部了。
如果只显示「cpio 层」而没有 zstd 层，说明 initrd 结构与预期不同（见
[troubleshooting A2](troubleshooting.md)）。

### 1.4 重启

```bash
reboot
```

> ⚠️ 必须重启。运行中的内核仍用旧模块；而且 `/vol1` 用 btrfs，热替换不掉。
>
> 补丁只放宽 flag 校验，**即使补丁有问题系统也能正常启动**，不用担心变砖。
> 万一真起不来，用 U 盘启动把 `.orig-backup` 覆盖回去即可。

### 1.5 验证补丁生效

```bash
mount -o ro /dev/mapper/vg1-volume_1 /tmp/t && ls /tmp/t && umount /tmp/t
dmesg | grep -c "BTRFS critical"     # 期望 0
```

看到群晖的共享文件夹就成功了。

---

## 阶段 2：注册为飞牛原生存储空间

### 2.1 扫描

```bash
cd /tmp
python3 adopt_synology.py --scan
```

**期望输出**（**盘符、容量、VG/LV 名都是示例，你的完全不同**）：

```
=== 可接入的群晖数据卷 ===
  vg1          volume_1     461.00g  /dev/mapper/vg1-volume_1
  vg2          volume_2     1.81t    /dev/mapper/vg2-volume_2
```

判断标准只有三条：**VG 名不是 `trim_` 开头**、**LV 名不是 `syno_vg_reserved_area`**、
**文件系统是 btrfs**。名称、数量、容量都不重要。

> 如果一个都没列出来，说明：① 阵列没组装、② LVM 没激活、或 ③ 卷不是 btrfs
> （ext4 卷本 skill 不支持）。先回 [阶段 0](#阶段-0准备工作) 检查。

### 2.2 演练

```bash
python3 adopt_synology.py --adopt --dry-run
```

会打印每个卷将要做的事，**不修改任何东西**。确认信息正确。

### 2.3 执行

```bash
python3 adopt_synology.py --adopt --yes
```

脚本会依次：

1. 备份 LVM 元数据（`vgcfgbackup`）和 mount 表（`pg_dump`）
2. 每个卷先只读试挂验证数据在
3. `vgrename` + `lvrename`（改名成 `trim_<uuid>/0`）
4. `btrfs subvolume set-default @syno`
5. 改名后再验证一次，失败自动回滚
6. 写 `mount` 表

**期望输出**（数值、UUID、卷号都是示例）：

```
── 处理 vg1/volume_1 (461.00g) ──
  只读试挂 OK：66 个条目
  顶层：@appconf, @appdata, <你的共享文件夹>, ...
  改名：vg1/volume_1 → trim_77500f3f_.../0
  改名完成
  默认子卷 → @syno (id=256)
  改名后验证 OK：66 个条目
  已注册 mount 表：id=2 uuid=trim_77500f3f_...-0
```

要点：
- 每个卷都会**新生成一个 UUID**（`uuidgen`），所以你看到的 `trim_xxx` 是随机的
- `id` 从现有 mount 表的最大 id + 1 开始，对应 `/vol<id>`
- 「只读试挂 OK」的条目数和顶层目录名，就是你群晖盘的真实内容

### 2.4 重启验证

```bash
reboot
```

重启后：

```bash
mount | grep ' /vol'
```

**期望**（卷号取决于你原有几个存储空间，下面只是示例）：

```
/dev/mapper/trim_77500f3f_...-0 on /vol2 type btrfs (rw,relatime,trimacl,...)
/dev/mapper/trim_98aca715_...-0 on /vol3 type btrfs (rw,relatime,trimacl,...)
/dev/mapper/trim_2b79ed94_...-0 on /vol4 type btrfs (rw,relatime,trimacl,...)
```

> 📌 **卷号怎么来的**：飞牛现有存储空间占 `/vol1`（系统盘），
> 所以你接入的第一个卷是 `/vol2`，以此类推。
> 如果原来已有 `/vol2`，你的群晖卷就从 `/vol3` 开始。

然后确认 UI：**系统设置 → 存储空间管理 → 存储空间**
应该能看到新增的存储空间，各带「正常」徽标、容量条、硬盘类型、文件系统信息。

---

## 阶段 3：共享文件夹挂进「我的文件」

此时在「文件管理 → 管理员视角 → 设备全部文件」里能看到各存储空间，
但**「我的文件」里看不到**。

### 3.1 看有哪些共享文件夹

```bash
python3 bind_shares.py --list
```

**期望**（目录名就是你群晖里的真实共享文件夹，`/volN` 的 N 以实际为准）：

```
=== 各存储空间可挂载的共享文件夹 ===
  [跳过] /vol1 是飞牛系统盘

/vol2  (/dev/mapper/trim_77500f3f-0)
  <共享文件夹 A>

/vol3  (/dev/mapper/trim_98aca715-0)
  <共享文件夹 B>
  <共享文件夹 C>
  ...
```

自动跳过：飞牛系统盘整卷、`homes`（会和飞牛用户目录冲突）、`docker`、
`@` 开头的系统目录、`1000`（飞牛个人空间本身）。

如果想挂 `homes` 里的某个用户目录，见
[troubleshooting C4](troubleshooting.md)。

### 3.2 生成清单

```bash
python3 bind_shares.py --generate
```

写到 `/root/_stor_adopt_backup/bindmounts.txt`，可手工编辑增删。

### 3.3 执行挂载

```bash
python3 bind_shares.py --apply
```

**期望**：

```
  ✅ /vol2/<A> -> /vol2/1000/<A>
  ✅ /vol3/<B> -> /vol3/1000/<B>
  ...
结果：成功 N，跳过 M，失败 0
正在刷新飞牛共享文件夹索引…
TrimAFS 已注册：
  /vol2/1000/<A>
  /vol3/1000/<B>
  ...
```

（数量取决于你群晖里有多少个共享文件夹。）

### 3.4 装开机自动恢复

```bash
python3 bind_shares.py --install-service
```

### 3.5 验证

浏览器打开飞牛 → 文件管理 → 我的文件，应该能看到这些文件夹，
「所在位置」列会显示「存储空间2/3/4」。

---

## 阶段 4：装升级自愈机制

```bash
python3 install.py --source-dir /tmp
```

或部署并立即给当前内核打补丁：

```bash
python3 install.py --source-dir /tmp --patch-now
```

验证：

```bash
python3 install.py --status
```

**期望**：

```
=== 部署状态 ===
  ✓ /usr/local/share/fnos-btrfs-patch
  ✓ .../gen_btrfs_patch.py
  ...
=== systemd ===
  fnos-btrfs-patch-guard.service          is-enabled   enabled
  fnos-btrfs-patch-guard.service          is-active    active
  fnos-btrfs-patch-watchdog.timer         is-enabled   enabled
  fnos-btrfs-patch-watchdog.timer         is-active    active
=== 当前内核补丁 ===
  <你的内核版本>: 已打补丁 ✓
```

---

## 阶段 5：最终验收

```bash
# 1. 存储空间都在
mount | grep ' /vol'

# 2. 群晖卷可读写（把 /volN/<某个共享文件夹> 换成你的真实路径）
touch /volN/<某个共享文件夹>/.wtest && rm /volN/<某个共享文件夹>/.wtest && echo "读写 OK"

# 3. zero btrfs 错误
dmesg | grep -c "BTRFS critical"

# 4. bind 挂载数
grep ' /vol' /proc/self/mountinfo | grep -c '/1000/'

# 5. 补丁状态
python3 /usr/local/share/fnos-btrfs-patch/gen_btrfs_patch.py \
    --check --kernel "$(uname -r)"

# 6. 服务状态
systemctl is-active fnos-btrfs-patch-guard.service fnos-synology-binds.service
```

全部通过后，**再重启一次**确认开机自动恢复：

```bash
reboot
# 等 1-2 分钟后
mount | grep ' /vol'
grep -c '/1000/' /proc/self/mountinfo
cat /var/run/fnos-btrfs-patch.status
```

---

## 附录 A：手工操作（脚本不可用时）

### A.1 手工打补丁

> ⚠️ **不要用固定偏移**（比如 `0xefbde`）—— 那是某一台机器、某个内核版本的地址，
> 换内核就完全不对，写错位置会**损坏模块导致系统起不来**。
>
> 正确做法是**按特征值扫描**。下面这段是自包含的，可独立运行：

```bash
MODDIR=/lib/modules/$(uname -r)/kernel/fs/btrfs
cp -a $MODDIR/btrfs.ko.xz $MODDIR/btrfs.ko.xz.orig-backup
xz -dc $MODDIR/btrfs.ko.xz > /tmp/b.ko

python3 - <<'EOF'
import struct, hashlib

d = bytearray(open('/tmp/b.ko', 'rb').read())

# 群晖私有 root flag: 实测 bit34/bit35/bit33+34 等，统一放行 bit33~39
SYN_ROOT = 0
for b in range(33, 40):
    SYN_ROOT |= (1 << b)

NEW = {
    0xfffefffffffffffe: (~((0x1000000000001) | SYN_ROOT)) & 0xFFFFFFFFFFFFFFFF,  # root flags
    0x1000000000001:    0x1000000000001 | SYN_ROOT,                              # display
    0x7FFFF000:         ((~0x80000FFF) & 0xFFFFFFFF) & (~(1 << 30) & 0xFFFFFFFF),# inode flags
}

changed = 0
for i in range(0x40, len(d) - 10):
    # movabs $imm64, %rXX  →  48/49 ba/b8 + 8 字节立即数
    if d[i] in (0x48, 0x49) and d[i+1] in (0xba, 0xb8):
        imm = int.from_bytes(d[i+2:i+10], 'little')
        if imm in NEW and imm != NEW[imm]:
            d[i+2:i+10] = NEW[imm].to_bytes(8, 'little')
            print("  movabs 0x%x -> 0x%x @0x%x" % (imm, NEW[imm], i))
            changed += 1
    # test $imm32, %eax  →  a9 + 4 字节立即数
    if d[i] == 0xa9:
        imm = struct.unpack_from('<I', d, i+1)[0]
        if imm in NEW and imm != NEW[imm]:
            struct.pack_into('<I', d, i+1, NEW[imm] & 0xFFFFFFFF)
            print("  test 0x%x -> 0x%x @0x%x" % (imm, NEW[imm], i))
            changed += 1

if changed == 0:
    raise SystemExit("✗ 没找到任何补丁点 —— 这个内核的 btrfs 结构与预期不符，"
                     "不要盲目打补丁，改用 gen_btrfs_patch.py 并人工分析")

# 剥离 PKCS#7 签名尾（带无效签名会被内核拒绝加载）
m = b'\x00\x00\x02\x00\x00\x00\x00\x00'
idx = d.rfind(m)
if idx > 0:
    d = d[:idx]
    print("  已剥离签名尾 @0x%x" % idx)

open('/tmp/b_patched.ko', 'wb').write(bytes(d))
print("  ✓ 共 %d 处，md5=%s" % (changed, hashlib.md5(bytes(d)).hexdigest()))
EOF

# 确认改对了再安装
test -f /tmp/b_patched.ko && xz -9 -c /tmp/b_patched.ko > $MODDIR/btrfs.ko.xz && depmod -a
```

要点：这段代码在**文件里直接搜字节模式**，与段地址、内核版本无关。
只要内核的 btrfs 里还是那三个掩码常量（上游定义的），就能定位到。

> 如果 `changed == 0`，说明新版内核的 btrfs 结构变了（比如掩码算法改写）。
> **此时不要盲目打补丁** —— 反汇编 `check_root_item` / `check_inode_item` 重新定位，
> 见 [troubleshooting E2](troubleshooting.md)。

### A.2 手工改 LVM 名 + 注册

> 下面的 `vg1` / `volume_1` 是示例，换成你 `--scan` 看到的实际名字。

```bash
# 备份
vgcfgbackup -f /root/_stor_adopt_backup/vg1.bak vg1
( cd /tmp && sudo -u postgres pg_dump -d trim -t mount --data-only --column-inserts ) \
  > /root/_stor_adopt_backup/mount_before.sql

# 改名
NEWVG="trim_$(uuidgen | tr 'A-Z-' 'a-z_')"
vgrename vg1 "$NEWVG"
lvrename "$NEWVG" volume_1 0
vgchange -ay "$NEWVG"

# 设默认子卷
mkdir -p /tmp/adopt
mount "/dev/mapper/${NEWVG}-0" /tmp/adopt
SID=$(btrfs subvolume list /tmp/adopt | awk '/path @syno$/{print $2}')
btrfs subvolume set-default "$SID" /tmp/adopt
umount /tmp/adopt

# 注册（id 必须是你 mount 表里下一个空闲号，对应 /vol<id>）
( cd /tmp && sudo -u postgres psql -d trim -c \
"SELECT id FROM mount ORDER BY id;" )        # 先看已用的 id
( cd /tmp && sudo -u postgres psql -d trim -c \
"INSERT INTO mount (id,uuid,comment,sync_speed_min,sync_speed_max,stripe_cache_size,warn_frsize,grow,state,pool_type)
 VALUES (2,'${NEWVG}-0','Synology vg1/volume_1',-1,-1,4096,10,'',0,0);" )
```

### A.3 手工 bind 挂载

> `/volN` 的 N 以实际为准（`mount | grep ' /vol'` 看）。

```bash
mkdir -p /volN/<共享文件夹名>/1000-placeholder 2>/dev/null
mkdir -p /volN/1000/<共享文件夹名>
mount --bind /volN/<共享文件夹名> /volN/1000/<共享文件夹名>
systemctl restart share_service
```

例如：

```bash
mkdir -p /vol2/1000/Data
mount --bind /vol2/Data /vol2/1000/Data
systemctl restart share_service
```

---

## 附录 B：回滚

### B.1 回滚内核补丁

```bash
MODDIR=/lib/modules/$(uname -r)/kernel/fs/btrfs
cp -a $MODDIR/btrfs.ko.xz.orig-backup $MODDIR/btrfs.ko.xz
cp -a /boot/initrd.img-$(uname -r).orig-backup /boot/initrd.img-$(uname -r)
depmod -a
reboot
```

回滚后飞牛完全正常，只是群晖卷挂不上。

### B.2 回滚 LVM 改名

```bash
# 用脚本
python3 adopt_synology.py --rollback trim_xxxx-0

# 或手工
( cd /tmp && sudo -u postgres psql -d trim -c "DELETE FROM mount WHERE uuid='trim_xxx-0';" )
lvrename trim_xxx 0 volume_1
vgrename trim_xxx vg1
```

### B.3 卸载 bind 挂载服务

```bash
systemctl disable --now fnos-synology-binds.service
rm /etc/systemd/system/fnos-synology-binds.service
rm /usr/local/sbin/fnos-synology-binds.sh
systemctl daemon-reload
```

---

## 附录 C：数据安全说明

整个流程**从未**执行：

- `mkfs` / `mkfs.btrfs` / `wipefs`
- `mdadm --stop` / `mdadm --zero-superblock`
- `dd` 写入数据区
- 任何写 btrfs 数据区的操作（`set-default` 只改超级块的一个字段）

真正的写操作只有：

| 操作 | 影响范围 |
|---|---|
| `vgrename` / `lvrename` | LVM 元数据（不是数据区） |
| `btrfs subvolume set-default` | 超级块里的默认子卷 ID 字段 |
| 写 `mount` 表 | 飞牛数据库 |
| `mount --bind` | 纯内存挂载，不落盘 |
| 内核模块补丁 | 只改 `/lib/modules` 和 `/boot` 的文件，不碰磁盘数据 |
