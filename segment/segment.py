#  Load the API
from inaSpeechSegmenter import Segmenter  # noqa: E402
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
import gc
import logging
import tensorflow as tf

from utils.ffmpeg import get_segment_process_length_array, ffmpeg
from utils.timestamp import fix_missing_stamps, sec2timestamp
from utils.logging import save_timestamps

# 媒体流最大时长处理（秒）；1G内存的进程推荐用10分钟/600秒，16G可以支持5小时，6GB VRAM可以支持5小时左右。
SEGMENT_THRES = 3600
# 分块之间前后各多取这么多秒的重叠。硬切会把压在边界上的歌切成两半，
# 其中较短的一半（不足 EXTRACT_SEG_THRES）会被当成闲聊丢掉，歌就短一截。
# 加 30 秒重叠后相邻两块都能完整覆盖边界，再由 _merge_overlapping 去重，
# 代价只有 60/3600 ≈ 1.7% 的额外计算。
SEGMENT_OVERLAP = 30
# 识歌分段最小阈值（秒），调大了会漏 调小了会多杂谈
EXTRACT_SEG_THRES = 60
# 最终识歌分段最小阈值（秒），调大了漏TV size 调小了多杂谈
EXTRACT_SEG_THRES_FINAL = 80
# 识歌分段连接的阈值（秒），调大了会两首歌分不开 调小了会碎
EXTRACT_SEG_CONNECT = 5
# 大了会碎 小了会两首歌分不开
ENERGY_RATIO = 0.03
# 实测（i3-6100 / 2C4T / 16GB 内存）：batch=16 → 17.1x 实时，
# batch=64 → 23.8x，batch=128 → 19.3x（反而变慢，4 个逻辑核超订了）
# 所以 64 是这台机器的甜点，不要再往上加。
BATCH_SIZE = 64


class TimestampMismatch(Exception):
    pass

# select a media to analyse
#  any media supported by ffmpeg may be used (video, audio, urls)


def segment(
        media: str, batch_size: int = BATCH_SIZE, energy_ratio: float = ENERGY_RATIO,
        start_sec: int = None, stop_sec: int = None):
    segmenter = Segmenter(
        vad_engine='sm',
        detect_gender=False,
        energy_ratio=energy_ratio,
        batch_size=batch_size)
    segmentation = segmenter(media, start_sec=start_sec, stop_sec=stop_sec)
    return segmentation


def _merge_overlapping(prev, chunk):
    """把新一块的分割结果并入已有列表，处理块与块之间的重叠区。

    分块现在前后各多取 SEGMENT_OVERLAP 秒，所以相邻两块会给出时间上重叠、
    标签基本一致的片段。这里：
      - 同标签且重叠 -> 并成一段，而不是留下两条交叠的条目
      - 不同标签且重叠 -> 后一块截到前一段结尾，丢掉重叠部分
    保证返回列表仍然按时间单调、互不重叠 —— 后面的 extract_music 依赖这点。
    """
    if not prev:
        return list(chunk)
    merged = list(prev)
    for seg in chunk:
        label, start, end = seg[0], seg[1], seg[2]
        if end <= start:
            continue
        last = merged[-1]
        last_label, last_start, last_end = last[0], last[1], last[2]
        if start < last_end:
            if label == last_label:
                if end > last_end:
                    merged[-1] = (last_label, last_start, end)
                continue
            start = last_end
            if start >= end:
                continue
        merged.append((label, start, end))
    return merged


def segment_wrapper(
        media: str, batch_size: int = BATCH_SIZE,
        energy_ratio: float = ENERGY_RATIO, segment_length_thres: int = 0,
        overlap: int = SEGMENT_OVERLAP):
    ''''''
    result = []
    for i in get_segment_process_length_array(
            media, segment_length_thres, overlap=overlap):
        logging.info([
            'segmenting', media, 'from',
            sec2timestamp(i[0]), 'to', sec2timestamp(i[1])])
        result = _merge_overlapping(result, segment(
            media, batch_size, energy_ratio,
            start_sec=i[0], stop_sec=i[1]))
        gc.collect()
        tf.keras.backend.clear_session()
    return result


