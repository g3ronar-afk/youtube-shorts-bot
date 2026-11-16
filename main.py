"""
Auto YouTube Shorts pipeline - retention-focused rewrite.

What changed vs. the old version (all aimed at the "swiped away" rate):
  * The hook is on screen AND spoken from frame 0 (no white flash, no silent lead-in,
    no slow -15% voice).
  * Script comes back from Gemini as JSON with a hook formula, an open loop and a
    replay-bait last line; weak/repeated hooks are rejected and regenerated.
  * A new cut / punch-in every ~2-3 s (cut on word boundaries) with a whoosh on each cut.
  * Real 1080x1920 output (the old code rendered at roughly 405x720).
  * Captions never disappear between words and never collide with the hook banner.
  * Synthesised dark soundtrack with an impact hit on frame 0 (no hot-linked MP3s that
    silently fail).
  * Rotating formats / voices / caption themes + post history, so the channel does not
    look like one template repeated (YouTube's "inauthentic content" policy targets that).

Environment variables
  GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS   (same as before)
  GEMINI_MODEL   optional, defaults to the model your old script used
  DRY_RUN        "true" = render the video but do NOT upload (used by the workflow)
"""
import os
import re
import sys
import json
import time
import wave
import bisect
import random
import asyncio
import datetime
from pathlib import Path

import numpy as np
import requests
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageChops

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from google.auth.transport.requests import Request
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from moviepy import VideoFileClip, AudioFileClip, CompositeAudioClip, VideoClip
from moviepy.audio.fx import MultiplyVolume

# ============================================================
# CONFIG
# ============================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
YOUTUBE_CREDENTIALS_JSON = os.environ.get("YOUTUBE_CREDENTIALS")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

W, H, FPS = 1080, 1920, 30
HISTORY_FILE = Path("history.json")
WORK_DIR = Path("work")
FONT_DIR = Path("assets/fonts")

CHANNEL_CTA = "Follow for daily Stoic survival rules"
SCRIPT_MIN_WORDS, SCRIPT_MAX_WORDS = 30, 70
HOOK_SECONDS = 3.5          # how long the headline banner stays on screen
VOICE_RATE = "+6%"          # old value was -15% (slow voice = more swipes)
VOICE_PITCH = "-2Hz"
VOICES = [
    "en-US-AndrewNeural",
    "en-US-BrianNeural",
    "en-US-GuyNeural",
    "en-US-ChristopherNeural",
]
DEFAULT_VOICE = "en-US-AndrewNeural"

_rng = random.Random()


def log(msg):
    print(msg, flush=True)


