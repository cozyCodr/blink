"""
Agent voice output proof (P5-04). Cloud TTS is injected as a fake so the whole
suite runs offline and free:

- POST /tts with a working fake client -> 200 + a non-empty base64 string.
- POST /tts with a raising fake client -> 200 + audio_base64 == null (the
  frontend then simply skips audio; the text path is unaffected).
- tts.synthesize returns the fake bytes with a working client, and raises
  TtsUnavailable when the client raises.
"""
import base64
import unittest

from fastapi.testclient import TestClient

from src.api import server
from src.agent import tts


_FAKE_MP3 = b"ID3fake-mp3-bytes\x00\x01\x02"


class _FakeResponse:
    def __init__(self, audio):
        self.audio_content = audio


class _FakeTtsClient:
    """Returns fixed fake MP3 bytes without any network call."""
    def synthesize_speech(self, *a, **k):
        return _FakeResponse(_FAKE_MP3)


class _RaisingTtsClient:
    """Forces the TtsUnavailable / graceful-degrade path."""
    def synthesize_speech(self, *a, **k):
        raise RuntimeError("no TTS creds in test")


class TestTtsGateway(unittest.TestCase):
    def tearDown(self):
        tts.set_client(None)

    def test_synthesize_returns_fake_bytes(self):
        tts.set_client(_FakeTtsClient())
        self.assertEqual(tts.synthesize("hello"), _FAKE_MP3)

    def test_synthesize_raises_when_client_raises(self):
        tts.set_client(_RaisingTtsClient())
        with self.assertRaises(tts.TtsUnavailable):
            tts.synthesize("hello")

    def test_synthesize_raises_on_empty_text(self):
        tts.set_client(_FakeTtsClient())
        with self.assertRaises(tts.TtsUnavailable):
            tts.synthesize("   ")


class TestTtsEndpoint(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        self.client = TestClient(server.app)
        self.ws = "ws_tts"

    def tearDown(self):
        tts.set_client(None)
        server.stores.clear()

    def test_tts_returns_audio_when_available(self):
        tts.set_client(_FakeTtsClient())
        r = self.client.post(f"/v1/workspaces/{self.ws}/tts", json={"text": "hello"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsInstance(body["audio_base64"], str)
        self.assertTrue(body["audio_base64"])
        self.assertEqual(base64.b64decode(body["audio_base64"]), _FAKE_MP3)
        self.assertEqual(body["mime"], "audio/mpeg")

    def test_tts_degrades_to_null_when_unavailable(self):
        tts.set_client(_RaisingTtsClient())
        r = self.client.post(f"/v1/workspaces/{self.ws}/tts", json={"text": "hello"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["audio_base64"])


"""
P12-03b — streaming synthesis.

Chirp 3 HD streams audio, so the reply can start playing while the rest is
still being made. These pin the contract the browser depends on: raw PCM
chunks in order, the sample rate on the response headers, the Charon voice and
PCM encoding actually requested, and a 503 (not a truncated body) whenever TTS
is unavailable, which is the client's cue to fall back to the whole-file /tts.
"""

_PCM_CHUNKS = [b"\x01\x02" * 128, b"\x03\x04" * 128, b"\x05\x06" * 64]


class _FakeStreamingTtsClient:
    """Streams fixed PCM chunks and keeps the requests it was handed."""

    def __init__(self, chunks=None):
        self.chunks = _PCM_CHUNKS if chunks is None else chunks
        self.requests = []

    def streaming_synthesize(self, requests):
        self.requests = list(requests)  # forces the config + input messages out
        return iter([_FakeResponse(c) for c in self.chunks])


class _RaisingStreamingTtsClient:
    def streaming_synthesize(self, requests):
        raise RuntimeError("no TTS creds in test")


class TestTtsStreamGateway(unittest.TestCase):
    def tearDown(self):
        tts.set_client(None)

    def test_stream_yields_the_chunks_in_order(self):
        tts.set_client(_FakeStreamingTtsClient())
        self.assertEqual(list(tts.synthesize_stream("hello")), _PCM_CHUNKS)

    def test_stream_asks_for_charon_and_pcm(self):
        fake = _FakeStreamingTtsClient()
        tts.set_client(fake)
        list(tts.synthesize_stream("hello"))
        config = fake.requests[0].streaming_config
        self.assertEqual(config.voice.name, tts.DEFAULT_VOICE)
        self.assertEqual(config.streaming_audio_config.sample_rate_hertz, tts.STREAM_SAMPLE_RATE)
        from google.cloud import texttospeech
        self.assertEqual(
            config.streaming_audio_config.audio_encoding,
            texttospeech.AudioEncoding.PCM,
        )
        self.assertEqual(fake.requests[1].input.text, "hello")

    def test_stream_raises_when_client_raises(self):
        tts.set_client(_RaisingStreamingTtsClient())
        with self.assertRaises(tts.TtsUnavailable):
            list(tts.synthesize_stream("hello"))

    def test_stream_raises_on_empty_text(self):
        tts.set_client(_FakeStreamingTtsClient())
        with self.assertRaises(tts.TtsUnavailable):
            list(tts.synthesize_stream("   "))

    def test_stream_raises_when_no_audio_arrives(self):
        tts.set_client(_FakeStreamingTtsClient(chunks=[]))
        with self.assertRaises(tts.TtsUnavailable):
            list(tts.synthesize_stream("hello"))


class TestTtsStreamEndpoint(unittest.TestCase):
    def setUp(self):
        server.stores.clear()
        self.client = TestClient(server.app)
        self.ws = "ws_tts_stream"

    def tearDown(self):
        tts.set_client(None)
        server.stores.clear()

    def test_stream_returns_pcm_and_its_sample_rate(self):
        tts.set_client(_FakeStreamingTtsClient())
        r = self.client.post(f"/v1/workspaces/{self.ws}/tts/stream", json={"text": "hello"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"".join(_PCM_CHUNKS))
        self.assertEqual(r.headers["x-sample-rate"], str(tts.STREAM_SAMPLE_RATE))
        self.assertEqual(r.headers["x-bytes-per-sample"], str(tts.STREAM_BYTES_PER_SAMPLE))

    def test_stream_returns_503_when_unavailable(self):
        tts.set_client(_RaisingStreamingTtsClient())
        r = self.client.post(f"/v1/workspaces/{self.ws}/tts/stream", json={"text": "hello"})
        self.assertEqual(r.status_code, 503)

    def test_stream_returns_503_when_no_audio_arrives(self):
        tts.set_client(_FakeStreamingTtsClient(chunks=[]))
        r = self.client.post(f"/v1/workspaces/{self.ws}/tts/stream", json={"text": "hello"})
        self.assertEqual(r.status_code, 503)

    def test_whole_file_tts_still_works_beside_the_stream(self):
        """The fallback the browser drops to must stay intact."""
        tts.set_client(_FakeTtsClient())
        r = self.client.post(f"/v1/workspaces/{self.ws}/tts", json={"text": "hello"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(base64.b64decode(r.json()["audio_base64"]), _FAKE_MP3)


if __name__ == "__main__":
    unittest.main()
