"""硅基流动 API 客户端（OpenAI 兼容协议）。"""
from __future__ import annotations

from typing import Any

from openai import OpenAI

from src.config import SILICONFLOW_API_KEY, SILICONFLOW_BASE_URL, SILICONFLOW_MODEL


class SiliconFlowClient:
    """硅基流动大模型客户端，走 OpenAI 兼容接口。"""

    def __init__(self) -> None:
        self.client = OpenAI(
            api_key=SILICONFLOW_API_KEY,
            base_url=SILICONFLOW_BASE_URL,
        )
        self.model = SILICONFLOW_MODEL

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        """发送多轮对话，返回模型原始文本（期望为 JSON 字符串）。"""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""
