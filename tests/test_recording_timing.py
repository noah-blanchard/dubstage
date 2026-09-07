# -*- coding: utf-8 -*-
"""
Prueft die Aufnahme-Zeitachse von DubStage / DubStage recording timing.

Alles hier laeuft ohne Geraet: die Zeitachse ist reine Rechnung, und der
Schnitt bei LOS wird gegen ein nachgebautes PortAudio mit steuerbaren
ADC- und DAC-Uhren geprueft.
"""

import os
import runpy
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dubstage_core as ds
from fake_audio import FakeBackend

SR = ds.SR


def make_pack(spans, backing=None, original=None, sr=SR):
    """Ein Pack nur aus Zahlen / a pack made of numbers only."""
    pack = ds.DubPack(os.path.join("packs", "test"))
    pack.video = "dub_video.mp4"
    for i, (start, dur) in enumerate(spans):
        line = ds.Line("%02d_line_%d-000.wav" % (i, int(start)), start, i)
        line.duration = float(dur)
        line.audio = np.zeros(int(round(dur * sr)), dtype=np.float32)
        pack.lines.append(line)
    pack.video_duration = max(s + d for s, d in spans) + 5.0
    pack.backing_audio = backing
    pack.original_audio = original
    return pack


# ==========================================================================
#  Reine Zeitrechnung / pure timing
# ==========================================================================

class TimelineTests(unittest.TestCase):

    def test_middle_line_starts_at_the_previous_clip(self):
        pack = make_pack([(16.0, 1.0), (20.0, 2.0)])
        tl = ds.recording_timeline(pack, 1)
        self.assertAlmostEqual(tl.media_start, 16.0)
        self.assertAlmostEqual(tl.front_pad, 0.0)
        self.assertAlmostEqual(tl.lead_duration, 4.0)
        # Der Countdown endet genau auf dem Clipbeginn aus DubForge.
        self.assertAlmostEqual(tl.video_time(tl.lead_duration), 20.0)
        self.assertEqual(tl.count_samples[0], tl.go_sample - 3 * SR)

    def test_distant_previous_clip_is_capped_at_five_seconds(self):
        pack = make_pack([(2.0, 1.0), (40.0, 1.0)])
        tl = ds.recording_timeline(pack, 1)
        self.assertAlmostEqual(tl.media_start, 35.0)
        self.assertAlmostEqual(tl.lead_duration, 5.0)
        self.assertAlmostEqual(tl.front_pad, 0.0)

    def test_close_previous_clip_starts_before_it(self):
        pack = make_pack([(19.2, 0.5), (20.0, 1.0)])
        tl = ds.recording_timeline(pack, 1)
        self.assertAlmostEqual(tl.media_start, 17.0)     # vor dem Vorgaenger
        self.assertAlmostEqual(tl.lead_duration, 3.0)    # drei volle Sekunden
        self.assertAlmostEqual(tl.front_pad, 0.0)

    def test_first_line_after_four_seconds_needs_no_padding(self):
        tl = ds.recording_timeline(make_pack([(4.0, 1.0)]), 0)
        self.assertAlmostEqual(tl.media_start, 0.0)
        self.assertAlmostEqual(tl.front_pad, 0.0)
        self.assertAlmostEqual(tl.lead_duration, 4.0)

    def test_first_line_at_one_second_is_padded(self):
        tl = ds.recording_timeline(make_pack([(1.0, 1.0)]), 0)
        self.assertAlmostEqual(tl.media_start, 0.0)
        self.assertAlmostEqual(tl.front_pad, 2.0)
        self.assertAlmostEqual(tl.lead_duration, 3.0)
        # Waehrend der Fuellzeit steht das frueheste Bild.
        self.assertAlmostEqual(tl.video_time(0.0), 0.0)
        self.assertAlmostEqual(tl.video_time(1.9), 0.0)
        self.assertAlmostEqual(tl.video_time(2.5), 0.5)

    def test_every_countdown_number_lasts_one_second(self):
        tl = ds.recording_timeline(make_pack([(6.0, 1.0)]), 0)
        self.assertEqual(len(tl.count_samples), 3)
        for a, b in zip(tl.count_samples, tl.count_samples[1:]):
            self.assertEqual(b - a, SR)
        lead = tl.lead_duration
        for label, marks in (("3", (lead - 3.0, lead - 2.01)),
                             ("2", (lead - 2.0, lead - 1.01)),
                             ("1", (lead - 1.0, lead - 0.01))):
            for at in marks:
                self.assertEqual(tl.countdown_label(at), label)
        self.assertIsNone(tl.countdown_label(lead - 3.5))

    def test_go_sits_on_the_clip_timestamp(self):
        tl = ds.recording_timeline(make_pack([(16.0, 1.0), (20.0, 2.0)]), 1)
        self.assertEqual(tl.countdown_label(tl.lead_duration), "GO")
        self.assertAlmostEqual(tl.video_time(tl.lead_duration), 20.0)
        # LOS haelt nichts an: das Bild laeuft gleichmaessig weiter.
        self.assertAlmostEqual(tl.video_time(tl.lead_duration + 0.5), 20.5)
        self.assertIsNone(tl.countdown_label(tl.lead_duration + 0.3))

    def test_stop_is_go_plus_line_plus_tail(self):
        tl = ds.recording_timeline(make_pack([(16.0, 1.0), (20.0, 2.0)]), 1)
        self.assertEqual(tl.clip_end_sample - tl.go_sample, 2 * SR)
        self.assertEqual(tl.stop_sample - tl.clip_end_sample,
                         int(round(1.5 * SR)))
        self.assertEqual(tl.take_samples, int(round(3.5 * SR)))
        self.assertAlmostEqual(tl.total_duration, tl.lead_duration + 3.5)


