#!/usr/bin/env bash
# BettaFish 抖音 A 股上市公司分析视频监控 —— 阿里云 ECS 一键部署（Ubuntu 24.04 / 22.04）
#
# 需要 Ubuntu 24.04（自带 PostgreSQL 16，与本机版本一致，才能导入本机数据）
# 用法（root 身份）：
#   bash install.sh
# 可选：事先把本机的 .env 上传为 /root/bettafish.env，把数据备份上传为 /root/bettafish.dump，
#       脚本会自动使用（API 密钥、知识库和已跟踪视频的数据都会带过来）。
set -euo pipefail

APP=/opt/BettaFishApp
REPO=https://github.com/cg44864317-Jacky/BettaFish.git
BRANCH=feature/stock-video-monitor
ENV_UPLOAD=/root/bettafish.env
DUMP_UPLOAD=/root/bettafish.dump
RUN_AT_HOUR=${RUN_AT_HOUR:-5}
RUN_AT_MIN=${RUN_AT_MIN:-0}

say()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
ok()   { echo -e "\033[32m  ✓ $*\033[0m"; }
warn() { echo -e "\033[33m  ! $*\033[0m"; }

[ "$(id -u)" = 0 ] || { echo "请用 root 运行：sudo bash install.sh"; exit 1; }

say "1/8 时区设为北京时间"
timedatectl set-timezone Asia/Shanghai
ok "$(date)"

say "2/8 安装系统软件（git、Python、PostgreSQL、ffmpeg）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git python3 python3-venv python3-dev build-essential \
    postgresql postgresql-contrib ffmpeg libgl1 libglib2.0-0 cron curl
systemctl enable --now postgresql cron
ok "$(python3 --version)，$(psql --version)"

# 内存小于 4GB 时加 4GB 交换空间，避免图片文字识别时内存不足
if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 3800 ] && ! swapon --show | grep -q swapfile; then
    say "内存较小，添加 4GB 交换空间"
    fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "交换空间已启用"
fi

say "3/8 下载代码"
if [ -d "$APP/.git" ]; then
    git -C "$APP" pull --ff-only origin "$BRANCH"
else
    git clone --depth 50 -b "$BRANCH" "$REPO" "$APP"
fi
ok "代码在 $APP"

say "4/8 安装 Python 依赖（只装监控模块需要的）"
python3 -m venv "$APP/.venv"
PIP="$APP/.venv/bin/pip"
MIRROR=""
for m in https://pypi.org/simple https://mirrors.aliyun.com/pypi/simple; do
    if curl -s -m 10 -o /dev/null "$m/six/"; then MIRROR=$m; break; fi
done
[ -n "$MIRROR" ] || { echo "无法连接 pip 下载源"; exit 1; }
ok "pip 下载源：$MIRROR"
"$PIP" install -q -U pip -i "$MIRROR"
"$PIP" install -q -i "$MIRROR" --timeout 60 \
    "sqlalchemy>=2" "psycopg[binary]" pymysql pydantic pydantic-settings python-dotenv loguru httpx \
    openai openpyxl akshare rapidocr_onnxruntime
ok "依赖安装完成"

say "5/8 创建数据库"
DB_PASS_FILE=/root/.bettafish_db_password
[ -f "$DB_PASS_FILE" ] || (umask 077; head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' > "$DB_PASS_FILE")
DB_PASS=$(cat "$DB_PASS_FILE")
sudo -u postgres psql -qtAc "SELECT 1 FROM pg_roles WHERE rolname='bettafish'" | grep -q 1 \
    && sudo -u postgres psql -qc "ALTER USER bettafish WITH PASSWORD '$DB_PASS';" \
    || sudo -u postgres psql -qc "CREATE USER bettafish WITH PASSWORD '$DB_PASS';"
sudo -u postgres psql -qtAc "SELECT 1 FROM pg_database WHERE datname='bettafish'" | grep -q 1 \
    || sudo -u postgres psql -qc "CREATE DATABASE bettafish OWNER bettafish ENCODING 'UTF8' TEMPLATE template0;"
ok "数据库 bettafish（只允许本机访问，不对外开放端口）"

say "6/8 配置 .env"
ENV="$APP/.env"
if [ -f "$ENV_UPLOAD" ]; then
    cp "$ENV_UPLOAD" "$ENV"; ok "使用上传的 $ENV_UPLOAD"
elif [ ! -f "$ENV" ]; then
    cp "$APP/.env.example" "$ENV"; warn "没有找到上传的 .env，已用模板创建，稍后需要填写 API 密钥"
fi
sed -i 's/\r$//' "$ENV"
setenv() {  # 设置或追加 KEY=VALUE
    if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}
setenv DB_DIALECT postgresql
setenv DB_HOST 127.0.0.1
setenv DB_PORT 5432
setenv DB_USER bettafish
setenv DB_PASSWORD "$DB_PASS"
setenv DB_NAME bettafish
setenv STOCK_VIDEO_DISCOVERY_SOURCE tikhub_only   # 云服务器不能扫码登录抖音，只用 TikHub
setenv STOCK_VIDEO_OCR_BACKEND rapidocr
setenv STOCK_VIDEO_FINISH_BY "${FINISH_BY:-08:50}"  # 服务器上没有 7 点的冲突，开盘前完成即可
chmod 600 "$ENV"
ok ".env 已配置（数据库、只用 TikHub、图片文字识别）"

say "7/8 导入数据 / 建表"
cd "$APP/MindSpider"
PY="$APP/.venv/bin/python"
if [ -f "$DUMP_UPLOAD" ]; then
    PGPASSWORD="$DB_PASS" pg_restore -h 127.0.0.1 -U bettafish -d bettafish \
        --no-owner --no-tablespaces --no-privileges --clean --if-exists "$DUMP_UPLOAD" \
        || warn "导入时有提示信息（通常可忽略），请看上面的输出"
    ok "已导入本机数据（知识库、已跟踪视频、每日数据、预警）"
fi
"$PY" -m StockVideoMonitor.main init-db
if ! PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U bettafish -d bettafish -tAc \
        "SELECT count(*) FROM a_share_company" 2>/dev/null | grep -qv '^0$'; then
    warn "知识库为空：需要运行 build-kb（约 1~2 小时，会调用大模型）。命令见部署说明。"
fi

say "8/8 设置每天 $(printf '%02d:%02d' "$RUN_AT_HOUR" "$RUN_AT_MIN") 自动运行"
chmod +x "$APP/scripts/aliyun/run_daily.sh"
cat > /etc/cron.d/bettafish <<EOF
# BettaFish 抖音上市公司分析视频监控：每天北京时间 $(printf '%02d:%02d' "$RUN_AT_HOUR" "$RUN_AT_MIN")
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$RUN_AT_MIN $RUN_AT_HOUR * * * root $APP/scripts/aliyun/run_daily.sh
EOF
chmod 644 /etc/cron.d/bettafish
systemctl restart cron
ok "定时任务已写入 /etc/cron.d/bettafish"

say "部署完成"
echo "  · 自检：     cd $APP/MindSpider && $PY -m StockVideoMonitor.main selftest"
echo "  · 立即运行： $APP/scripts/aliyun/run_daily.sh"
echo "  · 看日志：   tail -f $APP/logs/stock_video_\$(date +%Y%m%d).log"
echo "  · 跟踪表：   $APP/reports/"
