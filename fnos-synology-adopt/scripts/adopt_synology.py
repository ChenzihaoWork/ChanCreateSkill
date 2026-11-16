#!/usr/bin/env python3
"""
群晖(Synology)硬盘接入飞牛 fnOS —— 主编排脚本

把群晖 Basic 卷（分区 → mdadm → LVM → btrfs）无损接入飞牛，注册为
「原生存储空间」，在飞牛 UI 里可见、开机自动挂载。

接入的本质
----------
飞牛的存储空间约定：
  * LVM 卷组名  trim_<uuid>
  * 逻辑卷名    0
  * 设备路径    /dev/mapper/trim_<uuid>-0
  * PostgreSQL 库 trim 的 mount 表里有一行记录（id 对应 /volN）

所以「接入」= 把群晖那套 LVM 改成飞牛命名 + 往 mount 表补一行，
剩下的交给飞牛的 triminit 自己挂载（不要自建 systemd 挂载服务！）

前置条件（必须先满足，否则卷一挂不上）
--------------------------------------
内核 btrfs 模块必须已打群晖兼容补丁（见 gen_btrfs_patch.py + ird_tool.py）。
未打补丁时挂载会报：
    BTRFS critical: invalid root flags, have 0x400000000 expect mask 0x1000000000001

用法
----
  # 1) 先看有哪些可接入的群晖卷（只读，安全）
  python3 adopt_synology.py --scan

  # 2) 演练：只打印将要执行的操作，不改任何东西
  python3 adopt_synology.py --adopt --dry-run

  # 3) 真正执行
  python3 adopt_synology.py --adopt --yes

  # 4) 只读试挂某个卷，确认数据在
  python3 adopt_synology.py --mount-check vg1/volume_1

  # 5) 回滚某个卷的改名
  python3 adopt_synology.py --rollback trim_xxxx-0

安全设计
--------
* 默认 --dry-run，必须显式 --yes 才真正写入
* 所有危险操作前自动备份 LVM 元数据和 mount 表
* 改名后立刻只读试挂验证，失败自动回滚
* 绝不执行 mkfs / mdadm --stop / 任何写数据区的操作
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

BACKUP_DIR = "/root/_stor_adopt_backup"
STATE_FILE = os.path.join(BACKUP_DIR, "adopt_state.json")

# mount 表必需列（不同飞牛版本可能增减，缺关键列会提前报错而不是静默失败）
REQUIRED_MOUNT_COLS = {"id", "uuid", "pool_type"}


# ---------------------------------------------------------------- 工具

def run(cmd, check=False, timeout=120):
    """执行命令，返回 (rc, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("命令失败: %s\n%s" % (cmd, r.stderr))
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "超时"


def log(msg=""):
    print(msg, flush=True)


def psql(sql, db="trim"):
    """执行 SQL，返回 ([(row), ...], err)。自动适配有无 sudo 的环境。"""
    base = ["psql", "-d", db, "-P", "pager=off", "-t", "-A", "-F", "\t", "-c", sql]
    # 优先 sudo -u postgres；没有 sudo 就用 su postgres -c
    if shutil.which("sudo"):
        cmd = ["sudo", "-n", "-u", "postgres"] + base
        rc, out, err = run(cmd)
        if rc == 0 or "not allowed" not in (err or ""):
            if rc != 0 and err:
                return None, err
            rows = [line.split("\t") for line in out.splitlines() if line.strip()]
            return rows, None
    # 回退：su postgres
    quoted = " ".join(shlex.quote(x) if " " in x else x for x in base)
    rc, out, err = run(["su", "-", "postgres", "-c", quoted])
    if rc != 0:
        return None, err or "psql 执行失败"
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    return rows, None


def is_root():
    return os.geteuid() == 0


# ---------------------------------------------------------------- 探测

def list_vgs():
    """列出所有 VG 及其实用信息"""
    rc, out, _ = run(["vgs", "--noheadings", "-o",
                      "vg_name,vg_size,vg_free,pv_name"],)
    vgs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            vgs[parts[0]] = {"size": parts[1], "free": parts[2], "pvs": []}
    # 补 PV
    rc, out, _ = run(["pvs", "--noheadings", "-o", "pv_name,vg_name"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in vgs:
            vgs[parts[1]]["pvs"].append(parts[0])
    return vgs


def list_lvs(vg=None):
    """列出 LV，返回 [{vg, lv, size, attr, fstype, dev}, ...]"""
    cmd = ["lvs", "--noheadings", "-o", "vg_name,lv_name,lv_size,lv_attr"]
    if vg:
        cmd.append(vg)
    rc, out, _ = run(cmd)
    lvs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        v, lv, size, attr = parts[0], parts[1], parts[2], parts[3]
        dev = "/dev/mapper/%s-%s" % (v.replace("-", "--"), lv.replace("-", "--"))
        fstype = ""
        rc2, o2, _ = run(["blkid", "-o", "value", "-s", "TYPE", dev])
        if rc2 == 0:
            fstype = o2
        lvs.append({"vg": v, "lv": lv, "size": size, "attr": attr,
                    "fstype": fstype, "dev": dev})
    return lvs


def scan_synology_candidates():
    """
    找出「未被飞牛纳管」的群晖数据卷：
      - 在 LVM 上
      - 文件系统是 btrfs
      - VG 名不是 trim_ 开头（未纳管）
      - LV 名不是 syno_vg_reserved_area（群晖的 12MB 保留区）
    """
    cands = []
    for lv in list_lvs():
        if lv["vg"].startswith("trim_"):
            continue
        if lv["lv"] == "syno_vg_reserved_area":
            continue
        if lv["fstype"] != "btrfs":
            continue
        cands.append(lv)
    return cands


def existing_mount_vols():
    """读取 mount 表，返回 {uuid: id}"""
    rows, err = psql("SELECT id, uuid FROM mount ORDER BY id;")
    if rows is None:
        return {}, err
    return {r[1]: int(r[0]) for r in rows}, None


def free_vol_id(taken):
    n = 1
    while n in taken:
        n += 1
    return n


# ---------------------------------------------------------------- 版本兼容

def check_fnos_compat():
    """
    检查当前飞牛环境是否符合本脚本的假设。
    返回 (ok, [警告/错误信息])。
    目的是「不兼容就明确报错」，而不是静默写坏数据库。
    """
    msgs = []

    # 1. 是不是飞牛
    if not os.path.exists("/usr/trim/bin/triminit"):
        msgs.append("✗ 未检测到 /usr/trim/bin/triminit —— 这似乎不是飞牛 fnOS 系统")

    # 2. mount 表存在且列齐全
    rows, err = psql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='mount' AND table_schema='public';")
    if rows is None:
        msgs.append("✗ 无法查询 PostgreSQL `trim` 库的 mount 表：%s" % (err or "未知错误"))
        msgs.append("  → 确认 PostgreSQL 已启动：systemctl status postgresql")
    else:
        cols = {r[0] for r in rows if r}
        missing = REQUIRED_MOUNT_COLS - cols
        if missing:
            msgs.append("✗ mount 表缺少必需列：%s" % ", ".join(sorted(missing)))
            msgs.append("  → 当前飞牛版本的数据库结构与脚本假设不符，请人工确认")
        else:
            extra = cols - REQUIRED_MOUNT_COLS - {
                "comment", "sync_speed_min", "sync_speed_max",
                "stripe_cache_size", "warn_frsize", "grow", "state"}
            if extra:
                msgs.append("! mount 表有脚本未使用的列（通常无影响）：%s"
                            % ", ".join(sorted(extra)))

    # 3. 必需命令
    for tool in ("vgrename", "lvrename", "vgchange", "mount", "umount", "blkid"):
        if not shutil.which(tool):
            msgs.append("✗ 缺少命令：%s" % tool)

    # 4. share_service（用于刷共享文件夹索引，缺失只警告）
    if not os.path.exists("/usr/lib/systemd/system/share_service.service") and \
       not os.path.exists("/etc/systemd/system/share_service.service"):
        msgs.append("! 未找到 share_service —— 共享文件夹自动注册可能不生效，"
                    "挂载后需手动重启相关服务")

    fatal = any(m.startswith("✗") for m in msgs)
    return (not fatal), msgs


# ---------------------------------------------------------------- 验证

def try_mount_ro(dev, mnt):
    """只读试挂，成功返回 (True, 条目数, 顶层名列表)"""
    os.makedirs(mnt, exist_ok=True)
    rc, _, err = run(["mount", "-o", "ro", dev, mnt])
    if rc != 0:
        return False, 0, [], err
    try:
        names = sorted(os.listdir(mnt))
        return True, len(names), names[:10], None
    finally:
        run(["umount", mnt])


def get_syno_subvol_id(dev):
    """取 @syno 子卷的 id"""
    tmp = "/tmp/_adopt_subvol_probe"
    os.makedirs(tmp, exist_ok=True)
    rc, _, _ = run(["mount", dev, tmp])
    if rc != 0:
        return None
    try:
        rc, out, _ = run(["btrfs", "subvolume", "list", tmp])
        for line in out.splitlines():
            m = re.search(r"ID\s+(\d+).*path\s+@syno$", line)
            if m:
                return int(m.group(1))
        return None
    finally:
        run(["umount", tmp])


# ---------------------------------------------------------------- 备份

def backup_all():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    done = []

    for vg in list_vgs():
        if vg.startswith("trim_"):
            continue
        path = os.path.join(BACKUP_DIR, "%s.bak" % vg)
        if not os.path.exists(path):
            rc, _, err = run(["vgcfgbackup", "-f", path, vg])
            if rc == 0:
                done.append(path)

    path = os.path.join(BACKUP_DIR, "mount_%s.sql" % ts)
    with open(path, "w") as f:
        r = subprocess.run(["sudo", "-u", "postgres", "pg_dump",
                            "-d", "trim", "-t", "mount",
                            "--data-only", "--column-inserts"],
                           capture_output=True, text=True, cwd="/tmp")
        f.write(r.stdout)
    done.append(path)
    return done


# ---------------------------------------------------------------- 核心操作

def adopt_one(lv, vol_id, dry_run=False):
    """接入单个卷，返回 (ok, msg)"""
    old_vg, old_lv = lv["vg"], lv["lv"]
    dev = lv["dev"]

    log("")
    log("── 处理 %s/%s (%s) ──" % (old_vg, old_lv, lv["size"]))

    # 1) 只读试挂，确认数据在
    ok, n, names, err = try_mount_ro(dev, "/tmp/_adopt_check")
    if not ok:
        return False, ("只读挂载失败: %s\n"
                       "  → 若报 'invalid root flags'，说明内核 btrfs 模块未打群晖补丁，"
                       "请先运行 gen_btrfs_patch.py 并同步 initrd" % err)
    log("  只读试挂 OK：%d 个条目" % n)
    if names:
        log("  顶层：%s" % ", ".join(names[:8]))

    # 2) 生成飞牛风格 VG 名
    import uuid
    new_uuid = str(uuid.uuid4())
    new_vg = "trim_" + new_uuid.replace("-", "_")
    new_dev = "/dev/mapper/%s-0" % new_vg.replace("-", "--")
    log("  改名：%s/%s → %s/0" % (old_vg, old_lv, new_vg))

    if dry_run:
        log("  [dry-run] 跳过实际改名")
        return True, "dry-run"

    # 3) 改 VG 名 + LV 名
    rc, out, err = run(["vgrename", old_vg, new_vg])
    if rc != 0:
        return False, "vgrename 失败: %s" % err
    rc, out, err = run(["lvrename", new_vg, old_lv, "0"])
    if rc != 0:
        # 回滚 VG 名
        run(["vgrename", new_vg, old_vg])
        return False, "lvrename 失败（已回滚 vgrename）: %s" % err
    run(["vgchange", "-ay", new_vg])
    log("  改名完成")

    # 4) 设默认子卷为 @syno（这样 /volN 根下直接是共享文件夹）
    sid = get_syno_subvol_id(new_dev)
    if sid:
        tmp = "/tmp/_adopt_setdefault"
        os.makedirs(tmp, exist_ok=True)
        if run(["mount", new_dev, tmp])[0] == 0:
            run(["btrfs", "subvolume", "set-default", str(sid), tmp])
            run(["umount", tmp])
        log("  默认子卷 → @syno (id=%d)" % sid)
    else:
        log("  ! 未找到 @syno 子卷，使用默认子卷")

    # 5) 改名后再验证一次，失败则回滚
    ok, n, names, err = try_mount_ro(new_dev, "/tmp/_adopt_verify")
    if not ok:
        log("  ✗ 改名后验证失败，回滚")
        run(["lvrename", new_vg, "0", old_lv])
        run(["vgrename", new_vg, old_vg])
        return False, "改名后挂载失败（已回滚）: %s" % err
    log("  改名后验证 OK：%d 个条目" % n)

    # 6) 写 mount 表
    muuid = "%s-0" % new_vg
    sql = ("INSERT INTO mount (id,uuid,comment,sync_speed_min,sync_speed_max,"
           "stripe_cache_size,warn_frsize,grow,state,pool_type) VALUES "
           "(%d,'%s','Synology %s/%s','-1','-1',4096,10,'',0,0) "
           "ON CONFLICT (uuid) DO NOTHING;" % (vol_id, muuid, old_vg, old_lv))
    rows, err = psql(sql)
    if rows is None:
        return False, "写 mount 表失败: %s" % err
    log("  已注册 mount 表：id=%d uuid=%s" % (vol_id, muuid))

    return True, {"new_vg": new_vg, "uuid": muuid, "vol_id": vol_id}


def rollback_one(muuid):
    """回滚：从 mount 表删记录 + LVM 改回群晖命名"""
    m = re.match(r"^(trim_[0-9a-f_]+)-0$", muuid)
    if not m:
        return False, "uuid 格式不对: %s" % muuid
    vg = m.group(1)
    rows, _ = psql("SELECT id FROM mount WHERE uuid='%s';" % muuid)
    if rows:
        psql("DELETE FROM mount WHERE uuid='%s';" % muuid)
        log("已删除 mount 表记录")
    rc, out, _ = run(["lvs", "--noheadings", "-o", "lv_name", vg])
    lv = out.split()[0] if out.split() else "0"
    run(["lvrename", vg, lv, "volume_1"])
    run(["vgrename", vg, "vg1"])
    return True, "已回滚（VG 名恢复为 vg1，LV 恢复为 volume_1）"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="群晖硬盘接入飞牛 fnOS（无损，注册为原生存储空间）")
    ap.add_argument("--scan", action="store_true", help="只列出可接入的群晖卷")
    ap.add_argument("--adopt", action="store_true", help="执行接入")
    ap.add_argument("--mount-check", metavar="VG/LV", help="只读试挂并列出内容")
    ap.add_argument("--rollback", metavar="UUID",
                    help="回滚指定存储空间（trim_xxx-0）")
    ap.add_argument("--dry-run", action="store_true", help="演练，不写入")
    ap.add_argument("--yes", action="store_true", help="确认执行（不加则只演练）")
    args = ap.parse_args()

    if not is_root():
        log("✗ 需要 root 权限（sudo）")
        return 1

    # --- 只读试挂 ---
    if args.mount_check:
        if "/" in args.mount_check:
            vg, lv = args.mount_check.split("/", 1)
        else:
            vg, lv = args.mount_check, "0"
        dev = "/dev/mapper/%s-%s" % (vg.replace("-", "--"), lv.replace("-", "--"))
        ok, n, names, err = try_mount_ro(dev, "/tmp/_mc")
        if ok:
            log("✓ %s 可挂载，%d 个条目：" % (dev, n))
            for x in names:
                log("  " + x)
            return 0
        log("✗ 挂载失败: %s" % err)
        return 1

    # --- 回滚 ---
    if args.rollback:
        ok, msg = rollback_one(args.rollback)
        log(("✓ " if ok else "✗ ") + str(msg))
        return 0 if ok else 1

    # --- 版本兼容检查（所有写操作前必做）---
    compat_ok, msgs = check_fnos_compat()
    if msgs and (args.adopt or args.scan):
        log("=== 环境兼容检查 ===")
        for m in msgs:
            log("  " + m)
        log("")
        if not compat_ok:
            log("✗ 环境不符合脚本假设，已中止。请按上面的提示处理后再试。")
            return 1

    # --- 扫描 ---
    cands = scan_synology_candidates()
    if args.scan or not args.adopt:
        log("=== 可接入的群晖数据卷 ===")
        if not cands:
            log("  （未发现符合条件的数据卷）")
            log("")
            log("可能原因：")
            log("  1) mdadm 阵列未组装   →  mdadm --assemble --scan")
            log("  2) LVM 未激活          →  vgchange -ay")
            log("  3) 卷不是 btrfs         →  本 skill 只支持 btrfs；ext4 卷请手工处理")
            log("  4) 卷已被飞牛纳管       →  检查 lvs 里是否已有 trim_ 开头的 VG")
            log("")
            log("排查命令：")
            log("  cat /proc/mdstat")
            log("  pvs; vgs; lvs -o vg_name,lv_name,lv_size,lv_attr")
            return 0
        for c in cands:
            log("  %-12s %-16s %-8s %s" % (c["vg"], c["lv"], c["size"], c["dev"]))
        log("")
        log("%d 个卷待接入。建议先 --adopt --dry-run 演练，确认无误后 --adopt --yes" % len(cands))
        return 0

    # --- 接入 ---
    if not cands:
        log("✗ 未发现可接入的群晖卷")
        return 1

    do_write = args.yes and not args.dry_run
    if not do_write:
        log("⚠ 演练模式（不会修改任何东西）。确认无误后加 --yes 真正执行。")
        log("")

    # 预检：内核补丁是否生效
    ok, _, names, err = try_mount_ro(cands[0]["dev"], "/tmp/_pre")
    if not ok and "invalid root flags" in (err or ""):
        log("✗ 内核 btrfs 模块未打群晖补丁，无法挂载。")
        log("  请先执行：")
        log("    python3 scripts/gen_btrfs_patch.py --kernel \"$(uname -r)\"")
        log("    python3 scripts/ird_tool.py --initrd /boot/initrd.img-$(uname -r) \\")
        log("        --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko")
        log("    reboot")
        return 1

    if do_write:
        log("=== 备份 LVM 元数据与 mount 表 ===")
        for p in backup_all():
            log("  " + p)

    existing, err = existing_mount_vols()
    if existing is None:
        log("✗ 无法读取 mount 表: %s" % err)
        return 1
    taken = set(existing.values())

    results = []
    for c in cands:
        vid = free_vol_id(taken)
        taken.add(vid)
        ok, info = adopt_one(c, vid, dry_run=not do_write)
        results.append((c, ok, info))
        if not ok:
            log("  ✗ %s" % info)

    log("")
    log("=== 汇总 ===")
    for c, ok, info in results:
        if ok and isinstance(info, dict):
            log("  ✓ %s/%s → /vol%d  (%s)" % (c["vg"], c["lv"], info["vol_id"], info["uuid"]))
        elif ok:
            log("  ○ %s/%s （演练，未改动）" % (c["vg"], c["lv"]))
        else:
            log("  ✗ %s/%s : %s" % (c["vg"], c["lv"], info))

    if do_write and any(ok and isinstance(i, dict) for _, ok, i in results):
        log("")
        log("下一步：重启或等待 triminit 自动挂载，然后：")
        log("  1) 检查挂载：mount | grep ' /vol'")
        log("  2) 刷新共享文件夹：systemctl restart share_service")
        log("  3) 挂载共享文件夹到个人空间：python3 scripts/bind_shares.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
