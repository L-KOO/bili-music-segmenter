#!/usr/bin/env python3
"""验证码过了之后，立即把积压的待上传目录传上去，不等 celery 的定时重试。

为什么需要它：
  B 站风控会拦死 biliup，celery 的 retry_upload 在两次尝试之间 sleep 5~30 分钟，
  而且这个 sleep 占着 -c 1 的唯一 worker 槽位。就算你在浏览器里手动上传解除了
  风控，队列也要等它睡醒才动。这个脚本绕过 celery，直接调 biliup 上传。

怎么"检测"验证码已过：
  没有可靠的旁路信号（你是在浏览器里操作的），所以检测手段就是真的试一次：
  上传成功 = 风控已解除；失败 = 还在拦，指数退避。

和 celery 并存怎么办：
  上传前先把目录改名。celery 里那个还在 sleep 的旧任务拿着旧路径只会失败退出，
  物理上不可能重复上传。另外要求当前没有 biliup 进程在跑。

用法：
  python upload_resume.py [--dry-run]
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
DRY = '--dry-run' in sys.argv

# 失败退避：第 1/2/3/4/5+ 次失败后分别等多少秒
BACKOFF = [120, 240, 480, 900, 1800]
# 一次成功后，隔多久再传下一个（不给 B 站风控再抓一次的机会）
SUCCESS_GAP = int(os.environ.get('RESUME_SUCCESS_GAP', '900'))
# 单次运行最多传几个（防止一次把整个队列灌上去）
MAX_PER_RUN = int(os.environ.get('RESUME_MAX_PER_RUN', '3'))


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
    now = int(time.time())
    next_try, fails, last_count = load_state()

    dirs = pending_dirs()
    count = len(dirs)

    # 目录数变少 = 有进展：可能是 celery 传成功了，也可能是用户在收到告警后
    # 手动上传并清掉了目录。两种都说明「情况变了，该重新试一次」，统一解除退避。
    if last_count is not None and count < last_count:
        log('目录数 %d -> %d，判定有进展，解除退避' % (last_count, count))
        next_try, fails = 0, 0

    if count == 0:
        # 队列空了：一起归零并落盘（不能只在非零时写，重置后恒为假）
        save_state(0, 0, 0)
        log('没有待上传目录')
        return 0

    if now < next_try:
        log('退避中，还有 %d 秒' % (next_try - now))
        save_state(next_try, fails, count)
        return 0

    if biliup_running():
        log('有 biliup 正在上传，跳过')
        save_state(next_try, fails, count)
        return 0

    log('待上传 %d 个，开始尝试' % count)
    ok = 0
    for d in dirs[:MAX_PER_RUN]:
        name = os.path.basename(d)
        newd, cmd = take(d)
        if newd is None:
            continue                     # take() 内部已记日志

        if DRY:
            log('[DRY] 会上传 %s -> %s' % (name, os.path.basename(newd)))
            ok += 1
            continue

        paths = media_paths(cmd)
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            # 文件已经被清走了，调 biliup 也没用，直接丢掉这一单
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
            if ok < min(count, MAX_PER_RUN):
                save_state(int(time.time()) + SUCCESS_GAP, 0, count - ok)
                log('还有待传，%d 秒后再来' % SUCCESS_GAP)
                return 0
            continue

        log('失败（rc=%d，%ds）' % (rc, dt))
        fails += 1
        wait = BACKOFF[min(fails - 1, len(BACKOFF) - 1)]
        save_state(int(time.time()) + wait, fails, count)
        log('风控大概还没解除，%d 秒后再试（连续失败 %d 次）' % (wait, fails))
        return 1

    save_state(0, 0, max(0, count - ok))
    log('本轮完成，成功 %d 个' % ok)
    return 0


if __name__ == '__main__':
    sys.exit(main())