# ==========================================================================
#  Monitorton / playback construction
# ==========================================================================

class MonitorAudioTests(unittest.TestCase):

    def setUp(self):
        n = int(40 * SR)
        self.original = np.full(n, 0.5, dtype=np.float32)
        self.backing = np.full(n, -0.25, dtype=np.float32)

    def test_lead_in_is_original_and_switches_at_go_without_a_gap(self):
        pack = make_pack([(16.0, 1.0), (20.0, 2.0)],
                         backing=self.backing, original=self.original)
        tl = ds.recording_timeline(pack, 1)
        mon = ds.build_monitor_audio(pack, tl)
        self.assertEqual(len(mon), tl.stop_sample)
        self.assertTrue(np.all(mon[:tl.go_sample] == 0.5))
        self.assertTrue(np.all(mon[tl.go_sample:] == -0.25))
        self.assertEqual(mon[tl.go_sample - 1], np.float32(0.5))
        self.assertEqual(mon[tl.go_sample], np.float32(-0.25))

    def test_tail_stays_backing_only_when_others_speak(self):
        original = self.original.copy()
        tl_start = int(22.0 * SR)                 # jemand redet im Nachlauf
        original[tl_start:tl_start + SR] = 0.9
        pack = make_pack([(16.0, 1.0), (20.0, 2.0)],
                         backing=self.backing, original=original)
        tl = ds.recording_timeline(pack, 1)
        mon = ds.build_monitor_audio(pack, tl)
        self.assertTrue(np.all(mon[tl.clip_end_sample:] == -0.25))

    def test_without_backing_the_target_window_is_silent(self):
        pack = make_pack([(16.0, 1.0), (20.0, 2.0)], original=self.original)
        tl = ds.recording_timeline(pack, 1)
        mon = ds.build_monitor_audio(pack, tl)
        self.assertTrue(np.all(mon[:tl.go_sample] == 0.5))
        self.assertTrue(np.all(mon[tl.go_sample:] == 0.0))

    def test_front_padding_is_silence(self):
        pack = make_pack([(1.0, 1.0)], backing=self.backing,
                         original=self.original)
        tl = ds.recording_timeline(pack, 0)
        mon = ds.build_monitor_audio(pack, tl)
        pad = int(round(tl.front_pad * SR))
        self.assertTrue(np.all(mon[:pad] == 0.0))
        self.assertTrue(np.all(mon[pad:tl.go_sample] == 0.5))


# ==========================================================================
#  Schnitt bei LOS / capture alignment
# ==========================================================================

