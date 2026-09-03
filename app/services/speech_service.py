from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from urllib.request import Request
from urllib.request import urlopen as default_urlopen


def agent_status(env: Mapping[str, str]) -> dict:
    key = env.get("PAWPILOT_LLM_API_KEY", "").strip()
    model = env.get("PAWPILOT_LLM_MODEL", "").strip()
    return {"configured": bool(key and model), "model": model or None}


def stt_status(env: Mapping[str, str], runtime_status=None) -> dict:
    if env.get("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        model = env.get("PAWPILOT_SENSEVOICE_MODEL", "iic/SenseVoiceSmall").strip()
        basic = {"configured": bool(model), "model": model or None}
        if runtime_status is None:
            return basic
        return {**basic, "provider": "sensevoice", **runtime_status()}
    key = env.get("PAWPILOT_STT_API_KEY", "").strip()
    model = env.get("PAWPILOT_STT_MODEL", "").strip()
    return {"configured": bool(key and model), "model": model or None}


def transcribe_audio(
    audio: bytes,
    content_type: str,
    env: Mapping[str, str],
    *,
    urlopen=default_urlopen,
    local_transcriber=None,
) -> str:
    if env.get("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        if local_transcriber is None:
            from sensevoice_stt import transcribe_sensevoice
            local_transcriber = transcribe_sensevoice
        return local_transcriber(audio, content_type, env)
    status = stt_status(env)
    if not status["configured"]:
        raise RuntimeError("后端语音识别 API 尚未配置")
    if not audio:
        raise ValueError("录音内容为空")
    mime = content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"
    extension = {
        "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
        "audio/mpeg": "mp3", "audio/wav": "wav",
    }.get(mime, "webm")
    boundary = f"----PawPilot{uuid.uuid4().hex}"
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{status['model']}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n".encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.{extension}\"\r\nContent-Type: {mime}\r\n\r\n").encode(),
        audio,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    base_url = env.get("PAWPILOT_STT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request = Request(
        f"{base_url}/audio/transcriptions", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {env['PAWPILOT_STT_API_KEY']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    text = str(payload.get("text", "")).strip()
    if not text:
        raise ValueError("语音识别服务未返回文字")
    return text


def _extract_json_object(content: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        raise ValueError("模型未返回 JSON 对象")
    value = json.loads(match.group(0))
    if not isinstance(value, dict) or "value" not in value:
        raise ValueError("模型返回缺少 value 字段")
    return value


def interpret_text(text, step, context, env, service_ids, *, urlopen=default_urlopen):
    status = agent_status(env)
    if not status["configured"]:
        raise RuntimeError("模型 API 尚未配置")
    prompt = (
        "你是宠物护理预约系统的意图解析器。只返回 JSON，不要解释。"
        "格式为 {\"value\": 任意JSON值, \"confidence\": 0到1的数字}。"
        f"service 步骤的 value 只能是 {'、'.join(service_ids)} 或 null；"
        "date 步骤返回 YYYY-MM-DD 或 null；time 步骤返回 HH:MM 或 null；"
        "confirm 步骤返回 yes、no、restart 或 null。"
    )
    body = json.dumps(
        {
            "model": status["model"], "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"step": step, "text": text, "context": context}, ensure_ascii=False)},
            ],
        }, ensure_ascii=False,
    ).encode("utf-8")
    base = env.get("PAWPILOT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request = Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {env['PAWPILOT_LLM_API_KEY']}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return _extract_json_object(payload["choices"][0]["message"]["content"])
