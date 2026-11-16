#!/usr/bin/env python3
"""
initrd 内嵌 btrfs.ko 替换工具（飞牛 fnOS 实测结构）

为什么需要
----------
飞牛的 initrd.img 里内嵌了一份**未压缩的** btrfs.ko（新版 dracut 生成的），
启动时 initramfs 阶段由 `scripts/local-premount/btrfs` 抢先 modprobe 加载。
一旦这份原版被加载，/lib/modules 里的补丁版就永远没有机会生效
（模块只能加载一次）。这就是"模块打了补丁却不生效"的根因。

因此**必须同时替换两处**：
  1. /lib/modules/<ver>/kernel/fs/btrfs/btrfs.ko.xz   （运行时模块）
  2. /boot/initrd.img-<ver> 内嵌的 btrfs.ko           （启动早期模块）

initrd 结构（飞牛实测）
----------------------
  layer0: microcode cpio（newc 格式，未压缩，仅含 CPU 微码）
  padding（若干 0 字节）
  layer1: zstd 压缩的 cpio（真正的根文件系统 + 内核模块）

本工具自动识别该结构，也可处理单层未压缩 cpio。

用法
----
  # 替换并写入（会先备份为 <initrd>.orig-backup）
  python3 ird_tool.py --initrd /boot/initrd.img-$(uname -r) \\
                      --ko /usr/local/share/fnos-btrfs-patch/btrfs_patched_$(uname -r).ko

  # 只看会做什么，不写入
  python3 ird_tool.py --initrd ... --ko ... --dry-run

  # 只检查 initrd 内 btrfs.ko 的 md5
  python3 ird_tool.py --initrd /boot/initrd.img-$(uname -r) --inspect

退出码：0 成功（含"已是目标版本"）；1 失败
"""
import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

MAGIC = b"070701"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b\x08"
BTRFS_KO_SUFFIX = b"kernel/fs/btrfs/btrfs.ko"


# ---------------------------------------------------------------- cpio

def parse_cpio(data, off=0):
    """
    解析 newc 格式 cpio 层。
    返回 (entries, end_offset)；entries 为 [[name, payload, fields13], ...]
    entries 末尾含 TRAILER!!!。
    """
    ents = []
    o = off
    n = len(data)
    while o + 110 <= n:
        if data[o:o + 6] != MAGIC:
            return None, None
        hdr = data[o + 6:o + 110]          # 13 个 8 字节十六进制字段
        try:
            f = [int(hdr[k * 8:(k + 1) * 8], 16) for k in range(13)]
        except ValueError:
            return None, None
        filesize = f[6]
        namesize = f[11]
        if namesize == 0 or namesize > 4096:
            return None, None
        name = data[o + 110:o + 110 + namesize - 1]
        dstart = o + 110 + namesize
        dstart += (4 - dstart % 4) % 4
        if dstart + filesize > n:
            return None, None
        ents.append([name, data[dstart:dstart + filesize], f])
        o = dstart + filesize
        o += (4 - o % 4) % 4
        if name == b"TRAILER!!!":
            return ents, o
    return None, None


def build_cpio(ents):
    """按 newc 格式重建 cpio 层（保持原 inode/mode/uid/gid/mtime）"""
    out = bytearray()
    for e in ents:
        name, payload, f = e[0], e[1], e[2]
        namefield = name + b"\x00"
        h = MAGIC
        h += b"%08x" % f[0]              # c_ino
        h += b"%08x" % f[1]              # c_mode
        h += b"%08x" % f[2]              # c_uid
        h += b"%08x" % f[3]              # c_gid
        h += b"%08x" % f[4]              # c_nlink
        h += b"%08x" % f[5]              # c_mtime
        h += b"%08x" % len(payload)      # c_filesize
        h += b"%08x" % f[7]              # c_devmajor
        h += b"%08x" % f[8]              # c_devminor
        h += b"%08x" % f[9]              # c_rdevmajor
        h += b"%08x" % f[10]             # c_rdevminor
        h += b"%08x" % len(namefield)    # c_namesize
        h += b"%08x" % f[12]             # c_check
        assert len(h) == 110
        out += h + namefield
        out += b"\x00" * ((4 - len(out) % 4) % 4)
        out += payload
        out += b"\x00" * ((4 - len(out) % 4) % 4)
    return bytes(out)


def find_cpio_layers(data, min_entries=5):
    """
    顺序遍历，返回 [(offset, ents, end), ...]

    注意：不能用「扫描所有 MAGIC 位置」的方式 —— cpio 内每个文件条目都以
    MAGIC 开头，会误判出大量重叠的「层」。必须从 0 开始顺序推进：
    解析一层 → 跳过 padding → 若下一个位置是 MAGIC 就继续，否则停止。
    """
    layers = []
    off = 0
    n = len(data)
    while off + 110 <= n:
        if data[off:off + 6] != MAGIC:
            break
        ents, end = parse_cpio(data, off)
        if not ents or len(ents) < min_entries:
            break
        layers.append((off, ents, end))
        nxt = end
        # 跳过 padding（层之间可能有 0 填充）
        while nxt < n and data[nxt] == 0:
            nxt += 1
        off = nxt
    return layers


