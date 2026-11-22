"""Create a timed emoji overlay from local face detections."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import subprocess
import tempfile


MODEL_NAME = "face_detection_yunet_2023mar.onnx"
SCAN_RATE = 15
HOLD_FRAMES = 3
CIRCLE = "m 50 0 b 78 0 100 22 100 50 b 100 78 78 100 50 100 b 22 100 0 78 0 50 b 0 22 22 0 50 0"
EYES = (
    "m 0 0 l 0 0 "
    "m 32 27 b 23 27 23 49 32 49 b 41 49 41 27 32 27 "
    "m 68 27 b 59 27 59 49 68 49 b 77 49 77 27 68 27"
)
SMILE = "m 0 0 l 0 0 m 21 55 b 39 66 61 66 79 55 b 73 89 27 89 21 55"


def overlay_available(tools):
    return (tools / MODEL_NAME).is_file() and importlib.util.find_spec("cv2") is not None


def _overlap(left, right):
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    width = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0, min(ay + ah, by + bh) - max(ay, by))
    return width * height / max(1, min(aw * ah, bw * bh))


def _hold_faces(previous, seen):
    # Keep a box briefly when the detector drops a face between nearby frames.
    held = [(box, age + 1) for box, age in previous
            if age < HOLD_FRAMES and all(_overlap(box, current) < .35 for current in seen)]
    return [(box, 0) for box in seen] + held


def _timestamp(seconds):
    ticks = round(seconds * 100)
    hours, remaining = divmod(ticks, 360000)
    minutes, remaining = divmod(remaining, 6000)
    seconds, ticks = divmod(remaining, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{ticks:02}"


def _emoji_events(start, end, rectangle):
    x, y, width, height = rectangle
    radius = math.ceil(math.hypot(width, height) * .61)
    left = round(x + width / 2 - radius)
    top = round(y + height / 2 - radius)
    scale = 2 * radius
    transform = rf"{{\an7\pos({left},{top})\fscx{scale}\fscy{scale}\p1}}"
    begin, finish = _timestamp(start), _timestamp(end)
    return [
        f"Dialogue: {layer},{begin},{finish},{style},,0,0,0,,{transform}{drawing}\n"
        for layer, style, drawing in ((0, "Emoji", CIRCLE), (1, "Features", EYES), (1, "Features", SMILE))
    ]


def write_emoji_overlay(video, start, end, framing, width, height, model_dir, target):
    """Write an ASS overlay in the coordinate space of the exported crop."""
    if not overlay_available(model_dir):
        raise RuntimeError("Face emoji needs OpenCV and the local YuNet model. Run SETUP_WINDOWS.bat.")
    import cv2
    import numpy

    sample_width = 320
    sample_height = round(height * sample_width / width / 2) * 2
    detector = cv2.FaceDetectorYN.create(str(model_dir / MODEL_NAME), "",
                                         (sample_width, sample_height), .72, .30, 100)
    duration = end - start
    filters = [*framing, f"fps={SCAN_RATE}", f"scale={sample_width}:{sample_height}"]
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(Path(video).resolve()),
        "-t", f"{duration:.3f}", "-an", "-vf", ",".join(filters), "-pix_fmt", "bgr24",
        "-f", "rawvideo", "pipe:1",
    ]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {sample_width}
PlayResY: {sample_height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Emoji,Arial,24,&H0042CCFF,&H0042CCFF,&H002C659F,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1
Style: Features,Arial,24,&H00262137,&H00262137,&H00262137,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    active = []
    frame_index = covered = 0
    frame_bytes = sample_width * sample_height * 3
    with tempfile.TemporaryFile() as errors, target.open("w", encoding="utf-8") as overlay:
        overlay.write(header)
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as scan:
            try:
                while data := scan.stdout.read(frame_bytes):
                    if len(data) != frame_bytes:
                        break
                    frame = numpy.frombuffer(data, dtype=numpy.uint8).reshape(sample_height, sample_width, 3)
                    _, matches = detector.detect(frame)
                    seen = [tuple(float(coord) for coord in match[:4]) for match in matches] if matches is not None else []
                    active = _hold_faces(active, seen)
                    begin = frame_index / SCAN_RATE
                    finish = min(duration, (frame_index + 1) / SCAN_RATE)
                    if active and finish > begin:
                        for box, _ in active:
                            overlay.writelines(_emoji_events(begin, finish, box))
                        covered += 1
                    frame_index += 1
                if scan.wait():
                    errors.seek(0)
                    raise RuntimeError("Face scan failed: " + errors.read()[-1400:].decode(errors="replace"))
            finally:
                if scan.poll() is None:
                    scan.kill()
    return covered
