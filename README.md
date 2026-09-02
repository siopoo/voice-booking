# PawPilot AI语音预约系统

用于 AI 开发工程师考核的可运行 Demo，参考 CallPilot 类型产品的业务路径。项目展示两条闭环：客户从申请、审核、配置到 Demo 激活；访客从语音/文字咨询到预约创建、查询、改期和取消。

> 当前是本地单门店演示系统。没有实现真实电话号码、短信发送、支付或医疗建议，“Demo 已激活”不等于电话渠道已开通。

## 核心设计

```text
浏览器麦克风/文字
  ├─ SenseVoice（可选）→ 失败回退浏览器 SpeechRecognition/文字
  └─ 文本
       ↓
LangChain Agent（理解、决策、自然回复）
       ↓ LangGraph 预约阶段 + 8 个业务工具
配置事实 ─→ 统一预约校验 ─→ SQLite
       ↑        手机/时区/日期/时长/并发/幂等
前端草稿、工具轨迹、耗时、预约状态
```

大模型不能直接决定价格、可用时段或数据库结果。门店事实来自 `config/business.json`，所有写入最终经过后端规则和数据库约束。模型未配置时，本地可靠流程仍能完成基础预约。

## 客户开通与交付

`客户申请与三项授权 → 公开信息整理 → 人工审核 → 门店配置 → 文字/语音测试 → 客户验收 → Demo激活 → 后续商业电话交付`

- 页面：`http://127.0.0.1:8000/delivery.html`
- 文档：[客户开通与交付流程](docs/客户开通与交付流程.md)
- 考核材料：[项目讲解稿](docs/项目讲解稿.md) · [现场演示脚本](docs/现场演示脚本.md) · [常见问题与答案](docs/常见问题与答案.md)

申请、采集结果、状态、配置草稿和验收清单保存在 SQLite；激活会更新当前门店配置。外部网站不可达时使用稳定演示数据或人工录入回退。

## AI 预约业务流程

工具：`get_business_profile`、`get_services`、`update_booking_draft`、`check_availability`、`create_booking`、`find_bookings`、`reschedule_booking`、`cancel_booking`。

系统收集服务、宠物名称/类型、日期、时间、联系人和 11 位大陆手机号。信息齐全后完整复述，只有明确确认才创建。后端统一验证 Asia/Shanghai 当地时间、当天至配置窗口、休息日、当天过期时段、允许时段、服务结束不晚于关门、占用冲突。创建与改期共用这些规则；取消后释放时段。

后端也提供可选 REST 接口：`POST /api/bookings`、`POST /api/bookings/query`、`POST /api/bookings/reschedule`、`POST /api/bookings/cancel`，方便不经过页面的系统集成和验收。

`Idempotency-Key` 在一次提交过程中复用，服务器保存不可变响应快照。新会话产生新键，因此取消后可合法重约相同信息。

## 目录

```text
voice-booking/
├─ server.py                 # HTTP API、预约规则、SQLite、开通流程
├─ booking_agent.py          # LangChain Agent 与工具
├─ booking_workflow.py       # LangGraph 确定性流程状态
├─ business_config.py        # 配置校验、版本与安全回退
├─ sensevoice_stt.py         # 本地 STT 与真实运行状态
├─ config/business.json      # 当前有效门店配置
├─ static/                   # 预约主页、交付页、原生 JS/CSS
├─ docs/                     # 业务与考核材料
├─ test_*.py / *_test.mjs    # Python 与 Node 测试
└─ .env.example              # API 配置模板
```

## Windows 安装与启动

请先进入实际克隆目录；新克隆项目**不会预先存在 `.venv`**。

```powershell
cd voice-booking
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python server.py
```

打开预约主页 `http://127.0.0.1:8000/` 或客户开通页 `http://127.0.0.1:8000/delivery.html`。终端按 `Ctrl+C` 停止。

不创建 `.env`、不配置 API Key 也可启动，页面显示“本地可靠模式”，使用文字或浏览器语音完成预约。

## 配置 DeepSeek / OpenAI 兼容模型

```powershell
Copy-Item .env.example .env
notepad .env
```

