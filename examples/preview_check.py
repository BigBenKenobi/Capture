"""Exercise a live PAL preview and compare the master with a preview-off capture."""

from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

SECONDS = 8
WIDTH, HEIGHT = 320, 240
RGB_BYTES = WIDTH * HEIGHT * 3
PREVIEW_RATE = Fraction(25, 2)
STALL_START, STALL_END = 2, 5
PROGRESS_TOLERANCE = 0.5
RECOVERY_SECONDS = 1.5
TAG_BITS, TAG_CELL = 8, 8

# Each pair of consecutive motion samples becomes one interlaced PAL frame.
SIGNAL = (
    "smptebars=size=720x576:rate=50,format=yuv422p[bars];"
    "color=black:size=720x64:rate=50,format=yuv422p,"
    "drawbox=x=0:y=0:w=32:h=64:c=white:t=fill,scroll=h=-0.0083333333[marker];"
    "[bars][marker]overlay=x=0:y=80:format=yuv422,"
    "drawbox=x=16:y=16:w=48:h=48:c=white:t=fill:enable='lt(mod(t,1),0.08)',"
    "tinterlace=interleave_top,setsar=16/15[out0];"
    "aevalsrc=exprs='0.2*sin(2*PI*660*t)*lt(mod(t,1),0.08)|"
    "0.2*sin(2*PI*880*t)*lt(mod(t,1),0.08)':s=48000:c=stereo[out1]"
)


def check_capabilities(hardware=False, analysis=False):
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"Install FFmpeg with {' and '.join(missing)} on PATH.")

    def help_text(tool, *options):
        result = subprocess.run([tool, "-hide_banner", *options], capture_output=True,
                                text=True, timeout=10, check=True)
        return result.stdout + result.stderr

    def registered(text):
        return {name for entry in re.findall(r"^\s*[A-Z.]+\s+(\S+)", text, re.M)
                for name in entry.split(",")}

    requirements = {
        "filters": "smptebars format color drawbox scroll overlay tinterlace setsar "
                   "aevalsrc split yadif fps scale aresample aformat anull null",
        "encoders": "ffv1 pcm_s16le rawvideo",
        "decoders": "ffv1 pcm_s16le rawvideo wrapped_avframe pcm_f64le",
        "muxers": "matroska fifo rawvideo framehash s16le",
        "demuxers": "matroska",
        "devices": "lavfi",
        "pix_fmts": "yuv422p rgb24",
    }
    if hardware:
        requirements["devices"] += " video4linux2 alsa"
        requirements["pix_fmts"] += " yuyv422"
    if hardware or analysis:
        requirements["filters"] += " idet"
    for category, names in requirements.items():
        available = registered(help_text("ffmpeg", f"-{category}"))
        missing.extend(f"{category}: {name}" for name in names.split() if name not in available)
    ffmpeg_options = ("fps_mode readrate readrate_initial_burst readrate_catchup copyts "
                      "flush_packets cluster_time_limit max_interleave_delta fifo_format "
                      "queue_size drop_pkts_on_overflow slicecrc enc_time_base output_ts_offset")
    if hardware:
        ffmpeg_options += " timestamps standard channel input_format video_size framerate sample_rate channels thread_queue_size"
    for tool, names in (
        ("ffmpeg", ffmpeg_options),
        ("ffprobe", "show_packets show_streams show_entries of"),
    ):
        available = set(re.findall(r"^\s*-(\w+)(?=\s|\[|$)", help_text(tool, "-h", "full"), re.M))
        missing.extend(f"{tool} option: -{name}" for name in names.split() if name not in available)
    if "matroska" not in registered(help_text("ffprobe", "-demuxers")):
        missing.append("ffprobe demuxer: matroska")
    for tool, names in (("ffmpeg", ("file", "pipe")), ("ffprobe", ("file",))):
        available = set(help_text(tool, "-protocols").split())
        missing.extend(f"{tool} protocol: {name}" for name in names if name not in available)
    if missing:
        raise RuntimeError("Missing FFmpeg capabilities:\n  " + "\n  ".join(missing))
    for tool in ("ffmpeg", "ffprobe"):
        print(help_text(tool, "-version").splitlines()[0], flush=True)


