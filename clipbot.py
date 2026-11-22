#!/usr/bin/env python3
"""ClipBot Windows: local video to Shorts pipeline, Python stdlib + FFmpeg.

Local speech recognition, scene-based highlights, captioning and narration.
"""
from __future__ import annotations

import argparse
import html
import json
import importlib.util
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from bisect import bisect_left, bisect_right
from contextlib import nullcontext
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from pipeline_addons import validate_youtube_url, fetch_authorized_video, generate_hook, narrate, voice_available, suggest_title, suggest_description
import youtube_api
from monitor import start_monitor
from face_emoji import overlay_available, write_emoji_overlay
from shorts_captions import write_pop_captions
import creator_studio
import postiz_drafts
from subject_tracking import tracked_crop

ROOT = Path(os.environ.get("CLIPBOT_HOME", str(Path.home() / "Videos" / "ClipBot" if os.name == "nt" else Path.home() / "clipbot"))).expanduser().resolve()
INPUT = ROOT / "input"
OUTPUT = ROOT / "output"
WORK = ROOT / "work"
TOOLS = Path(os.environ.get("CLIPBOT_TOOLS", str(Path(__file__).with_name("tools")))).expanduser()
WHISPER_BIN = Path(os.environ.get("WHISPER_BIN", str(TOOLS / "whisper.cpp" / "build" / "bin" / ("whisper-cli.exe" if os.name == "nt" else "whisper-cli")))).expanduser()
WHISPER_MODEL = Path(os.environ.get("WHISPER_MODEL", str(TOOLS / "whisper.cpp" / "models" / "ggml-base.en.bin"))).expanduser()
ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_UPLOAD = 2_000_000_000

def public_origin():
    value = os.environ.get("CLIPBOT_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if not value:
        return ""
    address = urlsplit(value)
    if (address.scheme != "https" or not address.hostname or address.username or address.password
            or address.path or address.query or address.fragment or not re.fullmatch(
                r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])", address.netloc)):
        raise ValueError("CLIPBOT_PUBLIC_ORIGIN must be a bare HTTPS hostname.")
    return value


for p in (INPUT, OUTPUT, WORK):
    p.mkdir(parents=True, exist_ok=True)

lock = threading.Lock()
state = {"busy": False, "stage": "Ready", "progress": 0, "message": "Upload a video to begin.",
         "files": sorted(x.name for x in OUTPUT.glob("*.mp4") if x.is_file()), "error": None, "job": None, "job_kind": None, "mode": "", "logs": [], "last_upload": None, "started_at": None, "finished_at": None, "updated_at": None, "active_step": 0}


STEP_OF_STAGE = {
    "Queued": 0, "Importing source": 0, "Analyzing video": 0,
    "Extracting speech": 1, "Transcribing locally": 1,
    "Scanning scenes": 2, "Selecting clips": 2, "Rendering clips": 3,
    "Clips exported": 4, "Finished": 4, "Finished + privately uploaded": 5,
    "Publishing to YouTube": 5, "Uploading private drafts": 5,
    "Uploaded to YouTube": 5, "Preparing variation": 3,
    "Rendering revision": 3, "Revision ready": 4, "Rendering storyboard": 3,
}


def update(stage=None, progress=None, message=None):
    with lock:
        if stage is not None:
            state["stage"] = stage
            if stage in STEP_OF_STAGE:
                state["active_step"] = STEP_OF_STAGE[stage]
        state["updated_at"] = int(time.time())
        if progress is not None:
            state["progress"] = progress
        if message is not None:
            state["message"] = message
            state["logs"].append(message)
            state["logs"] = state["logs"][-30:]
    if message:
        print(f"[{stage or 'ClipBot'}] {message}", flush=True)


def run(cmd, *, description="Processing", cwd=None):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", cwd=cwd)
    if proc.returncode:
        details = (proc.stderr or proc.stdout)[-1500:].strip()
        raise RuntimeError(f"{description} failed: {details}")
    return (proc.stdout or "").strip()


def output_files(pattern):
    yield from OUTPUT.glob(pattern)
    for folder in OUTPUT.iterdir():
        if folder.is_dir() and not folder.is_symlink() and not getattr(folder, "is_junction", lambda: False)():
            yield from folder.glob(pattern)


def valid_output_name(name):
    return isinstance(name, str) and name == Path(name).name and ":" not in name and name.endswith(".mp4")


def output_path(name):
    path = OUTPUT / name
    if not valid_output_name(name):
        raise ValueError("Invalid MP4 name")
    if path.is_symlink():
        return OUTPUT / ".unavailable"
    if path.is_file():
        return path
    for folder in OUTPUT.iterdir():
        if folder.is_dir() and not folder.is_symlink() and not getattr(folder, "is_junction", lambda: False)():
            candidate = folder / name
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
    return path


def local_export(name):
    path = output_path(name)
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Local export not found")
    return path


def export_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def remove_local_export(name, expected=None):
    path = local_export(name)
    current = export_identity(path)
    # Never delete a replacement that appeared after the upload began.
    if expected is not None and (path, current) != expected:
        raise ValueError("The local export changed during upload.")
    path.unlink()
    return current[2]


def upload_export(filename, title, description, privacy, delete_after_upload=False):
    path = local_export(filename)
    expected = (path, export_identity(path)) if delete_after_upload else None
    video_id = youtube_api.yt_upload(path, title, description, privacy)
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        raise RuntimeError("YouTube did not return a valid video ID. Local copy kept.")
    youtube_api.record_upload(filename, video_id, title, privacy)
    cleanup = ""
    if delete_after_upload:
        update(message="Checking that YouTube finished processing before removing the local copy.")
        try:
            for attempt in range(4):
                status = youtube_api.upload_status(video_id)
                if status == "processed":
                    freed = remove_local_export(filename, expected)
                    with lock:
                        state["files"] = [name for name in state["files"] if name != filename]
                    cleanup = f"Local MP4 deleted. Freed {freed / 1048576:.2f} MiB."
                    break
                if status in ("failed", "rejected", "deleted") or attempt == 3:
                    cleanup = f"Local copy kept. YouTube status: {status or 'unconfirmed'}."
                    break
                time.sleep(5)
        except (OSError, ValueError, RuntimeError) as error:
            logging.warning("Could not complete local cleanup: %s", error)
            cleanup = "Local copy kept. Cleanup could not be verified or completed."
        update(message=cleanup)
    return video_id, cleanup


def downloader_installed():
    return bool(shutil.which("yt-dlp") or importlib.util.find_spec("yt_dlp"))


def safe_file(name):
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9 _.()\-]", "_", name).strip(" .")
    return name[:120] or "upload.mp4"


def probe_duration(video):
    raw = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(video)], description="Video probe")
    result = float(raw)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Unable to detect valid video duration")
    return result


def srt_seconds(value):
    x = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", value)
    if not x:
        raise ValueError(f"Bad SRT timestamp: {value}")
    h, m, s, ms = (int(t) for t in x.groups())
    return h * 3600 + m * 60 + s + ms / (10 ** len(x.group(4)))


