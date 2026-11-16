#!/usr/bin/env python3
"""
一键部署飞牛+群晖接入工具链（自愈机制 + systemd 服务）

部署内容
--------
  /usr/local/share/fnos-btrfs-patch/gen_btrfs_patch.py   补丁生成器
  /usr/local/share/fnos-btrfs-patch/ird_tool.py          initrd 替换器
  /usr/local/share/fnos-btrfs-patch/adopt_synology.py    接入编排
  /usr/local/share/fnos-btrfs-patch/bind_shares.py       共享文件夹挂载
  /usr/local/sbin/fnos-btrfs-patch-watchdog.sh           自愈脚本
  /etc/systemd/system/fnos-btrfs-patch-guard.service     开机+定时检查
  /etc/systemd/system/fnos-btrfs-patch-watchdog.timer    30 分钟定时器

⚠ systemd 关键配置（踩过的坑）
------------------------------
1. **必须用 graphical.target**，不能用 multi-user.target
   飞牛的 multi-user.target 永远是 inactive，挂上去服务永不触发。

2. **绝对不要写 After=trim_main.service**
   trim_main 是 Type=simple + Restart=always 的常驻守护进程，
   systemd 认为它永远不会"进入启动完成状态"，
   于是所有 After=trim_main 的 oneshot 服务被无限推迟、永不执行。

3. TimeoutStartSec 要放在 [Service] 段，放 [Unit] 会报 "Unknown key"。

用法
----
  # 部署（把本 skill 的 scripts/ 目录作为源）
  sudo python3 install.py --source-dir /path/to/skill/scripts

  # 部署并立即给当前内核打补丁
  sudo python3 install.py --source-dir ... --patch-now

  # 只检查部署状态
  python3 install.py --status
"""
import argparse
import os
import shutil
import subprocess
import sys

TOOLS_DIR = "/usr/local/share/fnos-btrfs-patch"
WATCHDOG = "/usr/local/sbin/fnos-btrfs-patch-watchdog.sh"
GUARD_UNIT = "/etc/systemd/system/fnos-btrfs-patch-guard.service"
TIMER_UNIT = "/etc/systemd/system/fnos-btrfs-patch-watchdog.timer"

SCRIPTS = [
    "gen_btrfs_patch.py",
    "ird_tool.py",
    "adopt_synology.py",
    "bind_shares.py",
]

GUARD_UNIT_CONTENT = """[Unit]
Description=Check and auto-repair Synology btrfs compatibility patch
# ⚠ 不要写 After=trim_main.service
#   trim_main 是 Type=simple + Restart=always 的常驻守护进程，
#   systemd 认为它永远不"启动完成"，依赖它的 oneshot 服务会被无限推迟。

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=600
ExecStart={watchdog}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
"""

TIMER_UNIT_CONTENT = """[Unit]
Description=Periodically check Synology btrfs patch status

[Timer]
OnBootSec=8min
OnUnitActiveSec=30min
AccuracySec=1min
Unit=fnos-btrfs-patch-guard.service
Persistent=true

[Install]
WantedBy=graphical.target
"""


def run(cmd, check=False, timeout=300):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("%s\n%s" % (cmd, r.stderr))
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "超时"


def log(msg=""):
    print(msg, flush=True)


