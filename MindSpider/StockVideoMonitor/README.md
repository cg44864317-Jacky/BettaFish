# 抖音 A 股上市公司分析视频监控

每天自动发现抖音上新发布的股票分析视频，识别其中**具体分析 A 股上市公司**的视频（公司可以通过名称、外号、代码、产品或技术被提及），对这些视频连续跟踪 14 天的点赞、评论、转发、收藏，并在出现异常时预警。

## 每日流程

```
三路召回 ─► 去重入库 ─► 粗筛（大模型看标题文案）─► 刷新详情 ─► 语音/画面解析 ─► 精判打标
   │                                                                              │
   │  关键词搜索（MediaCrawler，失败自动切 TikHub）                                  ▼
   │  财经博主池最新作品（TikHub）                                       上市公司分析视频
   │  人气榜股票名作为当日动态搜索词（AKShare）                            ├─ 进入 14 天跟踪
   │                                                                     └─ 作者加入博主池
   ▼
每日跟踪（TikHub 批量详情，50 条/次）─► 每日快照 ─► 视频级 + 公司级预警 ─► 通知
```

| 环节 | 数据源 | 说明 |
|---|---|---|
| 关键词搜索 | MediaCrawler（主）/ TikHub（备） | 只要最近一天发布的视频；`STOCK_VIDEO_DISCOVERY_SOURCE` 可改主数据源 |
| 博主池 | TikHub | MediaCrawler 的博主模式会抓全部历史作品，开销太大，不用 |
| 每日跟踪 | TikHub | `fetch_multi_video_v2`，每次 50 条、0.05 美元 |
| 公司知识库 | AKShare + 大模型 | 公司列表、主营介绍；大模型补充外号、产品、技术、品牌 |

## 快速开始

在 `MindSpider` 目录下执行：

```bash
pip install akshare                      # 构建知识库需要
python -m StockVideoMonitor.main init-db # 建表（MindSpider 的 --init-db 也会一起建）

# 1. 构建知识库（首次约 30-60 分钟，之后建议每周跑一次）
python -m StockVideoMonitor.main build-kb --enrich-limit 50   # 先试 50 家看效果
python -m StockVideoMonitor.main build-kb                      # 全量

# 2. 执行一次每日任务
python -m StockVideoMonitor.main run

# 3. 每天定时执行（推荐用 cron，以下二选一）
#    crontab:  30 7 * * *  cd /path/to/BettaFish/MindSpider && python -m StockVideoMonitor.main run >> logs/stock_video.log 2>&1
python -m StockVideoMonitor.main schedule --at 07:30

# 其他
python -m StockVideoMonitor.main status                 # 概况
python -m StockVideoMonitor.main add-creator <sec_uid>  # 手动加入博主（支持主页链接）
```

> MediaCrawler 第一次运行会弹出抖音扫码登录，登录状态会保存下来。

## 配置

所有配置写在项目根目录 `.env`，完整列表见 `settings.py`，常用项已加入 `.env.example`。

**必填**：`TIKHUB_API_KEY`、数据库配置（与 MindSpider 共用）、大模型（默认复用 `MINDSPIDER_*`）。

### 语音识别与画面文字识别

默认关闭（`none`），只用标题和文案判断。开启后识别率会明显提高，因为很多视频的标题很模糊，公司名只在语音或画面里出现。两者都需要系统安装 `ffmpeg`。

| 后端 | 语音 `STOCK_VIDEO_ASR_BACKEND` | 画面 `STOCK_VIDEO_OCR_BACKEND` |
|---|---|---|
| `command` | 调用你自己的命令行工具，`STOCK_VIDEO_ASR_COMMAND` 里用 `{input}` 代表音频路径，程序读取标准输出 | 同理，`{input}` 代表图片路径 |
| `openai` | OpenAI 兼容的 `/audio/transcriptions`（`STOCK_VIDEO_ASR_BASE_URL` / `_API_KEY` / `_MODEL`） | — |
| `funasr` | 本地 FunASR（`pip install funasr modelscope`，建议 GPU） | — |
| `rapidocr` / `paddleocr` | — | 本地 OCR（`pip install rapidocr_onnxruntime` 或 `paddleocr`） |

## 数据表

| 表 | 内容 |
|---|---|
| `a_share_company` | 知识库。`manual_locked=1` 的公司不会被自动更新覆盖，可人工维护外号、产品、技术 |
| `douyin_stock_video` | 所有股票相关视频。`stage`：`prefilter_rejected` 粗筛排除 / `analyzed` 已精判 / `failed` 出错；`is_company_analysis=1` 为上市公司分析视频；`tracking_status`：`active` / `expired` / `unavailable` |
| `douyin_stock_video_company` | 视频涉及的公司：识别依据 `basis`（name/code/product/technology）、观点 `stance`（bullish/bearish/neutral）、原文证据 |
| `douyin_stock_video_snapshot` | 每日互动快照 |
| `douyin_finance_creator` | 财经博主池（自动加入 + 手动加入） |
| `stock_video_alert` | 预警记录 |

`douyin_stock_video` 已加入 InsightEngine 的搜索范围，分析时可直接检索到这些视频（含语音文字和摘要）。

## 预警规则（按日）

| 级别 | 类型 | 条件（默认值） |
|---|---|---|
| 视频 | `first_day_burst` | 首次快照点赞 ≥ 20000 |
| 视频 | `spike` | 点赞/转发/收藏当日增量 ≥ 自身此前日均 × 3，且超过最小增量（点赞 2000、转发 300、收藏 500）|
| 视频 | `unavailable` | 连续 2 次查不到（删除、私密、仅部分可见）|
| 公司 | `video_surge` | 当日新增分析视频 ≥ 5 条，且 ≥ 过去 7 日日均 × 3 |
| 公司 | `interaction_surge` | 当日互动增量 ≥ 10000，且 ≥ 过去 7 日日均 × 3 |
| 公司 | `stance_shift` | 当日与过去 7 日相比，看空占比变化 ≥ 40%（两边各至少 3 条）|

阈值都在 `.env` 里可调，建议跑一两周后根据实际数据调整。通知目前只写日志，其他渠道在 `notifiers.py` 中继承 `Notifier` 实现即可。

## 费用估算（TikHub）

- 每日跟踪：同时跟踪 N 条视频，每天约 `N / 50 × 0.05` 美元（1400 条约 1.4 美元/天）
- 解析前刷新详情：每天新标记的视频数 / 50 × 0.05 美元
- 博主池：每个博主每天约 1 次调用；TikHub 搜索（作为备用时）每次 0.01 美元

## 注意事项

- MediaCrawler 使用「非商业学习许可证」，仅限学习研究，禁止大规模抓取和商业用途；商业使用请联系作者授权，或把 `STOCK_VIDEO_DISCOVERY_SOURCE` 改为 `tikhub`。
- 抖音的视频播放地址几小时就会过期，所以解析前会用 TikHub 刷新一次。
- 测试：`pytest tests/test_stock_video_monitor.py`（SQLite + 模拟数据源，不访问网络）。
