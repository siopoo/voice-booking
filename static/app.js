import { VoiceSession } from "./voice-session.mjs";
import { sendAgentMessage } from "./agent-client.mjs";
import { BackendVoiceRecorder } from "./transcription-client.mjs";
import { describeBookingFlow, mergeAgentDraft } from "./draft-state.mjs";

const state = {
  step: "service",
  config: null,
  draft: {},
  availableSlots: [],
  busy: false,
  agentConfigured: false,
  sttConfigured: false,
  apiFallbackNotified: false,
  sessionId: "",
  activeMicLabel: "",
  idempotencyKey: "",
};

const elements = {
  messages: document.querySelector("#messages"),
  quickReplies: document.querySelector("#quick-replies"),
  form: document.querySelector("#text-form"),
  input: document.querySelector("#text-input"),
  mic: document.querySelector("#mic-button"),
  voiceTitle: document.querySelector("#voice-title"),
  voiceHint: document.querySelector("#voice-hint"),
  voiceLevel: document.querySelector("#voice-level"),
  micDevice: document.querySelector("#mic-device"),
  flowStage: document.querySelector("#flow-stage"),
  sound: document.querySelector("#sound-toggle"),
  result: document.querySelector("#booking-result"),
  agentTrace: document.querySelector("#agent-trace"),
};

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let voiceSession = null;
let backendVoiceRecorder = null;

if (navigator.mediaDevices?.getUserMedia && window.MediaRecorder) {
  backendVoiceRecorder = new BackendVoiceRecorder(
    {
      mediaDevices: navigator.mediaDevices,
      MediaRecorder: window.MediaRecorder,
      fetchImpl: (url, options) => fetch(url, options),
    },
    (text) => handleUserText(text),
    {
      onState: (voiceState) => {
        const recording = voiceState === "recording";
        elements.mic.classList.toggle("listening", recording);
        elements.mic.setAttribute("aria-label", recording ? "停止录音" : "开始说话");
        if (recording) {
          elements.voiceTitle.textContent = "正在录音…";
          elements.voiceHint.textContent = state.activeMicLabel
            ? `正在使用：${state.activeMicLabel} · 停顿后自动发送`
            : "请直接说话，停顿后自动发送";
        } else if (voiceState === "transcribing") {
          elements.mic.disabled = true;
          elements.voiceTitle.textContent = "正在识别语音…";
          elements.voiceHint.textContent = "录音正在发送到后端语音识别 API";
        } else {
          elements.mic.disabled = false;
          elements.voiceLevel.style.width = "0";
          elements.voiceTitle.textContent = "点击话筒开始说话";
          elements.voiceHint.textContent = "后端语音识别已就绪";
        }
      },
      onLevel: (level) => {
        elements.voiceLevel.style.width = `${Math.min(100, Math.round(level * 700))}%`;
      },
      onDevice: (label) => {
        state.activeMicLabel = label;
      },
      onError: (error) => {
        elements.voiceTitle.textContent = "语音识别失败";
        elements.voiceHint.textContent = error?.message || "请检查麦克风和后端 STT 配置";
      },
    },
  );
}

if (SpeechRecognition) {
  recognition = new SpeechRecognition();
  recognition.lang = "zh-CN";
  recognition.interimResults = true;
  recognition.continuous = true;
  voiceSession = new VoiceSession(
    { start: () => recognition.start(), stop: () => recognition.stop() },
    (text) => handleUserText(text),
    {
      onState: (listening) => {
        elements.mic.classList.toggle("listening", listening);
        elements.mic.setAttribute("aria-label", listening ? "停止说话" : "开始说话");
        elements.voiceTitle.textContent = listening ? "正在持续聆听…" : "点击话筒开始说话";
        elements.voiceHint.textContent = listening ? "请直接说话，再次点击可停止" : "推荐使用最新版 Edge 或 Chrome";
      },
      onError: (code) => {
        if (code === "not-allowed" || code === "service-not-allowed") {
          elements.voiceHint.textContent = "麦克风权限被拒绝，请在浏览器地址栏中允许";
        } else if (code === "audio-capture") {
          elements.voiceHint.textContent = "没有检测到可用麦克风，请检查系统输入设备";
        } else if (code === "network") {
          elements.voiceTitle.textContent = "浏览器语音服务不可用";
          elements.voiceHint.textContent = "浏览器在线识别网络不可达，请配置后端 STT 或使用文字输入";
        } else if (code !== "no-speech" && code !== "aborted") {
          elements.voiceHint.textContent = `语音识别暂不可用（${code}），可使用文字输入`;
        }
      },
    },
  );
  recognition.onstart = () => {
    elements.mic.classList.add("listening");
    elements.voiceTitle.textContent = "正在持续聆听…";
    elements.voiceHint.textContent = "请直接说话，再次点击可停止";
  };
  recognition.onresult = (event) => {
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const transcript = event.results[index][0].transcript;
      if (event.results[index].isFinal) voiceSession.onResult(transcript);
      else interim += transcript;
    }
    if (interim) elements.voiceHint.textContent = `识别中：${interim}`;
  };
  recognition.onerror = (event) => voiceSession.onError(event.error);
  recognition.onend = () => voiceSession.onEnd();
} else {
  elements.voiceTitle.textContent = "当前浏览器不支持语音识别";
  elements.voiceHint.textContent = "正在检查后端语音识别配置…";
}

function addMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  const speaker = role === "ai" ? "PawPilot AI" : "访客";
  node.innerHTML = `<span class="speaker">${speaker}</span>${escapeHtml(text)}`;
  elements.messages.appendChild(node);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function speak(text) {
  if (!elements.sound.checked || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text.replace(/[￥¥]/g, "元"));
  utterance.lang = "zh-CN";
  utterance.rate = 1.02;
  window.speechSynthesis.speak(utterance);
}

function reply(text, quickReplies = []) {
  addMessage("ai", text);
  speak(text);
  showQuickReplies(quickReplies);
}

function showQuickReplies(items) {
  elements.quickReplies.innerHTML = "";
  items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.label || item;
    button.addEventListener("click", () => handleUserText(item.value || item));
    elements.quickReplies.appendChild(button);
  });
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function normalize(text) {
  return text.trim().replace(/[，。！？,.!]/g, "");
}

function parseService(text) {
  const exact = state.config.services.find((service) => text.includes(service.name));
  if (exact) return exact;
  if (/基础|洗护|洗澡/.test(text)) return state.config.services.find((s) => s.id === "basic");
  if (/美容|造型|修剪/.test(text)) return state.config.services.find((s) => s.id === "grooming");
  if (/护理|SPA|spa|药浴/.test(text)) return state.config.services.find((s) => s.id === "spa");
  return null;
}

function parsePet(text) {
  const type = /猫|喵/.test(text) ? "猫" : /狗|犬|汪/.test(text) ? "狗" : "宠物";
  const nameMatch = text.match(/(?:叫|名字是|名叫)([\u4e00-\u9fa5A-Za-z0-9]{1,10})/);
  const cleaned = text.replace(/我家|一只|小猫|猫咪|猫|小狗|狗狗|狗|宠物|它|叫|名字是|名叫|的/g, "").trim();
  return { pet_type: type, pet_name: nameMatch?.[1] || cleaned || "宝贝" };
}

function parseDate(text) {
  const today = new Date(`${state.config.today}T12:00:00`);
  let target = new Date(today);
  if (text.includes("今天")) return state.config.today;
  if (text.includes("明天")) target.setDate(target.getDate() + 1);
  else if (text.includes("后天")) target.setDate(target.getDate() + 2);
  else {
    const iso = text.match(/(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})/);
    const md = text.match(/(\d{1,2})月(\d{1,2})[日号]?/);
    if (iso) return `${iso[1]}-${iso[2].padStart(2, "0")}-${iso[3].padStart(2, "0")}`;
    if (md) return `${today.getFullYear()}-${md[1].padStart(2, "0")}-${md[2].padStart(2, "0")}`;
    const weekdayNames = ["日", "一", "二", "三", "四", "五", "六"];
    const weekday = text.match(/(?:周|星期)([一二三四五六日天])/);
    if (!weekday) return null;
    const desired = weekdayNames.indexOf(weekday[1] === "天" ? "日" : weekday[1]);
    let delta = (desired - today.getDay() + 7) % 7;
    if (delta === 0 && !text.includes("本周")) delta = 7;
    target.setDate(target.getDate() + delta);
  }
  return target.toISOString().slice(0, 10);
}

