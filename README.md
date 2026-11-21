# Capture

Capture is a VHS capture project for Fedora. Record a lossless master once, then make viewing copies from that file. The repository includes a synthetic preview check and a PAL USB-Live2 recorder that saves timing, content and shutdown evidence for hardware verification.

**Hardware verification: PENDING.** No USB-Live2 hardware is currently available. The completed checks use generated input; sustained USB recording, actual field order, physical audio sync and device shutdown still need the PAL composite checklist below.

## Try the live preview

On Linux, use Python 3 with PySide6 and put both `ffmpeg` and `ffprobe` on `PATH`. The offscreen check and regression tests have been run on Fedora 44 x86_64 with Python 3.14.10, PySide6 6.11.2 (`python3-pyside6-6.11.2-1.fc44`) and FFmpeg/ffprobe 8.1.3 (`ffmpeg-free-8.1.3-3.fc44`). Other builds must provide the capabilities below; the version number alone is not enough.

Before opening Qt or starting a capture, the script checks for missing capabilities and names them in its error message. A successful startup prints both FFmpeg and ffprobe versions. Required build features are:

- The `lavfi` input device and Matroska demuxing in FFmpeg; Matroska demuxing and JSON packet/stream reporting in ffprobe.
- The `ffv1`, `pcm_s16le` and `rawvideo` encoders/decoders, plus `wrapped_avframe` and `pcm_f64le` decoding for generated input.
- The `matroska`, `fifo`, `rawvideo`, `framehash` and `s16le` muxers; `file` and `pipe` protocols; `yuv422p` and `rgb24` pixel formats.
- The `smptebars`, `color`, `format`, `drawbox`, `scroll`, `overlay`, `tinterlace`, `setsar`, `aevalsrc`, `split`, `yadif`, `fps`, `scale`, `aresample`, `aformat`, `anull` and `null` filters.
- `-fps_mode`, `-readrate`, `-readrate_initial_burst`, `-readrate_catchup` and `-copyts`; FFV1 level 3/slice CRC; FIFO queue/overflow controls; and Matroska cluster timing, packet flushing and interleaving controls.

From the repository root, run

```sh
python3 examples/preview_check.py
```

A 320×240 window shows PAL colour bars, a moving marker, a flashing cue and a small binary frame counter. The picture deliberately freezes from second two to second five to simulate a viewer that falls behind. Leave the window open. Once recording finishes, it closes and prints the comparison result. The check uses a generated signal with stereo audio and cleans up its temporary captures automatically.

For a machine without a display, run

```sh
QT_QPA_PLATFORM=offscreen python3 examples/preview_check.py
```

## How the viewer shares the capture

FFmpeg reads the input once and splits the video into two outputs.

| Output | What gets written |
| --- | --- |
| Lossless master | Full 720×576, 25 fps interlaced PAL video at 4:3. FFV1 level 3 with slice CRC preserves the 4:2:2 picture, and stereo PCM preserves the 48 kHz sound. |
| Live viewer | The top-field-first signal is deinterlaced with YADIF before reducing it to a 320×240, 12.5 fps RGB picture. A two-packet FIFO drops preview frames when the viewer falls behind. The PySide6 window reads complete frames from this output. |

The test generates the same eight seconds twice, once with the preview off and once with it on. To pass:

