# DubStage Recording Timing Redesign

## Goal

Make recording feel immediate, predictable, and forgiving. The player should be
able to listen to the scene context, follow a real three-second countdown, begin
speaking on GO, and reliably capture the first syllable. The saved take must line
up with the clip boundary created in DubForge without requiring manual offsets.

This change is limited to recording timing and contextual playback. It does not
add session saving, scoring, automatic performance alignment, or changes to clip
detection.

## Confirmed experience

For every recording attempt, DubStage follows this sequence:

1. Open and start the microphone before any lead-in playback begins.
2. Start the scene lead-in from the previous clip's beginning where practical.
3. Cap the contextual lead-in at five seconds.
4. Guarantee at least three full countdown seconds. Start earlier in the scene
   when the previous clip provides less than three seconds of context.
5. If the video itself has fewer than three seconds before the target clip,
   prepend the missing time using the earliest available video frame and silence.
6. Play normal scene audio during the lead-in, including preceding dialogue.
7. Draw `3`, `2`, and `1` over the moving video for exactly one second each.
8. Show `GO` at the precise DubForge clip boundary and clear it quickly. There is
   no pause or frozen GO screen.
9. From GO onward, mute all original dialogue and play only the backing track.
10. Continue video, background playback, and recording until 1.5 seconds after
    the clip ends.
11. Automatically keep only the interval from GO through clip end plus the
    1.5-second grace period. Discard microphone audio captured during lead-in.
12. Return to the line screen and wait. Do not replay automatically.

The existing **Original** action continues to play only the original clip. The
existing **My take** action continues to play only the saved take and its grace
period, with the background track. It does not replay the contextual lead-in.

## Current defect

The current countdown advances every 650 ms, shows GO, waits another 300 ms, and
only then opens the microphone. Starting an input stream can add another variable
delay. Background playback starts after the input stream, and the video uses a
separate `perf_counter()` clock. These independently started clocks explain both
reported symptoms:

- GO appears to stick before the scene starts.
- Speech begun on GO occurs before the microphone is ready, cutting the first
  syllable or shifting the take relative to the video.

The correction must remove microphone startup from the cue boundary. Adjusting
the existing timer constants alone would remain device-dependent and is not an
acceptable fix.

## Timing model

### One recording timeline

Create one explicit timeline for a recording attempt. All positions are sample
indices at DubStage's working rate (`44,100 Hz`), with the target clip boundary
as logical time zero.

```text
                 normal original audio             background only
        |-------------------------------------|--------------------------|
        lead-in start                 3   2   1  GO                 stop
                                                   0       clip end + 1.5 s

microphone:  [ open and capturing throughout ................................ ]
saved take:                                      [ exact retained window ------ ]
```

Represent the calculated attempt with a small immutable value object in
`dubstage_core.py`, containing:

- `media_start`: first real source timestamp used for the lead-in;
- `front_pad`: artificial still-frame/silence duration when the source lacks
  three seconds before the line;
- `lead_duration`: logical duration before GO, including `front_pad`;
- `line_duration`;
- `tail_duration`, fixed at `1.5` seconds;
- `total_duration`;
- sample indices for countdown boundaries, GO, clip end, and stop.

Calculate the lead-in as follows:

- Prefer the previous clip's start.
- Never begin more than five seconds before the target clip.
- Begin at least three seconds before the target when that much source exists.
- Clamp the real media start to zero.
- Add front padding until the logical lead duration is exactly three seconds if
  the source does not contain enough earlier footage.
- For the first line, use the start of the video as context and the same padding
  rule.

Overlapping or unusually close clips do not shorten the countdown. The playback
may begin before the previous clip when necessary to preserve three full seconds.

### Audio is the master clock

Use the sound device's monotonic stream time and callback frame positions as the
source of truth for recording progress. UI timers may request repaints, but they
must not decide when audio begins, when GO occurs, or which samples are retained.

The video renderer derives its frame timestamp from the recording timeline:

