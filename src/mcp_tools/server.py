"""AI Banking Agent — MCP 工具服务器（stdio 传输）。

本层只做协议适配：把 ReAct Agent 的 MCP 工具调用翻译成对模拟银行服务
（http://localhost:8001，见 mock_bank.py）的 HTTP 请求。
所有数据读写已下沉到 mock_bank.py，本文件不再直接操作 mock_data 下的 JSON 文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许以脚本方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from mcp.server.mcpserver import MCPServer

# 模拟银行服务地址
BANK_BASE = "http://localhost:8001"
_HTTP_TIMEOUT = 10  # 秒


# ============================================================
# HTTP 调用封装：统一处理网络异常，避免 MCP 链路被连接错误打断
# ============================================================
def _get(path: str, **params) -> dict:
    try:
        resp = requests.get(f"{BANK_BASE}{path}", params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"success": False, "message": f"银行服务不可用：{e}"}


def _post(path: str, body: dict) -> dict:
    try:
        resp = requests.post(f"{BANK_BASE}{path}", json=body, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"success": False, "message": f"银行服务不可用：{e}"}


mcp = MCPServer("ai-banking-agent")


# ============================================================
# 场景一：智能转账
# ============================================================
@mcp.tool()
def query_balance() -> dict:
    """【查询-绿色】查询当前账户余额和账户基本信息。"""
    return _get("/api/balance")


@mcp.tool()
def query_transfer_records(limit: int = 10) -> dict:
    """【查询-绿色】查询历史转账记录，按日期倒序。

    Args:
        limit: 返回条数上限，默认10
    """
    return _get("/api/transfer/records", limit=limit)


@mcp.tool()
def query_today_transfer_total() -> dict:
    """【查询-绿色】查询今日转账累计金额（用于风控限额判断）。"""
    return _get("/api/transfer/today-total")


@mcp.tool()
def transfer(payee: str, amount: float, remark: str = "", scheduled_date: str = "") -> dict:
    """【操作-黄/红色权限】转账。金额较小时为黄色权限；日累计超过1000元或大额时为红色权限。

    Args:
        payee: 收款人姓名
        amount: 转账金额（元）
        remark: 转账备注，可选
        scheduled_date: 预约执行日期（格式YYYY-MM-DD），留空则为即时转账
    """
    return _post("/api/transfer", {
        "payee": payee,
        "amount": amount,
        "remark": remark,
        "scheduled_date": scheduled_date,
    })


# ============================================================
# 场景二：账单分析
# ============================================================
@mcp.tool()
def query_bill_flow(start_date: str = "", end_date: str = "", limit: int = 50) -> dict:
    """【查询-绿色】查询账单流水，支持按日期区间筛选。

    Args:
        start_date: 开始日期（YYYY-MM-DD），留空不限制
        end_date: 结束日期（YYYY-MM-DD），留空不限制
        limit: 返回条数上限，默认50
    """
    return _get("/api/bill/flow", start_date=start_date, end_date=end_date, limit=limit)


@mcp.tool()
def consumption_stats(start_date: str = "", end_date: str = "") -> dict:
    """【查询-绿色】自定义周期消费分类统计。用户未指定时间时两个参数都留空，系统默认统计本月（1日~今天），禁止自行编造日期。

    Args:
        start_date: 开始日期（YYYY-MM-DD），留空则默认本月1日
        end_date: 结束日期（YYYY-MM-DD），留空则默认今天
    """
    return _get("/api/bill/stats", start_date=start_date, end_date=end_date)


@mcp.tool()
def bill_report(period: str = "") -> dict:
    """【查询-绿色】生成月度/年度账单报告。用户未指定时间时 period 留空，系统默认本月，禁止自行编造月份。

    Args:
        period: 月度格式 YYYY-MM（如 2026-09）或年度格式 YYYY（如 2026）；留空默认本月
    """
    return _get("/api/bill/report", period=period)


@mcp.tool()
def abnormal_alerts() -> dict:
    """【查询-绿色】异常交易风险告警（仅提示，不对账户做任何操作）。

    规则：单笔超过5000元 或 深夜时段(23:00-05:00)交易。
    """
    return _get("/api/bill/alerts")


# ============================================================
# 场景三：理财操作
# ============================================================
@mcp.tool()
def query_wealth_holdings() -> dict:
    """【查询-绿色】查询当前理财持仓及收益情况。"""
    return _get("/api/wealth/holdings")


@mcp.tool()
def query_wealth_products() -> dict:
    """【查询-绿色】查询全部在售理财产品信息。"""
    return _get("/api/wealth/products")


@mcp.tool()
def compare_wealth_products(product_ids: list[str]) -> dict:
    """【查询-绿色】对比多个理财产品（收益率、风险、起购金额、锁定期等）。

    Args:
        product_ids: 产品ID列表，如 ["WP001","WP003"]
    """
    return _post("/api/wealth/compare", {"product_ids": product_ids})


@mcp.tool()
def wealth_subscribe(product_id: str, amount: float) -> dict:
    """【操作-红色权限】理财申购，需确认+验证码核验。

    Args:
        product_id: 理财产品ID（如 WP001）
        amount: 申购金额（元）
    """
    return _post("/api/wealth/subscribe", {"product_id": product_id, "amount": amount})


@mcp.tool()
def wealth_redeem(holding_id: str, amount: float) -> dict:
    """【操作-红色权限】理财赎回，需确认+验证码核验。

    Args:
        holding_id: 持仓ID（如 HOLD001）
        amount: 赎回金额（元）
    """
    return _post("/api/wealth/redeem", {"holding_id": holding_id, "amount": amount})


# ============================================================
# 场景四：订阅代扣管理
# ============================================================
@mcp.tool()
def query_subscriptions() -> dict:
    """【查询-绿色】查询当前生效中的自动订阅代扣列表。"""
    return _get("/api/subscriptions")


@mcp.tool()
def query_subscription_history(limit: int = 20) -> dict:
    """【查询-绿色】查询历史代扣记录。

    Args:
        limit: 返回条数上限，默认20
    """
    return _get("/api/subscriptions/history", limit=limit)


@mcp.tool()
def cancel_subscription(subscription_id: str) -> dict:
    """【操作-黄色权限】取消自动订阅代扣，需用户确认。

    Args:
        subscription_id: 订阅ID（如 SUB001）
    """
    return _post("/api/subscriptions/cancel", {"subscription_id": subscription_id})


if __name__ == "__main__":
    mcp.run()