def srt_timestamp(t):
    t = max(0., t)
    millis = round(t * 1000)
    h, remain = divmod(millis, 3600000)
    m, remain = divmod(remain, 60000)
    s, ms = divmod(remain, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@dataclass
class Segment:
    start: float
    end: float
    text: str
    captions: tuple[Segment, ...] = ()
    words: tuple[Segment, ...] = ()


def parse_srt(path):
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    segments = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        for j, line in enumerate(lines):
            if "-->" not in line:
                continue
            start_txt, end_txt = (x.strip().split(" ")[0] for x in line.split("-->", 1))
            try:
                start, end = srt_seconds(start_txt), srt_seconds(end_txt)
            except ValueError:
                break
            txt = " ".join(lines[j+1:])
            if txt and end > start:
                segments.append(Segment(start, end, txt))
            break
    return sorted(segments, key=lambda x: x.start)


def make_clip_subtitle(segments, start, end, out):
    lines = []
    idx = 0
    for seg in (cue for segment in segments for cue in (segment.captions or (segment,))):
        if seg.end <= start or seg.start >= end:
            continue
        a, b = max(0, seg.start - start), min(end-start, seg.end-start)
        if b - a < .12:
            continue
        idx += 1
        clean = re.sub(r"\s+", " ", seg.text).strip()
        # Keep captions readable and avoid untrusted ASS override syntax via SRT.
        clean = clean.replace("{", "(").replace("}", ")")
        lines.append(f"{idx}\n{srt_timestamp(a)} --> {srt_timestamp(b)}\n{clean}\n")
    out.write_text("\n".join(lines), encoding="utf-8")
    return idx


HOOKS = ("why", "how", "never", "secret", "mistake", "crazy", "surprise", "actually",
         "wait", "imagine", "important", "best", "worst", "here's", "look", "listen",
         "problem", "finally", "didn't", "can't", "amazing", "unexpected", "nobody")


def score_window(segments, start, end):
    text = " ".join(seg.text.lower() for seg in segments if seg.start < end and seg.end > start)
    words = re.findall(r"\b[\w']+\b", text)
    if not words:
        return -50
    opening = " ".join(seg.text.lower() for seg in segments if start <= seg.start < start + 10)
    hooks = sum(1 for word in HOOKS if re.search(r"\b" + re.escape(word) + r"\b", opening))
    density = len(words) / max(1, (end-start)) * 60
    score = min(hooks, 4) * 3.5 + (2. if "?" in opening else 0.) + (1. if "!" in text else 0.)
    score += 1. if 65 <= density <= 190 else (-3. if density < 30 else 0.)
    score += min(len(words)/35., 4.)
    # Avoid awkward intros/outros when no complete phrase is available.
    if text.strip().endswith((".", "?", "!")):
        score += 1.
    return round(score, 2)


def scene_changes(video):
    """Find large frame changes on a downscaled preview."""
    out = run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
               "-an", "-vf", "fps=4,scale=160:-2,select='gt(scene,0.3)',metadata=print:file=-",
               "-f", "null", "-"], description="Scene scan")
    return tuple(float(time) for time in re.findall(r"pts_time:([\d.]+)", out))


def gameplay_signals(video):
    """Sample motion and game audio using inexpensive FFmpeg filters."""
    motion = run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
                  "-an", "-vf", "fps=4,scale=160:-2,format=yuv420p,scdet=t=0,metadata=print:file=-",
                  "-f", "null", "-"], description="Gameplay motion scan")
    frames = re.findall(r"pts_time:([\d.]+).*?lavfi\.scd\.mafd=([\d.]+)", motion, re.S)
    signals = [(float(t), min(float(value), 20) / 20, "visual motion")
               for t, value in frames]
    if has_audio(video):
        audio = run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video),
                     "-vn", "-ac", "1", "-af",
                     "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
                     "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                     "-f", "null", "-"], description="Gameplay audio scan")
        levels = [(float(t), float(value)) for t, value in
                  re.findall(r"pts_time:([\d.]+).*?RMS_level=(-?[\d.]+|-inf)", audio, re.S)
                  if value != "-inf"]
        if levels:
            baseline = sorted(level for _, level in levels)[len(levels) // 2]
            signals.extend((t, min((level - baseline) / 24, 1), "audio peak")
                           for t, level in levels if level >= baseline + 6)
    return signals


def pick_clips(segments, total, count=0, length=40, scenes=(), *, gameplay=None):
    """Select non-overlapping, source-grounded clips in chronological order."""
    if length <= 0 or total <= 0 or count < 0:
        raise ValueError("Invalid clip duration or count")
    if total <= length:
        return [(0., total, "Entire short source video", None)]
    candidates = []
    minimum = min(12, length * .6)
    segments = sorted(segments, key=lambda segment: segment.start)
    scenes = sorted(scenes)
    sentence_starts = [segment.start for segment in segments]
    last_speech_end = []
    for segment in segments:
        last_speech_end.append(max(segment.end, last_speech_end[-1] if last_speech_end else 0.))
    for i, seg in enumerate(segments):
        start = max(0., seg.start - .15)
        end = start
        limit = min(total, start + length)
        window = segments[i:bisect_right(sentence_starts, limit)]
        for following in window:
            if following.end <= limit:
                end = max(end, following.end)
        if end - start >= minimum:
            changes = bisect_left(scenes, end) - bisect_left(scenes, start)
            score = score_window(window, start, end) + min(changes, 5) * .5
            candidates.append((start, end, "Speech highlight", score))
    for cut in scenes:
        start = max(0., min(cut - 2, total - length))
        end = start + length
        j = bisect_left(sentence_starts, end) - 1
        if j >= 0 and last_speech_end[j] > start:
            continue
        changes = bisect_left(scenes, end) - bisect_left(scenes, start)
        score = .25 + min(5, changes) * .5
        candidates.append((start, end, "Visual scene change", score))
    if gameplay is not None:
        # Keep the strongest motion observation per second and cap the effect of cuts.
        bins = {}
        for timestamp, score, label in gameplay:
            if score >= .18 and 0 <= timestamp < total:
                second = bins.setdefault(int(timestamp), {})
                second[label] = max(second.get(label, 0), score)
        events = []
        for second, signals in sorted(bins.items()):
            if not events or second > events[-1][-1][0] + 2:
                events.append([])
            events[-1].append((second, signals))
        for event in events:
            peak, _ = max(event, key=lambda item: sum(item[1].values()))
            start = max(0., min(peak - length * .3, total - length))
            labels = sorted({label for t, signals in event if start <= t < start + length
                             for label in signals})
            scores = sorted((sum(signals.values()) for t, signals in event
                             if start <= t < start + length), reverse=True)
            score = scores[0] + .2 * sum(scores[1:5])
            candidates.append((start, start + length,
                               f"Gameplay {' + '.join(labels)} around {peak}s", score))
        if candidates:
            # Compare gameplay evidence on its own scale when present.
            candidates = [candidate for candidate in candidates if candidate[2].startswith("Gameplay")] or candidates
    if not candidates and not segments and not scenes:
        # Explicit timeline sampling when the source has no ranked candidates.
        slots = max(1, int(total // length))
        count = min(count or slots, slots)
        gap = (total - length) / max(1, count - 1)
        return [(i*gap, i*gap+length, "Evenly spaced fallback", None) for i in range(count)]
    chosen = []
    offsets = []
    for candidate in sorted(candidates, key=lambda c: (-c[3], c[0])):
        if count and len(chosen) == count:
            break
        start, end = candidate[:2]
        index = bisect_left(offsets, start)
        if index and chosen[index-1][1] > start:
            continue
        if index < len(chosen) and offsets[index] < end:
            continue
        offsets.insert(index, start)
        chosen.insert(index, candidate)
    return chosen


def transcribe(video, jobdir, duration, *, report=True):
    notify = update if report else lambda *args, **kwargs: None
    if not has_audio(video):
        return [], "no audio"
    model = TOOLS / "whisper-model"
    if (model / "model.bin").is_file() and importlib.util.find_spec("faster_whisper"):
        try:
            import numpy as np
            from faster_whisper import WhisperModel

            notify("Extracting speech", 15, "Decoding mono audio for Whisper")
            decoded = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video), "-map", "0:a:0",
                 "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
                capture_output=True,
            )
            if decoded.returncode:
                raise RuntimeError(decoded.stderr.decode(errors="replace").strip())
            audio = np.frombuffer(decoded.stdout, dtype="<f4")
            notify("Transcribing locally", 25, "Running Whisper with voice activity detection")
            recognizer = WhisperModel(str(model), device="cpu", compute_type="int8")
            language = os.environ.get("CLIPBOT_LANGUAGE") or None
            detected, _ = recognizer.transcribe(audio, language=language, vad_filter=True,
                                                 word_timestamps=True)
            segments = []
            for part in detected:
                words = part.words or []
                cues = []
                for index in range(0, len(words), 5):
                    group = words[index:index+5]
                    timed_words = tuple(Segment(w.start, w.end, w.word) for w in group)
                    cues.append(Segment(group[0].start, group[-1].end,
                                        "".join(w.word for w in group).strip(), words=timed_words))
                segments.append(Segment(part.start, part.end, part.text.strip(), tuple(cues)))
                notify(progress=min(49, 25 + int(24 * part.end / duration)))
            if segments:
                notify(message=f"Detected {len(segments)} timed speech segments")
                return segments, "local Whisper speech"
            return [], "no speech detected"
        except (RuntimeError, OSError, ValueError, ImportError) as exc:
            notify(message=f"Local speech recognition unavailable: {exc}")
    if not (WHISPER_BIN.is_file() and WHISPER_MODEL.is_file()):
        notify(message="No usable Whisper model; captions require a transcript or embedded subtitles.")
        return [], "transcription unavailable"
    audio = jobdir / "audio.wav"
    prefix = jobdir / "transcript"
    update("Extracting speech", 15, "Extracting 16 kHz mono audio")
    try:
        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio)], description="Audio extraction")
        notify("Transcribing locally", 30, "Whisper is running on this computer; larger sources may take a while")
        run([str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(audio),
             "-l", "en", "-t", "4", "-osrt", "-of", str(prefix)], description="Whisper transcription")
        segments = parse_srt(prefix.with_suffix(".srt"))
        if segments:
            notify(message=f"Detected {len(segments)} timed speech segments")
            return segments, "local Whisper.cpp speech"
        notify(message="No usable speech segments detected")
        return [], "no speech detected"
    except (RuntimeError, OSError) as exc:
        notify(message=f"Speech recognition unavailable: {exc}. Proceeding with visual analysis.")
        return [], "transcription failed"
    finally:
        audio.unlink(missing_ok=True)