```dotenv
PAWPILOT_LLM_API_KEY=替换为真实密钥
PAWPILOT_LLM_MODEL=deepseek-chat
PAWPILOT_LLM_BASE_URL=https://api.deepseek.com
PAWPILOT_LLM_TIMEOUT=45
PAWPILOT_LLM_MAX_RETRIES=3
```

保存后重启。`.env` 已被 Git 忽略；不要把密钥粘贴到代码、截图或提交记录。

## 配置语音识别

### 本地 SenseVoice

先按显卡环境安装匹配的 PyTorch/torchaudio，再执行：

```powershell
pip install -r requirements-sensevoice.txt
```

```dotenv
PAWPILOT_STT_PROVIDER=sensevoice
PAWPILOT_SENSEVOICE_MODEL=iic/SenseVoiceSmall
PAWPILOT_SENSEVOICE_HUB=ms
PAWPILOT_SENSEVOICE_DEVICE=auto
PAWPILOT_SENSEVOICE_LANGUAGE=zh
```

模型首次下载可能较慢。`GET /api/stt/status` 对 SenseVoice 返回 `loading/ready/error` 的真实状态；失败时页面回退浏览器识别或文字。

### OpenAI 兼容 STT

```dotenv
PAWPILOT_STT_PROVIDER=api
PAWPILOT_STT_API_KEY=替换为独立语音密钥
PAWPILOT_STT_MODEL=whisper-1
PAWPILOT_STT_BASE_URL=https://api.openai.com/v1
```

LLM Key 与 STT Key 分离；DeepSeek Chat API 本身不等于语音转文字 API。

## 门店配置

直接编辑 `config/business.json`，或通过交付页审核并激活。配置包含名称、类型、地址、时区、营业时间、休息日、预约窗口、服务、价格、时长、可选时段、语言和欢迎语。读取失败会输出错误并使用安全默认配置；配置版本变化后 Agent 自动重建。

## 测试

```powershell
python -m unittest -v test_booking_agent.py test_server.py test_sensevoice_stt.py test_business_config.py test_booking_rules.py test_onboarding.py
node agent_client_test.mjs
node draft_state_test.mjs
node transcription_client_test.mjs
node voice_activity_test.mjs
node voice_session_test.mjs
```

覆盖配置、Agent 工具、API、开通状态、授权、手机号、日期窗口、休息日、过期时段、服务越界、并发抢占、幂等快照、取消释放、脱敏和 STT 失败状态。

## 完整现场演示

1. 在 `/delivery.html` 提交预填申请，展示三项授权。
2. 依次资料采集、人工审核，修改服务价格或欢迎语。
3. 生成配置、进入测试、勾选七项验收、激活 Demo。
4. 打开 `/`，确认门店和服务同步。
5. 询价 → 选服务/日期 → 查询真实时段 → 填联系人手机号。
6. 确认前修改时间，观察草稿与工具轨迹更新，再明确确认。
7. 查看预约编号、脱敏手机号和状态；演示冲突、查询、改期、取消与释放。

## 常见错误

- **网页空白/旧 UI**：确认在仓库目录运行 `python server.py`，按 `Ctrl+F5`。
- **端口 8000 占用**：停止旧 `server.py` 进程后重启。
- **Agent Connection error**：检查 Base URL、模型名、密钥和网络；本地模式不受影响。
- **SenseVoice不可用**：查看 `/api/stt/status` 和终端错误，核对依赖、模型仓库与设备；先用浏览器语音/文字。
- **浏览器 network 错误**：浏览器在线识别网络不可达，改用 SenseVoice 或文字。
- **日期无时段**：可能是休息日、超过窗口、时段已过/占用或服务会超过关门时间。

## 隐私与功能边界

- `.env`、数据库、模型、虚拟环境和日志不提交；运营列表默认手机号脱敏。
- Demo 数据仅保存在本机 SQLite；正式系统需增加权限、加密、保留期限、删除流程和审计。
- 已实现：配置驱动、开通演示、文本/可选语音、Agent 工具、创建/查询/改期/取消、并发和幂等。
- 未实现：真实电话线路、短信、支付、CRM/POS、多租户、企业认证、生产监控与人工坐席。这些不影响当前考核闭环，上线前必须建设。
