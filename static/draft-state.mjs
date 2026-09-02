const FIELD_LABELS = {
  service_id: "服务",
  pet_name: "宠物名字",
  pet_type: "宠物类型",
  appointment_date: "日期",
  appointment_time: "时间",
  customer_name: "联系人",
  phone: "电话",
};

export function mergeAgentDraft(current, incoming, services) {
  const next = { ...current };
  Object.entries(incoming || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") next[key] = value;
  });
  if (incoming?.service_id) {
    next.service = services.find((item) => item.id === incoming.service_id)
      || { id: incoming.service_id, name: incoming.service_name || incoming.service_id };
  }
  delete next.service_id;
  delete next.service_name;
  return next;
}

export function describeBookingFlow(flow) {
  if (!flow) return "等待开始";
  if (flow.stage === "booked") return "预约已创建";
  if (flow.stage === "cancelled") return "预约已取消";
  if (flow.stage === "awaiting_confirmation") return "信息齐全 · 等待客户确认";
  const missing = (flow.missing_fields || []).map((field) => FIELD_LABELS[field] || field);
  return missing.length ? `收集中 · 还缺${missing.join("、")}` : "正在收集预约信息";
}
