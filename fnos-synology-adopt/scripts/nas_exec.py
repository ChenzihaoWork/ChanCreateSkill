#!/usr/bin/env python3
"""
NAS 远程执行助手（通过 SSH 以 root 执行脚本）

为什么需要
----------
飞牛 NAS 的操作必须在机器上以 root 执行。本工具让 Agent 能用一条命令
把本地脚本推上去执行，无需反复手输 SSH。

依赖：pip install paramiko

用法
----
  # 执行单条命令
  python3 nas_exec.py --host 192.168.1.10 --user admin --password 'xxx' \
      --cmd "uname -r"

  # 执行本地脚本文件（自动 sudo 提权）
  python3 nas_exec.py --host ... --user admin --password 'xxx' \
      --script ./my_script.sh

  # 上传文件到远端
  python3 nas_exec.py --host ... --user admin --password 'xxx' \
      --upload ./local.py:/tmp/remote.py

  # 从远端下载文件
  python3 nas_exec.py --host ... --user admin --password 'xxx' \
      --download /var/log/fnos-btrfs-patch.log:./patch.log

  # 用 SSH 私钥而非密码
  python3 nas_exec.py --host ... --user root --key ~/.ssh/id_rsa --cmd "..."
"""
import argparse
import os
import sys

try:
    import paramiko
except ImportError:
    print("需要 paramiko：pip install paramiko", file=sys.stderr)
    sys.exit(1)


def connect(args):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {
        "hostname": args.host,
        "port": args.port,
        "username": args.user,
        "timeout": args.timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if args.key:
        kw["key_filename"] = os.path.expanduser(args.key)
    else:
        kw["password"] = args.password
    c.connect(**kw)
    return c


def run_cmd(c, cmd, sudo=False, timeout=300):
    if sudo:
        cmd = "sudo -n bash -c %s" % shquote(cmd)
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    ap = argparse.ArgumentParser(description="NAS 远程执行助手")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password")
    ap.add_argument("--key", help="SSH 私钥路径")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--sudo", action="store_true", help="用 sudo 执行（需免密）")
    ap.add_argument("--cmd", help="要执行的命令")
    ap.add_argument("--script", help="要上传并执行的本地脚本")
    ap.add_argument("--upload", help="本地路径:远端路径")
    ap.add_argument("--download", help="远端路径:本地路径")
    ap.add_argument("--exec-timeout", type=int, default=600,
                    help="命令执行超时（秒）")
    args = ap.parse_args()

    if not args.password and not args.key:
        print("需要 --password 或 --key", file=sys.stderr)
        return 2

    c = connect(args)
    try:
        # 上传/下载
        if args.upload:
            local, remote = args.upload.split(":", 1)
            sftp = c.open_sftp()
            sftp.put(os.path.expanduser(local), remote)
            sftp.close()
            print("✓ 已上传 %s -> %s" % (local, remote))

        if args.download:
            remote, local = args.download.split(":", 1)
            sftp = c.open_sftp()
            sftp.get(remote, os.path.expanduser(local))
            sftp.close()
            print("✓ 已下载 %s -> %s" % (remote, local))

        # 执行脚本
        if args.script:
            local = os.path.expanduser(args.script)
            name = os.path.basename(local)
            remote = "/tmp/%s" % name
            sftp = c.open_sftp()
            sftp.put(local, remote)
            sftp.close()
            rc, out, err = run_cmd(c, "chmod +x %s && %s %s" %
                                   (shquote(remote), shquote(remote), ""),
                                   sudo=args.sudo, timeout=args.exec_timeout)
            sys.stdout.write(out)
            if err:
                sys.stderr.write("--- STDERR ---\n" + err)
            return rc

        # 执行命令
        if args.cmd:
            rc, out, err = run_cmd(c, args.cmd, sudo=args.sudo,
                                   timeout=args.exec_timeout)
            sys.stdout.write(out)
            if err:
                sys.stderr.write("--- STDERR ---\n" + err)
            return rc

        ap.print_help()
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