- during artificial front padding, show the earliest available source frame;
- during real lead-in, show `media_start + elapsed - front_pad`;
- at GO, show the target line start;
- afterward, continue forward through the 1.5-second tail.

If a Tk callback runs late, it skips to the correct current frame and countdown
state. It must never extend a number or GO and thereby move the cue.

### Pre-opened microphone and exact cropping

Extend `dubstage_core.Mic` with a recording-session API rather than calling the
current `start()` at GO:

- Open the input stream before lead-in playback.
- Allow enough startup callbacks to arrive before starting the visible/audio
  timeline, with a short bounded readiness timeout.
- Keep timestamped input chunks using PortAudio's `inputBufferAdcTime`.
- Mark the GO time in the same PortAudio timebase used by the input callbacks.
- At completion, extract exactly the samples whose ADC timestamps overlap the
  interval `[GO, GO + line duration + 1.5 seconds]`.
- Pad a short device underrun with zeros instead of shifting later audio earlier.
- Return an array with a deterministic length of
  `round((line duration + 1.5) * sample_rate)` samples.

This safety capture includes the countdown internally but never stores it in
`Line.take`. It handles callback buffers that straddle GO by slicing within that
buffer. It also avoids treating callback arrival time as sample time.

If the host audio backend does not supply usable ADC timestamps, fall back to a
sample counter established after the input stream reports ready. Log that the
fallback was used. Because the stream is already running for at least three
seconds, even the fallback cannot lose the first callback at GO.

## Playback construction

Load the video's complete original mono audio as part of `load_pack_audio`, in
addition to the per-line clips and optional backing track. This is required for
natural contextual lead-in playback.

Build one sample-accurate monitor buffer for the attempt:

- artificial front pad: silence;
- real lead-in: original scene audio from `media_start` up to the target start;
- target line and tail: matching backing-track samples;
- if no backing track exists: silence from GO through the tail.

Start this monitor buffer only after the microphone stream reports ready. Avoid
stopping or recreating the microphone at the transition to GO.

Prefer a full-duplex `sounddevice.Stream` when the selected input and default
output device can share it. Its callback writes the monitor samples and records
the input under one stream clock. Provide a compatible two-stream fallback for
device combinations that cannot open full duplex:

- input still opens first and remains continuous;
- output uses its own callback-based stream rather than `sounddevice.play()`;
- both callbacks retain their PortAudio timestamps;
- the recording timeline begins from the first scheduled output DAC sample;
- input is cropped against that timestamp.

Do not use `sounddevice.play()` for the recording attempt, because its convenience
API hides the output callback timing needed for synchronization.

## DubStage state changes

Replace the current `countdown -> record` handoff with a single recording-attempt
state that owns the whole lead-in, cue, target, and tail. Suggested substates are
informational only:

```text
idle -> preparing microphone -> lead-in/countdown -> active take -> tail -> idle
```

Required behavior:

- `do_record()` computes an immutable timeline and starts microphone preparation.
- Keep controls disabled through the complete attempt.
- Once the microphone is ready, start monitor audio and video together.
- Derive the displayed number from time remaining until GO:
  - `(2, 3]` seconds: `3`
  - `(1, 2]` seconds: `2`
  - `(0, 1]` seconds: `1`
- At zero, mark the retained capture boundary and show GO briefly without delaying
  audio or video.
- Clear GO after approximately 250 ms as a visual action only.
- Finish at `line duration + 1.5` seconds after GO, crop the timestamped capture,
  assign it to `line.take`, and return to idle.
- The live waveform begins at GO and displays only retained-window samples. Audio
  captured during countdown must not appear in the comparison strip.
- The playhead uses time relative to GO during the target and tail. During lead-in,
  it may stay at the left edge because the comparison strip represents the take.

Cancellation, Escape, window close, pack changes, and watchdog cleanup must close
both input and output streams and discard the unfinished attempt. A device error
must return the UI to idle and preserve the previous successful take.

The watchdog deadline covers microphone readiness, lead-in, line duration, tail,
and a small recovery margin. It must not use the current three-second assumption.

