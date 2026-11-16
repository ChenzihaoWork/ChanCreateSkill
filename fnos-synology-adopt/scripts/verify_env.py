#!/usr/bin/env python3
"""
环境自检 —— 在飞牛 NAS 上运行，检查接入所需的全部前置条件

用法：
  sudo python3 verify_env.py
  sudo python3 verify_env.py --verbose

退出码：0 全部就绪；1 有未满足项
"""
import argparse
import os
import re
import shutil
import subprocess
import sys


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


class Checker:
    def __init__(self):
        self.results = []   # (level, name, detail)

    def ok(self, name, detail=""):
        self.results.append(("OK", name, detail))

    def warn(self, name, detail=""):
        self.results.append(("WARN", name, detail))

    def fail(self, name, detail=""):
        self.results.append(("FAIL", name, detail))

    def report(self):
        icons = {"OK": "✓", "WARN": "!", "FAIL": "✗"}
        for lvl, name, detail in self.results:
            print("  %s %-34s %s" % (icons[lvl], name, detail))
        fails = sum(1 for r in self.results if r[0] == "FAIL")
        warns = sum(1 for r in self.results if r[0] == "WARN")
        print()
        if fails:
            print("结果：%d 项未通过，%d 项警告" % (fails, warns))
        elif warns:
            print("结果：全部关键项通过，%d 项警告" % warns)
        else:
            print("结果：全部通过 ✓")
        return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    c = Checker()

    print("=" * 68)
    print("飞牛 + 群晖接入环境自检")
    print("=" * 68)
    print()

    # ---------- 1. 系统 ----------
    print("【系统】")
    if os.geteuid() != 0:
        c.fail("root 权限", "请用 sudo 运行")
    else:
        c.ok("root 权限")

    rc, kver, _ = run("uname -r")
    if rc == 0:
        c.ok("内核版本", kver)
    else:
        c.fail("内核版本", "无法获取")

    # 飞牛识别
    fnos = os.path.exists("/usr/trim/bin/triminit")
    if fnos:
        c.ok("飞牛 fnOS", "已识别")
    else:
        c.warn("飞牛 fnOS", "未检测到 /usr/trim —— 确认这是飞牛系统？")

    # 必需工具
    for tool in ("python3", "xz", "zstd", "mdadm", "lvm", "btrfs"):
        p = shutil.which(tool)
        if p:
            c.ok("工具 %s" % tool, p)
        else:
            c.fail("工具 %s" % tool, "未安装")
    print()

    # ---------- 2. 群晖盘结构 ----------
    print("【群晖盘结构】")
    rc, mdstat, _ = run("cat /proc/mdstat")
    md_count = len(re.findall(r"^md\d+\s*:", mdstat or "", re.M))
    if md_count:
        c.ok("mdadm 阵列", "%d 个" % md_count)
    else:
        c.warn("mdadm 阵列", "无阵列（若群晖盘已插入，试 mdadm --assemble --scan）")

    rc, pvs, _ = run("pvs --noheadings -o pv_name,vg_name 2>/dev/null")
    vgs_found = []
    for line in (pvs or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] not in vgs_found:
            vgs_found.append(parts[1])
    syno_vgs = [v for v in vgs_found if v in ("vg1", "vg2", "vg3", "vg4")
                or re.match(r"^vg\d+$", v)]
    trim_vgs = [v for v in vgs_found if v.startswith("trim_")]
    if syno_vgs:
        c.ok("群晖 LVM 卷组", ", ".join(syno_vgs))
    if trim_vgs:
        c.ok("飞牛已纳管卷组", "%d 个" % len(trim_vgs))
    if not vgs_found:
        c.fail("LVM", "无卷组（试 vgchange -ay）")

    # 数据卷
    rc, lvs, _ = run("lvs --noheadings -o vg_name,lv_name,lv_size,lv_attr 2>/dev/null")
    data_lvs = []
    for line in (lvs or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            vg, lv, size = parts[0], parts[1], parts[2]
            if lv == "syno_vg_reserved_area":
                continue
            data_lvs.append((vg, lv, size))
    if data_lvs:
        c.ok("数据卷", "%d 个" % len(data_lvs))
        if args.verbose:
            for vg, lv, size in data_lvs:
                print("      %s/%s  %s" % (vg, lv, size))
    else:
        c.fail("数据卷", "未找到")
    print()

    # ---------- 3. 内核补丁 ----------
    print("【内核 btrfs 补丁】")
    patched = False
    gen = "/usr/local/share/fnos-btrfs-patch/gen_btrfs_patch.py"
    if not os.path.exists(gen):
        gen = "./gen_btrfs_patch.py"
    if os.path.exists(gen):
        rc, out, err = run([sys.executable, gen, "--check", "--kernel", kver])
        if "已打补丁" in (out or ""):
            patched = True
            c.ok("模块补丁", "已打补丁")
        else:
            c.fail("模块补丁", "未打补丁 —— 群晖卷无法挂载")
    else:
        c.warn("模块补丁", "找不到 gen_btrfs_patch.py，无法检查")

    # initrd
    initrd = "/boot/initrd.img-%s" % kver
    ird_backup = initrd + ".orig-backup"
    if os.path.exists(initrd):
        if os.path.exists(ird_backup):
            bigger = os.path.getsize(initrd) != os.path.getsize(ird_backup)
            c.ok("initrd", "%s（%s）" % (
                "已修改" if bigger else "与原版同大小", initrd))
        else:
            c.warn("initrd", "无备份，可能未同步（补丁会失效）")
    else:
        c.warn("initrd", "未找到")

    # 实测：能否挂载
    if data_lvs and patched:
        vg, lv, _ = data_lvs[0]
        dev = "/dev/mapper/%s-%s" % (vg.replace("-", "--"), lv.replace("-", "--"))
        mnt = "/tmp/_verify_mnt"
        os.makedirs(mnt, exist_ok=True)
        rc, _, err = run(["mount", "-o", "ro", dev, mnt])
        if rc == 0:
            n = len(os.listdir(mnt))
            run(["umount", mnt])
            c.ok("实测挂载", "%s → %d 个条目" % (dev, n))
        else:
            c.fail("实测挂载", "%s: %s" % (dev, err[:80]))
    print()

    # ---------- 4. 存储空间 ----------
    print("【飞牛存储空间】")
    rc, out, _ = run("grep ' /vol' /proc/self/mountinfo")
    vols = sorted(set(re.findall(r" /vol\d+ ", out or "")))
    if vols:
        c.ok("已挂载存储空间", "%d 个: %s" % (len(vols), ", ".join(v.strip() for v in vols)))
    else:
        c.warn("已挂载存储空间", "无")

    rows = None
    rc, out, err = run(
        ["sudo", "-u", "postgres", "psql", "-d", "trim", "-P", "pager=off",
         "-t", "-A", "-F", "\t", "-c", "SELECT id, uuid FROM mount ORDER BY id;"])
    if rc == 0:
        rows = [l.split("\t") for l in out.splitlines() if l.strip()]
        c.ok("mount 表", "%d 条记录" % len(rows))
        if args.verbose:
            for r in rows:
                print("      id=%s  %s" % (r[0], r[1]))
    else:
        c.fail("mount 表", "无法查询: %s" % err[:60])
    print()

    # ---------- 5. 共享文件夹 ----------
    print("【共享文件夹 bind 挂载】")
    rc, out, _ = run("grep ' /vol' /proc/self/mountinfo | grep '/1000/'")
    binds = [l for l in (out or "").splitlines() if l.strip()]
    if binds:
        c.ok("bind 挂载", "%d 个" % len(binds))
    else:
        c.warn("bind 挂载", "无（「我的文件」里看不到群晖共享文件夹）")

    for svc in ("fnos-synology-binds.service",
                "fnos-btrfs-patch-guard.service",
                "fnos-btrfs-patch-watchdog.timer"):
        rc, en, _ = run("systemctl is-enabled %s" % svc)
        rc2, ac, _ = run("systemctl is-active %s" % svc)
        if en == "enabled":
            c.ok(svc, "%s / %s" % (en, ac))
        else:
            c.warn(svc, "未安装或未启用")
    print()

    # ---------- 6. 自愈状态 ----------
    print("【自愈状态】")
    st = "/var/run/fnos-btrfs-patch.status"
    if os.path.exists(st):
        print("  " + open(st).read().strip().replace("\n", "\n  "))
    else:
        c.warn("状态文件", "不存在（自愈脚本尚未运行）")
    print()

    # ---------- 7. 错误检查 ----------
    print("【运行状态】")
    rc, out, _ = run("dmesg")
    ncrit = len(re.findall(r"BTRFS critical", out or ""))
    if ncrit == 0:
        c.ok("BTRFS 错误", "0 条")
    else:
        c.fail("BTRFS 错误", "%d 条" % ncrit)
        if args.verbose:
            for line in (out or "").splitlines():
                if "BTRFS critical" in line:
                    print("      " + line.strip()[:100])

    rc, out, _ = run("grep -c 'not existed' /usr/trim/logs/share_service.log 2>/dev/null")
    if (out or "0").isdigit() and int(out or 0) > 0:
        c.warn("share_service", "%s 条 'not existed'（时序问题，可 restart 修复）" % out)
    print()

    print("=" * 68)
    fails = c.report()
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