def has_audio(video):
    return bool(run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                     "stream=index", "-of", "csv=p=0", str(video)], description="Audio probe").strip())


def render_clip(video, start, end, out, captions_srt=None, width=720, narration_wav=None, *, face_emoji=False, caption_ass=None, framing_mode="default"):
    out = out.resolve()
    height = int(round(width * 16/9/2)) * 2
    crop_x = {"left": "0", "right": "iw-ow"}.get(os.environ.get("CLIPBOT_CROP"), "(iw-ow)/2")
    framing = [f"scale={width}:{height}:force_original_aspect_ratio=increase",
               f"crop={width}:{height}:{crop_x}"]
    if framing_mode == "fit":
        framing = [f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"]
    elif framing_mode == "smart":
        framing[1] = tracked_crop(video, start, end, width, height, TOOLS)
    elif framing_mode != "default":
        position = {"left": "0", "center": "(iw-ow)/2", "right": "iw-ow"}[framing_mode]
        framing[1] = f"crop={width}:{height}:{position}"
    filters = [*framing, "fps=30", "setsar=1"]
    scratch = tempfile.TemporaryDirectory(prefix="face-overlay-", dir=out.parent) if face_emoji else nullcontext()
    with scratch as filter_dir:
        if face_emoji:
            update(message="Scanning the exported frame for faces")
            overlay = Path(filter_dir) / "face_emoji.ass"
            covered = write_emoji_overlay(video, start, end, framing, width, height, TOOLS, overlay)
            if covered:
                filters.append("ass=face_emoji.ass")
                update(message="Added a smiley over detected faces. Review the export for any missed faces.")
            else:
                update(message="No faces detected in this clip. Review the export before sharing.")
        if caption_ass is not None:
            filter_dir = filter_dir or caption_ass.resolve().parent
            ass_target = Path(filter_dir) / "styled-captions.ass"
            if caption_ass.resolve() != ass_target.resolve():
                shutil.copyfile(caption_ass, ass_target)
            filters.append("ass=styled-captions.ass")
        if captions_srt is not None:
            # Fixed relative filenames keep Windows drive-letter paths out of filter expressions.
            filter_dir = filter_dir or captions_srt.resolve().parent
            caption_target = Path(filter_dir) / "captions.srt"
            if captions_srt.resolve() != caption_target.resolve():
                shutil.copyfile(captions_srt, caption_target)
            filters.append("subtitles=captions.srt:force_style='FontSize=25,Alignment=2,MarginV=65,Outline=2,Shadow=1,Bold=1'")
        filters.append("format=yuv420p")
        audio = has_audio(video)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-i", str(video)]
        if narration_wav:
            cmd.extend(["-i", str(narration_wav)])
        cmd.extend(["-t", f"{end-start:.3f}", "-vf", ",".join(filters),
                    "-map", "0:v:0"])
        if narration_wav and audio:
            cmd.extend(["-filter_complex", "[0:a:0]volume=0.35[bed];[1:a:0]volume=1.8[voice];[bed][voice]amix=inputs=2:duration=longest:dropout_transition=0[a]",
                        "-map", "[a]"])
        elif narration_wav:
            cmd.extend(["-map", "1:a:0"])
        elif audio:
            cmd.extend(["-map", "0:a:0"])
        else:
            cmd.append("-an")
        cmd.extend(["-threads", "2", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "26", "-c:a", "aac", "-b:a", "112k", "-movflags", "+faststart", str(out)])
        run(cmd, description="Clip rendering", cwd=filter_dir)


def process_video(file, count=0, length=40, width=720, captions=True, voice_mode="off", voice_script="", source_url="", will_upload=False, *, face_emoji=False, caption_style="classic", opening_text="", selection="classic", framing_mode="default"):
    if caption_style not in ("pop", "classic"):
        raise ValueError("Unknown caption style")
    if not isinstance(opening_text, str) or len(opening_text) > 55:
        raise ValueError("Opening text may contain at most 55 characters")
    opening_text = opening_text.strip()
    video = INPUT / safe_file(file)
    if not video.is_file() or video.suffix.lower() not in ALLOWED:
        raise ValueError("Choose a valid uploaded video")
    jobid = uuid.uuid4().hex[:8]
    jobdir = WORK / jobid
    jobdir.mkdir(parents=True, exist_ok=True)
    made = []
    try:
        update("Analyzing video", 5, f"Probing {video.name}")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("FFmpeg and ffprobe are required. Run SETUP_WINDOWS.bat and restart the terminal.")
        if face_emoji and not overlay_available(TOOLS):
            raise RuntimeError("Face emoji needs OpenCV and the local YuNet model. Run SETUP_WINDOWS.bat.")
        duration = probe_duration(video)
        embedded = jobdir / "embedded.srt"
        if run(["ffprobe", "-v", "error", "-select_streams", "s:0", "-show_entries",
                "stream=codec_name", "-of", "csv=p=0", str(video)], description="Subtitle probe") in {
                    "subrip", "mov_text", "ass", "ssa", "webvtt", "text"}:
            run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(video), "-map",
                 "0:s:0", str(embedded)], description="Embedded subtitle extraction")
        segments = parse_srt(embedded)
        speech = "embedded subtitles"
        if not segments:
            segments, speech = transcribe(video, jobdir, duration)
        elif captions and caption_style == "pop":
            timed, timing_method = transcribe(video, jobdir, duration)
            if any(cue.words for sentence in timed for cue in sentence.captions):
                segments, speech = timed, timing_method
            else:
                update(message="Word timing unavailable for this source. Using embedded phrase captions.")
        update("Scanning scenes", 50, "Analyzing visual scene changes")
        scenes = scene_changes(video)
        gameplay = None
        if selection == "gameplay":
            update("Scanning gameplay", 52, "Analyzing motion and audio peaks")
            gameplay = gameplay_signals(video)
        if segments:
            mode = f"{speech} + scene analysis"
        elif scenes:
            mode = f"visual highlights ({speech})"
        else:
            mode = f"evenly-spaced ({speech}; no scene changes)"
        update("Selecting clips", 55)
        clips = pick_clips(segments, duration, count, length, scenes, gameplay=gameplay)
        if any(reason.startswith("Gameplay") for _, _, reason, _ in clips):
            mode = f"gameplay motion and audio ({speech})"
        with lock:
            state["mode"] = mode
        update(message=f"Selected {len(clips)} clips ({mode})")
        if not clips:
            raise RuntimeError("No distinct highlights fit this duration. Try a shorter clip length.")
        exports = jobdir / f"{video.stem[:35]}_{jobid}"
        exports.mkdir()
        manifest = {"source": video.name, "source_url": source_url, "total_duration": duration, "selection_mode": mode,
                    "captions_requested": captions, "voice_mode": voice_mode, "face_emoji": face_emoji, "caption_style": caption_style,
                    "opening_text": opening_text, "framing": framing_mode, "clips": []}
        for index, (start, end, reason, score) in enumerate(clips, start=1):
            update("Rendering clips", round(55+40*(index-1)/len(clips)), f"Rendering clip {index}/{len(clips)}")
            filename = f"{video.stem[:35]}_{jobid}_short_{index:02d}.mp4"
            dest = exports / filename
            srt = None
            if segments and captions:
                srt = jobdir / f"clip_{index:02d}.srt"
                if make_clip_subtitle(segments, start, end, srt) == 0:
                    srt = None
            excerpt = " ".join(seg.text for seg in segments if seg.start < end and seg.end > start)
            voice_text = (voice_script[:700] if voice_mode == "custom" else
                          generate_hook(excerpt) if voice_mode == "auto" else "")
            voice_file = None
            if voice_mode != "off" and voice_text:
                voice_file = jobdir / f"voice_{index:02d}.wav"
                narrate(voice_text, voice_file)
            elif voice_mode == "auto":
                update(message="No transcript for automatic voice-over; keeping original audio")
            styled_captions = None
            pop = srt is not None and caption_style == "pop"
            if pop or opening_text:
                styled_captions = jobdir / "styled-captions.ass"
                write_pop_captions(segments if pop else (), start, end, styled_captions,
                                   opening_text=opening_text)
            render_clip(video, start, end, dest, captions_srt=None if pop else srt, width=width,
                        narration_wav=voice_file, face_emoji=face_emoji, caption_ass=styled_captions, framing_mode=framing_mode)
            if srt:
                shutil.copy2(srt, exports / (dest.stem + ".srt"))
            title = suggest_title(excerpt, number=index)
            desc = suggest_description(source_url, excerpt)
            (exports / (dest.stem + "_metadata.json")).write_text(json.dumps({"title": title, "description": desc, "voice_script": voice_text,
                 "reason": reason, "source_start": round(start, 2), "source_end": round(end, 2)}, indent=2), encoding="utf-8")
            made.append(filename)
            manifest["clips"].append({"filename": filename, "start": round(start,2), "end": round(end,2),
                                      "reason": reason, "heuristic_score": score, "captions": bool(srt),
                                      "title": title, "description": desc, "transcript_excerpt": excerpt[:1200],
                                      "voice_over": bool(voice_file), "voice_script": voice_text, "width": width})
        (exports / "job_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        final_dir = OUTPUT / exports.name
        exports.rename(final_dir)
        update("Clips exported" if will_upload else "Finished", 96 if will_upload else 100,
               f"Created {len(made)} MP4 clips in {final_dir}")
        return made, mode
    finally:
        shutil.rmtree(jobdir, ignore_errors=True)


def start_job(name, count, length, width, captions, voice_mode="off", voice_script="", url="", private_upload_after=False, *, delete_after_upload=False, face_emoji=False, caption_style="classic", opening_text="", selection="classic", framing_mode="default"):
    if url:
        validate_youtube_url(url)
    with lock:
        if state["busy"]:
            raise RuntimeError("A video is already processing")
        state.update(busy=True, stage="Queued", progress=0, message="Starting", files=[], error=None,
                     job=uuid.uuid4().hex[:8], job_kind="generate", started_at=int(time.time()), finished_at=None, logs=[], active_step=0, mode="")
    def background():
        try:
            if url:
                update("Importing source", 2, "Downloading the authorized YouTube video to this laptop")
                source_file = fetch_authorized_video(url, INPUT, report=lambda msg: update(message=msg))
                filename = source_file.name
            else:
                filename = name
            files, mode = process_video(filename, count, length, width, captions, voice_mode, voice_script,
                                        url, private_upload_after, face_emoji=face_emoji, caption_style=caption_style,
                                        opening_text=opening_text, selection=selection, framing_mode=framing_mode)
            with lock:
                state["files"] = files
                state["mode"] = mode
            if private_upload_after:
                for index, clip_filename in enumerate(files, start=1):
                    update("Uploading private drafts", 96 + 3 * (index - 1) // len(files),
                           f"Sending private Short {index}/{len(files)} to YouTube")
                    metadata_file = output_path(clip_filename).with_name(Path(clip_filename).stem + "_metadata.json")
                    metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
                    vid, cleanup = upload_export(clip_filename, metadata['title'], metadata['description'],
                                                 'private', delete_after_upload)
                    with lock:
                        state['last_upload'] = {"id": vid, "privacy": "private", "url": f"https://www.youtube.com/watch?v={vid}",
                                                "cleanup": cleanup}
                    update(message=f"Private YouTube draft {index}: {vid}")
                update("Finished + privately uploaded", 100, f"Exported and uploaded {len(files)} private Shorts")
        except Exception as exc:
            logging.exception("ClipBot job error")
            update("Failed", message=str(exc))
            with lock:
                state["error"] = str(exc)
        finally:
            with lock:
                state["busy"] = False
                state["finished_at"] = int(time.time())
    threading.Thread(target=background, daemon=True).start()


def start_studio(data):
    title, scenes = creator_studio.validate(data)
    source_name = data.get("source", "")
    if not isinstance(source_name, str) or (source_name and source_name != safe_file(source_name)):
        raise ValueError("Invalid background video")
    source = INPUT / source_name if source_name else None
    if source and (source.is_symlink() or not source.is_file() or source.suffix.lower() not in ALLOWED):
        raise ValueError("Choose an imported video for the background")
    if not voice_available():
        raise RuntimeError("Run SETUP_WINDOWS.bat to install the offline Piper narrator.")
    with lock:
        if state["busy"]:
            raise RuntimeError("A video is already processing")
        job = uuid.uuid4().hex[:8]
        state.update(busy=True, stage="Queued", progress=0, message="Preparing storyboard",
                     files=[], error=None, job=job, job_kind="generate", started_at=int(time.time()),
                     finished_at=None, logs=[], active_step=0, mode="local storyboard")

    def background():
        try:
            label = f"studio_{job}"
            filename = f"{label}.mp4"
            with tempfile.TemporaryDirectory(dir=WORK) as scratch:
                folder = Path(scratch)
                exports = folder / label
                exports.mkdir()
                duration = creator_studio.render(title, scenes, folder, exports / filename,
                    source=source, run=run, narrate=narrate, transcribe=transcribe, segment=Segment,
                    progress=lambda n, count: update("Rendering storyboard", 10 + 85 * n // count,
                                                      f"Narrating and rendering scene {n+1} of {count}"))
                description = "\n".join(scene["narration"] for scene in scenes)
                posts = {"YouTube Shorts": title + "\n\n" + description,
                         "Instagram Reels": title + "\n\n" + description,
                         "TikTok": title + " | " + scenes[0]["narration"][:280],
                         "Facebook Reels": title + "\n" + scenes[0]["narration"],
                         "X": title[:100] + " | " + scenes[0]["narration"][:160]}
                metadata = {"title": title, "description": description, "studio_posts": posts}
                (exports / f"{label}_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                (exports / "storyboard.json").write_text(json.dumps({"title": title, "scenes": scenes,
                    "duration": duration}, indent=2), encoding="utf-8")
                shutil.move(str(exports), str(OUTPUT / label))
            with lock:
                state["files"] = [filename]
            update("Finished", 100, "Storyboard video ready in My clips.")
        except Exception as error:
            logging.exception("Storyboard rendering failed")
            update("Failed", message=str(error))
            with lock:
                state["error"] = str(error)
        finally:
            with lock:
                state["busy"] = False
                state["finished_at"] = int(time.time())
    threading.Thread(target=background, daemon=True).start()


