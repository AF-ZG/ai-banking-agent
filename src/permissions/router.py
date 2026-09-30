"""权限路由 + 模拟验证码模块。

- 将意图识别后的待执行操作路由到绿/黄/红权限链路；
- 生成/校验模拟验证码（红色权限多因子验证）。
"""
from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from typing import Any

from src.config import (
    CANCEL_KEYWORDS,
    CONFIRM_KEYWORDS,
    SMALL_TRANSFER_DAILY_LIMIT,
    VERIFY_CODE_LENGTH,
)


# ============================================================
# 权限路由
# ============================================================
@dataclass
class PermissionDecision:
    level: str          # "green" | "yellow" | "red"
    tool_name: str
    tool_params: dict[str, Any]
    summary: str        # 给用户展示的操作摘要
    verify_code: str = ""
    code_attempts: int = 0
    confirmed: bool = False
    cancelled: bool = False
    completed: bool = False
    result: Any = None
    amount_warning: bool = False       # 金额来源于模糊输入，确认卡需加粗二次核对
    amount_warn_source: str = ""       # 触发来源：model_flag / 检测原因


GREEN_TOOLS = {
    "query_balance", "query_transfer_records", "query_today_transfer_total",
    "query_bill_flow", "consumption_stats", "bill_report", "abnormal_alerts",
    "query_wealth_holdings", "query_wealth_products", "compare_wealth_products",
    "query_subscriptions", "query_subscription_history",
}

YELLOW_TOOLS = {
    "transfer",           # 小额转账（含定时）
    "cancel_subscription",
}

RED_TOOLS = {
    "transfer",           # 大额 / 日累计超限
    "wealth_subscribe",
    "wealth_redeem",
}


def classify(tool_name: str, params: dict, today_total: float = 0.0) -> PermissionDecision:
    """基于工具名与参数判定权限等级并生成操作摘要。

    Args:
        tool_name: MCP 工具名称
        params: 工具调用参数
        today_total: 今日已转账累计金额（元），用于风控判断
    """
    if tool_name in GREEN_TOOLS:
        return PermissionDecision(level="green", tool_name=tool_name, tool_params=params,
                                   summary=_green_summary(tool_name, params))

    # transfer 需要额外判定
    if tool_name == "transfer":
        amount = params.get("amount", 0)
        scheduled_date = params.get("scheduled_date", "")
        # 大额（>1000 当日累计超过限额）或红色工具已列出的场景
        if amount > SMALL_TRANSFER_DAILY_LIMIT or (today_total + amount) > SMALL_TRANSFER_DAILY_LIMIT:
            return PermissionDecision(
                level="red", tool_name=tool_name, tool_params=params,
                summary=_transfer_summary(params, level="red"),
            )
        return PermissionDecision(
            level="yellow", tool_name=tool_name, tool_params=params,
            summary=_transfer_summary(params, level="yellow"),
        )

    if tool_name in YELLOW_TOOLS:
        return PermissionDecision(level="yellow", tool_name=tool_name, tool_params=params,
                                   summary=_yellow_summary(tool_name, params))

    if tool_name in RED_TOOLS:
        return PermissionDecision(level="red", tool_name=tool_name, tool_params=params,
                                   summary=_red_summary(tool_name, params))

    # 兜底：未知工具默认红色
    return PermissionDecision(level="red", tool_name=tool_name, tool_params=params,
                               summary=f"未知操作「{tool_name}」")


# ============================================================
# 用户意图识别辅助：检查取消 / 确认 / 验证码
# ============================================================
def is_cancel(text: str) -> bool:
    text = text.strip().lower()
    return any(k in text for k in CANCEL_KEYWORDS)


# 取消词后允许的纯语气尾巴（吧/啊/呀/哦/呢/嘛/哈/的/了）
_CANCEL_TRAILING = set("吧啊呀哦呢嘛哈的了")
_PUNCT_RE = re.compile(r"[\s，。！？、,.!?~～\-_:：;；]+")


def is_pure_cancel(text: str) -> bool:
    """判断是否为「纯取消指令」（如：取消 / 算了吧 / 不用了）。

    与 is_cancel 的区别：「取消爱奇艺那个」「把会员取消了」含业务宾语，
    表达的是新的操作意图（取消某项业务），不算纯取消，应放行给大模型处理。
    """
    compact = _PUNCT_RE.sub("", text.strip().lower())
    if not compact:
        return False
    for kw in CANCEL_KEYWORDS:
        if compact == kw:
            return True
        if compact.startswith(kw):
            tail = compact[len(kw):]
            if 0 < len(tail) <= 2 and all(ch in _CANCEL_TRAILING for ch in tail):
                return True
    return False


def is_confirm(text: str) -> bool:
    text = text.strip().lower()
    return any(k in text for k in CONFIRM_KEYWORDS)


def generate_code() -> str:
    """生成随机模拟验证码。"""
    return "".join(random.choices(string.digits, k=VERIFY_CODE_LENGTH))


