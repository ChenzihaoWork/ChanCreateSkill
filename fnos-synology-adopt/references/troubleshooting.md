# 故障排查

按现象索引。每条都给「怎么确认」和「怎么修」。

---

## A. 挂载阶段

### A1. `mount: can't read superblock`

```
mount: /tmp/t: can't read superblock on /dev/mapper/vg1-volume_1.
```

**确认**：

```bash
dmesg | tail -20 | grep -i btrfs
```

如果看到：

```
BTRFS critical: invalid root flags, have 0x400000000 expect mask 0x1000000000001
```

→ 内核 btrfs 模块未打群晖补丁。

**修**：

```bash
python3 gen_btrfs_patch.py --kernel "$(uname -r)"
python3 ird_tool.py --initrd /boot/initrd.img-$(uname -r) \
    --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko
reboot
```

---

### A2. 打完补丁、重启后仍报 `invalid root flags`，且 mask 没变

**确认**：

```bash
# 看错误里的 mask 是原始值还是补丁后的值
dmesg | grep "expect mask"
# 原始: expect mask 0x1000000000001      ← 补丁未生效
# 补丁: expect mask 0x100fe00000001      ← 补丁已生效
```

如果还是 `0x1000000000001`：

**原因**：initrd 里那份原版 btrfs.ko 抢先加载了。

**确认**：

```bash
python3 ird_tool.py --initrd /boot/initrd.img-$(uname -r) --inspect
# 看内嵌 btrfs.ko 的 md5 是否等于补丁版
```

**修**：同步 initrd 后重启。

```bash
python3 ird_tool.py --initrd /boot/initrd.img-$(uname -r) \
    --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko
reboot
```

---

### A3. `Key was rejected by service`

```
insmod: ERROR: could not insert module btrfs.ko: Key was rejected by service
```

**原因**：模块带**无效签名**（改了内容，签名对不上），内核拒绝加载。

**注意**：内核没启用 `CONFIG_MODULE_SIG_FORCE`（不强制签名），
但「有签名且验证失败」和「无签名」处理不同 —— 前者拒绝，后者只 taint。

**确认**：

```bash
modinfo /lib/modules/$(uname -r)/kernel/fs/btrfs/btrfs.ko.xz | grep sig_id
```

**修**：用脚本重新生成（会自动剥离签名尾）。

```bash
python3 gen_btrfs_patch.py --kernel "$(uname -r)"
```

手工剥离：

```python
marker = b"\x00\x00\x02\x00\x00\x00\x00\x00"
idx = data.rfind(marker)
data = data[:idx]
```

---

### A4. `Module btrfs is in use` / `umount: target is busy`

**原因**：`/vol1`（飞牛系统盘）还挂着，或有进程持有。

**确认谁持有**：

```bash
fuser -vm /vol1
# 或
for p in /proc/[0-9]*; do
  ls -l $p/fd 2>/dev/null | grep -q "/vol1" && echo "PID $(basename $p) $(cat $p/comm)"
done
```

常见持有者：`trim_file_monitor`、`nfsd`（NFS 服务）、`smbd`。

**修**：

```bash
systemctl stop trim_file_monitor.service
systemctl stop nfs-server rpcbind nfs-mountd
pkill -9 -f trim_file_monitor
umount /vol1
rmmod btrfs
```

> ⚠️ **不要用 `umount -l`（lazy）** —— 内核认为挂载还在，`rmmod` 会继续失败。

> 但通常**不需要热卸载模块**。改完模块文件直接 `reboot` 更简单更安全。

---

### A5. `unknown incompat flags detected: 0x40000000`

**原因**：inode flags 补丁没生效（第三处补丁）。

**确认**：

```bash
python3 gen_btrfs_patch.py --check --kernel "$(uname -r)"
# 看 inode_flags_mask 是否为 0x3ffff000
```

**修**：同 A1。

---

### A6. 某个卷能挂、另一个挂不上

**典型现象**：几个卷里有的能挂，某个挂不上，报 `have 0x<不同值>`。

**原因**：不同卷用的私有 flag 位不同（取决于 DSM 版本、是否开过快照、
是否装了特定套件）。

**确认实际值**：

```bash
mount -o ro /dev/mapper/<挂不上的卷> /tmp/t 2>&1
dmesg | tail -3 | grep -o "have 0x[0-9a-f]*"
```

**修**：确认补丁放行了整个 bit33~bit39 区间。

```bash
python3 gen_btrfs_patch.py --check --kernel "$(uname -r)"
# root_flags_mask 应为 0xfffeff01fffffffe（= ~(bit0|bit32|bit33..39)）
```

