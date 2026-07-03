"""Adaptive energy-based utterance segmenter for a 16 kHz mono float32 stream.

Tracks a noise floor while nobody speaks; an utterance starts when RMS exceeds
noise_floor * energy_ratio and ends after `silence_ms` of quiet. A short
pre-roll is prepended so the first syllable isn't clipped.
"""
from collections import deque

import numpy as np


class EnergySegmenter:
    def __init__(self, rate=16000, silence_ms=700, min_utterance_ms=400,
                 energy_ratio=2.0, preroll_ms=240, max_utterance_s=30,
                 abs_min_rms=0.0025):
        self.rate = rate
        self.max_silence = int(rate * silence_ms / 1000)
        self.min_voiced = int(rate * min_utterance_ms / 1000)
        self.ratio = energy_ratio
        self.abs_min = abs_min_rms
        self.max_samples = int(rate * max_utterance_s)
        self.preroll = deque(maxlen=max(1, preroll_ms // 30))
        self.noise = 0.001
        self._reset()

    def _reset(self):
        self.in_speech = False
        self.buf: list[np.ndarray] = []
        self.buf_len = 0
        self.voiced = 0
        self.silence = 0

    def feed(self, chunk: np.ndarray) -> list[np.ndarray]:
        """Feed one ~30 ms chunk; returns zero or more completed utterances."""
        out = []
        rms = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)
        thresh = max(self.noise * self.ratio, self.abs_min)

        if not self.in_speech:
            if rms > thresh:
                self.in_speech = True
                self.buf = list(self.preroll) + [chunk]
                self.buf_len = sum(len(c) for c in self.buf)
                self.voiced = len(chunk)
                self.silence = 0
            else:
                self.noise = 0.95 * self.noise + 0.05 * rms
                self.preroll.append(chunk)
        else:
            self.buf.append(chunk)
            self.buf_len += len(chunk)
            if rms > thresh:
                self.voiced += len(chunk)
                self.silence = 0
            else:
                self.silence += len(chunk)
                if self.silence >= self.max_silence:
                    if self.voiced >= self.min_voiced:
                        out.append(np.concatenate(self.buf))
                    self.preroll.clear()
                    self._reset()
            if self.in_speech and self.buf_len >= self.max_samples:
                out.append(np.concatenate(self.buf))
                self.preroll.clear()
                self._reset()
        return out