def deploy(source_dir):
    if not os.path.isdir(source_dir):
        log("✗ 源目录不存在: %s" % source_dir)
        return 1

    os.makedirs(TOOLS_DIR, exist_ok=True)
    for name in SCRIPTS + ["patch_watchdog.sh"]:
        src = os.path.join(source_dir, name)
        if not os.path.exists(src):
            log("  ! 跳过（源缺失）: %s" % name)
            continue
        dst = os.path.join(TOOLS_DIR, name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        log("  ✓ %s" % dst)

    # 自愈脚本安装到 sbin
    wd_src = os.path.join(TOOLS_DIR, "patch_watchdog.sh")
    if os.path.exists(wd_src):
        shutil.copy2(wd_src, WATCHDOG)
        os.chmod(WATCHDOG, 0o755)
        log("  ✓ %s" % WATCHDOG)

    # systemd units
    with open(GUARD_UNIT, "w") as f:
        f.write(GUARD_UNIT_CONTENT.format(watchdog=WATCHDOG))
    log("  ✓ %s" % GUARD_UNIT)

    with open(TIMER_UNIT, "w") as f:
        f.write(TIMER_UNIT_CONTENT)
    log("  ✓ %s" % TIMER_UNIT)

    run("systemctl daemon-reload")
    for unit in ("fnos-btrfs-patch-guard.service",
                 "fnos-btrfs-patch-watchdog.timer"):
        rc, out, err = run("systemctl enable %s" % unit)
        log("  enable %-38s %s" % (unit, (out or err).splitlines()[0] if (out or err) else "ok"))
    run("systemctl restart fnos-btrfs-patch-watchdog.timer")
    return 0


def patch_now():
    kver = run("uname -r")[1]
    log("")
    log("=== 给当前内核 %s 打补丁 ===" % kver)
    rc, out, err = run([sys.executable,
                        os.path.join(TOOLS_DIR, "gen_btrfs_patch.py"),
                        "--kernel", kver])
    log(out or err)
    if rc != 0:
        log("✗ 打补丁失败")
        return 1

    # 同步 initrd
    initrd = "/boot/initrd.img-%s" % kver
    ko = os.path.join(TOOLS_DIR, "btrfs_patched_%s.ko" % kver)
    if os.path.exists(initrd) and os.path.exists(ko):
        log("")
        log("=== 同步 initrd ===")
        rc, out, err = run([sys.executable,
                            os.path.join(TOOLS_DIR, "ird_tool.py"),
                            "--initrd", initrd, "--ko", ko])
        log(out or err)
        if rc != 0:
            log("✗ initrd 同步失败")
            return 1
    else:
        log("! initrd 或补丁模块不存在，跳过")
    log("")
    log("✓ 完成。需要重启才能让新模块生效：reboot")
    return 0


def status():
    log("=== 部署状态 ===")
    for p in [TOOLS_DIR] + [os.path.join(TOOLS_DIR, s) for s in SCRIPTS] + \
             [WATCHDOG, GUARD_UNIT, TIMER_UNIT]:
        exists = os.path.exists(p)
        log("  %s %s" % ("✓" if exists else "✗", p))

    log("")
    log("=== systemd ===")
    for unit in ("fnos-btrfs-patch-guard.service",
                 "fnos-btrfs-patch-watchdog.timer"):
        for prop in ("is-enabled", "is-active"):
            rc, out, _ = run("systemctl %s %s" % (prop, unit))
            log("  %-40s %-12s %s" % (unit, prop, out or "unknown"))

    log("")
    log("=== 当前内核补丁 ===")
    gen = os.path.join(TOOLS_DIR, "gen_btrfs_patch.py")
    if os.path.exists(gen):
        kver = run("uname -r")[1]
        rc, out, err = run([sys.executable, gen, "--check", "--kernel", kver])
        log(out or err)

    log("")
    log("=== 运行状态文件 ===")
    if os.path.exists("/var/run/fnos-btrfs-patch.status"):
        log(open("/var/run/fnos-btrfs-patch.status").read().strip())
    else:
        log("  （尚未运行过）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="部署飞牛+群晖接入工具链")
    ap.add_argument("--source-dir", help="skill 的 scripts 目录路径")
    ap.add_argument("--patch-now", action="store_true",
                    help="部署后立即给当前内核打补丁并同步 initrd")
    ap.add_argument("--status", action="store_true", help="只检查部署状态")
    args = ap.parse_args()

    if args.status:
        return status()

    if os.geteuid() != 0:
        log("✗ 需要 root 权限（sudo）")
        return 1

    if not args.source_dir:
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)))
        args.source_dir = src
        log("未指定 --source-dir，使用脚本所在目录：%s" % src)

    log("=== 1. 部署工具链 ===")
    rc = deploy(args.source_dir)
    if rc != 0:
        return rc

    if args.patch_now:
        rc = patch_now()
        if rc != 0:
            return rc

    log("")
    log("=== 部署完成 ===")
    log("查看状态：python3 %s/install.py --status" %
        os.path.dirname(os.path.abspath(__file__)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
