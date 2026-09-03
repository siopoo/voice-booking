# PawPilot AI Voice Booking Agent

一个面向宠物护理门店的可运行 AI 语音预约系统。它不是只会聊天的网页 Demo：LangGraph 负责业务状态推进，Service 负责规则与事务，SQLite 保存真实预约；LLM 只承担自然语言理解与表达，不能绕过确认门槛直接决定业务事实。

## 已实现能力

- 新建预约：服务、宠物、日期、时段、联系人、手机号逐项收集。
- 查询、改期、取消：按预约编号或手机号核验，改期和取消必须明确确认。
- 真实可用时段：营业时间、休息日、服务时长、预约窗口、已占用时段共同计算。
- 写入安全：明确确认才创建；SQLite 条件唯一索引处理并发抢占；幂等键阻止重复预约。
- 双运行模式：可靠模式无需 LLM；真实 Agent 模式支持 OpenAI-compatible 模型。
- 语音入口：浏览器语音、OpenAI-compatible STT 或本地 SenseVoice；文字输入始终可用。
- 客户开通：申请、采集、审核、生成配置、测试、验收、激活完整状态流。
- 工程化：分层架构、类型化异常、结构化脱敏日志、69 个 Python 测试、30 条 Agent Eval、前端单测、Docker 和 CI。

## 架构

```text
Browser (voice/text)
        |
        v
HTTP API / static UI  ---- request_id / sanitized errors
        |
        +--> LangChain model + thin tools
        |          |
        |          v
        |    LangGraph booking workflow
        |    intent -> collect -> availability -> confirmation -> write
        |          |
        +----------+
                   v
        Booking / Onboarding / Speech Services
                   |
             Repositories
                   |
                SQLite
```

关键边界：

- `app/agents/`：统一的 `BookingAgentState`、LangGraph 节点/条件边、提示词。
- `app/services/`：预约、开通和语音业务规则，不依赖 HTTP。
- `app/repositories/`：SQLite 持久化与查询。
- `app/api/`：路由、状态码、输入输出和安全错误映射。
- `server.py`：兼容入口和依赖组装，从原约 1045 行缩减到约 256 行。

更详细的状态机、事务边界和设计取舍见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## LangGraph 状态机

`BookingAgentState` 是预约流程的单一事实源，包含：

`messages`、`thread_id`、`intent`、`booking_draft`、`missing_fields`、`availability`、`selected_slot`、`confirmation_status`、`booking_result`、`stage`、`error`。

主要节点：

```text
understand_request
  -> collect_booking_info
      -> ask_for_missing_info -> END
      -> check_availability
          -> suggest_alternatives -> END
          -> await_confirmation
              -> END (模糊/拒绝/修改)
              -> create_booking -> completed -> END (明确确认)
```

当日期或时间改变，可用时段与已选时段立即失效；服务改变时，价格、时长、可用时段与已选时段全部失效。重复确认看到已有 `booking_result` 后直接完成，不再次写库。

## 本地启动

要求 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python server.py
```

打开 <http://127.0.0.1:8000/>，运营交付页是 <http://127.0.0.1:8000/delivery.html>，健康检查是 <http://127.0.0.1:8000/api/health>。

默认可靠模式不需要 API Key，预约、查询、改期、取消和开通流程都能运行。

## 模型与语音配置

配置只放在项目根目录 `.env`，不要把真实密钥提交到 Git：

```dotenv
PAWPILOT_LLM_API_KEY=your-api-key
PAWPILOT_LLM_MODEL=deepseek-chat
PAWPILOT_LLM_BASE_URL=https://api.deepseek.com
PAWPILOT_LLM_TIMEOUT=45
PAWPILOT_LLM_MAX_RETRIES=3
```

OpenAI-compatible 语音转文字：

```dotenv
PAWPILOT_STT_PROVIDER=api
PAWPILOT_STT_API_KEY=your-stt-key
PAWPILOT_STT_MODEL=whisper-1
PAWPILOT_STT_BASE_URL=https://api.openai.com/v1
```

本地 SenseVoice：

```powershell
python -m pip install -r requirements-sensevoice.txt
```

```dotenv
PAWPILOT_STT_PROVIDER=sensevoice
PAWPILOT_SENSEVOICE_MODEL=iic/SenseVoiceSmall
PAWPILOT_SENSEVOICE_HUB=ms
PAWPILOT_SENSEVOICE_DEVICE=auto
PAWPILOT_SENSEVOICE_LANGUAGE=zh
```

可选运行配置：`PAWPILOT_HOST`、`PAWPILOT_PORT`、`PAWPILOT_DATABASE_PATH`、`PAWPILOT_BUSINESS_CONFIG_PATH`、`PAWPILOT_LOG_LEVEL`。完整示例见 `.env.example`。

## Docker

Docker Compose 不要求 LLM Key；未提供时系统自动保持可靠模式。

```bash
docker compose up --build
```

数据库持久化到 `./data`，门店配置挂载自 `./config`。停止：

```bash
docker compose down
```

## 测试与评测

```powershell
python -m pip install -r requirements-dev.txt
pytest -q
ruff check app evals server.py booking_agent.py booking_workflow.py business_config.py sensevoice_stt.py
python -m evals.run_evals
npm test
```

Agent Eval 完全离线，不调用真实模型。当前基线：

| 指标 | 结果 |
|---|---:|
| 场景数 | 30 |
| 通过 | 30 |
| 明确确认后才写入 | 100% |
| 重复写入 | 0 |
| 业务事实幻觉 | 0 |

场景覆盖意图路由、缺字段、多轮收集、模糊/明确确认、拒绝、修改、字段失效、冲突替代、重复确认和服务异常。失败报告包含逐轮对话和节点 trace。

## 核心 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 门店与服务事实 |
| GET | `/api/slots?date=&service_id=` | 查询真实可用时段 |
| POST | `/api/bookings` | 创建预约，支持 `Idempotency-Key` |
| POST | `/api/bookings/query` | 查询预约 |
| POST | `/api/bookings/reschedule` | 明确确认后改期 |
| POST | `/api/bookings/cancel` | 明确确认后取消 |
| POST | `/api/chat` | 真实 Agent 对话 |
| POST | `/api/transcribe` | 音频转文字 |
| POST/GET | `/api/onboarding/applications` | 客户开通流程 |

## 安全与可靠性

- API Key 只从环境变量读取，状态接口、日志和响应从不返回密钥。
- 日志自动遮蔽手机号与 Authorization/API Key 字段。
- HTTP 响应隐藏堆栈和内部异常，只返回可行动的安全错误。
- LLM 不提供门店事实；服务、价格、营业时间和时段均来自配置或数据库工具。
- 业务写入由状态机确认门槛、Service 校验、幂等记录和数据库唯一约束共同保护。
- CI 清空模型/STT 凭据，只运行可重复的离线测试。

## 已知限制

- 目前使用进程内 LangGraph 会话状态；多实例部署应迁移到 Redis/PostgreSQL checkpointer。
- SQLite 适合考核和单机演示；高并发生产环境应迁移到 PostgreSQL，并使用事务锁或版本号。
- SenseVoice 依赖较大，默认 Docker 镜像不包含本地模型运行时。
- 当前 HTTP 层使用 Python 标准库，生产环境可迁移到 FastAPI/ASGI 以获得 schema、认证和监控生态。

面试讲解顺序与高频追问见 [docs/INTERVIEW_GUIDE.md](docs/INTERVIEW_GUIDE.md)。