def replace_btrfs_in_ents(ents, new_ko, new_md5):
    """在 entries 中替换 btrfs.ko，返回 (是否替换, 说明)"""
    for e in ents:
        if e[0].endswith(BTRFS_KO_SUFFIX):
            old_md5 = hashlib.md5(e[1]).hexdigest()
            if old_md5 == new_md5:
                return False, "btrfs.ko 已是目标版本 (md5=%s)" % old_md5
            e[1] = new_ko
            return True, "btrfs.ko 替换: %d bytes (md5=%s) -> %d bytes (md5=%s)" % (
                len(e[1]), old_md5, len(new_ko), new_md5)
    return False, "该层无 btrfs.ko"


# ---------------------------------------------------------------- 压缩层

def detect_compression(blob, max_skip=4096):
    """
    检测压缩格式。会跳过前导 0 字节（cpio 层之间的 padding，
    飞牛实测有 212 字节），最多跳 max_skip 字节。
    返回 (kind, offset) 或 (None, 0)
    """
    if not blob:
        return None, 0
    # 跳过前导 0（padding）
    off = 0
    limit = min(len(blob), max_skip)
    while off < limit and blob[off] == 0:
        off += 1
    head = blob[off:off + 8]
    if head.startswith(ZSTD_MAGIC):
        return "zstd", off
    if head.startswith(GZIP_MAGIC):
        return "gzip", off
    if head.startswith(b"\xfd7zXZ"):
        return "xz", off
    if head.startswith(b"\x04\x22\x4d\x18") or head.startswith(b"\x02\x21\x4c\x18"):
        return "lz4", off
    return None, 0


def decompress_to(blob, tmpdir):
    kind, skip = detect_compression(blob)
    if not kind:
        return None, None, "未知压缩格式"
    src = os.path.join(tmpdir, "layer.comp")
    dst = os.path.join(tmpdir, "layer.cpio")
    with open(src, "wb") as f:
        f.write(blob[skip:])          # 去掉 padding 再解压
    cmd = {"zstd": ["zstd", "-d", "-f", "-o", dst, src],
           "gzip": ["sh", "-c", "gzip -dc %s > %s" % (src, dst)],
           "xz": ["sh", "-c", "xz -dc %s > %s" % (src, dst)],
           "lz4": ["sh", "-c", "lz4 -dc %s > %s" % (src, dst)]}[kind]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not os.path.exists(dst):
        return None, kind, "解压失败: %s" % r.stderr.decode()[:200]
    return open(dst, "rb").read(), kind, None


def compress_from(raw, kind, tmpdir, level="-3"):
    src = os.path.join(tmpdir, "layer_new.cpio")
    dst = os.path.join(tmpdir, "layer_new.comp")
    with open(src, "wb") as f:
        f.write(raw)
    if kind == "zstd":
        cmd = ["zstd", "-f", level, "-T0", "-o", dst, src]
    elif kind == "gzip":
        cmd = ["sh", "-c", "gzip -c %s > %s" % (src, dst)]
    elif kind == "xz":
        cmd = ["sh", "-c", "xz -c %s > %s" % (src, dst)]
    elif kind == "lz4":
        cmd = ["sh", "-c", "lz4 -c %s > %s" % (src, dst)]
    else:
        return None
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not os.path.exists(dst):
        return None
    return open(dst, "rb").read()


# ---------------------------------------------------------------- 主流程