function parseTime(text) {
  const normalized = text.replace(/点半/, ":30").replace(/点/, ":00");
  const match = normalized.match(/(\d{1,2})(?::|：)(\d{2})/);
  if (!match) return null;
  let hour = Number(match[1]);
  if (/下午|晚上/.test(text) && hour < 12) hour += 12;
  return `${String(hour).padStart(2, "0")}:${match[2]}`;
}

function formatDate(day) {
  const date = new Date(`${day}T12:00:00`);
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

function updateSummary() {
  const service = state.draft.service;
  document.querySelector('[data-key="service"]').textContent = service ? `${service.name} · ¥${service.price}` : "待确认";
  document.querySelector('[data-key="pet"]').textContent = state.draft.pet_name
    ? `${state.draft.pet_name}${state.draft.pet_type ? `（${state.draft.pet_type}）` : ""}`
    : "待确认";
  document.querySelector('[data-key="date"]').textContent = state.draft.appointment_date ? formatDate(state.draft.appointment_date) : "待确认";
  document.querySelector('[data-key="time"]').textContent = state.draft.appointment_time || "待确认";
  document.querySelector('[data-key="customer"]').textContent = state.draft.customer_name || "待确认";
  document.querySelector('[data-key="phone"]').textContent = state.draft.phone || "待确认";
}

async function handleUserText(rawText) {
  if (state.busy || !rawText.trim()) return;
  const text = normalize(rawText);
  addMessage("user", rawText.trim());
  elements.input.value = "";
  showQuickReplies([]);

  if (state.agentConfigured) {
    await handleRealAgentTurn(rawText.trim());
    return;
  }

  const apiIntent = await interpretViaApi(rawText, state.step);

  if (state.step === "done") {
    if (/重新|再约|新的/.test(text)) return resetConversation();
    reply("当前预约已经完成。如需新增预约，请点击“重新开始”。", ["重新开始"]);
    return;
  }

  if (state.step === "service") {
    const service = state.config.services.find((item) => item.id === apiIntent?.value) || parseService(text);
    if (!service) {
      reply("我还不能确定服务项目。您可以选择基础洗护、精致美容或深度护理。", serviceReplies());
      return;
    }
    state.draft.service = service;
    state.step = "pet";
    updateSummary();
    reply(`好的，${service.name}是${service.price}元。请问您的宠物是猫咪还是狗狗，它叫什么名字？`, ["狗狗叫可乐", "猫咪叫布丁"]);
    return;
  }

  if (state.step === "pet") {
    const pet = parsePet(text);
    state.draft = { ...state.draft, ...pet };
    state.step = "date";
    updateSummary();
    reply(`收到，${pet.pet_name}是一只${pet.pet_type}。您希望预约哪一天？我们周一休息，可以预约未来两周。`, dateReplies());
    return;
  }

  if (state.step === "date") {
    const day = typeof apiIntent?.value === "string" ? apiIntent.value : parseDate(text);
    if (!day || day < state.config.today || day > state.config.maxDate) {
      reply("抱歉，我没有识别到有效日期。请选择未来两周内的一天。", dateReplies());
      return;
    }
    state.busy = true;
    const response = await fetch(`/api/slots?date=${encodeURIComponent(day)}&service_id=${encodeURIComponent(state.draft.service.id)}`);
    const data = await response.json();
    state.busy = false;
    if (!data.slots.length) {
      reply(`${formatDate(day)}休息或已经约满了，请换一天。`, dateReplies());
      return;
    }
    state.draft.appointment_date = day;
    state.availableSlots = data.slots;
    state.step = "time";
    updateSummary();
    reply(`${formatDate(day)}还有这些时间：${data.slots.join("、")}。您选几点？`, data.slots);
    return;
  }

  if (state.step === "time") {
    const time = typeof apiIntent?.value === "string" ? apiIntent.value : parseTime(text);
    if (!time || !state.availableSlots.includes(time)) {
      reply(`这个时间暂时不可约。当前可选：${state.availableSlots.join("、")}。`, state.availableSlots);
      return;
    }
    state.draft.appointment_time = time;
    state.step = "customer";
    updateSummary();
    reply("时间已为您暂选。请问怎么称呼您？", ["我姓陈", "我叫小林"]);
    return;
  }

  if (state.step === "customer") {
    const name = text.replace(/我姓|我叫|叫我|女士|先生/g, "").trim();
    if (!name) {
      reply("请告诉我您的称呼，例如“我姓陈”。");
      return;
    }
    state.draft.customer_name = name;
    state.step = "phone";
    updateSummary();
    reply(`${name}您好，请留下可以接收预约确认的手机号码。`);
    return;
  }

  if (state.step === "phone") {
    const phone = text.replace(/[^0-9]/g, "");
    if (!/^1\d{10}$/.test(phone)) {
      reply("号码似乎不完整，请提供11位手机号码。您也可以直接在输入框中填写。 ");
      return;
    }
    state.draft.phone = phone;
    state.step = "confirm";
    updateSummary();
    const d = state.draft;
    reply(`请确认：为${d.pet_name}预约${formatDate(d.appointment_date)} ${d.appointment_time}的${d.service.name}，联系人${d.customer_name}，电话${d.phone}。确认提交吗？`, ["确认预约", "重新开始"]);
    return;
  }

  if (state.step === "confirm") {
    if (apiIntent?.value === "restart" || /重新|取消|不对|修改/.test(text)) return resetConversation();
    if (apiIntent?.value !== "yes" && !/确认|可以|好的|是|提交/.test(text)) {
      reply("请说“确认预约”提交，或说“重新开始”修改信息。", ["确认预约", "重新开始"]);
      return;
    }
    await submitBooking();
  }
}

async function handleRealAgentTurn(text) {
  state.busy = true;
  elements.voiceTitle.textContent = "Agent 正在思考并调用业务工具…";
  try {
    const result = await sendAgentMessage(fetch, state.sessionId, text);
    state.draft = mergeAgentDraft(state.draft, result.draft, state.config.services);
    elements.flowStage.textContent = describeBookingFlow(result.flow);
    elements.flowStage.dataset.stage = result.flow?.stage || "collecting";
    updateSummary();
    renderToolTrace(result.tool_calls, result.latency_ms);
    reply(result.reply, []);
    if (result.tool_calls.some((call) => ["create_booking", "reschedule_booking", "cancel_booking"].includes(call.name))) {
      await loadBookings();
    }
  } catch (error) {
    reply(`Agent 暂时无法处理：${error.message}`);
  } finally {
    state.busy = false;
    elements.voiceTitle.textContent = "点击话筒开始说话";
  }
}

function renderToolTrace(toolCalls, latencyMs) {
  const labels = {
    get_business_profile: "读取门店事实",
    get_services: "读取服务与价格",
    check_availability: "查询真实可用时段",
    update_booking_draft: "更新结构化预约草稿",
    create_booking: "写入确认预约",
    find_bookings: "核对已有预约",
    reschedule_booking: "执行确认改期",
    cancel_booking: "执行确认取消",
  };
  const summary = document.createElement("div");
  summary.className = "trace-summary";
  summary.textContent = `Agent 完成 · ${latencyMs ?? "--"} ms · 调用 ${toolCalls.length} 个工具`;
  elements.agentTrace.appendChild(summary);
  toolCalls.forEach((call, index) => {
    const item = document.createElement("div");
    item.className = "trace-item";
    let result = call.result;
    try {
      const parsed = JSON.parse(call.result);
      result = JSON.stringify(parsed, null, 2);
      if (call.name === "create_booking" && parsed.status === "confirmed") {
        const service = state.config.services.find((entry) => entry.id === parsed.service_id);
        state.draft = {
          service,
          pet_name: parsed.pet_name,
          pet_type: parsed.pet_type,
          customer_name: parsed.customer_name,
          phone: parsed.phone,
          appointment_date: parsed.appointment_date,
          appointment_time: parsed.appointment_time,
        };
        updateSummary();
        elements.result.classList.remove("hidden");
        const replayNote = parsed.idempotent_replay ? "<br>重复请求已安全复用原预约" : "";
        elements.result.innerHTML = `<strong>预约成功</strong><br>预约编号：${escapeHtml(parsed.booking_code)}${replayNote}`;
      } else if (call.name === "reschedule_booking" && parsed.status === "confirmed") {
        elements.result.classList.remove("hidden");
        elements.result.innerHTML = `<strong>改期成功</strong><br>${escapeHtml(formatDate(parsed.appointment_date))} ${escapeHtml(parsed.appointment_time)}`;
      } else if (call.name === "cancel_booking" && parsed.status === "cancelled") {
        elements.result.classList.remove("hidden");
        elements.result.innerHTML = `<strong>预约已取消</strong><br>预约编号：${escapeHtml(parsed.booking_code)}`;
      }
    } catch (error) {
      // Tool output can be plain text; display it as returned.
    }
    item.innerHTML = `
      <strong>${index + 1}. ${escapeHtml(labels[call.name] || call.name)}</strong>
      <code>${escapeHtml(result)}</code>
    `;
    elements.agentTrace.appendChild(item);
  });
  elements.agentTrace.scrollTop = elements.agentTrace.scrollHeight;
}

async function interpretViaApi(text, step) {
  if (!state.agentConfigured || !["service", "date", "time", "confirm"].includes(step)) return null;
  try {
    const response = await fetch("/api/agent/interpret", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        step,
        context: {
          services: state.config.services.map(({ id, name }) => ({ id, name })),
          today: state.config.today,
          maxDate: state.config.maxDate,
          availableSlots: state.availableSlots,
        },
      }),
    });
    if (!response.ok) throw new Error("model unavailable");
    const payload = await response.json();
    return payload.result;
  } catch (error) {
    if (!state.apiFallbackNotified) {
      state.apiFallbackNotified = true;
      addMessage("ai", "模型 API 暂不可用，已自动切换到本地预约流程。核心功能不受影响。");
    }
    return null;
  }
}

