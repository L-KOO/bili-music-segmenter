import logging
import glob
import json
import os
import subprocess
import multiprocessing
from datetime import datetime

from network.cookieformatter import biliup_to_ytbdl_cookie_write2file
from network.download import ytbdl
from utils.filename import strip_medianame_out, put_medianame_backin
from utils.process import cell_stdout
from network.biliupload import bilibili_upload, BILIUP_ROUTE


# 失败记账：损坏的录播会让分割必然失败，而下一轮 12 小时循环又会把同一个
# 文件捡起来、白跑十几分钟再失败。这里记一个失败次数，达到上限就跳过。
# 想重试某个文件，把 _failed_media.json 里对应那一条删掉即可。
FAIL_LEDGER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '_failed_media.json')
FAIL_LIMIT = 2


def _load_ledger():
    try:
        with open(FAIL_LEDGER, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ledger(data):
    try:
        with open(FAIL_LEDGER, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception as e:
        logging.error(['写入失败记账文件出错', repr(e)])


def failure_count(media):
    return int(_load_ledger().get(media, 0))


def mark_failure(media):
    data = _load_ledger()
    data[media] = int(data.get(media, 0)) + 1
    _save_ledger(data)
    return data[media]


def clear_failure(media):
    data = _load_ledger()
    if media in data:
        del data[media]
        _save_ledger(data)


class InaBiliup():

    def __init__(
        self,
        media: str,
        outdir: str = os.getcwd(),
        episode_limit: int = 180,
        shazam_thread: int = min(max(multiprocessing.cpu_count(), 1), 4),
        ignore_errors: bool = True,
        sound_only: str = '',
        route: str = BILIUP_ROUTE,
        cleanup: bool = True,
        no_biliup: bool = False,
        use_celery: bool = True,
    ):
        self.cleanup = True  # False
        self.outdir = outdir
        self.media = media
        self.episode_limit = episode_limit
        self.shazam_thread = shazam_thread
        self.ignore_errors = ignore_errors
        self.sound_only = sound_only
        self.route = route
        self.cleanup = cleanup
        self.no_biliup = no_biliup
        self.use_celery = use_celery

    def run(self):
        try:
            media = self.media
            outdir = self.outdir
            if media == '':
                return
            _fails = failure_count(media)
            if _fails >= FAIL_LIMIT:
                logging.warning([
                    media, '已连续失败', _fails, '次，跳过不再重试；',
                    '删掉', FAIL_LEDGER, '里对应那条即可重试'])
                return
            logging.info(f'inaseging {media} at ' +
                         datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            os.chdir(outdir)

            media_url = ''
            if 'https:' in media:
                media_url = media
                # use biliup to renew the cookie file, and write to ytdlp netscape format.
                subprocess.call(['biliup', 'renew'])
                biliup_to_ytbdl_cookie_write2file()
                # , outdir = outdir
                media = ytbdl(media, soundonly=self.sound_only, aria=16)
            if not cell_stdout([
                'python',
                'inaseg.py',
                '--media={}'.format(media),
                '--outdir={}'.format(outdir),
                '--soundonly', self.sound_only,
                '--shazam',
                    '--cleanup']) == 0:
                raise BaseException()
            # inaseg failed?
            logging.info(['inaseg completed on', media])
            stripped_media_names = strip_medianame_out(outdir, media)
            logging.info(['preparing to upload', stripped_media_names])

            bilibili_upload(
                stripped_media_names, os.path.basename(media),
                source=None, episode_limit=self.episode_limit, useCelery=self.use_celery)
            logging.info(['finished stripping and uploading', media])
            clear_failure(media)
            if self.cleanup:
                if os.path.isfile(media):
                    os.remove(media)
                for i in stripped_media_names:
                    os.path.isfile(i) and os.remove(i)
            else:
                put_medianame_backin(
                    stripped_media_names, media,
                    shazamed=r'D:\tmp\ytd\convert2music',
                    nonshazamed=r'D:\tmp\ytd\extract')
        except KeyboardInterrupt:
            raise
        except BaseException:
            if self.ignore_errors:
                for i in glob.glob('*.mp4') + glob.glob('*.aria2') + \
                        glob.glob('*.part'):
                    os.path.isfile(i) and os.remove(i)
                # if os.path.isfile(media): os.remove(media)
                _n = mark_failure(media)
                logging.error(f'{media} failed ({_n}/{FAIL_LIMIT}). file is \
removed in automatic error ignore handler')
                if _n >= FAIL_LIMIT:
                    logging.error([
                        media, '连续失败已达上限，之后的循环会跳过它；',
                        '确认要重试就删掉', FAIL_LEDGER, '里对应那条'])
            else:
                raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='ina music segment')
    parser.add_argument('--media', type=str, nargs='+',
                        help='file path or weblink')
    logging.basicConfig(level=logging.DEBUG, handlers=[
        logging.FileHandler('./inaseg.log'),
        logging.StreamHandler()
    ])
    args = parser.parse_args()
    for media in args.media:
        logging.info(
            f'inaseging {media} at ' +
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        InaBiliup(media=media, use_celery=False).run()