> 算 bit 位时注意：`0x400000000` 是 **bit34** 不是 bit33。
> 万一你遇到的位超出 bit33~39，需要把 `gen_btrfs_patch.py` 里的
> `SYN_ROOT_BITS` 范围调大（改 `range(33, 40)`）。

---

## B. 存储空间注册阶段

### B1. 存储空间管理里看不到新卷

**确认**：

```bash
# 1. 卷挂上了吗
mount | grep ' /vol'

# 2. mount 表有记录吗
( cd /tmp && sudo -u postgres psql -d trim -c "SELECT * FROM mount ORDER BY id;" )

# 3. LV 名对不对
lvs | grep trim_
```

**排查表**：

| 检查项 | 期望 | 不符合的修法 |
|---|---|---|
| 卷已挂载 | `/volN` 存在 | 重启或手工 mount |
| mount 表有行 | `id` + `uuid` 对应 | `adopt_synology.py --adopt` |
| VG 名 | `trim_<uuid>` | `vgrename` |
| LV 名 | `0` | `lvrename` |

---

### B2. 存储空间显示但容量/状态异常

**确认**：

```bash
btrfs filesystem show /dev/mapper/trim_xxx-0
dmesg | grep -i btrfs | tail
```

如果 dmesg 有 btrfs 错误 → 回到 A 节。

---

### B3. `/volN` 下多了一层 `@syno`

**现象**：打开 `/volN` 看到的是一层 `@syno/`，要再点进去才是共享文件夹
（如 `@syno/Data` 而不是直接 `Data`）。

**原因**：默认子卷没设成 `@syno`。

**修**：

```bash
# 把 trim_xxx-0 和 /volN 换成你的实际值
mkdir -p /tmp/adopt
mount /dev/mapper/trim_xxx-0 /tmp/adopt
SID=$(btrfs subvolume list /tmp/adopt | awk '/path @syno$/{print $2}')
btrfs subvolume set-default "$SID" /tmp/adopt
umount /tmp/adopt
# 重新挂载生效
umount /volN && mount -o rw,noatime,trimacl,space_cache=v2 /dev/mapper/trim_xxx-0 /volN
```

> 注：部分群晖卷的顶层子卷不叫 `@syno` 而是别的名字（如 `@` 或直接用 root）。
> 先 `btrfs subvolume list` 看清楚哪个包含你的共享文件夹，再 set-default。

---

### B4. 飞牛日志刷 `unable to find record for ...-syno_vg_reserved_area`

```
[E] unable to find record for /dev/dm-2 uuid:trim_xxx-syno_vg_reserved_area
```

**这是正常噪音，不用管。**

`syno_vg_reserved_area` 是群晖的 12MB LVM 保留区（不是文件系统，挂不上）。
飞牛遍历 VG 下所有 LV，遇到它找不到 mount 表记录就会报这个。

不影响任何功能。

---

### B5. 卷号错乱（两个卷抢同一个 `/volN`）

**现象**：`/vol2` 挂的不是你以为的那个卷（比如大容量卷占用了小号）。

**原因**：飞牛按 **dm 设备号**顺序分配 vol 号，不是按 mount 表 id。
如果 LVM 激活顺序变了就可能错位。

**确认**：

```bash
grep " /vol" /proc/self/mountinfo | awk '{print $5, $NF}'
lvs -o vg_name,lv_name,lv_size
```

**修**：调换 mount 表的 id，让 id 顺序与实际 dm 号顺序一致。

```bash
( cd /tmp && sudo -u postgres psql -d trim -c \
  "UPDATE mount SET id=99 WHERE id=2; UPDATE mount SET id=2 WHERE id=3; UPDATE mount SET id=3 WHERE id=99;" )
reboot
```

或直接手工挂载到正确位置后，只保留正确的 mount 表记录。

---

## C. 共享文件夹阶段

### C1. 「我的文件」里看不到群晖的共享文件夹

**确认**：

```bash
# 1. bind 挂载在不在
grep ' /vol' /proc/self/mountinfo | grep '/1000/'

# 2. 目录内容对不对
ls /vol3/1000/
```

**修**：

```bash
python3 bind_shares.py --generate
python3 bind_shares.py --apply
```

**原理**：「我的文件」只显示 `/volN/1000/` 下的内容，
飞牛的 TrimAFS 会把其中每个目录自动注册为共享文件夹。

---

### C2. bind 挂载后「我的文件」还是空的

**确认**：

```bash
systemctl status share_service
grep "Need to create share folder" /usr/trim/logs/share_service.log | tail -20
grep "not existed" /usr/trim/logs/share_service.log | tail -10
```

如果看到：

```
[webdav] AddUserFoldersByStorageId, path '/vol3/1000' not existed
```

