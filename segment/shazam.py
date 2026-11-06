import os
import glob
import logging
import shutil
import subprocess
import tempfile
import uuid
import regex
import requests
import asyncio
from shazamio import Shazam

from utils.logging import save_timestamps


# ---- 识歌参数 -----------------------------------------------------------
# 原来是把整段（3~8 分钟）音频一次性丢给 Shazam。前奏、掌声、口白都会
# 污染指纹，而 Shazam 的匹配本来就是按 10~15 秒设计的，给太长反而更难中。
# 所以改成从音频里切一小段送过去，不中再换个位置重试。
PROBE_SECONDS = 12
# 依次尝试的取样位置（按整段时长的比例）。先中间，那里基本是副歌。
PROBE_FRACTIONS = (0.5, 0.25, 0.75)
# 相邻请求之间的间隔（秒），压太低容易被限流
PAUSE = 1.0
# 同时在途的请求数
CONCURRENCY = 4

semaphore = asyncio.Semaphore(CONCURRENCY)
myshazam = Shazam()


async def shazam_orig(file, **kwargs):
    match = await shazam(file)
    return shazam_title(match), match


async def shazaming(
    outdir, media, shazam_coverart_path='',
    shazam_func=shazam_orig, ignore_fails=False
):
    mediab = os.path.basename(media)
    files = glob.glob(os.path.join(
        outdir, '*' + os.path.splitext(mediab)[0][1:] + '_*'
    ))
    await asyncio.gather(*[shazam_threaded(
        file, shazam_coverart_path=shazam_coverart_path,
        shazam_func=shazam_func, ignore_fails=ignore_fails
    ) for file in files])
    save_timestamps(mediab=mediab,
                    key='shazam', val=[
                        os.path.basename(x)
                        for x in glob.glob(
                            os.path.join(
                                outdir, f"*{mediab[1: mediab.rfind('.')]}*")
                        )
                    ])


async def shazam_threaded(
    file, shazam_coverart_path='',
    shazam_func=shazam_orig, ignore_fails=True
):
    results = {}
    if ' by ' in file:
        return
    filename = file[:file.rfind('.')]
    fileext = file[len(filename):]
    fn = os.path.basename(filename)
    logging.info(['shazaming', fn])
    try:
        results[fn], match = await shazam_func(file)
        try:
            logging.info([fn, 'shazam found to be', results[fn]])
        except UnicodeEncodeError:
            logging.warning(
                [fn, 'shazam found but cant show unicode burr durr'])
        renamed_file = os.path.join(
            os.path.dirname(file),
            (fn + f"_{results[fn][0].replace(':', ' ')} by "
                  f"{results[fn][1].replace(r'/', '')}") + fileext
        )
        shutil.move(file, renamed_file)
        if os.path.isdir(shazam_coverart_path):
            shazam_coverart(match, renamed_file, shazam_coverart_path)
    except (IndexError, KeyError):
        logging.error([fn, 'shazam failed'])
    except Exception:
        if not ignore_fails:
            raise


def probe_duration(path):
    """取音频时长（秒）。拿不到就返回 0，调用方退化成从头切。"""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=60).stdout
        return float((out or '0').strip())
    except Exception:
        return 0.0


def make_probe(src, duration, fraction):
    """从 src 的 fraction 位置切 PROBE_SECONDS 秒音频，返回临时文件路径。"""
    if duration > 0:
        start = max(0.0, duration * fraction - PROBE_SECONDS / 2.0)
        if start + PROBE_SECONDS > duration:
            start = max(0.0, duration - PROBE_SECONDS)
    else:
        start = 0.0
    dst = os.path.join(
        tempfile.gettempdir(),
        'shazam_probe_%d_%s.mp3' % (os.getpid(), uuid.uuid4().hex[:10]))
    try:
        subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-ss', '%.3f' % start,
             '-t', str(PROBE_SECONDS), '-i', src, '-vn', '-ac', '1',
             '-ar', '44100', '-b:a', '128k', dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        pass
    if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
        return dst
    try:
        os.remove(dst)
    except OSError:
        pass
    return None


async def shazam(mp3):
    """多点取样识别：中间 -> 前段 -> 后段 -> 整段兜底，任一命中即返回。

    改动要点：
    - 送 12 秒片段而不是整段，指纹更干净；
    - 换 3 个位置重试，命中率明显高于单次；
    - 原来这里用的是阻塞的 time.sleep(3)，会把整个事件循环冻住，
      asyncio.gather 的并发完全失效；改成 await asyncio.sleep。
    """
    duration = await asyncio.to_thread(probe_duration, mp3)

    probes = await asyncio.gather(*[
        asyncio.to_thread(make_probe, mp3, duration, f)
        for f in PROBE_FRACTIONS])

    for probe in probes:
        if not probe:
            continue
        try:
            async with semaphore:
                match = await myshazam.recognize(probe)
                await asyncio.sleep(PAUSE)
            if isinstance(match, dict) and match.get('track'):
                return match['track']
        except Exception as e:
            logging.warning(['shazam probe error', os.path.basename(mp3),
                             repr(e)])
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass

    # 兜底：还认不出就用整段再试一次（即改造前的行为，不会比原来差）
    try:
        async with semaphore:
            match = await myshazam.recognize(mp3)
            await asyncio.sleep(PAUSE)
        if isinstance(match, dict) and match.get('track'):
            return match['track']
    except Exception as e:
        logging.warning(['shazam whole-file error', os.path.basename(mp3),
                         repr(e)])

    raise KeyError('no shazam match: %s' % os.path.basename(mp3))


def legalize_filename(fn):
    if regex.search(r'\p{IsHangul}', fn) is not None:
        raise KoreanCharException(fn)
    illegal_list = [
        [':', ' '],
        ['"', ''],
        [r'/', ''],
        [r'?', ''],
        [r'*', ''],
        ['\'', ''],
        ['<', ''],
        ['>', ''],
    ]
    for i in illegal_list:
        fn = fn.replace(i[0], i[1])
    return fn


def shazam_title(match):
    """取 title / subtitle。

    原实现里有一段 in the style of 的分支，算了个 artist 却从没被用过，
    而且 subtitle 缺字段时直接 KeyError。这里统一成 get + 空值判断：
    缺 title 当失败处理，不再把空结果误判成命中。
    """
    title = legalize_filename((match.get('title') or '').strip())
    subtitle = legalize_filename((match.get('subtitle') or '').strip())
    if not title:
        raise KeyError('shazam returned no title')
    return [title, subtitle]


def shazam_coverart(match, fn, outdir):
    try:
        albumart = match['images']['coverarthq']
        req = requests.get(albumart)
        with open(os.path.join(
            outdir, os.path.basename(fn) + albumart[albumart.rfind('.'):]
        ), 'wb') as f:
            f.write(req.content)
    except Exception:
        pass


class KoreanCharException(BaseException):
    pass
