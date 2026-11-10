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

判断依据不是「最老目录超过 5 小时」（会误报，因为上传本身有 40 分钟间隔），
而是「**5 小时没有任何进展**」：

- celery 每传完一场会 `rmtree` 掉整个目录
- 所以「目录数变少」或「最老目录换人」= 有进展，计时归零
- 两者都没有且超过 5 小时 = 真卡住

阈值取 5 小时而不是 3 小时：这条线的上行只有 ~0.7MB/s，5.4G 的场次
光上传就要 2.2 小时，加上 40 分钟间隔，两次「目录被清掉」之间约 2.9 小时，
用 3 小时会误报。

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

### 12. 修复：上传卡死告警从未发出（2026-11-10）

症状：三场 09:10 切完的录播一直没上传，也没收到任何提醒。

根因：`.upload_watch.state` 被写成了 5 个字段（"队列为空"的分支写了
`"$now 0 - $now $now"`），而脚本按 4 个字段 `read`，多出来的并进
`prev_alert` 变成 `"1794273001 1794273001"`；第 69 行的
`$((now - prev_alert))` 直接报 `syntax error in expression`。

因为判断写成 `[ waited -ge STALL ] && [ ... ]` 的短路形式，只有"真该报警"
的那一刻才会去算第二个条件，所以平时看起来完全正常，一到期就崩。

修复：
- 空队列分支改为只写 4 个字段
- `prev_alert` 读取后取第一个字段（`${prev_alert%% *}`），兼容已被写坏的历史状态
- 归一化现有状态文件

已知未修：恢复脚本改名重投（`.retry<epoch>`）会让监控误判为"有进展"并重置
计时，最长可把告警推迟 5 小时。

### 13. 修复：改名重投重置计时 + 恢复脚本字段错位

- `upload_watch.sh`：计算「最老目录签名」前先去掉 `.retry<epoch>` 后缀。
  恢复脚本改名重投不再被误判成「有进展」，否则计时被重置、告警最长推迟 STALL（5 小时）。
  同时把状态文件里的计时锚点改回最老目录的真实创建时间。
- `upload_recover.sh`：重投成功后写状态时把 `count` 错写成了 epoch，
  导致下一轮误报「检测到成功上传：目录 1794319801 -> 3」。改为写 `$count`。

### 14. 新增 `scripts/upload_resume.sh`：验证码解除后立即续传

问题：B 站风控会拦死 biliup，而 celery 的 `retry_upload` 在两次尝试之间 sleep
5~30 分钟，这个 sleep 还占着 `-c 1` 的唯一 worker 槽位。你在浏览器里过了验证码
之后，队列仍要等它睡醒才动。

更糟的情况：如果你手动上传完、把本地目录删了，celery 里那个任务会一直拿着
不存在的文件重试 15 次，把唯一的 worker 占死，后面的任务全被堵住。

做法：`upload_resume.py` 绕过 celery，直接调 `biliup upload`。

- 没有可靠的旁路信号能判断验证码是否已过（你是在浏览器操作的），
  所以检测手段就是**真的试一次**：成功 = 风控解除，失败 = 还在拦
- 失败按 2/4/8/15/30 分钟指数退避，避免轰炸
- 上传前先给目录改名（`.resume<epoch>`），celery 里那个旧任务拿旧路径只会
  失败退出，物理上不可能重复上传
- 要求当前没有 `biliup` 进程在跑才开始
- 一次成功后隔 `RESUME_SUCCESS_GAP`（默认 900 秒）再传下一个，
  不给风控连续抓的机会；单轮最多 `RESUME_MAX_PER_RUN`（默认 3）个
- 每 2 分钟由 cron 触发，与 watch/recover 共用 `/tmp/upload_pipeline.lock`

实测：风控解除后 2 分钟内自动接上，上传速率 7.2 MB/s
（被拦时只有 0.68 MB/s，是限速不是带宽）。

### 15. 源文件缺失时快速跳过 + 目录数减少解除退避

