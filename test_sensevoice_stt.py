import unittest


class SenseVoiceRuntimeTests(unittest.TestCase):
    def test_warmup_loads_configured_model_before_first_recording(self) -> None:
        try:
            from sensevoice_stt import warmup_sensevoice
        except ImportError:
            warmup_sensevoice = lambda *_args, **_kwargs: None
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
            transcribe_sensevoice = lambda *_args, **_kwargs: None

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
