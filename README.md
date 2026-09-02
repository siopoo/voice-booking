# PawPilot LangChain 语音预约 Agent

本项目的重点是后端大模型 Agent，不是网页。网页只负责把语音转成文字、播放回复以及展示工具调用轨迹。

## 核心架构

```text
客户语音
  → 浏览器 STT
  → POST /api/chat
  → LangChain create_agent（会话记忆 + 决策）
       ├─ get_business_profile：门店事实
       ├─ get_services：服务、价格、时长
       ├─ update_booking_draft：结构化记录已确认字段
       ├─ check_availability：读取真实空闲时段
       ├─ create_booking：确认后写入预约
       ├─ find_bookings：按编号或手机号核对预约
       ├─ reschedule_booking：确认后改期
       └─ cancel_booking：确认后取消并释放时段
  → LangGraph 确定性流程评估（收集中 / 等待确认 / 已创建 / 已取消）
  → SQLite 唯一约束防止重复预约
  → Agent 自然语言回复
  → 浏览器 TTS
```

大模型负责理解表达、维护上下文和决定何时调用工具，但不能编造价格、时段或绕过数据库。业务工具及 SQLite 才是事实来源。

## 业务闭环

1. Agent 调用 `get_services` 回答服务与价格；
2. 采集服务、宠物、日期、联系人等信息；
3. 调用 `check_availability`，只提供数据库返回的时段；
4. 信息齐全后向客户完整复述；
5. 客户明确确认后才调用 `create_booking`；
6. 工具再次校验时段并写入 SQLite；
7. 如果发生并发冲突，Agent 重新查时段并继续对话；
8. `/api/chat` 返回回复和工具调用轨迹，便于演示与审计。

结构化预约草稿不依赖 Agent 的自然语言措辞。模型通过
`update_booking_draft` 提交字段，LangGraph 根据必填字段和数据库执行结果计算流程阶段；
未知服务无法进入草稿，未经明确确认的创建、改期或取消会被业务工具拒绝。

## 环境要求与安装

LangChain 1.x 需要 Python 3.10 或更高版本。当前项目已经创建 Python 3.12 的 `.venv`。

```powershell
cd voice_booking_demo
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 配置模型 API

编辑项目根目录的 `.env` 文件：

```dotenv
PAWPILOT_LLM_API_KEY=你的 API Key
PAWPILOT_LLM_MODEL=deepseek-chat
PAWPILOT_LLM_BASE_URL=https://api.deepseek.com
PAWPILOT_LLM_TIMEOUT=45
PAWPILOT_LLM_MAX_RETRIES=3
```

使用其他兼容 OpenAI Chat Completions 的服务时，修改模型名和 Base URL。例如 OpenAI：

```dotenv
PAWPILOT_LLM_API_KEY=你的 API Key
PAWPILOT_LLM_MODEL=支持工具调用的模型名称
PAWPILOT_LLM_BASE_URL=https://api.openai.com/v1
PAWPILOT_LLM_TIMEOUT=45
PAWPILOT_LLM_MAX_RETRIES=3
```

系统环境变量优先于 `.env` 中的同名设置。模型必须支持 OpenAI Chat Completions 风格的工具调用。可选启用 LangSmith：

```dotenv
LANGSMITH_API_KEY=你的 LangSmith Key
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=pawpilot-booking-agent
```

保存 `.env` 后启动或重启服务：

```powershell
.\.venv\Scripts\python.exe server.py
```

### 可选：后端语音识别

浏览器自带的 Web Speech 服务可能因浏览器或网络环境返回 `network`。如需稳定的语音演示，可在同一个 `.env` 中配置一个兼容 OpenAI `/audio/transcriptions` 的语音转文字服务：

```dotenv
PAWPILOT_STT_API_KEY=你的语音识别 API Key
PAWPILOT_STT_MODEL=whisper-1
PAWPILOT_STT_BASE_URL=https://api.openai.com/v1
```

### 本地 SenseVoice 语音识别（推荐）

项目可直接在后端运行 SenseVoiceSmall。浏览器只负责录音，音频发送到
`POST /api/transcribe` 后由本机模型转成文字，因此不依赖浏览器内置的语音识别服务。

Windows + NVIDIA GPU 安装：

```powershell
.\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -r requirements-sensevoice.txt
```

在 `.env` 中配置：

```dotenv
PAWPILOT_STT_PROVIDER=sensevoice
PAWPILOT_SENSEVOICE_MODEL=FunAudioLLM/SenseVoiceSmall
PAWPILOT_SENSEVOICE_HUB=hf
PAWPILOT_SENSEVOICE_DEVICE=cuda:0
PAWPILOT_SENSEVOICE_LANGUAGE=zh
```

服务启动后会在后台预热 SenseVoice；首次运行仍需下载模型，但网页可以先打开，
模型完成加载后首句语音无需再等待冷启动。没有 NVIDIA GPU 时，把
`PAWPILOT_SENSEVOICE_DEVICE` 改为 `cpu`。

配置后，网页使用 `MediaRecorder` 录音并发送到本项目的 `/api/transcribe`，后端转写完成后再把文字交给 LangChain Agent。DeepSeek 的对话 Key 与 STT Key 相互独立；未配置 STT 时仍可使用浏览器语音识别或文字输入。

访问 <http://127.0.0.1:8000>。顶部显示“真实 Agent · 模型名”时，页面对话会全部进入 LangChain Agent；未配置模型时显示“本地可靠模式”，仅作为无网络兜底。

## API

### Agent 对话

```http
POST /api/chat
Content-Type: application/json

