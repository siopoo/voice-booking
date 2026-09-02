import assert from "node:assert/strict";

let helpers = {};
try {
  helpers = await import("./static/draft-state.mjs");
} catch (_error) {
  // The first TDD run intentionally reaches the assertion below.
}

assert.equal(typeof helpers.mergeAgentDraft, "function", "需要把 Agent 草稿映射为预约单状态");
assert.equal(typeof helpers.describeBookingFlow, "function", "需要向用户解释确定性流程阶段");

const services = [{ id: "grooming", name: "精致美容", price: 168 }];
const merged = helpers.mergeAgentDraft(
  { pet_name: "可乐" },
  { service_id: "grooming", pet_type: "狗", phone: "13800138000" },
  services,
);
assert.deepEqual(merged, {
  pet_name: "可乐",
  pet_type: "狗",
  phone: "13800138000",
  service: services[0],
});

assert.equal(
  helpers.describeBookingFlow({ stage: "collecting", missing_fields: ["appointment_date", "phone"] }),
  "收集中 · 还缺日期、电话",
);
assert.equal(
  helpers.describeBookingFlow({ stage: "awaiting_confirmation", missing_fields: [] }),
  "信息齐全 · 等待客户确认",
);
assert.equal(helpers.describeBookingFlow({ stage: "booked", missing_fields: [] }), "预约已创建");
assert.equal(helpers.describeBookingFlow({ stage: "cancelled", missing_fields: [] }), "预约已取消");

console.log("draft state tests passed");
