"""PCM conversion between Discord and Gemini Live.

Discord: signed 16-bit little-endian, 48 kHz, stereo interleaved.
Gemini Live: in 16-bit mono 16 kHz, out 16-bit mono 24 kHz.

Both converters are stateful so chunk boundaries never click or drop a sample.
"""

from __future__ import annotations

import numpy as np

_SAMPLE = np.dtype("<i2")
# 3 stereo frames at 48 kHz become 1 mono sample at 16 kHz: 3 * 2 ch * 2 bytes.
_DOWN_GROUP_BYTES = 12


class DiscordToGemini:
    """48 kHz stereo in, 16 kHz mono out.

    Downmix and decimate by 3 with a 3-tap box average. The box is a weak
    low-pass (about 0.6 at 8 kHz), so part of the 8 to 16 kHz band folds
    back. Acceptable for speech recognition, not for music.
    """

    def __init__(self) -> None:
        self._carry = b""

    def feed(self, pcm48_stereo: bytes) -> bytes:
        data = self._carry + pcm48_stereo
        usable = len(data) - len(data) % _DOWN_GROUP_BYTES
        self._carry = data[usable:]
        if not usable:
            return b""
        groups = np.frombuffer(data[:usable], dtype=_SAMPLE).reshape(-1, 3, 2)
        mono = groups.astype(np.int32).sum(axis=(1, 2)) / 6.0
        return np.clip(np.rint(mono), -32768, 32767).astype(_SAMPLE).tobytes()

    def reset(self) -> None:
        self._carry = b""


class Upsample16To24:
    """16 kHz mono in, 24 kHz mono out: what OpenAI Realtime takes as input.

    Linear interpolation at exact thirds of a sample: output n sits at input
    position 2n/3. Positions are counted in thirds as integers, so no float
    drift accumulates over a long call, and feeding the stream in any chunks
    gives the same samples as feeding it whole.
    """

    def __init__(self) -> None:
        self._carry = b""
        self._last: np.ndarray | None = None
        self._t3 = 0  # next output position, in thirds of an input sample

    def feed(self, pcm16_mono: bytes) -> bytes:
        data = self._carry + pcm16_mono
        usable = len(data) - len(data) % 2
        self._carry = data[usable:]
        if not usable:
            return b""
        new = np.frombuffer(data[:usable], dtype=_SAMPLE).astype(np.float64)
        samples = new if self._last is None else np.concatenate((self._last, new))
        span3 = (samples.size - 1) * 3
        t3 = np.arange(self._t3, span3, 2)
        index, frac = t3 // 3, (t3 % 3) / 3.0
        out = samples[index] * (1.0 - frac) + samples[index + 1] * frac if t3.size else np.empty(0)
        self._t3 = (int(t3[-1]) + 2 if t3.size else self._t3) - span3
        self._last = samples[-1:]
        return np.clip(np.rint(out), -32768, 32767).astype(_SAMPLE).tobytes()

    def reset(self) -> None:
        self._carry, self._last, self._t3 = b"", None, 0


class GeminiToDiscord:
    """24 kHz mono in, 48 kHz stereo out.

    Linear interpolation: each input sample x emits the midpoint with the
    previous sample, then x. The previous sample survives across chunks.
    """

    def __init__(self) -> None:
        self._carry = b""
        self._previous = 0

    def feed(self, pcm24_mono: bytes) -> bytes:
        data = self._carry + pcm24_mono
        usable = len(data) - len(data) % 2
        self._carry = data[usable:]
        if not usable:
            return b""
        samples = np.frombuffer(data[:usable], dtype=_SAMPLE).astype(np.int32)
        shifted = np.concatenate(([self._previous], samples[:-1]))
        self._previous = int(samples[-1])
        mid = (shifted + samples) // 2
        mono48 = np.empty(samples.size * 2, dtype=np.int32)
        mono48[0::2] = mid
        mono48[1::2] = samples
        stereo = np.repeat(mono48, 2)
        return stereo.astype(_SAMPLE).tobytes()

    def reset(self) -> None:
        self._carry = b""
        self._previous = 0
