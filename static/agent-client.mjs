export async function sendAgentMessage(fetcher, sessionId, message) {
  const response = await fetcher("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Agent 请求失败");
  return {
    reply: String(payload.reply || ""),
    tool_calls: Array.isArray(payload.tool_calls) ? payload.tool_calls : [],
    latency_ms: Number.isFinite(payload.latency_ms) ? payload.latency_ms : null,
    draft: payload.draft && typeof payload.draft === "object" ? payload.draft : {},
    flow: payload.flow && typeof payload.flow === "object" ? payload.flow : null,
  };
}
