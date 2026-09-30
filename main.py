"""AI Banking Agent — Web API 入口（FastAPI，端口 8000）。

职责：把 ReActAgent、沙箱拦截、模糊追问等核心逻辑以 HTTP 接口暴露给前端 index.html。
数据读写一律经由模拟银行服务 mock_bank.py（http://localhost:8001）。

启动方式（两个终端）：
    python mock_bank.py     # 模拟银行服务，端口 8001
    python main.py          # 本文件：Web API，端口 8000
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.agent.react_agent import ReActAgent
from src.config import LOG_DIR, check_api_key
from src.permissions.router import amount_to_chinese

BANK_URL = "http://localhost:8001"

# ============================================================
# 模拟沙箱：用户输入进入大模型前的安全检查门
# ============================================================
SANDBOX_MAX_LENGTH = 500  # 单次输入最大字符数

# 提示词注入 / 越权类敏感词根：采用「词根包含匹配」（正则），
# 只要输入里出现这些词根就拦截——无论前后或中间隔着什么字。
SANDBOX_SENSITIVE_KEYWORDS = (
    "忽略", "绕过", "无视", "开发者模式", "jailbreak",
)
_SENSITIVE_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in SANDBOX_SENSITIVE_KEYWORDS),
    re.IGNORECASE,
)

# 危险代码 / 系统命令字样（不区分大小写，命中即拦截）
SANDBOX_DANGEROUS_CODE_KEYWORDS = (
    "os.system", "subprocess", "eval(", "exec(",
    "rm -rf", "del /f", "format c", "delete",
)

SANDBOX_BLOCK_MESSAGE = "【沙箱拦截】检测到高风险输入，操作已被安全终止"
SANDBOX_DECISION = "沙箱拦截-未发送大模型"
SANDBOX_AUDIT_FILE = LOG_DIR / "audit_sandbox.jsonl"


def _log_sandbox_block(user_input: str, reason: str) -> None:
    """每次拦截强制追加一条审计日志（时间/用户原话/拦截原因/决策结果）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "user_input": user_input,
        "reason": reason,
        "decision": SANDBOX_DECISION,
    }
    with open(SANDBOX_AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sandbox_check(user_input: str) -> str | None:
    """用户输入安全检查门。

    在输入送入大模型之前调用：
    - 通过返回 None；
    - 命中风险规则则记录审计日志，并返回红色警告文本（调用方不得再调用 Agent）。
    """
    if len(user_input) > SANDBOX_MAX_LENGTH:
        reason = f"输入长度超限（{len(user_input)} > {SANDBOX_MAX_LENGTH} 字符）"
        _log_sandbox_block(user_input, reason)
        return f"{SANDBOX_BLOCK_MESSAGE}（{reason}）"

    hit = _SENSITIVE_PATTERN.search(user_input)
    if hit:
        reason = f"命中敏感词根「{hit.group(0)}」"
        _log_sandbox_block(user_input, reason)
        return f"{SANDBOX_BLOCK_MESSAGE}（{reason}）"

    lowered = user_input.lower()
    for kw in SANDBOX_DANGEROUS_CODE_KEYWORDS:
        if kw in lowered:
            reason = f"含危险代码字样「{kw}」"
            _log_sandbox_block(user_input, reason)
            return f"{SANDBOX_BLOCK_MESSAGE}（{reason}）"

    return None


# ============================================================
# FastAPI 应用
# ============================================================
app = FastAPI(title="AI Banking Agent API", version="2.0")

# 允许网页跨域请求（本地演示放开全部来源）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """CLI 时代的 ANSI 颜色码对网页无意义，统一剥掉。"""
    return _ANSI_RE.sub("", text or "")


class ChatRequest(BaseModel):
    user_input: str = ""
    session_id: str = "default"
    session_state: dict | None = None  # 预留：前端会话状态透传，目前由后端统一保管


class ActionRequest(BaseModel):
    action: str                      # confirm / cancel / verify
    session_id: str = "default"
    code: str | None = None          # verify 动作携带验证码


# 会话表：session_id -> (ReActAgent, 串行锁)。多轮对话状态全部在 Agent 实例内
# （history / pending / 模糊金额跨轮标记），会话不丢。
_sessions: dict[str, tuple[ReActAgent, asyncio.Lock]] = {}


async def _get_session(session_id: str) -> tuple[ReActAgent, asyncio.Lock]:
    if session_id not in _sessions:
        agent = ReActAgent()
        await agent.start()  # 启动 MCP 工具子进程
        _sessions[session_id] = (agent, asyncio.Lock())
    return _sessions[session_id]


def _today_total() -> float | None:
    """从模拟银行服务读取今日转账累计（供前端侧栏风控口径展示）。"""
    try:
        r = requests.get(f"{BANK_URL}/api/transfer/today-total", timeout=2)
        return r.json().get("today_transfer_total")
    except Exception:
        return None


def _build_response(agent: ReActAgent, reply: str, session_id: str) -> dict:
    """把 Agent 的文本回复 + 挂起状态翻译成与前端约定的结构化 JSON。

    约定：
    - {"type": "text", "content": ...}                                普通回复
    - {"type": "confirm", "level": "yellow|red", "details": ...}      确认卡片
    - {"type": "sms", "details": ..., "code": ...}                    验证码（演示环境回显）
    - {"type": "sandbox_block", "content": ...}                       沙箱拦截
    """
    base = {"session_id": session_id, "today_total": _today_total()}
    pending = agent.pending

    if pending is not None and pending.verify_code:
        # 红色第二步：验证码已下发
        return {
            **base,
            "type": "sms",
            "level": pending.level,
            "details": _strip_ansi(reply),
            "code": pending.verify_code,  # 演示环境直接回显，真实场景走短信通道
        }

    if pending is not None:
        # 黄色 / 红色第一步：确认卡片
        warning = ""
        if pending.amount_warning:
            try:
                amount = float(pending.tool_params.get("amount", 0))
            except (TypeError, ValueError):
                amount = 0.0
            warning = (
                f"您正在操作 {amount:.2f} 元（{amount_to_chinese(amount)}），"
                f"该金额来自模糊输入的澄清推断，请务必仔细核对后再确认！"
            )
        return {
            **base,
            "type": "confirm",
            "level": pending.level,
            "details": pending.summary,
            "warning": warning,
            "content": _strip_ansi(reply),
        }

    return {**base, "type": "text", "content": _strip_ansi(reply)}


async def _run_turn(session_id: str, text: str) -> dict:
    """一轮对话的完整管道：沙箱 -> Agent -> 结构化响应。"""
    blocked = sandbox_check(text)
    if blocked is not None:
        return {
            "type": "sandbox_block",
            "content": _strip_ansi(blocked),
            "session_id": session_id,
            "today_total": _today_total(),
        }
    agent, lock = await _get_session(session_id)
    async with lock:  # 同一会话串行，防止确认/验证码状态被并发打乱
        try:
            reply = await agent.chat(text)
        except Exception as e:
            return {
                "type": "text",
                "content": f"系统繁忙，请稍后再试。（{e}）",
                "session_id": session_id,
                "today_total": _today_total(),
            }
    return _build_response(agent, reply, session_id)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "sessions": len(_sessions), "bank_url": BANK_URL}


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    """接收前端用户输入，返回结构化响应（text/confirm/sms/sandbox_block）。"""
    text = (req.user_input or "").strip()
    if not text:
        return {"type": "text", "content": "请输入内容。", "session_id": req.session_id}
    return await _run_turn(req.session_id or "default", text)