def preview_filter(parity="tff", tag_bits=TAG_BITS, tag_expression="n"):
    # Deinterlace the known top-field-first signal before dropping frames or scaling.
    filters = [f"yadif=mode=send_frame:parity={parity}:deint=all", "fps=25/2",
               f"scale={WIDTH}:{HEIGHT}", "setsar=1", "format=rgb24"]
    # Carry the source sequence IN each packet, so FIFO drops also drop its tag.
    # The small black/white strip uses no font or separate (possibly buffered) side channel.
    filters += [f"drawbox=x=0:y={HEIGHT - TAG_CELL}:w={(tag_bits + 1) * TAG_CELL}:"
                f"h={TAG_CELL}:c=black:t=fill",
                f"drawbox=x={tag_bits * TAG_CELL}:y={HEIGHT - TAG_CELL}:w={TAG_CELL}:"
                f"h={TAG_CELL}:c=white:t=fill"]
    for bit in range(tag_bits):
        filters.append(f"drawbox=x={bit * TAG_CELL}:y={HEIGHT - TAG_CELL}:w={TAG_CELL}:"
                       f"h={TAG_CELL}:c=white:t=fill:enable='mod(floor(({tag_expression})/{1 << bit}),2)'")
    return ",".join(filters)


def capture_command(master, preview):
    args = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-nostdin", "-n"]
    if preview:
        # Some builds interpret 0 as the default 0.5s burst. One microsecond avoids
        # that fallback; disable accelerated catch-up so sequence time tracks wall time.
        args += ["-readrate", "1", "-readrate_initial_burst", "0.000001", "-readrate_catchup", "1"]
    args += ["-f", "lavfi", "-t", str(SECONDS), "-i", SIGNAL]
    if preview:
        args += [
            "-filter_complex", f"[0:v]split[m][p];[p]{preview_filter()}[screen]",
            "-map", "[m]",
        ]
    else:
        args += ["-map", "0:v:0"]
    args += [
        "-map", "0:a:0", "-c:v", "ffv1", "-level", "3", "-slicecrc", "1",
        "-pix_fmt", "yuv422p", "-fps_mode", "passthrough", "-c:a", "pcm_s16le",
        # Make completed clusters visible to probes while recording (on both masters).
        "-cluster_time_limit", "100", "-flush_packets", "1", "-max_interleave_delta", "100000",
        str(master),
    ]
    if preview:
        # Only the display branch can discard packets. One raw packet is one whole picture.
        args += [
            "-map", "[screen]", "-c:v", "rawvideo", "-threads:v", "1", "-fps_mode", "passthrough",
            "-f", "fifo", "-fifo_format", "rawvideo", "-queue_size", "2",
            "-drop_pkts_on_overflow", "1", "pipe:1",
        ]
    return args


def packet_timeline(master):
    """Read stored packet times in seconds, with one shared origin for BOTH tracks."""
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_packets", "-show_streams", "-show_entries",
        "stream=index,codec_type,time_base:packet=stream_index,pts,dts,duration",
        "-of", "json", str(master),
    ], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError(f"Cannot probe {master.name}: {result.stderr.strip()}")
    # An in-progress Matroska file can report premature EOF yet return complete packets.
    data = json.loads(result.stdout)
    streams = {stream["index"]: stream for stream in data["streams"]}
    if sorted(stream["codec_type"] for stream in streams.values()) != ["audio", "video"]:
        raise RuntimeError("Expected exactly one audio and one video track in the master.")
    timeline = {"video": [], "audio": []}
    for packet in data["packets"]:
        stream = streams[packet["stream_index"]]
        try:
            base = Fraction(stream["time_base"])
            times = tuple(int(packet[key]) * base for key in ("pts", "dts", "duration"))
        except (KeyError, ValueError) as error:
            raise RuntimeError("Missing stored master packet timestamps or duration.") from error
        if times[2] <= 0:
            raise RuntimeError("Invalid stored master packet duration.")
        timeline[stream["codec_type"]].append(times)
    if not all(timeline.values()):
        raise RuntimeError("The master has not written packets for both tracks.")
    return timeline


