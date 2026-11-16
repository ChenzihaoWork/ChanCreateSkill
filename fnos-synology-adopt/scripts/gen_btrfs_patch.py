#!/usr/bin/env python3
"""
飞牛 fnOS + 群晖(Synology) btrfs 兼容补丁生成器

原理
----
群晖 DSM 7.x 的 btrfs 使用私有 flag，主线 Linux 5.4+ 内核会直接拒绝挂载，报：

    BTRFS critical: corrupt leaf: invalid root flags,
                    have 0x400000000 expect mask 0x1000000000001
    BTRFS critical: corrupt leaf: unknown incompat flags detected: 0x40000000

需要放宽三处校验：

  1. root flags 掩码      0xfffefffffffffffe -> 0xfffeff01fffffffe   (放行 bit33~39)
  2. 错误显示掩码         0x1000000000001    -> 0x100fe00000001      (仅影响报错文案)
  3. inode flags 掩码     0x7ffff000         -> 0x3ffff000          (清除 bit30)

注意：0x7ffff000 **本身已包含 bit30**，所以必须用 `& ~bit` 清除而不是 `| bit`（后者是空操作）。

为什么不用上游源码重编译
------------------------
飞牛的 btrfs.ko 有私有挂载选项 `trimacl`（Opt_trimacl /
btrfs_xattr_trimacl_access_handler），上游内核没有，且有 `unrecognized mount option`
检查。用上游源码重编译会导致飞牛系统盘 /vol1 挂不上。
因此必须给飞牛自己的 btrfs.ko 打二进制补丁。

为什么要剥离模块签名
--------------------
内核未启用 CONFIG_MODULE_SIG_FORCE（不强制签名），但**带无效签名的模块会被拒绝**
（"Key was rejected by service"）。所以补丁后要截掉 PKCS#7 签名尾，
让内核按"未签名模块"处理（只会 taint，不影响加载）。

用法
----
  # 只检查当前内核补丁状态
  python3 gen_btrfs_patch.py --check --kernel "$(uname -r)"

  # 给指定内核打补丁并安装
  python3 gen_btrfs_patch.py --kernel "$(uname -r)"

  # 检查所有已安装内核
  python3 gen_btrfs_patch.py --check --kernel all

  # 指定其他模块根目录（调试用）
  python3 gen_btrfs_patch.py --kernel 6.18.0 --root /tmp/fake/lib/modules

退出码：0 成功/已打补丁；1 失败
"""
import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

# ============ 群晖私有 flag 定义 ============
# root flags: 实测 bit34（多数卷）、bit34+bit35（部分卷）；保守放行 bit33~bit39
SYN_ROOT_BITS = 0
for _b in range(33, 40):
    SYN_ROOT_BITS |= (1 << _b)

# inode flags: 群晖用 bit30 (0x40000000)
SYN_INODE_BIT = 1 << 30

# ============ 原始（未打补丁）特征值 ============
ORIG_ROOT_MASK = 0xfffefffffffffffe
ORIG_DISPLAY_MASK = 0x1000000000001
ORIG_INODE_MASK = 0x7FFFF000

# ============ 补丁后的目标值 ============
# root: 原有允许位 (bit0 RDONLY + bit32) 之外，再放行群晖私有位
NEW_ROOT_MASK = (~((0x1000000000001) | SYN_ROOT_BITS)) & 0xFFFFFFFFFFFFFFFF
NEW_DISPLAY_MASK = 0x1000000000001 | SYN_ROOT_BITS
# inode: BTRFS_INODE_FLAG_MASK = 0x80000FFF，取反得 0x7FFFF000（已含 bit30），
#        必须清除 bit30 才能放行群晖的私有 inode flag
NEW_INODE_MASK = ((~0x80000FFF) & 0xFFFFFFFF) & (~SYN_INODE_BIT & 0xFFFFFFFF)