→ `share_service` 启动时这些目录还不存在（时序问题）。

**修**：重启 share_service 让它重新扫描。

```bash
systemctl restart share_service
```

长时间看日志确认。

---

### C3. 软链接不生效

**现象**：用 `ln -s` 建的链接在飞牛 UI 里显示成一个坏掉的文件，
或根本看不到。

**原因**：飞牛 UI 只认真实目录或 bind 挂载，**不认软链接**。

**修**：改用 bind 挂载。

```bash
# 换成你的实际路径
rm /volN/1000/<共享文件夹名>                    # 删掉软链
mkdir -p /volN/1000/<共享文件夹名>
mount --bind /volN/<共享文件夹名> /volN/1000/<共享文件夹名>
systemctl restart share_service
```

---

### C4. 想挂 `homes`（群晖用户家目录）

**不要直接挂**。`/volN/homes/` 下是群晖各用户的家目录，
和飞牛的 `/volN/1000/` 机制冲突，会造成权限混乱。

**正确做法**：如果只是想让某个群晖用户的家目录可见，单独 bind 一个：

```bash
# <群晖用户名> 换成实际的（ls /volN/homes 看）
mkdir -p /volN/1000/<群晖用户名>-家目录
mount --bind /volN/homes/<群晖用户名> /volN/1000/<群晖用户名>-家目录
```

注意群晖和飞牛的同名用户 UID 可能不同
（比如群晖里 `zhangsan`=1026，飞牛里 `zhangsan`=1000），
文件属主会显示成数字。这不影响读写（飞牛用 trimacl 管权限），
但如果要纠正属主，用 `chown -R <飞牛uid>:<飞牛gid> /volN/homes/<用户>/`。

---

## D. 开机自动恢复阶段

### D1. 重启后 bind 挂载全丢了

**确认**：

```bash
systemctl is-active fnos-synology-binds.service
systemctl is-enabled fnos-synology-binds.service
tail -30 /var/log/fnos-synology-binds.log
```

**常见原因 1**：服务挂在 `multi-user.target`

```bash
systemctl show fnos-synology-binds.service -p WantedBy
# 期望: WantedBy=graphical.target
# 若显示 multi-user.target 或为空 → 错误
```

**修**：改成 `WantedBy=graphical.target`。

```bash
sed -i 's/^WantedBy=.*/WantedBy=graphical.target/' \
    /etc/systemd/system/fnos-synology-binds.service
systemctl daemon-reload
systemctl enable fnos-synology-binds.service
```

> 飞牛的 `multi-user.target` **永远是 inactive**（它走 graphical.target），
> 挂上去服务永远不触发。

---

### D2. 服务 enabled 但就是 never run

**确认**：

```bash
journalctl -u fnos-synology-binds.service -b --no-pager
# 如果什么都没有，说明根本没被触发
systemctl show fnos-synology-binds.service -p WantedBy -p After
```

**常见原因 2**：写了 `After=trim_main.service`

```bash
systemctl show fnos-synology-binds.service -p WantedBy
# 若结果里出现 trim_main.service → 就是这个问题
```

**原因解释**：`trim_main` 是 `Type=simple` + `Restart=always` 的**常驻守护进程**。
systemd 认为它永远不会「进入启动完成状态」，于是所有 `After=trim_main.service`
的 oneshot 服务被**无限推迟、永不执行**。

**修**：删掉 `After=trim_main.service` 和 `Wants=trim_main.service`。

```bash
sed -i '/^After=trim_main/d; /^Wants=trim_main/d' \
    /etc/systemd/system/fnos-synology-binds.service
systemctl daemon-reload
systemctl restart fnos-synology-binds.service
```

脚本内部已经有「等源目录就绪（最多 120 秒）」的逻辑，不需要 systemd 层面排序。

---

### D3. `Unknown key 'TimeoutStartSec' in section [Unit]`

日志里出现：

```
/etc/systemd/system/xxx.service:7: Unknown key 'TimeoutStartSec' in section [Unit], ignoring.
```

**原因**：`TimeoutStartSec` 是 `[Service]` 段的键，写到 `[Unit]` 会被忽略。

**修**：

```bash
sed -i '/^TimeoutStartSec/d' /etc/systemd/system/xxx.service
sed -i '/^\[Service\]/a TimeoutStartSec=300' /etc/systemd/system/xxx.service
systemctl daemon-reload
```

---

## E. 升级后阶段

### E1. 升级飞牛后群晖卷挂不上了

**确认**：

```bash
cat /var/run/fnos-btrfs-patch.status
tail -40 /var/log/fnos-btrfs-patch.log
python3 /usr/local/share/fnos-btrfs-patch/gen_btrfs_patch.py --check --kernel "$(uname -r)"
```