HTML = (Path(__file__).with_name("ui.html")).read_text(encoding="utf-8")


def make_variant(original_filename, experiment="shorter"):
    """Create a narrower second cut of exactly the source window used before.

    Suggestions are tentative and should be reviewed before posting. No auto upload.
    """
    if not valid_output_name(original_filename):
        raise ValueError('Invalid original clip')
    manifest = None
    item = None
    for mpath in output_files("*_manifest.json"):
        try:
            candidate = json.loads(mpath.read_text(encoding='utf-8'))
            found = next((c for c in candidate.get('clips', []) if c.get('filename') == original_filename), None)
            if found:
                manifest, item = candidate, found
                break
        except (OSError, ValueError, TypeError):
            continue
    if not item:
        raise RuntimeError('Original source manifest not available; cannot create this variation')
    source = INPUT / safe_file(manifest['source'])
    if not source.is_file():
        raise RuntimeError('Original source video is missing. Keep input files for revisions.')
    start = float(item['start'])
    previous_end = float(item['end'])
    original_duration = previous_end - start
    new_end = start + original_duration * .8 if experiment == "shorter" else previous_end
    if experiment == "shorter" and original_duration < 5:
        raise RuntimeError("Original clip is too short to trim further")
    stamp = uuid.uuid4().hex[:8]
    filename = f"{Path(original_filename).stem[:45]}_variant_{stamp}.mp4"
    target_dir = output_path(original_filename).parent
    target = target_dir / filename
    workdir = WORK / stamp
    workdir.mkdir(exist_ok=True)
    try:
        old_srt = output_path(original_filename).with_suffix('.srt')
        new_srt = workdir / 'variation.srt'
        subtitles = None
        if old_srt.is_file():
            if make_clip_subtitle(parse_srt(old_srt), 0, new_end-start, new_srt):
                subtitles = new_srt
        excerpt = item.get('transcript_excerpt', '')
        voice = None
        voice_text = generate_hook(excerpt) if experiment == 'hook' else item.get('voice_script', '')
        if voice_text and voice_available():
            voice = workdir / 'voice.wav'
            narrate(voice_text, voice)
        if not voice:
            voice_text = ""
        update("Rendering revision", 40, f"Rendering {experiment} experiment")
        width = item.get("width", 480)
        rendered = workdir / filename
        styled_captions = None
        pop = subtitles is not None and manifest.get("caption_style") == "pop"
        opening_text = manifest.get("opening_text", "")
        if pop or opening_text:
            styled_captions = workdir / "styled-captions.ass"
            write_pop_captions(parse_srt(subtitles) if pop else (), 0, new_end - start,
                               styled_captions, opening_text=opening_text)
        render_clip(source, start, new_end, rendered, framing_mode=manifest.get("framing", "default"), captions_srt=None if pop else subtitles, width=width,
                    narration_wav=voice, face_emoji=manifest.get("face_emoji") is True,
                    caption_ass=styled_captions)
        if subtitles:
            shutil.copy2(subtitles, target_dir / (target.stem + '.srt'))
        title = item.get("title", "A short highlight #Shorts")
        if experiment == "hook":
            title = ("Watch this: " + title.replace("#Shorts", "").strip())[:86].strip() + " #Shorts"
        description = item.get('description', 'Edited short #Shorts')
        (target_dir / (target.stem + '_metadata.json')).write_text(json.dumps({
            'title': title[:100], 'description': description, 'variant_of': original_filename,
            'change': '20% shorter' if experiment == 'shorter' else ('Alternate title and intro' if voice else 'Alternate title'),
            'voice_script': voice_text,
        }, indent=2), encoding='utf-8')
        revision = {**item, "filename": filename, "end": new_end, "title": title,
                    "voice_script": voice_text, "width": width}
        manifest_path = target_dir / (target.stem + "_manifest.json")
        manifest_path.write_text(json.dumps({**manifest, "clips": [revision]}, indent=2), encoding="utf-8")
        rendered.replace(target)
        update('Revision ready', 100, f'Exported candidate {filename} — review before upload')
        return filename
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def start_variant_job(filename, experiment="shorter"):
    with lock:
        if state['busy']:
            raise RuntimeError('A job is already processing')
        state.update(busy=True, stage='Preparing variation', progress=2, error=None,
                     message='Preparing second cut', files=[], last_upload=None, logs=[], job=uuid.uuid4().hex[:8], job_kind='revision', started_at=int(time.time()), finished_at=None, active_step=3)
    def background():
        try:
            output = make_variant(filename, experiment)
            with lock:
                state['files'] = [output]
        except Exception as exc:
            logging.exception('Revision failed')
            update('Revision failed', message=str(exc))
            with lock:
                state['error'] = str(exc)
        finally:
            with lock:
                state['busy'] = False
                state['finished_at'] = int(time.time())
    threading.Thread(target=background, daemon=True).start()


