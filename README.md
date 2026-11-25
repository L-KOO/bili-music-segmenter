# bili-music-segmenter

自动把直播录播切成单首歌，识歌命名后上传到 B 站。

适用于：唱见/歌台/音乐类直播的录播自动切片归档。输入一堆几小时的录播文件，输出一堆按歌曲切好、带歌名、已上传 B 站的独立视频。

---

## 目录

- [它做什么](#它做什么)
- [前置要求](#前置要求)
- [快速开始（Docker）](#快速开始docker)
- [配置项详解](#配置项详解)
- [手动安装（不用 Docker）](#手动安装不用-docker)
- [可选：录播合并脚本](#可选录播合并脚本)
- [可选：上传卡死告警](#可选上传卡死告警)
- [目录结构](#目录结构)
- [常见问题](#常见问题)

---

## 它做什么

```
录播文件（几小时的长视频）
    ↓  inaseg 容器
    ├─ 音频分离
    ├─ 音乐片段检测（inaSpeechSegmenter，区分人声/音乐/静音）
    ├─ ffmpeg 无损切片（-c copy，不重编码）
    ├─ shazam 识歌 → 重命名为「序号_歌名 by 歌手.mp4」
    └─ 投递上传任务
    ↓  inacelery 容器
    ├─ 调用 biliup 上传到 B 站
    ├─ 失败自动重试（默认 15 次，随机间隔 5~30 分钟）
    └─ 上传成功后清理本地文件
```

**关键特性**：

| 特性 | 说明 |
|---|---|
| 零转码切片 | `-c copy` 直接复制流，不损失画质，速度取决于磁盘 |
| 分块处理 | 长录播按 3600 秒分块，块间 30 秒重叠防止切歌断层 |
| 事件驱动 | 轮询检测新文件，不用等固定时间窗口 |
| 文件稳定检测 | 正在写入的文件不会被提前处理 |
| 已处理去重 | `.processed_media.json` 记录，重启不重复处理 |
| 失败记账 | 坏文件连续失败 2 次后跳过，不再每轮重跑 |

---

## 前置要求

| 项 | 要求 | 说明 |
|---|---|---|
| Docker | 20.10+ | 需要 `docker compose`（v2，命令是 `docker compose` 不是 `docker-compose`） |
| 磁盘空间 | ≥ 录播体积的 2 倍 | 切歌时需要临时分离音频 |
| 内存 | ≥ 4 GB | 分块处理 1 小时素材约需 3 GB |
| CPU | 无特殊要求 | 实测 i3-6100（2C4T）可跑，2 小时 56 分素材约 11 分钟切完 |
| 网络 | 能访问 B 站 API + Shazam | shazam 识歌需要外网 |

**ffmpeg / python 都不需要宿主安装**，全部在镜像内。

---

## 快速开始（Docker）

### 第 1 步：获取代码

```bash
git clone https://github.com/L-KOO/bili-music-segmenter.git
cd bili-music-segmenter
git checkout inaseg-cloud
```

### 第 2 步：创建配置文件

```bash
cp .env.example .env
```

用编辑器打开 `.env`，**至少填这一项**：

```bash
RECORDING_DIR=/你的/录播目录/绝对路径
```

> 这个目录里放待切的录播成品。例如 bililive-tools 的录制目录：
> `/vol1/1000/biliLive-tools/video/Bilibili`
>
> **必须是已存在的目录**，否则 docker 会创建空目录导致扫不到文件。

其余项一般不用改，详见[配置项详解](#配置项详解)。

### 第 3 步：构建镜像

```bash
docker compose build
```

首次构建约 15~30 分钟（要下载 TensorFlow 2.18 + inaSpeechSegmenter）。
国内网络如果卡在 pip，见[常见问题 Q1](#常见问题)。

### 第 4 步：登录 B 站（生成 cookies.json）

上传功能需要 B 站登录态，**必须手动执行一次**：

```bash
docker compose run --rm inaseg biliup -u /inaseg/cookies.json login
```

按终端提示操作：

1. 会输出一个二维码链接或 ASCII 二维码
2. 用手机 B 站 App 扫码
3. 确认登录后，容器内会写入 `/inaseg/cookies.json`
4. 因为 `./:/inaseg` 是挂载的，宿主机项目根目录也会出现 `cookies.json`

验证登录态：

```bash
docker compose run --rm inaseg biliup -u /inaseg/cookies.json renew
```

看到 `更新cookie` 字样即成功。

> **这一步会产生含账号凭据的文件**。`cookies.json` 已在 `.gitignore` 中，
> 切勿提交或分享给他人。
>
> cookie 有效期约 180 天，到期后重新执行 login 即可。

### 第 5 步：启动

```bash
docker compose up -d
```

查看运行状态：

```bash
# 切歌服务日志
docker compose logs -f inaseg

# 上传服务日志
docker compose logs -f inacelery
```

往 `RECORDING_DIR` 里放一个录播文件，`WATCH_INTERVAL` 秒内应该能看到日志。

### 第 6 步（可选）：调整投稿分区

默认 `UPLOAD_TID=31`（音乐/翻唱）。要改成其他分区，编辑 `.env`：

```bash
UPLOAD_TID=21    # 日常
```

改完重启生效：

```bash
docker compose restart inaseg inacelery
```

---

## 配置项详解

全部配置在 `.env` 中。分四类：

### 必填项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RECORDING_DIR` | 无 | 录播成品目录的宿主机绝对路径。容器内挂载为 `/recording` |
| `UPLOAD_TID` | `31` | B 站投稿分区 ID。31=音乐/翻唱，21=日常，138=搞笑 |
| `UPLOAD_COPYRIGHT` | `2` | 转载类型。1=自制，2=转载（切片归档通常算转载） |

### 登录态

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COOKIE_FILE` | `./cookies.json` | biliup 登录态文件路径（相对项目根目录）。由第 4 步生成 |

### 切歌行为

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WATCH_INTERVAL` | `120` | 轮询间隔（秒）。越小响应越快，但频繁 stat 目录。建议 60~300 |
| `STABLE_WAIT` | `10` | 文件 mtime 距今超过此秒数才认为写完。防止处理半成品 |
| `STABLE_CHECKS` | `2` | 连续 N 轮大小/mtime 不变才处理。总等待 = `STABLE_WAIT × (1 + STABLE_CHECKS)`<br>默认值合计 30 秒。网络盘建议调到 3 |

### 上传行为

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CELERY_CONCURRENCY` | `1` | **强烈建议保持 1**。并发 >1 会同时抢 B 站接口，极易触发风控 |
| `CELERY_LOGLEVEL` | `info` | `debug` / `info` / `warning` / `error` |
| `UPLOAD_INTERVAL` | `2400` | 两场之间的间隔（秒）。上一场传完后等待这么久再传下一场。风控频繁时调到 3600~7200 |

### 容器与路径

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IMAGE_NAME` | `ipynb-inaseg` | 镜像名。自己 build 时随意 |
| `WORK_DIR` | `./work` | 中间产物目录（宿主机相对路径）。需 ≥ 单场录播 2 倍空间 |
| `RESTART_POLICY` | `unless-stopped` | 容器重启策略 |

### 可选：告警

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PUSHPLUS_TOKEN` | 空 | PushPlus 推送 token。留空则告警脚本只写日志不推送 |
| `STALL_HOURS` | `5` | 上传卡死判定阈值（小时） |

---

## 手动安装（不用 Docker）

适合：已有 Python 环境、想直接跑在宿主、或要接进现有 systemd 服务。

完整步骤见 **[docs/MANUAL_INSTALL.md](docs/MANUAL_INSTALL.md)**，包含：

- Python 3.11 虚拟环境搭建
- ffmpeg / biliup 二进制安装
- 依赖安装顺序（有坑，官网没写）
- 配置与首次运行
- systemd 服务文件（开机自启 + 日志轮转）
- 与 Docker 方案的差异对照

---

## 可选：录播合并脚本

`scripts/rec_merge_unified.sh` 是**独立工具**，用于把录制工具产生的**碎片文件**
合并成整场录播。不需要 Docker / Python，纯 bash + ffmpeg。

| 录制工具产出 | 是否需要 |
|---|---|
| **单个完整文件**（bililive-tools 的 `.mp4`/`.m4s`、biliup 录制） | ❌ **不需要**，直接切歌 |
| **几十个碎片**（BililiveRecorder 录播姬、StreamCap、抖音录制器） | ✅ **需要合并** |

详细说明见 **[docs/REC_MERGE.md](docs/REC_MERGE.md)**，包含：

- 输入格式支持（含 `.m4s` 处理）
- 三个关键参数（稳定等待、编码超时）
- 数据源配置（`SOURCES` 数组，tree / flat 模式）
- 三种运行方式（手动 / 飞牛任务计划 launcher / cron）
- 自检机制与去跳变归一化的原理
- 常见问题（时长不对、m4s 不处理、文件不移动）

快速开始：

```bash
# 改脚本顶部的 SOURCES 数组指向你的目录
vi scripts/rec_merge_unified.sh

# 自检
bash scripts/check_rec_merge.sh scripts/rec_merge_unified.sh

# 运行
bash scripts/rec_merge_unified.sh
```

---

## 可选：上传卡死告警

`scripts/` 下有两个运维脚本，**默认不启用**：

| 脚本 | 作用 |
|---|---|
| `upload_watch.sh` | 检测「多久没有新上传进展」，超 `STALL_HOURS` 小时推 PushPlus 提醒 |
| `upload_recover.sh` | 上传任务重试耗尽后自动重投，并改名防重复上传 |

典型用途：B 站要求手机验证码时上传会卡住，这两个脚本能提醒你并自动恢复。

### 启用方式 A：宿主机 cron（推荐）

```bash
# 1) 填 PushPlus token（可选，不填只写日志）
cp scripts/.pushplus.env.example scripts/.pushplus.env
vi scripts/.pushplus.env     # 填 PUSHPLUS_TOKEN

# 2) 加 crontab（路径改成本项目实际位置）
crontab -e
*/10 * * * * /你的路径/bili-music-segmenter/scripts/upload_watch.sh   >> /你的路径/bili-music-segmenter/scripts/upload_watch.log 2>&1
*/10 * * * * /你的路径/bili-music-segmenter/scripts/upload_recover.sh >> /你的路径/bili-music-segmenter/scripts/upload_recover.log 2>&1
```

### 启用方式 B：docker compose profile

`docker-compose.yml` 底部有注释掉的 `maintenance` 服务，取消注释后：

```bash
docker compose --profile maintenance up -d
```

> 需要额外挂载 recording 目录进去，注意 compose 里那行路径写的是
> `${RECORDING_DIR}:./recording`，按实际情况调整。

---

## 目录结构

```
bili-music-segmenter/
├── docker-compose.yml        # 容器编排（本项目主入口）
├── .env.example              # 环境变量模板 → 复制为 .env
├── Dockerfile                # 镜像构建
├── requirements.txt          # Python 依赖
│
├── watcher.py                # 主循环：轮询 → 检测 → 切歌 → 投递上传
├── inaseg.py                 # 单文件切歌入口
├── biliup.py                 # 单个录播的完整处理流程（切歌+上传）
├── biliupload.py             # B 站上传封装（拼 biliup 命令）
│
├── segment/                  # 核心：音乐片段检测与切片
│   ├── segment.py            #   分块检测、切片、时长校验
│   └── shazam.py             #   识歌与重命名
│
├── network/                  # 取源与网络
│   ├── watcher.py            #   配置驱动的源扫描
│   ├── extractor.py          #   各类 extractor（glob/biliuserupload/...）
│   ├── download.py           #   yt-dlp 封装
│   └── biliupload.py         #   上传相关网络逻辑
│
├── utils/                    # 工具
│   ├── ffmpeg.py             #   ffmpeg/ffprobe 封装、分块计算
│   ├── filename.py           #   文件名清洗与重命名
│   └── timestamp.py          #   时间戳转换
│
├── configs/                  # 配置（改成你自己的）
│   ├── biliWatcher.yaml      #   切歌源配置
│   └── biliWrapper.json      #   投稿元数据映射（分区/标签/简介）
│
├── scripts/                  # 运维脚本（默认不启用）
│   ├── rec_merge_unified.sh  #   录播合并（可选，独立工具，见 docs/REC_MERGE.md）
│   ├── check_rec_merge.sh    #   合并脚本自检（四项结构性检查）
│   ├── upload_watch.sh       #   上传卡死告警
│   ├── upload_recover.sh     #   失败自动重投
│   └── upload_resume.sh      #   断点续传
│
└── docs/
    ├── MANUAL_INSTALL.md     # 手动安装说明（含 systemd）
    └── REC_MERGE.md          # 录播合并脚本说明
```

---

## 常见问题

### Q1：`docker compose build` 卡在 pip install

原因：TensorFlow 镜像内的 pip 源在国外。

解决：在 `Dockerfile` 的 `RUN pip3 install` 前加一行换源：

```dockerfile
RUN pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

或构建时临时指定：

```bash
docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

### Q2：日志显示「本轮候选 0」

按顺序排查：

1. `RECORDING_DIR` 路径是否正确？`ls` 一下确认有文件
2. 文件扩展名是否受支持？只认 `.mp4 .mkv .flv .ts .m4s`
3. 文件是否太小？小于 1 MB 的文件会被跳过
4. 是否已在 `.processed_media.json` 中？删掉该条目可重新处理

### Q3：切歌切出来很多碎片 / 一首歌被切成两段

调 `EXTRACT_SEG_THRES` 和 `EXTRACT_SEG_CONNECT`（在 `segment/segment.py` 顶部）：

- 碎片多 → 调大 `EXTRACT_SEG_THRES`（默认 60 秒）
- 两首歌分不开 → 调小 `EXTRACT_SEG_CONNECT`（默认 5 秒）
- 一首歌被切断 → 调大 `SEGMENT_OVERLAP`（默认 30 秒）

### Q4：上传报「该视频内部存在时间戳跳变」

录制流的时间戳不连续。本项目已做归一化处理（`normalize_video`），
如果仍报错，看 `docker compose logs inaseg | grep 归一化`：

- 显示「QSV 硬编码完成」→ 已修复，可重新上传
- 显示「软编码完成」→ 已修复（慢一些）
- 显示「保留原始输入（未归一化）」→ 两种编码都失败，见日志里的
  `work/normalize.err`

### Q5：上传报「数据异常，请重新压制」

通常是**帧率不是恒定（VFR）**或**音视频起始时间不对齐**。

本项目已强制 `-fps_mode cfr -r 30`，正常情况下不会出现。
如果出现，检查录制源是否异常（用 `ffprobe -show_streams 文件` 看
`avg_frame_rate` 与 `r_frame_rate` 是否一致）。

### Q6：shazam 识歌全部失败

`shazamio` 调用的是非官方接口，有频率限制。日志里出现大量
`shazam failed` 通常是：

1. 短时间内请求过多（本项目已加 1 秒间隔，仍可能触发）
2. Shazam 服务端变动

识歌失败不影响切片，只是文件名没有歌名，会以序号命名。

### Q7：切歌后本地文件被删了，想保留

在 `biliup.py` 的 `InaBiliup.run()` 里，`self.cleanup` 控制是否删除
源文件。设为 `False` 可保留（但会占空间）。

### Q8：想改切片命名格式

改 `segment/segment.py` 的 `extract_mah_stuff()` 末尾，输出文件名的拼接
逻辑在那里。

---

## 许可

见 [LICENSE](LICENSE)。

## 致谢

- [inaSpeechSegmenter](https://github.com/lovegaoshi/inaSpeechSegmenter) —— 音乐/语音片段检测
- [biliup](https://github.com/biliup/biliup) —— B 站上传
- [shazamio](https://github.com/shazamio/ShazamIO) —— 识歌
