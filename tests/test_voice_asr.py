import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from customer_service.handler import CSHandler


class VoiceAsrTests(unittest.TestCase):
    def _handler(self, **config):
        handler = object.__new__(CSHandler)
        handler.model_config = config
        return handler

    def test_api_provider_falls_back_to_local(self):
        handler = self._handler(
            voice_asr_provider="api",
            voice_asr_fallback_local=True,
        )
        handler._transcribe_audio_api = Mock(return_value=None)
        handler._transcribe_audio_local = Mock(return_value="本地结果")

        result = handler._transcribe_audio("voice.amr")

        self.assertEqual(result, "本地结果")
        handler._transcribe_audio_api.assert_called_once_with("voice.amr")
        handler._transcribe_audio_local.assert_called_once_with("voice.amr")

    @patch("customer_service.handler.requests.post")
    def test_api_transcription_reuses_generic_api_config(self, post):
        handler = self._handler(
            api_base="https://example.test/v1",
            api_key="test-key",
            voice_asr_provider="api",
            voice_asr_model="gpt-4o-transcribe",
            voice_asr_language="zh",
            voice_asr_timeout=30,
        )
        response = Mock(status_code=200)
        response.json.return_value = {"text": "识别成功"}
        post.return_value = response

        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        handler._convert_audio_for_api = Mock(return_value=(path, False))
        try:
            result = handler._transcribe_audio_api(path)
        finally:
            os.remove(path)

        self.assertEqual(result, "识别成功")
        call = post.call_args
        self.assertEqual(call.args[0], "https://example.test/v1/audio/transcriptions")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call.kwargs["data"]["model"], "gpt-4o-transcribe")
        self.assertEqual(call.kwargs["data"]["language"], "zh")
        self.assertEqual(call.kwargs["timeout"], (10, 30))


if __name__ == "__main__":
    unittest.main()
