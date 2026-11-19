#!/bin/bash
############################################
# 统一录播合并 / 移动脚本  v1.0
#
# 替代原来的 4 个任务：
#   yss(03) / 合并视频(05) / 凤梨移动(09) / 水姐移动(10) / 录播合并(11)
#
# 统一后的行为：
#   1. 扫描每个来源目录，按「主播 + 归属日期」分组（00:00~05:59 归前一天）
#   2. 组内全部碎片逐个转成 MPEG-TS（只换容器，不重编码）
#   3. TS 拼接 → 一个 mp4（关键：直接拼 mp4 会把时间轴拉长、丢帧）
#   4. 校验：成品时长必须 ≈ 各碎片时长之和（0.90 ~ 1.10）
#      不达标就自动重编码兜底，仍不达标则保留碎片、下轮重试
#   5. 跨卷安全移动：先 copy 到目标盘临时名 → 校验大小 → 改名
#   6. 输出命名统一：<主播>_<YYYY-MM-DD>_<HH-MM-SS>_merged.mp4
#
# 兼容的录制软件命名（自动识别，不用改代码）：
#   StreamCap   : 主播_2026-11-02_19-04-19_000.mp4   (带 _序号)
#   BiliLive    : 2026-11-03 15-05-52 标题.mp4       (日期 空格 时间)
#   BiliLive    : 标题_2026-11-03_15-05-52.mp4
#   抖音/快手   : 主播_20261103_150552.mp4           (紧凑格式)
#   任何格式    : 识别不到时间戳时，按目录名 + 文件 mtime 归组
############################################

############################################
# 一、来源配置
#
# 每行格式：  名字|源目录|目标目录|模式
#   模式 flat = 直接扫这个目录
#   模式 tree = 这个根目录下每个子目录各算一个主播，分别扫
#
# 新增录制软件 = 加一行，不用动下面的逻辑
############################################

SOURCES=(
    # StreamCap（哔哩哔哩）：根目录下每个主播一个子目录
    "StreamCap|/vol2/1000/acfun/StreamCap/downloads/哔哩哔哩|/vol1/1000/biliup/data/rec/merged|tree"

    # BiliLive-tools（御司司）
    "BiliLive|/vol1/1000/biliLive-tools/video/Bilibili|/vol1/1000/biliup/data/rec/merged|tree"

    # 抖音 · 盐水凤梨
    "抖音|/vol2/1000/douyin/downloads/抖音直播|/vol1/1000/biliup/data/rec/merged|tree"

    # 抖音 · Acfun 1001Project
    "Acfun|/vol2/1000/douyin/downloads/Acfun|/vol1/1000/biliup/data/rec/merged|tree"

    # 以后换录制软件，在这里加：
    # "新软件|/path/to/downloads/主播名|/vol1/1000/biliup/data/rec/merged|flat"
)

# 需要排除的目录（填绝对路径，整个目录跳过）
EXCLUDE_DIRS=(
    "/vol1/1000/biliLive-tools/video/Bilibili/-林夜子-"
)

############################################
# 二、通用配置
############################################

WORK_DIR="/vol1/1000/biliup/data/rec/.rec_work"
STATE_DIR="$WORK_DIR/state"
TMP_DIR="$WORK_DIR/tmp"
LOG_FILE="$WORK_DIR/rec_merge_unified.log"
LOCK_FILE="/tmp/rec_merge_unified.lock"

# 文件连续多少秒不变才认为录制结束
STABLE_SECONDS=90
# 组内最后一个碎片额外等待（防止边写边合）
PART_GRACE_SECONDS=180

# 凌晨归属：00:00 ~ (N-1):59 算前一天，0 = 不跨天
EARLY_HOUR_CUTOFF=6

# 同一主播同一天内，相邻两段之间的断档超过这个分钟数才切成两个文件
#   0  = 不切（同一天永远合成一份，当前采用）
#   45 = 中间断档 45 分钟以上才拆开
MAX_GAP_MINUTES=0

