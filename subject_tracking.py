"""Offline face and motion tracking for portrait crops."""
import subprocess

from face_emoji import MODEL_NAME, overlay_available


def tracked_crop(video, start, end, width, height, tools):
    if not overlay_available(tools):
        raise RuntimeError("Smart framing needs OpenCV and the YuNet model. Run SETUP_WINDOWS.bat.")
    import cv2
    import numpy as np

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(start), "-i", str(video),
           "-t", str(end-start), "-an", "-vf", "fps=2,scale=320:180:force_original_aspect_ratio=decrease,"
           "pad=320:180:(ow-iw)/2:(oh-ih)/2", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    scan = subprocess.run(cmd, capture_output=True)
    if scan.returncode:
        raise RuntimeError("Tracking scan failed: " + scan.stderr.decode(errors="replace")[-600:])
    detector = cv2.FaceDetectorYN.create(str(tools / MODEL_NAME), "", (320, 180), .75, .3, 100)
    frame_size = 320 * 180 * 3
    previous, center = None, .5
    positions = []
    for offset in range(0, len(scan.stdout) - frame_size + 1, frame_size):
        frame = np.frombuffer(scan.stdout, dtype=np.uint8, count=frame_size,
                              offset=offset).reshape(180, 320, 3)
        _, faces = detector.detect(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        target = center
        if faces is not None and len(faces):
            x, _, w, *_ = max(faces, key=lambda box: box[2] * box[3])
            target = float(x + w / 2) / 320
        elif previous is not None:
            difference = cv2.absdiff(gray, previous)
            _, moving = cv2.threshold(difference, 25, 255, cv2.THRESH_BINARY)
            mass = cv2.moments(moving, binaryImage=True)
            if 300 < mass["m00"] < 16000:
                target = mass["m10"] / mass["m00"] / 320
        previous = gray
        center += max(-.10, min(.10, .35 * (target - center)))
        positions.append(center)
    # Linearly interpolate sampled positions. Correct for the tracking input's padding.
    if not positions:
        return f"crop={width}:{height}:(iw-ow)/2"
    aspect = f"iw/ih"
    fraction = f"max(0,min(1,0.5+((TRACK)-0.5)*max(1,(16/9)/({aspect}))))"
    expression = f"{positions[-1]:.5f}"
    for i in reversed(range(len(positions)-1)):
        left, right = positions[i:i+2]
        expression = f"if(lt(t,{(i+1)/2}),{left:.5f}+{2*(right-left):.5f}*(t-{i/2}),{expression})"
    fraction = fraction.replace("TRACK", expression)
    return f"crop={width}:{height}:'max(0,min(iw-ow,iw*({fraction})-ow/2))'"