class PlaceChunkTests(unittest.TestCase):
    """Der reine Schnitt - ohne Geraet, ohne Uhr."""

    def chunk(self, start, first, count, sr=1000):
        return (start, np.arange(first, first + count, dtype=np.float32))

    def test_a_callback_straddling_go_is_cut_inside_itself(self):
        # Block beginnt 50 Samples vor LOS und laeuft darueber hinaus.
        chunks = [self.chunk(99.95, 1.0, 100)]
        out = ds.place_chunks(chunks, 100.0, 100, sr=1000)
        self.assertEqual(out[0], 51.0)
        self.assertEqual(out[49], 100.0)
        self.assertTrue(np.all(out[50:] == 0.0))

    def test_callbacks_before_on_and_after_go(self):
        chunks = [self.chunk(99.90, 1.0, 100),      # komplett davor
                  self.chunk(100.0, 200.0, 100),    # genau auf LOS
                  self.chunk(100.1, 300.0, 100)]    # danach
        out = ds.place_chunks(chunks, 100.0, 200, sr=1000)
        self.assertEqual(out[0], 200.0)
        self.assertEqual(out[99], 299.0)
        self.assertEqual(out[100], 300.0)

    def test_missing_samples_become_silence_without_shifting(self):
        chunks = [self.chunk(100.0, 1.0, 100),
                  # der Block bei 100.1 geht verloren
                  self.chunk(100.2, 300.0, 100)]
        out = ds.place_chunks(chunks, 100.0, 300, sr=1000)
        self.assertTrue(np.all(out[100:200] == 0.0))
        self.assertEqual(out[200], 300.0)          # nichts rutscht nach vorn

    def test_length_is_always_exact(self):
        out = ds.place_chunks([], 0.0, 777, sr=1000)
        self.assertEqual(len(out), 777)
        self.assertEqual(out.dtype, np.float32)

    def test_timestamps_usable_rejects_a_dead_clock(self):
        good = [(100.0, 0, np.zeros(100)), (100.1, 100, np.zeros(100))]
        self.assertTrue(ds.timestamps_usable(good, 1000))
        dead = [(0.0, 0, np.zeros(100)), (0.0, 100, np.zeros(100))]
        self.assertFalse(ds.timestamps_usable(dead, 1000))
        frozen = [(100.0, 0, np.zeros(100)), (100.0, 100, np.zeros(100))]
        self.assertFalse(ds.timestamps_usable(frozen, 1000))


