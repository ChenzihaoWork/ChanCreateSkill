#!/bin/bash
# =============================================================================
# 飞牛 fnOS 升级后 —— 群晖 btrfs 补丁自愈脚本
#
# 解决的问题
# ----------
# 飞牛升级（尤其换内核）后会发生两件事：
#   1. /lib/modules/<新内核>/kernel/fs/btrfs/btrfs.ko.xz 被换成未打补丁的原版
#   2. /boot/initrd.img-<新内核> 里也内嵌了未打补丁的 btrfs.ko
# 结果：群晖卷挂不上，报 "invalid root flags, have 0x400000000 expect mask 0x1000000000001"
#
# 注意：这不会导致飞牛系统起不来 —— 补丁只放宽群晖私有 flag 的校验，
#       不影响标准 btrfs 功能，飞牛系统盘 /vol1 照常挂载。坏的只是群晖卷。
#
# 本脚本做什么
# ------------
#   1. 检查当前内核的 btrfs.ko 是否已打补丁
#   2. 若未打，自动定位三处掩码并打补丁（按特征值扫描，不依赖固定地址）
#   3. 同步替换 initrd 内嵌的 btrfs.ko（不做这步补丁等于白打）
#   4. 确保 bind 挂载服务在运行
#
# 部署位置
# --------
#   /usr/local/sbin/fnos-btrfs-patch-watchdog.sh
#   /usr/local/share/fnos-btrfs-patch/gen_btrfs_patch.py
#   /usr/local/share/fnos-btrfs-patch/ird_tool.py
#
# 配套 systemd（注意都用 graphical.target）
# -----------------------------------------
#   fnos-btrfs-patch-guard.service   开机跑一次 + 被定时器触发
#   fnos-btrfs-patch-watchdog.timer  每 30 分钟跑一次
# =============================================================================
set -u

KVER="$(uname -r)"
LOG=/var/log/fnos-btrfs-patch.log
TOOLS=/usr/local/share/fnos-btrfs-patch
MODDIR="/lib/modules/${KVER}/kernel/fs/btrfs"
STATUS=/var/run/fnos-btrfs-patch.status

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "===== 群晖 btrfs 补丁自愈检查 (kernel=${KVER}) ====="

PATCHED=no
BINDS=0
NEED_REBOOT=no

# ---------------------------------------------------------------- 1. 检查
if [ ! -f "${TOOLS}/gen_btrfs_patch.py" ]; then
    log "✗ 找不到补丁生成器 ${TOOLS}/gen_btrfs_patch.py —— 部署不完整"
    exit 0
fi

CHECK_OUT=$(python3 "${TOOLS}/gen_btrfs_patch.py" --check --kernel "${KVER}" 2>&1)
echo "$CHECK_OUT" | tee -a "$LOG"
echo "$CHECK_OUT" | grep -q "已打补丁" && PATCHED=yes

# ---------------------------------------------------------------- 2. 打补丁
if [ "$PATCHED" = "no" ]; then
    log "⚠ 当前内核 btrfs 模块未打补丁（很可能是升级换了内核），尝试自动修复…"

    if python3 "${TOOLS}/gen_btrfs_patch.py" --kernel "${KVER}" 2>&1 | tee -a "$LOG"; then
        PATCHED=yes
        NEED_REBOOT=yes
        log "✓ 补丁模块已重新生成"

        # --- 3. 同步 initrd（关键！否则 initrd 抢先加载原版，补丁无效）---
        INITRD="/boot/initrd.img-${KVER}"
        KO_RAW="${TOOLS}/btrfs_patched_${KVER}.ko"

        if [ -f "$INITRD" ] && [ -f "$KO_RAW" ]; then
            if [ ! -f "${TOOLS}/ird_tool.py" ]; then
                log "! 缺少 ird_tool.py，跳过 initrd 同步（补丁重启后可能无效）"
            else
                if python3 "${TOOLS}/ird_tool.py" --initrd "$INITRD" --ko "$KO_RAW" 2>&1 | tee -a "$LOG"; then
                    log "✓ initrd 内 btrfs.ko 已同步为补丁版"
                else
                    log "✗ initrd 同步失败 —— 重启后群晖卷可能仍挂不上，需人工处理"
                fi
            fi
        else
            log "! initrd($INITRD) 或未压缩补丁模块($KO_RAW) 不存在"
        fi
    else
        log "✗ 补丁生成失败 —— 新版内核的 btrfs 内部结构可能已改变，需人工适配"
        log "  （群晖卷将无法挂载，但飞牛系统本身不受影响）"
        PATCHED=no
    fi
else
    log "✓ 当前内核 btrfs 模块已打补丁，无需处理"
fi

# ---------------------------------------------------------------- 4. bind 挂载
if systemctl is-enabled fnos-synology-binds.service >/dev/null 2>&1; then
    if ! systemctl is-active fnos-synology-binds.service >/dev/null 2>&1; then
        log "↻ bind 挂载服务未运行，尝试启动"
        systemctl start fnos-synology-binds.service >>"$LOG" 2>&1
    fi
fi

BINDS=$(grep ' /vol' /proc/self/mountinfo 2>/dev/null | grep -c '/1000/' || true)

# ---------------------------------------------------------------- 5. 状态
{
    echo "kernel=${KVER}"
    echo "btrfs_patched=${PATCHED}"
    echo "bind_mounts=${BINDS}"
    echo "need_reboot=${NEED_REBOOT}"
    echo "checked_at=$(date '+%F %T')"
} > "$STATUS" 2>/dev/null

# ---------------------------------------------------------------- 6. 提示
if [ "$PATCHED" = "yes" ] && [ "$NEED_REBOOT" = "yes" ]; then
    log ""
    log "⚠ 补丁已就绪，但当前运行中的内核仍是未打补丁的旧模块。"
    log "  需要重启后生效：  reboot"
    log "  （重启前群晖卷处于未挂载状态，属正常）"
fi

log "===== 检查完成 (patched=${PATCHED}, binds=${BINDS}) ====="
exit 0
