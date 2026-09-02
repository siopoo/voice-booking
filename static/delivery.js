const statusOrder = ["submitted", "collecting", "awaiting_review", "config_generated", "testing", "accepted", "activated"];
const checklistLabels = {
  business_profile_confirmed: "门店资料已确认",
  services_confirmed: "服务和价格已确认",
  hours_confirmed: "营业时间已确认",
  agent_config_generated: "Agent 配置已生成",
  text_test_passed: "文字预约测试通过",
  voice_test_passed: "语音预约测试通过",
  customer_accepted: "客户验收通过",
};
let current = null;
let baseConfig = null;

const form = document.querySelector("#application-form");
const reviewView = document.querySelector("#review-view");
const editor = document.querySelector("#config-editor");
const collectButton = document.querySelector("#collect-button");
const reviewButton = document.querySelector("#review-button");
const configButton = document.querySelector("#config-button");
const testButton = document.querySelector("#test-button");
const acceptButton = document.querySelector("#accept-button");
const activateButton = document.querySelector("#activate-button");

function escapeHtml(value) { const node = document.createElement("div"); node.textContent = String(value ?? ""); return node.innerHTML; }
function toast(message, isError = false) { const node = document.querySelector("#toast"); node.textContent = message; node.className = `toast ${isError ? "error" : ""}`; setTimeout(() => node.classList.add("hidden"), 3500); }
async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

function render() {
  if (!current) return;
  document.querySelectorAll("#stage-rail li").forEach((node) => {
    node.classList.toggle("complete", statusOrder.indexOf(node.dataset.status) <= statusOrder.indexOf(current.status));
    node.classList.toggle("active", node.dataset.status === current.status);
  });
  reviewView.classList.remove("empty-state");
  reviewView.innerHTML = `<dl><div><dt>申请编号</dt><dd>${escapeHtml(current.application_code)}</dd></div><div><dt>当前状态</dt><dd>${escapeHtml(current.status)}</dd></div><div><dt>企业</dt><dd>${escapeHtml(current.application.business_name)}</dd></div><div><dt>公开信息</dt><dd>${current.collected.summary ? "已整理（支持人工回退）" : "待整理"}</dd></div><div><dt>人工审核</dt><dd>${statusOrder.indexOf(current.status) >= 2 ? "已通过" : "待审核"}</dd></div></dl>`;
  collectButton.disabled = current.status !== "submitted";
  reviewButton.disabled = current.status !== "collecting";
  editor.disabled = !["awaiting_review", "config_generated"].includes(current.status);
  configButton.disabled = editor.disabled;
  testButton.disabled = current.status !== "config_generated";
  acceptButton.disabled = current.status !== "testing";
  activateButton.disabled = current.status !== "accepted";
  if (current.status === "activated") document.querySelector("#demo-link").classList.remove("hidden");
}

function configForApplication() {
  const value = structuredClone(baseConfig);
  delete value.config_version;
  value.business_name = current.application.business_name;
  value.business_type = current.application.business_type;
  value.agent_language = current.application.preferred_language;
  value.welcome_message = `您好，我是${value.business_name}的 AI 前台，请问想为您的宠物预约什么服务？`;
  return value;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  ["website_authorized", "contact_authorized", "representative_confirmed"].forEach((key) => data[key] = form.elements[key].checked);
  try {
    current = await api("/api/onboarding/applications", { method: "POST", body: JSON.stringify(data) });
    editor.value = JSON.stringify(configForApplication(), null, 2);
    form.querySelectorAll("input,select,textarea,button").forEach((node) => node.disabled = true);
    render(); toast("申请已保存到 SQLite，可继续资料审核");
  } catch (error) { toast(error.message, true); }
});

collectButton.addEventListener("click", () => action("collect", {}));
reviewButton.addEventListener("click", () => action("review", {}));
configButton.addEventListener("click", async () => {
  try { current = await api(`/api/onboarding/applications/${current.id}/config`, { method: "POST", body: JSON.stringify({ config: JSON.parse(editor.value) }) }); render(); toast("门店配置已生成，等待测试验收"); }
  catch (error) { toast(`配置无效：${error.message}`, true); }
});
testButton.addEventListener("click", () => action("test", {}));
acceptButton.addEventListener("click", () => {
  const checklist = {};
  document.querySelectorAll("#checklist input").forEach((input) => checklist[input.name] = input.checked);
  action("accept", { checklist });
});
activateButton.addEventListener("click", () => action("activate", {}));

async function action(name, body) {
  try { current = await api(`/api/onboarding/applications/${current.id}/${name}`, { method: "POST", body: JSON.stringify(body) }); render(); toast(name === "activate" ? "Demo 已激活，预约主页将读取新配置" : "流程状态已更新"); }
  catch (error) { toast(error.message, true); }
}

async function start() {
  const response = await api("/api/config");
  baseConfig = {
    business_name: response.business.name, business_type: "pet_groomer", address: response.business.address,
    timezone: response.timezone, opening_time: response.schedule.openingTime, closing_time: response.schedule.closingTime, closed_weekdays: response.schedule.closedWeekdays,
    booking_window_days: response.schedule.bookingWindowDays, services: response.services, appointment_slots: response.schedule.appointmentSlots,
    agent_language: "zh", welcome_message: response.welcomeMessage,
  };
  document.querySelector("#checklist").innerHTML = Object.entries(checklistLabels).map(([key,label]) => `<label><input type="checkbox" name="${key}"> ${label}</label>`).join("");
}
start().catch((error) => toast(error.message, true));
