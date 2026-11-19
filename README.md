# Capture

Capture is a VHS capture project for Fedora. The intended workflow is to record a lossless master once, then make viewing copies from that file. The first runnable prototype is a small live preview for monitoring a recording.

## Try the live preview

On Linux, use Python 3 with PySide6 and a system FFmpeg with the lavfi input, FFV1 encoder and fifo muxer. From the repository root, run

```sh
python3 examples/preview_check.py
```

A 320×240 window shows PAL colour bars, a moving marker and a flashing cue. The picture deliberately freezes from second two to second five to simulate a viewer that falls behind. Leave the window open. Once recording finishes, it closes and prints the comparison result. The check uses a generated signal with stereo audio and cleans up its temporary captures automatically.

For a machine without a display, run

```sh
QT_QPA_PLATFORM=offscreen python3 examples/preview_check.py
```

## How the viewer shares the capture

FFmpeg reads the input once and splits the video into two outputs.

| Output | What gets written |
| --- | --- |
| Lossless master | Full 720×576, 25 fps interlaced PAL video at 4:3. FFV1 level 3 with slice CRC preserves the 4:2:2 picture, and stereo PCM preserves the 48 kHz sound. |
| Live viewer | A 320×240, 12.5 fps RGB picture. A two-packet FIFO drops preview frames when the viewer falls behind. The PySide6 window reads complete frames from this output. |

The test generates the same eight seconds twice, once with the preview off and once with it on. To pass, the viewer must display frames before and after the stall, skip part of its feed, and the two masters must match on all 200 decoded video frames, their timestamps, and all 384,000 stereo sample pairs. FFmpeg may log `FIFO queue full` when the preview reader pauses. The test checks the master separately.

## Hardware handoff

The prototype runs entirely on synthetic input. The next application milestone adds the operator’s Record and Stop controls, session files, validation and MP4 export. For the Hauppauge USB-Live2, the recorder will own the video and audio devices and feed the viewer from that same recording session. Sustained capture, field order and audio sync still need verification on the Fedora workstation with a real tape.
