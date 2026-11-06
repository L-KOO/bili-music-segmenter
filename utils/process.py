import subprocess
import logging

# 上传之间那 40 分钟的间隔已经挪到 inacelery/celery.py 的 add 任务里了。
# 放在这里会挡在分割之前，白白占掉每场 38 分钟。


def cell_stdout(cmd, silent=False, encoding=None):
    logging.info(['calling', cmd, 'in terminal:'])
    with subprocess.Popen(cmd, stdout=subprocess.PIPE,
                          universal_newlines=True, encoding=encoding) as p:
        if not silent:
            try:
                for i in p.stdout:  # .decode("utf-8"):
                    logging.debug(i)
            except UnicodeDecodeError:
                # 锟斤拷锟斤拷锟斤拷锟斤拷锟斤拷锟斤拷锟斤拷
                logging.warning('decode failed! \
                    but at least you have this eror message...')
        p.wait()
    return p.returncode