async function submitBooking() {
  state.busy = true;
  reply("正在为您核对并提交预约，请稍候…");
  const payload = {
    service_id: state.draft.service.id,
    pet_name: state.draft.pet_name,
    pet_type: state.draft.pet_type,
    customer_name: state.draft.customer_name,
    phone: state.draft.phone,
    appointment_date: state.draft.appointment_date,
    appointment_time: state.draft.appointment_time,
    notes: "AI 语音预约演示",
  };
  const response = await fetch("/api/bookings", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": state.idempotencyKey },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  state.busy = false;
  if (!response.ok) {
    state.step = "date";
    delete state.draft.appointment_date;
    delete state.draft.appointment_time;
    updateSummary();
    reply(`${data.error}。我们重新选择日期吧。`, dateReplies());
    return;
  }
  state.step = "done";
  elements.result.classList.remove("hidden");
  elements.result.innerHTML = `<strong>预约成功</strong><br>预约编号：${escapeHtml(data.booking_code)}`;
  reply(`预约成功！您的预约编号是${data.booking_code}。该记录已经写入本机预约数据库，期待见到${state.draft.pet_name}。`, ["重新开始"]);
  await loadBookings();
}

function serviceReplies() {
  return state.config.services.map((s) => ({ label: `${s.name} ¥${s.price}`, value: s.name }));
}

function dateReplies() {
  const replies = [];
  for (let offset = 1; offset <= 5; offset += 1) {
    const day = new Date(`${state.config.today}T12:00:00`);
    day.setDate(day.getDate() + offset);
    if (day.getDay() !== 1) {
      const iso = day.toISOString().slice(0, 10);
      replies.push({ label: formatDate(iso), value: iso });
    }
    if (replies.length === 3) break;
  }
  return replies;
}

function resetConversation() {
  state.sessionId = window.crypto?.randomUUID?.() || `session-${Date.now()}`;
  state.idempotencyKey = window.crypto?.randomUUID?.() || `booking-${Date.now()}-${Math.random()}`;
  state.step = "service";
  state.draft = {};
  state.availableSlots = [];
  state.busy = false;
  elements.messages.innerHTML = "";
  elements.result.classList.add("hidden");
  elements.result.innerHTML = "";
  elements.agentTrace.innerHTML = "";
  elements.flowStage.textContent = "等待开始";
  elements.flowStage.dataset.stage = "idle";
  updateSummary();
  if (state.agentConfigured) {
    reply(
      `${state.config.welcomeMessage} 现在由大模型 Agent 为您服务，门店事实与预约结果均通过业务工具核验。`,
      ["有哪些服务？", "我想预约宠物美容", "明天有空吗？"],
    );
  } else {
    reply(state.config.welcomeMessage || `您好，我是${state.config.business.name}的 AI 前台。请问想为宝贝预约哪项服务？`, serviceReplies());
  }
}

async function loadBookings() {
  const response = await fetch("/api/bookings");
  const data = await response.json();
  const container = document.querySelector("#bookings");
  if (!data.bookings.length) {
    container.innerHTML = '<p class="empty">暂无预约。完成一次对话后，预约记录会出现在这里。</p>';
    return;
  }
  container.innerHTML = data.bookings.map((booking) => `
    <article class="booking-card">
      <span class="booking-code">${escapeHtml(booking.booking_code)}</span>
      <strong>${escapeHtml(booking.pet_name)} · ${escapeHtml(booking.service_name)}</strong>
      <p>${escapeHtml(formatDate(booking.appointment_date))} ${escapeHtml(booking.appointment_time)}</p>
      <p>${escapeHtml(booking.customer_name)} · ${escapeHtml(booking.phone)}</p>
      <span class="booking-status ${escapeHtml(booking.status)}">${booking.status === "cancelled" ? "已取消" : "已确认"}</span>
    </article>
  `).join("");
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  handleUserText(elements.input.value);
});
elements.mic.addEventListener("click", async () => {
  if (state.sttConfigured && backendVoiceRecorder) {
    if (backendVoiceRecorder.isRecording) backendVoiceRecorder.stop();
    else {
      window.speechSynthesis?.cancel();
      backendVoiceRecorder.setDeviceId(elements.micDevice.value);
      await backendVoiceRecorder.start();
      await refreshMicrophones();
    }
    return;
  }
  if (!voiceSession) return;
  if (voiceSession.isListening) {
    voiceSession.stop();
    return;
  }
  try {
    if (navigator.mediaDevices?.getUserMedia) {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
    }
    voiceSession.start();
  } catch (error) {
    elements.voiceTitle.textContent = "无法启动麦克风";
    elements.voiceHint.textContent = "请在浏览器地址栏中允许麦克风权限，或改用文字输入";
  }
});
elements.micDevice.addEventListener("change", () => {
  backendVoiceRecorder?.setDeviceId(elements.micDevice.value);
});
document.querySelector("#reset-button").addEventListener("click", resetConversation);
document.querySelector("#refresh-bookings").addEventListener("click", loadBookings);