# 输出文件名后缀
OUTPUT_SUFFIX=""

# 识别不到的扩展名一律忽略；这些才会处理
VIDEO_EXTS="mp4 mkv flv ts m4s"

# 只保留这份名单里的文件（防止把 .part/.tmp 当成分片）
IGNORE_PATTERNS="*.part *.tmp *.!qB *.merge* *_merged.*"

# 成品时长 / 碎片时长之和 的允许偏差
DURATION_RATIO_MIN=0.90
DURATION_RATIO_MAX=1.10

# 单个碎片转 TS 的超时（秒）
TS_TIMEOUT=1800
# 整组拼接的超时（秒）
MERGE_TIMEOUT=7200
# 兜底重编码的超时（秒）
ENCODE_TIMEOUT=21600

# 重编码参数（只在时间轴校验不过时用）
FIX_PRESET="ultrafast"
FIX_CRF=23
FIX_AUDIO_BITRATE="192k"

# 文件仍被进程打开时跳过（1=开）
CHECK_OPEN=${CHECK_OPEN:-1}
# 演练模式：只打印计划，不动文件
DRY_RUN=${DRY_RUN:-0}

FFMPEG="ffmpeg"
FFPROBE="ffprobe"

export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
shopt -s nullglob

############################################
# 三、基础函数
############################################

log(){
    local msg="$(date '+%F %T') $1"
    echo "$msg"
    [ -n "${LOG_FILE:-}" ] && echo "$msg" >> "$LOG_FILE" 2>/dev/null
}

init_dirs(){
    mkdir -p "$WORK_DIR" "$STATE_DIR" "$TMP_DIR" || {
        echo "无法创建工作目录 $WORK_DIR" >&2
        exit 1
    }
}

# 全局锁：同一时间只跑一份
acquire_lock(){
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        log "[跳过] 已有实例在运行（锁 $LOCK_FILE）"
        exit 0
    fi
}

state_file(){
    local f="$1"
    local h
    h=$(printf '%s' "$f" | md5sum | awk '{print $1}')
    echo "$STATE_DIR/${h}.state"
}

remove_state(){ rm -f -- "$(state_file "$1")"; }

is_open(){
    local f="$1"
    command -v fuser >/dev/null 2>&1 || return 1
    fuser "$f" >/dev/null 2>&1
}

# 稳定判定：size 连续 STABLE_SECONDS 秒不变，且不是被进程占用
is_stable(){
    local f="$1"
    [ -f "$f" ] || return 1

    local size mtime now sf
    size=$(stat -c%s "$f" 2>/dev/null) || return 1
    mtime=$(stat -c%Y "$f" 2>/dev/null) || return 1
    now=$(date +%s)

    # 只有"最近还在动"的文件才查占用；老文件一律跳过，避免 fuser 误报把任务卡死
    if (( CHECK_OPEN == 1 )) && (( now - mtime < 600 )) && is_open "$f"; then
        log "  [占用] 文件仍在写：$(basename "$f")"
        return 1
    fi

    sf=$(state_file "$f")

    # 老文件（mtime 已经超过稳定阈值）直接判稳定，不依赖状态文件
    if (( now - mtime >= STABLE_SECONDS )); then
        echo "$size $mtime $f" > "$sf" 2>/dev/null
        return 0
    fi

    if [ ! -f "$sf" ]; then
        # 老文件（mtime 已经超过稳定阈值）直接以 mtime 作为起点，不用再等
        if (( now - mtime >= STABLE_SECONDS )); then
            echo "$size $mtime $f" > "$sf"
        else
            echo "$size $now $f" > "$sf"
            return 1
        fi
    fi

    local last_size stable_start
    read -r last_size stable_start < "$sf" 2>/dev/null
    [[ "$last_size" =~ ^[0-9]+$ && "$stable_start" =~ ^[0-9]+$ ]] || {
        echo "$size $now $f" > "$sf"; return 1;
    }

    if [ "$size" != "$last_size" ] || [ "$mtime" -lt "$stable_start" ]; then
        echo "$size $now $f" > "$sf"
        return 1
    fi

    (( now - stable_start >= STABLE_SECONDS ))
}

