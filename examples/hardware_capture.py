"""Record a PAL USB-Live2 session and retain evidence for hardware verification."""

import argparse
from dataclasses import dataclass
from fractions import Fraction
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

try:
    from . import preview_check as preview
except ImportError:
    import preview_check as preview


TAG_BITS = 32  # Milliseconds from one session origin; does not wrap during a tape.
TIME_TOLERANCE = Fraction(1, 1000)  # Matroska's millisecond time base.
STALL_SECONDS = 3
RECOVERY_SECONDS = 1.5
END_LAG_SECONDS = 0.5  # Allow final packet boundaries and capture buffering, not a missing tail.


@dataclass(frozen=True)
class CaptureClock:
    wallclock: float
    monotonic: float

    @classmethod
    def start(cls):
        before = time.monotonic()
        wallclock = time.time()
        after = time.monotonic()
        return cls(wallclock, (before + after) / 2)

    def wallclock_at(self, monotonic):
        return self.wallclock + (monotonic - self.monotonic)

    def changed(self):
        return abs(time.time() - self.wallclock_at(time.monotonic())) > 0.5


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def inventory():
    result = {"video": [], "alsa_cards": "", "alsa_pcm": ""}
    for entry in sorted(Path("/sys/class/video4linux").glob("video*")):
        device = Path("/dev") / entry.name
        aliases = [str(path) for path in Path("/dev/v4l/by-id").glob("*")
                   if path.resolve() == device.resolve()]
        result["video"].append({"device": str(device), "name": (entry / "name").read_text().strip(),
                                "stable_paths": aliases})
    for key, path in (("alsa_cards", "/proc/asound/cards"), ("alsa_pcm", "/proc/asound/pcm")):
        if Path(path).exists():
            result[key] = Path(path).read_text()
    return result


def preflight(args):
    preview.check_capabilities(hardware=not args.synthetic, analysis=True)
    if not shutil.which("stdbuf"):
        raise RuntimeError("Install coreutils (stdbuf) for the live master packet observer.")
    if args.synthetic:
        return inventory()
    if not args.video or not args.audio:
        raise RuntimeError("Hardware capture requires --video and --audio; use --list-devices first.")
    device = Path(args.video)
    if not device.is_char_device() or not os.access(device, os.R_OK | os.W_OK):
        raise RuntimeError(f"Cannot open video device {device}; check its path, permissions and sandbox access.")
    # An explicit ALSA device prevents accidentally recording the desktop microphone.
    if not args.audio.startswith("hw:"):
        raise RuntimeError("Use the USB-Live2's explicit ALSA hw:CARD=...,DEV=0 device from arecord -l.")
    if not shutil.which("v4l2-ctl"):
        raise RuntimeError("Install v4l-utils (v4l2-ctl) to inspect USB-Live2 formats and inputs.")
    result = inventory()
    for option in ("--all", "--list-inputs", "--list-formats-ext", "--list-standards"):
        run = subprocess.run(["v4l2-ctl", "--device", args.video, option], capture_output=True,
                             text=True, check=True, timeout=10)
        result[option] = run.stdout + run.stderr
    if "YUYV" not in result["--list-formats-ext"] or "PAL" not in result["--list-standards"]:
        raise RuntimeError("This device does not advertise the required YUYV / PAL input format.")
    return result


