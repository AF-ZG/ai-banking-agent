"""ReAct 智能体核心循环。

- 通过 MCP 标准协议（stdio）连接工具服务器；
- 强制 JSON 结构化输出解析；
- 三级权限路由：绿色直执 / 黄色确认 / 红色确认+验证码；
- 全链路审计落盘。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import date
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.agent.prompts import SYSTEM_PROMPT
from src.audit import logger as audit
from src.config import MAX_REACT_STEPS, MCP_SERVER_SCRIPT, VERIFY_CODE_MAX_ATTEMPTS
from src.llm.client import SiliconFlowClient
from src.permissions.router import (
    PermissionDecision,
    amount_to_chinese,
    check_code,
    classify,
    detect_ambiguous_amount,
    generate_code,
    is_cancel,
    is_confirm,
    is_pure_cancel,
)

# 带金额参数、必须做模糊金额防护的操作工具
_AMOUNT_BEARING_TOOLS = {"transfer", "wealth_subscribe", "wealth_redeem"}
_TOOL_LABELS = {
    "transfer": "转账",
    "wealth_subscribe": "理财申购",
    "wealth_redeem": "理财赎回",
}


# ============================================================
# 工具函数
# ============================================================
def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象（容忍 markdown 包裹）。"""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"无法解析模型输出为 JSON：{text[:200]}")