# 组内最后一段还要再多等一会儿，避免刚切段就合并
is_ready(){
    local last="$1"
    # 老文件（mtime 已超过 稳定+宽限）直接放行
    local mt now
    mt=$(stat -c%Y "$last" 2>/dev/null) || mt=0
    now=$(date +%s)
    if (( mt > 0 && now - mt >= STABLE_SECONDS + PART_GRACE_SECONDS )); then
        return 0
    fi

    is_stable "$last" || return 1
    local sf stable_start now
    sf=$(state_file "$last")
    read -r _ stable_start < "$sf" 2>/dev/null
    [[ "$stable_start" =~ ^[0-9]+$ ]] || return 1
    now=$(date +%s)
    (( now - stable_start >= STABLE_SECONDS + PART_GRACE_SECONDS ))
}

ext_of(){
    case "${1,,}" in
        *.mp4) echo mp4 ;;
        *.mkv) echo mkv ;;
        *.flv) echo flv ;;
        *.ts)  echo ts ;;
        *.m4s) echo m4s ;;
        *)     echo "" ;;
    esac
}

is_video(){
    local e; e=$(ext_of "$1")
    [ -n "$e" ] || return 1
    case " $VIDEO_EXTS " in *" $e "*) return 0 ;; *) return 1 ;; esac
}

is_ignored(){
    local n="$1" p
    for p in $IGNORE_PATTERNS; do
        case "$n" in $p) return 0 ;; esac
    done
    return 1
}

############################################
# 四、命名解析
#
# 输出：流主|日期|时间   解析不到则输出空
#   StreamCap  主播_2026-11-02_19-04-19_000.mp4   → 主播|2026-11-02|19-04-19
#   BiliLive   2026-11-03 15-05-52 标题.mp4       → |2026-11-03|15-05-52
#   紧凑       主播_20261103_150552.mp4           → 主播|2026-11-03|15-05-52
############################################

parse_name(){
    local base="$1"
    base="${base%.*}"
    # 空格统一换成下划线，避免 [[ =~ ]] 把空格当分隔符
    local b="${base// /_}"

    # 1) 主播_YYYY-MM-DD_HH-MM-SS[_序号]
    if [[ "$b" =~ ^(.*)_([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{2}-[0-9]{2}-[0-9]{2})(_[0-9]+)?$ ]]; then
        local s="${BASH_REMATCH[1]}"
        s="${s%_}"; s="${s%-}"
        echo "${s}|${BASH_REMATCH[2]}|${BASH_REMATCH[3]}"
        return 0
    fi

    # 2) 以 YYYY-MM-DD_HH-MM-SS 开头（biliup / BiliLive-tools，后面可带标题）
    if [[ "$b" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{2})-([0-9]{2})-([0-9]{2})(.*)$ ]]; then
        echo "|${BASH_REMATCH[1]}|${BASH_REMATCH[2]}-${BASH_REMATCH[3]}-${BASH_REMATCH[4]}"
        return 0
    fi

    # 3) 主播_YYYYMMDD_HHMMSS[_序号]
    if [[ "$b" =~ ^(.*)_([0-9]{4})([0-9]{2})([0-9]{2})_([0-9]{2})([0-9]{2})([0-9]{2})(_[0-9]+)?$ ]]; then
        local s="${BASH_REMATCH[1]}"
        s="${s%_}"; s="${s%-}"
        echo "${s}|${BASH_REMATCH[2]}-${BASH_REMATCH[3]}-${BASH_REMATCH[4]}|${BASH_REMATCH[5]}-${BASH_REMATCH[6]}-${BASH_REMATCH[7]}"
        return 0
    fi

    # 4) 纯 YYYYMMDD_HHMMSS
    if [[ "$b" =~ ^([0-9]{4})([0-9]{2})([0-9]{2})_([0-9]{2})([0-9]{2})([0-9]{2})$ ]]; then
        echo "|${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]}|${BASH_REMATCH[4]}-${BASH_REMATCH[5]}-${BASH_REMATCH[6]}"
        return 0
    fi

    echo ""
    return 1
}

