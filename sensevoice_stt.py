from __future__ import annotations

import io
import os
import threading
from functools import lru_cache


_inference_lock = threading.Lock()


def _resolve_device(configured_device: str) -> str:
    if configured_device and configured_device.lower() != "auto":
        return configured_device
    import torch

    return "cuda:0" if torch.cuda.is_available() else "cpu"


@lru_cache(maxsize=2)
def _get_model(model_name: str, device: str, hub: str):
    from funasr import AutoModel

    print(f"正在加载 SenseVoice：{model_name} ({device})")
    return AutoModel(
        model=model_name,
        hub=hub,
        # FunASR 1.4.x already includes SenseVoice, so remote code execution and
        # the model repository's legacy auto-install hook are unnecessary.
        trust_remote_code=False,
        device=device,
        disable_update=True,
    )


def _decode_audio(audio: bytes, _content_type: str):
    import av
    import numpy as np

    chunks = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
        if audio_stream is None:
            raise ValueError("录音中没有音频轨道")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio_stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("录音中没有可识别的音频")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def warmup_sensevoice(env: dict[str, str] | None = None, model_getter=None) -> dict:
    """Load SenseVoice into memory before the first browser recording arrives."""
    source = os.environ if env is None else env
    model_name = source.get("PAWPILOT_SENSEVOICE_MODEL", "iic/SenseVoiceSmall").strip()
    hub = source.get("PAWPILOT_SENSEVOICE_HUB", "ms").strip() or "ms"
    device = _resolve_device(source.get("PAWPILOT_SENSEVOICE_DEVICE", "auto").strip())
    getter = _get_model if model_getter is None else model_getter
    getter(model_name, device, hub)
    return {"ready": True, "model": model_name, "device": device}


def transcribe_sensevoice(
    audio: bytes,
    content_type: str,
    env: dict[str, str] | None = None,
    decoder=None,
    model_getter=None,
    postprocessor=None,
) -> str:
    source = os.environ if env is None else env
    model_name = source.get("PAWPILOT_SENSEVOICE_MODEL", "iic/SenseVoiceSmall").strip()
    hub = source.get("PAWPILOT_SENSEVOICE_HUB", "ms").strip() or "ms"
    device = _resolve_device(source.get("PAWPILOT_SENSEVOICE_DEVICE", "auto").strip())
    language = source.get("PAWPILOT_SENSEVOICE_LANGUAGE", "zh").strip() or "zh"
    decoder = _decode_audio if decoder is None else decoder
    model_getter = _get_model if model_getter is None else model_getter
    if postprocessor is None:
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        postprocessor = rich_transcription_postprocess

    waveform = decoder(audio, content_type)
    model = model_getter(model_name, device, hub)
    with _inference_lock:
        result = model.generate(
            input=waveform,
            cache={},
            language=language,
            use_itn=True,
            batch_size=1,
        )
    if not result or not isinstance(result[0], dict):
        raise ValueError("SenseVoice 未返回识别结果")
    text = postprocessor(str(result[0].get("text", ""))).strip()
    if not text:
        raise ValueError("SenseVoice 没有识别到文字")
    return text