def start_upload_job(filename, title, desc, privacy, *, delete_after_upload=False):
    with lock:
        if state["busy"]:
            raise RuntimeError("A job is already running")
        state.update(busy=True, stage="Publishing to YouTube", progress=10, error=None,
                     message="Starting upload", logs=[], last_upload=None, job=uuid.uuid4().hex[:8], job_kind="upload", started_at=int(time.time()), finished_at=None, active_step=5)
    def background():
        try:
            video_id, cleanup = upload_export(filename, title, desc, privacy, delete_after_upload)
            update("Uploaded to YouTube", 100, f"Uploaded {filename} as {privacy}: https://www.youtube.com/watch?v={video_id}")
            with lock:
                state["last_upload"] = {"id": video_id, "privacy": privacy, "url": f"https://www.youtube.com/watch?v={video_id}",
                                        "cleanup": cleanup}
        except Exception as exc:
            logging.exception("Upload failed")
            update("Upload failed", message=str(exc))
            with lock:
                state["error"] = str(exc)
        finally:
            with lock:
                state["busy"] = False
                state["finished_at"] = int(time.time())
    threading.Thread(target=background, daemon=True).start()


def json_body(handler, limit=8192):
    try:
        size = int(handler.headers.get("Content-Length", "0"))
        if size < 1 or size > limit:
            raise ValueError("Request body missing or too large")
        if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("JSON content type required")
        obj = json.loads(handler.rfile.read(size))
        if not isinstance(obj, dict):
            raise ValueError("Expected an object")
        return obj
    except (TypeError, json.JSONDecodeError) as ex:
        raise ValueError("Invalid JSON") from ex