# 归属日期：凌晨 EARLY_HOUR_CUTOFF 点前算前一天
owned_date(){
    local date_str="$1" hour="$2"
    if (( 10#$hour < EARLY_HOUR_CUTOFF )); then
        date -d "$date_str -1 day" +%Y-%m-%d 2>/dev/null || echo "$date_str"
    else
        echo "$date_str"
    fi
}

# 组键：主播名 + 归属日期
group_key_of(){
    local file="$1" base dirname parsed s d t hour
    base="${file##*/}"
    dirname=$(basename "$(dirname "$file")")
    parsed=$(parse_name "$base") || parsed=""
    if [ -n "$parsed" ]; then
        s="${parsed%%|*}"
        local rest="${parsed#*|}"
        d="${rest%%|*}"
        t="${rest##*|}"
        [ -z "$s" ] && s="$dirname"
        hour="${t:0:2}"
        echo "${s}__$(owned_date "$d" "$hour")"
    else
        # 解析不到时间：用目录 + 文件 mtime 的日期
        d=$(date -d "@$(stat -c%Y "$file" 2>/dev/null || date +%s)" +%Y-%m-%d)
        echo "${dirname}__${d}"
    fi
}

############################################
# 五、合并引擎
############################################

# 碎片 → TS（只换容器）
to_ts(){
    local src="$1" dst="$2"
    case "${src,,}" in
        *.ts|*.flv)
            # 已经是 TS 系（annexb / ADTS），直接换容器
            timeout "$TS_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel error -y \
                -i "$src" -c copy -f mpegts "$dst" 2>>"$LOG_FILE"
            ;;
        *)
            timeout "$TS_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel error -y \
                -i "$src" -c copy -bsf:v h264_mp4toannexb -f mpegts "$dst" 2>>"$LOG_FILE"
            ;;
    esac
}

# 时长（秒，保留 3 位）
duration_of(){
    local f="$1" d
    d=$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)
    [[ "$d" =~ ^[0-9]+(\.[0-9]+)?$ ]] && echo "$d" || echo ""
}

# 校验成品：可读 + 时长 ≈ 碎片之和
validate_output(){
    local out="$1"; shift
    local files=("$@")

    local d_out; d_out=$(duration_of "$out")
    [ -n "$d_out" ] || { log "  [校验] 成品读不出时长"; return 1; }

    local expected=0 d f
    for f in "${files[@]}"; do
        d=$(duration_of "$f")
        [ -n "$d" ] || { log "  [校验] 读不出碎片时长：$(basename "$f")"; return 1; }
        expected=$(awk -v a="$expected" -v b="$d" 'BEGIN{printf "%.3f", a+b}')
    done

    local r
    r=$(awk -v o="$d_out" -v e="$expected" 'BEGIN{ if (e<=0) {print 0; exit} printf "%.3f", o/e }')
    log "  [校验] 成品 ${d_out}s / 碎片合计 ${expected}s = ${r}x"

    awk -v r="$r" -v lo="$DURATION_RATIO_MIN" -v hi="$DURATION_RATIO_MAX" \
        'BEGIN{ exit !(r>=lo && r<=hi) }'
}

# 兜底：全量重编码重建时间轴
reencode_fix(){
    local src="$1" dst="$2"
    timeout "$ENCODE_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel warning -y \
        -fflags +genpts -i "$src" \
        -vsync cfr \
        -c:v libx264 -preset "$FIX_PRESET" -crf "$FIX_CRF" \
        -af aresample=async=1:first_pts=0 \
        -c:a aac -b:a "$FIX_AUDIO_BITRATE" \
        -movflags +faststart "$dst"
}

