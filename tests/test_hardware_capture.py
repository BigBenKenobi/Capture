"""Hardware-path regressions using generated input; never open a capture device."""

from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from examples import hardware_capture as hardware


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "hardware_capture.py"
HAS_TOOLS = all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "stdbuf"))


class AuditChecks(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="capture-audit-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def fixture(self, name, audio_pts=120000, video_pts=0, audio_hash="abc"):
        path = self.root / name
        path.write_text("#tb 0: 1/1000000\n#tb 1: 1/1000000\n"
                        f"0, {video_pts}, {video_pts}, 40000, 829440, def\n"
                        f"1, {audio_pts}, {audio_pts}, 20000, 3840, {audio_hash}\n")
        return path

    def test_preserves_audio_offset_on_shared_timeline(self):
        result = hardware.compare_audit(self.fixture("source"), self.fixture("decoded"))
        self.assertAlmostEqual(result["audio_minus_video_start_seconds"], 0.12)
        self.assertEqual(result["audio"]["stereo_sample_pairs"], 960)

    def test_independently_zeroing_audio_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "audio timing differs"):
            hardware.compare_audit(self.fixture("source"), self.fixture("decoded", audio_pts=0))

    def test_matching_content_cannot_hide_a_global_timestamp_shift(self):
        with self.assertRaisesRegex(RuntimeError, "video timing differs"):
            hardware.compare_audit(self.fixture("source"), self.fixture("decoded", audio_pts=370000, video_pts=250000))

    def test_only_matroska_rounding_is_tolerated(self):
        hardware.compare_audit(self.fixture("source"), self.fixture("rounded", audio_pts=120999))
        with self.assertRaisesRegex(RuntimeError, "audio timing differs"):
            hardware.compare_audit(self.fixture("source"), self.fixture("shifted", audio_pts=122000))

    def test_content_damage_and_missing_tail_are_rejected(self):
        source = self.fixture("source")
        with self.assertRaisesRegex(RuntimeError, "content differs"):
            hardware.compare_audit(source, self.fixture("damaged", audio_hash="bad"))
        shortened = self.fixture("shortened")
        shortened.write_text("#tb 0: 1/1000000\n0, 0, 0, 40000, 829440, def\n")
        with self.assertRaisesRegex(RuntimeError, "packet count differs"):
            hardware.compare_audit(source, shortened)

    def test_input_discontinuity_is_reported_even_when_preserved(self):
        source = self.fixture("source")
        with source.open("a") as output:
            output.write("0, 120000, 120000, 40000, 829440, fed\n")
        result = hardware.compare_audit(source, source)
        self.assertEqual(result["video"]["discontinuities"], 1)
        self.assertAlmostEqual(result["video"]["clock_span_error_seconds"], 0.08)

    def test_stored_packet_duration_is_checked_separately_from_decoded_timing(self):
        source = self.fixture("source")
        evidence = self.root / "packets.jsonl"
        records = [{"stream_index": "0", "pts_time": "0", "dts_time": "0", "duration_time": "0.040"},
                   {"stream_index": "1", "pts_time": "0.120", "dts_time": "0.120", "duration_time": "0.020"}]
        evidence.write_text("".join(json.dumps(record) + "\n" for record in records))
        hardware.compare_audit(source, source, evidence)
        records[0]["duration_time"] = "0.080"
        evidence.write_text("".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(RuntimeError, "Stored master video packet timing"):
            hardware.compare_audit(source, source, evidence)


class ShutdownChecks(unittest.TestCase):
    def test_graceful_stop_precedes_escalation(self):
        process = Mock()
        process.poll.return_value = None
        stop = hardware.StopRequest()
        with patch.object(hardware.time, "monotonic", return_value=99):
            stop.request("operator")
        with patch.object(hardware.time, "monotonic", return_value=100):
            stop.service(process)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        with patch.object(hardware.time, "monotonic", return_value=111):
            stop.service(process)
        process.terminate.assert_called_once()
        with patch.object(hardware.time, "monotonic", return_value=113):
            stop.service(process)
        process.kill.assert_called_once()
        self.assertTrue(stop.terminated and stop.killed)
        self.assertEqual(stop.requested_at, 99)
        self.assertEqual(stop.signal_sent_at, 100)
        stop.request("capture finished")
        self.assertEqual(stop.reason, "operator")
        self.assertEqual(stop.requested_at, 99)

    def test_clock_baselines_ignore_setup_delay_but_detect_real_jumps(self):
        # The media origin was 1000; setup took 750 ms before recording began.
        with patch.object(hardware.time, "time", return_value=1000.75), \
                patch.object(hardware.time, "monotonic", return_value=50):
            clock = hardware.CaptureClock.start()
        self.assertAlmostEqual(clock.wallclock_at(58) - 1000, 8.75)
        with patch.object(hardware.time, "monotonic", return_value=52):
            for wallclock, changed in ((1002.75, False), (1003.5, True), (1002, True)):
                with self.subTest(wallclock=wallclock), patch.object(hardware.time, "time", return_value=wallclock):
                    self.assertEqual(clock.changed(), changed)

    def test_hardware_command_keeps_one_clock_and_unmodified_master_fields(self):
        args = hardware.parse_args(["--output", "/tmp/unused-capture-test", "--video", "/dev/video0",
                                    "--audio", "hw:CARD=Live2,DEV=0", "--field-order", "bff"])
        command = hardware.capture_command(args, 1700000000.125)
        self.assertIn("-copyts", command)
        self.assertNotIn("-start_at_zero", command)
        self.assertNotIn("-readrate", command)
        self.assertEqual(command.count("-output_ts_offset"), 2)
        for index, option in enumerate(command):
            if option == "-output_ts_offset":
                self.assertEqual(command[index + 1], "-1700000000.125000")
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[p]yadif=mode=send_frame:parity=bff", graph)
        self.assertNotIn("STARTPTS", graph)
        self.assertNotIn("setfield", graph)
        self.assertNotIn("async", graph)


class CompletionChecks(unittest.TestCase):
    def report(self, video_end=8.16, audio_end=8.12, stop_source=8, capture_seconds=8):
        return {"master": {"video": {"first_pts": 0, "end_pts": video_end},
                            "audio": {"first_pts": 0.12, "end_pts": audio_end}},
                "stop_source_seconds": stop_source, "capture_seconds": capture_seconds,
                "elapsed_seconds": 75, "clean_exit": True, "stalls": [{}], "errors": []}

    def test_either_or_both_tracks_ending_before_stop_fail(self):
        for video, audio, missing in ((8.16, 6.619979, ("audio",)), (6.5, 8.12, ("video",)),
                                     (6.5, 6.619979, ("video", "audio"))):
            with self.subTest(video=video, audio=audio):
                report = self.report(video, audio)
                hardware.finish_checks(report, 8)
                self.assertEqual(report["automatic_checks"], "fail")
                self.assertEqual(len(report["errors"]), len(missing))
                for track in missing:
                    self.assertTrue(any(f"{track} ended" in error for error in report["errors"]))

    def test_end_coverage_keeps_shared_origin_and_audio_offset(self):
        report = self.report(11.16, 11.12, stop_source=11)
        report["master"]["video"]["first_pts"] = 3
        report["master"]["audio"]["first_pts"] = 3.12
        hardware.finish_checks(report, 8)
        self.assertEqual(report["automatic_checks"], "pass")
        self.assertAlmostEqual(report["master"]["audio"]["end_lag_at_stop_seconds"], -0.12)

    def test_final_packet_buffering_has_an_explicit_bound(self):
        for lag, status in ((hardware.END_LAG_SECONDS, "pass"), (hardware.END_LAG_SECONDS + 0.001, "fail")):
            with self.subTest(lag=lag):
                report = self.report(audio_end=8 - lag)
                hardware.finish_checks(report, 8)
                self.assertEqual(report["automatic_checks"], status)

    def test_cleanup_time_cannot_complete_a_shortened_fixed_duration(self):
        report = self.report(6.64, 6.626667, stop_source=6.6, capture_seconds=6.6)
        hardware.finish_checks(report, 60)
        self.assertEqual(report["automatic_checks"], "incomplete")
        self.assertFalse(report["duration_completed"])
        self.assertEqual(report["errors"], [])

    def test_manual_capture_can_finish_after_a_successful_stall(self):
        report = self.report(6.64, 6.626667, stop_source=6.6, capture_seconds=6.6)
        hardware.finish_checks(report, 0)
        self.assertEqual(report["automatic_checks"], "pass")

    def test_capture_errors_take_priority_over_incomplete_duration(self):
        report = self.report(6.64, 5, stop_source=6.6, capture_seconds=6.6)
        hardware.finish_checks(report, 60)
        self.assertEqual(report["automatic_checks"], "fail")


@unittest.skipUnless(HAS_TOOLS and os.name == "posix", "Linux FFmpeg tools required")
class CaptureIntegrationChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory(prefix="capture-hardware-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.session = Path(cls.directory.name) / "finished"
        cls.capture_run = subprocess.run([sys.executable, "-B", str(SCRIPT), "--synthetic", "--headless",
                                  "--duration", "9", "--stall-after", "2", "--output", str(cls.session)],
                                 capture_output=True, text=True, timeout=45)
        cls.report = json.loads((cls.session / "report.json").read_text())

    def test_complete_capture_preserves_content_timing_and_recovers(self):
        self.assertEqual(self.capture_run.returncode, 0, self.capture_run.stdout + self.capture_run.stderr)
        self.assertEqual(self.report["automatic_checks"], "pass")
        self.assertFalse(self.report["hardware_verified"])
        self.assertTrue(self.report["clean_exit"])
        self.assertEqual(self.report["ffmpeg_returncode"], 255)
        self.assertFalse(self.report["stop"]["killed"])
        self.assertTrue(self.report["duration_completed"])
        self.assertGreaterEqual(self.report["capture_seconds"], 9)
        self.assertLess(self.report["capture_seconds"], self.report["elapsed_seconds"])
        for track in ("video", "audio"):
            self.assertLessEqual(self.report["master"][track]["end_lag_at_stop_seconds"], hardware.END_LAG_SECONDS)
        self.assertAlmostEqual(self.report["master"]["audio_minus_video_start_seconds"], 0.12, places=3)
        self.assertGreater(self.report["master"]["video"]["packets"], 200)
        self.assertGreater(self.report["master"]["field_order"]["idet_counts"]["tff"], 100)
        stall = self.report["stalls"][0]
        self.assertGreater(stall["skipped_frames"], 0)
        self.assertGreater(stall["recovery_source_seconds"] * 1000, stall["cutoff_ms"])
        self.assertLessEqual(stall["recovery_delay"], hardware.RECOVERY_SECONDS)
        self.assertGreater(stall["advances"]["audio"], 2.5)

    def test_packets_were_observed_from_the_file_during_the_stall(self):
        with (self.session / "packets.jsonl").open() as source:
            packets = [json.loads(line) for line in source]
        for index in (0, 1):
            during = [packet for packet in packets if int(packet["stream_index"]) == index
                      and 2.2 < packet["observed_at"] < 4.8]
            self.assertGreater(len(during), 20)
            self.assertGreater(Fraction(during[-1]["pts_time"]) - Fraction(during[0]["pts_time"]), 2)

    def test_sigterm_during_stall_finalizes_without_claiming_recovery(self):
        session = Path(self.directory.name) / "interrupted"
        with subprocess.Popen([sys.executable, "-B", str(SCRIPT), "--synthetic", "--headless", "--duration", "0",
                               "--stall-after", "2", "--output", str(session)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                deadline = time.monotonic() + 10
                while not (session / "master.mkv").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("Capture did not start")
                    time.sleep(0.05)
                time.sleep(3)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=25)
            finally:
                if process.poll() is None:
                    process.kill()
            report = json.loads((session / "report.json").read_text())
            self.assertEqual(process.returncode, 1, stdout + stderr)
            self.assertEqual(report["stop"]["reason"], "SIGTERM")
            self.assertTrue(report["clean_exit"])
            self.assertEqual(report["automatic_checks"], "incomplete")
            self.assertEqual(report["errors"], [])
            self.assertGreater(report["master"]["video"]["packets"], 50)

    def ctrl_c_capture(self, duration):
        session = Path(self.directory.name) / f"ctrl-c-{duration}"
        with subprocess.Popen([sys.executable, "-B", str(SCRIPT), "--synthetic", "--headless", "--duration", str(duration),
                               "--stall-after", "2", "--output", str(session)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
            try:
                deadline = time.monotonic() + 10
                while not (session / "master.mkv").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("Capture did not start")
                    time.sleep(0.05)
                time.sleep(6)
                # A terminal delivers Ctrl+C to a whole foreground process group,
                # not just Python. The observer must survive until the master closes.
                os.killpg(process.pid, signal.SIGINT)
                stdout, stderr = process.communicate(timeout=25)
            finally:
                if process.poll() is None:
                    process.kill()
            report = json.loads((session / "report.json").read_text())
            self.assertEqual(report["stop"]["reason"], "SIGINT")
            self.assertTrue(report["clean_exit"])
            self.assertEqual(report["errors"], [])
            self.assertTrue(report["stalls"])
            return process.returncode, report, stdout + stderr

    def test_terminal_ctrl_c_keeps_the_observer_alive_until_finalization(self):
        code, report, output = self.ctrl_c_capture(0)
        self.assertEqual(code, 0, output)
        self.assertEqual(report["automatic_checks"], "pass")

    def test_ctrl_c_after_recovery_cannot_pass_a_shortened_fixed_duration(self):
        code, report, output = self.ctrl_c_capture(60)
        self.assertEqual(code, 1, output)
        self.assertEqual(report["automatic_checks"], "incomplete")
        self.assertFalse(report["duration_completed"])
        self.assertLess(report["capture_seconds"], 60)

    def run_modified_capture(self, session, duration, command_builder):
        output = StringIO()
        with patch.object(hardware, "preflight", return_value={}), \
                patch.object(hardware, "capture_command", side_effect=command_builder), \
                redirect_stdout(output), redirect_stderr(output):
            code = hardware.main(["--synthetic", "--headless", "--duration", str(duration),
                                  "--stall-after", "2", "--output", str(session)])
        return code, json.loads((session / "report.json").read_text()), output.getvalue()

    def test_audio_eof_near_stop_fails_even_when_audit_and_stall_pass(self):
        original_command = hardware.capture_command

        def shortened_audio(args, origin):
            command = original_command(args, origin)
            source = command.index("-i") + 1
            command[source] = command[source].replace("c=stereo,asetpts", "c=stereo,atrim=duration=6.5,asetpts")
            # Keep the video device paced independently after the audio device ends.
            command[source] = command[source].replace("setsar=16/15,settb=AVTB", "setsar=16/15,realtime,settb=AVTB")
            return command

        code, report, output = self.run_modified_capture(Path(self.directory.name) / "audio-eof", 8, shortened_audio)
        self.assertEqual(code, 1, output)
        self.assertEqual(report["automatic_checks"], "fail")
        self.assertTrue(report["clean_exit"] and report["duration_completed"] and report["stalls"])
        self.assertAlmostEqual(report["master"]["audio"]["end_pts"], 6.62, places=3)
        self.assertGreater(report["master"]["video"]["end_pts"], 7.8)
        self.assertAlmostEqual(report["master"]["relative_clock_span_error_seconds"], 0, places=3)
        self.assertEqual(len(report["errors"]), 1, report["errors"])
        self.assertIn("audio ended", report["errors"][0])

    def test_delayed_session_setup_preserves_timing_without_false_clock_alarm(self):
        delay = 0.75
        original_command = hardware.capture_command
        original_write = hardware.write_json

        def delayed_device_start(args, origin):
            command = original_command(args, origin)
            source = command.index("-i") + 1
            # Real devices timestamp at launch, after setup. Emulate that using
            # generated frames; the shared output origin remains unchanged.
            command[source] = command[source].replace(f"{origin:.6f}", f"{origin + delay:.6f}")
            return command

        def slow_session_write(path, value):
            original_write(path, value)
            if path.name == "session.json":
                time.sleep(delay)

        with patch.object(hardware, "write_json", side_effect=slow_session_write):
            code, report, output = self.run_modified_capture(
                Path(self.directory.name) / "slow-setup", 7, delayed_device_start)
        self.assertEqual(code, 0, output)
        self.assertEqual(report["automatic_checks"], "pass")
        self.assertEqual(report["errors"], [])
        self.assertGreaterEqual(report["stop_source_seconds"] - report["capture_seconds"], delay)
        self.assertAlmostEqual(report["master"]["video"]["first_pts"], delay, places=3)
        self.assertAlmostEqual(report["master"]["audio_minus_video_start_seconds"], 0.12, places=3)


if __name__ == "__main__":
    unittest.main()
