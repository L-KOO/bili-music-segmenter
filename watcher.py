import time
import os
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

from network.watcher import watch


STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.processed_media.json')


def load_processed():
    """已处理文件集合。读不到就当空集，不让状态文件问题阻塞主流程。"""
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(str(x) for x in data)
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(['读取已处理状态失败，按空集继续', repr(e)])
    return set()


def save_processed(s):
    """原子落盘：先写临时文件再 replace，避免断电留下半个 json。"""
    tmp = STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sorted(s), f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logging.error(['写入已处理状态失败', repr(e)])


def is_file_stable(path, wait=10, checks=2):
    """判断文件是否已经写完。

    要求：mtime 距今 >= wait，且连续 checks 轮（每轮间隔 wait 秒）
    大小与 mtime 都不变。这样跨卷 cp 正在增长的目标文件也会被判为不稳定。
    """
    try:
        s1 = os.path.getsize(path)
        m1 = os.path.getmtime(path)
    except OSError:
        return False
    if time.time() - m1 < wait:
        return False
    for _ in range(checks):
        time.sleep(wait)
        try:
            s2 = os.path.getsize(path)
            m2 = os.path.getmtime(path)
        except OSError:
            return False
        if s2 != s1 or m2 != m1:
            return False
        s1, m1 = s2, m2
    return True


if __name__ == '__main__':
    from biliup import InaBiliup
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='ina music segment')
    parser.add_argument(
        '--watch_interval',
        type=int,
        default=0,
        help='in seconds. 0 means no repeat.')
    parser.add_argument(
        '--log_level',
        type=int,
        default=logging.INFO,
        help='log level')
    parser.add_argument(
        '--stable_wait',
        type=int,
        default=10,
        help='file must be unchanged for this many seconds')
    parser.add_argument(
        '--stable_checks',
        type=int,
        default=2,
        help='how many consecutive stable rounds required')
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, handlers=[
        RotatingFileHandler(
            './inaseg.log', maxBytes=32 * 1024 * 1024,
            backupCount=3, encoding='utf-8'),
        logging.StreamHandler()
    ])
    while True:
        logging.info([
            'biliWatcher loop has started on ',
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
        processed = load_processed()
        candidates = list(reversed(watch()))
        logging.info(['本轮候选', len(candidates), '已处理', len(processed)])
        for i in candidates:
            if i in processed:
                logging.debug(['跳过（已处理）', i])
                continue
            if not is_file_stable(
                    i, wait=args.stable_wait, checks=args.stable_checks):
                logging.info([
                    '跳过（文件仍在写入或刚落地，下轮再试）', i])
                continue
            logging.info([
                'calling biliupWrapper on', i, 'at',
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
            InaBiliup(media=i).run()
            processed.add(i)
            save_processed(processed)
        logging.info([
            'biliWatcher loop has completed on ',
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')])
        if args.watch_interval < 1:
            sys.exit(0)
        logging.debug(
            ['biliWatcher loop is now waiting for ', args.watch_interval])
        time.sleep(args.watch_interval)