# 空间预检：TS 中转要 2 份体积
check_space(){
    local total=0 f size free need
    for f in "$@"; do
        size=$(stat -c%s "$f" 2>/dev/null) || size=0
        total=$((total + size))
    done
    need=$(( total / 1024 * 2 + total / 10240 ))
    free=$(df -kP "$TMP_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
    [[ "$free" =~ ^[0-9]+$ ]] || return 0
    if (( need > free )); then
        log "  [空间不足] 需要约 $((need/1024)) MB，$TMP_DIR 只有 $((free/1024)) MB"
        return 1
    fi
    return 0
}

# 核心：一组碎片 → 一个 mp4
merge_group(){
    local src_dir="$1" dst_dir="$2" key="$3"; shift 3
    local files=("$@")
    local n=${#files[@]}

    local first="${files[0]}"

    # 输出名统一成 <主播>_<日期>_<时间>[_标题]_merged.mp4：
    #   - 沿用首段原名（已含日期时间，可能还带标题），去掉尾部 _序号
    #   - 空格一律换成下划线（历史上空格文件名给下游带来过麻烦）
    #   - 若原名没有以「主播_」开头，补上（有些来源是 日期_时间_标题，不带主播）
    local stem; stem=$(basename "${first%.*}")
    stem=$(printf '%s' "$stem" | sed -E 's/_[0-9]+$//')
    stem="${stem// /_}"
    local streamer="${key%%__*}"
    streamer="${streamer// /_}"
    case "$stem" in
        "${streamer}"_*) : ;;
        *) stem="${streamer}_${stem}" ;;
    esac
    local out_name="${stem}${OUTPUT_SUFFIX}.mp4"
    local dst_file="$dst_dir/$out_name"

    # 目标已存在：先核对时长，吻合才删源（防止半截文件/同名不同场次被误删）
    if [ -e "$dst_file" ] && [ -s "$dst_file" ]; then
        if validate_output "$dst_file" "${files[@]}"; then
            log "  [跳过] 目标已存在且时长吻合：$out_name"
            local f
            for f in "${files[@]}"; do
                (( DRY_RUN == 0 )) && rm -f -- "$f"
                remove_state "$f"
            done
            return 0
        fi
        log "  [警告] 目标已存在但时长不吻合，保留源文件不删：$out_name"
        return 1
    fi

    # 稳定性：所有碎片都要写完（单文件也一样）
    local last="${files[$((n-1))]}"
    if ! is_ready "$last"; then
        log "  [等待] 最后一段还没结束：$(basename "$last")"
        return 1
    fi

    # 单文件：直接改名移动（不重编码）
    if (( n == 1 )); then
        if (( DRY_RUN == 1 )); then
            log "  [DRY] 单文件移动：$(basename "$first") → $out_name"
            return 0
        fi
        safe_move "$first" "$dst_file" && log "  [完成] 单文件：$out_name"
        return $?
    fi

    log "  [合并] $key  共 ${n} 段 → $out_name"

    if (( DRY_RUN == 1 )); then
        log "  [DRY] 计划：TS 中转 ${n} 段 → 拼接 → $out_name"
        return 0
    fi

    if ! check_space "${files[@]}"; then
        log "  [等待] 空间不足，本轮跳过"
        return 1
    fi

    local work="$TMP_DIR/g_$$_${key//[^A-Za-z0-9]/_}"
    rm -rf -- "$work"; mkdir -p "$work" || return 1
    : > "$work/list.txt"

    local i=0 f ok=1
    for f in "${files[@]}"; do
        i=$((i+1))
        local ts="$work/$(printf '%03d' "$i").ts"
        if ! to_ts "$f" "$ts" || [ ! -s "$ts" ]; then
            log "  [TS中转失败] $(basename "$f")"
            ok=0; break
        fi
        printf "file '%s'\n" "$ts" >> "$work/list.txt"
    done

    local tmp_out="$work/out.mp4"
    if (( ok == 1 )); then
        timeout "$MERGE_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel warning -y \
            -fflags +genpts -f concat -safe 0 -i "$work/list.txt" \
            -map 0:v:0 -map "0:a?" \
            -c copy -movflags +faststart "$tmp_out" 2>"$work/merge.err" \
            && [ -s "$tmp_out" ] || ok=0
    fi

    # 兜底：mp4 直接拼（TS 转不出来时）
    if (( ok == 0 )); then
        log "  [兜底] 改用 mp4 直接拼接"
        : > "$work/list2.txt"
        for f in "${files[@]}"; do
            printf "file '%s'\n" "$(printf '%s' "$f" | sed "s/'/'\\\\''/g")" >> "$work/list2.txt"
        done
        rm -f "$tmp_out"
        timeout "$MERGE_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel warning -y \
            -f concat -safe 0 -i "$work/list2.txt" \
            -map 0:v:0 -map "0:a?" \
            -c copy -fflags +genpts -avoid_negative_ts make_zero 2>"$work/merge.err" \
            -movflags +faststart "$tmp_out" \
            && [ -s "$tmp_out" ] || { log "  [失败] 拼接失败"; [ -s "$work/merge.err" ] && log "  [ffmpeg] $(tail -3 "$work/merge.err" | tr '\n' ' ')"; rm -rf -- "$work"; return 1; }
    fi

    # 时长校验
    if ! validate_output "$tmp_out" "${files[@]}"; then
        log "  [校验失败] 时间轴不对，重编码兜底"
        local fixed="$work/fixed.mp4"
        if reencode_fix "$tmp_out" "$fixed" && [ -s "$fixed" ] && validate_output "$fixed" "${files[@]}"; then
            mv -f -- "$fixed" "$tmp_out"
            log "  [修复] 重编码后校验通过"
        else
            log "  [放弃] 重编码也没能修好，保留碎片等下一轮"
            rm -rf -- "$work"
            return 1
        fi
    fi

    # 多段合并输出做一次去跳变重编码：规范化 PTS/DTS、强制 CFR，规避 B 站二次转码时间跳变
    # 单段文件（n==1）不走此分支，零编码开销；多段合并才重编码，源文件可能已删，必须默认全量执行
    local normalized="$work/normalized.mp4"
    if timeout "$ENCODE_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel warning -y \\
        -hwaccel qsv -hwaccel_device /dev/dri/renderD128 \\
        -i "$tmp_out" \\
        -c:v h264_qsv -qp 23 -vsync cfr -fflags +genpts \\
        -af aresample=async=1:first_pts=0 \\
        -c:a aac -b:a "$FIX_AUDIO_BITRATE" \\
        -movflags +faststart "$normalized" 2>"$work/normalize.err"; then
        if [ -s "$normalized" ]; then
            tmp_out="$normalized"
            log "  [归一化] QSV 硬编码完成（几乎不占 CPU 核心）"
        else
            log "  [警告] QSV 输出为空，保留原始合并输出"
        fi
    else
        log "  [警告] QSV 硬编码失败，回退到软编码（-threads 1，留出 CPU 核心）"
        cat "$work/normalize.err" >> "$LOG_FILE" 2>/dev/null || true
        local normalized_soft="$work/normalized_soft.mp4"
        if timeout "$ENCODE_TIMEOUT" "$FFMPEG" -nostdin -hide_banner -loglevel warning -y \\
            -i "$tmp_out" \\
            -fflags +genpts -vsync cfr -threads 1 \\
            -c:v libx264 -preset "$FIX_PRESET" -crf "$FIX_CRF" \\
            -af aresample=async=1:first_pts=0 \\
            -c:a aac -b:a "$FIX_AUDIO_BITRATE" \\
            -movflags +faststart "$normalized_soft" 2>"$work/normalize_soft.err"; then
            if [ -s "$normalized_soft" ]; then
                tmp_out="$normalized_soft"
                log "  [归一化] 软编码完成（-threads 1）"
            fi
        else
            log "  [警告] 软编码也失败，保留原始合并输出"
            cat "$work/normalize_soft.err" >> "$LOG_FILE" 2>/dev/null || true
        fi
    fi

    # 落盘
    if ! safe_move "$tmp_out" "$dst_file"; then
        rm -rf -- "$work"
        return 1
    fi

    for f in "${files[@]}"; do
        rm -f -- "$f"
        remove_state "$f"
    done
    rm -rf -- "$work"
    log "  [完成] $out_name"
    return 0
}