## Public interfaces and stored data

Add or refine these internal interfaces in `dubstage_core.py`:

- `RecordingTimeline` value object and a pure
  `recording_timeline(pack, line_index, max_lead=5.0, countdown=3.0, tail=1.5)`
  calculator;
- a helper that constructs the monitor audio from original/backing tracks;
- a timestamped recording session exposed through `Mic`, with readiness,
  progress, GO marking, live retained envelope, completion, and cancellation;
- a pure timestamp-to-sample extraction helper for deterministic testing.

Keep `Line.take` as a mono `float32` array. Its semantic duration changes from
"whatever the stream returned" to exactly `line.duration + 1.5 seconds`. No pack
format or migration is required because takes currently exist only in memory.

Set the recording tail constant to `1.5`. Keep the countdown and lead-in cap as
named constants rather than user settings for this iteration.

## Tests

### Pure timing tests

- A normal middle line starts from the previous clip and receives a three-second
  countdown ending exactly at its clip boundary.
- A distant previous clip produces no more than five seconds of lead-in.
- A close previous clip causes playback to begin earlier than that clip so the
  countdown still has three full seconds.
- A first line after four seconds uses video from timestamp zero without padding.
- A first line at one second gets two seconds of still-frame/silence padding.
- Each countdown number occupies exactly one second on the logical timeline.
- GO equals the target clip timestamp and does not alter timeline progress.
- Stop equals GO plus line duration plus exactly 1.5 seconds.

### Capture-alignment tests

- Input callbacks beginning before, on, and after GO are sliced at the correct
  sample within the straddling callback.
- Different input/output latency values preserve the GO-aligned first sample.
- Countdown microphone data never appears in `Line.take`.
- A take contains exactly the required sample count.
- Missing input samples become silence without shifting recorded speech.
- The timestamp-free fallback retains the first GO callback because input is
  already warm.
- Device startup delays affect only preparation time, never the GO boundary.

### Playback and UI tests

- Lead-in audio uses the original scene and changes to backing-only at GO with no
  sample gap.
- The 1.5-second tail remains backing-only even if another original character
  speaks during that interval.
- Without a backing track, audio after GO is silent while video continues.
- Video advances during every countdown number and GO.
- A delayed UI repaint jumps to the correct number/frame instead of lengthening
  the countdown.
- The microphone is ready before lead-in audio starts.
- A failed microphone open shows an error without starting countdown or replacing
  a previous take.
- **Original** remains clip-only; **My take** remains take-only; completing a take
  waits for user input.
- Escape and watchdog recovery close streams and restore every control.

Use a fake audio backend with controllable ADC/DAC clocks and callback sizes for
automated tests. Add a manual latency check using a wired headset: play or display
a sharp GO cue, make a short click exactly on it, and verify that the captured
transient begins at take time zero within one audio callback (target: 20 ms or
better). Repeat at least ten times and report the maximum spread, since consistency
matters more than one lucky attempt.

## Acceptance criteria

- The microphone is active and has delivered at least one callback before the
  lead-in starts.
- `3`, `2`, and `1` each last one real second over continuously moving video.
- GO occurs at the target clip boundary without a 300 ms handoff or any frozen
  frame.
- Speaking on GO does not lose the initial consonant or syllable.
- Repeated wired-headphone attempts align consistently to within one callback,
  with a 20 ms target.
- Saved takes begin at GO and last through exactly 1.5 seconds after clip end.
- Countdown speech is discarded automatically.
- Original scene audio is heard during context; original dialogue is absent from
  GO through the complete grace period.
- Existing playback, finale rendering, pack compatibility, and microphone test
  continue to work.

## Documentation update

Update the English and German manuals after implementation. Describe the contextual
lead-in, real-time countdown, pre-opened microphone, background-only target window,
and 1.5-second grace period. Remove the current wording that implies recording
begins only after the countdown finishes, and state that countdown audio is
captured only as a synchronization buffer and discarded automatically.