def capture_command(args, origin):
    """One shared wall-clock origin; never zero audio and video independently."""
    root = args.output
    command = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-nostdin", "-n", "-copyts"]
    if args.synthetic:
        # Exercise the SAME epoch-based timestamp path as V4L2 + ALSA, with a nonzero
        # audio offset so accidentally normalizing each input separately is detectable.
        source = preview.SIGNAL.replace(
            "setsar=16/15[out0]", f"setsar=16/15,settb=AVTB,setpts=PTS+{origin:.6f}/TB[out0]")
        source = source.replace("c=stereo[out1]", f"c=stereo,asetpts=PTS+({origin:.6f}+0.12)/TB[out1]")
        command += ["-readrate", "1", "-readrate_initial_burst", "0.000001", "-readrate_catchup", "1",
                    "-f", "lavfi", "-i", source]
        audio = "0:a:0"
    else:
        command += ["-thread_queue_size", "128", "-f", "v4l2", "-timestamps", "abs",
                    "-standard", "PAL", "-channel", str(args.input), "-input_format", "yuyv422",
                    "-video_size", "720x576", "-framerate", "25", "-i", args.video,
                    "-thread_queue_size", "128", "-f", "alsa", "-sample_rate", "48000",
                    "-channels", "2", "-i", args.audio]
        audio = "1:a:0"
    # fps may round the first absolute PTS down by less than one preview frame.
    tag = f"max(0,floor((t-{origin:.6f})*1000+0.5))"
    filters = ("[0:v]format=yuv422p,setsar=16/15,split=3[m][p][audit];"
               f"[p]{preview.preview_filter(args.field_order, TAG_BITS, tag)}[screen]")
    command += ["-filter_complex", filters]
    # Both recording and input audit receive exactly the same single timestamp shift.
    common = ["-pix_fmt", "yuv422p", "-fps_mode", "passthrough", "-enc_time_base:v", "1:1000000",
              "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-avoid_negative_ts", "disabled",
              "-output_ts_offset", f"{-origin:.6f}"]
    command += ["-map", "[m]", "-map", audio, "-c:v", "ffv1", "-level", "3", "-slicecrc", "1",
                *common, "-cluster_time_limit", "100", "-flush_packets", "1",
                "-max_interleave_delta", "100000", str(root / "master.mkv")]
    # Small, sequential audit file: no second device read and no second recording.
    command += ["-map", "[audit]", "-map", audio, "-c:v", "rawvideo", "-threads:v", "1", *common,
                "-f", "framehash", "-hash", "sha256", "-flush_packets", "1", str(root / "source.framehash")]
    command += ["-map", "[screen]", "-c:v", "rawvideo", "-threads:v", "1", "-fps_mode", "passthrough",
                "-f", "fifo", "-fifo_format", "rawvideo", "-queue_size", "2",
                "-drop_pkts_on_overflow", "1", "pipe:1"]
    return command


