# -*- coding: utf-8 -*-
"""
Ein Audio-Backend zum Nachrechnen / a fake audio backend for tests.

Es tut so, als waere es sounddevice: steuerbare Uhr, steuerbare
Blockgroesse, steuerbare ADC- und DAC-Latenzen. Damit laesst sich der
Schnitt bei LOS pruefen, ohne dass ein Geraet mitspielen muss.
"""

import numpy as np


class FakeTime(object):
    """Das Zeit-Objekt, das PortAudio an den Rueckruf gibt."""

    def __init__(self, adc, dac, current):
        self.inputBufferAdcTime = adc
        self.outputBufferDacTime = dac
        self.currentTime = current


class FakeStream(object):

    def __init__(self, backend, kind, kwargs):
        self.backend = backend
        self.kind = kind
        self.kwargs = kwargs
        self.callback = kwargs.get("callback")
        self.active = False
        self.closed = False

    @property
    def time(self):
        return self.backend.now

    @property
    def latency(self):
        if self.kind == "duplex":
            return (self.backend.in_latency, self.backend.out_latency)
        return (self.backend.in_latency if self.kind == "in"
                else self.backend.out_latency)

    def start(self):
        self.active = True
        if self not in self.backend.streams:
            self.backend.streams.append(self)

    def stop(self):
        self.active = False

    def close(self):
        self.active = False
        self.closed = True


class FakeBackend(object):
    """
    sounddevice-Ersatz. `advance()` laesst die Uhr blockweise laufen und
    ruft dabei genau die Rueckrufe auf, die ein echtes Geraet aufrufen
    wuerde.
    """

    def __init__(self, sr=44100, block=256, in_latency=0.012,
                 out_latency=0.030, duplex=True, adc_times=True, t0=1000.0):
        self.sr = int(sr)
        self.block = int(block)
        self.in_latency = float(in_latency)
        self.out_latency = float(out_latency)
        self.duplex_ok = bool(duplex)
        self.adc_times = bool(adc_times)
        self.t0 = float(t0)
        self.now = float(t0)
        self.streams = []
        self.output = []            # ausgegebene Bloecke
        self.steps = 0
        self.deaf_steps = 0         # so viele Bloecke braucht der Start
        self.drop = set()           # Eingabebloecke, die verlorengehen
        # Standardquelle: jeder Wert ist die Samplenummer seiner ADC-Zeit,
        # gezaehlt ab Uhrbeginn. So verraet jedes Sample, wann es
        # aufgenommen wurde - und bleibt klein genug fuer float32.
        self.source = lambda t, n: (np.arange(n, dtype=np.float64)
                                    + self.sample_of(t)).astype(np.float32)

    def sample_of(self, when):
        """Samplenummer eines Zeitpunkts, gezaehlt ab Uhrbeginn."""
        return round((float(when) - self.t0) * self.sr)

    # ------------------------------------------------------- sounddevice
    def Stream(self, **kwargs):
        if not self.duplex_ok:
            raise RuntimeError("kein Vollduplex / no full duplex")
        return FakeStream(self, "duplex", kwargs)

    def InputStream(self, **kwargs):
        return FakeStream(self, "in", kwargs)

    def OutputStream(self, **kwargs):
        return FakeStream(self, "out", kwargs)

    def play(self, *a, **kw):
        pass

    def stop(self):
        pass

    def query_devices(self):
        return [{"name": "Fake", "max_input_channels": 1,
                 "max_output_channels": 2}]

    # ------------------------------------------------------------- Uhr
    def advance(self, seconds):
        for _ in range(int(round(float(seconds) * self.sr / self.block))):
            self.step()

    def advance_until(self, predicate, limit=20.0):
        """Laeuft, bis die Bedingung stimmt - hoechstens `limit` Sekunden."""
        end = self.now + float(limit)
        while self.now < end:
            if predicate():
                return True
            self.step()
        return predicate()

    def step(self):
        """Ein Block Audio - genau so, wie PortAudio ihn liefern wuerde."""
        n = self.block
        adc_real = self.now - self.in_latency
        info = FakeTime(adc_real if self.adc_times else 0.0,
                        self.now + self.out_latency, self.now)
        if self.steps < self.deaf_steps:
            # Das Geraet hat noch nicht angefangen: kein einziger Rueckruf.
            self.steps += 1
            self.now += n / float(self.sr)
            return
        indata = np.zeros((n, 1), dtype=np.float32)
        indata[:, 0] = self.source(adc_real, n)
        for stream in list(self.streams):
            if not stream.active or stream.callback is None:
                continue
            if stream.kind == "duplex":
                outdata = np.zeros((n, 1), dtype=np.float32)
                stream.callback(indata.copy(), outdata, n, info, None)
                self.output.append(outdata[:, 0].copy())
            elif stream.kind == "in":
                if self.steps not in self.drop:
                    stream.callback(indata.copy(), n, info, None)
            else:
                outdata = np.zeros((n, 1), dtype=np.float32)
                stream.callback(outdata, n, info, None)
                self.output.append(outdata[:, 0].copy())
        self.steps += 1
        self.now += n / float(self.sr)

    def played(self):
        """Alles, was ausgegeben wurde, am Stueck."""
        if not self.output:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.output)
