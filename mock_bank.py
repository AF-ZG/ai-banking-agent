"""AI Banking Agent — 模拟银行服务（FastAPI）。

运行方式：
    python mock_bank.py
启动后监听 http://127.0.0.1:8001 ，对外提供账户、转账、账单、理财、订阅代扣等 HTTP 接口。

本服务是 mock_data 目录下所有 JSON 文件的唯一读写入口；Agent 侧（src/mcp_tools/server.py）
通过 requests 调用这里的接口，不再直接操作文件。
"""
from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================================
# 数据读写层：mock_data 下 JSON 文件的唯一出入口
# ============================================================
_BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _BASE_DIR / "mock_data"
_lock = threading.Lock()

_FILES = {
    "account": "account.json",
    "transactions": "transactions.json",
    "transfer_records": "transfer_records.json",
    "wealth_products": "wealth_products.json",
    "wealth_holdings": "wealth_holdings.json",
    "subscriptions": "subscriptions.json",
    "subscription_history": "subscription_history.json",
    "scheduled_transfers": "scheduled_transfers.json",
}


def load(name: str) -> Any:
    path = _DATA_DIR / _FILES[name]
    with _lock, open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(name: str, data: Any) -> None:
    path = _DATA_DIR / _FILES[name]
    with _lock, open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def today_str() -> str:
    return date.today().isoformat()


def now_time_str() -> str:
    return datetime.now().strftime("%H:%M")


def gen_id(prefix: str, existing: list[dict]) -> str:
    """生成形如 TR20260925003 的业务编号。"""
    seq = len(existing) + 1
    return f"{prefix}{date.today().strftime('%Y%m%d')}{seq:03d}"


def data_date_range() -> tuple[str, str]:
    """账单流水实际覆盖的日期范围（最早 ~ 最晚），无数据时返回空串。"""
    txns = load("transactions")
    if not txns:
        return "", ""
    dates = sorted(t["date"] for t in txns)
    return dates[0], dates[-1]


# ============================================================
# 内部转账逻辑：校验余额并扣款（同步写账单流水）
# ============================================================
def _do_transfer(payee: str, amount: float, remark: str) -> dict:
    account = load("account")
    if amount <= 0:
        return {"success": False, "message": "转账金额必须大于0"}
    if account["balance"] < amount:
        return {"success": False, "message": f"余额不足，当前余额 {account['balance']:.2f} 元"}
    account["balance"] = round(account["balance"] - amount, 2)
    save("account", account)
    # 同步写入账单流水
    txns = load("transactions")
    txns.append({
        "id": gen_id("TXN", txns),
        "date": today_str(),
        "time": now_time_str(),
        "type": "支出",
        "category": "转账",
        "amount": amount,
        "counterparty": payee,
        "channel": "网银转账",
    })
    save("transactions", txns)
    return {"success": True, "balance_after": account["balance"]}


def _settle_due_scheduled_transfers() -> None:
    """启动时结算：到期的定时预约转账自动执行（保持逻辑闭环）。"""
    scheduled = load("scheduled_transfers")
    if not scheduled:
        return
    today = today_str()
    remaining = []
    for item in scheduled:
        if item["status"] == "待执行" and item["scheduled_date"] <= today:
            result = _do_transfer(item["payee"], item["amount"], item.get("remark", ""))
            if result["success"]:
                records = load("transfer_records")
                records.append({
                    "id": gen_id("TR", records),
                    "date": today,
                    "time": now_time_str(),
                    "payee": item["payee"],
                    "payee_account": item.get("payee_account", "未填写"),
                    "amount": item["amount"],
                    "remark": item.get("remark", ""),
                    "type": "定时预约转账",
                    "status": "成功",
                })
                save("transfer_records", records)
                item["status"] = "已执行"
            else:
                item["status"] = "执行失败"
        if item["status"] == "待执行":
            remaining.append(item)
    save("scheduled_transfers", remaining)


# ============================================================
# FastAPI 应用
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    _settle_due_scheduled_transfers()
    yield


app = FastAPI(title="AI Banking Agent — Mock Bank", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 请求体模型 ----------
class TransferRequest(BaseModel):
    payee: str
    amount: float
    remark: str = ""
    scheduled_date: str = ""


class CompareRequest(BaseModel):
    product_ids: list[str]


class SubscribeRequest(BaseModel):
    product_id: str
    amount: float


class RedeemRequest(BaseModel):
    holding_id: str
    amount: float


class CancelSubRequest(BaseModel):
    subscription_id: str


# ============================================================
# 场景一：智能转账
# ============================================================
@app.get("/api/balance")
def api_balance():
    """查询当前账户余额和账户基本信息。"""
    account = load("account")
    return {
        "holder_name": account["holder_name"],
        "card_number": account["card_number"],
        "balance": account["balance"],
        "currency": account["currency"],
        "status": account["status"],
    }


@app.get("/api/transfer/records")
def api_transfer_records(limit: int = Query(10, ge=1)):
    """查询历史转账记录，按日期倒序。"""
    records = load("transfer_records")
    records = sorted(records, key=lambda r: (r["date"], r.get("time", "")), reverse=True)
    return {"total": len(records), "records": records[:limit]}


@app.get("/api/transfer/today-total")
def api_transfer_today_total():
    """查询今日转账累计金额（用于风控限额判断）。"""
    today = today_str()
    records = load("transfer_records")
    total = sum(r["amount"] for r in records if r["date"] == today and r["status"] == "成功")
    return {"date": today, "today_transfer_total": round(total, 2)}


@app.post("/api/transfer")
def api_transfer(req: TransferRequest):
    """转账：scheduled_date 留空为即时转账，填日期为定时预约转账。"""
    payee, amount, remark, scheduled_date = req.payee, req.amount, req.remark, req.scheduled_date

    if scheduled_date:
        if scheduled_date < today_str():
            return {"success": False, "message": "预约日期不能早于今天"}
        scheduled = load("scheduled_transfers")
        item = {
            "id": gen_id("SCH", scheduled),
            "payee": payee,
            "amount": amount,
            "remark": remark,
            "scheduled_date": scheduled_date,
            "status": "待执行",
            "created_at": f"{today_str()} {now_time_str()}",
        }
        scheduled.append(item)
        save("scheduled_transfers", scheduled)
        return {
            "success": True,
            "type": "定时预约转账",
            "schedule_id": item["id"],
            "payee": payee,
            "amount": amount,
            "scheduled_date": scheduled_date,
            "message": f"已预约 {scheduled_date} 向 {payee} 转账 {amount:.2f} 元",
        }

    result = _do_transfer(payee, amount, remark)
    if not result["success"]:
        return result
    records = load("transfer_records")
    record = {
        "id": gen_id("TR", records),
        "date": today_str(),
        "time": now_time_str(),
        "payee": payee,
        "payee_account": "未填写",
        "amount": amount,
        "remark": remark,
        "type": "即时转账",
        "status": "成功",
    }
    records.append(record)
    save("transfer_records", records)
    return {
        "success": True,
        "type": "即时转账",
        "transfer_id": record["id"],
        "payee": payee,
        "amount": amount,
        "remark": remark,
        "balance_after": result["balance_after"],
        "message": f"已向 {payee} 转账 {amount:.2f} 元，余额 {result['balance_after']:.2f} 元",
    }


# ============================================================
# 场景二：账单分析
# ============================================================
@app.get("/api/bill/flow")
def api_bill_flow(start_date: str = "", end_date: str = "", limit: int = Query(50, ge=1)):
    """查询账单流水，支持按日期区间筛选。日期留空表示不限制。"""
    lo, hi = data_date_range()
    txns = load("transactions")
    if start_date:
        txns = [t for t in txns if t["date"] >= start_date]
    if end_date:
        txns = [t for t in txns if t["date"] <= end_date]
    txns = sorted(txns, key=lambda t: (t["date"], t.get("time", "")), reverse=True)
    result: dict = {
        "total": len(txns),
        "transactions": txns[:limit],
        "data_date_range": {"start": lo, "end": hi},
    }
    if (start_date or end_date) and not txns:
        result["notice"] = f"{start_date or '最早'} ~ {end_date or '最新'} 区间内暂无账单数据，当前账单数据覆盖范围为 {lo} ~ {hi}"
    return result


@app.get("/api/bill/stats")
def api_consumption_stats(start_date: str = "", end_date: str = ""):
    """自定义周期消费分类统计。

    start_date / end_date 均可留空；任一留空时默认统计【本月1日~今天】。
    """
    lo, hi = data_date_range()
    is_default = False
    if not start_date or not end_date:
        today = today_str()
        start_date = date.today().replace(day=1).isoformat()
        end_date = today
        is_default = True

    txns = load("transactions")
    txns = [t for t in txns if start_date <= t["date"] <= end_date]
    expense_by_cat: dict[str, float] = {}
    income_total = 0.0
    expense_total = 0.0
    for t in txns:
        if t["type"] == "支出":
            expense_by_cat[t["category"]] = round(expense_by_cat.get(t["category"], 0) + t["amount"], 2)
            expense_total += t["amount"]
        else:
            income_total += t["amount"]
    result: dict = {
        "period": f"{start_date} ~ {end_date}",
        "is_default_current_month": is_default,
        "income_total": round(income_total, 2),
        "expense_total": round(expense_total, 2),
        "expense_by_category": dict(sorted(expense_by_cat.items(), key=lambda x: x[1], reverse=True)),
        "data_date_range": {"start": lo, "end": hi},
    }
    if not txns:
        result["success"] = False
        result["message"] = (
            f"{start_date} ~ {end_date} 暂无账单数据（并非开支为0，而是该时段没有账单记录）；"
            f"当前账单数据覆盖范围为 {lo} ~ {hi}"
        )
    return result


@app.get("/api/bill/report")
def api_bill_report(period: str = ""):
    """生成月度/年度账单报告。period 形如 2026-09 或 2026；留空默认本月。"""
    lo, hi = data_date_range()
    is_default = False
    if not period:
        period = date.today().strftime("%Y-%m")
        is_default = True
    txns = load("transactions")
    if len(period) == 4:
        txns = [t for t in txns if t["date"].startswith(period)]
        period_label = f"{period}年"
    else:
        txns = [t for t in txns if t["date"].startswith(period)]
        period_label = f"{period}月"
    if not txns:
        return {
            "success": False,
            "message": f"{period_label}暂无账单数据（并非收支为0，而是该时段没有账单记录）；当前账单数据覆盖范围为 {lo} ~ {hi}",
            "data_date_range": {"start": lo, "end": hi},
        }
    income = sum(t["amount"] for t in txns if t["type"] == "收入")
    expense = sum(t["amount"] for t in txns if t["type"] == "支出")
    by_cat: dict[str, float] = {}
    for t in txns:
        if t["type"] == "支出":
            by_cat[t["category"]] = round(by_cat.get(t["category"], 0) + t["amount"], 2)
    top_expense = max(txns, key=lambda t: t["amount"] if t["type"] == "支出" else 0)
    return {
        "period": period_label,
        "is_default_current_month": is_default,
        "transaction_count": len(txns),
        "income_total": round(income, 2),
        "expense_total": round(expense, 2),
        "net": round(income - expense, 2),
        "expense_by_category": dict(sorted(by_cat.items(), key=lambda x: x[1], reverse=True)),
        "largest_expense": top_expense,
        "data_date_range": {"start": lo, "end": hi},
    }


@app.get("/api/bill/alerts")
def api_abnormal_alerts():
    """异常交易风险告警（仅提示，不对账户做任何操作）。"""
    txns = load("transactions")
    alerts = []
    for t in txns:
        reasons = []
        if t["type"] == "支出" and t["amount"] > 5000:
            reasons.append(f"单笔大额支出 {t['amount']:.2f} 元")
        hour = int(t.get("time", "12:00").split(":")[0])
        if hour >= 23 or hour < 5:
            reasons.append(f"深夜时段交易（{t.get('time', '')}）")
        if reasons:
            alerts.append({
                "transaction_id": t["id"],
                "date": t["date"],
                "time": t.get("time", ""),
                "counterparty": t["counterparty"],
                "amount": t["amount"],
                "risk_reasons": reasons,
            })
    alerts.sort(key=lambda a: a["date"], reverse=True)
    return {
        "alert_count": len(alerts),
        "alerts": alerts,
        "notice": "以上交易存在异常特征，请注意核实。系统仅做提示，未对账户做任何操作。",
    }


# ============================================================
# 场景三：理财操作
# ============================================================
@app.get("/api/wealth/holdings")
def api_wealth_holdings():
    """查询当前理财持仓及收益情况。"""
    holdings = load("wealth_holdings")
    total_invest = sum(h["invest_amount"] for h in holdings)
    total_value = sum(h["current_value"] for h in holdings)
    total_profit = sum(h["total_profit"] for h in holdings)
    return {
        "holding_count": len(holdings),
        "total_invest": round(total_invest, 2),
        "total_current_value": round(total_value, 2),
        "total_profit": round(total_profit, 2),
        "holdings": holdings,
    }


@app.get("/api/wealth/products")
def api_wealth_products():
    """查询全部在售理财产品信息。"""
    products = load("wealth_products")
    return {"product_count": len(products), "products": products}


@app.post("/api/wealth/compare")
def api_wealth_compare(req: CompareRequest):
    """对比多个理财产品（收益率、风险、起购金额、锁定期等）。"""
    product_ids = req.product_ids
    products = load("wealth_products")
    selected = [p for p in products if p["product_id"] in product_ids]
    missing = [pid for pid in product_ids if pid not in {p["product_id"] for p in products}]
    result = {
        "compare_count": len(selected),
        "products": [
            {
                "product_id": p["product_id"],
                "name": p["name"],
                "type": p["type"],
                "risk_level": p["risk_level"],
                "expected_annual_yield": p["expected_annual_yield"],
                "min_amount": p["min_amount"],
                "lock_period_days": p["lock_period_days"],
            }
            for p in selected
        ],
    }
    if missing:
        result["warning"] = f"未找到产品：{missing}"
    return result


@app.post("/api/wealth/subscribe")
def api_wealth_subscribe(req: SubscribeRequest):
    """理财申购。"""
    product_id, amount = req.product_id, req.amount
    products = load("wealth_products")
    product = next((p for p in products if p["product_id"] == product_id), None)
    if not product:
        return {"success": False, "message": f"未找到产品 {product_id}"}
    if amount < product["min_amount"]:
        return {"success": False, "message": f"低于起购金额 {product['min_amount']} 元"}
    account = load("account")
    if account["balance"] < amount:
        return {"success": False, "message": f"余额不足，当前余额 {account['balance']:.2f} 元"}

    account["balance"] = round(account["balance"] - amount, 2)
    save("account", account)

    holdings = load("wealth_holdings")
    existing = next((h for h in holdings if h["product_id"] == product_id and h["status"] == "持有中"), None)
    if existing:
        existing["invest_amount"] = round(existing["invest_amount"] + amount, 2)
        existing["current_value"] = round(existing["current_value"] + amount, 2)
        holding_id = existing["holding_id"]
    else:
        holding_id = gen_id("HOLD", holdings)
        holdings.append({
            "holding_id": holding_id,
            "product_id": product_id,
            "product_name": product["name"],
            "invest_amount": amount,
            "current_value": amount,
            "total_profit": 0.0,
            "purchase_date": today_str(),
            "maturity_date": "无固定期限" if product["lock_period_days"] == 0 else "到期日待计算",
            "status": "持有中",
        })
    save("wealth_holdings", holdings)
    return {
        "success": True,
        "holding_id": holding_id,
        "product_name": product["name"],
        "amount": amount,
        "balance_after": account["balance"],
        "message": f"已申购 {product['name']} {amount:.2f} 元，余额 {account['balance']:.2f} 元",
    }


@app.post("/api/wealth/redeem")
def api_wealth_redeem(req: RedeemRequest):
    """理财赎回。"""
    holding_id, amount = req.holding_id, req.amount
    holdings = load("wealth_holdings")
    holding = next((h for h in holdings if h["holding_id"] == holding_id), None)
    if not holding:
        return {"success": False, "message": f"未找到持仓 {holding_id}"}
    if holding["status"] != "持有中":
        return {"success": False, "message": "该持仓当前不可赎回"}
    if amount <= 0 or amount > holding["current_value"]:
        return {"success": False, "message": f"赎回金额需在 0 ~ {holding['current_value']:.2f} 元之间"}

    account = load("account")
    account["balance"] = round(account["balance"] + amount, 2)
    save("account", account)

    holding["current_value"] = round(holding["current_value"] - amount, 2)
    holding["invest_amount"] = round(max(holding["invest_amount"] - amount, 0), 2)
    if holding["current_value"] <= 0:
        holding["status"] = "已赎回"
    save("wealth_holdings", holdings)
    return {
        "success": True,
        "holding_id": holding_id,
        "product_name": holding["product_name"],
        "redeemed_amount": amount,
        "balance_after": account["balance"],
        "message": f"已赎回 {holding['product_name']} {amount:.2f} 元，余额 {account['balance']:.2f} 元",
    }


# ============================================================
# 场景四：订阅代扣管理
# ============================================================
@app.get("/api/subscriptions")
def api_subscriptions():
    """查询当前生效中的自动订阅代扣列表。"""
    subs = load("subscriptions")
    active = [s for s in subs if s["status"] == "生效中"]
    monthly_total = sum(s["amount_per_cycle"] for s in active if s["cycle"] == "每月")
    return {
        "active_count": len(active),
        "monthly_total": round(monthly_total, 2),
        "subscriptions": active,
    }


@app.get("/api/subscriptions/history")
def api_subscription_history(limit: int = Query(20, ge=1)):
    """查询历史代扣记录。"""
    history = load("subscription_history")
    history = sorted(history, key=lambda h: h["date"], reverse=True)
    return {"total": len(history), "records": history[:limit]}


@app.post("/api/subscriptions/cancel")
def api_cancel_subscription(req: CancelSubRequest):
    """取消自动订阅代扣。"""
    subscription_id = req.subscription_id
    subs = load("subscriptions")
    sub = next((s for s in subs if s["subscription_id"] == subscription_id), None)
    if not sub:
        return {"success": False, "message": f"未找到订阅 {subscription_id}"}
    if sub["status"] != "生效中":
        return {"success": False, "message": "该订阅已取消，无需重复操作"}
    sub["status"] = "已取消"
    sub["cancel_date"] = today_str()
    save("subscriptions", subs)
    return {
        "success": True,
        "subscription_id": subscription_id,
        "service_name": sub["service_name"],
        "message": f"已取消「{sub['service_name']}」自动代扣，后续将不再扣款",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