def check_code(user_input: str, expected: str) -> bool:
    """验证码校验，允许纯数字连续输入或包含在句子中。"""
    digits = "".join(ch for ch in user_input if ch.isdigit())
    return digits == expected and len(digits) == VERIFY_CODE_LENGTH


# ============================================================
# 摘要模板
# ============================================================
def _green_summary(tool_name: str, params: dict) -> str:
    return f"查询操作：{tool_name}"


def _yellow_summary(tool_name: str, params: dict) -> str:
    if tool_name == "cancel_subscription":
        return f"取消代扣：{params.get('subscription_id', '')}"
    return f"操作：{tool_name}"


def _red_summary(tool_name: str, params: dict) -> str:
    if tool_name == "wealth_subscribe":
        return f"理财申购：{params.get('product_id', '')}，金额 {params.get('amount', 0):.2f} 元"
    if tool_name == "wealth_redeem":
        return f"理财赎回：{params.get('holding_id', '')}，金额 {params.get('amount', 0):.2f} 元"
    return f"高风险操作：{tool_name}"


def _transfer_summary(params: dict, level: str) -> str:
    amount = params.get("amount", 0)
    payee = params.get("payee", "未知")
    remark = params.get("remark", "")
    scheduled_date = params.get("scheduled_date", "")
    remark_str = f"，备注：{remark}" if remark else ""
    if scheduled_date:
        return f"定时预约转账：{scheduled_date} 向 {payee} 转账 {amount:.2f} 元{remark_str}（权限：{level}）"
    return f"即时转账：向 {payee} 转账 {amount:.2f} 元{remark_str}（权限：{level}）"


# ============================================================
# 模糊金额检测（防参数幻觉的代码兜底）
# ============================================================
# 拼音单位 / 非标准英文单位：10qian、1wan、5kuai、2yuan（后面可直接跟中文，故不用 \b）
_PINYIN_AMOUNT_RE = re.compile(r"\d[\d,\.]*\s*(qian|wan|kuai|yuan|jioa)(?![a-z])", re.IGNORECASE)
# 数字+块/毛 + 直接跟数字（非标准口语）：10块5、3毛6
_NONSTANDARD_CN_RE = re.compile(r"\d\s*(块|毛)\s*\d")
# 一个数里出现多个小数点：1.2.3
_MULTI_DOT_RE = re.compile(r"\d+\.\d+\.")
# 整句只有裸数字（含小数）、没有任何明确货币单位
_BARE_NUMBER_RE = re.compile(r"^[\s\d.,]+$")


def detect_ambiguous_amount(text: str) -> str:
    """检测用户输入中的金额表达是否模糊。

    返回空串表示安全/无金额；返回非空字符串为模糊原因（用于审计）。
    注意：纯中文数字表述（如"两百块"）不含阿拉伯数字，不在此正则层拦截，
    由系统提示词约束大模型处理。
    """
    if not text:
        return ""
    if _PINYIN_AMOUNT_RE.search(text):
        return "含拼音/非标准单位金额（如 10qian、1wan）"
    if _NONSTANDARD_CN_RE.search(text):
        return "含非标准金额单位（如 10块5）"
    if _MULTI_DOT_RE.search(text):
        return "金额数字格式非法（多个小数点）"
    if _BARE_NUMBER_RE.match(text.strip()) and re.search(r"\d", text):
        return "只有数字没有明确货币单位"
    return ""


# ============================================================
# 金额大写转换（用于二次核对警告，如 10000.00 → 人民币壹万元整）
# ============================================================
_CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_CN_GROUP_UNITS = ("", "拾", "佰", "仟")
_CN_BIG_UNITS = ("", "万", "亿", "万亿")


def _int_to_chinese(n: int) -> str:
    if n == 0:
        return "零"
    groups: list[int] = []
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    parts: list[str] = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            if parts and not parts[-1].endswith("零"):
                parts.append("零")
            continue
        if parts and (parts[-1].endswith("万") or parts[-1].endswith("亿")) and g < 1000:
            parts.append("零")
        seg = ""
        pending_zero = False
        for j, ch in enumerate(f"{g:04d}"):
            d = int(ch)
            if d == 0:
                pending_zero = True
            else:
                if pending_zero and seg:
                    seg += "零"
                pending_zero = False
                seg += _CN_DIGITS[d] + _CN_GROUP_UNITS[3 - j]
        parts.append(seg + _CN_BIG_UNITS[i])
    res = "".join(parts).rstrip("零")
    while "零零" in res:
        res = res.replace("零零", "零")
    return res


def amount_to_chinese(amount: float) -> str:
    """把金额转成人民币大写读法，如 10000.0 -> '人民币壹万元整'，100.5 -> '人民币壹佰元伍角'。"""
    amount = round(float(amount), 2)
    integer = int(amount)
    cents = round((amount - integer) * 100)
    text = "人民币" + _int_to_chinese(integer) + "元"
    if cents == 0:
        text += "整"
    else:
        jiao, fen = divmod(cents, 10)
        if jiao:
            text += _CN_DIGITS[jiao] + "角"
        if fen:
            text += _CN_DIGITS[fen] + "分"
    return text