**修**：

```bash
systemctl start fnos-btrfs-patch-guard.service
# 看日志确认
tail -40 /var/log/fnos-btrfs-patch.log
# 如果提示 need_reboot=yes
reboot
```

---

### E2. 自愈脚本报「未能定位到可补丁的掩码」

```
✗ 未能定位到可补丁的掩码。该内核的 btrfs 内部结构可能已改变，需人工分析
```

**含义**：新版内核的 btrfs 代码结构变了，三处特征值（`0xfffefffffffffffe` /
`0x1000000000001` / `0x7ffff000`）在编译后不再是这三个立即数。

**这不是紧急故障** —— 飞牛系统本身完全正常，只是群晖卷挂不上。

**处理思路**：

1. 反汇编新模块，找 `check_root_item` 里的掩码

```bash
xz -dc /lib/modules/$(uname -r)/kernel/fs/btrfs/btrfs.ko.xz > /tmp/b.ko
objdump -d /tmp/b.ko > /tmp/b.dis
grep -n -B2 -A2 "check_root_item" /tmp/b.dis | head -40
# 找 movabs + test + jne 的组合
```

2. 找 `check_inode_item` 里的 `test $imm32,%eax`

```bash
grep -nE "test.*0x7ffff0|test.*0x[0-9a-f]*f000" /tmp/b.dis | head
```

3. 对照上游源码确认语义

```bash
# 本地 headers 里有定义
grep -A16 "BTRFS_INODE_FLAG_MASK" \
    /usr/src/linux-headers-$(uname -r)/include/uapi/linux/btrfs_tree.h
```

4. 把新找到的特征值加到 `gen_btrfs_patch.py` 的 `ORIG_TO_KIND` 字典里

**临时绕行**：回滚到旧内核，或暂时不升级。

---

### E3. 升级后飞牛起不来了

**先冷静**：补丁只放宽 flag 校验，**理论上不会导致启动失败**。
起不来更可能是升级本身的问题。

**确认**：接显示器看启动卡在哪一步。

**救援**：

1. 如果卡在内核启动 → grub 菜单选旧内核（按 `Esc` 或长按 `Shift`）
2. 如果卡在 systemd → `Ctrl+Alt+F2` 切 tty，登录后：

```bash
# 回滚补丁
MODDIR=/lib/modules/$(uname -r)/kernel/fs/btrfs
cp -a $MODDIR/btrfs.ko.xz.orig-backup $MODDIR/btrfs.ko.xz
cp -a /boot/initrd.img-$(uname -r).orig-backup /boot/initrd.img-$(uname -r)
# 停掉自建服务
systemctl disable --now fnos-synology-binds.service fnos-btrfs-patch-guard.service
reboot
```

3. 完全进不去 → U 盘启动 Live 系统，挂载系统盘后操作

---

### E4. 升级后 bind 挂载丢了

自愈脚本会处理，但可能要等最多 30 分钟（定时器周期）。
立即恢复：

```bash
systemctl start fnos-synology-binds.service
tail -20 /var/log/fnos-synology-binds.log
```

---

## F. 排查命令速查

```bash
# ── 状态总览 ──
cat /var/run/fnos-btrfs-patch.status

# ── 挂载 ──
mount | grep ' /vol'
grep ' /vol' /proc/self/mountinfo | grep '/1000/' | wc -l

# ── LVM ──
lvs -o vg_name,lv_name,lv_size,lv_attr
pvs; vgs

# ── 数据库 ──
( cd /tmp && sudo -u postgres psql -d trim -c \
  "SELECT id,uuid,comment FROM mount ORDER BY id;" )

# ── 内核模块 ──
modinfo btrfs | grep -E "filename|vermagic|sig_id"
lsmod | grep btrfs
dmesg | grep -i "BTRFS critical" | tail

# ── 补丁 ──
python3 /usr/local/share/fnos-btrfs-patch/gen_btrfs_patch.py \
    --check --kernel "$(uname -r)"

# ── 服务 ──
for s in fnos-btrfs-patch-guard.service fnos-synology-binds.service \
         fnos-btrfs-patch-watchdog.timer; do
  printf "%-40s %s / %s\n" "$s" \
    "$(systemctl is-enabled $s 2>&1)" "$(systemctl is-active $s 2>&1)"
done

# ── 日志 ──
tail -30 /var/log/fnos-btrfs-patch.log
tail -30 /var/log/fnos-synology-binds.log
grep -E "not existed|Need to create share folder" \
    /usr/trim/logs/share_service.log | tail -20

# ── 飞牛日志 ──
tail -20 /usr/trim/logs/triminit.log
```
