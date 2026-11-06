# 更新记录

本文件记录 2026-11 这轮针对 **Intel i3-6100 / 2C4T / 16GB / 无独显** 这台机器做的优化。
所有数字都是在这台机器上实测的，不是估算。

---

## 一、分割阶段（占单场耗时 ~80%）

### 1. `segment/segment.py` — BATCH_SIZE 16 → 64

`inaSpeechSegmenter` 的批大小原来的 16 太小，2 核 4 线程上每批的计算量喂不饱 CPU。

| batch | 速度 |
|---|---|
| 16（原值） | 17.1x 实时 |
| **64（现值）** | **23.8x 实时** |
| 128 | 19.3x（4 个逻辑核超订，反而变慢） |

### 2. `segment/segment.py` + `utils/ffmpeg.py` — 重叠分块

原来硬切 `0-3600, 3600-7200...`，压在块边界上的歌会被切成两半，其中不足
`EXTRACT_SEG_THRES`(60s) 的那半会被当成闲聊丢掉，歌就短一截。

现在每块前后各多取 30 秒，`_merge_overlapping` 做去重：

- 同标签且重叠 → 并成一段
- 不同标签且重叠 → 后一段裁到前一段结尾
- 保证结果按时间单调、互不交叠（`extract_music` 依赖这一点）

代价只有 60/3600 ≈ 1.7% 的额外计算。

---

## 二、切片阶段

### 3. `segment/segment.py` — 切片并发限流

原来 N 个片段就起 N 个 ffmpeg 线程同时读同一个机械盘，38 路并发把顺序读打成
随机读。改成固定并发（`INASEG_CUT_WORKERS`，默认 3）。

实测 23 个片段切片耗时 **3.1 秒**（源文件已在分割阶段进了页缓存）。

顺带补了 `os.makedirs(oud, exist_ok=True)`，避免输出目录不存在时 ffmpeg
静默失败、日志却报 successful。

### 4. `utils/process.py` + `inacelery/celery.py` — 40 分钟等待挪位置

原来 `cell_stdout` 开头有个 `time.sleep(2300)`（38 分钟），是挡在**分割之前**的，
每场白白浪费 38 分钟，而且遮住了真正的上传风控。

现在：

- 删掉 `cell_stdout` 里的 sleep
- 间隔挪到 `inacelery/celery.py` 的 `add` 任务结尾
- 因为 celery 是 `-c 1` 串行，效果就是「这一场传完 → 等 40 分钟 → 传下一场」
- 秒数可用环境变量 `UPLOAD_INTERVAL` 覆盖，默认 2400

分割和上传从此完全并行：容器 A 切下一场，容器 B 传上一场。

---

## 三、识歌

### 5. `segment/shazam.py` — 多点取样，命中率 20% → 60%

原来把整段（3~8 分钟）音频丢给 Shazam，前奏、掌声、口白都会污染指纹。

现在从 50% / 25% / 75% 三个位置各切 12 秒送过去，任一命中即返回，最后再用
整段兜底（等于不会比原来差）。

同一批 20 段真实歌切实测：

| | 命中 |
|---|---|
| 旧（整段） | 4/20 = 20% |
| **新（多点 12s）** | **12/20 = 60%** |

原有能认出的 4 首一首没丢。认不出的 8 段再换 7 个位置也救不回来，属于库里真没有。

顺带修掉 `time.sleep(3)`：异步函数里的阻塞 sleep 会冻住整个事件循环，让
`asyncio.gather` 的并发完全失效。

### 6. `segment/shazam.py` — `shazam_title` 健壮性

原实现里 `in the style of` 分支算了个 artist 却从没用过，且 `subtitle`
缺字段直接 KeyError。改成 `get` + 空值判断，缺 title 一律当失败。

---

## 四、日志与磁盘

### 7. `inaseg.py` / `watcher.py` — 降噪 + 滚动

- 日志级别 `DEBUG` → `INFO`
- `inaseg_inst.log` 改 `RotatingFileHandler`（64MB × 4），`inaseg.log`（32MB × 4）
- 新增 `DropFFmpegSpam` 过滤器，拦掉损坏音频导致的刷屏

坏掉的录播会让 ffmpeg 每帧刷一条 `invalid mpeg audio header`，实测单场能刷出
上千万条，把 `inaseg_inst.log` 写到 **9.5GB**，同时拖慢所有磁盘写。

### 8. `utils/ffmpeg.py` — 坏文件的报错修复

- `get_length_using_copied_audio`：ffmpeg 没生成临时文件时不再抛
  `FileNotFoundError`（它把「音频流损坏」这个真实原因盖掉了），改为明确的
  `RuntimeError`
- `get_segment_process_length_array`：拿不到时长时不再返回 `[[None, None]]`
  ——那会让 5~6 小时的文件被整块读进内存直接 OOM，改为报错退出

---

## 五、失败处理

### 9. `biliup.py` — 失败记账，跳过毒丸

损坏的录播会让分割**必然失败**，而下一轮 12 小时循环又会把同一个文件捡起来、
白跑十几分钟再失败，永远循环。

现在用 `_failed_media.json` 记失败次数，同一文件连续失败 2 次后跳过。
想重试就删掉对应那条。

---

## 六、上传监控与自愈（新增 `scripts/`）

### 10. `scripts/upload_watch.sh` — 卡死告警

B 站的人机验证会拦死 biliup，症状是 `inaupload/<场次>/` 一直不消失。

判断依据不是「最老目录超过 3 小时」（会误报，因为上传本身有 40 分钟间隔），
而是「**3 小时没有任何进展**」：

- celery 每传完一场会 `rmtree` 掉整个目录
- 所以「目录数变少」或「最老目录换人」= 有进展，计时归零
- 两者都没有且超过 3 小时 = 真卡住

触发后推 PushPlus，同一轮最多 6 小时提醒一次。

**token 不写在脚本里**：从同目录的 `.pushplus.env` 读取（已在 `.gitignore`）。

### 11. `scripts/upload_recover.sh` + `upload_requeue.py` — 死任务复活

`retry(times=15)` 用完之后任务会彻底失败，目录变成孤儿，永远没人再传。

现在的流程：

1. 从 celery 日志里识别任务死亡事件（`MaxRetryReached` / `WorkerLostError`）
2. 把对应目录登记为孤儿
3. 等上传通道恢复（最近 2 小时有成功上传）或兜底 3 小时
4. 且此刻没有 biliup 在跑，才重新投递

**防重复上传**：投递前先把目录改名，旧任务即使还活着也只会失败退出，
物理上不可能传两份。投递失败会自动把目录改回去。

---

## 七、实测效果

单场 2 小时 56 分素材（2.4GB）：

| 阶段 | 耗时 |
|---|---|
| 分割 | 8 分 22 秒 |
| 切片（23 段） | 3.1 秒 |
| 识歌（23 段） | 1 分 35 秒 |
| **合计** | **10 分 44 秒** |

改造前约 **70 分钟**（其中 38 分钟是纯 sleep）。

---

## 八、回退

改动分两批，各自带快照：

- `_rollback_20261104/restore.sh` — batch / 切片并发 / 日志 / shazam
- `_rollback_20261105/restore.sh` — 重叠分块 / 失败记账 / TF 线程

回退后需要重启容器：

```bash
docker restart ipnby-inaseg-1 ipnby-inacelery-1
```
