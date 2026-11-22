"""Local storyboard drafting and vertical video assembly."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from threading import Lock
from urllib.request import Request, urlopen
from wave import open as open_wave

from shorts_captions import write_pop_captions


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
        "scenes": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string", "maxLength": 45},
                    "narration": {"type": "string", "minLength": 20, "maxLength": 275},
                },
                "required": ["heading", "narration"], "additionalProperties": False,
            },
        },
    },
    "required": ["title", "scenes"], "additionalProperties": False,
}
MODEL_LOCK = Lock()


def local_model(prompt):
    tools = Path(os.getenv("CLIPBOT_TOOLS", Path(__file__).with_name("tools"))) / "llama"
    runner = tools / ("llama-completion.exe" if os.name == "nt" else "llama-completion")
    weights = tools / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    if not runner.is_file() or not weights.is_file():
        return None
    if not MODEL_LOCK.acquire(blocking=False):
        raise ValueError("A local storyboard is already being drafted.")
    try:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            (folder / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
            instruction = ("You write practical scripts for short videos. Create exactly three scenes based on the brief. "
                           "Use a hook and concrete narration. Write 20 to 30 spoken words per scene. Pick a title under "
                           "80 characters and headings under 45 characters. Do not invent factual claims. "
                           "Return only the JSON storyboard.")
            chat = (f"<|im_start|>system\n{instruction}<|im_end|>\n"
                    f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n")
            (folder / "prompt.txt").write_text(chat, encoding="utf-8")
            result = subprocess.run(
                [str(runner.resolve()), "-m", str(weights.resolve()), "-f", "prompt.txt",
                 "--json-schema-file", "schema.json", "--no-conversation", "--no-display-prompt",
                 "--simple-io", "-ngl", "0", "-c", "2048", "-n", "640",
                 "--temp", "0.25", "--seed", "42", "-t", str(min(4, os.cpu_count() or 2))],
                cwd=folder, capture_output=True, encoding="utf-8", errors="replace",
                timeout=150, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                return None
            candidate = result.stdout.strip()
            start = candidate.find("{")
            if start == -1:
                return None
            title, scenes = validate(json.JSONDecoder().raw_decode(candidate[start:])[0])
            for scene in scenes:
                text = scene["narration"]
                if len(text) >= 265 and not text.endswith((".", "!", "?")):
                    complete = list(re.finditer(r"[.!?](?=\s|$)", text))
                    if complete:
                        scene["narration"] = text[:complete[-1].end()]
            return {"title": title, "scenes": scenes, "engine": "Local Qwen"}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        MODEL_LOCK.release()


def draft(prompt):
    model = os.getenv("CLIPBOT_OLLAMA_MODEL")
    if model:
        task = (
            "Write a vertical short based on the following brief. Respond with JSON "
            "containing title and scenes. Each scene has heading and narration. "
            "Write 3 to 5 scenes, 75 to 120 spoken words in total. "
            "Use concrete details and do not invent facts. Brief: " + prompt
        )
        request = Request("http://127.0.0.1:11434/api/generate",
                          data=json.dumps({"model": model, "prompt": task, "format": "json",
                                           "stream": False}).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=120) as response:
                result = json.loads(json.load(response)["response"])
            title, scenes = validate(result)
            return {"title": title, "scenes": scenes, "engine": "Local Ollama"}
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ValueError("Local Ollama could not draft a storyboard. Check the model or edit the storyboard manually.") from error
    generated = local_model(prompt)
    if generated:
        return generated
    parts = [s.strip() for s in re.split(r"\n+|(?<=[.!?])\s+", prompt) if s.strip()]
    title = parts[0][:80]
    if len(parts) == 1:
        scenes = [
            {"heading": "Opening", "narration": prompt[:500]},
            {"heading": "Main point", "narration": ""},
            {"heading": "Takeaway", "narration": ""},
        ]
    else:
        scenes = [{"heading": f"Scene {n}", "narration": text[:500]}
                  for n, text in enumerate(parts[:8], 1)]
    return {"title": title, "scenes": scenes, "engine": "Offline storyboard template"}


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("Expected a storyboard")
    title, scenes = data.get("title"), data.get("scenes")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
        raise ValueError("Enter a title of up to 100 characters")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 8:
        raise ValueError("Use between one and eight scenes")
    cleaned = []
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError("Invalid scene")
        heading, narration = scene.get("heading"), scene.get("narration")
        if not isinstance(heading, str) or len(heading) > 55:
            raise ValueError("Scene headings must be at most 55 characters")
        if not isinstance(narration, str) or not 1 <= len(narration.strip()) <= 500:
            raise ValueError("Give each scene between 1 and 500 characters of narration")
        cleaned.append({"heading": heading.strip(), "narration": narration.strip()})
    return title.strip(), cleaned


def render(title, scenes, folder, destination, *, source, run, narrate, transcribe, segment, progress):
    colors = ("0x142437", "0x263329", "0x292843", "0x382B23")
    files = []
    elapsed = 0.
    for index, scene in enumerate(scenes):
        voice = folder / f"voice-{index}.wav"
        out = folder / f"scene-{index}.mp4"
        progress(index, len(scenes))
        narrate(scene["narration"], voice)
        with open_wave(str(voice), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
        length = duration + .35
        timed, _ = transcribe(voice, folder, duration, report=False)
        if not timed:
            # Fall back to estimated phrase timing when Whisper finds no usable speech.
            words = scene["narration"].split()
            timed = [segment(i / len(words) * duration,
                             min(i + 5, len(words)) / len(words) * duration,
                             " ".join(words[i:i+5])) for i in range(0, len(words), 5)]
        write_pop_captions(timed, 0, length, folder / "scene.ass",
                           opening_text=(scene["heading"] or title)[:55], opening_seconds=length)
        color = colors[index % len(colors)]
        background = ["-f", "lavfi", "-i", f"color=c={color}:s=480x854:r=30"]
        framing = ""
        if source:
            background = ["-stream_loop", "-1", "-i", str(source)]
            framing = ("scale=480:854:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                       f"pad=480:854:(ow-iw)/2:(oh-ih)/2:{color},fps=30,setsar=1,")
        filters = framing + ("drawbox=x=28:y=36:w=424:h=780:color=white@0.17:t=2,"
                   f"drawbox=x=28:y=805:w={round(424 * (index + 1) / len(scenes))}:h=4:color=0x8AF5C1:t=fill,"
                   "ass=scene.ass,format=yuv420p")
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *background, "-i", str(voice),
             "-map", "0:v:0", "-map", "1:a:0", "-t", f"{length:.3f}",
             "-af", "apad", "-ar", "48000", "-ac", "1", "-vf", filters,
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out)],
            description="Rendering storyboard scene", cwd=folder)
        files.append(out.name)
        elapsed += length
    (folder / "scenes.txt").write_text("".join(f"file {name}\n" for name in files), encoding="ascii")
    run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
         "-i", "scenes.txt", "-c", "copy", "-movflags", "+faststart", str(destination)],
        description="Assembling storyboard", cwd=folder)
    return elapsed
