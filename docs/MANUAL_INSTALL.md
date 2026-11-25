# 手动安装说明（不用 Docker）

适合场景：

- 已有 Python 3.11 环境，不想为了跑这个再装 Docker
- 需要接进已有的 systemd / supervisor 管理
- 想在已有 GPU 的机器上跑（Docker 镜像默认是 CPU 版）
- 调试时想直接改代码看效果

> **如果你只是想把程序跑起来**，强烈建议用 [README 的 Docker 方案](../README.md#快速开始docker)，
> 它把所有依赖版本都锁死了，不会有环境问题。
>
> 手动安装的坑主要在**依赖版本**（inaSpeechSegmenter 与 numpy/TensorFlow 的兼容性），
> 本文档已把踩过的坑标出来了。

---

## 目录

- [环境要求](#环境要求)
- [安装步骤](#安装步骤)
- [配置](#配置)
- [首次运行](#首次运行)
- [systemd 服务（开机自启）](#systemd-服务开机自启)
- [与 Docker 方案的差异](#与-docker-方案的差异)
- [排错](#排错)

---

## 环境要求

| 项 | 版本要求 | 检查命令 |
|---|---|---|
| 操作系统 | Linux（Debian/Ubuntu 推荐） | `uname -a` |
| Python | **3.11**（3.10 可能可以，3.12 未测） | `python3 --version` |
| ffmpeg | **6.0+**（需要 `-fps_mode` 参数） | `ffmpeg -version` |
| pip | 23.0+ | `pip3 --version` |
| 磁盘 | ≥ 2 倍单场录播体积 | `df -h` |
| 内存 | ≥ 4 GB | `free -h` |

### 为什么 Python 必须 3.11

- `inaSpeechSegmenter` 依赖 TensorFlow，TF 2.18 官方 wheel 覆盖 3.9~3.12
- 但 `numpy` 必须锁在 1.26.3（inaSpeechSegmenter 用了 numpy 2 已移除的 API）
- Python 3.12 + numpy 1.26.3 的组合编译时常失败，3.11 最稳

### 为什么 ffmpeg 必须 6.0+

本项目用了 `-fps_mode` 参数（替代已废弃的 `-vsync`），这是 ffmpeg 5.1+ 才有的。
ffmpeg 4.x 会直接报 `Unrecognized option 'fps_mode'`。

如果系统源里的 ffmpeg 太老：

```bash
# Ubuntu 22.04 自带的 ffmpeg 5.1 够用
sudo apt install ffmpeg

# 如果更老，用静态构建版
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg-release-amd64-static.tar.xz
sudo cp ffmpeg-*/ffmpeg ffmpeg-*/ffprobe /usr/local/bin/
ffmpeg -version   # 确认 >= 6.0
```

---

## 安装步骤

### 步骤 1：系统依赖

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip \
                    ffmpeg git wget curl build-essential
```

验证：

```bash
python3.11 --version    # 应显示 3.11.x
ffmpeg -version | head -1
```

### 步骤 2：获取代码

```bash
cd /opt   # 或你喜欢的目录
git clone https://github.com/L-KOO/bili-music-segmenter.git
cd bili-music-segmenter
git checkout inaseg-cloud
```

### 步骤 3：创建虚拟环境

**必须用 venv**，不要装到系统 Python（会污染系统包，且版本冲突难处理）。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

激活后命令行前缀会变成 `(.venv)`，之后所有 `pip`/`python` 都指虚拟环境。

> 退出虚拟环境用 `deactivate`。

### 步骤 4：升级 pip 并换源

```bash
pip install --upgrade pip
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 步骤 5：安装 inaSpeechSegmenter

**这一步最关键，顺序不能乱。**

```bash
pip install git+https://github.com/lovegaoshi/inaSpeechSegmenter.git
```

> 若 GitHub 访问困难，可先 clone 再本地安装：
> ```bash
> git clone https://github.com/lovegaoshi/inaSpeechSegmenter.git /tmp/ina
> pip install /tmp/ina
> ```

### 步骤 6：安装项目依赖

```bash
pip install -r requirements.txt
```

### 步骤 7：锁定 numpy 版本 ⚠️

**这一步不能跳。** inaSpeechSegmenter 用了 `np.float` 等 numpy 2 已移除的 API，
不锁版本会在运行时报 `AttributeError`。

```bash
pip install numpy==1.26.3
```

验证：

```bash
python -c "import numpy; print(numpy.__version__)"   # 必须 1.26.3
python -c "import tensorflow as tf; print(tf.__version__)"
python -c "from inaSpeechSegmenter import Segmenter; print('inaSpeechSegmenter OK')"
```

三条都不报错才算装好。

### 步骤 8：安装 biliup

上传功能依赖 biliup CLI。

**方式 A：下载预编译二进制（推荐，快）**

```bash
cd /tmp
wget https://github.com/biliup/biliup/releases/latest/download/biliupR-x86_64-unknown-linux-musl.tar.xz
tar xf biliupR-*.tar.xz
sudo cp biliupR-*/biliup /usr/local/bin/
sudo chmod +x /usr/local/bin/biliup
biliup --version
```

> 项目自带的 `biliupinit.py` 也能干这事，但它写死装到 `/bin`，
> 需要 sudo，且在部分系统会导致权限问题。建议用上面的手动方式。

**方式 B：cargo 编译**

```bash
# 需要先装 Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
cargo install biliup
```

编译约 10 分钟，产物在 `~/.cargo/bin/biliup`。

### 步骤 9：验证依赖完整性

```bash
source .venv/bin/activate
cd /你的路径/bili-music-segmenter

python -c "import yt_dlp, shazamio, yaml, celery, sqlalchemy, regex, requests; print('依赖 OK')"
python -c "from segment.segment import extract_music; print('segment 模块 OK')"
biliup --version
ffmpeg -version | head -1
```

---

## 配置

### 1. 环境变量

```bash
cp .env.example .env
vi .env
```

必填：

```bash
RECORDING_DIR=/你的/录播目录        # 宿主机绝对路径
UPLOAD_TID=31                       # 投稿分区
```

> 手动安装时 `.env` 不会被自动加载，需要你手动 export 或在 systemd 里指定。
> 见下方「首次运行」。

### 2. 切歌源配置

编辑 `configs/biliWatcher.yaml`：

```yaml
- extractor: glob
  filter: null
  last_url: null
  name: 本地录播目录
  url: /recording/*
```

**手动安装时要把 `/recording/*` 改成你的实际路径**（Docker 里才有 `/recording` 这个挂载点）：

```yaml
  url: /vol1/1000/biliLive-tools/video/Bilibili/*/*.mp4
```

> `glob` 用的是 Python `glob.glob()`，支持 `*`、`?`、`**`。
> 要递归子目录得写 `**/*.mp4`（但 Python glob 的 `**` 需要 `recursive=True`，
> 本项目未开，所以建议用 `/path/*/*.mp4` 这种多级通配）。

### 3. 投稿元数据（可选）

编辑 `configs/biliWrapper.json`，按文件名匹配来源/简介/标签。
不需要就保持默认的示例内容，匹配不到时会用 `n/a`。

### 4. 登录 B 站

```bash
biliup -u ./cookies.json login
```

按提示扫码。成功后项目根目录出现 `cookies.json`。

验证：

```bash
biliup -u ./cookies.json renew
```

---

## 首次运行

### 手动测试单文件

先别启动主循环，拿一个短录播测通再上量：

```bash
source .venv/bin/activate
cd /你的路径/bili-music-segmenter

# 语法自检
bash scripts/check_rec_merge.sh scripts/rec_merge_unified.sh

# 单文件切歌
python inaseg.py --media /path/to/test.mp4 --outdir /tmp/segtest
```

成功的话 `/tmp/segtest` 里会出现切好的 mp4。

### 启动主循环

```bash
source .venv/bin/activate
cd /你的路径/bili-music-segmenter

# 加载环境变量（手动指定，或在 systemd 里配）
set -a; source .env; set +a
export RECORDING_DIR WORK_DIR

# 切歌主进程
python watcher.py --watch_interval=120 --stable_wait=10 --stable_checks=2
```

**另开一个终端**启动上传 worker：

```bash
source .venv/bin/activate
cd /你的路径/bili-music-segmenter
set -a; source .env; set +a

celery -A inacelery worker -c 1 --loglevel=info
```

两个进程都需要常驻，建议用 systemd（下一节）。

---

## systemd 服务（开机自启）

两个服务需要分别管理。假设项目在 `/opt/bili-music-segmenter`。

### 切歌服务

`/etc/systemd/system/inaseg.service`：

```ini
[Unit]
Description=InaSeg 切歌服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=你的用户名
Group=你的用户组
WorkingDirectory=/opt/bili-music-segmenter

# 加载 .env 中的变量
EnvironmentFile=/opt/bili-music-segmenter/.env

# 用虚拟环境里的 python
ExecStart=/opt/bili-music-segmenter/.venv/bin/python \
          watcher.py \
          --watch_interval=120 \
          --stable_wait=10 \
          --stable_checks=2

Restart=always
RestartSec=10

# 日志（日志同时写到项目内 inaseg.log，这里是 stderr 兜底）
StandardOutput=journal
StandardError=journal
SyslogIdentifier=inaseg

# 安全加固（按需）
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 上传服务

`/etc/systemd/system/inacelery.service`：

```ini
[Unit]
Description=InaCelery 上传 worker
After=network-online.target inaseg.service
Wants=network-online.target
# 上传依赖切歌产出的任务，所以排在 inaseg 之后
After=inaseg.service

[Service]
Type=simple
User=你的用户名
Group=你的用户组
WorkingDirectory=/opt/bili-music-segmenter

EnvironmentFile=/opt/bili-music-segmenter/.env

ExecStart=/opt/bili-music-segmenter/.venv/bin/celery \
          -A inacelery worker -c 1 --loglevel=info

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=inacelery

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 启用

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now inaseg inacelery

# 状态
systemctl status inaseg
systemctl status inacelery

# 实时日志
journalctl -u inaseg -f
journalctl -u inacelery -f

# 重启
sudo systemctl restart inaseg inacelery
```

> **注意**：`EnvironmentFile` 要求 `.env` 是 `KEY=VALUE` 纯文本格式，
> 不能有 `export` 前缀，不能有引号（除非值本身需要）。
> 本项目的 `.env.example` 已符合这个格式。

### 日志轮转（可选）

项目内 `inaseg.log` 都会自己滚动（32MB × 3 份），无需额外配置。
systemd 的 journal 也有默认上限，一般不用管。

---

## 与 Docker 方案的差异

| 项 | Docker 方案 | 手动安装 |
|---|---|---|
| 路径 | 容器内固定 `/recording` | **要自己改 `configs/biliWatcher.yaml` 的 `url`** |
| 环境变量 | `env_file` 自动加载 | 需 `source .env` 或 systemd `EnvironmentFile` |
| 进程管理 | `restart: unless-stopped` | 需自己配 systemd |
| 依赖版本 | 镜像锁定 | **必须手动锁 numpy 1.26.3** |
| biliup | 镜像内 `/bin/biliup` | 要自己装到 `PATH` |
| cookies 路径 | `/inaseg/cookies.json` | 项目根目录 `./cookies.json` |
| 升级 | `docker compose pull && up -d` | `git pull` + 重装依赖 |
| 资源限制 | compose 的 `deploy.resources` | systemd 的 `MemoryMax` / `CPUQuota` |

---

## 排错

### `ImportError: cannot import name 'float' from 'numpy'`

numpy 版本不对。执行：

```bash
pip install numpy==1.26.3 --force-reinstall
```

### `Unrecognized option 'fps_mode'`

ffmpeg 太老。`ffmpeg -version` 确认 ≥ 6.0，否则按上文装静态版。

### `ModuleNotFoundError: No module named 'segment'`

没在项目根目录运行。`cd` 到含 `segment/` 的目录再执行。

### `biliup: command not found`

biliup 没装进 PATH。检查 `which biliup`，或把二进制移到 `/usr/local/bin`。

### `OSError: [Errno 12] Cannot allocate memory`

内存不足。两个办法：

1. 调小分块大小：`segment/segment.py` 顶部 `SEGMENT_THRES`（默认 3600 秒）改成 1800
2. 加 swap：
   ```bash
   sudo fallocate -l 4G /swapfile
   sudo chmod 600 /swapfile
   sudo mkswap /swapfile
   sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

### `celery` 启动报 `sqlalchemy` 版本冲突

本项目锁了 `sqlalchemy==1.4.46`（celery 的 sqla broker 不兼容 2.x）。
若被别的包升级了：

```bash
pip install sqlalchemy==1.4.46 --force-reinstall
```

### 切歌很慢

正常情况：i3-6100 上 2C4T 跑 3 小时素材约 11 分钟。
如果超过 30 分钟，检查：

1. `BATCH_SIZE`（`segment/segment.py` 顶部）：默认 64，别改更大
2. 磁盘是否是机械盘（大量并发读会拖慢，本项目已限 3 路并发）
3. 是否开了 swap（swap 上跑 TensorFlow 会极慢）

### 想要 GPU 加速

TensorFlow 检测到 GPU 会自动使用。需要：

1. 安装 CUDA 11.8 / 12.x + cuDNN
2. `pip install tensorflow[and-cuda]`
3. 验证：`python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"`

> Docker 方案有 `Dockerfile-gpu` 可直接用（需要 nvidia-container-toolkit）。