STEPS = [
    ("source", "Import source", "Link or local video"),
    ("speech", "Understand speech", "Offline transcript"),
    ("highlights", "Find highlights", "Speech-based ranking"),
    ("edit", "Edit Shorts", "Crop, available audio & captions"),
    ("export", "Export MP4", "Ready for review"),
    ("publish", "YouTube upload", "Only with channel approval"),
    ("analytics", "Check performance", "Owner metrics after upload"),
    ("improve", "Create revision", "Review an improved candidate"),
]


def dashboard_data():
    """Local dashboard snapshot. All statuses are based on actual files/job state.

    A progress value represents coarse processing milestones, not elapsed time.
    """
    with lock:
        job = dict(state)
        job["logs"] = list(state["logs"])
        job["files"] = list(state["files"])
    existing = []
    for f in output_files("*.mp4"):
        try:
            if f.is_file() and not f.is_symlink():
                existing.append((f, f.stat()))
        except FileNotFoundError:
            continue
    existing.sort(key=lambda entry: entry[1].st_mtime, reverse=True)
    sources = sorted((f for f in INPUT.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED),
                     key=lambda f: f.stat().st_mtime, reverse=True)
    uploads = youtube_api.uploads()
    try:
        analytics_history = json.loads((ROOT / "analytics_history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        analytics_history = {}
    outputs = []
    upload_names = {row.get("filename") for row in uploads.values()}
    local_bytes = 0
    for f, info in existing:
        local_bytes += info.st_size
        meta_path = f.with_name(f.stem + "_metadata.json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        outputs.append({"name": f.name, "size_mb": round(info.st_size / 1048576, 1),
                        "has_upload_record": f.name in upload_names,
                        "title": meta.get("title") or f.stem,
                        "has_captions": f.with_suffix(".srt").exists(),
                        "variant": bool(meta.get("variant_of")),
                        "studio_posts": meta.get("studio_posts"),
                        "reason": meta.get("reason"), "source_start": meta.get("source_start"),
                        "source_end": meta.get("source_end"),
                        "generated_at": int(info.st_mtime)})
    connected = youtube_api.is_connected()
    # Source of truth for past jobs: their saved manifests, not a guessed completion.
    previous_mode = ""
    manifests = sorted((x for x in output_files("*_manifest.json") if x.is_file()),
                       key=lambda x: x.stat().st_mtime, reverse=True)
    if manifests:
        try:
            previous_mode = str(json.loads(manifests[0].read_text(encoding="utf-8")).get("selection_mode", ""))
        except (ValueError, OSError):
            pass
    selection_mode = job.get("mode") or previous_mode
    is_fallback = selection_mode.lower().startswith("evenly-spaced")
    has_analytics = any(bool(v) for v in analytics_history.values())
    has_variant = any(f.get("variant") for f in outputs)
    has_outputs = len(existing) > 0
    active = int(job.get("active_step", 0))
    failed = bool(job.get("error")) and not job["busy"]
    complete_base = 5 if has_outputs else 0
    skipped_steps = {1, 2} if is_fallback else {1} if selection_mode.startswith("visual highlights") else set()
    workflow = []
    for i, (key, title, detail) in enumerate(STEPS):
        status = "waiting"
        if job["busy"]:
            if job.get("job_kind") == "generate":
                if i < active and i < 5:
                    status = "skipped" if i in skipped_steps else "done"
                elif i == active:
                    status = "active"
                elif i >= 5:
                    status = "optional"
            elif job.get("job_kind") == "upload":
                if i < 5 and has_outputs: status = "done"
                elif i == 5: status = "active"
                elif i > 5: status = "optional"
            elif job.get("job_kind") == "revision":
                if i < 3 and has_outputs: status = "done"
                elif i == 3 or i == 7: status = "active"
                elif i == 4: status = "waiting"
                elif i in (5, 6): status = "optional"
        elif failed and i == active:
            status = "error"
        else:
            if i < complete_base:
                status = "skipped" if i in skipped_steps else "done"
            elif i == 5 and bool(uploads):
                status = "done"
            elif i == 6 and has_analytics:
                status = "done"
            elif i == 7 and has_variant:
                status = "done"
            elif i >= 5:
                status = "optional"
        if status == "skipped" and i == 1:
            detail = "No usable transcript; automatic captions unavailable"
        elif status == "skipped" and i == 2:
            detail = "Timed cuts only; highlights not speech-ranked"
        workflow.append({"key": key, "title": title, "detail": detail, "status": status})

    capabilities = {
        "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "whisper": (WHISPER_BIN.is_file() and WHISPER_MODEL.is_file()) or
                   ((TOOLS / "whisper-model" / "model.bin").is_file() and bool(importlib.util.find_spec("faster_whisper"))),
        "voice": voice_available(),
        "downloader": downloader_installed(),
        "youtube_connected": connected,
        "postiz_configured": postiz_drafts.configured(),
        "oauth_configured": (ROOT / "oauth_client.json").is_file(),
    }
    if job["busy"]:
        next_action = {"title": "Processing on your laptop", "detail": "Keep ClipBot running. The current stage and logs update automatically.", "target": "progressPanel"}
    elif failed:
        next_action = {"title": "Resolve the latest error", "detail": "Read the error and logs below, then retry after fixing it.", "target": "progressPanel"}
    elif not capabilities["ffmpeg"]:
        next_action = {"title": "Finish required setup", "detail": "FFmpeg is missing. Run SETUP_WINDOWS.bat and restart ClipBot.", "target": "healthPanel"}
    elif not has_outputs:
        next_action = {"title": "Add your first source", "detail": "Paste a permitted YouTube link or import a local video, then choose Generate Shorts.", "target": "createPanel"}
    elif not uploads:
        next_action = {"title": "Review your generated clips", "detail": "Preview each MP4. Open your export folder or connect YouTube to publish a private draft.", "target": "libraryPanel"}
    elif not has_analytics:
        next_action = {"title": "Check your first upload", "detail": "Analytics may take time to populate. Use Check performance when data is available.", "target": "insightsPanel"}
    else:
        next_action = {"title": "Test an improved clip", "detail": "Use the performance suggestion to create a new cut; compare it before publishing.", "target": "insightsPanel"}
    return {"job": job, "workflow": workflow, "next_action": next_action,
            "outputs": outputs, "source_count": len(sources), "output_count": len(outputs),
            "local_export_bytes": local_bytes,
            "upload_count": len(uploads), "analytics_count": sum(len(v) for v in analytics_history.values() if isinstance(v,list)),
            "capabilities": capabilities, "folder": str(OUTPUT), "updated_at": int(time.time())}


class Handler(BaseHTTPRequestHandler):
    server_version = "ClipBot/0.5"

    def parse_request(self):
        if not super().parse_request():
            return False
        external = public_origin()
        host = self.headers.get("Host", "")
        local_proxy = (
            os.environ.get("CODESPACES") == "true"
            and self.client_address[0] == "127.0.0.1"
            and host in {"localhost", "127.0.0.1", f"localhost:{self.server.server_port}",
                         f"127.0.0.1:{self.server.server_port}"}
        )
        if external and host != urlsplit(external).netloc and not local_proxy:
            self.fail(403, "Unexpected request hostname")
            return False
        return True


    def log_message(self, fmt, *args):
        if self.path.startswith("/api/oauth/callback?"):
            logging.info("YouTube authorization callback")
        else:
            logging.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, message):
        self.send_json(code, {"error": str(message)})

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/api/dashboard":
            self.send_json(200, dashboard_data())
        elif parsed.path == "/api/youtube/status":
            self.send_json(200, {"connected": youtube_api.is_connected(),
                                 "oauth_configured": (ROOT / "oauth_client.json").is_file(),
                                 "uploads": youtube_api.uploads()})
        elif parsed.path == "/api/youtube/oauth-start":
            try:
                url = youtube_api.oauth_link(self.server.server_address[1], public_origin())
                self.send_json(200, {"url": url})
            except Exception as exc:
                self.fail(400, str(exc))
        elif parsed.path == "/api/oauth/callback":
            query = parse_qs(parsed.query)
            try:
                if query.get("error"):
                    raise RuntimeError("Google authorization was declined or failed")
                youtube_api.oauth_finish(query.get("state", [""])[0], query.get("code", [""])[0])
                self.send_response(303)
                self.send_header("Location", "/?connected=1")
                self.end_headers()
            except Exception as ex:
                self.fail(400, str(ex))
        elif parsed.path == "/api/metadata":
            name = parse_qs(parsed.query).get("name", [""])[0]
            if not valid_output_name(name):
                return self.fail(400, "Invalid MP4 name")
            meta_file = output_path(name).with_name(Path(name).stem + "_metadata.json")
            if not meta_file.is_file():
                return self.fail(404, "Clip metadata not available")
            self.send_json(200, json.loads(meta_file.read_text(encoding="utf-8")))
        elif parsed.path == "/api/preview":
            name = parse_qs(parsed.query).get("name", [""])[0]
            if not valid_output_name(name):
                return self.fail(400, "Invalid MP4 name")
            video = output_path(name)
            if not video.is_file() or video.is_symlink():
                return self.fail(404, "Video unavailable")
            size = video.stat().st_size
            first, last = 0, size-1
            requested = self.headers.get("Range", "")
            if requested:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                if not match:
                    return self.fail(416, "Unsupported range")
                first = int(match.group(1))
                last = min(size-1, int(match.group(2))) if match.group(2) else size-1
                if first > last:
                    return self.fail(416, "Invalid video range")
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            self.send_header("Content-Length", str(last-first+1))
            self.end_headers()
            try:
                with video.open("rb") as f:
                    f.seek(first)
                    remaining = last-first+1
                    while remaining:
                        block = f.read(min(remaining, 256*1024))
                        if not block: break
                        self.wfile.write(block)
                        remaining -= len(block)
            except ConnectionError:
                pass
        elif parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            with lock:
                self.send_json(200, dict(state))
        elif parsed.path == "/api/postiz/channels":
            try:
                self.send_json(200, {"channels": postiz_drafts.channels()})
            except (RuntimeError, ValueError) as error:
                self.fail(409, str(error))
        elif parsed.path == "/api/files":
            self.send_json(200, {"files": sorted(x.name for x in INPUT.iterdir() if x.is_file() and x.suffix.lower() in ALLOWED)})
        elif parsed.path == "/api/download":
            name = parse_qs(parsed.query).get("name", [""])[0]
            if not valid_output_name(name):
                return self.fail(400, "Invalid filename")
            path = output_path(name)
            if not path.is_file() or path.is_symlink():
                return self.fail(404, "Output not found")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_file(name)}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                with path.open("rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=1024*1024)
            except ConnectionError:
                pass
        else:
            self.fail(404, "Not found")

    def do_POST(self):
        origin = self.headers.get("Origin")
        external = public_origin()
        port = getattr(getattr(self, "server", None), "server_port", 8765)
        allowed = (external,) if external else (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        codespaces_same_origin = (
            external and os.environ.get("CODESPACES") == "true"
            and self.client_address[0] == "127.0.0.1"
            and self.headers.get("Sec-Fetch-Site") == "same-origin"
            and origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}")
        )
        if (external or origin) and origin not in allowed and not codespaces_same_origin:
            return self.fail(403, "Foreign origin not allowed")
        if self.path == "/api/exports/delete":
            try:
                data = json_body(self)
                if data.get("confirm") is not True:
                    return self.fail(400, "Confirm permanent deletion of the local MP4.")
                with lock:
                    if state["busy"]:
                        return self.fail(409, "Wait for the current job to finish.")
                    freed = remove_local_export(data.get("name", ""))
                    state["files"] = [name for name in state["files"] if name != data["name"]]
                return self.send_json(200, {"freed_bytes": freed})
            except FileNotFoundError as error:
                return self.fail(404, str(error))
            except (ValueError, TypeError) as error:
                return self.fail(400, str(error))
            except OSError as error:
                return self.fail(409, "Could not delete the file. Close any program using it and retry.")
        if self.path == "/api/postiz/drafts":
            try:
                data = json_body(self, limit=32768)
                name = data.get("name", "")
                selections = data.get("selections")
                if data.get("confirm") is not True:
                    return self.fail(400, "Confirm sending this video to Postiz.")
                if not valid_output_name(name) or not isinstance(selections, dict) or len(selections) > 20:
                    return self.fail(400, "Invalid video or destinations")
                if any(not isinstance(value, str) or len(value) > 4800 for value in selections.values()):
                    return self.fail(400, "Keep captions under 4800 characters.")
                video = output_path(name)
                if not video.is_file() or video.is_symlink():
                    return self.fail(404, "Export not found.")
                title = str(data.get("title", ""))[:100]
                return self.send_json(200, {"drafts": postiz_drafts.drafts(video, selections, title)})
            except (ValueError, AttributeError, TypeError, OSError, RuntimeError) as error:
                return self.fail(400, str(error))
        if self.path in ("/api/studio/draft", "/api/studio/render"):
            try:
                data = json_body(self, limit=16384)
                if self.path.endswith("/draft"):
                    prompt = data.get("prompt", "")
                    if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 2400:
                        return self.fail(400, "Enter a prompt between 3 and 2400 characters")
                    return self.send_json(200, creator_studio.draft(prompt.strip()))
                start_studio(data)
                return self.send_json(202, {"status": "started"})
            except (ValueError, TypeError, AttributeError) as error:
                return self.fail(400, str(error))
            except RuntimeError as error:
                return self.fail(409, str(error))
        if self.path == "/api/youtube/upload":
            try:
                data = json_body(self)
                name = data.get("name", "")
                if not valid_output_name(name) or not output_path(name).is_file():
                    return self.fail(400, "Pick an exported MP4")
                if data.get("confirm") is not True:
                    return self.fail(403, "Confirm you own/have rights to publish this video")
                privacy = data.get("privacy", "private")
                if privacy not in ("private", "unlisted", "public"):
                    return self.fail(400, "Invalid privacy selection")
                if privacy == "public" and data.get("publish_public") is not True:
                    return self.fail(403, "Public upload needs explicit confirmation")
                title = str(data.get("title", ""))[:100].strip()
                description = str(data.get("description", ""))[:4800].strip()
                if not title:
                    return self.fail(400, "Title required")
                start_upload_job(name, title, description, privacy,
                                 delete_after_upload=data.get("delete_after_upload") is True)
                self.send_json(202, {"status": "uploading"})
            except (ValueError, RuntimeError) as e:
                self.fail(409, str(e))
        elif self.path == "/api/youtube/analyze":
            try:
                data = json_body(self)
                vid = str(data.get("video_id", ""))
                entries = youtube_api.uploads()
                if vid not in entries:
                    return self.fail(400, "Choose a video uploaded by this ClipBot installation")
                result = youtube_api.performance(vid, entries[vid]["date"])
                youtube_api.remember_performance(vid, result)
                result["advice"] = youtube_api.advice(result)
                filename = entries[vid]["filename"]
                path = output_path(filename)
                metadata_path = path.with_name(path.stem + "_metadata.json")
                if metadata_path.is_file():
                    original = json.loads(metadata_path.read_text(encoding="utf-8")).get("variant_of")
                    for original_id, record in reversed(list(entries.items())):
                        if original and record["filename"] == original:
                            previous = youtube_api.latest_performance(original_id)
                            result["comparison"] = youtube_api.compare_performance(previous, result)
                            break
                self.send_json(200, result)
            except (RuntimeError, ValueError) as e:
                self.fail(400, str(e))
        elif self.path == "/api/youtube/revise":
            try:
                data = json_body(self)
                vid = str(data.get("video_id", ""))
                entry = youtube_api.uploads().get(vid)
                if not entry:
                    return self.fail(400, "Select a video previously uploaded through ClipBot")
                metrics = youtube_api.latest_performance(vid)
                if not metrics:
                    return self.fail(400, "Check performance before generating a revision")
                start_variant_job(entry['filename'], youtube_api.revision_experiment(metrics))
                self.send_json(202, {"status": "creating an alternative cut"})
            except (RuntimeError, ValueError) as e:
                self.fail(400, str(e))
        elif self.path == "/api/youtube/disconnect":
            youtube_api.disconnect()
            self.send_json(200, {"status": "disconnected"})
        elif self.path == "/api/upload":
            if self.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                return self.fail(415, "Expected binary upload")
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.fail(411, "Content-Length required")
            if size < 1 or size > MAX_UPLOAD:
                return self.fail(413, "Video must be between 1 byte and 2 GB")
            if shutil.disk_usage(ROOT).free < size + 150_000_000:
                return self.fail(507, "Not enough free storage")
            name = safe_file(unquote(self.headers.get("X-Filename", "video.mp4")))
            if Path(name).suffix.lower() not in ALLOWED:
                return self.fail(415, "Video format not supported")
            dest = INPUT / name
            if dest.exists():
                dest = INPUT / f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"
            tmp = INPUT / f".upload_{uuid.uuid4().hex}.part"
            try:
                with tmp.open("wb") as f:
                    left = size
                    while left:
                        chunk = self.rfile.read(min(left, 1024*1024))
                        if not chunk:
                            raise RuntimeError("Upload interrupted")
                        f.write(chunk)
                        left -= len(chunk)
                tmp.rename(dest)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                return self.fail(400, str(exc))
            self.send_json(200, {"name": dest.name})
        elif self.path == "/api/start":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 4096:
                    return self.fail(400, "Invalid request size")
                data = json.loads(self.rfile.read(size))
                name = data.get("name", "")
                url = str(data.get("url", "")).strip()
                if url:
                    if data.get("rights_confirmed") is not True:
                        return self.fail(403, "Confirm you have the rights and platform permission to process this link")
                    validate_youtube_url(url)
                elif not isinstance(name, str) or name != safe_file(name):
                    return self.fail(400, "Invalid video name")
                count, length, width = (int(data.get(k, v)) for k, v in (("count",0),("length",40),("width",720)))
                if count < 0 or length not in (20,30,40,60) or width not in (480,720):
                    return self.fail(400, "Invalid export settings")
                if not url and not (INPUT / name).is_file():
                    return self.fail(404, "Upload not found")
                voice_mode = str(data.get("voice_mode", "off"))
                voice_script = str(data.get("voice_script", ""))
                if voice_mode not in ("off", "auto", "custom") or len(voice_script) > 700:
                    return self.fail(400, "Invalid voice-over setting")
                if voice_mode == "custom" and not voice_script.strip():
                    return self.fail(400, "Type the words to narrate")
                auto_upload = data.get("auto_private_upload") is True
                if auto_upload and (data.get("rights_confirmed") is not True or not youtube_api.is_connected()):
                    return self.fail(403, "Private auto-upload requires publishing rights confirmation and YouTube authorization")
                caption_style = data.get("caption_style", "classic")
                if caption_style not in ("pop", "classic"):
                    return self.fail(400, "Unknown caption style")
                opening_text = data.get("opening_text", "")
                if not isinstance(opening_text, str) or len(opening_text) > 55:
                    return self.fail(400, "Opening text may contain at most 55 characters")
                selection = data.get("selection", "classic")
                framing_mode = data.get("framing", "default")
                if selection not in ("classic", "gameplay") or framing_mode not in (
                        "default", "fit", "left", "center", "right", "smart"):
                    return self.fail(400, "Unknown highlight or framing mode")
                start_job(name, count, length, width, data.get("captions") is True, voice_mode,
                          voice_script, url, auto_upload,
                          delete_after_upload=auto_upload and data.get("delete_after_upload") is True,
                          face_emoji=data.get("face_emoji") is True,
                          caption_style=caption_style, opening_text=opening_text,
                          selection=selection, framing_mode=framing_mode)
                self.send_json(202, {"status":"started"})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
                self.fail(400, str(e))
            except RuntimeError as e:
                self.fail(409, str(e))
        else:
            self.fail(404, "Not found")


def main():
    parser = argparse.ArgumentParser(description="ClipBot: local video clipping for Windows")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="Run localhost browser UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"],
                   help="Use 0.0.0.0 only on a private container network behind an authenticated proxy.")
    p = sub.add_parser("clip", help="Process one locally stored video")
    p.add_argument("video", help="Source video basename located in ~/clipbot/input")
    p.add_argument("--count", type=int, default=0, help="0 = automatic/all viable")
    p.add_argument("--seconds", type=int, default=40)
    p.add_argument("--width", type=int, choices=[480,720], default=720)
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--caption-style", choices=["pop", "classic"], default="pop")
    p.add_argument("--opening-text", default="", help="short on-screen text for the opening")
    p.add_argument("--face-emoji", action="store_true", help="cover detected faces with a smiley")
    p.add_argument("--selection", choices=["classic", "gameplay"], default="classic")
    p.add_argument("--framing", choices=["default", "fit", "left", "center", "right", "smart"], default="default")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.command == "serve":
        external = public_origin()
        if args.bind != "127.0.0.1" and not external:
            parser.error("A trusted HTTPS public origin is required for a private container bind.")
        start_monitor()
        address = external or f"http://127.0.0.1:{args.port}"
        print(f"ClipBot is ready at {address}", flush=True)
        print("Keep ClipBot running while processing. Ctrl+C to stop.")
        ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
    elif args.command == "clip":
        files, mode = process_video(args.video, args.count, args.seconds, args.width,
                                    not args.no_captions, face_emoji=args.face_emoji,
                                    caption_style=args.caption_style, opening_text=args.opening_text,
                                    selection=args.selection, framing_mode=args.framing)
        print("Created", len(files), "clips using", mode)
        for f in files:
            print(output_path(f))


if __name__ == "__main__":
    main()