async function start() {
  const [configResponse, agentResponse, sttResponse] = await Promise.all([
    fetch("/api/config"),
    fetch("/api/agent/status"),
    fetch("/api/stt/status"),
  ]);
  state.config = await configResponse.json();
  const agentStatus = await agentResponse.json();
  const sttStatus = await sttResponse.json();
  state.agentConfigured = agentStatus.configured;
  state.sttConfigured = Boolean((sttStatus.ready ?? sttStatus.configured) && backendVoiceRecorder);
  if (state.sttConfigured) await refreshMicrophones();
  document.querySelector("#agent-mode").textContent = agentStatus.configured
    ? `真实 Agent · ${agentStatus.model}`
    : "本地可靠模式";
  document.querySelector("#business-name").textContent = state.config.business.name;
  document.querySelector("#business-hours").textContent = `${state.config.business.hours}\n${state.config.business.address}`;
  if (state.sttConfigured) {
    elements.mic.disabled = false;
    elements.voiceHint.textContent = `后端语音识别已就绪 · ${sttStatus.model}`;
  } else if (sttStatus.configured && !sttStatus.ready && voiceSession) {
    elements.voiceHint.textContent = `后端语音识别${sttStatus.state === "loading" ? "加载中" : "不可用"}，已回退浏览器语音识别`;
  } else if (!voiceSession) {
    elements.mic.disabled = true;
    elements.voiceTitle.textContent = "语音输入尚未配置";
    elements.voiceHint.textContent = "请配置后端 STT API，或使用文字输入";
  }
  resetConversation();
  loadBookings();
}

async function refreshMicrophones() {
  if (!backendVoiceRecorder) return;
  try {
    const selected = elements.micDevice.value;
    const devices = await backendVoiceRecorder.listAudioInputs();
    if (!devices.length) return;
    elements.micDevice.innerHTML = '<option value="">自动选择真实麦克风</option>';
    devices.forEach((device) => {
      const option = document.createElement("option");
      option.value = device.deviceId;
      option.textContent = device.label;
      elements.micDevice.appendChild(option);
    });
    if (devices.some((device) => device.deviceId === selected)) {
      elements.micDevice.value = selected;
    }
  } catch (error) {
    // Device labels may remain unavailable until microphone permission is granted.
  }
}

start();