# 跨卷安全移动：copy 到目标盘临时名 → 校验大小 → rename
# 对 .m4s：先 ffprobe 验证是有效视频，有效则重命名为 .mp4 后移动
safe_move(){
    local src="$1" dst="$2"
    if [ -e "$dst" ] && [ -s "$dst" ]; then
        rm -f -- "$src"; remove_state "$src"; return 0
    fi

    # 视频完整性校验：非视频文件不移动
    local fmt
    fmt=$(ffprobe -v error -show_entries format=format_name -of default=noprint_wrappers=1:nokey=1 "$src" 2>/dev/null | tr -d '\r\n')
    case "$fmt" in
        mov,mp4,m4a,3gp,3g2,mj2)
            ;;  # 正常视频/音频，继续
        *)
            log "  [跳过] 非视频文件或损坏：$(basename "$src") (fmt=$fmt)"
            return 1
            ;;
    esac

    local tmp="$TMP_DIR/.moving.$$.$(basename "$dst")"
    rm -f -- "$tmp"
    if ! cp -- "$src" "$tmp"; then
        log "  [失败] 复制失败：$(basename "$src")"
        rm -f -- "$tmp"; return 1
    fi
    local a b
    a=$(stat -c%s "$src"); b=$(stat -c%s "$tmp")
    if [ "$a" != "$b" ]; then
        log "  [失败] 大小校验不一致：$a vs $b"
        rm -f -- "$tmp"; return 1
    fi

    # .m4s 改为 .mp4
    local final_dst="$dst"
    if [[ "$dst" == *.m4s ]]; then
        final_dst="${dst%.m4s}.mp4"
    fi

    if ! mv -- "$tmp" "$final_dst"; then
        log "  [失败] 改名失败：$(basename "$final_dst")"
        rm -f -- "$tmp"; return 1
    fi
    rm -f -- "$src"; remove_state "$src"
    return 0
}