class SessionCaptureTests(unittest.TestCase):
    """Der Schnitt gegen ein nachgebautes PortAudio."""

    def build(self, backend, spans=((16.0, 1.0), (20.0, 0.4)), index=1):
        pack = make_pack(list(spans),
                         backing=np.zeros(int(40 * SR), dtype=np.float32),
                         original=np.zeros(int(40 * SR), dtype=np.float32))
        tl = ds.recording_timeline(pack, index)
        monitor = ds.build_monitor_audio(pack, tl)
        mic = ds.Mic(SR)
        mic.sd = backend
        session = mic.open_session(tl, monitor)
        return mic, session, tl

    def run_attempt(self, backend, **kw):
        mic, session, tl = self.build(backend, **kw)
        backend.advance_until(session.ready, limit=2.0)
        self.assertTrue(session.ready())
        session.arm()
        backend.advance_until(session.finished, limit=30.0)
        take = mic.finish_session()
        return session, tl, take

    def expected_first(self, session, backend):
        """Der Wert, den das erste behaltene Sample tragen muss."""
        return backend.sample_of(session.go_time())

    def test_take_starts_exactly_at_go(self):
        backend = FakeBackend()
        session, tl, take = self.run_attempt(backend)
        self.assertFalse(session.fallback)
        self.assertLessEqual(abs(take[0] - self.expected_first(session, backend)), 1)

    def test_take_has_the_required_sample_count(self):
        backend = FakeBackend()
        session, tl, take = self.run_attempt(backend)
        self.assertEqual(len(take), tl.take_samples)
        self.assertEqual(len(take), int(round((0.4 + 1.5) * SR)))

    def test_countdown_audio_never_reaches_the_take(self):
        backend = FakeBackend()
        session, tl, take = self.run_attempt(backend)
        # Die Quelle zaehlt Samples hoch: alles vor LOS ist kleiner.
        self.assertGreaterEqual(float(take.min()),
                                self.expected_first(session, backend) - 1)

    def test_the_take_is_a_gapless_run_from_go(self):
        backend = FakeBackend()
        session, tl, take = self.run_attempt(backend)
        self.assertTrue(np.all(np.diff(take) == 1.0))

    def test_latency_does_not_move_the_go_boundary(self):
        for in_lat, out_lat, block in ((0.0, 0.0, 128), (0.05, 0.12, 1024),
                                       (0.003, 0.25, 480)):
            backend = FakeBackend(in_latency=in_lat, out_latency=out_lat,
                                  block=block)
            session, tl, take = self.run_attempt(backend)
            self.assertLessEqual(abs(take[0] - self.expected_first(session, backend)), 1,
                                 "Latenz %s/%s" % (in_lat, out_lat))

    def test_device_startup_delay_only_costs_preparation_time(self):
        backend = FakeBackend()
        backend.deaf_steps = 300               # Geraet braucht lange
        mic, session, tl = self.build(backend)
        self.assertFalse(session.ready())
        backend.advance_until(session.ready, limit=5.0)
        session.arm()
        backend.advance_until(session.finished, limit=30.0)
        take = mic.finish_session()
        self.assertLessEqual(abs(take[0] - self.expected_first(session, backend)), 1)

    def test_two_stream_fallback_keeps_the_boundary(self):
        backend = FakeBackend(duplex=False)
        session, tl, take = self.run_attempt(backend)
        self.assertFalse(session.duplex)
        self.assertLessEqual(abs(take[0] - self.expected_first(session, backend)), 1)

    def test_a_lost_input_callback_becomes_silence(self):
        backend = FakeBackend(duplex=False)
        mic, session, tl = self.build(backend)
        backend.advance_until(session.ready, limit=2.0)
        session.arm()
        # Einen Block mitten in der Zeile verlieren.
        lost = backend.steps + int((tl.lead_duration + 0.2) * SR
                                   / backend.block)
        backend.drop.add(lost)
        backend.advance_until(session.finished, limit=30.0)
        take = mic.finish_session()
        self.assertEqual(len(take), tl.take_samples)
        zeros = int(np.count_nonzero(take == 0.0))
        self.assertGreaterEqual(zeros, backend.block - 1)
        # Was danach kam, steht immer noch an seiner Stelle.
        first = self.expected_first(session, backend)
        self.assertLessEqual(abs(take[-1] - (first + len(take) - 1)), 1)

    def test_fallback_without_adc_timestamps_keeps_the_first_callback(self):
        backend = FakeBackend(adc_times=False)
        session, tl, take = self.run_attempt(backend)
        self.assertTrue(session.fallback)
        # Ohne Zeitstempel zaehlen wir Samples - auf einen Block genau.
        self.assertLessEqual(abs(take[0] - self.expected_first(session, backend)),
                             backend.block)
        self.assertEqual(len(take), tl.take_samples)

    def test_live_envelope_only_shows_the_retained_window(self):
        backend = FakeBackend()
        mic, session, tl = self.build(backend)
        backend.advance_until(session.ready, limit=2.0)
        session.arm()
        backend.advance(tl.lead_duration - 0.5)     # noch im Countdown
        self.assertEqual(session.envelope()[0], [])
        backend.advance_until(session.finished, limit=30.0)
        env, step = session.envelope()
        self.assertTrue(env)
        self.assertLessEqual(len(env) * step, tl.take_duration + step)

    def test_monitor_audio_reaches_the_output(self):
        backend = FakeBackend()
        pack = make_pack([(16.0, 1.0), (20.0, 0.4)],
                         backing=np.full(int(40 * SR), -0.25, dtype=np.float32),
                         original=np.full(int(40 * SR), 0.5, dtype=np.float32))
        tl = ds.recording_timeline(pack, 1)
        monitor = ds.build_monitor_audio(pack, tl)
        mic = ds.Mic(SR)
        mic.sd = backend
        session = mic.open_session(tl, monitor)
        backend.advance_until(session.ready, limit=2.0)
        quiet = len(backend.played())
        session.arm()
        backend.advance_until(session.finished, limit=30.0)
        played = backend.played()
        # Vor arm() bleibt der Ausgang stumm, danach kommt der Monitor.
        self.assertTrue(np.all(played[:quiet] == 0.0))
        audible = played[quiet:quiet + len(monitor)]
        self.assertTrue(np.array_equal(audible, monitor))
        mic.cancel_session()

    def test_cancel_closes_both_streams(self):
        backend = FakeBackend(duplex=False)
        mic, session, tl = self.build(backend)
        backend.advance_until(session.ready, limit=2.0)
        session.arm()
        backend.advance(0.2)
        mic.cancel_session()
        self.assertTrue(all(s.closed for s in backend.streams))
        self.assertIsNone(mic.session)

    def test_microphone_runs_before_anything_is_audible(self):
        backend = FakeBackend()
        mic, session, tl = self.build(backend)
        backend.advance_until(session.ready, limit=2.0)
        session.arm()
        backend.advance(0.2)
        # Beim ersten hoerbaren Block lagen schon Eingabedaten vor.
        self.assertGreaterEqual(session._arm_frame, backend.block)
        mic.cancel_session()


# ==========================================================================
#  Oberflaeche / user interface
# ==========================================================================

