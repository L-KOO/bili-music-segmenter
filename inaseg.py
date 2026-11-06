#  Load the API
import os
import glob
import logging
import tempfile
import asyncio
from logging.handlers import RotatingFileHandler

# TF 在 2C4T 的机器上默认会开过多线程互相抢，显式钉住。
# 必须在 import tensorflow 之前设置 —— 下面 segment.segment 会拉起 tf。
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('TF_NUM_INTRAOP_THREADS', '4')
os.environ.setdefault('TF_NUM_INTEROP_THREADS', '1')

from segment.shazam import shazaming
from network.download import ytbdl
from segment.segment import extract_mah_stuff, extract_music, segment_wrapper,\
    SEGMENT_THRES, TimestampMismatch


class DropFFmpegSpam(logging.Filter):
    """丢掉损坏音频导致的刷屏日志。

    坏掉的录播会让 ffmpeg 每帧刷一条告警，实测单场能刷出上千万条，
    把 inaseg_inst.log 写到 9.5GB。这些行没有任何诊断价值，直接拦掉。
    """

    PATTERNS = (
        'invalid mpeg audio header',
        'skipping junk at',
        'channel element',
        'Error while decoding stream',
    )

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self.PATTERNS)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='ina music segment')
    parser.add_argument('--media', type=str, help='file path or weblink')
    parser.add_argument(
        '--outdir', type=str, default=tempfile.gettempdir(),
        help='directory media will be downloaded into (if url) and extracted into;\
             if docker, use as the mounted folder! and make sure -u is you!')
    parser.add_argument(
        '--shazam', action='store_true', default=False,
        help='shazam the extracted files')
    parser.add_argument(
        '--shazam_coverart', type=str, default='',
        help='save shazam cover art too')
    parser.add_argument(
        '--aria', type=int, default=None,
        help='use aria, specifying the number of threads')
    parser.add_argument(
        '--shazam_multithread', type=int, default=1,
        help='use multithreaded shazam')
    parser.add_argument(
        '--soundonly', type=str, default=r'-f bestaudio',
        help='specify an empty string to extract both video and audio')
    parser.add_argument(
        '--seg_connect', type=int, default=5,
        help='max seconds for 2 music segments to be considered \
            the same (takes care of random miss-detection/small rap segments)')
    parser.add_argument(
        '--cleanup', action='store_true', default=False,
        help='delete the original media file once completed.')
    parser.add_argument(
        '--max_segment_length',
        type=int,
        default=SEGMENT_THRES,
        help='for computers with limited RAM (eg a 5 hrs stream \
            requires ~6GB VRAM), set this to process streams in \
                this speficied segments to avoid ram overflow. in seconds.')
    _log_handlers = [
        RotatingFileHandler(
            './inaseg_inst.log', maxBytes=64 * 1024 * 1024,
            backupCount=3, encoding='utf-8'),
        logging.StreamHandler()
    ]
    _noise = DropFFmpegSpam()
    for _h in _log_handlers:
        _h.addFilter(_noise)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(message)s',
        handlers=_log_handlers)
    args = parser.parse_args()
    # media = ytbdl('')
    if args.media is not None:
        media = args.media
    else:
        media = 'https://www.bilibili.com/video/BV16S411w7dY/?spm_id_from=333.999.0.0'
        raise Exception('no media')
    if 'https:' in media:
        media = ytbdl(
            media, soundonly=args.soundonly,
            aria=args.aria, outdir=args.outdir)
    if len(glob.glob(os.path.join(
            args.outdir,
            f'*{os.path.splitext(os.path.basename(media))[0][1:]}_*'))) == 0:
        import tensorflow as tf
        gpus = tf.config.experimental.list_physical_devices('GPU')
        logging.info(gpus)
        tf.get_logger().setLevel(logging.WARNING)
        try:
            timestamps = []
            saved_timestamp = extract_music(segment_wrapper(
                media, segment_length_thres=args.max_segment_length),
                segment_connect=args.seg_connect)
            extract_mah_stuff(
                media, segmented_stamps=saved_timestamp,
                outdir=args.outdir, rev=False,
                timestamps=timestamps,
                soundonly=(args.soundonly != ''))
            saved_timestamp = None
        except TimestampMismatch:
            raise
    else:
        logging.warning((
            'segmentation', media, 'stopped to prevent posssible duplication'))
    logging.info(['segmentation', media, 'successful'])
    if args.cleanup and os.path.isfile(media):
        os.remove(media)
    if args.shazam:
        async def myshazam():
            await shazaming(
                args.outdir, media, args.shazam_coverart,)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(myshazam())
        loop.close()
    import sys
    sys.exit(0)
