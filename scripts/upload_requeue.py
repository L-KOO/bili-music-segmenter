import json
import os
import re
import shutil
import sys
import time

"""把指定的待上传目录改名后重新投递到 celery。

为什么要改名：原任务可能还在 retry 循环里。直接再投一次，两个任务会传同一批
文件，B 站上就出现两份重复视频。改名之后旧路径下的文件不存在了，旧任务即使
还活着也只会失败退出，不可能重复上传。

用法:
  echo -e "目录名1\n目录名2" | python upload_requeue.py <最小年龄秒> 0
  不给 stdin 时默认挑所有超过最小年龄的目录。
输出: 重投后的目录名 JSON 列表
"""

UP = '/inaseg/inaupload'
min_age = int(sys.argv[1]) if len(sys.argv) > 1 else 1800
dry = (len(sys.argv) > 2 and sys.argv[2] == '1')

want = set()
if not sys.stdin.isatty():
    for line in sys.stdin:
        line = line.strip()
        if line:
            want.add(line)

sys.path.insert(0, '/inaseg')   # 脚本在 /inaseg/scripts 下，要把 /inaseg 加进路径
from inacelery.celery import add  # noqa: E402

now = int(time.time())
done = []
for name in sorted(os.listdir(UP)):
    if want and name not in want:
        continue
    d = os.path.join(UP, name)
    if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, 'cmd.txt')):
        continue
    if now - int(os.path.getmtime(d)) < min_age:
        continue
    try:
        names = os.listdir(d)
    except OSError:
        continue
    if not any(n.lower().endswith('.mp4') for n in names):
        print('SKIP %s: 没有 mp4 了' % name, file=sys.stderr)
        continue
    try:
        with open(os.path.join(d, 'cmd.txt'), encoding='utf-8') as f:
            cmd = json.load(f)
    except Exception as e:
        print('SKIP %s: %r' % (name, e), file=sys.stderr)
        continue

    base = re.sub(r'(\.retry\d+)+$', '', name)
    newd = os.path.join(UP, '%s.retry%d' % (base, now))

    if dry:
        done.append('%s -> %s' % (name, os.path.basename(newd)))
        continue

    shutil.move(d, newd)
    try:
        new_cmd = [str(x).replace(d + '/', newd + '/') for x in cmd]
        with open(os.path.join(newd, 'cmd.txt'), 'w', encoding='utf-8') as f:
            json.dump(new_cmd, f)
        add.delay(json.dumps(new_cmd))
    except Exception as e:
        shutil.move(newd, d)        # 投递失败就改回去，别让任务从队列里消失
        print('FAIL %s: %r' % (name, e), file=sys.stderr)
        continue
    done.append(os.path.basename(newd))

print(json.dumps(done, ensure_ascii=False))
