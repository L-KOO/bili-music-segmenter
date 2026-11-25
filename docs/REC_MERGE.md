# rec_merge_unified.sh —— 录播合并与归档脚本

> **这是一个独立工具**，不依赖本项目的其他代码，也不需要 Docker、Python 或 inaSpeechSegmenter。
> 它只做一件事：把**同一场直播产生的多个碎片文件**合并成一个成品，并归档到指定目录。

## 什么时候需要它

不同录制工具产出的文件形态不同：

| 录制工具 | 产出形态 | 是否需要本脚本 |
|---|---|---|
| **bililive-tools** | 单文件 `.mp4` / `.m4s` 直接完整 | ❌ 不需要，直接切歌即可 |
| **BililiveRecorder（录播姬）** | 按「分段」切碎，一场几十个 `.flv` | ✅ **需要合并** |
| **StreamCap / 抖音录制器** | 按时间切片，一场多个文件 | ✅ 需要合并 |
| **biliup 自带录制** | 单个 `.flv` | ❌ 不需要 |

简单判断：**如果你一场直播只有一个视频文件，就不需要这个脚本。**
如果是几十个碎片，就需要。

## 它做什么

```
输入：一堆碎片文件（同一场直播，按时间命名）
    ↓
1. 按「主播 + 归属日期」分组
2. 组内按时间排序，检查最后一段是否写完（避免合并到一半）
3. 单文件直接改名归档；多文件走合并
4. 合并方式：碎片 → TS 中转 → 拼接（关键：直接拼 MP4 会拉长时间轴丢帧）
5. 合并后归一化编码（去时间戳跳变，防 B 站拒收）
6. 跨卷安全移动（copy → 校验大小 → rename）
7. 输出：<主播>_<YYYY-MM-DD>_<HH-MM-SS>_<标题>.mp4
```

## 支持的输入格式

- **视频**：`.mp4` `.mkv` `.flv` `.ts` **`.m4s`**
- `.m4s` 会被 `ffprobe` 验证为有效视频后重命名为 `.mp4`
- 非视频文件（缩略图、字幕、日志）自动跳过

## 使用前必读：三个关键参数

脚本顶部有三个参数，**改了会影响行为**：

```bash
STABLE_SECONDS=90          # 文件连续多少秒不变才认为录完了
PART_GRACE_SECONDS=...     # 最后一段额外等待（防止刚切段就合并）
ENCODE_TIMEOUT=21600       # 单次编码超时（6 小时）
```

- `STABLE_SECONDS` 太小 → 可能合并到正在写的文件，产物损坏
- 录播姬默认每 10 分钟切一段，90 秒足够判断
- 网盘 / 慢盘环境建议调到 120~180

## 配置：数据源

脚本中间有一段 `SOURCES` 数组，每一项是 `"名称|源目录|目标目录|模式"`：

```bash
SOURCES=(
    "StreamCap|/vol2/1000/acfun/StreamCap/downloads/哔哩哔哩|/vol1/1000/biliup/data/rec/merged|tree"
    "BiliLive|/vol1/1000/biliLive-tools/video/Bilibili|/vol1/1000/biliup/data/rec/merged|tree"
    # "新来源|/path/to/downloads|/path/to/output|flat"
)
```

| 模式 | 含义 |
|---|---|
| `tree` | 源目录下每个子目录是一个主播（`源/主播名/*.flv`） |
| `flat` | 源目录直接放文件，不分主播 |

`EXCLUDE_DIRS` 数组可排除特定主播目录：

```bash
EXCLUDE_DIRS=("/vol1/1000/biliLive-tools/video/Bilibili/某个不想处理的主播")
```

## 运行方式

### 方式一：手动运行

```bash
bash scripts/rec_merge_unified.sh
```

### 方式二：飞牛 / 群晖任务计划（推荐）

这类系统的任务计划常有**900 秒硬超时**，超过会被杀掉。所以要写一个 launcher，
只负责拉起后台进程然后立即退出：

`launcher.sh`：

```bash
#!/bin/bash
# 飞牛任务计划有 900 秒硬超时，这里只负责拉起后台 worker，自己秒退
WORK=/vol1/1000/biliup/data/rec/.rec_work
SCRIPT=/vol1/1000/biliup/data/rec/rec_merge_unified.sh
mkdir -p "$WORK"
setsid nohup bash "$SCRIPT" >> "$WORK/worker.out" 2>&1 < /dev/null &
echo "$(date '+%F %T') 启动 worker pid=$!"
exit 0
```

然后任务计划里执行这个 launcher（比如每 6 小时一次）：

```
0 */6 * * * /path/to/launcher.sh
```

### 方式三：cron 直接跑

如果你的环境没有超时限制：