class MasterObserver:
    """Feed a growing master to ONE ffprobe, with bounded memory and sequential I/O.

    EOF is withheld until capture finishes. Re-probing the whole growing recording
    every second would become progressively more expensive on a long tape.
    """

    def __init__(self, root, started):
        self.root, self.started = root, started
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.latest = {}
        self.error = None
        self.log = (root / "observer.log").open("wb")
        self.process = subprocess.Popen([
            "stdbuf", "-oL", "ffprobe", "-v", "error", "-probesize", "1048576", "-analyzeduration", "100000",
            "-show_packets", "-show_entries", "packet=stream_index,pts_time,dts_time,duration_time,size",
            "-of", "compact=p=0", "pipe:0",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, start_new_session=True)
        self.workers = [threading.Thread(target=work, daemon=True) for work in (self.feed, self.read)]
        for worker in self.workers:
            worker.start()

    def feed(self):
        try:
            master = self.root / "master.mkv"
            while not master.exists():
                if self.done.wait(0.02):
                    return
            with master.open("rb") as source:
                while True:
                    chunk = source.read(256 * 1024)
                    if chunk:
                        self.process.stdin.write(chunk)
                        self.process.stdin.flush()
                    elif self.done.is_set():
                        break
                    else:
                        self.done.wait(0.02)
        except (OSError, ValueError) as error:
            self.error = str(error)
        finally:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass

    def read(self):
        try:
            with (self.root / "packets.jsonl").open("w") as evidence:
                for line in self.process.stdout:
                    fields = dict(item.split("=", 1) for item in line.decode().strip().split("|") if "=" in item)
                    index = int(fields["stream_index"])
                    if index not in (0, 1):
                        raise ValueError("Unexpected master track")
                    pts = Fraction(fields["pts_time"])
                    elapsed = time.monotonic() - self.started
                    with self.lock:
                        self.latest[("video", "audio")[index]] = (pts, elapsed)
                    evidence.write(json.dumps({"observed_at": elapsed, **fields}) + "\n")
        except (OSError, ValueError, KeyError) as error:
            self.error = f"Cannot observe stored master packets: {error}"

    def snapshot(self):
        with self.lock:
            return dict(self.latest)

    def close(self):
        self.done.set()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.error = "Master packet observer did not finish."
            self.process.kill()
            self.process.wait()
        for worker in self.workers:
            worker.join(timeout=2)
        self.process.stdout.close()
        self.log.close()
        if self.process.returncode:
            self.error = self.error or f"Master packet observer exited {self.process.returncode}."


@dataclass
class StopRequest:
    reason: str = ""
    requested_at: float | None = None
    signal_sent_at: float | None = None
    terminated: bool = False
    killed: bool = False

    def request(self, reason):
        if not self.reason:
            self.requested_at = time.monotonic()
            self.reason = reason

    def service(self, process):
        if not self.reason or process.poll() is not None:
            return
        now = time.monotonic()
        if self.signal_sent_at is None:
            process.send_signal(signal.SIGINT)
            self.signal_sent_at = now
        elif now - self.signal_sent_at > 12 and not self.killed:
            process.kill()
            self.killed = True
        elif now - self.signal_sent_at > 10 and not self.terminated:
            process.terminate()
            self.terminated = True


def make_window(stop):
    try:
        from PySide6.QtGui import QImage, QPixmap
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
    except ImportError as error:
        raise RuntimeError("Install PySide6 for the viewer, or use --headless.") from error

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Capture · PAL USB-Live2 verification")
            self.picture = QLabel("Waiting for video…")
            self.picture.setFixedSize(preview.WIDTH, preview.HEIGHT)
            self.status = QLabel("Recording")
            button = QPushButton("Stop and validate")
            button.clicked.connect(lambda: stop.request("stop button"))
            layout = QVBoxLayout(self)
            for widget in (self.picture, self.status, button):
                layout.addWidget(widget)

        def closeEvent(self, event):
            stop.request("window closed")
            event.accept()

        def display(self, pixels):
            frame = QImage(pixels, preview.WIDTH, preview.HEIGHT, preview.WIDTH * 3,
                           QImage.Format.Format_RGB888)
            self.picture.setPixmap(QPixmap.fromImage(frame))

    app = QApplication.instance() or QApplication(sys.argv)
    window = Window()
    window.show()
    return app, window


def record(args, origin, command):
    # The media origin may precede setup/metadata I/O. Only paired samples can
    # distinguish that startup delay from a clock adjustment during capture.
    clock = CaptureClock.start()
    started = clock.monotonic
    stop = StopRequest()
    app, window = (None, None) if args.headless else make_window(stop)
    previous = {number: signal.signal(number, lambda number, frame: stop.request(signal.Signals(number).name))
                for number in (signal.SIGINT, signal.SIGTERM)}
    report = {"stalls": [], "errors": [], "received_preview_frames": 0}
    process = observer = None
    try:
        with (args.output / "capture.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
            os.set_blocking(process.stdout.fileno(), False)
            observer = MasterObserver(args.output, started)
            buffer = bytearray()
            last_tag = None
            next_stall = args.stall_after
            stall = None
            eof = False
            next_health = 0
            while True:
                elapsed = time.monotonic() - started
                if app:
                    app.processEvents()
                if args.duration and elapsed >= args.duration:
                    stop.request("duration reached")
                stop.service(process)
                latest = observer.snapshot()
                if observer.error and not stop.reason:
                    report["errors"].append(observer.error)
                    stop.request("observer failure")
                if elapsed >= next_health:
                    next_health = elapsed + 1
                    if elapsed > 8 and not stop.reason and (len(latest) != 2 or any(
                            elapsed - seen > 2 or time.time() - origin - float(pts) > 2
                            for pts, seen in latest.values())):
                        report["errors"].append("Master packets stopped advancing on disk.")
                        stop.request("master stopped")
                    if clock.changed() and not stop.reason:
                        report["errors"].append("System wall clock jumped; hardware timing cannot be verified.")
                        stop.request("clock changed")
                if stall is None and elapsed >= next_stall and not stop.reason:
                    stall = {"start": next_stall, "samples": [], "before_tag": last_tag,
                             "resumed": None, "recovered": False, "skipped_frames": 0}
                holding = False
                if stall and not stop.reason:
                    if stall["resumed"] is None:
                        if elapsed >= stall["start"] + len(stall["samples"]):
                            if len(latest) != 2:
                                raise RuntimeError("No master packets available at the preview stall.")
                            stall["samples"].append((elapsed, {track: value[0] for track, value in latest.items()}))
                        holding = len(stall["samples"]) < STALL_SECONDS + 1
                        if not holding:
                            stall["advances"] = preview.check_stall_progress(
                                stall["samples"], stall["start"], stall["start"] + STALL_SECONDS)
                            stall["resumed"] = elapsed
                            stall["cutoff_ms"] = (time.time() - origin + 0.08) * 1000
                    elif not stall["recovered"] and elapsed > stall["resumed"] + RECOVERY_SECONDS:
                        raise RuntimeError("No fresh source-timestamped preview frame after the stall.")
                # ALWAYS drain during shutdown, including a stop in the middle of a stall.
                if not holding or stop.reason:
                    for _ in range(32):
                        try:
                            chunk = os.read(process.stdout.fileno(), preview.RGB_BYTES)
                        except BlockingIOError:
                            break
                        if not chunk:
                            eof = True
                            break
                        buffer.extend(chunk)
                        while len(buffer) >= preview.RGB_BYTES:
                            pixels = bytes(buffer[:preview.RGB_BYTES])
                            del buffer[:preview.RGB_BYTES]
                            tag = preview.preview_sequence(pixels, TAG_BITS)
                            if last_tag is not None and tag <= last_tag:
                                raise RuntimeError("Preview source timestamps did not increase.")
                            if tag / 1000 > time.time() - origin + 0.08:
                                raise RuntimeError(f"Preview timestamp {tag / 1000:.3f}s runs ahead of the capture clock.")
                            if stall and last_tag is not None and tag - last_tag > 120:
                                stall["skipped_frames"] += round((tag - last_tag) / 80) - 1
                            last_tag = tag
                            report["received_preview_frames"] += 1
                            if stall and stall["resumed"] is not None and not stall["recovered"]:
                                if tag <= stall["cutoff_ms"]:
                                    continue
                                if stall["before_tag"] is None or not stall["skipped_frames"]:
                                    raise RuntimeError("Preview did not demonstrate a gap across its stall.")
                                delay = time.monotonic() - started - stall["resumed"]
                                if delay > RECOVERY_SECONDS:
                                    raise RuntimeError("Fresh preview recovery exceeded its deadline.")
                                stall["recovered"] = True
                                stall["recovery_source_seconds"] = tag / 1000
                                stall["recovery_delay"] = delay
                                report["stalls"].append(stall)
                                print(f"Stall at {stall['start']:.0f}s: master advanced "
                                      f"{stall['advances']['video']:.2f}s video / {stall['advances']['audio']:.2f}s audio; "
                                      f"fresh preview at {tag / 1000:.2f}s.", flush=True)
                                next_stall += args.stall_every
                                stall = None
                            if window:
                                window.display(pixels)
                if window:
                    window.status.setText(f"{'Finalizing' if stop.reason else 'Recording'} · {elapsed:.0f}s")
                if process.poll() is not None and eof:
                    break
                time.sleep(0.01)
            if buffer:
                report["errors"].append("Preview ended with an incomplete RGB frame.")
            if not stop.reason:
                report["errors"].append(f"Capture exited unexpectedly with status {process.returncode}.")
            if stall is not None:
                report["interrupted_stall"] = stall
    except (RuntimeError, OSError, ValueError) as error:
        report["errors"].append(str(error))
        stop.request("verification failure")
    finally:
        # A validation error follows the same graceful stop path as an operator stop.
        if process:
            stop.request("capture finished")
            while process.poll() is None:
                stop.service(process)
                try:
                    os.read(process.stdout.fileno(), 1024 * 1024)
                except (BlockingIOError, OSError):
                    pass
                if app:
                    app.processEvents()
                time.sleep(0.01)
            process.stdout.close()
            report["ffmpeg_returncode"] = process.returncode
        if observer:
            observer.close()
            if observer.error and observer.error not in report["errors"]:
                report["errors"].append(observer.error)
        for number, handler in previous.items():
            signal.signal(number, handler)
        if window:
            window.close()
        report["stop"] = vars(stop)
        report["capture_seconds"] = stop.requested_at - started if stop.requested_at is not None else 0
        report["stop_source_seconds"] = (clock.wallclock_at(stop.requested_at) - origin
                                          if stop.requested_at is not None else None)
        report["elapsed_seconds"] = time.monotonic() - started
        report["clean_exit"] = bool(process and process.returncode in (0, 255) and
                                    stop.signal_sent_at is not None and not stop.terminated and not stop.killed)
    return report


def finish_checks(report, duration):
    """Require both tracks through the stop request, independently of audit equality."""
    report["requested_duration_seconds"] = duration
    report["duration_completed"] = not duration or report["capture_seconds"] >= duration
    if "master" in report and report["stop_source_seconds"] is not None:
        report["maximum_end_lag_seconds"] = END_LAG_SECONDS
        for track in ("video", "audio"):
            # Keep both ends on the original shared timeline, including any A/V offset.
            lag = report["stop_source_seconds"] - report["master"][track]["end_pts"]
            report["master"][track]["end_lag_at_stop_seconds"] = lag
            if lag > END_LAG_SECONDS:
                report["errors"].append(f"Captured {track} ended {lag:.3f}s before the stop request; "
                                        f"maximum allowed lag is {END_LAG_SECONDS:.3f}s.")
    report["automatic_checks"] = "pass" if (report["clean_exit"] and report["duration_completed"] and
                                              report["stalls"] and not report["errors"] and
                                              "interrupted_stall" not in report) else "incomplete" if not report["errors"] else "fail"


def hashes(path, stream):
    """Stream one track's framehash records; never load a tape into memory."""
    bases = {}
    with path.open() as source:
        for line in source:
            if line.startswith("#tb "):
                index, base = line[4:].split(":")
                bases[int(index)] = Fraction(base.strip())
            elif line.strip() and not line.startswith("#"):
                parts = [part.strip() for part in line.split(",")]
                if int(parts[0]) == stream:
                    base = bases[stream]
                    yield (int(parts[2]) * base, int(parts[1]) * base, int(parts[3]) * base,
                           int(parts[4]), parts[5])


def stored_packets(path, stream):
    with path.open() as evidence:
        for line in evidence:
            packet = json.loads(line)
            if int(packet["stream_index"]) == stream:
                try:
                    yield tuple(Fraction(packet[key]) for key in ("pts_time", "dts_time", "duration_time"))
                except (KeyError, ValueError) as error:
                    raise RuntimeError("Missing stored master packet timestamps or duration.") from error


def compare_audit(source, decoded, packets=None):
    result = {}
    for index, track in enumerate(("video", "audio")):
        count = size = discontinuities = 0
        first = last = None
        maximum_error = Fraction(0)
        maximum_stored_error = Fraction(0)
        recorded_times = stored_packets(packets, index) if packets is not None else None
        examples = []
        for original, stored in itertools.zip_longest(hashes(source, index), hashes(decoded, index)):
            if original is None or stored is None:
                raise RuntimeError(f"Master {track} packet count differs from the captured input audit.")
            if original[3:] != stored[3:]:
                raise RuntimeError(f"Master {track} decoded content differs from captured input at packet {count}.")
            error = max(abs(original[pos] - stored[pos]) for pos in (0, 1, 2))
            maximum_error = max(maximum_error, error)
            if error > TIME_TOLERANCE:
                raise RuntimeError(f"Master {track} timing differs from captured input at packet {count}: {float(error):.6f}s.")
            if recorded_times is not None:
                times = next(recorded_times, None)
                if times is None:
                    raise RuntimeError(f"Missing stored master {track} packets in the observer evidence.")
                stored_error = max(abs(original[pos] - times[pos]) for pos in (0, 1, 2))
                maximum_stored_error = max(maximum_stored_error, stored_error)
                if stored_error > TIME_TOLERANCE:
                    raise RuntimeError(f"Stored master {track} packet timing differs from captured input at packet {count}.")
            if last:
                expected = Fraction(1, 25) if index == 0 else Fraction(last[3], 48000 * 4)
                gap = original[0] - last[0] - expected
                tolerance = Fraction(1, 50) if index == 0 else Fraction(1, 200)
                if original[0] <= last[0] or abs(gap) > tolerance:
                    discontinuities += 1
                    if len(examples) < 10:
                        examples.append({"packet": count, "gap_seconds": float(gap)})
            first = first or original
            last = original
            count += 1
            size += original[3]
        if not count:
            raise RuntimeError(f"No {track} in the input audit.")
        if recorded_times is not None and next(recorded_times, None) is not None:
            raise RuntimeError(f"Extra stored master {track} packets in the observer evidence.")
        result[track] = {"packets": count, "decoded_bytes": size, "first_pts": float(first[0]),
                         "end_pts": float(last[0] + last[2]), "maximum_timestamp_error": float(maximum_error),
                         "maximum_stored_timestamp_error": float(maximum_stored_error) if recorded_times is not None else None,
                         "discontinuities": discontinuities, "discontinuity_examples": examples}
        nominal_span = Fraction(count, 25) if index == 0 else Fraction(size, 48000 * 4)
        result[track]["clock_span_error_seconds"] = float(last[0] + last[2] - first[0] - nominal_span)
    result["audio"]["stereo_sample_pairs"] = result["audio"]["decoded_bytes"] // 4
    result["audio_minus_video_start_seconds"] = result["audio"]["first_pts"] - result["video"]["first_pts"]
    result["audio_minus_video_end_seconds"] = result["audio"]["end_pts"] - result["video"]["end_pts"]
    result["relative_clock_span_error_seconds"] = (result["audio"]["clock_span_error_seconds"] -
                                                     result["video"]["clock_span_error_seconds"])
    return result


def validate_master(root):
    with (root / "decode.log").open("wb") as log:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-n", "-xerror", "-copyts",
            "-err_detect", "crccheck+explode", "-i", str(root / "master.mkv"), "-map", "0:v:0", "-map", "0:a:0",
            "-vf", "idet", "-c:v", "rawvideo", "-threads:v", "1", "-pix_fmt", "yuv422p",
            "-fps_mode", "passthrough", "-enc_time_base:v", "1:1000000", "-c:a", "pcm_s16le",
            "-avoid_negative_ts", "disabled", "-f", "framehash", "-hash", "sha256", str(root / "decoded.framehash"),
        ], stdout=subprocess.DEVNULL, stderr=log, check=True)
    result = compare_audit(root / "source.framehash", root / "decoded.framehash", root / "packets.jsonl")
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(root / "master.mkv")
    ], timeout=15))
    write_json(root / "master-metadata.json", metadata)
    if float(metadata.get("format", {}).get("duration", 0)) <= 0:
        raise RuntimeError("Master has no finalized duration.")
    streams = metadata["streams"]
    if (len(streams) != 2 or streams[0].get("codec_name") != "ffv1" or
            streams[0].get("pix_fmt") != "yuv422p" or
            (streams[0].get("width"), streams[0].get("height")) != (720, 576) or
            streams[1].get("codec_name") != "pcm_s16le" or streams[1].get("sample_rate") != "48000" or
            streams[1].get("channels") != 2):
        raise RuntimeError("Master does not have the required PAL FFV1 / stereo PCM format.")
    counts = re.findall(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*(\d+) Progressive:\s*(\d+) Undetermined:\s*(\d+)",
                        (root / "decode.log").read_text(errors="replace"))
    result["field_order"] = {"driver_metadata": streams[0].get("field_order", "unknown"),
                             "idet_counts": dict(zip(("tff", "bff", "progressive", "undetermined"), map(int, counts[-1]))) if counts else {},
                             "verification": "pending moving-picture review; metadata and idet are evidence, not proof"}
    result["physical_audio_sync"] = "pending visual/audio cue review at start, middle and end"
    return result


