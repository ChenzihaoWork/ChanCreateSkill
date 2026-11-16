#!/usr/bin/env python3
"""
把群晖共享文件夹 bind 挂载到飞牛个人空间（/volN/1000/）

为什么需要
----------
飞牛的「文件管理 → 我的文件」**只显示各存储空间下 /volN/1000/ 目录里的内容**。
群晖的共享文件夹在卷根目录（如 /volN/Data），所以不会出现在「我的文件」里，
只能在「管理员视角 → 设备全部文件」中看到。

飞牛的 share_service 里有个 TrimAFS 组件，会把 /volN/1000/ 下的**每个目录**
自动注册成一个共享文件夹（日志："Need to create share folder '<path>', name '<n>'"）。
所以只要把群晖共享文件夹 bind 挂载过去，它们就会出现在「我的文件」里。

⚠ 必须用 bind 挂载，不能用软链接 —— 飞牛 UI 不认软链接（会显示成坏链接，属主也不对）。

排除项
------
* 以 @ 开头的目录：群晖系统目录（@appconf / @eaDir / @database ...）
* homes：群晖用户家目录，挂进去会和飞牛的同名用户目录冲突
* docker：容器数据，不建议暴露到个人空间

用法
----
  # 1) 只看各卷有哪些可挂载的共享文件夹（只读）
  python3 bind_shares.py --list

  # 2) 生成清单（写到 /root/_stor_adopt_backup/bindmounts.txt）
  python3 bind_shares.py --generate

  # 3) 立即执行挂载（用已有清单）
  python3 bind_shares.py --apply

  # 4) 安装开机自动恢复（systemd 服务，挂 graphical.target）
  python3 bind_shares.py --install-service

  # 5) 全流程：生成清单 + 挂载 + 装服务
  python3 bind_shares.py --generate --apply --install-service
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

MANIFEST = "/root/_stor_adopt_backup/bindmounts.txt"
WD_SCRIPT = "/usr/local/sbin/fnos-synology-binds.sh"
UNIT_PATH = "/etc/systemd/system/fnos-synology-binds.service"
LOG = "/var/log/fnos-synology-binds.log"

# 这些目录不挂载
EXCLUDE_NAMES = {
    "homes",              # 群晖用户家目录，会和飞牛用户目录冲突
    "docker",             # 容器数据
    "@eaDir",             # 群晖缩略图缓存
    "thumb",              # 飞牛缩略图
    "lost+found",
    "appcenter-downloads",  # 飞牛应用中心下载目录
    "@appmeta",           # 飞牛应用元数据
}
EXCLUDE_PREFIX = ("@", "$", ".")   # 系统目录/回收站/隐藏目录

# 飞牛系统盘的标志目录（出现即判定为系统盘，整卷跳过）
FNOS_SYSTEM_MARKERS = ("@appmeta", "appcenter-downloads")


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "超时"


def log(msg=""):
    print(msg, flush=True)


def is_root():
    return os.geteuid() == 0


def mounted_vols():
    """返回 [(volN 路径, 设备, 卷容量描述)]"""
    rc, out, _ = run("grep ' /vol' /proc/self/mountinfo")
    vols = []
    seen = set()
    for line in out.splitlines():
        parts = line.split(" - ")
        if len(parts) < 2:
            continue
        pre = parts[0].split()
        post = parts[1].split()
        if len(pre) < 5 or len(post) < 2:
            continue
        mnt = pre[4]
        if not re.match(r"^/vol\d+$", mnt):
            continue
        if mnt in seen:
            continue
        seen.add(mnt)
        vols.append((mnt, post[1]))
    return sorted(vols, key=lambda x: int(x[0][4:]))


def is_bind_mount(path):
    rc, _, _ = run(["mountpoint", "-q", path])
    return rc == 0


def list_candidates():
    """
    列出所有**群晖卷**里可挂载的共享文件夹，返回 [(src, dst), ...]

    会自动跳过飞牛自己的系统盘（通过 @appmeta / appcenter-downloads 标志目录识别），
    否则会把系统盘里的杂项目录也 bind 过去。
    """
    pairs = []
    for vol, dev in mounted_vols():
        try:
            items = set(os.listdir(vol))
        except OSError:
            continue

        # 飞牛系统盘：整卷跳过
        if any(m in items for m in FNOS_SYSTEM_MARKERS):
            log("  [跳过] %s 是飞牛系统盘" % vol)
            continue

        for name in sorted(items):
            if name in EXCLUDE_NAMES:
                continue
            if name.startswith(EXCLUDE_PREFIX):
                continue
            if name == "1000":          # 飞牛个人空间目录本身
                continue
            src = os.path.join(vol, name)
            if not os.path.isdir(src):
                continue
            dst = os.path.join(vol, "1000", name)
            pairs.append((src, dst))
    return pairs


def generate_manifest(path=MANIFEST):
    pairs = list_candidates()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# 群晖共享文件夹 -> 飞牛个人空间 bind 挂载清单\n")
        f.write("# 格式: <源路径> <目标路径>\n")
        f.write("# 由 bind_shares.py --generate 生成于 %s\n" %
                __import__("datetime").datetime.now().strftime("%F %T"))
        for src, dst in pairs:
            f.write("%s %s\n" % (src, dst))
    log("已生成清单：%s（%d 条）" % (path, len(pairs)))
    for src, dst in pairs:
        log("  %s -> %s" % (src, dst))
    return pairs


def read_manifest(path=MANIFEST):
    pairs = []
    if not os.path.exists(path):
        return pairs
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pairs.append((parts[0], parts[1]))
    return pairs


def apply_binds(path=MANIFEST):
    pairs = read_manifest(path)
    if not pairs:
        log("✗ 清单为空：%s（先运行 --generate）" % path)
        return 1

    ok = skip = fail = 0
    for src, dst in pairs:
        if not os.path.isdir(src):
            log("  跳过（源不存在）: %s" % src)
            skip += 1
            continue
        os.makedirs(dst, exist_ok=True)
        if is_bind_mount(dst):
            log("  已挂载: %s" % dst)
            skip += 1
            continue
        rc, _, err = run(["mount", "--bind", src, dst])
        if rc == 0:
            log("  ✅ %s -> %s" % (src, dst))
            ok += 1
        else:
            log("  ❌ %s -> %s : %s" % (src, dst, err))
            fail += 1

    log("")
    log("结果：成功 %d，跳过 %d，失败 %d" % (ok, skip, fail))
    if ok:
        log("正在刷新飞牛共享文件夹索引…")
        run("systemctl restart share_service")
        time_sleep(8)
        rc, out, _ = run("grep 'Need to create share folder' /usr/trim/logs/share_service.log | tail -20")
        if out:
            log("TrimAFS 已注册：")
            for line in out.splitlines()[-15:]:
                m = re.search(r"folder '([^']+)'", line)
                if m:
                    log("  " + m.group(1))
    return 0 if fail == 0 else 1


def time_sleep(s):
    import time
    time.sleep(s)


# ---------------------------------------------------------------- systemd

WD_SCRIPT_CONTENT = r'''#!/bin/bash
# 恢复群晖共享文件夹 -> 飞牛个人空间的 bind 挂载
# 清单: {manifest}
# 由 fnos-synology-binds.service 在系统启动后调用
LOG={log}
LIST={manifest}
log(){{ echo "[$(date '+%F %T')] $*" >> "$LOG"; }}

log "=== 开始恢复群晖共享文件夹 bind 挂载 ==="
[ -f "$LIST" ] || {{ log "清单不存在: $LIST"; exit 0; }}

ok=0; skip=0; fail=0
while read -r src dst; do
  [ -z "$src" ] && continue
  case "$src" in \#*) continue;; esac
  # 等源卷就绪（最多 120 秒）
  for i in $(seq 1 60); do [ -d "$src" ] && break; sleep 2; done
  [ -d "$src" ] || {{ log "[跳过] 源不存在: $src"; skip=$((skip+1)); continue; }}
  mkdir -p "$dst" 2>/dev/null
  if mountpoint -q "$dst"; then skip=$((skip+1)); continue; fi
  if mount --bind "$src" "$dst" 2>&1; then
    log "[OK] $src -> $dst"; ok=$((ok+1))
  else
    log "[FAIL] $src -> $dst"; fail=$((fail+1))
  fi
done < "$LIST"

log "=== 完成: 成功 $ok, 跳过 $skip, 失败 $fail ==="
[ "$ok" -gt 0 ] && systemctl restart share_service >> "$LOG" 2>&1 && log "已刷新共享文件夹索引"
exit 0
'''

UNIT_CONTENT = """[Unit]
Description=Bind-mount Synology shared folders into FNOS personal space
# ⚠ 关键: 挂 graphical.target, 不要挂 multi-user.target(飞牛不走它, 永远 inactive)
# ⚠ 关键: 绝对不要写 After=trim_main.service
#   trim_main 是 Type=simple + Restart=always 的常驻守护进程,
#   systemd 认为它永远不"启动完成", 所有依赖它的 oneshot 服务会被无限推迟、永不执行。

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=300
ExecStart={script}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
"""


def install_service():
    os.makedirs(os.path.dirname(WD_SCRIPT), exist_ok=True)
    with open(WD_SCRIPT, "w") as f:
        f.write(WD_SCRIPT_CONTENT.format(manifest=MANIFEST, log=LOG))
    os.chmod(WD_SCRIPT, 0o755)
    log("已写入恢复脚本：%s" % WD_SCRIPT)

    with open(UNIT_PATH, "w") as f:
        f.write(UNIT_CONTENT.format(script=WD_SCRIPT))
    log("已写入 systemd 服务：%s" % UNIT_PATH)

    run("systemctl daemon-reload")
    rc, out, err = run("systemctl enable fnos-synology-binds.service")
    log("enable：%s" % (out or err))
    rc, out, _ = run("systemctl is-enabled fnos-synology-binds.service")
    log("状态：%s" % out)
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="把群晖共享文件夹 bind 挂载到飞牛个人空间 /volN/1000/")
    ap.add_argument("--list", action="store_true", help="只列出可挂载的共享文件夹")
    ap.add_argument("--generate", action="store_true", help="生成挂载清单")
    ap.add_argument("--apply", action="store_true", help="执行挂载")
    ap.add_argument("--install-service", action="store_true",
                    help="安装开机自动恢复的 systemd 服务")
    ap.add_argument("--manifest", default=MANIFEST, help="清单文件路径")
    args = ap.parse_args()

    if not is_root():
        log("✗ 需要 root 权限（sudo）")
        return 1

    did = False
    if args.list:
        did = True
        log("=== 各存储空间可挂载的共享文件夹 ===")
        cands = list_candidates()
        by_vol = {}
        for src, dst in cands:
            vol = os.path.dirname(src)
            by_vol.setdefault(vol, []).append(os.path.basename(src))
        if not by_vol:
            log("  （未发现可挂载的共享文件夹）")
            log("  → 确认群晖卷已挂载：mount | grep ' /vol'")
        for vol in sorted(by_vol, key=lambda p: int(re.sub(r"\D", "", p) or 0)):
            dev = dict(mounted_vols()).get(vol, "?")
            log("")
            log("%s  (%s)" % (vol, dev))
            for name in sorted(by_vol[vol]):
                log("  %s" % name)

    if args.generate:
        did = True
        generate_manifest(args.manifest)

    if args.apply:
        did = True
        apply_binds(args.manifest)

    if args.install_service:
        did = True
        install_service()

    if not did:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