class StubSession(object):
    """Ein Versuch, dessen Uhr der Test stellt."""

    def __init__(self, timeline, monitor):
        self.tl = timeline
        self.monitor = monitor
        self.pos = 0.0
        self.armed = False
        self.closed = False
        self.completed = False
        self.take = np.linspace(0.1, 0.9, timeline.take_samples,
                                dtype=np.float32)

    def ready(self):
        return True

    def arm(self):
        self.armed = True

    def started(self):
        return self.armed

    def position(self):
        return self.pos

    def finished(self):
        return self.pos >= self.tl.total_duration

    def envelope(self):
        return [], 0.02

    def level(self):
        return 0.0

    def complete(self):
        self.completed = True
        self.closed = True
        return self.take

    def cancel(self):
        self.closed = True


class StubMic(ds.Mic):
    """Mikrofon ohne Geraet - liefert StubSession."""

    def __init__(self):
        ds.Mic.__init__(self, SR)
        self.sd = FakeBackend()
        self.fail = False
        self.played = []

    def open_session(self, timeline, monitor, device=None, log=None):
        if self.fail:
            raise RuntimeError("Geraet belegt / device busy")
        self.cancel_session()
        self.session = StubSession(timeline, monitor)
        return self.session

    def play(self, data):
        self.played.append(np.asarray(data, dtype=np.float32))

    def stop_play(self):
        pass

    def stop(self):
        return np.zeros(0, dtype=np.float32)


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"),
                     "Tk display required")
