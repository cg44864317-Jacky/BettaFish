# -*- coding: utf-8 -*-
"""大模型调用：OpenAI 兼容接口，要求返回 JSON。"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional, Protocol

from loguru import logger

from .settings import MonitorSettings, get_settings


class JSONLLM(Protocol):
    def complete_json(self, system: str, user: str) -> Any: ...


def extract_json(text: str) -> Any:
    """从模型输出中取出 JSON（兼容 ```json 代码块、前后多余文字）。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise ValueError(f"无法解析模型输出为 JSON：{text[:200]}")


class OpenAICompatLLM:
    def __init__(self, settings: Optional[MonitorSettings] = None):
        from openai import OpenAI

        s = settings or get_settings()
        api_key = s.STOCK_VIDEO_LLM_API_KEY or s.MINDSPIDER_API_KEY
        if not api_key:
            raise RuntimeError("未配置大模型 API Key（STOCK_VIDEO_LLM_API_KEY 或 MINDSPIDER_API_KEY）")
        # 设置超时，避免个别请求无响应时把整个每日任务卡住
        self.client = OpenAI(api_key=api_key, base_url=s.STOCK_VIDEO_LLM_BASE_URL or s.MINDSPIDER_BASE_URL,
                             timeout=120, max_retries=1)
        self.model = s.STOCK_VIDEO_LLM_MODEL or s.MINDSPIDER_MODEL_NAME

    def complete_json(self, system: str, user: str, retries: int = 2) -> Any:
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.1,
                )
                return extract_json(resp.choices[0].message.content or "")
            except Exception as e:  # 网络错误或 JSON 解析失败都重试
                last = e
                logger.warning(f"[LLM] 第 {attempt + 1} 次调用失败：{e}")
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"大模型调用失败：{last}")
