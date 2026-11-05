# -*- coding: utf-8 -*-
"""
文字识别子进程：python -m StockVideoMonitor.ocr_worker <backend> <图片1> <图片2> ...

在独立进程里运行，主程序用超时控制；识别库偶尔会卡住，卡住时整个子进程被结束，不会拖住每日任务。
标准输出为 JSON 数组（识别出的文字行）。
"""

from __future__ import annotations

import json
import sys


def read_rapidocr(paths):
    from rapidocr_onnxruntime import RapidOCR  # type: ignore

    engine = RapidOCR()
    lines = []
    for p in paths:
        result, _ = engine(p)
        lines.extend(item[1] for item in result or [])
    return lines


def read_paddleocr(paths):
    from paddleocr import PaddleOCR  # type: ignore

    engine = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
    lines = []
    for p in paths:
        for page in engine.ocr(p) or []:
            lines.extend(item[1][0] for item in page or [])
    return lines


def main(argv) -> int:
    if len(argv) < 2:
        print("[]")
        return 0
    backend, paths = argv[0], argv[1:]
    readers = {"rapidocr": read_rapidocr, "paddleocr": read_paddleocr}
    lines = readers[backend](paths)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(json.dumps(lines, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
