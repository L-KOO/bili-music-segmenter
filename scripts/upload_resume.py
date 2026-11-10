#!/usr/bin/env python3
"""补传通道：只在「武装」状态下工作，把积压的待上传目录传完就自动解除武装。

为什么不常驻：
  B 站风控会拦死 biliup。之前这个脚本每 2 分钟无条件试一次，被拦时等于每 30 分钟
  往 B 站打一次真实上传请求，而且健康时还会抢在 celery 前面传。现在改成事件驱动：
  平时一条命令都不发，只有收到「武装」信号才动手。

武装信号从哪来：
  你手动过验证码并删掉本地目录后，inaupload 的目录数会变少；
  upload_watch.sh 每 10 分钟扫一次，发现目录数变少就 touch 一个标记文件。
  你的操作习惯是「一定会在手动上传后删目录」，所以这个信号是可靠的。

工作方式：
  命中武装标记 -> 循环把待传目录一个个传完（同一次运行内，间隔 IN_RUN_GAP 秒）
  -> 全部清空后删掉标记、退出。中途失败则保留标记，按退避等下一轮继续。

和 celery 并存怎么办：
  上传前先把目录改名。celery 里那个还在 sleep 的旧任务拿着旧路径只会失败退出，
  物理上不可能重复上传。另外要求当前没有 biliup 进程在跑。

用法：
  python upload_resume.py            常态（cron 调用，未武装则什么都不做）
  python upload_resume.py --drain    无视武装标记，立刻把当前剩余的传完
  python upload_resume.py --dry-run  只看会传什么，不动文件
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

UP = '/inaseg/inaupload'
STATE = '/inaseg/.upload_resume.state'
ARM = '/inaseg/.upload_resume.arm'

DRY = '--dry-run' in sys.argv
# --drain / --dry-run 都无视武装标记，供人工一次性调用
FORCE = ('--drain' in sys.argv) or DRY

# 失败退避：第 1/2/3/4/5+ 次失败后分别等多少秒
BACKOFF = [120, 240, 480, 900, 1800]
# 同一次运行内，两场之间隔多久（不给 B 站风控连续抓的机会）
IN_RUN_GAP = int(os.environ.get('RESUME_INRUN_GAP', '60'))
# 单次运行最多传几个，防止一次性把整队列灌上去；到上限就保留武装等下一轮
MAX_DRAIN = int(os.environ.get('RESUME_MAX_DRAIN', '10'))


def log(msg):
    print('[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


def has_mp4(d):
    try:
        return any(n.lower().endswith('.mp4') for n in os.listdir(d))
    except OSError:
        return False


def pending_dirs():
    out = []
    try:
        names = os.listdir(UP)
    except OSError:
        return out
    for name in names:
        d = os.path.join(UP, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, 'cmd.txt')) \
                and has_mp4(d):
            out.append(d)
    out.sort(key=lambda p: os.path.getmtime(p))
    return out


def biliup_running():
    try:
        r = subprocess.run(['pgrep', '-f', 'biliup upload'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def load_state():
    """状态文件: <next_try> <fails> <last_count>，兼容只有 2 个字段的旧格式。"""
    try:
        with open(STATE) as f:
            parts = f.read().split()
        next_try = int(parts[0])
        fails = int(parts[1])
        last_count = int(parts[2]) if len(parts) > 2 else None
        return next_try, fails, last_count
    except Exception:
        return 0, 0, None


def save_state(next_try, fails, count):
    try:
        with open(STATE, 'w') as f:
            f.write('%d %d %d\n' % (next_try, fails, count))
    except Exception as e:
        log('写状态失败: %r' % (e,))


def disarm():
    try:
        os.remove(ARM)
    except OSError:
        pass


def base_name(name):
    return re.sub(r'(\.(?:retry|resume)\d+)+$', '', name)


def media_paths(cmd):
    """从 biliup 的 argv 里取出真正的文件参数。"""
    return [str(x) for x in cmd[2:] if not str(x).startswith('-')]


def take(d):
    """改名并改写 cmd.txt 里的路径，返回 (新目录, argv)；目录没了就返回 (None, None)。"""
    name = os.path.basename(d)
    newd = os.path.join(UP, '%s.resume%d' % (base_name(name), int(time.time())))
    if DRY:
        return newd, None
    # 列举之后、改名之前，目录可能已经被删（用户手动清理）
    if not (os.path.isdir(d) and os.path.isfile(os.path.join(d, 'cmd.txt'))):
        log('跳过（目录已不存在）: %s' % name)
        return None, None
    try:
        shutil.move(d, newd)
    except OSError as e:
        log('跳过（改名失败）: %s %r' % (name, e))
        return None, None
    try:
        with open(os.path.join(newd, 'cmd.txt'), encoding='utf-8') as f:
            cmd = json.load(f)
    except Exception as e:
        log('跳过（cmd.txt 读不出）: %s %r' % (name, e))
        return None, None
    cmd = [str(x).replace(d + '/', newd + '/') for x in cmd]
    with open(os.path.join(newd, 'cmd.txt'), 'w', encoding='utf-8') as f:
        json.dump(cmd, f)
    return newd, cmd


def main():
    if not FORCE and not os.path.exists(ARM):
        return 0                      # 未武装：静默退出，不发任何上传请求

    now = int(time.time())
    next_try, fails, last_count = load_state()

    dirs = pending_dirs()
    count = len(dirs)

    if count == 0:
        save_state(0, 0, 0)
        disarm()
        log('没有待上传目录，解除武装')
        return 0

    # 武装期间目录数又变少 = 又有进展，立刻解除退避，不必干等
    if last_count is not None and count < last_count:
        log('目录数 %d -> %d，解除退避' % (last_count, count))
        next_try, fails = 0, 0

    if now < next_try:
        log('退避中，还有 %d 秒（武装保留）' % (next_try - now))
        save_state(next_try, fails, count)
        return 0

    if biliup_running():
        log('有 biliup 正在上传，跳过（武装保留）')
        save_state(next_try, fails, count)
        return 0

    log('已武装，待上传 %d 个，开始补传' % count)
    ok = 0

    while True:
        dirs = pending_dirs()
        if not dirs:
            save_state(0, 0, 0)
            disarm()
            log('剩余已全部传完（本次 %d 个），解除武装' % ok)
            return 0

        if ok >= MAX_DRAIN:
            save_state(0, 0, len(dirs))
            log('已达单轮上限 %d 个，武装保留，等下一轮继续' % MAX_DRAIN)
            return 0

        d = dirs[0]
        name = os.path.basename(d)
        newd, cmd = take(d)
        if newd is None:
            continue                  # take() 内部已记日志

        if DRY:
            log('[DRY] 会上传 %s -> %s' % (name, os.path.basename(newd)))
            ok += 1
            continue

        paths = media_paths(cmd)
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            log('跳过（源文件已不在，缺 %d/%d）: %s'
                % (len(missing), len(paths), name))
            try:
                shutil.rmtree(newd)
            except OSError:
                pass
            continue

        log('上传 %s' % os.path.basename(newd))
        t0 = time.time()
        try:
            rc = subprocess.run(cmd).returncode
        except Exception as e:
            log('调用 biliup 出错: %r' % (e,))
            rc = -1
        dt = int(time.time() - t0)

        if rc == 0:
            log('成功（%ds），清理目录' % dt)
            try:
                shutil.rmtree(newd)
            except Exception as e:
                log('删目录失败: %r' % (e,))
            ok += 1
            if pending_dirs():
                log('%d 秒后传下一个' % IN_RUN_GAP)
                time.sleep(IN_RUN_GAP)
            continue

        fails += 1
        wait = BACKOFF[min(fails - 1, len(BACKOFF) - 1)]
        save_state(int(time.time()) + wait, fails, len(pending_dirs()))
        log('失败（rc=%d，%ds），武装保留，%d 秒后再试（连续失败 %d 次）'
            % (rc, dt, wait, fails))
        return 1


if __name__ == '__main__':
    sys.exit(main())