@app.post("/api/action")
async def action(req: ActionRequest) -> dict:
    """接收用户点击「确认/取消」或提交验证码的动作，推进挂起中的业务流程。

    动作统一映射为等价文本进入正常对话管道，权限与状态机逻辑复用 ReActAgent。
    """
    mapping = {"confirm": "确认", "cancel": "取消"}
    if req.action == "verify":
        if not (req.code or "").strip():
            return {"type": "text", "content": "请输入 6 位验证码。", "session_id": req.session_id}
        text = req.code.strip()
    elif req.action in mapping:
        text = mapping[req.action]
    else:
        return {"type": "text", "content": f"未知动作：{req.action}", "session_id": req.session_id}
    return await _run_turn(req.session_id or "default", text)


@app.post("/api/reset")
async def reset(req: ActionRequest | None = None) -> dict:
    """重置会话：停掉该会话的 Agent（MCP 子进程），下次对话自动重建。"""
    session_id = (req.session_id if req else "default") or "default"
    if session_id in _sessions:
        agent, _ = _sessions.pop(session_id)
        try:
            await agent.stop()
        except Exception:
            pass
    return {"ok": True, "session_id": session_id}


@app.on_event("startup")
def _startup() -> None:
    if not check_api_key():
        print("⚠️  未检测到硅基流动 API 密钥（.env 的 SILICONFLOW_API_KEY），对话请求会失败。")
    print("AI Banking Agent Web API 已启动：http://localhost:8000 （银行服务：%s）" % BANK_URL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    for agent, _ in list(_sessions.values()):
        try:
            await agent.stop()
        except Exception:
            pass
    _sessions.clear()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