# ============================================================
# 0. HISTORY (stops the channel repeating itself)
# ============================================================
def load_history():
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history, entry):
    history.append(entry)
    HISTORY_FILE.write_text(
        json.dumps(history[-300:], indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ============================================================
# 1. SCRIPT GENERATION
# ============================================================
FORMATS = [
    {
        "name": "red_flags",
        "brief": "Reveal 3 specific, uncomfortable signs of narcissistic manipulation. "
                 "Number them out loud (one, two, three). Save the sharpest sign for last.",
    },
    {
        "name": "reframe",
        "brief": "Take a belief the viewer holds ('being the bigger person keeps the peace') "
                 "and flip it with one concrete reason why it backfires.",
    },
    {
        "name": "stoic_rule",
        "brief": "Teach ONE Stoic principle (Marcus Aurelius, Epictetus or Seneca) as a "
                 "modern rule for dealing with a toxic person. Paraphrase the idea.",
    },
    {
        "name": "exact_words",
        "brief": "Give the exact sentence to say (or the exact thing NOT to say) to a "
                 "manipulator, and why it works (gray rock, refusing to justify or explain).",
    },
    {
        "name": "scenario",
        "brief": "A mini-scene in second person ('You tell them you're hurt. They say...'), "
                 "then reveal what they were really doing.",
    },
    {
        "name": "uncomfortable_truth",
        "brief": "One uncomfortable truth about why people stay in toxic dynamics, "
                 "delivered with compassion and power. Make the viewer feel seen.",
    },
]

BAD_STARTS = (
    "stop forgiving", "you need to", "it is important", "it's important", "we should",
    "most people", "have you ever", "did you know", "imagine", "in a world", "in today",
    "are you", "do you ever", "let me tell", "today i", "hey guys", "welcome",
)

FALLBACK_QUERIES = [
    "dark storm clouds", "city night rain", "marble statue", "candle flame dark",
    "man silhouette walking", "ocean waves dark", "foggy forest", "chess pieces",
    "mirror reflection", "clock ticking", "lightning sky", "empty street night",
]


def clean_text(s):
    s = s or ""
    s = (s.replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("—", ", ").replace("–", "-").replace("…", "..."))
    s = re.sub(r"[*_#`~<>]", "", s)
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", s)     # stage directions like (pause)
    s = re.sub(r"[^\x20-\x7E]", "", s)             # emoji / non-ascii the font can't draw
    return re.sub(r"\s+", " ", s).strip()


def first_sentence(text):
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return parts[0] if parts else (text or "")


def build_prompt(fmt, history):
    recent = [(h.get("hook") or h.get("title") or "").strip() for h in history[-25:]]
    recent_block = "\n".join(f"- {r}" for r in recent if r) or "- (none yet)"
    return (
        "You write viral YouTube Shorts for a US audience in the niche of narcissistic "
        "manipulation and Stoic resilience.\n"
        "GOAL: the viewer must NOT swipe in the first 2 seconds, must watch to the very "
        "end, and should want to replay.\n\n"
        f"FORMAT FOR THIS VIDEO: {fmt['name']} - {fmt['brief']}\n\n"
        "RULES FOR THE SPOKEN SCRIPT (field \"script\"):\n"
        "1. 40 to 55 words total. English only. Plain spoken words. No emojis, no hashtags, "
        "no stage directions.\n"
        "2. The FIRST sentence is the hook (9 words max). It must do ONE of these: "
        "(a) name a painfully specific situation the viewer has lived, e.g. 'When they say "
        "you're too sensitive, listen closely.' (b) promise a number plus a payoff, e.g. "
        "'Three phrases narcissists use right before they discard you.' (c) contradict a "
        "belief the viewer holds, e.g. 'Being the bigger person is exactly how they win.' "
        "The hook must make them NEED the next sentence.\n"
        "3. Never open with: 'Stop forgiving', 'You need to', 'Most people', 'Have you ever', "
        "'Did you know', 'Imagine', 'In a world', 'Are you'.\n"
        "4. Short sentences (average 7 words, never more than 12). Speak directly to 'you'.\n"
        "5. Keep an open loop: hold the sharpest payoff back for the LAST sentence.\n"
        "6. The final sentence is a mic-drop of 8 words or fewer that makes people want to "
        "replay the video.\n"
        "7. No fake quotes. If you mention a Stoic, paraphrase the idea; never invent words "
        "and attribute them to a real person.\n"
        "8. Never diagnose real people. Never encourage revenge, harassment or self-harm.\n"
        "9. Avoid AI-sounding filler: journey, tapestry, unlock, delve, in today's world.\n\n"
        "OTHER FIELDS:\n"
        "- \"on_screen_hook\": 3 to 7 words. A punchy headline shown on screen during the "
        "first 3 seconds. Related to the hook but not an identical copy; may promise a "
        "number or a reveal.\n"
        "- \"title\": max 60 characters. Curiosity or a direct challenge. No hashtags, no "
        "quotation marks. Do not reuse the opening words of the recent titles below.\n"
        "- \"description\": 1-2 sentences with natural keywords (narcissist, manipulation, "
        "Stoicism, boundaries...). No hashtags.\n"
        "- \"hashtags\": array of 4 single-word lowercase hashtags WITHOUT the # symbol.\n"
        "- \"visual_queries\": array of 5 stock-footage search terms, 1-3 plain words each, "
        "naming real things that exist as stock video (storm clouds, marble statue, city "
        "night rain, silhouette walking, candle flame, chess pieces). Dark, cinematic mood. "
        "All different. They will be cut together in order.\n\n"
        "RECENT VIDEOS - do not repeat or paraphrase these:\n"
        f"{recent_block}\n\n"
        "Return ONLY valid JSON with exactly these keys: script, on_screen_hook, title, "
        "description, hashtags, visual_queries."
    )


_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def call_gemini(prompt):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = get_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=1.0, response_mime_type="application/json"
                ),
            )
            return (resp.text or "").strip()
        except genai_errors.APIError as e:
            if getattr(e, "code", None) in (429, 500, 503) and attempt < max_retries - 1:
                delay = 2 * (2 ** attempt) + random.uniform(0, 1)
                log(f"[Gemini] {e.code} - retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            raise
    return ""


def extract_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Gemini JSON was not an object")
    return data


def _strip_hashtags(s):
    # must run BEFORE clean_text(), which deletes the '#' and would leave a stray word behind
    return re.sub(r"#\w+", "", s or "")


def normalize_post(data, fmt_name):
    script = clean_text(_strip_hashtags(str(data.get("script", ""))))
    script = re.sub(r"(?i)^script:\s*", "", script)
    spoken_hook = first_sentence(script)

    on_screen = clean_text(str(data.get("on_screen_hook", "")))
    if not on_screen:
        on_screen = " ".join(spoken_hook.split()[:7])
    on_screen = " ".join(on_screen.split()[:8]).rstrip(".,;:")

    title = clean_text(_strip_hashtags(str(data.get("title", "")))).replace('"', "")
    title = title.strip(" -|:")
    if not title:
        title = spoken_hook
    if len(title) > 60:
        title = title[:60].rsplit(" ", 1)[0].rstrip(",;:-")

    tags = []
    for t in data.get("hashtags", []) or []:
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())
        if t and t not in tags:
            tags.append(t)
    for base in ("narcissist", "stoicism", "mindset"):
        if len(tags) >= 5:
            break
        if base not in tags:
            tags.append(base)

    queries = []
    for q in data.get("visual_queries", []) or []:
        q = clean_text(str(q)).lower()[:40]
        if q and q not in queries:
            queries.append(q)

    return {
        "format": fmt_name,
        "script": script,
        "hook": spoken_hook,
        "on_screen_hook": on_screen,
        "title": title,
        "description": clean_text(_strip_hashtags(str(data.get("description", "")))),
        "hashtags": tags[:5],
        "visual_queries": queries[:5],
    }


