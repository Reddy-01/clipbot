"""High-contrast captions for vertical exports, with real word timing when available."""
from __future__ import annotations

import re
import textwrap


HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Pop,Arial,48,&H00FFFFFF,&H00FFFFFF,&H00171016,&H00171016,-1,0,0,0,100,100,0,0,1,5,1,5,58,98,0,1
Style: Hook,Arial,51,&H006AF4C2,&H00FFFFFF,&H00171016,&H00171016,-1,0,0,0,100,100,1,0,1,5,1,5,55,95,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
ACTIVE = r"{\1c&H6AF4C2&}"
RESET = r"{\1c&HFFFFFF&}"


def _clean(text):
    text = re.sub(r"</?(?:i|b|u|font)(?:\s[^>]*)?>", "", text, flags=re.IGNORECASE)
    return " ".join(text.split()).translate(str.maketrans({"{": "(", "}": ")", "\\": "/"}))


def _time(seconds):
    ticks = round(max(0, seconds) * 100)
    minutes, ticks = divmod(ticks, 6000)
    hours, minutes = divmod(minutes, 60)
    seconds, ticks = divmod(ticks, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{ticks:02}"


def _event(start, end, style, text, y=920):
    begin, finish = _time(start), _time(end)
    if begin == finish or end <= start:
        return ""
    lines = text.split(r"\N")
    scale = min(100, max(52, 210 // len(lines)))
    position = rf"{{\pos(340,{y})\fscx{scale}\fscy{scale}}}"
    return f"Dialogue: 0,{begin},{finish},{style},,0,0,0,,{position}{text}\n"


def _plain(text, width=25):
    return r"\N".join(textwrap.wrap(_clean(text), width, break_on_hyphens=False))


def _karaoke(words, active):
    lines = [[]]
    width = 0
    for index, word in enumerate(words):
        length = len(word)
        if width and width + length + 1 > 25:
            lines.append([])
            width = 0
        lines[-1].append(ACTIVE + word + RESET if index == active else word)
        width += length + bool(width)
    return r"\N".join(" ".join(line) for line in lines)


def write_pop_captions(segments, start, end, path, *, opening_text="", opening_seconds=3.2):
    """Output phrase captions using timestamped words when the source provides them."""
    events = []
    opening = _plain(opening_text, width=19)
    if opening:
        events.append(_event(0, min(end - start, opening_seconds), "Hook", opening, y=245))
    for sentence in segments:
        for cue in sentence.captions or (sentence,):
            left, right = max(cue.start, start), min(cue.end, end)
            if right - left < .12:
                continue
            words = [(word, _clean(word.text)) for word in cue.words if word.end > left and word.start < right]
            words = [(word, text) for word, text in words if text]
            if not words:
                text = _plain(cue.text)
                if text:
                    events.append(_event(left - start, right - start, "Pop", text))
                continue
            display = [text for _, text in words]
            for i, (word, _) in enumerate(words):
                begin = left if i == 0 else max(left, word.start)
                finish = min(right, words[i+1][0].start) if i+1 < len(words) else right
                events.append(_event(begin - start, finish - start, "Pop", _karaoke(display, i)))
    path.write_text(HEADER + "".join(events), encoding="utf-8")


def write_text_cards(cards, path):
    """Write editorial text for visuals with no dialogue to transcribe."""
    events = []
    for start, end, text in cards:
        opening = start == 0
        lines = _plain(text, width=19 if opening else 24)
        if lines:
            events.append(_event(start, end, "Hook" if opening else "Pop", lines, y=245 if opening else 910))
    path.write_text(HEADER + "".join(events), encoding="utf-8")