{
  "session_id": "demo-call-001",
  "message": "我想给可乐预约明天下午的精致美容"
}
```

响应会同时返回自然语言回复和工具调用轨迹：

```json
{
  "reply": "明天下午还有……",
  "latency_ms": 846,
  "draft": {
    "service_id": "grooming",
    "pet_name": "可乐",
    "appointment_date": "2026-09-03"
  },
  "flow": {
    "stage": "collecting",
    "missing_fields": ["appointment_time", "customer_name", "phone"]
  },
  "tool_calls": [
    {
      "name": "check_availability",
      "result": "{\"date\":\"...\",\"slots\":[...]}"
    }
  ]
}
```

同一个 `session_id` 会通过 LangGraph `InMemorySaver` 保持通话上下文。

其他确定性业务接口：

- `GET /api/agent/status`：模型配置状态，不返回密钥；
- `GET /api/slots?date=YYYY-MM-DD`：查询真实时段；
- `POST /api/bookings`：直接创建预约，供后台集成使用；
- `GET /api/bookings`：运营侧预约列表。

`POST /api/bookings` 和 Agent 的 `create_booking` 工具均支持 `idempotency_key`。
未显式传入时，后端会按预约核心字段生成稳定指纹；相同请求重试会返回原预约，
不会重复占用时段。

## 语音交互优化

- 页面会显示当前麦克风并允许手动切换输入设备；默认会避开 VoiceMeeter 等虚拟输出设备；
- 音量条实时显示输入强度，检测到有效说话后静音约 0.9 秒会自动结束录音并提交；
- 在用户真正开口前保持静音不会误提交；
- 每轮 Agent 回复展示耗时、工具调用数量、调用顺序及工具结果，方便讲解业务决策过程。

## 推荐考核演示

不要按固定按钮逐步操作，直接用自然语言和 Agent 对话：

1. “你们有哪些服务，精致美容多少钱？”——观察 `get_services`；
2. “我家狗狗可乐想约明天下午。”——观察 `check_availability`；
3. 补充联系人和手机号；
4. Agent 复述后先说“时间改成另一个时段”——展示会话理解；
5. 最后说“确认预约”——观察 `create_booking` 和数据库记录；
6. 再开一个会话抢占同一时段——展示冲突校验。
7. 用预约编号或手机号查询，再要求改期或取消——展示二次确认和时段释放。

这套演示体现的不是模型会聊天，而是它能在业务规则约束下完成真实事务。

## 隐私说明

真实 Agent 模式会把本次对话内容发送给你配置的模型服务商，其中可能包含联系人和手机号。正式上线前应增加隐私告知、脱敏、数据保留策略与合规审查。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_booking_agent.py test_server.py
& 'C:\Users\Mqx\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' agent_client_test.mjs
& 'C:\Users\Mqx\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' voice_session_test.mjs
& 'C:\Users\Mqx\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' transcription_client_test.mjs
& 'C:\Users\Mqx\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' voice_activity_test.mjs
```

## 电话渠道升级

浏览器 STT/TTS 只是演示渠道。接入真实电话时，用 OpenAI Realtime、Twilio、Vapi 或 LiveKit 替换语音传输层即可，`create_agent`、四个业务工具和数据库逻辑可以保持不变。