def extract_music(
        segmentation, segment_thres=EXTRACT_SEG_THRES,
        segment_thres_final=EXTRACT_SEG_THRES_FINAL,
        segment_connect=EXTRACT_SEG_CONNECT, start_padding=1, end_padding=4):
    r = []
    # bridges noEnergy segments that are likely fragmented
    for i in range(len(segmentation)-2, 0, -1):
        if segmentation[i][0] == 'noEnergy' and \
                segmentation[i][2] - segmentation[i][1] < 4 and \
                segmentation[i-1][0] == segmentation[i+1][0]:
            segmentation[i-1] = (
                segmentation[i-1][0],
                segmentation[i-1][1],
                segmentation[i+1][2])
    for i in segmentation:
        if i[0] == 'music' and i[2]-i[1] > segment_thres:
            r.append(['', i[1] - start_padding, i[2] + end_padding])
    for i in range(len(r)-1, 0, -1):
        if r[i][1] - r[i-1][2] < segment_connect:
            r[i-1][2] = r[i][2]
            r[i][1] = r[i][2] + 1
    rf = []
    for i in r:
        if i[1] < 5:
            continue
        if i[2]-i[1] > segment_thres_final:
            rf.append(i)
    return [['{}:{}:{}'.format(str(int(x[1]//3600)),
                               str(int(x[1] % 3600 // 60)),
                               str(int(x[1] % 60)).zfill(2)),
             '{}:{}:{}'.format(str(int(x[2]//3600)),
                               str(int(x[2] % 3600 // 60)),
                               str(int(x[2] % 60)).zfill(2))] for x in rf]


def run_cmds_bounded(cmds, workers=None):
    """按固定并发数跑 ffmpeg，而不是每个片段开一个线程。

    原实现是 38 个片段就起 38 个 ffmpeg，同时去读同一个机械盘上的源文件。
    38 路并发随机读会把顺序读打成随机读，吞吐塌到几十 MB/s，整个切片阶段
    反而比串行还慢。限制到 2~4 路后磁盘访问基本连续，顺序读能跑满带宽。

    并发数可以用环境变量 INASEG_CUT_WORKERS 覆盖，默认 3。
    """
    if not cmds:
        return
    if workers is None:
        try:
            workers = int(os.environ.get('INASEG_CUT_WORKERS', '3'))
        except ValueError:
            workers = 3
    workers = max(1, min(workers, len(cmds)))
    logging.info(['cutting', len(cmds), 'segments with', workers, 'workers'])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # list() 是为了让异常在提交方抛出，与原 join 语义一致
        list(pool.map(ffmpeg, cmds))


def extract_mah_stuff(
        media, segmented_stamps, outdir=None, rev=False,
        delimited='/', timestamps=[], soundonly=True):
    nameswitch = False
    timestamps_ext = segmented_stamps
    try:
        if len(timestamps) > 0:
            raise FileNotFoundError()
        for i in open(r"D:\tmp\ytd\timstamp.ini", 'r', encoding='UTF-8'):
            for k in [
                ['\n', ''], ['」', ''], ['~「', ' '], ['「', ' '],
                    ['『', ' '], ['』', ' '], ['　', ' '], ['	', ' '], ['\'', ''],]:
                i = i.replace(k[0], k[1])
            if ' （' in i:
                i = i[:i.find(' （')]
            if ':' in i:
                timestamps.append([i[:i.find(' ')], i[i.find(' ')+1:]])
                # timestamps[-1][1] = timestamps[-1][1][1:]
                if delimited in timestamps[-1][1]:
                    timestamps[-1][1] = [
                        timestamps[-1][1][:timestamps[-1][1].find('/')],
                        timestamps[-1][1][timestamps[-1][1].find('/')+1:]]
                    if rev:
                        timestamps[-1][1].reverse()
                    timestamps[-1][1] = ' by '.join(timestamps[-1][1])
                # timestamps[-1][1].replace('/', ' by ')
                # timestamps[-1][1] = ' by '.join(timestamps[-1][1].split('/'))
                # timestamps[-1][1] = timestamps[-1][1].replace('-', 'by')
                while timestamps[-1][1][0] == ' ':
                    timestamps[-1][1] = timestamps[-1][1][1:]
                while timestamps[-1][1][-1] == ' ':
                    timestamps[-1][1] = timestamps[-1][1][:-1]
        #        nameswitch = True
            elif nameswitch:
                nameswitch = False
                timestamps[-1].append(i)
        if len(timestamps) > 0:  # and len(timestamps) != len(timestamps_ext):
            logging.info('checking timestamp correspondence and removing\
                mismatched ones (come on, are you really gonna do this manually)')
            timestamps = fix_missing_stamps(timestamps, timestamps_ext)
            timestamps_ext = fix_missing_stamps(timestamps_ext, timestamps)
            if len(timestamps) != len(timestamps_ext):
                raise TimestampMismatch(
                    'check timestamp assist', timestamps, timestamps_ext)
        with open(r"D:\tmp\ytd\timstamp.ini", 'w') as f:  # noqa: F841
            pass
    except FileNotFoundError:
        pass
    if len(timestamps) > 0:
        logging.info([
            'timestamp assist',
            [[timestamps[x][0],
              timestamps_ext[x][1], timestamps[x][1], ]
              for x in range(len(timestamps))]])
    else:
        logging.info([
            'extracted timestamps',
            ['{} - {}'.format(x[0], x[1]) for x in timestamps_ext]])
    try:
        for count, x in enumerate(timestamps_ext):
            logging.info(f'{str(count).zfill(2)}: {x[0]} - {x[1]}')
    except Exception:
        pass
    save_timestamps(mediab=os.path.basename(media),
                    key='timestamps', val=timestamps_ext)
    nameswitch = False
    file = media
    filename = file[:file.rfind('.')]
    fileext = file[len(filename):]
    filename = os.path.basename(filename)
    cmds = []
    for i in range(len(timestamps_ext)):
        oud = outdir if outdir else os.path.dirname(file)
        # 输出目录不存在时 ffmpeg 会直接失败，但下面的 ffmpeg() 不检查返回码，
        # 日志照样打 "successful"。这里补一行，避免静默丢产物。
        os.makedirs(oud, exist_ok=True)
        encoding = ['-c:v', 'copy', '-c:a', 'copy']  # '-c:v copy -c:a copy'
        if soundonly:
            encoding = ['-vn', '-ab', '320k']  # '-vn -ab 320k'
            fileext = '.mp3'
        try:
            prefix = timestamps[i][1].zfill(2)
            cmds.append([
                'ffmpeg',
                '-ss',
                timestamps[i][0],
                '-to',
                timestamps_ext[i][1],
                '-i',
                file,
                '-reset_timestamps', '1',
            ] + encoding + [
                os.path.join(
                    oud, filename + f'_{str(i).zfill(2)}_{prefix}' + fileext),

            ] + encoding)
        except Exception:
            prefix = str(i).zfill(2)
            cmds.append([
                'ffmpeg',
                '-ss',
                timestamps_ext[i][0],
                '-to',
                timestamps_ext[i][1],
                '-i',
                "{}".format(file),
            ] + encoding + [
                "{}".format(os.path.join(
                    oud, filename + '_' + prefix + fileext)),
            ])
    run_cmds_bounded(cmds)
    # k[-1].join()