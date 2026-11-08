#!/usr/bin/env bash
# BettaFish 每日任务（由 /etc/cron.d/bettafish 每天调用，也可手动运行）
APP=/opt/BettaFishApp
mkdir -p "$APP/logs"
LOG="$APP/logs/stock_video_$(date +%Y%m%d).log"
export PYTHONUTF8=1 TZ=Asia/Shanghai

# 同一时间只运行一个；最多运行 3 小时，防止卡住
exec 9>"$APP/.run.lock"
if ! flock -n 9; then
    echo "===== $(date '+%F %T') 上一次任务还在运行，本次跳过 =====" >> "$LOG"
    exit 0
fi

{
    echo "===== Daily run started $(date '+%F %T') ====="
    git -C "$APP" pull --ff-only origin feature/stock-video-monitor
    cd "$APP/MindSpider" || exit 1
    timeout 3h "$APP/.venv/bin/python" -m StockVideoMonitor.main run --scheduled
    echo "===== Daily run finished $(date '+%F %T') (exit $?) ====="
} >> "$LOG" 2>&1
