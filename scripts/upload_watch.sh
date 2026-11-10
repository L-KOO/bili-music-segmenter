#!/bin/bash
# 监控 /vol2/1000/ipynb/inaupload：
# 待上传的切片如果连续 5 小时没有任何一个被成功上传（目录没被清掉），
# 说明 biliup 被 B 站的人机验证拦住了，推一条 PushPlus 提醒去手动传一个文件。
#
# 判断依据：
#   celery 每传完一场会用 shutil.rmtree 删掉整个目录，所以「目录数变少」
#   或者「最老的那个目录换人」就代表有进展；两者都没有超过 3 小时 = 卡住。
#   这样既不会被 40 分钟的上传间隔误报，也不会因为新任务进来而重置计时。
set -u

PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
UP=/vol2/1000/ipynb/inaupload
STATE=/vol2/1000/ipynb/.upload_watch.state
LOG=/vol2/1000/ipynb/scripts/upload_watch.log

# PushPlus token 放在同目录的 .pushplus.env 里（已加入 .gitignore，不会进仓库）。
# 文件内容就一行：PUSHPLUS_TOKEN=你的token
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.pushplus.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
TOKEN="${PUSHPLUS_TOKEN:-}"

STALL=$((5 * 3600))        # 多久没进展算卡住（大文件上传本身就要 2~3 小时）
COOLDOWN=$((6 * 3600))     # 同一轮卡住最多多久提醒一次
now=$(date +%s)

[ -d "$UP" ] || exit 0

# 日志防膨胀（原地截断，不换 inode，cron 的 O_APPEND 重定向仍然有效）
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ] && : > "$LOG"

# 收集还带 .mp4 的待上传目录（空目录不算）
pending=()
while IFS= read -r d; do
    [ -n "$d" ] || continue
    c=$(find "$d" -maxdepth 1 -type f -name '*.mp4' 2>/dev/null | wc -l)
    [ "$c" -gt 0 ] && pending+=("$d")
done < <(find "$UP" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)

count=${#pending[@]}

prev_count=""; prev_sig=""; prev_shrink=""; prev_alert=0
if [ -f "$STATE" ]; then
    read -r prev_count prev_sig prev_shrink prev_alert < "$STATE" 2>/dev/null || true
fi

if [ "$count" -eq 0 ]; then
    echo "0 - $now $now" > "$STATE"
    exit 0
fi

oldest="${pending[0]}"
oldest_name=$(basename "$oldest")
# 恢复脚本会给目录加 .retry<epoch> 后缀；改名重投本身不代表真有进展，
# 所以算签名前先把后缀去掉，否则计时被重置、告警最长推迟 STALL 小时。
oldest_base=$(printf '%s' "$oldest_name" | sed 's/\(\.retry[0-9]\+\)\+$//')
oldest_sig=$(printf '%s' "$oldest_base" | md5sum | cut -d' ' -f1)
oldest_ts=$(stat -c %Y "$oldest" 2>/dev/null || echo "$now")

# 有没有进展？
shrink=${prev_shrink:-}
if [ -z "$shrink" ] || [ "$shrink" = "-" ]; then
    shrink=$oldest_ts                       # 首次运行：以最老目录的创建时间为基准
elif [ "$count" -lt "${prev_count:-0}" ] || [ "$oldest_sig" != "${prev_sig:-}" ]; then
    shrink=$now                             # 有目录被成功上传，重新计时
fi

waited=$((now - shrink))
ttl=$((now - oldest_ts))
prev_alert=${prev_alert%% *}; prev_alert=${prev_alert:-0}

if [ "$waited" -ge "$STALL" ] && [ $((now - prev_alert)) -ge "$COOLDOWN" ]; then
    if [ -z "$TOKEN" ]; then
        echo "[$(date '+%F %T')] 该提醒了，但 $ENV_FILE 里没有 PUSHPLUS_TOKEN，跳过推送"
        exit 0
    fi
    size=$(du -sch "${pending[@]}" 2>/dev/null | tail -1 | cut -f1)
    title="歌切上传卡住了，需要手动过验证码"
    content=$(cat <<EOF
inaupload 已经 $((waited / 3600)) 小时没有文件被成功上传。

待上传：${count} 个目录，合计 ${size}
最久的一个：${oldest_name}
它已经等了约 $((ttl / 3600)) 小时。

处理办法：
打开 B 站创作中心手动上传任意一个文件，过掉人机验证，
之后队列会自动继续跑。

时间：$(date '+%Y-%m-%d %H:%M:%S')
EOF
)
    resp=$(curl -s -G --max-time 20 "https://www.pushplus.plus/send" \
        --data-urlencode "token=$TOKEN" \
        --data-urlencode "title=$title" \
        --data-urlencode "content=$content" \
        --data-urlencode "template=txt" 2>&1)
    echo "[$(date '+%F %T')] 触发提醒 waited=${waited}s pending=${count} resp=${resp}"
    prev_alert=$now
else
    echo "[$(date '+%F %T')] 待上传 ${count} 个，无进展 ${waited}s（阈值 ${STALL}s）"
fi

echo "$count $oldest_sig $shrink $prev_alert" > "$STATE"
