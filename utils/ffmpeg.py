
import os
import subprocess
import tempfile
from threading import Thread
import logging

import math

from utils.timestamp import timestamp2sec, sec2timestamp

def get_length(filename):
    if not filename:
        return "0"
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            '-sexagesimal',
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            filename
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    return str(result.stdout)[2:-5] #float() without -sexagesimal

def get_length_using_copied_audio(filename: str):
    temp_audio_file = os.path.join(
            tempfile.gettempdir(),
            'get_length_acodec_temp.mp4'
        )
    try:
        os.remove(temp_audio_file)
    except OSError:
        pass
    try:
        subprocess.call([
            'ffmpeg',
            '-i',
            filename,
            '-reset_timestamps',
            '1',
            '-vn',
            '-acodec',
            'copy',
            temp_audio_file,
            ])
        if not os.path.isfile(temp_audio_file):
            # ffmpeg 放弃时压根不会生成临时文件。原代码在这里直接
            # os.remove，会抛 FileNotFoundError，把“音频流损坏”这个
            # 真正的原因盖掉，日志里只能看到一句莫名其妙的删除失败。
            raise RuntimeError(
                f'无法读取 {filename} 的音频流：'
                'ffmpeg 未能复制出音频，文件很可能已损坏')
        result = get_length(temp_audio_file)
    finally:
        try:
            os.remove(temp_audio_file)
        except OSError:
            pass
    return result

def split_in_half(filename, ):
    length = timestamp2sec(get_length(filename))/2
    cmds = [
        'ffmpeg -i "{}" {} -c:v copy -c:a copy "{}"'.format(
            filename, 
            '-to {}'.format(str(length)),
            filename[:filename.rfind('.')] + "_a" + filename[filename.rfind('.'):]),
        'ffmpeg -i "{}" {} -c:v copy -c:a copy "{}"'.format(
            filename, 
            '-ss {} '.format(str(length)),
            filename[:filename.rfind('.')] + "_b" + filename[filename.rfind('.'):]),
        
            ]
    k = [Thread(target=ffmpeg, args=(x,)) for x in cmds]
    for i in k:
        i.start()
    k[-1].join()
    os.remove(filename)

def ffmpeg(cmd, wait = True):
    logging.info(('calling', cmd, 'in terminal:'))
    process = subprocess.Popen(cmd)
    if wait:
        process.wait()
    return 1

def get_segment_process_length_array(filename: str, thres: int = 0,
                                    overlap: int = 0):
    if not thres:
        return [[None, None]]
    try:
        file_length = timestamp2sec(get_length(filename))
    except Exception:
        file_length = 0
    if file_length == 0:
        logging.warning(f'ffprobe on {filename} length failed.\
            now extracting the audio and probing the resulting audio file.')
        file_length = timestamp2sec(get_length_using_copied_audio(filename))
        # something is wrong; happens with DDrecorder's raw streams.
        # i just copy the audio segment and probe that one instead.
    if file_length <= 0:
        # 拿不到时长时，原代码会走到下面的 return [[None, None]]，
        # 于是 5~6 小时的文件被当成一整块读进内存 —— 这台 16GB 的机器
        # 会直接 MemoryError 崩溃。与其 OOM，不如报一个能看懂的错误。
        raise ValueError(
            f'无法确定 {filename} 的时长，已放弃分块处理；'
            '音频流很可能已损坏')
    logging.info((filename, 'total seconds', sec2timestamp(file_length)))
    if thres > file_length:
        return [[None, None]]
    logging.debug((f'filelength {str(file_length)} is \
        larger than thres {str(thres)}, triggering a segmentation.'))
    result = []
    for x in range(math.ceil(file_length / thres)):
        # 每块前后各扩 overlap 秒，让边界附近的音频在两块里都完整出现，
        # 避免一首歌被硬切在边界上、短的那半被当成闲聊扔掉。
        result.append([max(0, x * thres - overlap),
                       (x + 1) * thres + overlap])
    result[0][0] = None
    result[-1][1] = None
    return result