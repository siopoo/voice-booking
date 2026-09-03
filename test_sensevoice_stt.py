import unittest
from unittest.mock import patch


class SenseVoiceRuntimeTests(unittest.TestCase):
    def test_auto_device_falls_back_to_cpu_when_optional_torch_is_missing(self) -> None:
        import builtins

        import sensevoice_stt

        real_import = builtins.__import__
        calls = []

        def import_without_torch(name, *args, **kwargs):
            if name == "torch":
                raise ModuleNotFoundError("No module named 'torch'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_torch):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                sensevoice_stt.warmup_sensevoice(
                    env={"PAWPILOT_SENSEVOICE_MODEL": "demo"},
                    model_getter=lambda model, device, hub: calls.append(
                        (model, device, hub)
                    )
                    or (_ for _ in ()).throw(RuntimeError("model unavailable")),
                )

        self.assertEqual(calls, [("demo", "cpu", "ms")])

    def test_failed_warmup_is_reported_as_not_ready(self) -> None:
        import sensevoice_stt

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            sensevoice_stt.warmup_sensevoice(
                env={"PAWPILOT_SENSEVOICE_MODEL": "demo"},
                model_getter=lambda *_args: (_ for _ in ()).throw(RuntimeError("model unavailable")),
            )
        status = sensevoice_stt.sensevoice_runtime_status()
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["ready"])
        self.assertIn("model unavailable", status["error"])

    def test_warmup_loads_configured_model_before_first_recording(self) -> None:
        try:
            from sensevoice_stt import warmup_sensevoice
        except ImportError:
            def warmup_sensevoice(*_args, **_kwargs):
                return None
        calls = []

        result = warmup_sensevoice(
            {
                "PAWPILOT_SENSEVOICE_MODEL": "FunAudioLLM/SenseVoiceSmall",
                "PAWPILOT_SENSEVOICE_HUB": "hf",
                "PAWPILOT_SENSEVOICE_DEVICE": "cuda:0",
            },
            model_getter=lambda model, device, hub: calls.append((model, device, hub)) or object(),
        )

        self.assertEqual(result, {"ready": True, "model": "FunAudioLLM/SenseVoiceSmall", "device": "cuda:0"})
        self.assertEqual(calls, [("FunAudioLLM/SenseVoiceSmall", "cuda:0", "hf")])

    def test_audio_bytes_are_decoded_and_transcribed_with_local_model(self) -> None:
        try:
            from sensevoice_stt import transcribe_sensevoice
        except ModuleNotFoundError:
            def transcribe_sensevoice(*_args, **_kwargs):
                return None

        calls = []
        model_calls = []

        class FakeModel:
            def generate(self, **kwargs):
                calls.append(kwargs)
                return [{"text": "<|zh|><|NEUTRAL|><|Speech|>我想预约基础洗护"}]

        result = transcribe_sensevoice(
            b"webm-audio",
            "audio/webm",
            env={
                "PAWPILOT_SENSEVOICE_MODEL": "FunAudioLLM/SenseVoiceSmall",
                "PAWPILOT_SENSEVOICE_DEVICE": "cuda:0",
                "PAWPILOT_SENSEVOICE_LANGUAGE": "zh",
                "PAWPILOT_SENSEVOICE_HUB": "hf",
            },
            decoder=lambda audio, content_type: [0.0, 0.2, -0.1],
            model_getter=lambda model, device, hub: model_calls.append((model, device, hub)) or FakeModel(),
            postprocessor=lambda text: text.split(">")[-1],
        )

        self.assertEqual(result, "我想预约基础洗护")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"], [0.0, 0.2, -0.1])
        self.assertEqual(calls[0]["language"], "zh")
        self.assertTrue(calls[0]["use_itn"])
        self.assertEqual(model_calls, [("FunAudioLLM/SenseVoiceSmall", "cuda:0", "hf")])


if __name__ == "__main__":
    unittest.main()