```bash
# 每 6 小时跑一次
0 */6 * * * /path/to/bili-music-segmenter/scripts/rec_merge_unified.sh >> /var/log/rec_merge.log 2>&1
```

## 日志位置

| 方式 | 日志 |
|---|---|
| launcher 方式 | `/vol1/1000/biliup/data/rec/.rec_work/worker.out` |
| cron 直接跑 | 重定向到哪就是哪（见上面示例） |
| 脚本内部日志 | `$WORK_DIR/rec_merge_unified.log`（滚动，64MB × 4） |

实时查看：

```bash
tail -f /vol1/1000/biliup/data/rec/.rec_work/worker.out
```

## 自检机制

脚本启动时会自动调用 `scripts/check_rec_merge.sh` 做四项检查：

| 检查 | 目的 |
|---|---|
| `bash -n` 语法 | 基础语法错误 |
| **续行完整性** | 反斜杠后不能紧跟空行或注释（曾导致 ffmpeg 命令断裂） |
| 关键函数存在性 | `merge_group` / `normalize_video` / `safe_move` / `to_ts` / `reencode_fix` |
| **顶层定义顺序** | 函数不能在定义前被调用（bash 运行时才注册函数） |

**自检失败会直接退出（退出码 2），不会带着断裂的命令去处理录播。**

这是为了防一个真实踩过的坑：用 heredoc 改脚本时反斜杠被吃掉一层，导致
ffmpeg 的参数被当成独立命令执行，产出半成品文件，上传 B 站报「数据异常」。

手动执行自检：

```bash
bash scripts/check_rec_merge.sh scripts/rec_merge_unified.sh
```

## 去跳变归一化

合并后的文件会做一次重编码（`normalize_video` 函数），目的是让 B 站能正常转码：

| 处理 | 参数 | 解决的问题 |
|---|---|---|
| 强制恒定帧率 | `-fps_mode cfr -r 30` | 录制流常是 VFR，B 站报「数据异常」 |
| 重建时间戳 | `-fflags +genpts` | 碎片拼接产生的时间戳断层 |
| 音频重采样 | `aresample=async=1:first_pts=0` | 音视频起始时间不对齐 |
| 快速播放 | `-movflags +faststart` | moov 原子前置，边下边播 |

编码优先用 **QSV 硬编码**（Intel 核显），失败回退 **libx264 软编码**（`-threads 1`，
留出 CPU 给其他任务）。两者都失败时保留原始合并输出，不覆盖。

> **注意**：如果机器没有 Intel 核显，QSV 会失败并自动走软编码，速度会慢很多。
> 纯 CPU 环境建议把 `FIX_PRESET` 从 `ultrafast` 按需调整。

## 常见问题

### Q：合并出来的文件时长不对 / 有花屏

先看 worker.out 里有没有 `[校验] 成品 xxx s / 碎片合计 xxx s = xxx倍`。
正常情况下应该在 `0.90 ~ 1.10` 之间。超出说明拼接有问题。

常见原因：
- 碎片之间有缺失（录制中断）
- 碎片编码参数不一致（中途改了录制设置）

### Q：`.m4s` 文件没被处理

1. 确认 `ffprobe` 能读出它：
   ```bash
   ffprobe -v error -show_entries format=format_name 文件.m4s
   ```
   应该输出 `mov,mp4,m4a,3gp,3g2,mj2`
2. 看日志里是否有 `[跳过] 非视频文件或损坏`

### Q：文件一直在原目录，没被移动

按顺序查：

1. 文件是否还在写入？`STABLE_SECONDS` 内被修改过的文件会被跳过
2. 最后一段是否写完？`is_ready()` 会检查
3. 日志里是否有 `[等待] 最后一段还没结束`
4. 目标目录是否已有同名文件？已存在会删源文件不重复合并

### Q：想保留 `_merged` 后缀

脚本顶部有 `OUTPUT_SUFFIX=""`，改成 `OUTPUT_SUFFIX="_merged"` 即可。

### Q：想不重编码（只要合并）

把 `normalize_video` 的调用注释掉，或设置一个开关。但**不推荐**——
不归一化的话 B 站很可能报时间戳异常。

## 与其他部分的关系

```
rec_merge_unified.sh（本脚本）
    ↓ 输出合并好的整场录播
    ↓ 放到 RECORDING_DIR
    ↓
bili-music-segmenter 的 inaseg 容器
    ↓ 切成单首歌
    ↓
inacelery 容器
    ↓ 上传 B 站
```

**两者可以完全独立使用**：

- 只用合并脚本 → 把成品拿去别处用
- 只用切歌 → 录制工具本身就是单文件，不需要合并
- 两者都用 → 录播姬碎片 → 合并 → 切歌 → 上传（全自动）

## 许可

与本项目一致，见根目录 [LICENSE](../LICENSE)。
