#!/usr/bin/env bash
# BettaFish 每日任务（由 /etc/cron.d/bettafish 每天调用，也可手动运行）
APP=/opt/BettaFishApp
mkdir -p "$APP/logs"
LOG="$APP/logs/stock_video_$(date +%Y%m%d).log"
export PYTHONUTF8=1 TZ=Asia/Shanghai

# 同一时间只运行一个；最多运行 4 小时（须长于程序自己的截止时间 STOCK_VIDEO_FINISH_BY，默认 08:50），防止卡住
exec 9>"$APP/.run.lock"
if ! flock -n 9; then
    echo "===== $(date '+%F %T') 上一次任务还在运行，本次跳过 =====" >> "$LOG"
    exit 0
fi

{
    echo "===== Daily run started $(date '+%F %T') ====="
    git -C "$APP" pull --ff-only origin feature/stock-video-monitor
    cd "$APP/MindSpider" || exit 1
    timeout 4h "$APP/.venv/bin/python" -m StockVideoMonitor.main run --scheduled
    rc=$?
    [ "$rc" = 124 ] && echo "!!!!! 超过 4 小时被强制结束（跟踪表可能未生成）"
    echo "===== Daily run finished $(date '+%F %T') (exit $rc) ====="
} >> "$LOG" 2>&1
