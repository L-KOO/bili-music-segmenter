from celery import Celery
import subprocess
import logging
import json
import shutil
import os
import time
# 1. 导入 random 模块用于生成随机数
import random

from utils.util import retry


class SourceGone(BaseException):
    """源文件已不存在，重试没有任何意义。

    故意继承 BaseException：utils.util.retry 只捕获 Exception，
    所以它会直接穿透重试装饰器 —— 不消耗 15 次重试、不 sleep 5~30 分钟、
    立即把 -c 1 的唯一 worker 槽位让给下一个任务。
    """


def media_paths(cmd):
    """从 biliup 的 argv 里取出真正的文件参数。

    cmd 形如 ['biliup','upload','/a.mp4','/b.mp4','--title=x','-l=qn']：
    前两个是程序名与子命令，其余以 '-' 开头的都是参数（-l=qn 是单横线）。
    """
    return [str(x) for x in cmd[2:] if not str(x).startswith('-')]


def ensure_media(cmd):
    paths = media_paths(cmd)
    if not paths:
        raise SourceGone('cmd 里没有文件参数')
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        raise SourceGone('%d/%d 个文件已不存在（例：%s）'
                         % (len(missing), len(paths), missing[0]))


def cleanup_dir(cmd):
    """只在目录里一个 mp4 都不剩时才删，避免误删还没处理完的内容。"""
    paths = media_paths(cmd)
    if not paths:
        return
    d = os.path.dirname(paths[0])
    if not os.path.isdir(d):
        return
    left = [n for n in os.listdir(d) if n.lower().endswith('.mp4')]
    if left:
        logging.warning(['目录里还有 %d 个 mp4，不清理' % len(left), d])
        return
    try:
        shutil.rmtree(d)
        logging.info(['已清理空目录', d])
    except Exception as e:
        logging.error(['清理目录失败', d, repr(e)])

app = Celery('tasks', broker='sqla+sqlite:///celerydb.sqlite')

# 两次上传之间的间隔（秒）。原来这 38 分钟是 sleep 在 utils/process.py 里、
# 挡在分割之前的，等于每场白白浪费 38 分钟；现在改成上一场传完之后再等，
# 分割/切片照常跑，不占用机器时间。可用环境变量 UPLOAD_INTERVAL 覆盖。
UPLOAD_INTERVAL = int(os.environ.get('UPLOAD_INTERVAL', '2400'))

# 2. 生成 5分钟(300秒) 到 30分钟(1800秒) 之间的随机秒数
#    random.randint(a, b) 会返回 [a, b] 区间内的整数（包含边界值）
random_timeout = random.randint(300, 1800)

# 3. 将随机秒数传入 retry 装饰器的 timeout 参数
@retry(times=15, timeout=random_timeout, exceptions=(Exception,))  # 注意：Exception 后加逗号，避免被当作单个元素而非元组
def retry_upload(cmd):
    ensure_media(cmd)          # 每次重试前都校验：文件没了就没必要重试
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        logging.warning('biliup failed... retrying.')
        raise Exception('upload failed.')
    

@app.task
def add(cmd):
    cmd = json.loads(cmd)
    try:
        retry_upload(cmd)
    except SourceGone as e:
        # 跳过：不重试、不 sleep(UPLOAD_INTERVAL)，立刻把 worker 让给下一个任务
        logging.warning(['skip: 源文件已不在，放弃这个任务', str(e)])
        cleanup_dir(cmd)
        return
    logging.info([cmd, 'completed.'])
    logging.info(['removing', cmd[2]])
    # 4. 增加异常捕获（可选但推荐）：避免删除目录失败导致任务崩溃
    try:
        shutil.rmtree(os.path.dirname(cmd[2]))
    except Exception as e:
        logging.error(f'Failed to remove directory {os.path.dirname(cmd[2])}: {str(e)}')
    # 这一场传完之后再等 UPLOAD_INTERVAL，然后才轮到下一场。
    # celery worker 是 -c 1 串行的，所以这就是「上一场传完 → 等 40 分钟 → 传下一场」。
    logging.info([
        'upload done, waiting', UPLOAD_INTERVAL,
        'seconds before the next upload'])
    time.sleep(UPLOAD_INTERVAL)