def _format_tool_result(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


# ============================================================
# ReAct Agent
# ============================================================
class ReActAgent:
    def __init__(self, llm_client: SiliconFlowClient | None = None):
        self.llm = llm_client or SiliconFlowClient()
        self.history: list[dict[str, str]] = []
        self.turn_id = ""
        self.pending: PermissionDecision | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.available_tools: set[str] = set()
        self.tool_catalog: str = ""  # 注入系统提示词的工具清单文本
        self._amount_was_ambiguous: str = ""  # 近轮用户输入中的模糊金额原因（跨轮兜底）

    # -------- 生命周期 --------
    async def start(self) -> None:
        """启动 MCP 服务器子进程并建立 stdio 会话。"""
        self._exit_stack = AsyncExitStack()
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER_SCRIPT)],
        )
        read, write = await self._exit_stack.enter_async_context(stdio_client(server_params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        tools = await self._session.list_tools()
        self.available_tools = {t.name for t in tools.tools}
        # 生成工具清单文本，注入系统提示词，让模型准确选择工具名
        self.tool_catalog = "\n".join(
            f"- {t.name}：{(t.description or '').strip()}"
            for t in tools.tools
        )

    async def stop(self) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    # -------- 主对话入口 --------
    async def chat(self, user_input: str) -> str:
        """处理一轮用户输入，返回最终回复（确认/验证均为独立轮次，无内联输入）。

        每轮结束写一条标准审计记录（5 要素：时间/用户原话/工具/参数/决策结果）。
        """
        self.turn_id = uuid.uuid4().hex[:12]
        rec: dict[str, Any] = {"user_input": user_input, "tool": None, "params": {}, "decision": ""}

        try:
            # 取消指令处理：
            # - 有挂起的确认/验证任务时安全优先，任何含取消语义的输入一律终止挂起任务；
            # - 无挂起任务时，只有「纯取消指令」（取消/算了吧）才终止；
            #   含业务宾语的（如「取消爱奇艺那个」「把会员取消了」）是新操作意图，放行给大模型。
            if is_cancel(user_input) and (self.pending is not None or is_pure_cancel(user_input)):
                if self.pending:
                    pending = self.pending
                    rec["tool"] = pending.tool_name
                    rec["params"] = pending.tool_params
                    rec["decision"] = "用户取消-待执行链路已清空"
                    audit.log("cancel", {
                        "turn_id": self.turn_id,
                        "user_input": user_input,
                        "pending_tool": pending.tool_name,
                        "pending_params": pending.tool_params,
                    })
                    self.pending = None
                    self._amount_was_ambiguous = ""
                    return "已终止当前任务，待执行链路已清空。"
                rec["decision"] = "无待执行任务-取消未完成需求"
                self._amount_was_ambiguous = ""
                return "好的，已取消，未完成的需求不再继续。"

            # 有待确认/待验证的操作：进入确认链路，不再走 LLM
            if self.pending:
                return await self._handle_pending(user_input, rec)

            audit.log("turn_start", {"turn_id": self.turn_id, "user_input": user_input})
            self.history.append({"role": "user", "content": user_input})

            for step in range(MAX_REACT_STEPS):
                today = date.today()
                system_prompt = (
                    SYSTEM_PROMPT
                    + f"\n\n# 当前系统日期（时间推算的唯一依据，禁止编造日期）\n"
                    + f"今天是 {today.isoformat()}（{today.strftime('%Y年%m月%d日')}），"
                    + f"本月为 {today.strftime('%Y-%m')}，本月1日为 {today.replace(day=1).isoformat()}。"
                )
                if self.tool_catalog:
                    system_prompt += (
                        "\n\n# 可用MCP工具清单（tool_name 必须严格从以下名称中选择，禁止编造）\n"
                        + self.tool_catalog
                    )
                raw = self.llm.chat([{"role": "system", "content": system_prompt}] + self.history)
                try:
                    parsed = _extract_json(raw)
                except Exception:
                    # 给模型一次自我修正机会
                    self.history.append({"role": "assistant", "content": raw})
                    self.history.append({"role": "user", "content": "【系统】你的输出不是合法JSON，请严格按协议重新输出。"})
                    continue

                thought = parsed.get("thought", "")
                tool_name = parsed.get("tool_name")
                tool_params = parsed.get("tool_params") or {}
                reply = parsed.get("response", "")
                amount_clarified = bool(parsed.get("amount_clarified"))

                audit.log("llm_step", {
                    "turn_id": self.turn_id,
                    "step": step,
                    "thought": thought,
                    "tool_name": tool_name,
                    "tool_params": tool_params,
                })

                # 模糊金额检测必须先于「无需工具直接回复」分支：
                # 追问轮（tool_name=null）也要记住用户说过 10qian，供澄清后的下一轮兜底。
                ambiguous_reason = detect_ambiguous_amount(user_input)
                if ambiguous_reason:
                    self._amount_was_ambiguous = ambiguous_reason

                # 无需工具：直接回复
                if not tool_name:
                    if not rec["decision"]:
                        rec["decision"] = "绿色权限-无需工具，直接答复"
                    self.history.append({"role": "assistant", "content": reply})
                    audit.log("turn_end", {"turn_id": self.turn_id, "response": reply})
                    return reply

                # 工具存在性校验
                if tool_name not in self.available_tools:
                    self.history.append({"role": "assistant", "content": raw})
                    self.history.append({"role": "user", "content": f"【系统】工具 {tool_name} 不存在，请检查或向用户说明无法办理。"})
                    continue

                # 权限判定（转账需先查今日累计）
                today_total = 0.0
                if tool_name == "transfer":
                    total_result = await self._execute_tool("query_today_transfer_total", {})
                    today_total = total_result.get("today_transfer_total", 0.0)
                decision = classify(tool_name, tool_params, today_total=today_total)

                # 模糊金额防护：以代码正则检测（含跨轮记忆）为唯一触发源。
                # 模型 amount_clarified 标记仅作提示词约束手段，不作为警告依据——
                # 实测模型会对标准写法（如"100元"）误打标，信任它会导致警告疲劳。
                if tool_name in _AMOUNT_BEARING_TOOLS and self._amount_was_ambiguous:
                    decision.amount_warning = True
                    decision.amount_warn_source = f"代码检测：{self._amount_was_ambiguous}"

                # 绿色：直接执行并继续 ReAct
                if decision.level == "green":
                    tool_result = await self._execute_tool(tool_name, tool_params)
                    audit.log("tool_call", {
                        "turn_id": self.turn_id,
                        "tool_name": tool_name,
                        "tool_params": tool_params,
                        "permission_level": "green",
                        "user_confirmed": None,
                        "verification_passed": None,
                        "tool_result": tool_result,
                    })
                    rec["tool"] = tool_name
                    rec["params"] = tool_params
                    rec["decision"] = "绿色权限-自动执行"
                    self.history.append({"role": "assistant", "content": raw})
                    self.history.append({
                        "role": "user",
                        "content": f"【系统】工具 {tool_name} 返回结果：\n{_format_tool_result(tool_result)}\n请基于该结果给用户最终回复（tool_name 填 null）。",
                    })
                    continue

                # 黄色 / 红色：挂起，由系统展示确认卡，等待用户下一轮输入
                self.pending = decision
                rec["tool"] = tool_name
                rec["params"] = tool_params
                base_decision = (
                    "黄色权限-待用户确认" if decision.level == "yellow" else "红色权限-待用户确认"
                )
                warning_banner = ""
                if decision.amount_warning:
                    # 强制安全审计：参数来源于模糊推断，已要求二次确认
                    audit.log("amount_clarified_confirm", {
                        "turn_id": self.turn_id,
                        "tool_name": tool_name,
                        "tool_params": tool_params,
                        "trigger": decision.amount_warn_source,
                        "message": "参数来源于模糊推断，已要求二次确认",
                    })
                    warning_banner = self._amount_warning_banner(decision)
                    base_decision += "-金额来源于模糊推断-已要求二次确认"
                    self._amount_was_ambiguous = ""  # 已挂到 pending，消费掉跨轮标记
                rec["decision"] = base_decision
                self.history.append({"role": "assistant", "content": reply})
                level_text = "黄色（需确认）" if decision.level == "yellow" else "红色（需确认+验证码）"
                return (
                    f"{warning_banner}"
                    f"————————————————\n"
                    f"【操作详情】{decision.summary}\n"
                    f"【权限等级】{level_text}\n"
                    f"请回复「确认」执行，或「取消」终止。"
                )

            rec["decision"] = "处理失败-ReAct步数超限"
            return "抱歉，本轮处理步骤过多，请换个方式描述需求。"
        finally:
            # 每轮固定落一条 5 要素标准审计记录
            rec["decision"] = rec["decision"] or "处理异常-系统错误"
            audit.log_turn(rec["user_input"], rec["tool"], rec["params"], rec["decision"])

    # -------- 确认 / 验证链路 --------
    async def _handle_pending(self, user_input: str, rec: dict[str, Any]) -> str:
        decision = self.pending
        if not decision:
            rec["decision"] = "系统状态异常"
            return "系统状态异常，请重新发起操作。"

        rec["tool"] = decision.tool_name
        rec["params"] = decision.tool_params

        # 任意时刻可取消
        if is_cancel(user_input):
            audit.log("cancel", {
                "turn_id": self.turn_id,
                "user_input": user_input,
                "pending_tool": decision.tool_name,
                "pending_params": decision.tool_params,
            })
            self.pending = None
            self._amount_was_ambiguous = ""
            rec["decision"] = "用户取消-待执行链路已清空"
            msg = "已取消该操作，待执行链路已清空。"
            self.history.append({"role": "assistant", "content": msg})
            return msg

        # 黄色：确认即执行
        if decision.level == "yellow":
            if is_confirm(user_input):
                decision.confirmed = True
                result = await self._execute_tool(decision.tool_name, decision.tool_params)
                audit.log("tool_call", {
                    "turn_id": self.turn_id,
                    "tool_name": decision.tool_name,
                    "tool_params": decision.tool_params,
                    "permission_level": "yellow",
                    "user_confirmed": True,
                    "verification_passed": None,
                    "amount_clarified": decision.amount_warning,
                    "tool_result": result,
                })
                self.pending = None
                rec["decision"] = "黄色权限-用户确认后执行成功" if result.get("success", True) else "黄色权限-用户确认后执行失败"
                reply = f"✅ 操作已执行：{result.get('message', _format_tool_result(result))}"
                self.history.append({"role": "user", "content": user_input})
                self.history.append({"role": "assistant", "content": reply})
                return reply
            rec["decision"] = "黄色权限-待用户确认（已提醒）"
            return self._pending_reminder(decision)

        # 红色第一步：确认后下发验证码
        if not decision.confirmed:
            if is_confirm(user_input):
                decision.confirmed = True
                decision.verify_code = generate_code()
                audit.log("confirm", {
                    "turn_id": self.turn_id,
                    "user_input": user_input,
                    "pending_tool": decision.tool_name,
                    "pending_params": decision.tool_params,
                })
                rec["decision"] = "红色权限-已确认，验证码已下发待核验"
                msg = (
                    f"📩 模拟短信验证码已发送：【{decision.verify_code}】\n"
                    f"请输入 {len(decision.verify_code)} 位验证码完成核验（输入「取消」终止）。"
                )
                self.history.append({"role": "user", "content": user_input})
                self.history.append({"role": "assistant", "content": msg})
                return msg
            rec["decision"] = "红色权限-待用户确认（已提醒）"
            return self._pending_reminder(decision)

        # 红色第二步：验证码核验（非取消输入均计为一次尝试）
        decision.code_attempts += 1
        if check_code(user_input, decision.verify_code):
            result = await self._execute_tool(decision.tool_name, decision.tool_params)
            audit.log("tool_call", {
                "turn_id": self.turn_id,
                "tool_name": decision.tool_name,
                "tool_params": decision.tool_params,
                "permission_level": "red",
                "user_confirmed": True,
                "verification_passed": True,
                "amount_clarified": decision.amount_warning,
                "tool_result": result,
            })
            self.pending = None
            rec["decision"] = "红色权限-验证码核验通过后执行成功" if result.get("success", True) else "红色权限-验证码核验通过后执行失败"
            reply = f"✅ 验证码核验通过，操作已执行：{result.get('message', _format_tool_result(result))}"
            self.history.append({"role": "user", "content": user_input})
            self.history.append({"role": "assistant", "content": reply})
            return reply

        if decision.code_attempts >= VERIFY_CODE_MAX_ATTEMPTS:
            audit.log("verify_failed", {
                "turn_id": self.turn_id,
                "pending_tool": decision.tool_name,
                "pending_params": decision.tool_params,
                "attempts": decision.code_attempts,
            })
            self.pending = None
            rec["decision"] = "红色权限-验证码错误超限-操作终止"
            msg = "❌ 验证码错误次数过多，本次操作已终止，待执行链路已清空。"
            self.history.append({"role": "assistant", "content": msg})
            return msg

        remaining = VERIFY_CODE_MAX_ATTEMPTS - decision.code_attempts
        rec["decision"] = f"红色权限-验证码错误（剩余{remaining}次机会）"
        return (
            f"❌ 验证码错误，请重新输入 {len(decision.verify_code)} 位数字验证码"
            f"（剩余 {remaining} 次机会，输入「取消」终止）。"
        )

    def _pending_reminder(self, decision: PermissionDecision) -> str:
        """待确认状态下收到无关输入：保持挂起，仅提醒，不处理新需求。"""
        return (
            f"⚠️ 当前有未完成的待确认操作：{decision.summary}\n"
            f"请先回复「确认」执行或「取消」终止，处理完毕后我再为您办理其他需求。"
        )

    def _amount_warning_banner(self, decision: PermissionDecision) -> str:
        """金额来自模糊推断时，在确认卡顶部加粗展示数值+人民币大写，强制二次核对。"""
        try:
            amount = float(decision.tool_params.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0.0
        label = _TOOL_LABELS.get(decision.tool_name, "操作")
        return (
            f"\033[1m\033[91m⚠️ 金额二次核对：您正在{label} {amount:.2f} 元"
            f"（{amount_to_chinese(amount)}）\n"
            f"该金额来自模糊输入的澄清推断，请务必仔细核对后再确认！\033[0m\n"
        )

    # -------- MCP 工具调用 --------
    async def _execute_tool(self, tool_name: str, params: dict) -> Any:
        """通过 MCP 协议调用工具，返回解析后的业务数据。"""
        assert self._session is not None, "MCP 会话未初始化"
        result = await self._session.call_tool(tool_name, params)
        # MCP 返回 content 列表，取文本内容并解析 JSON
        if result.content:
            text = result.content[0].text  # type: ignore[attr-defined]
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}
        return {"success": False, "message": "工具无返回"}