def review_clips(root, duration):
    destination = root / "review"
    destination.mkdir()
    for label, position in (("start", 0), ("middle", max(0, duration / 2 - 2.5)), ("end", max(0, duration - 5))):
        for parity in ("tff", "bff"):
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-n", "-ss", str(position), "-i", str(root / "master.mkv"),
                "-t", "5", "-map", "0:v:0", "-map", "0:a:0", "-vf", f"yadif=mode=send_field:parity={parity}:deint=all",
                "-c:v", "ffv1", "-level", "3", "-c:a", "pcm_s16le", str(destination / f"{label}-{parity}.mkv"),
            ], check=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--video", help="USB-Live2 V4L2 path, preferably /dev/v4l/by-id/…")
    parser.add_argument("--audio", help="USB-Live2 ALSA identifier, e.g. hw:CARD=...,DEV=0")
    parser.add_argument("--input", type=int, default=0, help="V4L2 input index; default 0 (composite on USB-Live2)")
    parser.add_argument("--field-order", choices=("auto", "tff", "bff"), default="auto", help="Preview parity only; master fields are preserved")
    parser.add_argument("--output", type=Path, help="New session directory; existing directories are never overwritten")
    parser.add_argument("--duration", type=float, default=60, help="Wall-clock seconds; 0 records until Stop/Ctrl+C")
    parser.add_argument("--stall-after", type=int, default=10, help="Seconds before first three-second preview stall")
    parser.add_argument("--stall-every", type=int, default=60, help="Seconds between stall starts")
    parser.add_argument("--headless", action="store_true", help="Run preview transport checks without Qt")
    parser.add_argument("--synthetic", action="store_true", help="Exercise this pipeline with generated PAL and a 120 ms audio offset")
    parser.add_argument("--review-clips", action="store_true", help="After validation, save TFF/BFF 50 fps clips with audio at start/middle/end")
    args = parser.parse_args(argv)
    if not args.list_devices and not args.output:
        parser.error("--output is required")
    if (not math.isfinite(args.duration) or args.duration < 0 or args.duration > 12 * 3600 or
            args.stall_after < 2 or args.stall_every < 10 or args.input < 0):
        parser.error("Use duration 0..43200, stall-after >=2, stall-every >=10 and input >=0")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.list_devices:
        print(json.dumps(inventory(), indent=2))
        return 0
    devices = preflight(args)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    origin = time.time()
    command = capture_command(args, origin)
    write_json(args.output / "session.json", {"arguments": vars(args), "wallclock_origin": origin,
                                              "command": command, "devices": devices,
                                              "synthetic": args.synthetic, "hardware_verified": False})
    print(f"Recording into {args.output}. Stop/Ctrl+C finalizes the master, then validates it.", flush=True)
    report = record(args, origin, command)
    write_json(args.output / "report.json", report)
    print("Capture stopped. Decoding the saved master to verify its input audit and field evidence…", flush=True)
    try:
        report["master"] = validate_master(args.output)
        if args.review_clips:
            review_clips(args.output, report["master"]["video"]["end_pts"])
        if any(report["master"][track]["discontinuities"] for track in ("video", "audio")):
            report["errors"].append("Captured input has timestamp discontinuities; inspect the master timing report.")
        if abs(report["master"]["relative_clock_span_error_seconds"]) > 0.1:
            report["errors"].append("Audio/video clock span errors differ by more than 100 ms; review drift.")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        report["errors"].append(str(error))
    warnings = [line for line in (args.output / "capture.log").read_text(errors="replace").splitlines()
                if re.search(r"\bxrun\b|\boverrun\b|\bcorrupt\b|non.?monoton", line, re.I)]
    if warnings:
        report["capture_warnings"] = warnings[:20]
        report["errors"].append("Capture reported input/timestamp errors; inspect capture.log.")
    finish_checks(report, args.duration)
    report["hardware_verified"] = False
    write_json(args.output / "report.json", report)
    print(f"Automatic checks: {report['automatic_checks'].upper()}. Evidence: {args.output / 'report.json'}")
    if not report["duration_completed"]:
        print(f"Stopped after {report['capture_seconds']:.3f}s of the requested {args.duration:.3f}s.")
    print("Physical field order and audio sync still require review of a real source.")
    for error in report["errors"]:
        print(error, file=sys.stderr)
    return 0 if report["automatic_checks"] == "pass" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