- Both masters must match on all 200 decoded video frames and all 384,000 stereo sample pairs. ffprobe also compares every stored audio and video packet's PTS, DTS and duration, converted with exact rational time bases to seconds on the same timeline. Neither track is reset to zero, so an audio offset, a video offset or a shift of both tracks fails even when decoded content matches.
- The master must advance during the viewer stall. Short, flushed Matroska clusters let ffprobe observe packets already written to the file at approximately seconds 2, 3, 4 and 5 while preview reads remain stopped. Both tracks must advance at wall-clock speed in each interval and across the whole stall, with a 0.5-second tolerance for muxing and scheduling. Completing the right file later is insufficient.
- Preview sequences must increase and contain gaps. The binary counter is embedded in the RGB packet before the FIFO, so dropped frames also drop their identifiers. Real-time input pacing uses a one-microsecond initial burst (zero selects FFmpeg's half-second default on the tested build) and disables accelerated catch-up. The sequence identifies source time. Old queued pictures are drained on resume; a frame newer than the actual resume time (plus one frame of rounding margin) must arrive within 1.5 seconds. Frames must also have appeared before the stall.

FFmpeg may log `FIFO queue full` when the preview reader pauses. The script reports measured master progress and the source time of fresh recovery before printing `PASS`. A slow or heavily loaded machine can fail the timing bounds; such a run has not demonstrated uninterrupted capture. See FFmpeg's [timestamp and input pacing options](https://ffmpeg.org/ffmpeg.html), [Matroska muxer controls](https://ffmpeg.org/ffmpeg-formats.html#matroska-2), and [ffprobe packet reporting](https://ffmpeg.org/ffprobe.html) for the underlying interfaces.

## Regression checks

Run the checks without a display:

```sh
python3 -B -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python3 -B examples/preview_check.py
```

The regression suite checks delayed audio with unchanged PCM, video offsets smaller than a frame, shared timeline shifts, altered decoded content, stale/late preview recovery, real generated sequence tags and missing-capability diagnostics. Generated-input capture regressions also cover audio ending before the stop request, Ctrl+C shortening a fixed-duration run, delayed session setup, and graceful manual shutdown. Unit checks distinguish actual forward/backward clock jumps from setup delay and keep cleanup time out of duration completion. A Linux integration check suspends FFmpeg during the stall, resumes it and confirms that the final master still matches while the progress check rejects the recording pause. FFmpeg-dependent cases are skipped if the tools are absent; the Qt offscreen command is the separate end-to-end check. No CI is configured.

## USB-Live2 capture: PAL composite

`examples/hardware_capture.py` records 720×576 PAL at 25 fps with stereo 48 kHz PCM. It opens the selected V4L2 and ALSA devices once, preserves the interlaced master, and feeds the deinterlaced viewer from that session. Composite input index 0 is the default. This version supports PAL; verify the actual input index before the first run.

Hardware capture additionally needs FFmpeg's `video4linux2` and `alsa` inputs, the `idet` filter, `stdbuf` from coreutils, and `v4l2-ctl` from v4l-utils. ALSA device discovery uses `arecord` from alsa-utils. On Fedora, install missing utilities with `sudo dnf install v4l-utils alsa-utils coreutils`. PySide6 is required for the window; `--headless` exercises the same preview transport without a window.

Once the equipment is connected and a PAL tape is playing:

```sh
python3 examples/hardware_capture.py --list-devices
arecord -l
v4l2-ctl --list-devices
v4l2-ctl --device /dev/video0 --list-inputs --list-formats-ext --list-standards
```

Choose the USB-Live2 video path and its matching ALSA capture card. Prefer the reported `/dev/v4l/by-id/…` video alias and an ALSA card name over numbers that can change after reconnecting. The Linux kernel lists USB-Live2 as a [cx231xx device, USB ID 2040:c200](https://docs.kernel.org/admin-guide/media/cx231xx-cardlist.html). The script records the device inventory in each session; it requires explicit video and audio selections so it cannot silently choose a microphone.

Replace the two placeholder device values below with the discovered values. The output directory must be new:

```sh
python3 examples/hardware_capture.py \
  --video /dev/v4l/by-id/YOUR_USB_LIVE2_VIDEO_DEVICE \
  --audio hw:CARD=YOUR_USB_LIVE2_CARD,DEV=0 \
  --input 0 --duration 60 --review-clips \
  --output captures/pal-first
```

The viewer pauses its reads for three seconds after ten seconds, then once per minute. The **Stop and validate** button, closing the window, Ctrl+C, SIGTERM and the duration limit all request graceful FFmpeg finalization. Preview reads resume during shutdown even if Stop arrives during a stall. A ten-second shutdown timeout triggers termination, then a two-second timeout triggers a kill; either escalation prevents a successful clean-shutdown result. Validation runs after capture finishes, and can take time for a long recording.

After the short run, repeat with `--duration 1800 --output captures/pal-30min` for a thirty-minute run, using the same device selections. `--duration 0` records until stopped. `--field-order tff` or `--field-order bff` changes preview deinterlacing only; use the order established by the moving-picture review below. The default `auto` follows the incoming field flags. The master retains its original fields. Audio is available in the master and review clips; the live window displays video.

## What a hardware session checks

The recorder uses [V4L2 absolute timestamps](https://ffmpeg.org/ffmpeg-devices.html#video4linux2_002c-v4l2) and ALSA capture timestamps on a common wall-clock timeline. `-copyts` preserves their relationship; a single session origin is subtracted from both outputs. It does not independently reset the tracks or stretch audio to hide drift. System clock jumps invalidate the timing check.

| Evidence | Purpose |
| --- | --- |
| `master.mkv` | The retained FFV1 level 3 / slice CRC and stereo PCM master. Never deleted automatically. |
| `source.framehash`, `decoded.framehash` | Compare input-side decoded frames and PCM packets with a complete decode of the saved master, including SHA-256 content, packet counts, PTS, DTS and duration. Up to 1 ms of timestamp rounding is allowed for Matroska. |
| `packets.jsonl`, `observer.log` | A single ffprobe reads bytes as they are written to the growing master. Timestamp observations prove on-disk progress independently of the preview and input audit. Every stored packet's PTS, DTS and duration is also checked against the input audit, separately from decoded timing. Sequential reading keeps memory and per-second work bounded on long tapes. |
| `report.json` | Stall progress/recovery, clean-exit status, capture duration to the stop request, duration completion, input timestamp discontinuities, sample counts, clock span errors, A/V endpoint coverage and field-order evidence. `elapsed_seconds` also includes shutdown/observer cleanup; it does not determine duration completion. |
| `session.json`, `capture.log`, `decode.log`, `master-metadata.json` | Device inventory, exact command, clock origin, FFmpeg build/logs, full decode results and finalized container/stream metadata. |
| `review/` with `--review-clips` | Five-second clips from the start, middle and end, each bob-deinterlaced to 50 fps in both TFF and BFF order, with audio. The master stays intact. |

During every completed stall, both master tracks must advance at wall-clock speed with the synthetic check's 0.5-second tolerance. Preview packets carry a 32-bit source timestamp in milliseconds, so the tag supports sustained sessions. Recovery must show an actual gap in the received feed and a source timestamp newer than the resume time, within 1.5 seconds. Queued old frames are drained without counting as recovery. Outside stalls, the observer also checks that both tracks remain active and no more than two seconds behind the capture clock.

Final validation fails on content loss, a timestamp change beyond Matroska rounding, input timestamp discontinuities (more than 20 ms video or 5 ms audio cadence error), more than 100 ms difference between the tracks' clock span errors, or logged input overruns/timestamp corruption. Both tracks must also extend to within 0.5 seconds of the stop request on the shared source timeline. This allowance covers final packet boundaries and capture buffering; an earlier audio or video EOF fails even when every accepted packet matches its audit. The check preserves the original A/V offset and reports each track's lag at stop.

Matching the input audit proves preservation of the frames and samples FFmpeg accepted; it cannot prove that a tape or device supplied every original field. Stopping before a nonzero `--duration`, or during an unfinished stall, is **INCOMPLETE** when there are no verification errors, even when the master finalized correctly. Shutdown and validation time cannot complete the requested recording duration. With `--duration 0`, a manual stop after a successful stall can **PASS**. Exit status is zero only for **PASS**; **FAIL** and **INCOMPLETE** return one. All available evidence remains in the session directory.

### Field order and physical audio sync

An automatic **PASS** does not assert physical audio sync or correct field order. The recorder always writes `hardware_verified: false`; document any future hardware findings separately with the session evidence. `idet` counts and stored field metadata help choose what to inspect; a static picture, repeated fields or noisy VHS can make them inconclusive.

1. Use a moving scene and compare the TFF/BFF review clips frame by frame. Choose the order with steady forward motion; the wrong order tends to step backward every other field. Confirm at the beginning, middle and end before selecting the preview parity for subsequent sessions.
2. Use a known simultaneous picture/sound cue, preferably a flash and click test signal, at the beginning, middle and end. Measure the audible cue against the visual cue in the master or review clips. Record the offset and whether it grows. Packet start/end offsets are timing diagnostics, not measurements of lip sync; start/stop buffering can make endpoint offsets differ.
3. Verify `clean_exit: true`, no termination/kill escalation, no decode errors and complete input/master audit agreement. Exercise the duration limit, Stop/window close and Ctrl+C, including a Stop during a preview stall. Keep the resulting reports with the tapes' session records.

### PAL composite USB-Live2 test checklist — PENDING

All items below remain unchecked until equipment is available. Use a playing PAL source with motion and known simultaneous picture/sound cues. Keep a separate, new output directory for each run and record the source, device selections, FFmpeg build, results and any measurement uncertainty in that session's review notes.

- [ ] **Connect and identify the inputs.** Connect composite video and the matching left/right audio to USB-Live2. Use the discovery commands above to confirm the video path, ALSA capture card, PAL/YUYV support and composite input index; verify index 0 rather than assuming it. Check output storage and device access before recording.
- [ ] **Complete the 60-second first capture.** Run the `pal-first` command above with `--review-clips`. Confirm a stable picture, moving detail and both audio channels in the saved master or clips. Require `automatic_checks: pass`, `duration_completed: true`, `errors: []` and `clean_exit: true`; the stall at second 10 must recover with a fresh frame.
- [ ] **Establish field order.** Compare the TFF and BFF 50 fps clips frame by frame using motion. Record which order gives steady forward movement, then confirm it at the start, middle and end of the longer recording. Treat driver metadata and `idet` as supporting evidence. Leave this item pending if motion is insufficient or the result is inconclusive. Set `--field-order tff` or `--field-order bff` for subsequent previews only after this review; the master fields remain intact.
- [ ] **Complete sustained recording.** Run the 30-minute command below without stopping early. Require at least 1,800 seconds in `capture_seconds`, `duration_completed: true`, all 30 scheduled stalls recovered, and `automatic_checks: pass`. For every stall, both tracks must advance through the pause and a source timestamp newer than `cutoff_ms` must recover within 1.5 seconds. Check zero timestamp discontinuities, matching content audits, stored/decoded timing errors within 1 ms, relative clock-span error within 100 ms, and each track's `end_lag_at_stop_seconds` no greater than 0.5 seconds. Inspect logs for input errors and the saved picture/sound for missing or repeated content.
- [ ] **Measure physical audio sync.** Set the acceptable picture/sound offset and drift tolerance before testing. Measure a known simultaneous cue near the start, middle and end of the master, recording `audio cue time - video cue time` in milliseconds; positive values mean audio is late. Record the change from start to end and the measurement precision. Use review clips only where they contain suitable cues; otherwise inspect the corresponding master section. Missing or ambiguous cues leave sync pending. The synthetic 120 ms offset and packet endpoint offsets are not hardware lip-sync measurements.
- [ ] **Exercise clean shutdown.** Perform each case in the table below using separate session directories. Wait for final validation to finish, then require `clean_exit: true`, `stop.terminated: false`, `stop.killed: false`, `errors: []`, a finalized master that decodes completely, and complete input/master audit agreement. Confirm both tracks cover the stop request. An expected **INCOMPLETE** duration/recovery result can still demonstrate clean shutdown.
- [ ] **Retain and review the evidence.** Keep the master, both framehash audits, packet observations, logs, reports and TFF/BFF review clips. Record field-order and cue measurements separately, with the shutdown case and observed result for each run. Leave untested, failed or inconclusive items pending; an automatic **PASS** alone does not complete hardware verification. Preserve the recorder's `hardware_verified: false` field.

For the sustained test, replace the device placeholders and use the confirmed composite index. This requests three-second stalls at seconds 10, 70, …, 1750 and saves start/middle/end review clips:

```sh
python3 examples/hardware_capture.py \
  --video /dev/v4l/by-id/YOUR_USB_LIVE2_VIDEO_DEVICE \
  --audio hw:CARD=YOUR_USB_LIVE2_CARD,DEV=0 \
  --input 0 --duration 1800 --stall-after 10 --stall-every 60 --review-clips \
  --output captures/pal-30min
```

| Shutdown case | Run conditions | Expected result when all other checks succeed |
| --- | --- | --- |
| Automatic duration limit | Let the 60-second and 30-minute runs reach their limits | **PASS**, clean exit, `stop.reason: duration reached` |
| Stop button, window close, Ctrl+C, SIGTERM | Test each separately with `--duration 0`; stop after a completed stall and recovery. Send SIGTERM only to the recorder's identified PID. | **PASS**, clean exit |
| Stop during a preview stall | Use `--duration 0`; stop while preview reads are paused, for example during seconds 10–13 | **INCOMPLETE** with `interrupted_stall`, clean exit; no claim of completed recovery |
| Early stop of a fixed-duration run | Use `--duration 60`; stop after the first recovery but before second 60 | **INCOMPLETE**, `duration_completed: false`, clean exit; cleanup time must not satisfy the duration |

**PASS** returns recorder exit status 0. Expected **INCOMPLETE** cases return 1; judge their shutdown result from the report rather than treating that exit status as a shutdown failure. FFmpeg's reported status 255 is accepted after the recorder's graceful SIGINT; escalation or validation errors still prevent a pass.

### Exercise the hardware path before connecting equipment

```sh
python3 examples/hardware_capture.py --synthetic --headless \
  --duration 32 --stall-after 2 --stall-every 10 --review-clips \
  --output captures/synthetic-hardware-path
```

This uses the same recorder, growing-file observer, retained-session audit and shutdown path, with an intentionally delayed 120 ms audio track to test timing preservation. Omit `--headless` to exercise the Qt viewer, or set `QT_QPA_PLATFORM=offscreen` without that flag for offscreen Qt verification.

On the Fedora build above, a 32-second synthetic run passed three stalls, preserved 804 video frames and 1,537,024 stereo sample pairs, retained the 120 ms audio offset, and generated all six 50 fps review clips. A retained copy is in `captures/synthetic-validation/`, including a separate stored-timestamp recheck. Offscreen Qt startup and graceful stop during a stall were also exercised. Regression tests cover the original synthetic check and the capture/audit/shutdown path.

### Completed 30-minute synthetic soak — 21 November 2026

The full headless soak and a separate verification of the saved evidence both **PASSED** on the Fedora build above. It used generated input only, with three-second preview stalls once per minute starting at second 10. It did not open capture hardware or test the Qt window for 30 minutes.

| Check | Verified result |
| --- | --- |
| Recording duration | 1,800.004 seconds before the stop request; requested duration completed |
| Repeated stalls | All 30 passed; both master tracks advanced during every pause (minimum 2.960 seconds video / 2.965 seconds audio across each three-second stall) |
| Fresh preview recovery | All 30 passed; slowest recovery 245.416 ms against a 1.5-second deadline; every recovered frame was newer than its cutoff |
| Content preservation | All 45,014 video frames and 84,394 audio packets matched their input hashes; 86,419,456 stereo sample pairs retained |
| Timestamp preservation | Decoded PTS, DTS and durations matched exactly; maximum stored timing error was 0 ms video / 0.333333 ms audio, within the 1 ms allowance |
| Shared timeline | Intentional 120 ms audio start offset preserved; zero discontinuities and zero relative clock-span error |
| End coverage and shutdown | Both tracks covered the stop request; `clean_exit: true`, no termination/kill escalation, no reported errors; shutdown and observer cleanup took 0.545 seconds |

Packets flushed during graceful finalization extend slightly beyond the 1,800-second stop request. Cleanup time did not count toward recording duration. Generated-frame TFF detection is evidence about the synthetic signal only; physical field order and audio sync remain pending.

Retained local evidence: [review summary](/home/BigBen_Kenobi/Documents/ChatGPT/Capture/captures/synthetic-soak-30min-2026-11-21/SUMMARY.md), [recorder report](/home/BigBen_Kenobi/Documents/ChatGPT/Capture/captures/synthetic-soak-30min-2026-11-21/retry/session/report.json), [independent verification](/home/BigBen_Kenobi/Documents/ChatGPT/Capture/captures/synthetic-soak-30min-2026-11-21/retry/verification.json), and [run metadata/command](/home/BigBen_Kenobi/Documents/ChatGPT/Capture/captures/synthetic-soak-30min-2026-11-21/retry/run.json). The master, audits, logs, source snapshot and checksum manifest are also retained. `captures/` is ignored by Git, so these files are not included in a fresh checkout.

The initial launch was interrupted after roughly 21 minutes and did not finalize a shutdown report; it is retained as [incomplete evidence](/home/BigBen_Kenobi/Documents/ChatGPT/Capture/captures/synthetic-soak-30min-2026-11-21/interruption.json). The passing result above is from a fresh, continuous 30-minute run under a persistent runner, stored in `retry/`. It does not combine the two attempts. **Hardware verification remains PENDING**, with `hardware_verified: false`.
