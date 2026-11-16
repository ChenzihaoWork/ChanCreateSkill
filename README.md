# ChanCreateSkill

个人 Skill 库 —— 收录自研的 WorkBuddy Agent Skills。

每个 Skill 放在一个独立的顶层目录里，目录名即 Skill 名（`name` 字段）。
点开任意目录下的 `README.md`，就能看到它的**优势、作用、大致实现方式**。

---

## 收录清单

| # | Skill | 分类 | 一句话说明 | 状态 |
|---|-------|------|-----------|------|
| 1 | [fnos-synology-adopt](./fnos-synology-adopt/) | system | 把群晖拆下来的硬盘**无损**接入飞牛 fnOS，成为原生存储空间 | ✅ 已收录 |

> 后续新增的 Skill 会在此表登记。

---

## Skill 索引

### 1. `fnos-synology-adopt` —— 群晖硬盘接入飞牛 fnOS

**作用**：解决「群晖硬盘插到飞牛上挂不上去」这个具体问题。
不格式化、不丢数据，让群晖的 btrfs 卷在飞牛上被识别为原生存储空间，
群晖原有的共享文件夹出现在飞牛的「我的文件」里，飞牛升级后还能自动恢复。

**核心优势**：

- **不重编译内核**。飞牛的 btrfs.ko 带私有挂载选项 `trimacl`，用上游源码重编译会导致系统盘 `/vol1` 挂不上、系统起不来。本 Skill 走二进制补丁路线，只放宽群晖私有 flag 的校验。
- **不会变砖**。补丁只放宽 flag 校验，不影响标准 btrfs 功能；飞牛系统盘用标准 flags，照常启动。升级换内核最坏结果只是「群晖盘暂时挂不上」。
- **自适应，不写死**。补丁点按**特征值扫描**定位而非硬编码偏移；initrd 支持 zstd/gzip/xz/lz4 多种压缩；mount 表 id 自动取空闲号；群晖 VG/LV 名称任意（按 `fstype=btrfs` 探测）。换一台 NAS、换一个内核版本都不用改代码。
- **探测不到就明确失败**，不猜着往下走。写错 mount 表或改错 LVM 名的代价远高于停一下问人。
- **全程可回滚**。先只读验证 → dry-run 演练 → 危险操作前自动备份 LVM 元数据与数据库 → 改名后只读试挂验证，失败自动回滚。
- **升级自愈**。内置 systemd 服务 + 定时检查，飞牛升级换内核后自动重新打补丁并同步 initrd。

**大致实现方式**：

```
群晖硬盘
  └─ 分区 (linux_raid_member)
      └─ mdadm 阵列
          └─ LVM (vg1/volume_1)
              └─ btrfs (带群晖私有 flag)
                  ↓ ① 打补丁 + ② 同步 initrd 内嵌模块
              ↓ ③ LVM 改名为 trim_<uuid>/0 + 设默认子卷 + 写 mount 表
          飞牛原生存储空间 (/vol2, /vol3 ...)
              ↓ ④ 共享文件夹 bind 挂载到 /volN/1000/
          飞牛「我的文件」中可见
```

关键技术动作：

| 步骤 | 脚本 | 做什么 |
|------|------|--------|
| ① | `gen_btrfs_patch.py` | 给飞牛自带 btrfs.ko 打二进制补丁，放宽三处 root/inode flag 掩码 |
| ② | `ird_tool.py` | 替换 initrd 内嵌的 btrfs.ko —— **只换 `/lib/modules` 是无效的**，initrd 会抢先加载原版 |
| ③ | `adopt_synology.py` | 扫描卷 → LVM 改名 `trim_<uuid>/0` → 设默认子卷 `@syno` → 往 PostgreSQL `trim` 库的 `mount` 表插行 |
| ④ | `bind_shares.py` | 把共享文件夹 bind 挂到 `/volN/1000/`，飞牛的 TrimAFS 会自动注册为共享文件夹（**不能用软链接**） |
| ⑤ | `patch_watchdog.sh` + `install.py` | 部署 systemd 服务与定时检查，实现升级后自愈 |

**目录结构**：

```
fnos-synology-adopt/
├── README.md              ← 说明文档（优势 / 作用 / 实现方式）
├── SKILL.md               ← Skill 定义与完整操作手册
├── scripts/               ← 8 个可执行脚本
└── references/            ← 原理详解 / 操作手册 / 故障排查
```

**适用边界**：

| 群晖存储类型 | 支持度 |
|---|---|
| Basic（单盘）、RAID 1 | ✅ 脚本直接可用 |
| RAID 5/6、SHR、JBOD | ⚠️ 需手工确认 mdadm/PV 后按文档执行 |
| ext4 卷、加密共享文件夹、iSCSI LUN | ❌ 不支持 |

---

## 目录约定

```
<repo-root>/
├── README.md                     ← 本索引
└── <skill-name>/
    ├── README.md                 ← Skill 说明文档（优势 / 作用 / 实现方式）
    ├── SKILL.md                  ← Skill 定义（frontmatter + 使用说明）
    ├── scripts/                  ← 可执行脚本
    └── references/               ← 深度参考文档
```

## License

MIT
