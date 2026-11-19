"""Exercise a live PAL preview and compare the master with a preview-off capture."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel


SECONDS = 8
WIDTH, HEIGHT = 320, 240
RGB_BYTES = WIDTH * HEIGHT * 3

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


def capture_command(master, preview):
    args = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-nostdin", "-n"]
    if preview:
        args += ["-readrate", "1"]
    args += ["-f", "lavfi", "-t", str(SECONDS), "-i", SIGNAL]
    if preview:
        args += [
            "-filter_complex", f"[0:v]split[m][p];[p]fps=25/2,scale={WIDTH}:{HEIGHT},"
            "setsar=1,format=rgb24[screen]",
            "-map", "[m]",
        ]
    else:
        args += ["-map", "0:v:0"]
    args += [
        "-map", "0:a:0", "-c:v", "ffv1", "-level", "3", "-slicecrc", "1",
        "-pix_fmt", "yuv422p", "-fps_mode", "passthrough", "-c:a", "pcm_s16le", str(master),
    ]
    if preview:
        # Only the display branch can discard packets. One raw packet is one whole picture.
        args += [
            "-map", "[screen]", "-c:v", "rawvideo", "-threads:v", "1",
            "-f", "fifo", "-fifo_format", "rawvideo", "-queue_size", "2",
            "-drop_pkts_on_overflow", "1", "pipe:1",
        ]
    return args


def show_preview(app, process):
    window = QLabel("Waiting for the PAL signal…")
    window.setWindowTitle("Capture · Synthetic PAL preview")
    window.setFixedSize(WIDTH, HEIGHT)
    window.show()

    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    buffer = bytearray()
    frame_count = 0
    shown_before = shown_after = finished = False
    started = time.monotonic()
    timer = QTimer(window)

    def read_preview():
        nonlocal frame_count, shown_before, shown_after, finished
        elapsed = time.monotonic() - started
        if elapsed > SECONDS + 15:
            app.exit(2)
            return
        if 2 <= elapsed < 5:
            return  # Deliberately stop reading for three seconds.
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
                image = QImage(pixels, WIDTH, HEIGHT, WIDTH * 3, QImage.Format.Format_RGB888)
                window.setPixmap(QPixmap.fromImage(image))
                frame_count += 1
                shown_before |= elapsed < 2
                shown_after |= elapsed >= 5

    timer.timeout.connect(read_preview)
    timer.start(10)
    code = app.exec()
    timer.stop()
    if code or not finished:
        raise RuntimeError("Preview timed out or the window closed before capture finished.")
    if process.returncode:
        raise RuntimeError(f"Capture exited with status {process.returncode}.")
    if not (shown_before and shown_after and 0 < frame_count < SECONDS * 12.5) or buffer:
        raise RuntimeError("Expected complete preview frames before and after the stall, with some skipped.")
    return frame_count


def decoded(master, options):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(master), *options, "-"])


def check_masters(reference, monitored):
    # framehash includes presentation timestamps and a SHA-256 digest of each decoded frame.
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
    print(f"PASS: {frames} identical master frames and {len(audio) // 4:,} identical stereo sample pairs.")


def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("Install FFmpeg and make sure ffmpeg is on PATH.")
    app = QApplication(sys.argv)
    with TemporaryDirectory(prefix="capture-preview-") as directory:
        reference = Path(directory) / "preview-off.mkv"
        monitored = Path(directory) / "preview-on.mkv"
        subprocess.run(capture_command(reference, False), check=True)
        with subprocess.Popen(capture_command(monitored, True), stdout=subprocess.PIPE) as process:
            try:
                count = show_preview(app, process)
            finally:
                if process.poll() is None:
                    process.kill()
        print(f"Viewer received {count} frames across a three-second read stall.")
        check_masters(reference, monitored)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
