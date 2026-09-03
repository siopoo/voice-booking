# 架构设计

## 1. 目标与约束

系统目标是可靠完成预约，而不是让模型自由生成一个看似合理的答案。设计遵循三条约束：业务事实来自配置和数据库；写操作必须经过确定性规则；没有模型和语音服务时仍能完整演示。

## 2. 分层与依赖方向

```text
app/api          HTTP、序列化、状态码、安全错误
    |
app/agents       LangChain 交互 + LangGraph 控制流
    |
app/services     业务规则、确认门槛、事务语义
    |
app/repositories SQL 与数据映射
    |
app/db           连接、schema、迁移

app/core         Settings、类型化异常、日志（横切能力）
```

依赖只向下。`server.py` 是 composition root，同时保留旧函数名作为兼容 facade，因此前端、旧测试和已有调用方不用同时迁移。

## 3. LangGraph 控制面

`BookingAgentState` 保存消息、会话 ID、意图、预约草稿、缺失字段、真实可用时段、选中时段、确认状态、写入结果、阶段和错误。节点返回状态 patch，条件边决定下一步。

关键安全规则：

1. 缺字段时只进入 `ask_for_missing_info`，不查时段、不写库。
2. 信息齐全后必须经过 `check_availability`。
3. 选中时段不在工具结果中时进入 `suggest_alternatives`。
4. 只有白名单明确确认短语把状态置为 `confirmed`；“好吧”“听起来可以”等保持 `pending`。
5. `create_booking` 再次检查确认状态，并把 Service 异常写入可审计状态。
6. 已存在 `booking_result` 时直接到 `completed`，避免重复确认触发第二次写入。

## 4. 状态失效规则

- `appointment_date` 或 `appointment_time` 改变：清空 `availability`、`selected_slot` 和确认状态。
- `service_id` 改变：额外清空 `service_price`、`service_duration`，防止旧服务派生事实污染新服务。
- 改期先查新时段，再明确确认，再调用 Service。

这解决了 Agent 应用里常见的“模型记住旧值、部分修改后误提交”问题。

## 5. 事务、并发与幂等

Booking Service 做格式、预约窗口、休息日、营业时间和服务结束时间校验。Repository 执行 SQL：

- `appointments(appointment_date, appointment_time) WHERE status='confirmed'` 条件唯一索引，让并发抢同一时段只有一个成功。
- `idempotency_records` 保存首次响应快照；相同幂等键重放返回相同预约，不读取修改后的记录。
- 取消把状态改成 `cancelled`，条件唯一索引自动释放时段。

## 6. 模型与工具边界

LangChain 模型负责自然语言理解和友好回复。LLM 不是可信授权边界，模型生成的 Tool Call 参数一律视为不可信输入。模型只绑定读取事实、更新草稿、查询时段和查找预约的工具；创建、改期、取消没有暴露给 LLM。

所有数据库变更都由确定性的 LangGraph 状态迁移授权，并由对应 Graph mutation node 调用现有 Booking Service。即使模型伪造 `customer_confirmed=True` 或伪造写工具结果，也不能进入数据库写路径。

可靠模式不调用模型；真实 Agent 模式使用 OpenAI-compatible Chat API。两种模式共享同一个 SQLite、配置和业务 Service。

### Safety Invariants

1. 没有基于最后一条真实用户消息得出的明确确认，数据库不得写入。
2. LLM 无法直接调用 create、reschedule 或 cancel 写操作。
3. 日期、时间或服务变化会使旧可用性、选中时段和确认状态失效。
4. 重复确认不会创建重复预约。
5. 数据库唯一约束始终是时段冲突的最终裁决者。

写操作采用四层保护：conditional edge、mutation node guard、Service validation、database constraint。

## 7. 可观测性与错误模型

每个 HTTP 请求生成或透传 `request_id`。结构化日志记录 `request_id`、`thread_id`、工具名、耗时、状态机阶段和预约编号。手机号统一遮蔽为 `138****8000`，Authorization、API Key、Token 等字段变为 `***`。

预期业务失败使用 `BookingValidationError`、`BookingConflictError`、`OnboardingValidationError` 等类型；HTTP 层把它们映射为 400/409。未知异常只记录异常类型，对客户端返回通用消息。

## 8. 测试策略

- 单元测试：配置、失效规则、Service、Repository、工具、语音适配器。
- 集成测试：启动真实 HTTP server，跑健康检查、新建、查询、改期、取消。
- Agent Eval：30 条确定性场景，不使用真实模型；输出会话与节点 trace。
- 前端测试：草稿状态、Agent 客户端、转写、VAD 和语音会话。
- CI：Python 3.11 lint/test/eval、Node 20 前端测试、Docker build。

## 9. 扩展方向

生产化可以把 SQLite 换为 PostgreSQL，把内存状态换成持久化 LangGraph checkpointer，把标准库 HTTP 换成 FastAPI，并补充 OAuth/RBAC、速率限制、OpenTelemetry、队列和人工接管。由于上层只依赖 Service/Repository 接口，这些替换不需要重写状态机。
