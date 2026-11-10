#!/bin/bash
# 上传失败兜底：任务重试耗尽死掉之后，把它的目录记下来，
# 等到上传通道恢复正常（出现一次成功上传）再重新排进队列。
#
# 为什么不能"看到成功就立刻补投所有剩留目录"：
#   剩下的目录通常只是正常排着队，直接补投会让原任务和补投任务互相踩，
#   白烧 15 次重试。所以必须先确认「这个目录的任务真的死了」——
#   信号是 celery 日志里的 MaxRetryReached / WorkerLostError。
#
# 触发重投（两个条件都要满足）：
#   1. 存在已判死的孤儿目录
#   2. 最近 2 小时有过成功上传（通道正常）  或  距离上次重投超过 3 小时（兜底）
#   另外要求此刻没有 biliup 在跑，避免和正在进行的上传抢。
set -u

PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
UP=/vol2/1000/ipynb/inaupload
STATE=/vol2/1000/ipynb/.upload_recover.state
ORPHANS=/vol2/1000/ipynb/.upload_recover.orphans
SINCE=/vol2/1000/ipynb/.upload_recover.since
LOG=/vol2/1000/ipynb/scripts/upload_recover.log
LOCK=/tmp/upload_pipeline.lock
PY=/inaseg/scripts/upload_requeue.py     # 容器内路径（/inaseg 就是 /vol2/1000/ipynb）

MIN_AGE=3600        # 目录至少存在 60 分钟才考虑
RECENT_WIN=7200     # 2 小时内有过成功上传 -> 通道正常
FORCE_EVERY=10800   # 否则每 3 小时兜底一次
now=$(date +%s)

[ -d "$UP" ] || exit 0
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ] && : > "$LOG"

exec 9>"$LOCK"
flock -n 9 || exit 0

log() { echo "[$(date '+%F %T')] $*"; }

# ---- 当前目录 ----
cur=()
while IFS= read -r d; do
    [ -n "$d" ] || continue
    [ -f "$d/cmd.txt" ] && cur+=("$(basename "$d")")
done < <(find "$UP" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
count=${#cur[@]}

prev_count=0; last_success=0; last_force=0
[ -f "$STATE" ] && read -r prev_count last_success last_force < "$STATE"
if [ "${last_force:-0}" -eq 0 ]; then
    last_force=$now
    last_success=$((now - RECENT_WIN - 1))
fi

# ---- 成功判定：目录数变少 ----
if [ "$count" -lt "${prev_count:-0}" ]; then
    last_success=$now
    log "检测到成功上传：目录 ${prev_count} -> ${count}"
fi

# ---- 扫描任务死亡事件 ----
scan_from=$(cat "$SINCE" 2>/dev/null || echo $((now - 3600)))
deaths=$(docker logs --since "$scan_from" ipnby-inacelery-1 2>&1 \
         | grep -acE "MaxRetryReached|WorkerLostError|raised unexpected" || true)
echo "$now" > "$SINCE"

touch "$ORPHANS"
if [ "${deaths:-0}" -gt 0 ]; then
    log "发现 ${deaths} 次任务死亡事件，登记最老的未记账目录"
    for i in $(seq 1 "$deaths"); do
        for name in "${cur[@]}"; do
            grep -qxF "$name" "$ORPHANS" && continue
            echo "$name" >> "$ORPHANS"
            log "  登记孤儿: $name"
            break
        done
    done
fi

# ---- 清掉已经不存在的孤儿 ----
tmp=$(mktemp)
while IFS= read -r n; do
    [ -n "$n" ] || continue
    [ -d "$UP/$n" ] && [ -f "$UP/$n/cmd.txt" ] && echo "$n" >> "$tmp"
done < "$ORPHANS"
mv "$tmp" "$ORPHANS"
orphan_n=$(grep -c . "$ORPHANS" 2>/dev/null || true)
orphan_n=${orphan_n:-0}

echo "$count $last_success $last_force" > "$STATE"

if [ "$orphan_n" -eq 0 ]; then
    log "待上传 ${count} 个，无孤儿，正常（成功距今 $(((now - last_success) / 60)) 分）"
    exit 0
fi

# ---- 判断现在能不能补投 ----
if [ $((now - last_success)) -le "$RECENT_WIN" ]; then
    why="最近有成功上传，通道正常"
elif [ $((now - last_force)) -ge "$FORCE_EVERY" ]; then
    why="兜底超时"
else
    log "孤儿 ${orphan_n} 个，但通道未恢复（成功距今 $(((now - last_success) / 60)) 分），等"
    exit 0
fi

if docker exec ipnby-inacelery-1 sh -c 'pgrep -c biliup' >/dev/null 2>&1; then
    log "孤儿 ${orphan_n} 个，但此刻有 biliup 在跑，下一轮再说"
    exit 0
fi

log "触发重投：${why}，孤儿 ${orphan_n} 个"
out=$(cat "$ORPHANS" | docker exec -i -w /inaseg ipnby-inacelery-1 \
        python "$PY" "$MIN_AGE" 0 2>/tmp/upload_recover.err)
echo "$out" | sed "s/^/[$(date '+%F %T')] 结果: /"
err=$(tail -3 /tmp/upload_recover.err 2>/dev/null)
[ -n "$err" ] && log "stderr: $err"

if echo "$out" | grep -q '\["'; then
    : > "$ORPHANS"
    echo "$count $last_success $now" > "$STATE"
fi