def check_stall_progress(samples, start=STALL_START, end=STALL_END):
    """Each sample is (wall elapsed, {track: latest stored PTS})."""
    if len(samples) != end - start + 1:
        raise RuntimeError("Missing master progress samples during the viewer stall.")
    for elapsed, _ in samples:
        if not start <= elapsed <= end + PROGRESS_TOLERANCE:
            raise RuntimeError("Master progress was sampled outside the viewer stall.")
    advances = {}
    for track in ("video", "audio"):
        for (before, first), (after, last) in zip(samples, samples[1:]):
            wall_delta = after - before
            media_delta = float(last[track] - first[track])
            if wall_delta < 0.5 or abs(media_delta - wall_delta) > PROGRESS_TOLERANCE:
                raise RuntimeError(f"Master {track} did not keep advancing during the viewer stall "
                                   f"({media_delta:.3f}s of media in {wall_delta:.3f}s).")
        wall_delta = samples[-1][0] - samples[0][0]
        advances[track] = float(samples[-1][1][track] - samples[0][1][track])
        if wall_delta < end - start - PROGRESS_TOLERANCE or abs(
                advances[track] - wall_delta) > PROGRESS_TOLERANCE:
            raise RuntimeError(f"Master {track} lost time during the viewer stall.")
    return advances


def preview_sequence(pixels, tag_bits=TAG_BITS):
    def cell(bit):
        offset = ((HEIGHT - TAG_CELL // 2) * WIDTH + bit * TAG_CELL + TAG_CELL // 2) * 3
        rgb = pixels[offset:offset + 3]
        if rgb not in (b"\x00\x00\x00", b"\xff\xff\xff"):
            raise RuntimeError("Invalid preview sequence tag or incomplete RGB frame.")
        return rgb == b"\xff\xff\xff"

    if len(pixels) != RGB_BYTES or not cell(tag_bits):
        raise RuntimeError("Invalid preview frame or sequence marker.")
    return sum(1 << bit for bit in range(tag_bits) if cell(bit))


def check_preview_frames(received, resumed):
    sequences = [sequence for _, sequence in received]
    if not sequences or any(not 0 <= seq < SECONDS * PREVIEW_RATE for seq in sequences):
        raise RuntimeError("Missing or out-of-range preview sequences.")
    gaps = [right - left for left, right in zip(sequences, sequences[1:])]
    if any(gap <= 0 for gap in gaps) or not any(gap > 1 for gap in gaps):
        raise RuntimeError("Expected ordered preview sequences with some skipped during the stall.")
    # Leave one frame of margin for readrate/filter rounding. Old packets read after
    # resuming cannot pass this source-time test, even when they have sequence gaps.
    fresh_sequence = math.ceil(resumed * PREVIEW_RATE) + 1
    recovery = [(elapsed, seq) for elapsed, seq in received
                if resumed <= elapsed <= resumed + RECOVERY_SECONDS and seq >= fresh_sequence]
    if not any(elapsed < STALL_START for elapsed, _ in received) or not recovery:
        raise RuntimeError("Expected preview frames before the stall and fresh source frames "
                           f"within {RECOVERY_SECONDS}s of resuming.")
    return recovery[0][1] / PREVIEW_RATE


def show_preview(app, process, master, started):
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QLabel

    window = QLabel("Waiting for the PAL signal…")
    window.setWindowTitle("Capture · Synthetic PAL preview")
    window.setFixedSize(WIDTH, HEIGHT)
    window.show()

    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    buffer = bytearray()
    received = []
    samples = []
    resumed = None
    finished = False
    failure = None
    timer = QTimer(window)

    def read_preview():
        nonlocal resumed, finished
        elapsed = time.monotonic() - started
        if elapsed > SECONDS + 15:
            app.exit(2)
            return
        if elapsed >= STALL_START and len(samples) < STALL_END - STALL_START + 1:
            if elapsed >= STALL_START + len(samples):
                timeline = packet_timeline(master)
                elapsed = time.monotonic() - started
                samples.append((elapsed, {track: packets[-1][0] for track, packets in timeline.items()}))
            if len(samples) < STALL_END - STALL_START + 1:
                return  # Stop reading the preview; keep observing actual master writes.
            resumed = elapsed
        if resumed is None and elapsed >= STALL_START:
            return
        while True:
            try:
                chunk = os.read(fd, RGB_BYTES)
            except BlockingIOError:
                break
            if not chunk:
                if process.poll() is not None:
                    finished = True
                    app.quit()
                break
            buffer.extend(chunk)
            if len(buffer) >= RGB_BYTES:
                pixels = bytes(buffer[:RGB_BYTES])
                del buffer[:RGB_BYTES]
                sequence = preview_sequence(pixels)
                received.append((time.monotonic() - started, sequence))
                if resumed is not None and sequence < math.ceil(resumed * PREVIEW_RATE) + 1:
                    continue  # Drain stale queued pictures without counting them as recovery.
                image = QImage(pixels, WIDTH, HEIGHT, WIDTH * 3, QImage.Format.Format_RGB888)
                window.setPixmap(QPixmap.fromImage(image))

    def tick():
        nonlocal failure
        try:
            read_preview()
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            failure = error
            app.exit(2)

    timer.timeout.connect(tick)
    timer.start(10)
    code = app.exec()
    timer.stop()
    window.close()
    if failure is not None:
        raise failure
    if code or not finished:
        raise RuntimeError("Preview timed out or the window closed before capture finished.")
    if process.returncode:
        raise RuntimeError(f"Capture exited with status {process.returncode}.")
    if buffer:
        raise RuntimeError("Incomplete preview frame at end of capture.")
    advances = check_stall_progress(samples)
    recovered = check_preview_frames(received, resumed)
    print(f"Master advanced {advances['video']:.3f}s video / {advances['audio']:.3f}s audio "
          "during the viewer stall.")
    print(f"Viewer received {len(received)} sequenced frames; fresh recovery at source {float(recovered):.2f}s.")
    return len(received)


def decoded(master, options):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-nostdin", "-copyts",
                                    "-i", str(master), *options, "-"], timeout=30)


