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
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        logging.warning('biliup failed... retrying.')
        raise Exception('upload failed.')
    

@app.task
def add(cmd):
    cmd = json.loads(cmd)
    retry_upload(cmd)
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