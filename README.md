# AI Banking Agent · 智能银行助手

基于 **ReAct + MCP + Function Calling** 架构的安全型银行业务智能体。

用户通过自然语言下达指令，Agent 经过「沙箱安全检查 → 意图理解 → 三色权限路由 → 工具执行」的完整链路，完成转账、账单分析、理财操作、订阅代扣管理四大核心业务。全部数据为本地 Mock，无真实金融接口与资金风险。

## 核心特性

- **三色权限分级**：绿色（查询类自动执行）/ 黄色（小额写操作需确认）/ 红色（大额写操作需确认 + 6 位验证码）
- **沙箱安全门**：用户输入进入大模型前先做敏感词根（正则包含匹配）、危险代码、超长输入三重拦截，拦截不触达大模型
- **防幻觉机制**：关键参数缺失强制追问，严禁编造；模糊金额（如 `10qian`）代码层正则兜底检测，澄清后确认卡加粗警告
- **多轮对话状态**：跨轮拼接上下文（「给张三转账」+「100块」）、挂起任务管理、取消语义识别
- **全链路审计**：操作日志 / 沙箱拦截日志 / 模糊参数二次确认日志均以 JSONL 落盘

## 环境要求

- Python 3.11+（开发环境为 3.14）
- 硅基流动（SiliconFlow）API 密钥
- 操作系统：Windows / macOS / Linux

## 部署说明

**启动顺序不可颠倒：先 mock_bank.py，再 main.py，最后打开 index.html。**

### 1. 安装依赖

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置密钥

在项目根目录新建 `.env` 文件（已被 .gitignore 隔离，不会上传）：

```env
SILICONFLOW_API_KEY=你的硅基流动API密钥
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_MODEL=Qwen/Qwen2.5-72B-Instruct
```

### 3. 启动服务（两个终端）

```bash
# 终端 1：模拟银行服务（端口 8001，负责 mock_data 数据读写）
python mock_bank.py

# 终端 2：Agent Web API（端口 8000，对接前端）
python main.py
```

### 4. 打开前端

用浏览器直接打开根目录的 `index.html` 即可。左侧边栏提供 22 个快捷测试按钮，覆盖四大业务场景、安全攻击测试和模糊表达场景。

## 测试用例运行方法

测试位于 `tests/` 目录，每个文件可直接用 Python 运行（不依赖 pytest）：

```bash
# 沙箱拦截测试（6 用例：正常放行 / 注入拦截 / 危险代码 / 超长 / 不触达大模型 / 审计落盘）
python tests/test_sandbox.py

# 金额防护测试（模糊金额检测、大写转换、警告触发链路、标准写法不误伤）
python tests/test_amount_guard.py

# 模糊对话测试（分步补金额、模糊指代订阅，Mock LLM）
python tests/test_fuzzy_dialogue.py

# 集成测试（Mock LLM + 真实 MCP 全链路：绿/黄/红权限、挂起提醒、验证码）
# 注意：需先启动 mock_bank.py，且会真实写入 mock_data（测试后建议核对数据）
python tests/test_integration.py
```

## 架构说明

```
浏览器 index.html
      │  fetch (POST /api/chat, /api/action)
      ▼
main.py（FastAPI :8000）
  ├─ 沙箱安全门 sandbox_check()
  ├─ ReActAgent（src/agent/react_agent.py）
  │    ├─ 系统提示词（src/agent/prompts.py）
  │    ├─ 权限路由（src/permissions/router.py：绿/黄/红分级、模糊金额检测）
  │    └─ MCP stdio 客户端
  │         ▼
  │    src/mcp_tools/server.py（16 个工具）
  │         │  requests HTTP
  │         ▼
  └─→ mock_bank.py（FastAPI :8001）
           │  读写 JSON 文件
           ▼
      mock_data/*.json（账户 / 流水 / 转账 / 理财 / 订阅）
```

详细架构与算法说明见 [docs/architecture.md](docs/architecture.md)，安全机制详见 [docs/security_report.md](docs/security_report.md)。

## 目录结构

```
├── index.html            # 前端页面（纯 HTML/CSS/JS，fetch 调用后端）
├── main.py               # Agent Web API 入口（FastAPI :8000）
├── mock_bank.py          # 模拟银行服务（FastAPI :8001，16 个 REST 接口）
├── requirements.txt
├── mock_data/            # 假数据（8 个 JSON 文件）
├── src/
│   ├── agent/            # ReActAgent 核心循环 + 系统提示词
│   ├── llm/              # 硅基流动 API 客户端
│   ├── mcp_tools/        # MCP 工具服务器（16 个 @mcp.tool）
│   ├── permissions/      # 三色权限路由 + 模糊金额检测
│   ├── audit/            # JSONL 审计日志
│   └── config.py         # 密钥加载、路径、风控阈值
├── tests/                # 测试用例（直接 python 运行）
└── logs/                 # 运行期审计日志（.gitignore 已忽略）
```

## 免责声明

本项目为竞赛演示原型，全部数据为本地 Mock，不包含任何真实金融接口。请勿用于生产环境。
