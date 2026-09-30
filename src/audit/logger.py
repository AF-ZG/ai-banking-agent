"""操作审计日志（JSON Lines 格式）。

标准审计文件 audit.jsonl：每条记录固定 5 要素——
    时间 / 用户原话 / 调用的工具 / 参数（账号打码） / 决策结果
详细事件流 audit_detail.jsonl：全链路诊断事件（意图、ReAct 步骤、确认状态等）。
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from typing import Any

from src.config import AUDIT_DETAIL_FILE, AUDIT_LOG_FILE, LOG_DIR

_lock = threading.Lock()

# 视为账号/卡号的参数字段名（不区分大小写）
_ACCOUNT_FIELDS = {
    "payee_account", "account", "account_id", "card_number", "card",
    "from_account", "to_account", "payer_account",
}
_DIGITS_RE = re.compile(r"\D")


def new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


# ============================================================
# 账号打码
# ============================================================
def mask_value(value: Any) -> str:
    """账号打码：8 位以上保留前 4 后 4，6-7 位保留前 2 后 2。"""
    digits = _DIGITS_RE.sub("", str(value))
    if len(digits) >= 8:
        return f"{digits[:4]} **** {digits[-4:]}"
    if len(digits) >= 6:
        return f"{digits[:2]}****{digits[-2:]}"
    return str(value)


def mask_sensitive(params: dict[str, Any] | None) -> dict[str, Any]:
    """递归打码参数中的账号/卡号字段；其余字段原样保留。"""
    if not params:
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, dict):
            out[k] = mask_sensitive(v)
        elif isinstance(v, list):
            out[k] = [mask_sensitive(x) if isinstance(x, dict) else x for x in v]
        elif k.lower() in _ACCOUNT_FIELDS:
            out[k] = mask_value(v)
        elif isinstance(v, str) and len(_DIGITS_RE.sub("", v)) >= 8 and re.fullmatch(r"[\d\* ]{8,}", v):
            out[k] = mask_value(v)  # 纯数字串兜底打码（如误传完整卡号）
        else:
            out[k] = v
    return out


# ============================================================
# 标准审计（5 要素）
# ============================================================
def log_turn(user_input: str, tool: str | None, params: dict[str, Any] | None, decision: str) -> None:
    """追加一条标准审计记录，每条固定包含 5 个信息：
    时间 / 用户原话 / 调用的工具 / 参数（账号已打码）/ 决策结果。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "user_input": user_input,
        "tool": tool,
        "params": mask_sensitive(params),
        "decision": decision,
    }
    with _lock:
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
# 详细事件流（诊断用）
# ============================================================
def log(event_type: str, data: dict[str, Any]) -> None:
    """追加一条详细诊断事件（写入 audit_detail.jsonl，不作为标准审计交付物）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event_type": event_type,
        **data,
    }
    with _lock:
        with open(AUDIT_DETAIL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
