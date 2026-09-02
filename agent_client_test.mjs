import assert from "node:assert/strict";
import { sendAgentMessage } from "./static/agent-client.mjs";


{
  let request = null;
  const fakeFetch = async (url, options) => {
    request = { url, options };
    return {
      ok: true,
      async json() {
        return {
          reply: "10点可以预约",
          tool_calls: [{ name: "check_availability", result: '{"slots":["10:00"]}' }],
          latency_ms: 420,
          draft: { pet_name: "可乐", pet_type: "狗" },
          flow: { stage: "collecting", missing_fields: ["service_id"] },
        };
      },
    };
  };
  const result = await sendAgentMessage(fakeFetch, "session-1", "明天几点有空？");
  assert.equal(request.url, "/api/chat");
  assert.deepEqual(JSON.parse(request.options.body), {
    session_id: "session-1",
    message: "明天几点有空？",
  });
  assert.equal(result.reply, "10点可以预约");
  assert.equal(result.tool_calls[0].name, "check_availability");
  assert.equal(result.latency_ms, 420);
  assert.deepEqual(result.draft, { pet_name: "可乐", pet_type: "狗" });
  assert.deepEqual(result.flow, { stage: "collecting", missing_fields: ["service_id"] });
}

{
  const fakeFetch = async () => ({
    ok: false,
    async json() { return { error: "模型不可用" }; },
  });
  await assert.rejects(
    () => sendAgentMessage(fakeFetch, "session-2", "你好"),
    /模型不可用/,
  );
}

console.log("agent client tests passed");
