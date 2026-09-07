# -*- coding: utf-8 -*-
"""
dubstage_core.py  --  Spiel-Logik fuer DubStage / game logic.

Video laeuft, Zeile nachsprechen, am Ende laeuft die ganze Szene
mit der eigenen Stimme. Keine GUI hier drin.
"""

import collections
import os
import re
import glob
import shutil
import struct
import tempfile
import threading
import time
import wave

import numpy as np

import dubforge_core as pc

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PACKS_DIR = os.path.join(APP_DIR, "packs")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "dubstage_cache")

SR = 44100                 # Arbeits-Samplerate / working sample rate
FRAME_FPS = 25             # Bilder pro Sekunde fuer die Wiedergabe
FRAME_W = 960              # Breite der Einzelbilder - nie hochskalieren

# Aufnahme-Zeitmodell / recording timing model.  Feste Groessen, keine
# Einstellungen - der Ablauf soll auf jedem Rechner gleich klingen.
REC_TAIL = 1.5             # Nachlauf nach dem Clip / grace period
REC_COUNTDOWN = 3.0        # volle Sekunden 3 - 2 - 1 vor LOS
REC_MAX_LEAD = 5.0         # laengster Vorlauf / longest context lead-in
GO_HOLD = 0.25             # wie lange "LOS" stehen bleibt, rein optisch


# ==========================================================================
#  Packs finden / find packs
# ==========================================================================

class Line(object):
    """Eine Zeile des Dub-Packs / one line of a dub pack."""

    def __init__(self, path, start, index):
        self.path = path
        self.file = os.path.basename(path)
        self.start = float(start)
        self.index = index
        self.name = self._label()
        self.caption = ""       # Untertitel / subtitle
        self.audio = None       # Original-Sample / original sample
        self.duration = 0.0
        self.take = None        # Aufnahme des Spielers / player recording

    def _label(self):
        stem = os.path.splitext(self.file)[0]
        stem = re.sub(r"^\d+[_\-]", "", stem)
        stem = re.sub(r"_\d+-\d{1,3}$", "", stem)
        return stem.replace("_", " ").strip() or self.file

    @property
    def end(self):
        return self.start + self.duration


class DubPack(object):

    def __init__(self, folder):
        self.folder = folder
        self.name = os.path.basename(folder)
        self.video = None
        self.backing = None
        self.backing_audio = None
        self.original_audio = None     # kompletter Originalton der Szene
        self.lines = []
        self.frames = []
        self.fps = FRAME_FPS
        self.video_duration = 0.0

    def __repr__(self):
        return "<DubPack %s, %d Zeilen>" % (self.name, len(self.lines))


_TS_IN_NAME = re.compile(r"_(\d+)-(\d{1,3})(?:\.[A-Za-z0-9]+)?$")


