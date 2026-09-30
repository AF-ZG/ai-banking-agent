"""模糊金额防护测试（防参数幻觉，Mock LLM，无需真实 API Key）。

验证：
1. detect_ambiguous_amount：拼音/非标单位/裸数字全部识别，标准写法不误伤
2. amount_to_chinese：金额大写转换
3. 引擎流程A：10qian → 模型按规则追问；澄清后带 amount_clarified 标记
   → 确认卡顶部加粗警告 + 人民币大写 + 审计落盘「参数来源于模糊推断，已要求二次确认」
4. 引擎流程B：模型忘记打标时，代码正则+跨轮记忆仍强制警告（纵深防御）
5. 反向回归：标准写法"100元"确认卡不出现金额警告

运行方式（项目根目录）：
    python tests/test_amount_guard.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.react_agent import ReActAgent  # noqa: E402
from src.config import AUDIT_DETAIL_FILE  # noqa: E402
from src.permissions.router import amount_to_chinese, detect_ambiguous_amount  # noqa: E402


class MockLLM:
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


# ============================================================
# 用例1：模糊金额检测规则
# ============================================================
def test_detect_rules() -> None:
    banner("用例1：模糊金额检测（拼音/非标/裸数字必拦，标准写法放行）")
    must_block = {
        "10qian": "拼音",
        "给张三转10qian": "句中拼音",
        "1wan": "拼音wan",
        "10块5": "非标准单位",
        "3毛6": "非标准单位",
        "100": "裸数字",
        "100.5": "裸小数",
        "1.2.3元": "多小数点",
    }
    for text, tag in must_block.items():
        reason = detect_ambiguous_amount(text)
        print(f"[{tag}] 输入「{text}」→ {reason or '未拦截！'}")
        assert reason, f"模糊金额未被识别：{text}"

    must_pass = ["100元", "给张三转账200元", "200块", "1.5万元", "3万块钱", "查一下余额"]
    for text in must_pass:
        reason = detect_ambiguous_amount(text)
        print(f"[放行] 输入「{text}」→ {reason or '安全'}")
        assert not reason, f"标准写法被误伤：{text}"
    print("✅ PASS：该拦的全拦，该放的全放")


# ============================================================
# 用例2：金额大写
# ============================================================
def test_chinese_amount() -> None:
    banner("用例2：人民币大写转换")
    cases = {10000: "壹万", 100: "壹佰", 100.5: "伍角", 8000: "捌仟", 200.0: "元整"}
    for amt, key in cases.items():
        cn = amount_to_chinese(amt)
        print(f"{amt} → {cn}")
        assert key in cn
    assert amount_to_chinese(10000) == "人民币壹万元整"
    print("✅ PASS：大写转换正确")


# ============================================================
# 用例3：模型按规则打标 amount_clarified → 加粗警告卡 + 审计
# ============================================================
async def test_flagged_clarification() -> None:
    banner("用例3：10qian 先追问 → 澄清后确认卡加粗警告并审计")
    llm = MockLLM([
        json.dumps({
            "thought": "10qian 金额模糊，不能脑补，必须追问",
            "tool_name": None,
            "tool_params": {},
            "response": "您输入的“10qian”是指10元还是10000元（10千）？请明确一下金额。",
        }, ensure_ascii=False),
        json.dumps({
            "thought": "用户明确是10000元，按规则带 amount_clarified 标记",
            "tool_name": "transfer",
            "tool_params": {"payee": "张三", "amount": 10000, "remark": ""},
            "amount_clarified": True,
            "response": "已收到您的转账请求",
        }, ensure_ascii=False),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()

    reply1 = await agent.chat("给张三转账，10qian")
    print(f"第一轮：{reply1}")
    assert "10000" in reply1 and "10元" in reply1, "必须给出可能解读让用户澄清，禁止脑补"
    assert agent.pending is None

    reply2 = await agent.chat("是10000元，转吧")
    print(f"第二轮确认卡：\n{reply2}")
    assert "金额二次核对" in reply2, "确认卡必须有加粗二次核对警告"
    assert "10000.00" in reply2 and "壹万" in reply2, "警告必须含数值与人民币大写"
    assert "红色" in reply2, "1万元必须走红色权限，不得降级"
    assert agent.pending is not None and agent.pending.amount_warning is True

    # 审计校验：最近的 amount_clarified_confirm 事件
    event = _last_audit_event("amount_clarified_confirm")
    print("审计事件：", json.dumps(event, ensure_ascii=False))
    assert event and event["message"] == "参数来源于模糊推断，已要求二次确认"
    assert event["tool_params"]["amount"] == 10000

    await agent.stop()
    print("✅ PASS：澄清后警告卡与强制审计均生效")


# ============================================================
# 用例4：模型忘记打标 → 代码正则+跨轮记忆兜底
# ============================================================
async def test_heuristic_fallback() -> None:
    banner("用例4：模型未打标时，代码层仍强制警告（防模型不听话）")
    llm = MockLLM([
        json.dumps({"thought": "追问", "tool_name": None, "tool_params": {},
                    "response": "您说的10qian是指多少钱？"}, ensure_ascii=False),
        # 故意不打 amount_clarified 标记，验证代码兜底
        json.dumps({"thought": "用户澄清为10000元", "tool_name": "transfer",
                    "tool_params": {"payee": "张三", "amount": 10000},
                    "response": "好的"}, ensure_ascii=False),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()
    await agent.chat("10qian给张三")
    reply = await agent.chat("10000元")
    print(reply)
    assert "金额二次核对" in reply and "壹万" in reply, "模型漏标时代码必须兜住"
    assert agent.pending.amount_warn_source.startswith("代码检测"), "来源应标明为代码检测"
    await agent.stop()
    print("✅ PASS：纵深防御生效，模型漏标也逃不过代码校验")


# ============================================================
# 用例5：标准金额不得误报警告
# ============================================================
async def test_standard_amount_no_warning() -> None:
    banner("用例5：标准写法「100元」不出现金额警告")
    llm = MockLLM([
        json.dumps({"thought": "标准小额转账", "tool_name": "transfer",
                    "tool_params": {"payee": "王五", "amount": 100, "remark": ""},
                    "response": "好的"}, ensure_ascii=False),
    ])
    agent = ReActAgent(llm_client=llm)
    await agent.start()
    reply = await agent.chat("给王五转100元")
    print(reply)
    assert "金额二次核对" not in reply, "标准金额不应触发模糊警告"
    assert "【操作详情】" in reply and "黄色" in reply
    assert agent.pending.amount_warning is False
    await agent.stop()
    print("✅ PASS：标准写法不误伤，正常走黄色确认")


def _last_audit_event(event_type: str) -> dict | None:
    if not AUDIT_DETAIL_FILE.exists():
        return None
    found = None
    for line in AUDIT_DETAIL_FILE.read_text(encoding="utf-8").strip().splitlines():
        rec = json.loads(line)
        if rec.get("event_type") == event_type:
            found = rec
    return found


async def main() -> None:
    print("\n" + "=" * 60)
    print("  模糊金额防护测试：10qian 不得被脑补成 10 元")
    print("=" * 60)
    test_detect_rules()
    test_chinese_amount()
    await test_flagged_clarification()
    await test_heuristic_fallback()
    await test_standard_amount_no_warning()
    print("\n" + "=" * 60)
    print("  全部金额防护测试通过 ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