def check_masters(reference, monitored):
    # ffprobe reads stored times without FFmpeg's input-start normalization or
    # re-encoding timebase rounding. Do not zero either track independently.
    reference_times = packet_timeline(reference)
    monitored_times = packet_timeline(monitored)
    for track in ("video", "audio"):
        if reference_times[track] != monitored_times[track]:
            raise RuntimeError(f"The stored master {track} packet timing differs (PTS, DTS or duration).")
    # Keep decoded content checks as well: matching timestamps alone say nothing about samples.
    video_options = ["-map", "0:v:0", "-pix_fmt", "yuv422p", "-fps_mode", "passthrough",
                     "-f", "framehash", "-hash", "sha256"]
    reference_video = decoded(reference, video_options)
    frames = sum(not line.startswith(b"#") for line in reference_video.splitlines())
    if frames != SECONDS * 25 or decoded(monitored, video_options) != reference_video:
        raise RuntimeError("The master video differs in frame count, timing or decoded pixels.")

    pcm_options = ["-map", "0:a:0", "-f", "s16le"]
    audio = decoded(reference, pcm_options)
    if len(audio) != SECONDS * 48000 * 2 * 2 or decoded(monitored, pcm_options) != audio:
        raise RuntimeError("The decoded stereo PCM differs in length or content.")
    print(f"PASS: {frames} identical master frames and {len(audio) // 4:,} identical stereo sample pairs; "
          "stored audio/video PTS, DTS and durations match on a common timeline.")


def main():
    check_capabilities()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise RuntimeError("Install PySide6 for this Python interpreter.") from error
    app = QApplication(sys.argv)
    with TemporaryDirectory(prefix="capture-preview-") as directory:
        reference = Path(directory) / "preview-off.mkv"
        monitored = Path(directory) / "preview-on.mkv"
        subprocess.run(capture_command(reference, False), check=True, timeout=30)
        started = time.monotonic()
        with subprocess.Popen(capture_command(monitored, True), stdout=subprocess.PIPE) as process:
            try:
                show_preview(app, process, monitored, started)
            finally:
                if process.poll() is None:
                    process.kill()
        check_masters(reference, monitored)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
