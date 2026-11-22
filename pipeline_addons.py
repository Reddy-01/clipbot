"""Safe YouTube link intake, lightweight local TTS, metadata generation."""
from __future__ import annotations

import os
import re
import shutil
import importlib.util
import sys
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}


def validate_youtube_url(url):
    if not isinstance(url, str) or len(url) > 1200:
        raise ValueError('Invalid YouTube link')
    parsed = urlsplit(url.strip())
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ValueError('Paste a full HTTPS youtube.com or youtu.be link')
    if parsed.port not in (None, 443):
        raise ValueError('Use the standard YouTube HTTPS port')
    if not parsed.path or parsed.path == '/':
        raise ValueError('Paste a specific video link')
    return url.strip()


def fetch_authorized_video(url, input_folder, report=print):
    """Download a single permitted video (no playlists, no login bypass).

    This is an optional user-directed download; video download and reuse must
    be allowed by the owner and the platform. Access failures are reported.
    """
    url = validate_youtube_url(url)
    if importlib.util.find_spec('yt_dlp'):
        downloader = [sys.executable, '-m', 'yt_dlp']
    elif shutil.which('yt-dlp'):
        downloader = [shutil.which('yt-dlp')]
    else:
        raise RuntimeError('yt-dlp missing. Run SETUP_WINDOWS.bat.')
    input_folder = Path(input_folder).resolve()
    input_folder.mkdir(parents=True, exist_ok=True)
    version = subprocess.run([*downloader, '--ignore-config', '--version'],
                             capture_output=True, text=True, check=True, timeout=30)
    report(f'yt-dlp {version.stdout.strip()}')
    node = shutil.which('node')
    deno = shutil.which('deno')
    runtime = ['--js-runtimes', f'node:{node}'] if node and not deno else []
    cmd = [*downloader, '--ignore-config', '--use-extractors', 'youtube',
           '--no-playlist', '--no-simulate', '--no-progress', '--restrict-filenames',
           '--no-overwrites', '--match-filter', '!is_live', '--max-filesize', '1500M', '--socket-timeout', '20',
           '--retries', '3', *runtime, '-f', 'bv*[height<=720]+ba/b[height<=720]/b',
           '--merge-output-format', 'mp4', '--remux-video', 'mp4',
           '--print', 'after_move:filepath', '-o',
           str(input_folder / 'youtube_%(id)s.%(ext)s'), '--', url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('YouTube download timed out. Retry or import a local MP4.') from exc
    if result.returncode:
        hint = '' if node or deno else ' Install Deno or Node.js 22+ for YouTube JavaScript support.'
        raise RuntimeError('YouTube download failed. Try updating yt-dlp or importing an authorized MP4.'
                           + hint + ' ' + result.stderr.strip()[-1200:])
    lines = [x.strip() for x in result.stdout.splitlines() if x.strip()]
    if not lines:
        raise RuntimeError('No video saved. Check the source and download size limit, or import an MP4.')
    path = Path(lines[-1]).resolve()
    if not path.is_file() or path.parent != input_folder.resolve() or path.suffix.lower() != '.mp4':
        raise RuntimeError('Downloaded media file could not be verified')
    return path


def generate_hook(clip_text):
    """Safe, transparent NON-AI narration using only actual transcript words."""
    flat = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', '', clip_text)).strip()
    # Avoid inventing claims. A word-for-word excerpt isn't an AI summary.
    sentence = re.split(r'(?<=[.!?])\s+', flat)[0].strip()
    if not sentence:
        return ''
    snippet = ' '.join(sentence.split()[:13]).strip(' .,!')
    return f'Listen to this: {snippet}.' if snippet else ''


def piper_model():
    tools = Path(os.environ.get("CLIPBOT_TOOLS", Path(__file__).with_name("tools")))
    model = tools / "piper" / "en_US-lessac-low.onnx"
    if model.is_file() and model.with_suffix(".onnx.json").is_file() and importlib.util.find_spec("piper"):
        return model
    return None


@lru_cache(maxsize=1)
def load_piper(model):
    from piper import PiperVoice

    return PiperVoice.load(str(model))


def voice_available():
    return bool(piper_model() or shutil.which('espeak') or shutil.which('espeak-ng'))


def narrate(text, path):
    """Synthesize locally with Piper or eSpeak."""
    clean = re.sub(r'\s+', ' ', text)[:700].strip()
    if not clean:
        return None
    cmd = shutil.which('espeak') or shutil.which('espeak-ng')
    model = piper_model()
    if model:
        from wave import open as open_wave

        with open_wave(str(path), "wb") as output:
            load_piper(model).synthesize_wav(clean, output)
        return path
    if not cmd:
        raise RuntimeError("Run SETUP_WINDOWS.bat to install Piper, or choose original audio.")
    args = [cmd, "-v", "en-us", "-s", "155", "-w", str(path), "--", clean]
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=90)
    if result.returncode or not path.is_file() or path.stat().st_size <= 44:
        raise RuntimeError('Voice-over synthesis failed: ' + result.stderr[-800:])
    return path


def suggest_title(transcript, number=1):
    """Draft, not a promise of SEO performance. Always preview before upload."""
    cleaned = re.sub(r'\s+', ' ', transcript).strip()
    first = re.split(r'[.!?]', cleaned)[0].strip(' \"')
    first = ' '.join(first.split()[:9]).strip()
    if first:
        return (first[:73].rstrip(' ,.-') + ' #Shorts')[:100]
    return f'Highlight #{number} #Shorts'


def suggest_description(source_url, transcript):
    excerpt = re.sub(r'\s+', ' ', transcript).strip()[:220]
    source = f'\nSource: {source_url}' if source_url else ''
    return (f'{excerpt}\n\nEdited short from a source I am authorized to use.{source}\n#Shorts').strip()