############################################
# 六、扫描一个目录
############################################

# 片段开始时间（epoch）：优先从文件名解析，解析不到用 mtime
start_epoch_of(){
    local f="$1" p rest d t
    p=$(parse_name "$(basename "$f")") || p=""
    if [ -n "$p" ]; then
        rest="${p#*|}"
        d="${rest%%|*}"
        t="${rest##*|}"
        date -d "$d ${t//-/:}" +%s 2>/dev/null && return 0
    fi
    stat -c%Y "$f" 2>/dev/null || echo 0
}

process_dir(){
    local src_dir="$1" dst_dir="$2" _ex
    for _ex in "${EXCLUDE_DIRS[@]}"; do
        [ -n "$_ex" ] || continue
        if [ "$src_dir" = "$_ex" ]; then
            log "  [排除] $src_dir"
            return 0
        fi
    done
    [ -d "$src_dir" ] || return 0
    mkdir -p "$dst_dir" 2>/dev/null

    # 收集 -> 分组
    declare -A GRP_MAP=()
    local f name key
    for f in "$src_dir"/*; do
        [ -f "$f" ] || continue
        name="${f##*/}"
        is_ignored "$name" && continue
        is_video "$name" || continue
        key=$(group_key_of "$f")
        GRP_MAP["$key"]+="$f"$'\n'
    done

    if (( ${#GRP_MAP[@]} == 0 )); then
        unset GRP_MAP
        return 0
    fi

    log "  [分组] ${#GRP_MAP[@]} 组"

    for key in "${!GRP_MAP[@]}"; do
        local files
        mapfile -t files < <(printf '%s' "${GRP_MAP[$key]}" | sort -u | grep -v '^$')
        (( ${#files[@]} == 0 )) && continue

        # 组内若还有 ts 未稳定，先跳过整组（避免把录制中的 ts 拼进去）
        local has_unstable_ts=0
        for f in "${files[@]}"; do
            case "${f,,}" in
                *.ts) is_stable "$f" || has_unstable_ts=1 ;;
            esac
        done
        if (( has_unstable_ts == 1 )); then
            log "  [等待] $key 组内有 ts 还在写"
            continue
        fi

        # 按「断档」切场次：上一段结束 -> 这一段开始 超过 MAX_GAP_MINUTES 才拆。
        # 注意必须用「上一段的结束时间」做基准：如果拿开始时间相减，一段连续录了
        # 两小时的长录播会被自己两小时的时长顶过阈值，被错误地拆成两份。
        local cur=() prev_end=0 st
        for f in "${files[@]}"; do
            st=$(start_epoch_of "$f")
            if (( ${#cur[@]} > 0 && MAX_GAP_MINUTES > 0 )) && (( st - prev_end > MAX_GAP_MINUTES * 60 )); then
                merge_group "$src_dir" "$dst_dir" "$key" "${cur[@]}"
                cur=()
            fi
            cur+=("$f")
            prev_end=$(stat -c %Y "$f" 2>/dev/null || echo "$st")
        done
        (( ${#cur[@]} > 0 )) && merge_group "$src_dir" "$dst_dir" "$key" "${cur[@]}"
    done

    unset GRP_MAP
}

############################################
# 七、主流程
############################################

init_dirs
acquire_lock

# 退出时收拾子进程（timeout/ffmpeg），避免留下孤儿进程继续写临时文件
cleanup(){
    pkill -P $$ 2>/dev/null
    return 0
}
trap cleanup EXIT INT TERM

log "============================================"
log "统一录播合并启动  DRY_RUN=$DRY_RUN  CHECK_OPEN=$CHECK_OPEN"
log "来源数量：${#SOURCES[@]}"
log "============================================"

for conf in "${SOURCES[@]}"; do
    [ -z "$conf" ] && continue
    [[ "$conf" == \#* ]] && continue

    IFS='|' read -r sname src dst mode <<< "$conf"
    if [ -z "$src" ] || [ -z "$dst" ]; then
        log "[跳过] 配置无效：$conf"
        continue
    fi
    if [ ! -d "$src" ]; then
        log "[跳过] 源不存在：$sname → $src"
        continue
    fi

    log "--------------------------------------------"
    log "[来源] $sname  ($mode)"
    log "  $src → $dst"
    log "--------------------------------------------"

    if [ "$mode" = "tree" ]; then
        for sub in "$src"/*/; do
            [ -d "$sub" ] || continue
            log "[主播] $(basename "${sub%/}")"
            process_dir "${sub%/}" "$dst"
        done
    else
        process_dir "$src" "$dst"
    fi
done

# 清理失效状态
for sf in "$STATE_DIR"/*.state; do
    [ -f "$sf" ] || continue
    src=$(awk 'NR==1{print $3}' "$sf" 2>/dev/null)
    [ -n "$src" ] && [ ! -f "$src" ] && rm -f "$sf"
done

if (( DRY_RUN == 1 )); then
    log "[DRY] 演练模式：以上均为计划，未移动/合并/删除任何文件"
fi

log "============================================"
log "全部完成"
log "============================================"
exit 0
