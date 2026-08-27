# src/agent/tts.py
"""
Central Google Cloud Text-to-Speech access for Focus Agent.

Mirrors src/agent/llm.py: a single place for the voice/model choice, a lazily
built + injectable client (so unit tests inject a fake and never hit the
network), and a TtsUnavailable exception the caller catches to degrade
gracefully to a text-only reply.

Credentials follow the same ADC bridge as llm.py:
- On Cloud Run the runtime service account is keyless — google.auth.default()
  resolves it automatically, no key file needed.
- Locally, GOOGLE_APPLICATION_CREDENTIALS (loaded from .env) or an active
  `gcloud auth application-default login` supplies ADC.

Ops note: the project must have `texttospeech.googleapis.com` enabled and the
runtime SA needs a role that grants the TTS synthesize permission (Cloud
Text-to-Speech has no dedicated predefined role; grant `roles/serviceusage.serviceUsageConsumer`
plus billing on the project, which is what the API check requires). This is a
one-time user/ops step, not something this module can do.
"""
from __future__ import annotations

from typing import Iterator

# Blink's voice: Charon, a calm male Chirp 3 HD voice (user-picked 2026-08-26).
# Callers may override.
DEFAULT_VOICE = "en-US-Chirp3-HD-Charon"

#: Streaming synthesis returns headerless LINEAR16 (signed 16-bit little-endian)
#: at this rate. The client needs both numbers to turn the byte stream into
#: playable audio and to know how many seconds it is holding.
STREAM_SAMPLE_RATE = 24000
STREAM_BYTES_PER_SAMPLE = 2


class TtsUnavailable(RuntimeError):
    """Raised when Cloud TTS cannot be reached (missing lib, no creds, API error).

    Callers should catch this and degrade to a text-only reply rather than fail
    the whole turn.
    """


_client = None  # cached texttospeech.TextToSpeechClient, or an injected fake


def set_client(client) -> None:
    """Inject a client (real or fake). Tests use this to avoid network + spend."""
    global _client
    _client = client


def get_client():
    """Lazily build a TextToSpeechClient from ADC. Injectable via set_client."""
    global _client
    if _client is not None:
        return _client
    try:
        from google.cloud import texttospeech
    except ImportError as e:  # pragma: no cover
        raise TtsUnavailable(f"google-cloud-texttospeech not installed: {e}")
    try:
        # The client resolves ADC on its own: Cloud Run's keyless runtime SA,
        # or GOOGLE_APPLICATION_CREDENTIALS / gcloud ADC locally.
        _client = texttospeech.TextToSpeechClient()
    except Exception as e:  # pragma: no cover - depends on ambient creds
        raise TtsUnavailable(f"Could not build Cloud TTS client: {e}")
    return _client


def synthesize(text: str, voice_name: str = DEFAULT_VOICE) -> bytes:
    """Synthesize `text` to MP3 audio bytes via Google Cloud Text-to-Speech.

    Uses a neutral, calm en-US Neural2 voice and MP3 encoding. Raises
    TtsUnavailable on any failure (missing library, no credentials, API error)
    so the caller can fall back to a silent, text-only reply.

    Args:
        text: The agent reply to speak. Empty/whitespace raises TtsUnavailable.
        voice_name: A Cloud TTS voice name (default: a calm female Neural2 voice).

    Returns:
        MP3 audio bytes.
    """
    if not text or not text.strip():
        raise TtsUnavailable("No text to synthesize.")

    client = get_client()
    try:
        from google.cloud import texttospeech

        synthesis_input = texttospeech.SynthesisInput(text=text)
        # Name-driven selection; no ssml_gender hint so the named voice always wins.
        voice = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        )
        resp = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
    except Exception as e:
        raise TtsUnavailable(f"Cloud TTS call failed: {type(e).__name__}: {str(e)[:200]}")

    audio = getattr(resp, "audio_content", None)
    if not audio:
        raise TtsUnavailable("Cloud TTS returned no audio content.")
    return audio


def synthesize_stream(
    text: str, voice_name: str = DEFAULT_VOICE
) -> Iterator[bytes]:
    """Stream `text` to speech as raw PCM chunks (P12-03b).

    Chirp 3 HD supports bidirectional STREAMING synthesis, which hands back the
    first audio chunk in a few hundred milliseconds instead of making the caller
    wait for the whole file. `synthesize` above still exists and is still the
    fallback: this one is the fast path.

    The stream is headerless LINEAR16, mono, at STREAM_SAMPLE_RATE. There is no
    container and no duration header, so the total length of the reply is only
    known once the stream ends. Callers that need a duration up front should use
    `synthesize` instead.

    Yields:
        Non-empty PCM byte chunks, in order.

    Raises:
        TtsUnavailable: on empty text, a missing library, missing credentials,
        or any API error. The first `next()` on this generator is where a setup
        failure lands, so a caller can decide its HTTP status before it starts
        writing a response body.
    """
    if not text or not text.strip():
        raise TtsUnavailable("No text to synthesize.")

    client = get_client()
    try:
        from google.cloud import texttospeech

        config = texttospeech.StreamingSynthesizeConfig(
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US",
                name=voice_name,
            ),
            streaming_audio_config=texttospeech.StreamingAudioConfig(
                audio_encoding=texttospeech.AudioEncoding.PCM,
                sample_rate_hertz=STREAM_SAMPLE_RATE,
            ),
        )

        def _requests():
            # The first message carries config only, every later one input only.
            yield texttospeech.StreamingSynthesizeRequest(streaming_config=config)
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=text)
            )

        responses = client.streaming_synthesize(_requests())
    except Exception as e:
        raise TtsUnavailable(
            f"Cloud TTS streaming call failed: {type(e).__name__}: {str(e)[:200]}"
        )

    sent_any = False
    try:
        for resp in responses:
            chunk = getattr(resp, "audio_content", None)
            if chunk:
                sent_any = True
                yield chunk
    except Exception as e:
        if not sent_any:
            raise TtsUnavailable(
                f"Cloud TTS streaming call failed: {type(e).__name__}: {str(e)[:200]}"
            )
        # Mid-stream failure: stop cleanly. The caller has real audio already and
        # the text path is unaffected, so this degrades rather than fabricating.
        return

    if not sent_any:
        raise TtsUnavailable("Cloud TTS streamed no audio content.")