def validate_post(post, history):
    hard, soft = [], []
    n = len(post["script"].split())
    if n < SCRIPT_MIN_WORDS:
        hard.append(f"script too short ({n} words)")
    if n > SCRIPT_MAX_WORDS:
        hard.append(f"script too long ({n} words)")
    low = post["script"].lower()
    if any(low.startswith(b) for b in BAD_STARTS):
        soft.append("weak hook opening")
    if len(post["hook"].split()) > 12:
        soft.append("hook too long")
    key = " ".join(low.split()[:5])
    for h in history[-40:]:
        if " ".join(str(h.get("script", "")).lower().split()[:5]) == key:
            soft.append("hook repeats a recent video")
            break
    return hard, soft


def generate_post(history):
    recent_formats = [h.get("format") for h in history[-2:]]
    pool = [f for f in FORMATS if f["name"] not in recent_formats] or FORMATS
    best = None
    for attempt in range(1, 5):
        fmt = _rng.choice(pool)
        try:
            data = extract_json(call_gemini(build_prompt(fmt, history)))
        except (ValueError, KeyError, AttributeError) as e:
            log(f"[Script] attempt {attempt}: unusable response ({e})")
            continue
        post = normalize_post(data, fmt["name"])
        hard, soft = validate_post(post, history)
        if hard:
            log(f"[Script] attempt {attempt}: rejected - {', '.join(hard)}")
            continue
        best = post
        if not soft:
            return post
        log(f"[Script] attempt {attempt}: {', '.join(soft)} - regenerating")
    if best:
        return best
    raise RuntimeError("Gemini did not return a usable script after 4 attempts")


# ============================================================
# 2. STOCK FOOTAGE (several clips per video -> frequent cuts)
# ============================================================
def search_pixabay(query):
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 30,
                    "safesearch": "true", "min_height": 720},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"[Pixabay] '{query}' -> HTTP {r.status_code}")
            return []
        return r.json().get("hits", [])
    except Exception as e:
        log(f"[Pixabay] '{query}' failed: {e}")
        return []


def pick_variant(hit):
    vids = hit.get("videos", {}) or {}
    for key in ("medium", "small", "large"):
        v = vids.get(key)
        if v and v.get("url") and 0 < int(v.get("size", 0) or 0) < 80_000_000:
            return v
    return None


def rank_hits(hits):
    portrait, landscape = [], []
    for h in hits:
        if float(h.get("duration", 0) or 0) < 4:
            continue
        v = pick_variant(h)
        if not v:
            continue
        is_portrait = int(v.get("height", 0) or 0) > int(v.get("width", 0) or 0)
        (portrait if is_portrait else landscape).append((h, v))
    _rng.shuffle(portrait)
    _rng.shuffle(landscape)
    return portrait + landscape