背景：手动上传后把本地目录删掉，排队中的 celery 任务会拿着不存在的路径重试
15 次（每次间隔 5~30 分钟），把 `-c 1` 的唯一 worker 占死，后面所有任务被堵住。

改动：

- `inacelery/celery.py`
  - 新增 `SourceGone(BaseException)`：继承 `BaseException` 以穿透
    `utils.util.retry` 的 `except Exception`，不消耗重试次数、不 sleep
  - `retry_upload()` 每次调用前 `ensure_media(cmd)`，任一文件缺失即抛
  - `add()` 捕获后打日志、清理空目录、**立即 return**（不 sleep `UPLOAD_INTERVAL`）
- `scripts/upload_resume.py`
  - `take()` 改名前后都校验目录/文件，目录已删则返回 `None` 跳过
  - 上传前再校验 cmd 里的文件，缺失就丢掉这一单，不调 biliup
  - 状态文件增加第 3 个字段 `last_count`：目录数变少即判定有进展、**解除退避**
    （手动删目录只会发生在上传失败之后，所以和「上传成功」统一处理是安全的）
  - 修复：队列清空时状态没落盘（`if next_try or fails` 在刚重置后恒为假，
    会把陈旧 `next_try` 留在文件里）
- `scripts/upload_requeue.py` / `upload_recover.sh`：孤儿判定加「至少还有 1 个 mp4」

实测：重启后残留的 3 个陈旧任务各在 **3 毫秒**内跳过，不再占用 worker。

### 16. 补传通道改为「武装制」：不常驻、传完自动解除

问题：`upload_resume.py` 原先每 2 分钟无条件工作，没有终止条件。被风控拦住时，
它每 30 分钟（`BACKOFF` 封顶 1800 秒）就往 B 站打一次真实上传请求，永不停止；
健康时还会抢在 celery 前面传（celery 每单后 sleep 2400 秒，而 resume 每 2 分钟扫一次）。

改法：给补传通道加一个「武装标记」`.upload_resume.arm`，默认不工作。

- `scripts/upload_resume.py`
  - 未武装时**直接 return**，不扫描、不发请求（cron 拉起来只是毫秒级空转）
  - 已武装时进入 drain 循环：把待传目录一个个传完，全部清空后**删除标记并退出**
  - 失败时**保留标记**，按 `BACKOFF`（120/240/480/900/1800 秒）等下一轮继续
  - 同一次运行内两场之间隔 `RESUME_INRUN_GAP`（默认 60 秒）
  - 单轮上限 `RESUME_MAX_DRAIN`（默认 10 个），到上限保留标记等下一轮
  - 新增 `--drain`（无视标记立即补传）和 `--dry-run`
  - 移除失效的 `RESUME_SUCCESS_GAP`（drain 模式下由 `RESUME_INRUN_GAP` 取代）
    和实际不生效的 `RESUME_MAX_PER_RUN`
- `scripts/upload_watch.sh`
  - 新增 `ARM_FILE` 变量；在「目录数变少」分支里 `touch` 武装标记并记一行日志
  - 你手动上传后删掉目录 -> 下一次 watch（≤10 分钟）自动武装 -> 下一次 resume
    （≤2 分钟）接手把剩下的传完 -> 自动解除武装

实测：未武装时零输出；watch 检测到 2->1 自动武装；武装后无可传目录自动解除；
`--drain` 可人工无视标记立即补传。

（补充）武装条件加了闸门：

只有「本轮已经卡到过 `STALL`（5 小时）」之后目录数才变少，才会武装。

否则正常批量上传时，celery 每传完一场都会让目录数变少，会误武装；resume 随即
把剩下的以 60 秒间隔连着传完，等于把 40 分钟的上传间隔彻底废掉，反而招风控。

- 正常批量（10 分钟前才进展过）-> 目录数变少 -> **不武装**
- 真卡住（6 小时没进展）-> 目录数变少 -> **武装**

「提前看到就处理」这种情况（没等到 5 小时告警）不会自动武装，用
`touch /vol2/1000/ipynb/.upload_resume.arm` 手动武装即可。
