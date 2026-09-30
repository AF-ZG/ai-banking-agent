"""集成测试：Mock LLM + 真实 MCP 服务器，验证 ReAct 全链路。

无需 API Key，用预设的模型响应替代真实大模型调用，验证：
1. MCP 服务器启动 & Agent 连接
2. 绿色查询工具自动执行
3. 黄色小额转账 → 确认流程
4. 红色大额转账 → 确认+验证码流程
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.react_agent import ReActAgent


class MockLLM:
    """模拟大模型：按预设顺序返回响应。"""

    def __init__(self, responses):
        self.responses = responses
        self.idx = 0

    def chat(self, messages, temperature=0.3):
        if self.idx < len(self.responses):
            resp = self.responses[self.idx]
            self.idx += 1
            return resp
        return json.dumps({"thought": "结束", "tool_name": None, "tool_params": {}, "response": "对话结束"})


async def test_green_query():
    print("\n" + "=" * 60)
    print("  集成测试1：绿色查询（余额查询）自动执行")
    print("=" * 60)
    llm = MockLLM([
        json.dumps({"thought": "用户要查余额", "tool_name": "query_balance", "tool_params": {}, "response": "正在查询您的余额"}),
        json.dumps({"thought": "已拿到余额", "tool_name": None, "tool_params": {}, "response": "查询完成"}),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()
    reply = await agent.chat("查余额")
    print(f"Agent 回复：{reply}")
    assert "查询完成" in reply, "绿色查询未完成"
    print("✅ PASS：绿色查询自动执行，数据来自后端 MCP 工具")
    await agent.stop()


async def test_yellow_transfer():
    print("\n" + "=" * 60)
    print("  集成测试2：黄色小额转账 → 系统确认卡 → 一次确认即执行")
    print("=" * 60)
    llm = MockLLM([
        json.dumps({"thought": "用户要小额转账", "tool_name": "transfer", "tool_params": {"payee": "王五", "amount": 200, "remark": "借款"}, "response": "已收到您的转账请求"}),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    # 第一轮：模型直接输出工具调用，系统展示确认卡（不依赖模型文本索要确认）
    reply = await agent.chat("给王五转200")
    print(f"第一轮回复：{reply}")
    assert "【操作详情】" in reply and "确认" in reply, "未展示系统确认卡"

    # 第二轮：用户确认 → 直接执行（无需二次确认）
    reply = await agent.chat("确认")
    print(f"Agent 回复：{reply}")
    assert "操作已执行" in reply and "200" in reply, "小额转账未按预期执行"
    print("✅ PASS：黄色操作单次确认即执行，结果明确")
    await agent.stop()


async def test_red_transfer_cancel():
    print("\n" + "=" * 60)
    print("  集成测试3：红色大额转账 → 取消终止，不执行")
    print("=" * 60)
    llm = MockLLM([
        json.dumps({"thought": "大额转账", "tool_name": "transfer", "tool_params": {"payee": "陌生人", "amount": 8000}, "response": "已收到您的转账请求"}),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    reply = await agent.chat("给陌生人转8000")
    assert "【操作详情】" in reply, "未展示系统确认卡"
    # 模拟用户取消
    reply = await agent.chat("取消")
    print(f"Agent 回复：{reply}")
    assert "取消" in reply or "终止" in reply, "取消指令未正确终止"
    assert agent.pending is None, "取消后挂起任务未清空"
    print("✅ PASS：红色操作取消后不执行，资金安全")
    await agent.stop()


async def test_pending_reminder():
    print("\n" + "=" * 60)
    print("  集成测试4：待确认状态下输入新需求 → 保持挂起仅提醒")
    print("=" * 60)
    llm = MockLLM([
        json.dumps({"thought": "用户要小额转账", "tool_name": "transfer", "tool_params": {"payee": "王五", "amount": 200}, "response": "已收到您的转账请求"}),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    await agent.chat("给王五转200")  # 挂起待确认
    # 待确认状态下输入无关新需求 → 不执行、不吞掉，仅提醒
    reply = await agent.chat("帮我转账给李四 300 元")
    print(f"Agent 回复：{reply}")
    assert "待确认" in reply and "王五" in reply, "未正确提醒挂起任务"
    assert agent.pending is not None, "挂起任务被意外清空"

    # 随后确认 → 仍执行的是王五那笔（不会错位成李四）
    reply = await agent.chat("确认")
    print(f"Agent 回复：{reply}")
    assert "操作已执行" in reply and "王五" in reply, "确认执行的操作与挂起任务不一致"
    print("✅ PASS：待确认状态下新需求被明确提醒，确认执行对象不错位")
    await agent.stop()


async def test_red_full_flow():
    print("\n" + "=" * 60)
    print("  集成测试5：红色大额转账 → 确认 → 验证码 → 执行")
    print("=" * 60)
    llm = MockLLM([
        json.dumps({"thought": "大额转账", "tool_name": "transfer", "tool_params": {"payee": "陌生人", "amount": 8000}, "response": "已收到您的转账请求"}),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    reply = await agent.chat("给陌生人转8000")
    assert "【操作详情】" in reply and "红色" in reply, "未展示红色确认卡"

    reply = await agent.chat("确认")
    print(f"确认后回复：{reply}")
    assert "验证码" in reply, "确认后未下发验证码"
    code = agent.pending.verify_code

    reply = await agent.chat(code)
    print(f"Agent 回复：{reply}")
    assert "验证码核验通过" in reply and "操作已执行" in reply, "验证码通过后未执行"
    assert agent.pending is None, "执行后挂起任务未清空"
    print("✅ PASS：红色操作确认+验证码两步核验后执行")
    await agent.stop()


async def main():
    print("\n" + "=" * 60)
    print("  集成测试：Mock LLM + 真实 MCP 全链路")
    print("=" * 60)
    await test_green_query()
    await test_yellow_transfer()
    await test_red_transfer_cancel()
    await test_pending_reminder()
    await test_red_full_flow()
    print("\n" + "=" * 60)
    print("  全部集成测试通过 ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
