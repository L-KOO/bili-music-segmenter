#!/bin/bash
# 每 2 分钟探一次：验证码解除后立刻把积压目录传上去，不等 celery 的定时重试。
# 具体逻辑见 upload_resume.py。与 upload_watch/recover 共用同一把锁，不会同时动目录。
set -u
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
P=/vol2/1000/ipynb

exec 9>/tmp/upload_pipeline.lock
flock -n 9 || exit 0

docker exec -w /inaseg ipnby-inacelery-1 \
    python /inaseg/scripts/upload_resume.py 2>&1