class StageRecordingTests(unittest.TestCase):
    """Die Buehne selbst - mit echten Widgets, ohne Fenster und Geraet."""

    def setUp(self):
        namespace = runpy.run_path("DubStage.pyw", run_name="ui_test")
        self.Game = namespace["Game"]
        self.mod = self.Game.__init__.__globals__
        original_init = tk.Tk.__init__

        def hidden_init(root, *args, **kwargs):
            original_init(root, *args, **kwargs)
            root.withdraw()

        self.patches = [
            patch.object(tk.Tk, "__init__", hidden_init),
            patch.dict(self.mod, {"load_cfg": lambda: {"lang": "en",
                                                       "check_updates": False},
                                  "save_cfg": lambda cfg: None}),
            patch.object(self.Game, "show_menu", lambda self: None),
            patch.object(self.Game, "_check_update", lambda self: None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.stop_patches)

        self.app = self.Game()
        self.addCleanup(self.app.destroy)
        self.app.mic = StubMic()
        n = int(40 * SR)
        self.app.pack = make_pack(
            [(16.0, 1.0), (20.0, 0.4)],
            backing=np.full(n, -0.25, dtype=np.float32),
            original=np.full(n, 0.5, dtype=np.float32))
        self.app.pack.fps = 25.0
        self.app.line_i = 1
        self.app.build_stage()
        self.app.update_idletasks()
        self.frames_shown = []
        self.app.show_frame = self.frames_shown.append

    def stop_patches(self):
        for p in reversed(self.patches):
            p.stop()

    # ------------------------------------------------------------ Helfer
    def line(self):
        return self.app.pack.lines[self.app.line_i]

    def session(self):
        return self.app.mic.session

    def overlay(self):
        cv = self.app.cv
        if cv.itemcget(self.app.overlay_text, "state") == "hidden":
            return None
        return cv.itemcget(self.app.overlay_text, "text")

    def tick_at(self, seconds):
        self.session().pos = float(seconds)
        self.app._attempt_tick()

    def controls(self):
        return (self.app.b_orig, self.app.b_rec, self.app.b_take,
                self.app.b_prev, self.app.b_next)

    # ------------------------------------------------------------- Tests
    def test_microphone_is_open_before_the_lead_in_starts(self):
        self.app.do_record()
        self.assertIsNotNone(self.session())
        self.assertTrue(self.session().armed)      # erst nach ready()
        self.assertEqual(self.app.phase, "record")

    def test_controls_stay_disabled_for_the_whole_attempt(self):
        self.app.do_record()
        for stage in (0.0, 2.0, 3.0, 3.2, 4.0):
            self.tick_at(stage)
            for b in self.controls():
                self.assertFalse(b.enabled, "Knopf offen bei %.1f" % stage)

    def test_video_advances_through_every_number_and_go(self):
        self.app.do_record()
        tl = self.session().tl
        del self.frames_shown[:]
        for at in (0.0, 1.0, 2.0, tl.lead_duration, tl.lead_duration + 0.2):
            self.tick_at(at)
        self.assertEqual(len(self.frames_shown), 5)
        self.assertEqual(self.frames_shown,
                         sorted(self.frames_shown))
        self.assertGreater(self.frames_shown[-1], self.frames_shown[0])
        self.assertAlmostEqual(self.frames_shown[3], self.line().start)

    def test_a_late_repaint_jumps_to_the_right_number(self):
        self.app.do_record()
        tl = self.session().tl
        self.tick_at(tl.lead_duration - 3.0)
        self.assertEqual(self.overlay(), "3")
        # Tk kommt spaet zurueck - die Zahl wird nicht verlaengert.
        self.tick_at(tl.lead_duration - 0.4)
        self.assertEqual(self.overlay(), "1")
        self.tick_at(tl.lead_duration)
        self.assertEqual(self.overlay(), self.mod["t"]("go"))

    def test_go_clears_quickly_without_holding_the_scene(self):
        self.app.do_record()
        tl = self.session().tl
        self.tick_at(tl.lead_duration + 0.1)
        self.assertEqual(self.overlay(), self.mod["t"]("go"))
        self.tick_at(tl.lead_duration + 0.3)
        self.assertIsNone(self.overlay())
        # Das Bild laeuft dabei weiter.
        self.assertGreater(self.frames_shown[-1], self.frames_shown[0])

    def test_finishing_stores_the_retained_window_and_waits(self):
        self.app.do_record()
        session = self.session()
        tl = session.tl
        self.tick_at(tl.total_duration)
        self.assertTrue(session.completed)
        self.assertEqual(len(self.line().take), tl.take_samples)
        self.assertEqual(self.app.phase, "idle")           # nichts spielt nach
        self.assertIsNone(self.app._play)
        self.assertIsNone(self.app._rec_session)
        for b in self.controls():
            self.assertTrue(b.enabled)

    def test_a_failed_microphone_keeps_the_previous_take(self):
        keep = np.full(1000, 0.3, dtype=np.float32)
        self.line().take = keep
        self.app.mic.fail = True
        with patch.object(self.mod["messagebox"], "showerror") as err:
            self.app.do_record()
        self.assertTrue(err.called)
        self.assertIs(self.line().take, keep)
        self.assertEqual(self.app.phase, "idle")
        self.assertIsNone(self.overlay())
        self.assertTrue(self.app.b_rec.enabled)

    def test_escape_discards_the_attempt_and_restores_the_controls(self):
        keep = np.full(1000, 0.3, dtype=np.float32)
        self.line().take = keep
        self.app.do_record()
        session = self.session()
        self.tick_at(1.0)
        self.app._on_escape()
        self.assertTrue(session.closed)
        self.assertFalse(session.completed)
        self.assertIs(self.line().take, keep)
        self.assertEqual(self.app.phase, "idle")
        for b in self.controls():
            self.assertTrue(b.enabled)

    def test_the_watchdog_covers_the_whole_attempt(self):
        line = self.line()
        tl = ds.recording_timeline(self.app.pack, self.app.line_i)
        self.app.do_record()
        session = self.session()
        # Die Notbremse darf nicht vor Bereitschaft, Vorlauf, Zeile und
        # Nachlauf zuschlagen.
        left = self.app._phase_deadline - self.mod["time"].perf_counter()
        self.assertGreater(left, tl.total_duration)
        self.app._phase_deadline = self.mod["time"].perf_counter() - 1.0
        self.app._pump()
        self.assertEqual(self.app.phase, "idle")
        self.assertTrue(session.closed)
        self.assertIsNone(line.take)
        for b in (self.app.b_orig, self.app.b_rec, self.app.b_next):
            self.assertTrue(b.enabled)

    def test_original_plays_the_clip_only(self):
        line = self.line()
        line.audio = np.full(int(line.duration * SR), 0.4, dtype=np.float32)
        self.app.play_original()
        self.assertEqual(len(self.app.mic.played[-1]), len(line.audio))
        self.app._force_idle()

    def test_my_take_plays_the_take_only(self):
        line = self.line()
        line.take = np.full(int(round((line.duration + ds.REC_TAIL) * SR)),
                            0.4, dtype=np.float32)
        self.app.play_take()
        self.assertEqual(len(self.app.mic.played[-1]), len(line.take))
        self.app._force_idle()

    def test_leaving_the_stage_closes_a_running_attempt(self):
        self.app.do_record()
        session = self.session()
        self.app._stop_audio()
        self.assertTrue(session.closed)
        self.assertIsNone(self.app.mic.session)


if __name__ == "__main__":
    unittest.main()