def process(initrd, ko_path=None, dry_run=False, inspect_only=False):
    if not os.path.exists(initrd):
        print("✗ initrd 不存在: %s" % initrd)
        return False
    data = open(initrd, "rb").read()
    print("initrd : %s (%d bytes)" % (initrd, len(data)))

    new_ko = None
    new_md5 = None
    if ko_path:
        if not os.path.exists(ko_path):
            print("✗ 补丁模块不存在: %s" % ko_path)
            return False
        new_ko = open(ko_path, "rb").read()
        new_md5 = hashlib.md5(new_ko).hexdigest()
        print("目标模块: %s (%d bytes, md5=%s)" % (ko_path, len(new_ko), new_md5))

    layers = find_cpio_layers(data)
    print("cpio 层 : 找到 %d 个" % len(layers))
    for (off, ents, end) in layers:
        has = any(e[0].endswith(BTRFS_KO_SUFFIX) for e in ents)
        print("  @0x%-10x %5d 条目  %s" % (off, len(ents), "含 btrfs.ko" if has else ""))

    if inspect_only:
        found_any = False
        # 1) 未压缩层里的 btrfs.ko
        for (off, ents, end) in layers:
            for e in ents:
                if e[0].endswith(BTRFS_KO_SUFFIX):
                    found_any = True
                    print("  → btrfs.ko @层0x%x: %d bytes, md5=%s"
                          % (off, len(e[1]), hashlib.md5(e[1]).hexdigest()))
        # 2) 压缩层里的 btrfs.ko
        if layers:
            cursor = layers[-1][0] + layers[-1][2]
            rest = data[cursor:]
            kind, skip = detect_compression(rest)
            if kind:
                tmpdir = tempfile.mkdtemp(prefix="ird-insp-")
                try:
                    raw, k, err = decompress_to(rest, tmpdir)
                    if err:
                        print("  ! %s 层解压失败: %s" % (kind, err))
                    else:
                        print("  [%s 压缩层 @+0x%x]" % (kind, cursor + skip))
                        inner_layers = find_cpio_layers(raw)
                        for (off2, ents2, end2) in inner_layers:
                            for e in ents2:
                                if e[0].endswith(BTRFS_KO_SUFFIX):
                                    found_any = True
                                    print("    → btrfs.ko: %d bytes, md5=%s"
                                          % (len(e[1]), hashlib.md5(e[1]).hexdigest()))
                        if inner_layers:
                            print("    层内 %d 个条目" % len(inner_layers[0][1]))
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
        if not found_any:
            print("  （未找到 btrfs.ko）")
        return True

    if new_ko is None:
        print("✗ 需要 --ko 参数")
        return False

    tmpdir = tempfile.mkdtemp(prefix="ird-")
    try:
        out = bytearray()
        cursor = 0
        replaced_any = False

        for idx, (off, ents, end) in enumerate(layers):
            out += data[cursor:off]              # 层之间的 padding 原样保留
            layer_bytes = data[off:end]

            # 先看这层是否有 btrfs.ko（未压缩层）
            if any(e[0].endswith(BTRFS_KO_SUFFIX) for e in ents):
                changed, msg = replace_btrfs_in_ents(ents, new_ko, new_md5)
                print("  层@0x%x: %s" % (off, msg))
                if changed:
                    layer_bytes = build_cpio(ents)
                    replaced_any = True
            else:
                print("  层@0x%x: 无 btrfs.ko（%d 条目）" % (off, len(ents)))

            out += layer_bytes
            cursor = off + end

            # 该层之后若是压缩块（可能带前导 padding），是真正的根 fs 层
            rest = data[cursor:]
            kind, skip = detect_compression(rest) if rest else (None, 0)
            if kind:
                print("  检测到 %s 压缩层 @+0x%x（padding %d 字节）"
                      % (kind, cursor + skip, skip))
                raw, k, err = decompress_to(rest, tmpdir)
                if err:
                    print("    ! %s，原样保留" % err)
                    out += rest
                    cursor = len(data)
                    continue
                inner = find_cpio_layers(raw)
                changed = False
                for (off2, ents2, end2) in inner:
                    if any(e[0].endswith(BTRFS_KO_SUFFIX) for e in ents2):
                        ch, msg = replace_btrfs_in_ents(ents2, new_ko, new_md5)
                        print("    %s 内层: %s" % (k, msg))
                        if ch:
                            raw = build_cpio(ents2)
                            changed = True
                            replaced_any = True
                if changed:
                    newcomp = compress_from(raw, k, tmpdir)
                    if newcomp:
                        out += rest[:skip] + newcomp   # 保留 padding
                        print("    已重新压缩 (%d -> %d bytes)" % (len(rest) - skip, len(newcomp)))
                    else:
                        print("    ! 重新压缩失败，原样保留")
                        out += rest
                else:
                    out += rest
                cursor = len(data)

        out += data[cursor:]

        if not replaced_any:
            print("\n无需修改：initrd 内 btrfs.ko 已是目标版本")
            return True

        if dry_run:
            print("\n[dry-run] 未写入。预计大小 %d bytes（原 %d）" % (len(out), len(data)))
            return True

        # 备份 + 原子写入
        backup = initrd + ".orig-backup"
        if not os.path.exists(backup):
            shutil.copy2(initrd, backup)
            print("\n已备份原 initrd -> %s" % backup)
        tmp_out = initrd + ".new"
        with open(tmp_out, "wb") as f:
            f.write(bytes(out))
        os.replace(tmp_out, initrd)
        print("已写入 %s (%d bytes，原 %d)" % (initrd, len(out), len(data)))
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="替换 initrd 内嵌的 btrfs.ko")
    ap.add_argument("--initrd", required=True, help="initrd 路径")
    ap.add_argument("--ko", help="补丁版 btrfs.ko（未压缩）")
    ap.add_argument("--dry-run", action="store_true", help="不写入，只报告")
    ap.add_argument("--inspect", action="store_true",
                    help="只列出 initrd 内的 btrfs.ko 信息")
    args = ap.parse_args()
    ok = process(args.initrd, args.ko, args.dry_run, args.inspect)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
