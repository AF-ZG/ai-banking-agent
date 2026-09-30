"""全局配置：密钥安全加载、路径、风控阈值。

密钥安全策略：
1. 优先读取环境变量（操作系统级，最安全）；
2. 其次读取项目根目录 .env 文件（已被 .gitignore 隔离）；
3. 代码中任何位置禁止硬编码密钥。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent.parent
MOCK_DATA_DIR = BASE_DIR / "mock_data"
LOG_DIR = BASE_DIR / "logs"
AUDIT_LOG_FILE = LOG_DIR / "audit.jsonl"            # 标准审计：每条 5 要素（时间/用户原话/工具/参数/决策结果）
AUDIT_DETAIL_FILE = LOG_DIR / "audit_detail.jsonl"  # 详细事件流：诊断用
MCP_SERVER_SCRIPT = BASE_DIR / "src" / "mcp_tools" / "server.py"

load_dotenv(BASE_DIR / ".env")

# ---------- 硅基流动 API ----------
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
SILICONFLOW_MODEL = os.getenv("SILICONFLOW_MODEL", "Qwen/Qwen2.5-72B-Instruct")

# ---------- 风控阈值 ----------
SMALL_TRANSFER_DAILY_LIMIT = 1000.0   # 黄色权限：日累计转账上限（元），超过升级红色
VERIFY_CODE_LENGTH = 6                # 模拟验证码长度
VERIFY_CODE_MAX_ATTEMPTS = 3          # 验证码最大尝试次数
MAX_REACT_STEPS = 8                   # 单轮对话 ReAct 最大循环步数

# ---------- 取消指令 ----------
CANCEL_KEYWORDS = ("取消", "不用了", "终止", "算了", "停止", "cancel")
CONFIRM_KEYWORDS = ("确认", "同意", "好的", "可以", "执行", "是", "ok", "yes", "y")


def check_api_key() -> bool:
    """检查密钥是否已配置（不打印密钥本身）。"""
    return bool(SILICONFLOW_API_KEY) and SILICONFLOW_API_KEY != "your_api_key_here"
