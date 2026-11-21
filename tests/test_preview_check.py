"""Regression checks for timing, stalls and capability diagnostics; no Qt display needed."""

from contextlib import redirect_stdout
from fractions import Fraction
import io
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from examples import preview_check as check


HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class StallChecks(unittest.TestCase):
    def samples(self, video=(2, 3, 4, 5), audio=(2, 3, 4, 5)):
        return [(wall + 0.1, {"video": Fraction(v), "audio": Fraction(a)})
                for wall, v, a in zip(range(2, 6), video, audio)]

    def test_both_tracks_advance_at_capture_rate(self):
        self.assertEqual(check.check_stall_progress(self.samples()), {"video": 3, "audio": 3})

    def test_pause_cannot_be_hidden_by_later_catchup(self):
        for track in ("audio", "video"):
            with self.subTest(track=track), self.assertRaisesRegex(RuntimeError, track):
                check.check_stall_progress(self.samples(**{track: (2, 2, 4, 5)}))

    def test_slow_recording_fails_total_progress(self):
        with self.assertRaisesRegex(RuntimeError, "lost time"):
            check.check_stall_progress(self.samples(video=(2, 2.6, 3.2, 3.8)))

    def test_samples_after_stall_cannot_prove_progress(self):
        samples = self.samples()
        samples[-1] = (8, samples[-1][1])
        with self.assertRaisesRegex(RuntimeError, "outside"):
            check.check_stall_progress(samples)

    def test_stale_buffered_frames_cannot_count_as_recovery(self):
        # Has frames before/after the wall-clock stall AND skips frames: the old check passed.
        received = [(0.2, 0), (1.0, 10), (1.9, 20), (5.2, 21), (5.3, 24)]
        with self.assertRaisesRegex(RuntimeError, "fresh source frames"):
            check.check_preview_frames(received, resumed=5.1)

    def test_fresh_frames_must_recover_promptly(self):
        received = [(0.2, 0), (1.9, 20), (7.2, 85)]
        with self.assertRaisesRegex(RuntimeError, "within 1.5s"):
            check.check_preview_frames(received, resumed=5.1)

    def test_recovery_uses_actual_resume_time(self):
        with self.assertRaisesRegex(RuntimeError, "fresh source frames"):
            check.check_preview_frames([(1.0, 10), (5.5, 64)], resumed=5.4)

    def test_fresh_ordered_preview_with_drops_passes(self):
        received = [(0.2, 0), (1.9, 20), (5.2, 21), (5.5, 67), (5.6, 68)]
        self.assertEqual(check.check_preview_frames(received, resumed=5.1), Fraction(134, 25))


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg and ffprobe required")
class MasterTimingChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory(prefix="capture-regression-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.reference = cls.root / "reference.mkv"
        with patch.object(check, "SECONDS", 1):
            subprocess.run(check.capture_command(cls.reference, False), check=True, timeout=30)

    def compare(self, path):
        with patch.object(check, "SECONDS", 1), redirect_stdout(io.StringIO()):
            check.check_masters(self.reference, path)

    def shifted(self, track, offset):
        path = self.root / f"{track}-{offset}.mkv"
        args = ["ffmpeg", "-v", "error", "-nostdin", "-n", "-copyts", "-i", str(self.reference),
                "-itsoffset", str(offset), "-i", str(self.reference)]
        args += ["-map", "1:v" if track in ("video", "both") else "0:v",
                 "-map", "1:a" if track in ("audio", "both") else "0:a",
                 "-c", "copy", "-avoid_negative_ts", "disabled", str(path)]
        subprocess.run(args, check=True, timeout=10)
        return path

    def test_identical_nonzero_start_times_pass(self):
        shifted = self.shifted("both", 0.5)
        self.assertEqual(check.packet_timeline(shifted)["video"][0][0], Fraction(1, 2))
        with patch.object(check, "SECONDS", 1), redirect_stdout(io.StringIO()):
            check.check_masters(shifted, shifted)

    def test_audio_shift_fails_even_with_identical_pcm(self):
        shifted = self.shifted("audio", 0.25)
        options = ["-map", "0:a:0", "-f", "s16le"]
        self.assertEqual(check.decoded(self.reference, options), check.decoded(shifted, options))
        with self.assertRaisesRegex(RuntimeError, "stored master audio packet timing"):
            self.compare(shifted)

    def test_video_and_common_origin_shifts_fail(self):
        for track in ("video", "both"):
            with self.subTest(track=track), self.assertRaisesRegex(RuntimeError, "stored master video"):
                self.compare(self.shifted(track, 0.25))

    def test_subframe_video_shift_is_not_rounded_away(self):
        shifted = self.shifted("video", 0.001)
        with self.assertRaisesRegex(RuntimeError, "stored master video packet timing"):
            self.compare(shifted)

    def test_content_corruption_with_matching_timestamps_fails(self):
        for track, filter_args, message in (
            ("video", ["-vf", "negate"], "decoded pixels"),
            ("audio", ["-af", "volume=0"], "PCM differs"),
        ):
            with self.subTest(track=track):
                path = self.root / f"changed-{track}.mkv"
                command = ["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(self.reference),
                           "-map", "0", "-c:v", "ffv1", "-c:a", "pcm_s16le", *filter_args, str(path)]
                subprocess.run(command, check=True, timeout=10)
                self.assertEqual(check.packet_timeline(self.reference), check.packet_timeline(path))
                with self.assertRaisesRegex(RuntimeError, message):
                    self.compare(path)

    def test_real_preview_tags_are_sequential(self):
        path = self.root / "tagged.mkv"
        with patch.object(check, "SECONDS", 1):
            # Observe arrival time as well as the tag: an initial read burst would
            # invalidate the wall/source-time relationship used to reject stale frames.
            started = time.monotonic()
            with subprocess.Popen(check.capture_command(path, True), stdout=subprocess.PIPE) as process:
                sequences = []
                try:
                    while pixels := process.stdout.read(check.RGB_BYTES):
                        elapsed = time.monotonic() - started
                        sequence = check.preview_sequence(pixels)
                        sequences.append(sequence)
                        self.assertLessEqual(float(sequence / check.PREVIEW_RATE),
                                             elapsed + float(1 / check.PREVIEW_RATE))
                    self.assertEqual(process.wait(timeout=10), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
        self.assertGreaterEqual(len(sequences), 12)
        self.assertEqual(sequences, list(range(len(sequences))))


@unittest.skipUnless(HAS_FFMPEG and hasattr(signal, "SIGSTOP"), "Linux FFmpeg required")
class PausedRecorderCheck(unittest.TestCase):
    def test_identical_completed_master_does_not_excuse_a_recording_pause(self):
        with TemporaryDirectory(prefix="capture-paused-") as directory:
            reference = Path(directory) / "reference.mkv"
            monitored = Path(directory) / "paused.mkv"
            subprocess.run(check.capture_command(reference, False), check=True, timeout=30)
            started = time.monotonic()
            with subprocess.Popen(check.capture_command(monitored, True), stdout=subprocess.DEVNULL) as process:
                samples = []
                try:
                    for target in range(check.STALL_START, check.STALL_END + 1):
                        time.sleep(max(0, started + target - time.monotonic()))
                        timeline = check.packet_timeline(monitored)
                        samples.append((time.monotonic() - started,
                                        {track: packets[-1][0] for track, packets in timeline.items()}))
                        if target == check.STALL_START:
                            os.kill(process.pid, signal.SIGSTOP)
                    os.kill(process.pid, signal.SIGCONT)
                    self.assertEqual(process.wait(timeout=20), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
            # Reproduce the false positive: final decoded masters still match perfectly.
            with redirect_stdout(io.StringIO()):
                check.check_masters(reference, monitored)
            with self.assertRaisesRegex(RuntimeError, "did not keep advancing"):
                check.check_stall_progress(samples)


class CapabilityChecks(unittest.TestCase):
    def test_missing_ffprobe_is_reported_before_qt_startup(self):
        with patch.object(check.shutil, "which", side_effect=lambda name: None if name == "ffprobe" else name):
            with self.assertRaisesRegex(RuntimeError, "ffprobe on PATH"):
                check.check_capabilities()

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg and ffprobe required")
    def test_missing_filters_and_options_are_named(self):
        run = subprocess.run

        def older_build(command, **kwargs):
            result = run(command, **kwargs)
            if command[0] == "ffmpeg":
                for field in ("stdout", "stderr"):
                    text = getattr(result, field)
                    text = re.sub(r"^.*\bscroll\b.*\n", "", text, flags=re.M)
                    text = re.sub(r"^\s*-(fps_mode|readrate)(?:\s|\[).*\n", "", text, flags=re.M)
                    setattr(result, field, text)
            return result

        with patch.object(check.subprocess, "run", side_effect=older_build):
            with self.assertRaises(RuntimeError) as raised:
                check.check_capabilities()
        for missing in ("filters: scroll", "ffmpeg option: -fps_mode", "ffmpeg option: -readrate"):
            self.assertIn(missing, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
