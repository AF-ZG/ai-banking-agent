"""模糊输入 / 多轮对话编排测试（Mock LLM，无需真实 API Key）。

用预设的模型响应验证 ReAct 引擎对新提示词规则的支持是否到位：
1. 分步补充信息：第一句"给张三转账"（缺金额→追问），第二句"100块"（拼接→黄色确认卡）
2. 模糊指代取消会员：先 query_subscriptions 查候选 → 列候选追问"哪一个"
   → 用户选定后输出 cancel_subscription（黄色确认卡，权限不跳过）

运行方式（项目根目录）：
    python tests/test_fuzzy_dialogue.py

说明：
- 场景1不依赖银行服务（黄色操作在确认前不会真正扣款，今日累计查询失败时按0处理）。
- 场景2需要先启动模拟银行服务：python mock_bank.py
  （要真实验证"大模型能否听懂口语"，请按文末方式用 python main.py 手工测试）。
"""
import asyncio
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.react_agent import ReActAgent  # noqa: E402

BANK_URL = "http://localhost:8001"


class MockLLM:
    """按预设顺序返回响应的模拟大模型（模拟遵守新提示词后的行为）。"""

    def __init__(self, responses):
        self.responses = responses
        self.idx = 0

    def chat(self, messages, temperature=0.3):
        if self.idx < len(self.responses):
            resp = self.responses[self.idx]
            self.idx += 1
            return resp
        return json.dumps({"thought": "结束", "tool_name": None, "tool_params": {}, "response": "对话结束"})


def banner(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def bank_available() -> bool:
    try:
        requests.get(f"{BANK_URL}/api/balance", timeout=2)
        return True
    except requests.RequestException:
        return False


# ============================================================
# 场景1：分步补充信息（缺参追问 → 第二轮拼接出完整参数）
# ============================================================
async def test_stepwise_fill_amount() -> None:
    banner("场景1：「给张三转账」→ 追问金额 → 「100块」拼接执行")
    llm = MockLLM([
        # 第一轮：意图明确但缺金额，按规则 tool_name=null 只追问
        json.dumps({
            "thought": "用户要给张三转账，但没说金额，不能瞎猜，先追问",
            "tool_name": None,
            "tool_params": {},
            "response": "好的，请问您要给张三转多少钱？",
        }, ensure_ascii=False),
        # 第二轮：结合历史拼出完整参数，输出 transfer（系统接管黄色确认）
        json.dumps({
            "thought": "结合上一轮，用户要给张三转账100元",
            "tool_name": "transfer",
            "tool_params": {"payee": "张三", "amount": 100, "remark": ""},
            "response": "已收到您的转账请求",
        }, ensure_ascii=False),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    reply1 = await agent.chat("给张三转账")
    print(f"第一轮回复：{reply1}")
    assert "多少" in reply1 or "金额" in reply1, "缺金额时必须追问，不能瞎转"
    assert agent.pending is None, "追问阶段不应产生挂起操作"

    reply2 = await agent.chat("100块")
    print(f"第二轮回复：\n{reply2}")
    assert "【操作详情】" in reply2, "补齐参数后必须进入系统确认卡"
    assert "张三" in reply2 and "100.00" in reply2, "确认卡必须是拼接后的完整参数（张三/100元）"
    assert "黄色" in reply2, "100元小额转账必须走黄色权限，不得跳过"
    assert agent.pending is not None and agent.pending.tool_name == "transfer"

    # 追问阶段（pending 尚未产生时）说"算了"：取消未完成需求
    agent.pending = None
    reply_cancel = await agent.chat("算了")
    print(f"取消回复：{reply_cancel}")
    assert "取消" in reply_cancel

    await agent.stop()
    print("✅ PASS：碎片信息跨轮拼接正确，缺参先追问，权限流程未跳过")


# ============================================================
# 场景2：模糊指代（先查订阅候选 → 列候选追问 → 选定后黄色确认卡）
# ============================================================
async def test_ambiguous_subscription_reference() -> None:
    banner("场景2：「退掉那个每个月扣钱的会员」→ 先查候选 → 澄清 → 确认卡")
    llm = MockLLM([
        # 第一步：指代不明，按规则先调绿色查询工具
        json.dumps({
            "thought": "用户指代不明，先查询生效中的订阅候选",
            "tool_name": "query_subscriptions",
            "tool_params": {},
            "response": "正在为您查询代扣服务",
        }, ensure_ascii=False),
        # 第二步：拿到真实候选后 tool_name=null，请用户选择是哪一个
        json.dumps({
            "thought": "查到3个生效订阅，需要用户明确取消哪一个，不能替用户选",
            "tool_name": None,
            "tool_params": {},
            "response": (
                "您名下生效中的自动扣费有：①爱奇艺黄金VIP会员 25元/月（SUB001）；"
                "②网易云音乐黑胶VIP 15元/月（SUB002）；③iCloud+ 200GB 21元/月（SUB003）。"
                "请问您要取消哪一个？"
            ),
        }, ensure_ascii=False),
        # 第三轮：用户选定爱奇艺，带准确ID输出取消工具（系统接管黄色确认）
        json.dumps({
            "thought": "用户选定爱奇艺，对应 SUB001，输出取消代扣工具",
            "tool_name": "cancel_subscription",
            "tool_params": {"subscription_id": "SUB001"},
            "response": "已为您提交取消申请",
        }, ensure_ascii=False),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    reply1 = await agent.chat("退掉那个每个月扣钱的会员")
    print(f"第一轮回复：{reply1}")
    # 必须基于真实查询结果澄清：候选名称/金额/ID 来自 mock_data/subscriptions.json
    assert "哪一个" in reply1 or "哪个" in reply1, "指代不明时必须列出候选请用户选择"
    assert "爱奇艺" in reply1 and "SUB001" in reply1, "候选必须来自查询工具返回的真实数据"
    assert "25" in reply1
    assert agent.pending is None, "澄清阶段不得提前挂起写操作"

    reply2 = await agent.chat("取消爱奇艺那个")
    print(f"第二轮回复：\n{reply2}")
    assert "【操作详情】" in reply2 and "黄色" in reply2, "选定后必须走黄色权限确认卡"
    assert "SUB001" in reply2, "确认卡必须携带准确订阅ID"

    await agent.stop()
    print("✅ PASS：隐含指代先查后问，候选真实，选定后权限确认不跳过")


async def main() -> None:
    print("\n" + "=" * 60)
    print("  模糊输入 / 多轮对话编排测试")
    print("=" * 60)
    await test_stepwise_fill_amount()
    if bank_available():
        await test_ambiguous_subscription_reference()
    else:
        banner("场景2：跳过（未检测到模拟银行服务）")
        print("场景2 需要查询真实订阅数据，请先在另一个终端启动：")
        print("    .venv\\Scripts\\python.exe mock_bank.py")
        print("启动后重新运行本测试即可。")
    print("\n" + "=" * 60)
    print("  已执行的场景全部通过 ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