def download_clip(url, dest):
    with requests.get(url, timeout=90, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    if Path(dest).stat().st_size < 100_000:
        raise RuntimeError("download too small")
    clip = VideoFileClip(str(dest), audio=False)
    if clip.duration < 3:
        clip.close()
        raise RuntimeError("clip too short")
    return clip


def fetch_clips(queries, want):
    clips, used = [], set()
    candidates = list(queries) + _rng.sample(FALLBACK_QUERIES, k=len(FALLBACK_QUERIES))
    for q in candidates:
        if len(clips) >= want:
            break
        for hit, v in rank_hits(search_pixabay(q))[:6]:
            if hit.get("id") in used:
                continue
            dest = WORK_DIR / f"clip_{len(clips)}.mp4"
            try:
                clip = download_clip(v["url"], dest)
            except Exception as e:
                log(f"[Pixabay] download failed ({e})")
                continue
            used.add(hit.get("id"))
            clips.append(clip)
            log(f"[Pixabay] clip {len(clips)}/{want}: '{q}' ({clip.size[0]}x{clip.size[1]}, {clip.duration:.0f}s)")
            break
    if len(clips) < 2:
        raise RuntimeError("Could not download at least 2 usable stock clips")
    return clips


# ============================================================
# 3. VOICEOVER + WORD TIMINGS
# ============================================================
async def _tts_once(text, voice, out_path):
    kwargs = {"rate": VOICE_RATE, "pitch": VOICE_PITCH}
    try:
        # newer edge-tts versions only send word timings if you ask for them
        comm = edge_tts.Communicate(text, voice, boundary="WordBoundary", **kwargs)
    except TypeError:
        comm = edge_tts.Communicate(text, voice, **kwargs)
    words, sentences = [], []
    with open(out_path, "wb") as f:
        async for chunk in comm.stream():
            kind = chunk.get("type")
            if kind == "audio":
                f.write(chunk["data"])
            elif kind in ("WordBoundary", "SentenceBoundary"):
                start = chunk["offset"] / 1e7
                item = {"text": chunk["text"], "start": start,
                        "end": start + chunk["duration"] / 1e7}
                (words if kind == "WordBoundary" else sentences).append(item)
    return words, sentences


async def generate_voiceover(text, voice, out_path):
    last_err = None
    for attempt in range(3):
        try:
            v = voice if attempt == 0 else DEFAULT_VOICE
            words, sents = await _tts_once(text, v, out_path)
            if os.path.getsize(out_path) < 2000:
                raise RuntimeError("TTS returned empty audio")
            log(f"[TTS] {v}: {len(words)} word events, {len(sents)} sentence events")
            return words, sents
        except Exception as e:
            last_err = e
            log(f"[TTS] attempt {attempt + 1}/3 failed: {e}")
            await asyncio.sleep(4)
    raise last_err


def detect_leading_silence(audio_clip, threshold=0.03, max_trim=0.6):
    """Seconds of silence at the start of the voiceover (so word 1 lands on frame 0)."""
    try:
        sr = 16000
        arr = audio_clip.to_soundarray(fps=sr)
        env = np.abs(arr).max(axis=1) if arr.ndim == 2 else np.abs(arr)
        idx = int(np.argmax(env > threshold))
        if env[idx] <= threshold:
            return 0.0
        return float(min(max(idx / sr - 0.04, 0.0), max_trim))
    except Exception as e:
        log(f"[Audio] silence detection skipped: {e}")
        return 0.0


def _word_weight(w):
    core = re.sub(r"\W", "", w)
    weight = max(2, len(core)) + 2
    if w and w[-1] in ",;:":
        weight += 3
    if w and w[-1] in ".!?":
        weight += 6
    return weight


def spread_words(words, start, end):
    weights = [_word_weight(w) for w in words]
    total_w = float(sum(weights)) or 1.0
    out, t = [], start
    for w, wt in zip(words, weights):
        dur = (end - start) * wt / total_w
        out.append({"word": w, "start": t, "end": t + dur})
        t += dur
    return out


def build_word_timings(script, word_events, sentence_events, audio_len):
    """Best available timings: real word events > sentence events > weighted estimate."""
    if word_events:
        return [{"word": e["text"], "start": e["start"], "end": e["end"]} for e in word_events]
    if sentence_events:
        out = []
        for s in sentence_events:
            toks = s["text"].split()
            if toks:
                out += spread_words(toks, s["start"], s["end"])
        if out:
            return out
    return spread_words(script.split(), 0.0, audio_len)


def finalize_timings(raw, lead, total):
    """Shift by the trimmed silence and make captions continuous (no gaps = no flicker)."""
    out = []
    for w in raw:
        s = max(0.0, w["start"] - lead)
        if s >= total:
            continue
        out.append({"word": w["word"], "start": s, "raw_end": max(s, w["end"] - lead)})
    out.sort(key=lambda x: x["start"])
    if not out:
        return []
    out[0]["start"] = 0.0
    for i, w in enumerate(out):
        w["end"] = out[i + 1]["start"] if i + 1 < len(out) else total
    return [w for w in out if w["end"] - w["start"] > 0.02]


# ============================================================
# 4. SYNTHESISED SOUNDTRACK (impact on frame 0, whoosh on cuts)
# ============================================================
def synth_soundtrack(total, cut_times, out_path, seed=None):
    rng = np.random.default_rng(seed)
    sr = 44100
    length = total + 0.6
    n = int(length * sr)
    t = np.arange(n) / sr

    # dark minor drone
    root = float(rng.choice([55.0, 58.27, 61.74, 65.41, 73.42]))
    ratios = [1.0, 1.189, 1.498, 2.0, 2.378]
    pad = np.zeros(n)
    for i, r in enumerate(ratios):
        lfo = 0.65 + 0.35 * np.sin(2 * np.pi * (0.06 + 0.025 * i) * t + rng.uniform(0, 2 * np.pi))
        for det in (-0.35, 0.35):
            pad += np.sin(2 * np.pi * (root * r + det) * t + rng.uniform(0, 2 * np.pi)) * lfo / (1 + 0.6 * i)
    pad /= (np.max(np.abs(pad)) + 1e-9)
    pad *= np.minimum(1.0, t / 0.05)                 # 50 ms fade-in (no click)
    pad *= np.clip((length - t) / 0.6, 0, 1)         # fade-out
    out = pad * 0.25

    def place(sig, at):
        i0 = int(at * sr)
        if i0 < 0:
            sig, i0 = sig[-i0:], 0
        if i0 >= n:
            return
        seg = sig[: n - i0]
        out[i0:i0 + len(seg)] += seg

    # impact hit at t=0: the "pattern interrupt" that makes autoplay-on-sound viewers stay
    bt = np.arange(int(1.0 * sr)) / sr
    phase = 2 * np.pi * (38 * bt + 60 * (1 - np.exp(-14 * bt)) / 14)
    boom = np.sin(phase) * np.exp(-4.0 * bt)
    click = rng.standard_normal(len(bt)) * np.exp(-bt * 60) * 0.4
    place(boom + click, 0.0)

    # slow heartbeat for tension
    period = 60.0 / rng.uniform(58, 72)
    ht = np.arange(int(0.25 * sr)) / sr
    thump = np.sin(2 * np.pi * 52 * ht) * np.exp(-ht * 22)
    beat = 1.0
    while beat < length:
        place(thump * 0.30, beat)
        place(thump * 0.18, beat + 0.17)
        beat += period

    # whoosh into every cut
    for c in cut_times:
        m = int(0.32 * sr)
        noise = rng.standard_normal(m)
        low = np.convolve(noise, np.ones(40) / 40, mode="same")
        env = np.sin(np.linspace(0, np.pi, m)) ** 2
        place(low * env * 2.0, c - 0.20)

    out = out / (np.max(np.abs(out)) + 1e-9) * 0.40
    right = np.roll(out, 31)
    stereo = np.stack([out, right], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return str(out_path)


# ============================================================
# 5. TEXT OVERLAYS (fonts, hook banner, karaoke captions, CTA)
# ============================================================
THEMES = [
    {"name": "gold",  "active_text": (255, 214, 0),   "box": None,
     "hook_bg": (255, 214, 0),  "hook_fg": (10, 10, 10)},
    {"name": "green", "active_text": (255, 255, 255), "box": (0, 191, 73),
     "hook_bg": (0, 191, 73),   "hook_fg": (255, 255, 255)},
    {"name": "red",   "active_text": (255, 255, 255), "box": (220, 38, 38),
     "hook_bg": (220, 38, 38),  "hook_fg": (255, 255, 255)},
    {"name": "cyan",  "active_text": (0, 229, 255),   "box": None,
     "hook_bg": (0, 229, 255),  "hook_fg": (5, 10, 15)},
]

FONT_URLS = [
    "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
    "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
]
SYSTEM_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def get_font_path():
    """Anton (bold, condensed, the classic Shorts caption look). Falls back to system fonts."""
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    p = FONT_DIR / "Anton-Regular.ttf"
    if p.exists() and p.stat().st_size > 10_000:
        return str(p)
    for url in FONT_URLS:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            if len(r.content) > 10_000 and r.content[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                p.write_bytes(r.content)
                return str(p)
        except Exception as e:
            log(f"[Font] Anton download failed ({e})")
    log("[Font] using a system font instead")
    for f in SYSTEM_FONTS:
        if os.path.exists(f):
            return f
    return None


def load_font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def group_words(timings, max_words=3, max_chars=17, pause=0.40):
    groups, cur, chars = [], [], 0
    for i, w in enumerate(timings):
        L = len(w["word"])
        gap = 0.0
        if i:
            prev = timings[i - 1]
            gap = w["start"] - prev.get("raw_end", prev["end"])
        if cur and (len(cur) >= max_words or chars + 1 + L > max_chars or gap > pause):
            groups.append(cur)
            cur, chars = [], 0
        cur.append(w)
        chars += L + (1 if len(cur) > 1 else 0)
    if cur:
        groups.append(cur)
    return groups


def render_caption(group, active_idx, font_path, theme):
    words = [w["word"].upper() for w in group]
    size = 120
    while True:
        font = load_font(font_path, size)
        space = font.getlength(" ")
        widths = [font.getlength(x) for x in words]
        total_w = sum(widths) + space * (len(words) - 1)
        if total_w <= W * 0.88 or size <= 52:
            break
        size -= 6
    stroke = max(5, size // 14)
    asc, desc = font.getmetrics()
    pad_y = 22
    img = Image.new("RGBA", (W, asc + desc + 2 * pad_y + 2 * stroke), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = (W - total_w) / 2
    baseline = pad_y + stroke + asc
    for i, (word, wd) in enumerate(zip(words, widths)):
        active = i == active_idx
        if active and theme["box"]:
            l, t, r, b = d.textbbox((x, baseline), word, font=font, anchor="ls")
            d.rounded_rectangle([l - 16, t - 12, r + 16, b + 12], radius=16, fill=theme["box"])
        fill = theme["active_text"] if active else (255, 255, 255)
        d.text((x, baseline), word, font=font, fill=fill, anchor="ls",
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        x += wd + space
    return img


def _wrap(text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and font.getlength(trial) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def render_hook(text, font_path, theme):
    text = text.upper()
    size = 100
    while True:
        font = load_font(font_path, size)
        lines = _wrap(text, font, W * 0.84)
        if len(lines) <= 2 or size <= 60:
            break
        size -= 6
    asc, desc = font.getmetrics()
    pad_x, pad_y, gap = 28, 14, 12
    box_h = asc + desc + 2 * pad_y
    img = Image.new("RGBA", (W, len(lines) * box_h + (len(lines) - 1) * gap), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 0
    for ln in lines:
        lw = font.getlength(ln)
        x = (W - lw) / 2
        d.rounded_rectangle([x - pad_x, y, x + lw + pad_x, y + box_h], radius=20, fill=theme["hook_bg"])
        d.text((x, y + pad_y), ln, font=font, fill=theme["hook_fg"])
        y += box_h + gap
    return img


def render_cta(text, font_path, theme):
    text = text.upper()
    pad_x, pad_y = 36, 18
    size = 42
    while True:
        font = load_font(font_path, size)
        lw = font.getlength(text)
        if lw + 2 * pad_x <= W * 0.9 or size <= 24:
            break
        size -= 2
    asc, desc = font.getmetrics()
    box_w, box_h = int(lw + 2 * pad_x), asc + desc + 2 * pad_y
    img = Image.new("RGBA", (W, box_h + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (W - box_w) // 2
    d.rounded_rectangle([x0, 0, x0 + box_w, box_h], radius=box_h // 2,
                        fill=(0, 0, 0, 175), outline=theme["hook_bg"], width=4)
    d.text((x0 + pad_x, pad_y), text, font=font, fill=(255, 255, 255))
    return img


def build_shade():
    """Darkening + vignette + cool tint, as one multiply layer (faster than a ColorClip overlay)."""
    y = np.linspace(-1, 1, H)[:, None]
    x = np.linspace(-1, 1, W)[None, :]
    r = np.sqrt(x ** 2 + (y ** 2) * 0.6)
    vign = 1.0 - 0.45 * np.clip(r - 0.35, 0, 1) ** 1.4
    base = np.clip(0.64 * vign, 0.22, 1.0)
    rgb = np.dstack([base * 0.93, base * 0.97, base * 1.0])
    return Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))


# ============================================================
# 6. VIDEO COMPOSITION
# ============================================================
def plan_spans(total, timings, rng):
    """Cut points: roughly every 2.4-3.2 s, snapped to the nearest word start."""
    target = rng.uniform(2.4, 3.2)
    n = max(3, int(round(total / target)))
    word_starts = [w["start"] for w in timings]
    starts = [0.0]
    for i in range(1, n):
        ideal = total * i / n
        snapped = min(word_starts, key=lambda s: abs(s - ideal)) if word_starts else ideal
        if snapped - starts[-1] >= 1.6 and total - snapped >= 1.2:
            starts.append(snapped)
    ends = starts[1:] + [total]
    return list(zip(starts, ends))


def assign_sources(spans, n_sources, rng):
    order = list(range(n_sources))
    rng.shuffle(order)
    uses = {}
    segments = []
    for i, (s, e) in enumerate(spans):
        src = order[i % n_sources]
        u = uses.get(src, 0)
        uses[src] = u + 1
        zoom_in = i % 2 == 0
        segments.append({
            "start": s, "end": e, "src": src, "uses": u,
            "frac": rng.random(),
            "z0": 1.0 if zoom_in else 1.20,
            "z1": 1.20 if zoom_in else 1.03,
            "pan": rng.uniform(-0.04, 0.04),
        })
    return segments


def zoom_box(sw, sh, z, cx_frac, cy_frac):
    target_ar = W / H
    if sw / sh > target_ar:
        ch, cw = float(sh), sh * target_ar
    else:
        cw, ch = float(sw), sw / target_ar
    cw, ch = cw / z, ch / z
    x0 = min(max(cx_frac * sw - cw / 2, 0), sw - cw)
    y0 = min(max(cy_frac * sh - ch / 2, 0), sh - ch)
    return (x0, y0, x0 + cw, y0 + ch)


def build_frame_function(segments, sources, timings, hook_text, theme, font_path, total):
    """Returns make_frame(t) -> HxWx3 uint8. Everything (zoom, cuts, overlays) is drawn here."""
    shade = build_shade()
    hook_img = render_hook(hook_text, font_path, theme)
    hook_y = int(H * 0.12)

    cap_track = []
    for group in group_words(timings):
        for ai, w in enumerate(group):
            cap_track.append((w["start"], render_caption(group, ai, font_path, theme)))
    cap_starts = [c[0] for c in cap_track]

    cta_img = render_cta(CHANNEL_CTA, font_path, theme)
    cta_y = int(H * 0.70)
    cta_t0 = max(HOOK_SECONDS + 1.0, total * 0.55)
    cta_t1 = min(total - 0.4, cta_t0 + 2.4)

    seg_starts = [s["start"] for s in segments]
    white = Image.new("RGB", (W, H), (255, 255, 255))

    def make_frame(t):
        t = min(max(t, 0.0), total - 1e-3)
        si = max(bisect.bisect_right(seg_starts, t) - 1, 0)
        seg = segments[si]
        src = sources[seg["src"]]
        seg_len = max(seg["end"] - seg["start"], 0.1)
        lt = t - seg["start"]
        p = min(max(lt / seg_len, 0.0), 1.0)

        usable = max(src.duration - 0.15, 0.5)
        room = max(usable - seg_len, 0.0)
        offset = ((seg["uses"] * 0.37 + seg["frac"]) % 1.0) * room
        frame = src.get_frame((offset + lt) % usable)
        sh, sw = frame.shape[:2]

        z = seg["z0"] + (seg["z1"] - seg["z0"]) * p
        box = zoom_box(sw, sh, z, 0.5 + seg["pan"] * (p - 0.5) * 2, 0.5)
        img = Image.fromarray(frame).resize((W, H), Image.Resampling.BILINEAR, box=box)
        img = ImageChops.multiply(img, shade)

        if si > 0 and lt < 0.12:                      # quick flash on every cut
            img = Image.blend(img, white, 0.28 * (1 - lt / 0.12))

        if t < HOOK_SECONDS:                          # visible from frame 0, no fade-in
            img.paste(hook_img, (0, hook_y), hook_img)

        ci = max(bisect.bisect_right(cap_starts, t) - 1, 0)
        cap = cap_track[ci][1]
        img.paste(cap, (0, int(H * 0.52 - cap.height / 2)), cap)

        if cta_t0 <= t < cta_t1:
            img.paste(cta_img, (0, cta_y), cta_img)
        return np.array(img)

    return make_frame


def create_video(post, output_path="final_output.mp4"):
    WORK_DIR.mkdir(exist_ok=True)
    theme = _rng.choice(THEMES)
    voice_name = _rng.choice(VOICES)
    font_path = get_font_path()
    script = post["script"]

    log(f"[Video] voice={voice_name} theme={theme['name']}")
    voice_path = str(WORK_DIR / "voiceover.mp3")
    word_events, sent_events = asyncio.run(generate_voiceover(script, voice_name, voice_path))

    voice_raw = AudioFileClip(voice_path)
    lead = detect_leading_silence(voice_raw)
    voice_len = float(voice_raw.duration)
    raw = build_word_timings(script, word_events, sent_events, voice_len)
    speech_end = max(w["end"] for w in raw) - lead
    total = max(4.0, min(voice_len - lead, speech_end + 0.35) + 0.15)   # tight tail = clean loop
    timings = finalize_timings(raw, lead, total)
    log(f"[Video] duration={total:.1f}s, trimmed {lead:.2f}s of lead-in, {len(timings)} caption words")

    spans = plan_spans(total, timings, _rng)
    sources = fetch_clips(post["visual_queries"], want=min(5, len(spans)))
    segments = assign_sources(spans, len(sources), _rng)
    log(f"[Video] {len(segments)} cuts over {len(sources)} clips")

    music_path = synth_soundtrack(total, [s[0] for s in spans[1:]], WORK_DIR / "soundtrack.wav")
    voice = voice_raw.subclipped(lead).with_effects([MultiplyVolume(1.25)])
    music = AudioFileClip(music_path)
    audio = CompositeAudioClip([music, voice]).with_duration(total)

    make_frame = build_frame_function(
        segments, sources, timings, post["on_screen_hook"], theme, font_path, total
    )
    clip = VideoClip(make_frame, duration=total).with_audio(audio)

    log("[Video] Rendering 1080x1920 ...")
    clip.write_videofile(
        output_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        audio_bitrate="192k",
        preset="veryfast",
        threads=4,
        ffmpeg_params=["-crf", "20", "-movflags", "+faststart"],
        logger=None,
    )

    for c in sources:
        try:
            c.close()
        except Exception:
            pass
    for c in (voice_raw, music, clip):
        try:
            c.close()
        except Exception:
            pass
    return output_path, {"voice": voice_name, "theme": theme["name"], "duration": round(total, 1)}


# ============================================================
# 7. UPLOAD TO YOUTUBE
# ============================================================
BASE_TAGS = [
    "stoicism", "narcissist", "narcissism", "mindset", "shorts", "dark psychology",
    "emotional control", "toxic relationship", "stoic wisdom", "self improvement",
]


def build_description(post):
    ordered = list(post["hashtags"])[:4]
    if "shorts" not in ordered:
        ordered.append("shorts")
    hashtags = " ".join("#" + h for h in ordered)
    parts = [post["description"], f"{CHANNEL_CTA}.", hashtags]
    return "\n\n".join(p for p in parts if p).replace("<", "").replace(">", "")[:4800]


def build_tags(post):
    tags, size = [], 0
    for t in list(post["hashtags"]) + BASE_TAGS:
        if t not in tags and size + len(t) + 1 <= 450:
            tags.append(t)
            size += len(t) + 1
    return tags


def upload_to_youtube(video_file_path, title, description, tags):
    creds_dict = json.loads(YOUTUBE_CREDENTIALS_JSON)
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=creds_dict["refresh_token"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
    )
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title.replace("<", "").replace(">", "")[:100],
            "description": description,
            "tags": tags,
            "categoryId": "27",
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log(f"[YouTube] Uploaded {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(2 ** retries + random.random())
                continue
            raise
        except (ConnectionError, TimeoutError) as e:
            if retries >= 5:
                raise
            retries += 1
            log(f"[YouTube] network error ({e}); retry {retries}/5")
            time.sleep(2 ** retries)
    log(f"[YouTube] Success! Video ID: {response['id']}")
    return response["id"]


# ============================================================
# MAIN
# ============================================================
def main():
    history = load_history()

    log("=== 1) Generating script ===")
    post = generate_post(history)
    log(f"Format : {post['format']}")
    log(f"Title  : {post['title']}")
    log(f"Banner : {post['on_screen_hook']}")
    log(f"Script : {post['script']}")

    log("=== 2) Creating video ===")
    video_path, meta = create_video(post)

    if DRY_RUN:
        log(f"=== DRY RUN: video saved to {video_path}, skipping upload and history ===")
        return

    log("=== 3) Uploading to YouTube ===")
    video_id = upload_to_youtube(
        video_path, post["title"], build_description(post), build_tags(post)
    )
    save_history(history, {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "video_id": video_id,
        "format": post["format"],
        "title": post["title"],
        "hook": post["hook"],
        "script": post["script"],
        **meta,
    })
    log("=== DONE ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise
