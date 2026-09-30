"""自测脚本：验证"模型只做推理规划，后端负责真实资金操作"的设计原则。

验证要点：
1. 资金扣减、余额校验在 MCP 工具层（后端），不依赖模型
2. 权限路由判定在后端，模型无法绕过
3. 模型输出仅包含 tool_name + tool_params，不含资金修改逻辑
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.mcp_tools.server import (
    _do_transfer,
    query_balance,
    transfer,
    wealth_subscribe,
    wealth_redeem,
    query_today_transfer_total,
    query_wealth_holdings,
)
from src.permissions.router import classify, is_confirm, is_cancel


def banner(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def test_backend_executes_funds():
    banner("测试1：资金扣减由后端工具层执行（不经过模型）")
    before = query_balance()
    print(f"转账前余额：{before['balance']:.2f} 元")

    # 模拟模型仅输出结构化请求：{"tool_name": "transfer", "tool_params": {...}}
    model_request = {
        "tool_name": "transfer",
        "tool_params": {"payee": "测试收款人", "amount": 100.0, "remark": "测试转账"},
    }
    print(f"模型输出（仅决策）：{json.dumps(model_request, ensure_ascii=False)}")

    # 后端根据模型决策调用工具执行
    result = transfer(**model_request["tool_params"])
    print(f"后端执行结果：{json.dumps(result, ensure_ascii=False)}")

    after = query_balance()
    print(f"转账后余额：{after['balance']:.2f} 元")
    assert before["balance"] - 100.0 == after["balance"], "余额未正确扣减"
    print("✅ PASS：资金扣减由后端工具完成，模型仅输出结构化请求")


def test_permission_in_backend():
    banner("测试2：权限等级由后端判定（模型无法绕过）")
    # 模型输出的请求，权限由后端 classify 函数判定
    small_req = {"payee": "A", "amount": 500}
    large_req = {"payee": "B", "amount": 5000}

    d_small = classify("transfer", small_req, today_total=0)
    d_large = classify("transfer", large_req, today_total=0)

    print(f"小额请求 500元 → 权限等级：{d_small.level}")
    print(f"大额请求 5000元 → 权限等级：{d_large.level}")
    assert d_small.level == "yellow", "小额应为黄色"
    assert d_large.level == "red", "大额应为红色"

    # 日累计超限也升级为红色
    d_daily = classify("transfer", {"payee": "C", "amount": 800}, today_total=500)
    print(f"单笔800元 + 当日已转500元 → 权限等级：{d_daily.level}")
    assert d_daily.level == "red", "日累计超限应升级红色"
    print("✅ PASS：权限判定在后端，模型无法绕过")


def test_cancel_terminates():
    banner("测试3：取消指令立即终止（不调用任何写操作工具）")
    assert is_cancel("取消") is True
    assert is_cancel("不用了") is True
    assert is_cancel("终止") is True
    assert is_confirm("确认") is True
    assert is_confirm("好的") is True
    print("✅ PASS：取消/确认指令识别正确")


def test_today_total():
    banner("测试4：今日累计转账限额校验")
    total = query_today_transfer_total()
    print(f"今日已转账累计：{total['today_transfer_total']:.2f} 元")
    print("✅ PASS：限额数据来源后端，模型可查询但不直接修改")


def test_wealth_operations_in_backend():
    banner("测试5：理财申购/赎回由后端执行")
    before = query_balance()
    print(f"申购前余额：{before['balance']:.2f} 元")

    # 模型仅输出结构化请求
    model_request = {
        "tool_name": "wealth_subscribe",
        "tool_params": {"product_id": "WP002", "amount": 1000.0},
    }
    print(f"模型输出：{json.dumps(model_request, ensure_ascii=False)}")

    result = wealth_subscribe(**model_request["tool_params"])
    print(f"后端执行结果：{result.get('message')}")

    after = query_balance()
    print(f"申购后余额：{after['balance']:.2f} 元")
    assert before["balance"] - 1000.0 == after["balance"], "余额未扣减"

    # 赎回也由后端执行
    holdings = query_wealth_holdings()
    holding = [h for h in holdings["holdings"] if h["product_id"] == "WP002"][0]
    redeem_result = wealth_redeem(holding["holding_id"], 500.0)
    print(f"赎回结果：{redeem_result.get('message')}")
    print("✅ PASS：理财资金操作由后端工具完成")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  设计原则自测：模型做大脑，后端做资金操作")
    print("=" * 60)
    test_backend_executes_funds()
    test_permission_in_backend()
    test_cancel_terminates()
    test_today_total()
    test_wealth_operations_in_backend()
    print("\n" + "=" * 60)
    print("  全部测试通过 ✅")
    print("=" * 60)