ORIG_TO_KIND = {
    ORIG_ROOT_MASK: "root_flags_mask",
    ORIG_DISPLAY_MASK: "display_mask",
    ORIG_INODE_MASK: "inode_flags_mask",
}
NEW_TO_KIND = {
    NEW_ROOT_MASK: "root_flags_mask",
    NEW_DISPLAY_MASK: "display_mask",
    NEW_INODE_MASK: "inode_flags_mask",
}
KIND_TO_NEW = {
    "root_flags_mask": NEW_ROOT_MASK,
    "display_mask": NEW_DISPLAY_MASK,
    "inode_flags_mask": NEW_INODE_MASK,
}

TEXT_OFF = 0x40  # .text 段在文件中的起始偏移（飞牛内核实测）


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- 扫描

def scan_masks(data):
    """
    扫描 .ko 中的目标立即数，返回 {kind: [(file_off, size, value), ...]}

    同时匹配「原始值」和「已补丁值」，因此既能检测也能幂等打补丁。
    """
    found = {}
    n = len(data)
    for i in range(TEXT_OFF, n - 10):
        b0, b1 = data[i], data[i + 1]
        # movabs $imm64, %rXX : 48 ba (rdx) / 49 b8 (r8) 等
        if b0 in (0x48, 0x49) and b1 in (0xba, 0xb8):
            imm = int.from_bytes(data[i + 2:i + 10], "little")
            kind = ORIG_TO_KIND.get(imm) or NEW_TO_KIND.get(imm)
            if kind:
                found.setdefault(kind, []).append((i + 2, 8, imm))
        # test $imm32, %eax : a9
        if data[i] == 0xA9:
            imm = struct.unpack_from("<I", data, i + 1)[0]
            if imm in (ORIG_INODE_MASK, NEW_INODE_MASK):
                found.setdefault("inode_flags_mask", []).append((i + 1, 4, imm))
    return found


def is_patched(data):
    """已打补丁 = root 与 inode 两处都是新值"""
    found = scan_masks(data)
    root_ok = any(v == NEW_ROOT_MASK for _, _, v in found.get("root_flags_mask", []))
    inode_ok = any(v == NEW_INODE_MASK for _, _, v in found.get("inode_flags_mask", []))
    return (root_ok and inode_ok), found


# ---------------------------------------------------------------- 打补丁

def apply_patch(data):
    """就地应用全部补丁，返回 (新bytes, [变更说明])"""
    found = scan_masks(data)
    out = bytearray(data)
    applied = []
    for kind, hits in found.items():
        target = KIND_TO_NEW[kind]
        for off, size, val in hits:
            if val == target:
                continue
            if size == 8:
                out[off:off + 8] = target.to_bytes(8, "little")
            else:
                struct.pack_into("<I", out, off, target & 0xFFFFFFFF)
            applied.append("%-18s 0x%x -> 0x%x" % (kind, val, target))
    return bytes(out), applied


def strip_signature(data):
    """截掉 PKCS#7 签名尾（无效签名会被内核拒绝加载）"""
    marker = b"\x00\x00\x02\x00\x00\x00\x00\x00"
    idx = data.rfind(marker)
    if idx < 0:
        return data, None
    return data[:idx], idx


# ---------------------------------------------------------------- 单内核处理

def process_kernel(kver, modroot="/lib/modules", check_only=False,
                   share_dir="/usr/local/share/fnos-btrfs-patch"):
    moddir = os.path.join(modroot, kver, "kernel/fs/btrfs")
    ko_xz = os.path.join(moddir, "btrfs.ko.xz")
    ko_plain = os.path.join(moddir, "btrfs.ko")

    if os.path.exists(ko_xz):
        src, is_xz = ko_xz, True
    elif os.path.exists(ko_plain):
        src, is_xz = ko_plain, False
    else:
        return None, "找不到 btrfs 模块（%s）" % moddir

    tmpdir = tempfile.mkdtemp(prefix="btrfs-patch-")
    try:
        raw = os.path.join(tmpdir, "btrfs.ko")
        if is_xz:
            with open(raw, "wb") as out:
                r = subprocess.run(["xz", "-dc", src], stdout=out, stderr=subprocess.PIPE)
            if r.returncode != 0:
                return None, "解压失败: %s" % r.stderr.decode()[:200]
        else:
            shutil.copy2(src, raw)

        data = open(raw, "rb").read()
        patched, found = is_patched(data)

        if check_only:
            log("  %s: %s" % (kver, "已打补丁 ✓" if patched else "未打补丁 ✗"))
            for kind in ("root_flags_mask", "inode_flags_mask"):
                vals = [v for _, _, v in found.get(kind, [])]
                log("    %-18s %s" % (kind, ", ".join("0x%x" % v for v in vals) or "(未找到)"))
            return patched, (None if patched else "未打补丁")

        if patched:
            log("  %s: 已是补丁版，跳过" % kver)
            return True, None

        newdata, applied = apply_patch(data)
        if not applied:
            return None, ("未能定位到可补丁的掩码。该内核的 btrfs 内部结构可能已改变，"
                          "需人工分析新版 btrfs.ko 重新定位补丁点。")

        for line in applied:
            log("    " + line)

        newdata, sig_at = strip_signature(newdata)
        if sig_at:
            log("    已剥离 PKCS#7 签名尾 (@0x%x)" % sig_at)

        tmpko = os.path.join(tmpdir, "patched.ko")
        with open(tmpko, "wb") as f:
            f.write(newdata)

        # 备份原模块（只备份一次，保留飞牛原厂版本）
        if os.path.exists(ko_xz) and not os.path.exists(ko_xz + ".orig-backup"):
            shutil.copy2(ko_xz, ko_xz + ".orig-backup")
            log("    已备份原模块 -> %s.orig-backup" % ko_xz)

        # 压缩并原子替换
        outpath = ko_xz + ".new"
        with open(outpath, "wb") as out:
            r = subprocess.run(["xz", "-9", "-c", tmpko], stdout=out, stderr=subprocess.PIPE)
        if r.returncode != 0:
            os.unlink(outpath)
            return None, "压缩失败"
        os.replace(outpath, ko_xz)
        log("    已安装 -> %s" % ko_xz)

        # 保存未压缩版供 initrd 工具使用
        md5 = hashlib.md5(newdata).hexdigest()
        log("    md5=%s" % md5)
        if share_dir:
            try:
                os.makedirs(share_dir, exist_ok=True)
                keep = os.path.join(share_dir, "btrfs_patched_%s.ko" % kver)
                shutil.copy2(tmpko, keep)
                log("    已保存未压缩版 -> %s" % keep)
            except OSError as e:
                log("    ! 保存未压缩版失败: %s" % e)
        return True, None
    except Exception as e:
        return None, "处理异常: %s" % e
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="飞牛 fnOS + 群晖 btrfs 兼容补丁生成器")
    ap.add_argument("--kernel", required=True,
                    help="内核版本号，或 all（扫描所有已安装内核）")
    ap.add_argument("--check", action="store_true",
                    help="只检查补丁状态，不修改任何文件")
    ap.add_argument("--root", default="/lib/modules",
                    help="模块根目录（默认 /lib/modules）")
    ap.add_argument("--share-dir", default="/usr/local/share/fnos-btrfs-patch",
                    help="保存未压缩补丁模块的目录（供 initrd 工具使用）")
    args = ap.parse_args()

    if args.kernel == "all":
        try:
            kvers = sorted(d for d in os.listdir(args.root)
                           if os.path.isdir(os.path.join(args.root, d)))
        except OSError:
            log("无法读取 %s" % args.root)
            return 1
    else:
        kvers = [args.kernel]

    results = {}
    for kv in kvers:
        log("=== 内核 %s ===" % kv)
        ok, err = process_kernel(kv, args.root, args.check, args.share_dir)
        if err:
            log("  ✗ %s" % err)
        results[kv] = (ok, err)

    log("")
    log("=== 汇总 ===")
    for kv, (ok, err) in results.items():
        status = "OK" if ok else ("FAIL: %s" % (err or "未知"))
        log("  %-32s %s" % (kv, status))
    return 0 if all(v[0] for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