def timestamp_from_name(filename):
    """'07_MyClip_44-048.wav' -> 44.048 ; sonst None."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _TS_IN_NAME.search(stem)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2)
    return float(whole) + float(frac) / (10 ** len(frac))


def timestamps_from_txt(folder):
    """Liest _TIMESTAMPS.txt als Rueckfallebene / fallback."""
    path = os.path.join(folder, "_TIMESTAMPS.txt")
    out = {}
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"(\S+\.wav)\s+(-?[\d.]+)", line)
                if m:
                    out[m.group(1)] = float(m.group(2))
    except Exception:
        pass
    return out


AUDIO_EXT = (".wav", ".mp3", ".ogg", ".flac")
VIDEO_EXT = (".mp4", ".ogv", ".mkv", ".webm", ".mov", ".avi")


def load_pack(folder):
    """Baut ein DubPack aus einem Ordner. None, wenn kein Dub-Pack."""
    if not os.path.isdir(folder):
        return None
    video = None
    for ext in VIDEO_EXT:                       # mp4 bevorzugt, ogv weiter ok
        cand = os.path.join(folder, "dub_video" + ext)
        if os.path.isfile(cand):
            video = cand
            break
    if not video:
        return None

    pack = DubPack(folder)
    pack.video = video

    from_txt = timestamps_from_txt(folder)
    entries = []
    for f in sorted(os.listdir(folder)):
        low = f.lower()
        if not low.endswith(AUDIO_EXT):
            continue
        if low.startswith("_backing_track"):
            pack.backing = os.path.join(folder, f)
            continue
        if f.startswith("_"):
            continue
        ts = timestamp_from_name(f)
        if ts is None:
            ts = from_txt.get(f)
        entries.append((f, ts))

    known = [e for e in entries if e[1] is not None]
    if not known:
        # Ohne Zeitstempel koennen wir nicht synchronisieren.
        return None

    known.sort(key=lambda e: e[1])
    captions = pc.read_captions(folder)
    for i, (f, ts) in enumerate(known):
        line = Line(os.path.join(folder, f), ts, i)
        line.caption = captions.get(f, "")
        pack.lines.append(line)
    return pack


def find_packs(extra_dirs=None):
    """Sucht Dub-Packs im eigenen packs-Ordner und in zusaetzlichen Ordnern."""
    roots = []

    def add_root(p):
        if p and os.path.isdir(p) and os.path.normpath(p) not in \
                [os.path.normpath(r) for r in roots]:
            roots.append(p)

    add_root(PACKS_DIR)
    for p in (extra_dirs or []):
        add_root(p)

    packs = []
    seen = set()
    for root in roots:
        for entry in sorted(os.listdir(root)):
            folder = os.path.join(root, entry)
            key = os.path.normpath(folder).lower()
            if key in seen or not os.path.isdir(folder):
                continue
            seen.add(key)
            p = load_pack(folder)
            if p:
                packs.append(p)
    return packs


# ==========================================================================
#  Audio
# ==========================================================================

def read_wav_mono(path, sr=SR):
    """Liest beliebiges Audio als Mono-Float-Array (ueber ffmpeg)."""
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        pc.run([pc.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", path, "-ac", "1", "-ar", str(sr),
                "-c:a", "pcm_s16le", tmp], check=True)
        with wave.open(tmp, "rb") as w:
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_wav_mono(path, data, sr=SR):
    data = np.clip(np.asarray(data, dtype=np.float32), -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return path


def normalize(data, peak=0.97):
    data = np.asarray(data, dtype=np.float32)
    m = float(np.max(np.abs(data))) if len(data) else 0.0
    if m < 1e-6:
        return data
    return data * (peak / m)


def load_pack_audio(pack, sr=SR, progress=None):
    """
    Laedt alle Original-Samples, den Backing Track und den kompletten
    Originalton der Szene. Letzterer traegt den Vorlauf vor der Aufnahme.
    """
    total = len(pack.lines) + 2
    for i, line in enumerate(pack.lines):
        line.audio = read_wav_mono(line.path, sr)
        line.duration = len(line.audio) / float(sr)
        if progress:
            progress((i + 1) / float(total))
    pack.backing_audio = (read_wav_mono(pack.backing, sr)
                          if pack.backing else None)
    if progress:
        progress((len(pack.lines) + 1) / float(total))
    try:
        pack.original_audio = read_wav_mono(pack.video, sr)
    except Exception:
        pack.original_audio = None     # Vorlauf laeuft dann eben still
    pack.video_duration = pc.probe_duration(pack.video)
    if progress:
        progress(1.0)
    return pack


# ==========================================================================
#  Analyse / analysis
# ==========================================================================

def trim_silence(data, sr=SR, rel=0.08, pad=0.04, frame_ms=20):
    """
    Schneidet Stille vorn und hinten weg. Damit kostet ein spaeter
    Einsatz keine Punkte - nur der Verlauf selbst zaehlt.
    """
    data = np.asarray(data, dtype=np.float32)
    hop = max(1, int(sr * frame_ms / 1000.0))
    usable = (len(data) // hop) * hop
    if usable < hop * 2:
        return data
    rms = np.sqrt((data[:usable].reshape(-1, hop) ** 2).mean(axis=1))
    peak = float(rms.max())
    if peak < 1e-6:
        return data
    active = np.nonzero(rms >= peak * rel)[0]
    if not len(active):
        return data
    a = max(0, int(active[0] * hop - pad * sr))
    b = min(len(data), int((active[-1] + 1) * hop + pad * sr))
    return data[a:b]


def rms_db(data):
    data = np.asarray(data, dtype=np.float32)
    if not len(data):
        return -120.0
    r = float(np.sqrt((data ** 2).mean()))
    return 20.0 * np.log10(max(r, 1e-9))


# ==========================================================================
#  Video-Frames
# ==========================================================================

def frames_dir_for(pack):
    key = re.sub(r"[^\w]+", "_", os.path.normpath(pack.folder))[-90:]
    return os.path.join(CACHE_DIR, key)


def extract_frames(pack, fps=FRAME_FPS, width=FRAME_W, log=None, force=False):
    """
    Zerlegt das OGV vorab in JPEGs. Umgeht damit jede Codec-Frage
    bei der Wiedergabe - genau der Punkt, an dem das Original haengt.
    """
    outdir = frames_dir_for(pack)
    stamp = os.path.join(outdir, "done.txt")
    want = "%d %d" % (int(fps), int(width))
    if force and os.path.isdir(outdir):
        shutil.rmtree(outdir, ignore_errors=True)
    if os.path.isfile(stamp):
        try:
            with open(stamp, "r", encoding="utf-8") as f:
                have = f.read().strip()
        except Exception:
            have = ""
        if have == want:
            pack.frames = sorted(glob.glob(os.path.join(outdir, "f*.jpg")))
            pack.fps = float(fps)
            if pack.frames:
                return pack.frames
        # andere Aufloesung -> neu erzeugen
        shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    pc.run([pc.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", pack.video,
            "-vf", "fps=%d,scale=%d:-2" % (int(fps), int(width)),
            "-q:v", "5", os.path.join(outdir, "f%05d.jpg")], log=log)
    pack.frames = sorted(glob.glob(os.path.join(outdir, "f*.jpg")))
    pack.fps = float(fps)
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(want)
    return pack.frames


def frame_at(pack, seconds):
    """Index des Bildes zum Zeitpunkt / frame index for a time."""
    if not pack.frames:
        return None
    i = int(round(seconds * pack.fps))
    return max(0, min(len(pack.frames) - 1, i))


# ==========================================================================
#  Dub-Mix rendern
# ==========================================================================

def fit_len(data, n):
    """
    Bringt ein Array auf genau n Werte. Noetig, weil
    int(len(x)/sr*sr) nicht immer wieder len(x) ergibt - sonst knallt
    das Mischen von Aufnahme und Backing Track bei ~8% aller Cliplaengen.
    """
    data = np.asarray(data, dtype=np.float32)
    n = max(0, int(n))
    if len(data) == n:
        return data
    out = np.zeros(n, dtype=np.float32)
    m = min(n, len(data))
    out[:m] = data[:m]
    return out


def slice_audio(data, start, duration, sr=SR):
    """Schneidet einen Bereich heraus, mit Nullen aufgefuellt."""
    if data is None:
        return np.zeros(int(duration * sr), dtype=np.float32)
    a = int(max(0.0, start) * sr)
    n = int(duration * sr)
    out = np.zeros(n, dtype=np.float32)
    chunk = data[a:a + n]
    out[:len(chunk)] = chunk
    return out


def render_dub(pack, sr=SR, duck=0.18, log=None):
    """
    Legt alle Aufnahmen an ihre Zeitstempel ueber den Backing Track.
    Zeilen ohne Aufnahme behalten das Original - so wie es der
    Dub-Guide fuer nicht gewaehlte Figuren beschreibt.
    """
    total = max(pack.video_duration, 0.1)
    for line in pack.lines:
        total = max(total, line.end + 1.0)
    n = int(total * sr) + sr

    backing = getattr(pack, "backing_audio", None)
    if backing is not None:
        base = np.zeros(n, dtype=np.float32)
        base[:min(n, len(backing))] = backing[:n]
        have_backing = True
    else:
        original = getattr(pack, "original_audio", None)
        if original is None:
            original = read_wav_mono(pack.video, sr)
        base = np.zeros(n, dtype=np.float32)
        base[:min(n, len(original))] = original[:n]
        have_backing = False

    for line in pack.lines:
        a = int(line.start * sr)
        if line.take is not None and len(line.take):
            take = normalize(np.asarray(line.take, dtype=np.float32), 0.92)
            if not have_backing:
                # Original an dieser Stelle leiser ziehen
                b = min(n, a + max(len(take), int(line.duration * sr)))
                base[a:b] *= duck
            b = min(n, a + len(take))
            base[a:b] += take[:b - a]
        elif line.audio is not None and have_backing:
            b = min(n, a + len(line.audio))
            base[a:b] += line.audio[:b - a] * 0.9

    return normalize(base, 0.97)


def export_dub_video(pack, mixed_audio, out_path, sr=SR, log=None):
    """Schreibt Video + eigener Tonspur als MP4 zum Weitergeben."""
    tmp_wav = tempfile.mktemp(suffix=".wav")
    write_wav_mono(tmp_wav, mixed_audio, sr)
    try:
        pc.run([pc.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", pack.video, "-i", tmp_wav,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "192k", "-shortest", out_path], log=log)
    finally:
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)
    return out_path


# ==========================================================================
#  Aufnahme-Zeitachse / recording timeline
# ==========================================================================

_REC_FIELDS = ("media_start", "front_pad", "lead_duration", "line_duration",
               "tail_duration", "total_duration", "line_start", "countdown",
               "sr", "count_samples", "go_sample", "clip_end_sample",
               "stop_sample")


class RecordingTimeline(collections.namedtuple("_RecTimeline", _REC_FIELDS)):
    """
    Eine komplette Aufnahme in Zahlen / one recording attempt as numbers.

    Nullpunkt der Zeitachse ist der Beginn des Vorlaufs. LOS liegt genau
    auf dem Clipbeginn aus DubForge. Alles andere - Bild, Ton, Schnitt -
    leitet sich daraus ab, damit nichts mit eigener Uhr laeuft.
    """

    __slots__ = ()

    @property
    def take_samples(self):
        """Laenge der behaltenen Aufnahme in Samples."""
        return self.stop_sample - self.go_sample

    @property
    def take_duration(self):
        return self.line_duration + self.tail_duration

    @property
    def go_time(self):
        """Sekunden vom Beginn des Vorlaufs bis LOS."""
        return self.lead_duration

    def video_time(self, elapsed):
        """
        Bildzeitpunkt im Quellvideo zu einem Punkt der Zeitachse.
        Waehrend der kuenstlichen Vorlaufzeit steht das frueheste Bild.
        """
        elapsed = float(elapsed)
        if elapsed <= self.front_pad:
            return self.media_start
        return self.media_start + (elapsed - self.front_pad)

    def countdown_label(self, elapsed, go_hold=GO_HOLD):
        """
        '3', '2', '1', 'GO' oder None - allein aus der Restzeit bis LOS.
        Ein spaeter Zeichenaufruf springt damit zur richtigen Zahl,
        statt eine Zahl zu verlaengern und das Stichwort zu verschieben.
        """
        elapsed = float(elapsed)
        left = self.lead_duration - elapsed
        if left > self.countdown:
            return None
        if left > 0.0:
            return str(int(np.ceil(left - 1e-9)))
        if elapsed < self.lead_duration + go_hold:
            return "GO"
        return None


def recording_timeline(pack, line_index, max_lead=REC_MAX_LEAD,
                       countdown=REC_COUNTDOWN, tail=REC_TAIL, sr=SR):
    """
    Rechnet den Vorlauf einer Zeile aus - ohne Geraet, ohne Uhr, ohne GUI.

    Regeln: am liebsten ab dem vorigen Clip, nie mehr als fuenf Sekunden
    davor, aber immer mindestens drei. Fehlt dem Video vorne die Zeit,
    wird mit Standbild und Stille aufgefuellt.
    """
    line = pack.lines[line_index]
    target = float(line.start)
    prev = float(pack.lines[line_index - 1].start) if line_index > 0 else 0.0

    start = min(prev, target - countdown)     # drei volle Sekunden sichern
    start = max(start, target - max_lead)     # aber nie mehr als fuenf
    media_start = max(0.0, start)             # nichts vor dem Videoanfang

    real_lead = max(0.0, target - media_start)
    front_pad = max(0.0, countdown - real_lead)
    lead = real_lead + front_pad
    line_dur = float(line.duration)

    go_sample = int(round(lead * sr))
    steps = max(0, int(round(countdown)))
    count_samples = tuple(go_sample - int(round((countdown - i) * sr))
                          for i in range(steps))
    return RecordingTimeline(
        media_start=media_start,
        front_pad=front_pad,
        lead_duration=lead,
        line_duration=line_dur,
        tail_duration=float(tail),
        total_duration=lead + line_dur + float(tail),
        line_start=target,
        countdown=float(countdown),
        sr=int(sr),
        count_samples=count_samples,
        go_sample=go_sample,
        clip_end_sample=go_sample + int(round(line_dur * sr)),
        stop_sample=go_sample + int(round((line_dur + float(tail)) * sr)))


def build_monitor_audio(pack, timeline, sr=SR):
    """
    Der Ton, den der Spieler waehrend eines Versuchs hoert - ein einziger
    Puffer, damit der Wechsel bei LOS keine Luecke bekommt.

    Vorne Stille fuer die kuenstliche Vorlaufzeit, dann der echte
    Szenenton mit allen vorangehenden Repliken, ab LOS nur noch der
    Backing Track. Ohne Backing Track bleibt es ab LOS still.
    """
    tl = timeline
    n = int(tl.stop_sample)
    out = np.zeros(n, dtype=np.float32)

    pad = int(round(tl.front_pad * sr))
    real = max(0, int(tl.go_sample) - pad)
    original = getattr(pack, "original_audio", None)
    if real > 0 and original is not None:
        seg = slice_audio(original, tl.media_start, real / float(sr), sr)
        out[pad:pad + real] = fit_len(seg, real)

    backing = getattr(pack, "backing_audio", None)
    if backing is not None and n > tl.go_sample:
        m = n - int(tl.go_sample)
        seg = slice_audio(backing, tl.line_start, m / float(sr), sr)
        out[tl.go_sample:] = fit_len(seg, m)
    return out


def blit(out, data, offset):
    """
    Schreibt data an die Stelle offset in out - auch teilweise davor
    oder dahinter. Gibt zurueck, bis wohin geschrieben wurde.
    """
    data = np.asarray(data, dtype=np.float32)
    n = len(out)
    a = int(offset)
    if a >= n or a + len(data) <= 0:
        return 0
    src = max(0, -a)
    dst = max(0, a)
    m = min(len(data) - src, n - dst)
    if m <= 0:
        return 0
    out[dst:dst + m] = data[src:src + m]
    return dst + m


def place_chunks(chunks, go_time, n_samples, sr=SR):
    """
    Schneidet aus zeitgestempelten Eingabebloecken genau das Fenster ab
    LOS heraus. chunks: (Startzeit, Daten). Reine Rechnung, damit sich
    der Schnitt ohne Geraet pruefen laesst.

    Ein Block, der ueber LOS hinweggeht, wird in sich geteilt. Fehlende
    Samples bleiben Null - spaeteres Material rutscht nie nach vorn.
    """
    n = max(0, int(n_samples))
    out = np.zeros(n, dtype=np.float32)
    for start, data in chunks:
        blit(out, data, int(round((float(start) - float(go_time)) * sr)))
    return out


def timestamps_usable(chunks, sr, tol=0.25):
    """
    Laufen die ADC-Zeitstempel wirklich mit den Samples mit?
    Manche Backends liefern nur Nullen - dann taugen sie nicht als Uhr.
    """
    marks = [(float(t), int(i)) for (t, i, _d) in chunks if t]
    if len(marks) < 2:
        return False
    (t0, i0), (t1, i1) = marks[0], marks[-1]
    if t1 <= t0 or i1 <= i0:
        return False
    want = (i1 - i0) / float(sr)
    return abs((t1 - t0) - want) <= max(0.02, want * tol)


# ==========================================================================
#  Mikrofon / microphone
# ==========================================================================

def audio_backend():
    """Gibt das sounddevice-Modul zurueck oder None."""
    try:
        import sounddevice as sd
        return sd
    except Exception:
        return None


ENV_MS = 20          # Aufloesung der laufenden Huellkurve / live envelope


READY_CHUNKS = 2      # so viele Rueckrufe muessen vor dem Vorlauf da sein
READY_TIMEOUT = 1.5   # laenger warten wir nicht auf das Geraet
CAPTURE_GRACE = 0.6   # Wartezeit, bis der Eingang das Fenster nachgeliefert hat


def _stamp(time_info, name):
    """Zeitstempel aus dem PortAudio-Rueckruf, 0.0 wenn es keinen gibt."""
    try:
        return float(getattr(time_info, name))
    except Exception:
        return 0.0


class RecordingSession(object):
    """
    Ein Aufnahmeversuch mit Zeitstempeln / one timestamped attempt.

    Das Mikrofon laeuft schon, bevor ueberhaupt etwas zu hoeren ist. Der
    Schnitt am Ende richtet sich nach den ADC-Zeitstempeln von PortAudio -
    nicht danach, wann ein Rueckruf im Programm ankommt. Genau das ist der
    Unterschied, der die erste Silbe rettet.
    """

    def __init__(self, sd, timeline, monitor, sr=SR, device=None,
                 blocksize=0, env_ms=ENV_MS, log=None):
        self.sd = sd
        self.tl = timeline
        self.sr = int(sr)
        self.monitor = np.asarray(monitor, dtype=np.float32)
        self.device = device
        self.blocksize = int(blocksize)
        self.log = log
        self.duplex = False
        self.fallback = False          # ohne brauchbare ADC-Zeitstempel

        self._lock = threading.Lock()
        self._chunks = []              # (adc_time, frame_index, data)
        self._frames_in = 0
        self._arm_frame = None
        self._armed = False
        self._dac0 = None              # Streamzeit des ersten Monitor-Samples
        self._dac_lead = 0.0           # Vorlauf der Ausgabe vor dem Hoeren
        self._in_lat = 0.0             # Verzug des Eingangs
        self._wall0 = None
        self._out_pos = 0
        self._spent = False            # Monitor komplett ausgegeben
        self._closed = False

        self._stream = None            # Vollduplex oder Eingang
        self._out_stream = None
        self._clock = None             # Stream, dessen Uhr wir lesen

        self._env_step = max(1, int(self.sr * env_ms / 1000.0))
        self._env = []
        self._live = np.zeros(max(0, int(timeline.take_samples)),
                              dtype=np.float32)
        self._live_end = 0

    # ------------------------------------------------------------ Rueckrufe
    def _read_in(self, indata, frames, time_info):
        data = np.array(indata[:, 0], dtype=np.float32)
        adc = _stamp(time_info, "inputBufferAdcTime")
        with self._lock:
            idx = self._frames_in
            self._frames_in += int(frames)
            self._chunks.append((adc, idx, data))
            if self._armed and self._dac0 is not None:
                self._place_live(adc, idx, data)

    def _write_out(self, outdata, frames, time_info):
        outdata[:] = 0.0
        if not self._armed:
            return
        if self._dac0 is None:
            # Erster hoerbarer Block: hier beginnt die Zeitachse.
            self._dac0 = _stamp(time_info, "outputBufferDacTime")
            now = _stamp(time_info, "currentTime")
            self._dac_lead = (self._dac0 - now) if now else self._latency(1)
            with self._lock:
                self._arm_frame = self._frames_in
        a = self._out_pos
        b = min(len(self.monitor), a + int(frames))
        if b > a:
            outdata[:b - a, 0] = self.monitor[a:b]
        self._out_pos = a + int(frames)
        if self._out_pos >= len(self.monitor):
            self._spent = True

    def _on_duplex(self, indata, outdata, frames, time_info, status):
        # Ausgang zuerst: dann steht die Zeitachse, bevor der Eingang
        # desselben Blocks einsortiert wird.
        self._write_out(outdata, frames, time_info)
        self._read_in(indata, frames, time_info)

    def _on_input(self, indata, frames, time_info, status):
        self._read_in(indata, frames, time_info)

    def _on_output(self, outdata, frames, time_info, status):
        self._write_out(outdata, frames, time_info)

    # ------------------------------------------------------------ Aufbau
    def open(self):
        """Oeffnet das Mikrofon. Der Ausgang bleibt bis arm() stumm."""
        if self.sd is None:
            raise RuntimeError("sounddevice fehlt / missing")
        try:
            self._stream = self.sd.Stream(
                samplerate=self.sr, channels=(1, 1), dtype="float32",
                blocksize=self.blocksize, device=(self.device, None),
                callback=self._on_duplex)
            self._stream.start()
            self.duplex = True
        except Exception:
            # Manche Geraetepaare koennen kein Vollduplex - dann zwei
            # Stroeme, der Eingang trotzdem zuerst und durchgehend.
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
            self._stream = None
            kwargs = {"samplerate": self.sr, "channels": 1,
                      "dtype": "float32", "blocksize": self.blocksize,
                      "callback": self._on_input}
            if self.device is not None:
                kwargs["device"] = self.device
            self._stream = self.sd.InputStream(**kwargs)
            self._stream.start()
            self.duplex = False
        self._clock = self._stream
        return self

    def ready(self):
        """Sind genug Eingaberueckrufe da, um loszulegen?"""
        with self._lock:
            return len(self._chunks) >= READY_CHUNKS

    def wait_ready(self, timeout=READY_TIMEOUT, step=0.005):
        """Wartet begrenzt auf das Geraet. True, wenn es rechtzeitig kam."""
        end = time.perf_counter() + float(timeout)
        while time.perf_counter() < end:
            if self.ready():
                return True
            time.sleep(step)
        return self.ready()

    def arm(self):
        """
        Startet die Zeitachse: ab jetzt laeuft der Monitorton, und der
        erste ausgegebene Block legt den Nullpunkt fest.
        """
        if self._armed:
            return
        # Die Latenz jetzt merken - beim Schnitt sind die Stroeme zu.
        self._in_lat = self._latency(0)
        with self._lock:
            self.fallback = not timestamps_usable(self._chunks, self.sr)
            self._wall0 = time.perf_counter()
        if self.fallback:
            self._note("DubStage: ADC-Zeitstempel unbrauchbar - "
                       "Rueckfall auf Samplezaehler / falling back to "
                       "the input sample counter.")
        if not self.duplex:
            self._out_stream = self.sd.OutputStream(
                samplerate=self.sr, channels=1, dtype="float32",
                blocksize=self.blocksize, callback=self._on_output)
            self._armed = True
            self._out_stream.start()
            self._clock = self._out_stream
        else:
            self._armed = True

    # ------------------------------------------------------------ Ablauf
    def started(self):
        """Laeuft der hoerbare Teil schon?"""
        return self._armed and self._dac0 is not None

    def position(self):
        """
        Sekunden seit Beginn des Vorlaufs, gemessen an der Streamuhr.
        Das ist die Stelle, die der Spieler gerade hoert - danach richten
        sich Bild und Countdown.
        """
        if not self.started():
            return 0.0
        now = self._stream_time()
        if now is None:
            return max(0.0, time.perf_counter() - (self._wall0 or 0.0))
        return max(0.0, now - self._dac0)

    def _stream_time(self):
        try:
            return float(self._clock.time)
        except Exception:
            return None

    def go_time(self):
        """Zeitpunkt von LOS in derselben Uhr wie die Eingabestempel."""
        if self._dac0 is None:
            return None
        return self._dac0 + self.tl.lead_duration

    def _latency(self, which):
        """Ein- oder Ausgabelatenz des Geraets, 0.0 wenn unbekannt."""
        stream = self._out_stream if which and self._out_stream else self._stream
        try:
            lat = stream.latency
        except Exception:
            return 0.0
        try:
            return float(lat[which])
        except Exception:
            try:
                return float(lat)
            except Exception:
                return 0.0

    def go_frame(self):
        """
        Samplenummer von LOS im Eingabestrom - fuer den Rueckfall ohne
        Zeitstempel. Die Latenz beider Richtungen zaehlt mit: der Ausgang
        laeuft der Uhr voraus, der Eingang hinterher.
        """
        if self._arm_frame is None:
            return None
        delay = self.tl.lead_duration + self._dac_lead + self._in_lat
        return self._arm_frame + int(round(delay * self.sr))

    def captured_enough(self):
        """Liegt das behaltene Fenster vollstaendig als Eingabe vor?"""
        go = self.go_time()
        if go is None:
            return False
        with self._lock:
            if self.fallback:
                go_f = self.go_frame()
                return (go_f is not None
                        and self._frames_in >= go_f + self.tl.take_samples)
            if not self._chunks:
                return False
            stamp, _idx, data = self._chunks[-1]
            end = stamp + len(data) / float(self.sr)
        return end >= go + self.tl.take_duration

    def finished(self):
        """
        Ist der Versuch durch? Der Ton laeuft der Aufnahme um die Latenz
        voraus - deshalb zaehlt nicht nur die Position, sondern auch, ob
        der Eingang das Fenster schon ganz geliefert hat.
        """
        if self._closed:
            return True
        if not self.started():
            return False
        pos = self.position()
        if pos < self.tl.total_duration:
            return False
        return (self.captured_enough()
                or pos >= self.tl.total_duration + CAPTURE_GRACE)

    # ------------------------------------------------------------ Anzeige
    def _place_live(self, adc, idx, data):
        """Schreibt einen Block ins behaltene Fenster - nur ab LOS."""
        off = self._offset_of(adc, idx)
        if off is None:
            return
        end = blit(self._live, data, off)
        if end > self._live_end:
            self._live_end = end

    def _offset_of(self, adc, idx):
        if self.fallback:
            go_f = self.go_frame()
            return None if go_f is None else int(idx) - go_f
        go_t = self.go_time()
        return None if go_t is None else int(round((adc - go_t) * self.sr))

    def envelope(self):
        """
        Huellkurve des behaltenen Fensters. Beginnt bei LOS - was waehrend
        des Countdowns ins Mikrofon faellt, taucht hier nie auf.
        """
        with self._lock:
            step = self._env_step
            k = self._live_end // step
            have = len(self._env)
            if k > have:
                block = self._live[have * step:k * step].reshape(k - have,
                                                                 step)
                self._env.extend(np.abs(block).max(axis=1).tolist())
            return list(self._env), step / float(self.sr)

    def level(self):
        if not self._env:
            return 0.0
        return float(min(1.0, max(self._env[-5:]) * 1.4))

    # ------------------------------------------------------------ Abschluss
    def complete(self):
        """
        Schliesst die Stroeme und liefert genau das Fenster von LOS bis
        Clipende plus Nachlauf - immer dieselbe Laenge.
        """
        go = self.go_time()
        frames = int(self.tl.take_samples)
        self.close()
        with self._lock:
            chunks = list(self._chunks)
        if go is None:
            return np.zeros(frames, dtype=np.float32)
        if not self.fallback and not self._covers(chunks, go, frames):
            # Zeitstempel sahen brauchbar aus, treffen das Fenster aber
            # nicht - dann doch ueber den Samplezaehler schneiden.
            self._note("DubStage: ADC-Zeitstempel treffen das Fenster "
                       "nicht - Rueckfall auf Samplezaehler.")
            self.fallback = True
        if self.fallback:
            go_f = self.go_frame()
            if go_f is None:
                return np.zeros(frames, dtype=np.float32)
            pairs = [(go + (idx - go_f) / float(self.sr), data)
                     for (_t, idx, data) in chunks]
        else:
            pairs = [(t, data) for (t, _i, data) in chunks]
        return place_chunks(pairs, go, frames, self.sr)

    def _covers(self, chunks, go, frames):
        span = frames / float(self.sr)
        for (t, _i, data) in chunks:
            if t and t < go + span and t + len(data) / float(self.sr) > go:
                return True
        return False

    def cancel(self):
        """Versuch verwerfen / discard the attempt."""
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for stream in (self._out_stream, self._stream):
            if stream is None:
                continue
            for step in (stream.stop, stream.close):
                try:
                    step()
                except Exception:
                    pass
        self._out_stream = None
        self._stream = None

    def _note(self, text):
        if self.log:
            try:
                self.log(text)
                return
            except Exception:
                pass
        print(text)


class Mic(object):
    """Aufnahme, optional mit gleichzeitiger Wiedergabe des Backing Tracks."""

    def __init__(self, sr=SR):
        self.sr = sr
        self.sd = audio_backend()
        self._stream = None
        self._chunks = []
        self._env = []                                  # Spitzenwerte je Fenster
        self._env_step = max(1, int(sr * ENV_MS / 1000.0))
        self._buf = np.zeros(0, dtype=np.float32)
        self.session = None            # laufender Aufnahmeversuch

    @property
    def available(self):
        return self.sd is not None

    def devices(self):
        if not self.sd:
            return []
        out = []
        for i, d in enumerate(self.sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append((i, d.get("name", "?")))
        return out

    def start(self, playback=None, device=None):
        """playback: Float-Array, das waehrend der Aufnahme laufen soll."""
        if not self.sd:
            raise RuntimeError("sounddevice fehlt / missing")
        self._chunks = []
        self._env = []
        self._env_step = max(1, int(self.sr * ENV_MS / 1000.0))
        self._buf = np.zeros(0, dtype=np.float32)

        def cb(indata, frames, time_info, status):
            data = np.asarray(indata[:, 0], dtype=np.float32).copy()
            self._chunks.append(data)
            # Spitzenwert je 20-ms-Fenster mitschreiben, damit die
            # Oberflaeche die Aufnahme live zeichnen kann.
            buf = data if not len(self._buf) else \
                np.concatenate([self._buf, data])
            step = self._env_step
            k = len(buf) // step
            if k:
                blocks = buf[:k * step].reshape(k, step)
                self._env.extend(np.abs(blocks).max(axis=1).tolist())
                buf = buf[k * step:]
            self._buf = buf

        kwargs = {"samplerate": self.sr, "channels": 1, "callback": cb,
                  "dtype": "float32"}
        if device is not None:
            kwargs["device"] = device
        self._stream = self.sd.InputStream(**kwargs)
        self._stream.start()
        if playback is not None and len(playback):
            try:
                self.sd.play(np.asarray(playback, dtype=np.float32), self.sr)
            except Exception:
                pass

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        try:
            if self.sd:
                self.sd.stop()
        except Exception:
            pass
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._chunks).astype(np.float32)

    # -------------------------------------------------- Aufnahmeversuch
    def open_session(self, timeline, monitor, device=None, log=None):
        """
        Oeffnet Mikrofon und Monitorton fuer einen Versuch. Der Eingang
        laeuft ab hier durchgehend - bei LOS wird nichts mehr gestartet.
        """
        if not self.sd:
            raise RuntimeError("sounddevice fehlt / missing")
        self.cancel_session()
        session = RecordingSession(self.sd, timeline, monitor, sr=self.sr,
                                   device=device, log=log)
        session.open()
        self.session = session
        return session

    def finish_session(self):
        """Beendet den Versuch und liefert das behaltene Fenster."""
        session, self.session = self.session, None
        if session is None:
            return None
        return session.complete()

    def cancel_session(self):
        """Bricht einen laufenden Versuch ab und verwirft ihn."""
        session, self.session = self.session, None
        if session is not None:
            try:
                session.cancel()
            except Exception:
                pass

    def envelope(self):
        """Bisher aufgenommene Huellkurve und ihre Schrittweite in Sekunden."""
        if self.session is not None:
            return self.session.envelope()
        return list(self._env), self._env_step / float(self.sr)

    def level(self):
        """Aktueller Pegel 0..1 fuer die Aussteuerungsanzeige."""
        if self.session is not None:
            return self.session.level()
        if not self._env:
            return 0.0
        return float(min(1.0, max(self._env[-5:]) * 1.4))

    def play(self, data):
        if not self.sd or data is None or not len(data):
            return
        try:
            self.sd.play(np.asarray(data, dtype=np.float32), self.sr)
        except Exception:
            pass

    def stop_play(self):
        try:
            if self.sd:
                self.sd.stop()
        except Exception:
            pass
