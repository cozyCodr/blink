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


"""
2026-09-01 — speech never contains a URL.

The user heard Blink read a Google grounding redirect out loud: hundreds of
base64 characters. Sources now travel as structured data, but a model can still
emit a link, so the synthesis path itself strips URLs before speaking. This is
presentation only: nothing is added, and the TEXT returned to the client is
never touched by this.
"""

_GROUNDING_URL = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
    "AUZIYQG4K1B2j94YC_" + "Qx7Zk9" * 20 + "=="
)


class TestSpeechStripsUrls(unittest.TestCase):
    def tearDown(self):
        tts.set_client(None)
        server.stores.clear()

    def test_speakable_replaces_a_grounding_url_with_a_spoken_standin(self):
        said = tts.speakable(f"The exam is on 12 November. Source: {_GROUNDING_URL}")
        self.assertNotIn("http", said)
        self.assertNotIn("vertexaisearch", said)
        self.assertIn("12 November", said)
        self.assertIn(tts.SPOKEN_URL_STANDIN, said)

    def test_speakable_leaves_url_free_text_alone(self):
        line = "You have two sessions left today, and both fit before six."
        self.assertEqual(tts.speakable(line), line)

    def test_speakable_handles_www_and_several_links(self):
        said = tts.speakable("See www.examboard.org and https://gov.uk/exams for dates.")
        self.assertNotIn("www.", said)
        self.assertNotIn("https://", said)
        self.assertEqual(said.count(tts.SPOKEN_URL_STANDIN), 2)

    def test_synthesis_is_handed_text_with_no_url(self):
        class _Capturing:
            def __init__(self):
                self.text = None

            def synthesize_speech(self, *a, **k):
                self.text = k["input"].text
                return _FakeResponse(_FAKE_MP3)

        fake = _Capturing()
        tts.set_client(fake)
        tts.synthesize(f"The date is confirmed. {_GROUNDING_URL}")
        self.assertNotIn("http", fake.text)
        self.assertIn("The date is confirmed.", fake.text)

    def test_streaming_synthesis_is_handed_text_with_no_url(self):
        fake = _FakeStreamingTtsClient()
        tts.set_client(fake)
        list(tts.synthesize_stream(f"The date is confirmed. {_GROUNDING_URL}"))
        spoken = fake.requests[1].input.text
        self.assertNotIn("http", spoken)
        self.assertIn("The date is confirmed.", spoken)

    def test_a_url_only_reply_still_synthesizes(self):
        tts.set_client(_FakeTtsClient())
        self.assertEqual(tts.synthesize(_GROUNDING_URL), _FAKE_MP3)

    def test_the_client_text_is_never_rewritten_by_the_tts_call(self):
        # /tts returns audio only; the reply text the client holds is its own,
        # and nothing on this path mutates the caller's string.
        tts.set_client(_FakeTtsClient())
        server.stores.clear()
        client = TestClient(server.app)
        original = f"The exam is on 12 November. {_GROUNDING_URL}"
        r = client.post("/v1/workspaces/ws_tts_urls/tts", json={"text": original})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json()), {"audio_base64", "mime"})
        self.assertIn(_GROUNDING_URL, original)  # untouched in the caller's hands



if __name__ == "__main__":
    unittest.main()
