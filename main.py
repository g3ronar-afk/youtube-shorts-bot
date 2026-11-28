"""
Auto YouTube Shorts pipeline - v3.

v3 adds (on top of everything in v2 below):
  * Topic priors taken from this channel's own top Shorts ("say this phrase...", signs,
    boundaries) until real view data exists, plus a new "say_this" format.
  * Shorts -> long-form funnel: the description links to your latest public long-form video
    (made by longform.py) and some Shorts show "watch the full breakdown" on screen.
  * Monetization block read from monetization.json: free guide link and affiliate links
    (with a disclosure line). Empty URLs are skipped, so nothing broken is ever posted.

Swipe-away is decided in the first ~2 seconds, so v2 puts its effort there:

  SCRIPT   3-step "hook tournament": Gemini writes 8 openers, a second call scores them
           like a retention analyst, and the winner is locked in as the first spoken line.
           Rotating formats, endings (mic-drop / loop / comment-bait) and lengths.
  FIRST 2s Hook banner + caption + voice + impact sound on frame 0, a snap-zoom on the
           first frames, and the opening shot is the MOST DYNAMIC moment of the best clip
           (motion is measured, static footage is avoided).
  PACING   Faster cuts early (every ~1.6-2.2 s), slower later; pop-in captions; cuts land
           on word boundaries; whoosh on every cut.
  PICTURE  Sharper source files (1080p instead of 720p), optional portrait footage from
           Pexels, true 1080x1920 output, text kept inside YouTube's UI safe zone.
  SOUND    Music ducks under the voice (sidechain), loudness normalised to -14 LUFS.
  SUBS     CTA promises the NEXT video ("Follow for the next rule: ...") and the next
           run really makes that video.
  LEARNING If the YouTube token has read access, every run pulls views/likes of past
           videos and feeds the winners/losers back into the next script.
  ENGINE   No moviepy: frames go straight into ffmpeg, so the whole render is testable
           and faster.

Environment variables
  GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS   (same as before)
  PEXELS_API_KEY   optional - portrait stock footage (free key at pexels.com/api)
  GEMINI_MODEL     optional - defaults to the model your old script used
  DRY_RUN          "true" = render the video but do NOT upload
"""
import os
import re
import sys
import json
import time
import wave
import bisect
import random
import shutil
import asyncio
import datetime
import statistics
import subprocess
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

# ============================================================
# CONFIG
# ============================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
YOUTUBE_CREDENTIALS_JSON = os.environ.get("YOUTUBE_CREDENTIALS")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

W, H, FPS = 1080, 1920, 30
HISTORY_FILE = Path("history.json")
WORK_DIR = Path("work")
FONT_DIR = Path("assets/fonts")

CHANNEL_CTA = "Follow for daily Stoic survival rules"
SCRIPT_MIN_WORDS, SCRIPT_MAX_WORDS = 28, 70
HOOK_SECONDS = 3.0
VOICE_RATE = "+6%"
VOICE_PITCH = "-2Hz"
VOICES = [
    "en-US-AndrewNeural",
    "en-US-BrianNeural",
    "en-US-GuyNeural",
    "en-US-ChristopherNeural",
]
DEFAULT_VOICE = "en-US-AndrewNeural"
PROFILE_FPS = 6          # motion analysis sampling rate
MONETIZATION_FILE = Path("monetization.json")
LONGFORM_FILE = Path("longform.json")

# This channel's own best Shorts (Studio, last 28 days). Used as style references until the
# bot has enough of its own view data to learn from.
WINNING_SHORTS = [
    "3 Hidden Signs Someone Is Mind Gaming You",
    "How manipulators turn your boundaries against you",
    "Say these four words to end their game",
    "Say this phrase when they force you to defend yourself",
    "When their apology turns into your guilt",
]
# Until real data exists, lean toward formats that match those winners (1.0 = neutral).
FORMAT_PRIOR = {"say_this": 2.5, "exact_words": 2.0, "red_flags": 1.5,
                "decode_tactic": 1.5, "reframe": 1.2}

_rng = random.Random()
_ffmpeg = None


def log(msg):
    print(msg, flush=True)


# ============================================================
# 0. FFMPEG HELPERS
# ============================================================
def ffmpeg_bin():
    global _ffmpeg
    if _ffmpeg:
        return _ffmpeg
    p = shutil.which("ffmpeg")
    if not p:
        try:
            import imageio_ffmpeg
            p = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            p = None
    if not p:
        raise RuntimeError("ffmpeg not found: install ffmpeg or `pip install imageio-ffmpeg`")
    _ffmpeg = p
    return p


def run_ffmpeg(args, timeout=600):
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode("utf-8", "ignore")[-700:])
    return r


def probe(path):
    """(width, height, duration_seconds) parsed from `ffmpeg -i` (no ffprobe needed)."""
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True)
    text = r.stderr.decode("utf-8", "ignore")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", text)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return w, h, dur


COVER_CROP = "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'"   # centre-crop any source to 9:16


# ============================================================
# 1. HISTORY + PERFORMANCE MEMORY
# ============================================================
def load_history():
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history, entry=None):
    if entry:
        history.append(entry)
    HISTORY_FILE.write_text(
        json.dumps(history[-300:], indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _parse_date(s):
    try:
        d = datetime.datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def aged_entries(history, min_hours=36):
    """Videos old enough for their view count to mean something, with stats attached."""
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for h in history:
        if h.get("views") is None:
            continue
        d = _parse_date(h.get("date"))
        if d and (now - d).total_seconds() >= min_hours * 3600:
            out.append(h)
    return out


def format_performance(history):
    """{format: median views}, only for formats with >=2 aged videos (and >=8 videos overall)."""
    aged = aged_entries(history)
    if len(aged) < 8:
        return {}
    by = {}
    for h in aged:
        by.setdefault(h.get("format"), []).append(h["views"])
    return {k: statistics.median(v) for k, v in by.items() if k and len(v) >= 2}


def winners_notes():
    return (
        "PROVEN WINNERS on this channel (its most-viewed Shorts so far). Match their directness "
        "and the 'say this exact phrase' / 'hidden signs' / 'boundaries' angles, but write "
        "something NEW, never a copy:\n"
        + "\n".join(f"- {t}" for t in WINNING_SHORTS) + "\n\n"
    )


def performance_notes(history):
    aged = aged_entries(history)
    if len(aged) < 10:
        return winners_notes()
    ranked = sorted(aged, key=lambda h: h["views"], reverse=True)
    best = [h.get("hook", "") for h in ranked[:5] if h.get("hook")]
    worst = [h.get("hook", "") for h in ranked[-3:] if h.get("hook")]
    if not best:
        return winners_notes()
    return winners_notes() + (
        "REAL PERFORMANCE DATA from the bot's own recent videos (views). Learn the pattern, "
        "do not copy:\n"
        "BEST hooks:\n" + "\n".join(f"- {b}" for b in best) + "\n"
        "WORST hooks (avoid this style):\n" + "\n".join(f"- {b}" for b in worst) + "\n\n"
    )


def pending_teaser(history):
    """The previous video promised a topic - the next video should deliver it."""
    if not history:
        return ""
    last = history[-1]
    d = _parse_date(last.get("date"))
    teaser = (last.get("next_teaser") or "").strip()
    if teaser and d:
        age = (datetime.datetime.now(datetime.timezone.utc) - d).total_seconds()
        if age < 72 * 3600:
            return teaser
    return ""


# ============================================================
# 2. SCRIPT GENERATION (hook tournament)
# ============================================================
FORMATS = [
    {"name": "red_flags",
     "brief": "Reveal 3 specific, uncomfortable signs of narcissistic manipulation. Number them "
              "out loud (one, two, three). Save the sharpest sign for last."},
    {"name": "reframe",
     "brief": "Take a belief the viewer holds ('being the bigger person keeps the peace') and "
              "flip it with one concrete reason why it backfires."},
    {"name": "stoic_rule",
     "brief": "Teach ONE Stoic principle (Marcus Aurelius, Epictetus or Seneca) as a modern rule "
              "for dealing with a toxic person. Paraphrase the idea, never invent quotes."},
    {"name": "exact_words",
     "brief": "Give the exact sentence to say (or the exact thing NOT to say) to a manipulator, "
              "and why it works (gray rock, refusing to justify or explain)."},
    {"name": "say_this",
     "brief": "Name ONE specific moment (they force you to defend yourself, guilt-trip you, "
              "twist your words, give the silent treatment) and give ONE exact phrase of 3-8 "
              "words to say. Say the phrase out loud early, then explain in two short sentences "
              "why it works and what happens if you don't say it."},
    {"name": "scenario",
     "brief": "A mini-scene in second person ('You tell them you're hurt. They say...'), then "
              "reveal what they were really doing."},
    {"name": "uncomfortable_truth",
     "brief": "One uncomfortable truth about why people stay in toxic dynamics, delivered with "
              "compassion and power. Make the viewer feel seen."},
    {"name": "decode_tactic",
     "brief": "Name ONE manipulation tactic (gaslighting, silent treatment, love bombing, "
              "hoovering, triangulation, DARVO), show how it sounds in real life in one short "
              "example, then give the counter-move."},
    {"name": "after_mistake",
     "brief": "The #1 mistake people make right after being discarded, guilt-tripped or "
              "ghosted by a narcissist, and what to do instead."},
]

ENDINGS = {
    "mic_drop": "FINAL sentence: a mic-drop of 8 words or fewer that reframes everything "
                "(the line people would screenshot).",
    "loop": "FINAL sentence: a lead-in fragment of 8 words or fewer that flows grammatically "
            "into the FIRST sentence, so when the video replays it sounds like one continuous "
            "thought. Example: first sentence 'They want you doubting your own memory.' final "
            "sentence 'And the reason they do it is simple.'",
    "comment": "FINAL sentence: ask ONE easy question the viewer answers in the comments, "
               "12 words or fewer, e.g. 'Which one hit home? Comment one, two or three.'",
}
ENDING_WEIGHTS = {"mic_drop": 4, "loop": 3, "comment": 3}

BAD_STARTS = (
    "stop forgiving", "you need to", "it is important", "it's important", "we should",
    "most people", "have you ever", "did you know", "imagine", "in a world", "in today",
    "are you", "do you ever", "let me tell", "today i", "hey guys", "welcome", "so,",
    "let's talk", "here's the thing", "what if i told",
)
AI_ISMS = ("journey", "tapestry", "unlock", "delve", "in today's world", "let's dive",
           "game-changer", "navigate the", "testament")

FALLBACK_QUERIES = [
    "dark storm clouds", "city night rain", "silhouette walking fog", "candle flame dark",
    "person alone window night", "chess pieces", "mirror reflection", "clock ticking",
    "lightning sky", "empty street night", "hands clenched", "woman looking out window",
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


def _strip_hashtags(s):
    # must run BEFORE clean_text(), which deletes the '#' and would leave a stray word behind
    return re.sub(r"#\w+", "", s or "")


def first_sentence(text):
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return parts[0] if parts else (text or "")


def split_sentences(text):
    return [p for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p]


_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def call_gemini(prompt, temperature=1.0):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = get_client().models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temperature, response_mime_type="application/json"
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
    m = re.search(r"[\{\[].*[\}\]]", text, re.S)
    data = json.loads(m.group(0) if m else text)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    return data


# ---- stage A: ideate hooks ---------------------------------------------------
def prompt_hooks(fmt, history, teaser, notes):
    recent = [(h.get("hook") or "").strip() for h in history[-25:]]
    recent_block = "\n".join(f"- {r}" for r in recent if r) or "- (none yet)"
    return (
        "You write the OPENING LINE of YouTube Shorts for a US audience in the niche of "
        "narcissistic manipulation and Stoic resilience.\n"
        "The opening line is spoken in the first 2 seconds. Its only job: make a stranger who "
        "is scrolling stop and stay.\n\n"
        f"VIDEO FORMAT: {fmt['name']} - {fmt['brief']}\n"
        + (f"TOPIC LOCK: this video must deliver on this promise made in the last video: "
           f"'{teaser}'.\n" if teaser else "")
        + "\nWrite 8 DIFFERENT opening lines, 5 to 10 words each, plain spoken English, no "
        "emojis. Use 8 different structures, for example:\n"
        "- callout: 'If they say this, you're already being played.'\n"
        "- number + payoff: 'Three phrases narcissists use right before they discard you.'\n"
        "- contradiction: 'Being the bigger person is exactly how they win.'\n"
        "- forbidden knowledge: 'Narcissists hope you never learn this.'\n"
        "- mid-scene: 'They said you're too sensitive. Listen closely.'\n"
        "- prediction: 'You'll hear this phrase right before they vanish.'\n"
        "- uncomfortable truth: 'You miss them because they trained you to.'\n"
        "- Stoic flip: 'Marcus Aurelius would never have replied to that text.'\n"
        "Each line must make a specific claim or name a specific situation. No generic advice, "
        "no preaching.\n"
        "Never start with: 'Stop forgiving', 'You need to', 'Most people', 'Have you ever', "
        "'Did you know', 'Imagine', 'Are you', 'Here's the thing'.\n\n"
        + notes
        + "RECENT VIDEOS - do not repeat or paraphrase these:\n" + recent_block + "\n\n"
        'Return ONLY valid JSON: {"hooks": ["...", "...", ...]}'
    )


def ideate_hooks(fmt, history, teaser, notes):
    data = extract_json(call_gemini(prompt_hooks(fmt, history, teaser, notes), 1.0))
    raw = data.get("hooks") if isinstance(data, dict) else data
    hooks = []
    for h in raw or []:
        h = clean_text(_strip_hashtags(str(h))).strip('" ')
        words = h.split()
        if not (4 <= len(words) <= 12):
            continue
        if any(h.lower().startswith(b) for b in BAD_STARTS):
            continue
        if h not in hooks:
            hooks.append(h)
    if len(hooks) < 3:
        raise ValueError(f"only {len(hooks)} usable hooks")
    return hooks[:8]


# ---- stage B: critic picks the winner ---------------------------------------
def prompt_pick(hooks):
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(hooks))
    return (
        "You are a ruthless YouTube Shorts retention analyst. Score each opening line from 1 "
        "to 10 on:\n"
        "- STOP-POWER: would a bored stranger stop scrolling within 2 seconds?\n"
        "- SPECIFICITY: concrete situation or claim, not generic advice\n"
        "- CURIOSITY GAP: do they NEED the next sentence?\n"
        "- RELATABILITY: does someone dealing with a narcissist instantly recognise their life?\n"
        "- NOVELTY: not a cliche they have seen a thousand times\n"
        "Penalise anything vague, preachy, long-winded or that sounds written by an AI. Be "
        "harsh: most lines deserve 4-6.\n\n"
        f"OPENING LINES:\n{numbered}\n\n"
        'Return ONLY valid JSON: {"scores": [one number per line, same order], "best": <index>}'
    )


def pick_best_hook(hooks):
    data = extract_json(call_gemini(prompt_pick(hooks), 0.2))
    scores = data.get("scores") if isinstance(data, dict) else None
    best = None
    if isinstance(scores, list) and len(scores) == len(hooks):
        try:
            scores = [float(s) for s in scores]
            best = max(range(len(hooks)), key=lambda i: scores[i])
            log("[Hooks] scores: " + ", ".join(f"{s:.0f}" for s in scores))
        except (TypeError, ValueError):
            best = None
    if best is None and isinstance(data, dict):
        try:
            best = int(data.get("best"))
        except (TypeError, ValueError):
            best = None
    if best is None or not (0 <= best < len(hooks)):
        best = 0
    return hooks[best]


# ---- stage C: write the full package ----------------------------------------
def prompt_script(fmt, ending, target, hook, history, teaser, notes):
    recent = [(h.get("title") or "").strip() for h in history[-25:]]
    recent_block = "\n".join(f"- {r}" for r in recent if r) or "- (none yet)"
    lo, hi = max(SCRIPT_MIN_WORDS, target - 8), min(SCRIPT_MAX_WORDS, target + 8)
    if hook:
        hook_rule = f'The FIRST sentence must be EXACTLY: "{hook}"'
    else:
        hook_rule = ("The FIRST sentence is the hook (9 words max): name a painfully specific "
                     "situation, promise a number plus a payoff, or contradict a belief the "
                     "viewer holds. It must make them NEED the next sentence.")
    return (
        "You write viral YouTube Shorts for a US audience in the niche of narcissistic "
        "manipulation and Stoic resilience.\n"
        "GOAL: the viewer must not swipe in the first 2 seconds, must watch to the end, and "
        "should want to replay.\n\n"
        f"FORMAT: {fmt['name']} - {fmt['brief']}\n"
        + (f"TOPIC LOCK: deliver on this promise made in the last video: '{teaser}'.\n"
           if teaser else "")
        + "\nRULES FOR THE SPOKEN SCRIPT (field \"script\"):\n"
        f"1. About {target} words (between {lo} and {hi}). English only, plain spoken words, no "
        "emojis, hashtags or stage directions.\n"
        f"2. {hook_rule}\n"
        "3. Short sentences, average 7 words, none over 14. Speak directly to 'you'.\n"
        "4. Structure: hook, then ONE concrete moment (a short scene, exact phrase or specific "
        "example, never abstract advice), then the insight that reframes it, then the ending.\n"
        "5. Keep an open loop: hold the sharpest payoff for the second half. No filler intro, "
        "no 'let me explain', no recap.\n"
        f"6. {ENDINGS[ending]}\n"
        "7. No fake quotes. If you mention a Stoic, paraphrase the idea; never invent words and "
        "attribute them to a real person.\n"
        "8. Never diagnose real people. Never encourage revenge, harassment or self-harm.\n"
        "9. Avoid AI filler: journey, tapestry, unlock, delve, in today's world.\n\n"
        "OTHER FIELDS:\n"
        "- \"on_screen_hook\": 3 to 7 words shown on screen during the first 3 seconds. Must "
        "make sense with the SOUND OFF and be readable in one second. Complement the spoken "
        "hook, do not copy it.\n"
        "- \"title\": max 60 characters, curiosity or a direct challenge, no hashtags, no "
        "quotation marks, different opening words from the recent titles below.\n"
        "- \"description\": 1-2 sentences with natural keywords (narcissist, manipulation, "
        "Stoicism, boundaries). No hashtags.\n"
        "- \"hashtags\": array of 4 single-word lowercase hashtags WITHOUT the # symbol.\n"
        "- \"visual_queries\": array of 6 stock-footage search terms (2-4 plain words) in the "
        "order of the story. Prefer PEOPLE and emotion: person alone at night window, hands "
        "gripping a table, silhouette walking in fog, couple arguing seen from behind, shattered "
        "mirror, storm clouds, chess pieces. Concrete things that exist as stock video, no "
        "abstract words. All different.\n"
        "- \"next_teaser\": 3 to 7 words promising the NEXT video's topic, e.g. 'The three-word "
        "phrase that ends gaslighting'. It must be a different topic from this video.\n\n"
        + notes
        + "RECENT TITLES - do not repeat:\n" + recent_block + "\n\n"
        "Return ONLY valid JSON with exactly these keys: script, on_screen_hook, title, "
        "description, hashtags, visual_queries, next_teaser."
    )


def _same_sentence(a, b):
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    return norm(a) == norm(b)


def normalize_post(data, fmt_name, ending="mic_drop", hook=None):
    script = clean_text(_strip_hashtags(str(data.get("script", ""))))
    script = re.sub(r"(?i)^script:\s*", "", script)

    if hook:
        hook = hook.strip()
        if hook[-1] not in ".!?":
            hook += "."
        if not _same_sentence(first_sentence(script), hook):
            rest = script[len(first_sentence(script)):].strip()
            script = f"{hook} {rest}".strip()
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

    teaser = clean_text(_strip_hashtags(str(data.get("next_teaser", "")))).strip(' ."\'')
    teaser = " ".join(teaser.split()[:7])
    if len(teaser.split()) < 2:
        teaser = ""

    if teaser:
        cta_lines = ["FOLLOW FOR THE NEXT RULE:", teaser.upper()]
    else:
        cta_lines = [CHANNEL_CTA.upper()]

    return {
        "format": fmt_name,
        "ending": ending,
        "script": script,
        "hook": spoken_hook,
        "on_screen_hook": on_screen,
        "title": title,
        "description": clean_text(_strip_hashtags(str(data.get("description", "")))),
        "hashtags": tags[:5],
        "visual_queries": queries[:6],
        "next_teaser": teaser,
        "cta_lines": cta_lines,
    }


def validate_post(post, history, lo=SCRIPT_MIN_WORDS, hi=SCRIPT_MAX_WORDS):
    hard, soft = [], []
    n = len(post["script"].split())
    if n < lo:
        hard.append(f"script too short ({n} words)")
    if n > hi:
        hard.append(f"script too long ({n} words)")
    low = post["script"].lower()
    if any(low.startswith(b) for b in BAD_STARTS):
        soft.append("weak hook opening")
    if len(post["hook"].split()) > 12:
        soft.append("hook too long")
    if any(a in low for a in AI_ISMS):
        soft.append("AI-sounding filler")
    sents = split_sentences(post["script"])
    if sents and max(len(s.split()) for s in sents) > 18:
        soft.append("a sentence is too long")
    if post.get("ending") == "comment" and "comment" not in " ".join(sents[-2:]).lower():
        soft.append("comment ending missing")
    key = " ".join(low.split()[:5])
    for h in history[-40:]:
        if " ".join(str(h.get("script", "")).lower().split()[:5]) == key:
            soft.append("hook repeats a recent video")
            break
    return hard, soft


def pick_format(history):
    recent = [h.get("format") for h in history[-2:]]
    pool = [f for f in FORMATS if f["name"] not in recent] or FORMATS
    perf = format_performance(history)
    in_pool = [(perf[f["name"]], f) for f in pool if f["name"] in perf]
    if len(in_pool) >= 3 and _rng.random() < 0.6:
        top = [f for _, f in sorted(in_pool, key=lambda x: x[0], reverse=True)[:2]]
        log("[Learn] exploiting best-performing formats: " + ", ".join(f["name"] for f in top))
        return _rng.choice(top)
    weights = [FORMAT_PRIOR.get(f["name"], 1.0) for f in pool]
    return _rng.choices(pool, weights=weights)[0]


def generate_post(history):
    fmt = pick_format(history)
    ending = _rng.choices(list(ENDING_WEIGHTS), weights=list(ENDING_WEIGHTS.values()))[0]
    target = _rng.randint(34, 56)
    teaser = pending_teaser(history)
    notes = performance_notes(history)
    log(f"[Script] format={fmt['name']} ending={ending} target={target} words"
        + (f" | delivering teaser: {teaser}" if teaser else ""))

    hook = None
    try:
        hooks = ideate_hooks(fmt, history, teaser, notes)
        hook = pick_best_hook(hooks)
        log(f"[Hooks] winner: {hook}")
    except Exception as e:
        log(f"[Hooks] tournament skipped ({e}); writing without a locked hook")

    best = None
    for attempt in range(1, 4):
        try:
            data = extract_json(call_gemini(
                prompt_script(fmt, ending, target, hook, history, teaser, notes), 0.9))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except Exception as e:
            log(f"[Script] attempt {attempt}: unusable response ({e})")
            continue
        post = normalize_post(data, fmt["name"], ending, hook)
        hard, soft = validate_post(post, history)     # only the absolute 28-70 word range is hard
        if hard:
            log(f"[Script] attempt {attempt}: rejected - {', '.join(hard)}")
            continue
        best = post
        if not soft:
            return post
        log(f"[Script] attempt {attempt}: {', '.join(soft)} - regenerating")
    if best:
        return best
    raise RuntimeError("Gemini did not return a usable script after 3 attempts")


# ============================================================
# 3. STOCK FOOTAGE (Pexels portrait + Pixabay, motion-aware)
# ============================================================
def search_pexels(query):
    if not PEXELS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 20},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"[Pexels] '{query}' -> HTTP {r.status_code}")
            return []
        out = []
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and f.get("link")
                     and int(f.get("height") or 0) >= 1280]
            if not files or float(v.get("duration", 0) or 0) < 4:
                continue
            f = min(files, key=lambda x: abs(int(x["height"]) - 1920))
            out.append({"id": f"px{v['id']}", "url": f["link"], "w": int(f["width"]),
                        "h": int(f["height"]), "dur": float(v["duration"]),
                        "portrait": True, "src": "pexels"})
        return out
    except Exception as e:
        log(f"[Pexels] '{query}' failed: {e}")
        return []


def search_pixabay(query):
    if not PIXABAY_API_KEY:
        return []
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 30, "safesearch": "true",
                    "video_type": "film", "min_height": 720},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"[Pixabay] '{query}' -> HTTP {r.status_code}")
            return []
        out = []
        for hit in r.json().get("hits", []):
            if float(hit.get("duration", 0) or 0) < 4:
                continue
            vids = hit.get("videos", {}) or {}
            for key in ("large", "medium", "small"):          # sharpest first
                v = vids.get(key)
                size = int((v or {}).get("size", 0) or 0)
                if v and v.get("url") and 0 < size < 80_000_000:
                    w, h = int(v.get("width", 0) or 0), int(v.get("height", 0) or 0)
                    out.append({"id": f"pb{hit['id']}", "url": v["url"], "w": w, "h": h,
                                "dur": float(hit["duration"]), "portrait": h > w,
                                "src": "pixabay"})
                    break
        return out
    except Exception as e:
        log(f"[Pixabay] '{query}' failed: {e}")
        return []


def rank_candidates(cands):
    portrait = [c for c in cands if c["portrait"]]
    landscape = [c for c in cands if not c["portrait"]]
    _rng.shuffle(portrait)
    _rng.shuffle(landscape)
    return portrait + landscape


def download_file(url, dest):
    with requests.get(url, timeout=90, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    if Path(dest).stat().st_size < 100_000:
        raise RuntimeError("download too small")


def motion_profile(path):
    """Mean frame-to-frame change per sample (6/s) over the visible 9:16 area."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-t", "40", "-i", str(path),
           "-vf", f"fps={PROFILE_FPS},{COVER_CROP},scale=72:128,format=gray",
           "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True).stdout
    fsz = 72 * 128
    n = len(out) // fsz
    if n < 4:
        return None
    frames = np.frombuffer(out[: n * fsz], np.uint8).reshape(n, 128, 72).astype(np.int16)
    prof = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)).astype(np.float64)
    return np.minimum(prof, np.percentile(prof, 90))    # ignore one-off scene-cut spikes


def fetch_clips(queries, want):
    clips, used = [], set()
    candidates = list(queries) + _rng.sample(FALLBACK_QUERIES, k=len(FALLBACK_QUERIES))
    for q in candidates:
        if len(clips) >= want:
            break
        pool = rank_candidates(search_pexels(q) + search_pixabay(q))
        for c in pool[:6]:
            if c["id"] in used:
                continue
            dest = WORK_DIR / f"clip_{len(clips)}.mp4"
            try:
                download_file(c["url"], dest)
                w, h, dur = probe(dest)
                if w <= 0 or dur < 3:
                    raise RuntimeError(f"unusable file ({w}x{h}, {dur:.1f}s)")
            except Exception as e:
                log(f"[Stock] download failed ({e})")
                continue
            used.add(c["id"])
            clips.append({"path": str(dest), "w": w, "h": h, "dur": dur, "id": c["id"],
                          "src": c["src"], "profile": motion_profile(dest)})
            log(f"[Stock] clip {len(clips)}/{want}: '{q}' via {c['src']} ({w}x{h}, {dur:.0f}s)")
            break
    if not clips:
        raise RuntimeError("Could not download any usable stock clip")
    if len(clips) < 3:
        log(f"[Stock] WARNING: only {len(clips)} clip(s); cuts will reuse them")
    return clips


# ============================================================
# 4. VOICEOVER + WORD TIMINGS
# ============================================================
async def _tts_once(text, voice, out_path):
    kwargs = {"rate": VOICE_RATE, "pitch": VOICE_PITCH}
    try:
        # edge-tts >= 7 only sends word timings if you ask for them
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


def decode_pcm(path, sr=16000):
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("could not decode voiceover")
    return np.frombuffer(r.stdout, np.float32), sr


def speech_bounds(pcm, sr, thr=0.015, max_trim=0.6):
    """(start, end) in seconds of the actual speech, so word 1 lands on frame 0."""
    block = max(1, sr // 100)
    n = len(pcm) // block
    if n == 0:
        return 0.0, len(pcm) / sr
    bm = np.abs(pcm[: n * block]).reshape(n, block).max(axis=1)
    idx = np.where(bm > thr)[0]
    if len(idx) == 0:
        return 0.0, len(pcm) / sr
    start = min(max(idx[0] * block / sr - 0.04, 0.0), max_trim)
    end = (idx[-1] + 1) * block / sr
    return float(start), float(end)


def _syllables(w):
    w = re.sub(r"[^a-z]", "", w.lower())
    if not w:
        return 1
    n = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def _word_weight(w):
    wt = 1.0 + 2.0 * _syllables(w)
    if w and w[-1] in ",;:":
        wt += 2.5
    if w and w[-1] in ".!?":
        wt += 4.5
    return wt


def spread_words(words, start, end):
    weights = [_word_weight(w) for w in words]
    total_w = float(sum(weights)) or 1.0
    out, t = [], start
    for w, wt in zip(words, weights):
        dur = (end - start) * wt / total_w
        out.append({"word": w, "start": t, "end": t + dur})
        t += dur
    return out


def build_word_timings(script, word_events, sentence_events, speech_start, speech_end):
    """Best available: real word events > sentence events > syllable-weighted estimate."""
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
    return spread_words(script.split(), speech_start, speech_end)


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
# 5. SYNTHESISED SOUNDTRACK (impact on frame 0, whoosh on cuts)
# ============================================================
def synth_soundtrack(total, cut_times, out_path, seed=None):
    rng = np.random.default_rng(seed)
    sr = 44100
    length = total + 0.6
    n = int(length * sr)
    t = np.arange(n) / sr

    root = float(rng.choice([55.0, 58.27, 61.74, 65.41, 73.42]))     # dark minor drone
    ratios = [1.0, 1.189, 1.498, 2.0, 2.378]
    pad = np.zeros(n)
    for i, r in enumerate(ratios):
        lfo = 0.65 + 0.35 * np.sin(2 * np.pi * (0.06 + 0.025 * i) * t + rng.uniform(0, 2 * np.pi))
        for det in (-0.35, 0.35):
            pad += np.sin(2 * np.pi * (root * r + det) * t + rng.uniform(0, 2 * np.pi)) * lfo / (1 + 0.6 * i)
    pad /= (np.max(np.abs(pad)) + 1e-9)
    pad *= np.minimum(1.0, t / 0.05)
    pad *= np.clip((length - t) / 0.6, 0, 1)
    out = pad * 0.25

    def place(sig, at):
        i0 = int(at * sr)
        if i0 < 0:
            sig, i0 = sig[-i0:], 0
        if i0 >= n:
            return
        seg = sig[: n - i0]
        out[i0:i0 + len(seg)] += seg

    bt = np.arange(int(1.0 * sr)) / sr                                # impact on frame 0
    phase = 2 * np.pi * (38 * bt + 60 * (1 - np.exp(-14 * bt)) / 14)
    place(np.sin(phase) * np.exp(-4.0 * bt) + rng.standard_normal(len(bt)) * np.exp(-bt * 60) * 0.4, 0.0)

    period = 60.0 / rng.uniform(58, 72)                               # slow heartbeat
    ht = np.arange(int(0.25 * sr)) / sr
    thump = np.sin(2 * np.pi * 52 * ht) * np.exp(-ht * 22)
    beat = 1.0
    while beat < length:
        place(thump * 0.30, beat)
        place(thump * 0.18, beat + 0.17)
        beat += period

    for c in cut_times:                                               # whoosh into every cut
        m = int(0.32 * sr)
        low = np.convolve(rng.standard_normal(m), np.ones(40) / 40, mode="same")
        place(low * np.sin(np.linspace(0, np.pi, m)) ** 2 * 2.0, c - 0.20)

    out = out / (np.max(np.abs(out)) + 1e-9) * 0.40
    stereo = np.stack([out, np.roll(out, 31)], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return str(out_path)


def mix_audio(voice_path, lead, music_path, total, out_path):
    """Voice on top, music ducked under it, -14 LUFS. Falls back to simpler graphs if needed."""
    st = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
    voice = (f"[0:a]atrim=start={lead:.3f},asetpts=PTS-STARTPTS,{st},highpass=f=70,"
             "acompressor=threshold=0.1:ratio=3:attack=5:release=80:makeup=2")
    tail = f"atrim=end={total:.3f},asetpts=PTS-STARTPTS"
    variants = [
        ("ducked + loudness-normalised",
         f"{voice},asplit=2[v][vs];[1:a]{st}[m];"
         "[m][vs]sidechaincompress=threshold=0.03:ratio=8:attack=10:release=300[md];"
         "[v][md]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[mx];"
         f"[mx]loudnorm=I=-14:TP=-1.5:LRA=9,aresample=44100,{tail}[out]"),
        ("plain mix",
         f"{voice}[v];[1:a]{st},volume=0.22[m];"
         f"[v][m]amix=inputs=2:duration=longest:dropout_transition=0,volume=2,{tail}[out]"),
        ("voice only",
         f"[0:a]atrim=start={lead:.3f},asetpts=PTS-STARTPTS,{st},{tail}[out]"),
    ]
    for name, graph in variants:
        try:
            run_ffmpeg(["-i", voice_path, "-i", music_path, "-filter_complex", graph,
                        "-map", "[out]", "-ar", "44100", "-ac", "2", out_path])
            if Path(out_path).exists() and Path(out_path).stat().st_size > 1000:
                log(f"[Audio] mix: {name}")
                return name
        except Exception as e:
            log(f"[Audio] '{name}' failed: {str(e)[-200:]}")
    raise RuntimeError("audio mixing failed")


# ============================================================
# 6. TEXT OVERLAYS
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
    "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/anton/Anton-Regular.ttf",
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
            log(f"[Font] Anton download failed ({str(e)[:80]})")
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


def render_cta(lines, font_path, theme):
    lines = [l.upper() for l in lines if l]
    pad_x, pad_y, gap = 36, 14, 4
    size = 42
    while True:
        font = load_font(font_path, size)
        widths = [font.getlength(l) for l in lines]
        if max(widths) + 2 * pad_x <= W * 0.9 or size <= 24:
            break
        size -= 2
    asc, desc = font.getmetrics()
    line_h = asc + desc
    box_w = int(max(widths) + 2 * pad_x)
    box_h = line_h * len(lines) + gap * (len(lines) - 1) + 2 * pad_y
    img = Image.new("RGBA", (W, box_h + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (W - box_w) // 2
    d.rounded_rectangle([x0, 0, x0 + box_w, box_h], radius=min(44, box_h // 2),
                        fill=(0, 0, 0, 185), outline=theme["hook_bg"], width=4)
    r, g, b = theme["hook_bg"]
    bright = 0.299 * r + 0.587 * g + 0.114 * b > 110        # dark accents (red) are hard to read on black
    y = pad_y
    for i, (ln, lw) in enumerate(zip(lines, widths)):
        accent = bright and len(lines) > 1 and i == len(lines) - 1
        d.text(((W - lw) / 2, y), ln, font=font,
               fill=theme["hook_bg"] if accent else (255, 255, 255))
        y += line_h + gap
    return img


def build_shade():
    """Darkening + vignette + cool tint as one multiply layer."""
    y = np.linspace(-1, 1, H)[:, None]
    x = np.linspace(-1, 1, W)[None, :]
    r = np.sqrt(x ** 2 + (y ** 2) * 0.6)
    vign = 1.0 - 0.45 * np.clip(r - 0.35, 0, 1) ** 1.4
    base = np.clip(0.70 * vign, 0.22, 1.0)
    rgb = np.dstack([base * 0.94, base * 0.98, base * 1.0])
    return Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))


# ============================================================
# 7. EDIT PLAN (cuts + which part of which clip)
# ============================================================
def plan_spans(total, timings, rng):
    """Fast cuts early (1.6-2.2 s), slower later (2.3-3.1 s), snapped to word starts."""
    word_starts = [w["start"] for w in timings]
    starts, t, i = [0.0], 0.0, 0
    while True:
        seg = rng.uniform(1.6, 2.2) if i < 2 else rng.uniform(2.3, 3.1)
        nxt = t + seg
        if total - nxt < 1.4:
            break
        snapped = min(word_starts, key=lambda s: abs(s - nxt)) if word_starts else nxt
        if abs(snapped - nxt) > 0.45 or snapped - t < 1.3:
            snapped = nxt
        starts.append(snapped)
        t, i = snapped, i + 1
    ends = starts[1:] + [total]
    return list(zip(starts, ends))


def _overlaps(a0, a1, taken):
    return any(not (a1 <= b0 or a0 >= b1) for b0, b1 in taken)


def best_window(clip, seg_len, taken):
    """(offset, score): the most dynamic free stretch of `clip` that is `seg_len` long."""
    max_start = clip["dur"] - seg_len - 0.15
    if max_start <= 0:
        return 0.0, 0.0                                   # clip shorter than the cut: loop it
    prof = clip.get("profile")
    if prof is None or len(prof) < 3:
        for _ in range(12):
            s = _rng.uniform(0, max_start)
            if not _overlaps(s, s + seg_len, taken):
                return s, 0.0
        return _rng.uniform(0, max_start), 0.0
    w = max(1, int(round(seg_len * PROFILE_FPS)))
    cum = np.concatenate([[0.0], np.cumsum(prof)])
    best_score, best_s = -1.0, 0.0
    for i in range(len(prof)):
        s = i / PROFILE_FPS
        if s > max_start:
            break
        if _overlaps(s, s + seg_len, taken):
            continue
        j = min(i + w, len(prof))
        score = float(cum[j] - cum[i]) / max(1, j - i)
        if score > best_score:
            best_score, best_s = score, s
    if best_score < 0:
        return _rng.uniform(0, max_start), 0.0
    return best_s, best_score


def assign_segments(spans, clips, rng):
    n = len(clips)
    taken = {i: [] for i in range(n)}
    order = list(range(n))
    rng.shuffle(order)
    rr, prev, segs = 0, None, []
    for i, (s, e) in enumerate(spans):
        seg_len = e - s
        if i == 0:      # the opening shot = most dynamic moment among ALL clips
            cands = []
            for k, c in enumerate(clips):
                off_k, score_k = best_window(c, seg_len, taken[k])
                cands.append((score_k, k, off_k))
            score, ci, off = max(cands)
        else:
            ci = order[rr % n]
            rr += 1
            if n > 1 and ci == prev:
                ci = order[rr % n]
                rr += 1
            off, score = best_window(clips[ci], seg_len, taken[ci])
        taken[ci].append((off, off + seg_len))
        prev = ci
        zoom_in = i % 2 == 0
        segs.append({
            "start": s, "end": e, "clip": ci, "offset": off, "score": score,
            "loop": off + seg_len + 0.15 > clips[ci]["dur"],
            "z0": 1.0 if zoom_in else 1.20, "z1": 1.20 if zoom_in else 1.03,
            "pan": rng.uniform(-0.04, 0.04),
        })
    return segs


# ============================================================
# 8. RENDER (frames -> ffmpeg)
# ============================================================
def zoom_box(z, cx_frac):
    cw, ch = W / z, H / z
    x0 = min(max(cx_frac * W - cw / 2, 0), W - cw)
    y0 = min(max(0.5 * H - ch / 2, 0), H - ch)
    return (x0, y0, x0 + cw, y0 + ch)


def segment_frames(clip, seg, n_frames):
    """Yield n_frames HxWx3 uint8 frames (already centre-cropped to 9:16 and scaled)."""
    fsz = W * H * 3
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if seg["loop"]:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-ss", f"{0.0 if seg['loop'] and seg['offset'] <= 0 else seg['offset']:.3f}",
            "-i", clip["path"],
            "-vf", f"{COVER_CROP},scale={W}:{H}:flags=lanczos,fps={FPS},format=rgb24",
            "-frames:v", str(n_frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=fsz * 2)
    last = None
    try:
        for _ in range(n_frames):
            buf = proc.stdout.read(fsz)
            if len(buf) == fsz:
                last = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            elif last is None:
                raise RuntimeError("could not decode frames from stock clip")
            yield last                                    # short clip: hold the last frame
    finally:
        try:
            proc.stdout.close()
        finally:
            proc.kill()
            proc.wait()


def build_composer(timings, post, theme, font_path, total):
    shade = build_shade()
    hook_img = render_hook(post["on_screen_hook"], font_path, theme)
    hook_y = int(H * 0.12)
    cap_track = []
    for group in group_words(timings):
        for ai, w in enumerate(group):
            cap_track.append((w["start"], render_caption(group, ai, font_path, theme)))
    cap_starts = [c[0] for c in cap_track]
    cap_cy = int(H * 0.52)
    cta_img = render_cta(post["cta_lines"], font_path, theme)
    cta_y = int(H * 0.64)                                 # above YouTube's bottom UI zone
    cta_t0 = max(HOOK_SECONDS + 1.0, total * 0.60)
    cta_t1 = min(total - 0.3, cta_t0 + 3.0)
    white = Image.new("RGB", (W, H), (255, 255, 255))
    pop_cache = {}

    def popped(img, key, scale):
        k = (key, scale)
        if k not in pop_cache:
            pop_cache[k] = img.resize((max(1, int(img.width * scale)),
                                       max(1, int(img.height * scale))), Image.BILINEAR)
        return pop_cache[k]

    def compose(base, t, seg, seg_idx):
        seg_len = max(seg["end"] - seg["start"], 0.1)
        lt = t - seg["start"]
        p = min(max(lt / seg_len, 0.0), 1.0)
        z = seg["z0"] + (seg["z1"] - seg["z0"]) * p
        if seg_idx == 0 and t < 0.30:                     # snap-zoom: motion on frame 0
            z *= 1 + 0.26 * (1 - t / 0.30) ** 2
        img = Image.fromarray(base)
        if z > 1.001:
            img = img.resize((W, H), Image.Resampling.BILINEAR,
                             box=zoom_box(z, 0.5 + seg["pan"] * (p - 0.5) * 2))
        img = ImageChops.multiply(img, shade)
        if seg_idx > 0 and lt < 0.13:                     # quick flash on every cut
            img = Image.blend(img, white, 0.20 * (1 - lt / 0.13))

        if t < HOOK_SECONDS:                              # on screen from frame 0
            hk = hook_img if t >= 0.07 else popped(hook_img, "hook", 0.93)
            img.paste(hk, ((W - hk.width) // 2, hook_y + (hook_img.height - hk.height) // 2), hk)

        ci = max(bisect.bisect_right(cap_starts, t) - 1, 0)
        cap = cap_track[ci][1]
        age = t - cap_starts[ci]
        if age < 0.07:                                    # pop-in for each new word
            cap = popped(cap, ci, 0.86 if age < 0.034 else 0.94)
        img.paste(cap, ((W - cap.width) // 2, cap_cy - cap.height // 2), cap)

        if cta_t0 <= t < cta_t1:
            img.paste(cta_img, (0, cta_y), cta_img)
        return img

    return compose


def pick_encoder_vf():
    """bt709 colour tagging so colours match what players expect; plain fallback if unsupported."""
    candidates = ["scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "format=yuv420p"]
    for vf in candidates:
        try:
            run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-vf", vf,
                        "-c:v", "libx264", "-f", "null", "-"])
            return vf
        except Exception:
            continue
    return "format=yuv420p"


def render_video(segments, clips, compose, total, mix_path, output_path):
    total_frames = int(round(total * FPS))
    vf = pick_encoder_vf()
    colour_tags = (["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
                   if "bt709" in vf else [])
    log_path = WORK_DIR / "encoder.log"
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(mix_path), "-map", "0:v", "-map", "1:a",
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-profile:v", "high", *colour_tags, "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
           "-t", f"{total:.3f}", str(output_path)]
    with open(log_path, "wb") as logf:
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=logf)
        try:
            for si, seg in enumerate(segments):
                f0 = int(round(seg["start"] * FPS))
                f1 = total_frames if si == len(segments) - 1 else int(round(seg["end"] * FPS))
                n = max(1, f1 - f0)
                for j, base in enumerate(segment_frames(clips[seg["clip"]], seg, n)):
                    t = (f0 + j) / FPS
                    enc.stdin.write(compose(base, t, seg, si).tobytes())
            enc.stdin.close()
            rc = enc.wait()
        except BrokenPipeError:
            rc = enc.wait()
        except Exception:
            enc.kill()
            enc.wait()
            raise
    if rc != 0 or not Path(output_path).exists():
        tail = Path(log_path).read_text(errors="ignore")[-600:]
        raise RuntimeError(f"encoder failed (code {rc}): {tail}")


def create_video(post, output_path="final_output.mp4"):
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(exist_ok=True)
    theme = _rng.choice(THEMES)
    voice_name = _rng.choice(VOICES)
    font_path = get_font_path()
    script = post["script"]
    log(f"[Video] voice={voice_name} theme={theme['name']}")

    voice_path = str(WORK_DIR / "voiceover.mp3")
    word_events, sent_events = asyncio.run(generate_voiceover(script, voice_name, voice_path))
    pcm, sr = decode_pcm(voice_path)
    lead, speech_end = speech_bounds(pcm, sr)
    total = max(4.0, speech_end - lead + 0.25)             # tight tail = clean loop
    raw = build_word_timings(script, word_events, sent_events, lead, speech_end)
    timings = finalize_timings(raw, lead, total)
    log(f"[Video] duration={total:.1f}s, trimmed {lead:.2f}s lead-in, {len(timings)} caption words")

    spans = plan_spans(total, timings, _rng)
    clips = fetch_clips(post["visual_queries"], want=min(6, len(spans)))
    segments = assign_segments(spans, clips, _rng)
    log(f"[Video] {len(segments)} cuts over {len(clips)} clips; opening shot motion score "
        f"{segments[0]['score']:.1f}")

    music_path = synth_soundtrack(total, [s[0] for s in spans[1:]], WORK_DIR / "soundtrack.wav")
    mix_path = WORK_DIR / "mix.wav"
    mix_audio(voice_path, lead, music_path, total, mix_path)

    compose = build_composer(timings, post, theme, font_path, total)
    log("[Video] Rendering 1080x1920 ...")
    t0 = time.time()
    render_video(segments, clips, compose, total, mix_path, output_path)
    log(f"[Video] rendered in {time.time() - t0:.0f}s")
    return output_path, {"voice": voice_name, "theme": theme["name"], "duration": round(total, 1),
                         "clips": [c["id"] for c in clips]}


# ============================================================
# 9. YOUTUBE (auth, stats, upload)
# ============================================================
BASE_TAGS = [
    "stoicism", "narcissist", "narcissism", "mindset", "shorts", "dark psychology",
    "emotional control", "toxic relationship", "stoic wisdom", "self improvement",
]


def load_monetization():
    try:
        data = json.loads(MONETIZATION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def clean_url(u):
    u = (u or "").strip() if isinstance(u, str) else ""
    return u if u.startswith(("http://", "https://")) else ""


def monetization_block(cfg, funnel_url=""):
    """Funnel link + free guide + affiliate links (with disclosure). Empty URLs are skipped."""
    lines = []
    if funnel_url:
        lines.append(f"Full breakdown: {funnel_url}")
    g = cfg.get("free_guide") or {}
    if clean_url(g.get("url")):
        lines.append(f"FREE GUIDE - {g.get('title') or 'Free guide'}: {g['url'].strip()}")
    aff = [a for a in (cfg.get("affiliate_links") or [])
           if isinstance(a, dict) and a.get("label") and clean_url(a.get("url"))]
    if aff:
        lines.append(cfg.get("affiliate_disclosure") or
                     "Some links below are affiliate links. If you buy through them I may earn "
                     "a commission at no extra cost to you.")
        lines += [f"- {a['label']}: {a['url'].strip()}" for a in aff[:4]]
    return "\n".join(lines)


def find_funnel_url(youtube, cfg):
    """Latest PUBLIC/unlisted long-form video made by longform.py (or a manual override)."""
    manual = clean_url(cfg.get("funnel_url"))
    if manual:
        return manual
    try:
        entries = json.loads(LONGFORM_FILE.read_text(encoding="utf-8"))
        entries = [e for e in entries if isinstance(e, dict) and e.get("video_id")][-5:]
    except Exception:
        return ""
    for e in reversed(entries):
        if e.get("privacy") in ("public", "unlisted"):
            return f"https://youtu.be/{e['video_id']}"
    if youtube and entries:          # uploaded private -> has it been published since? (needs read access)
        try:
            resp = youtube.videos().list(
                part="status", id=",".join(e["video_id"] for e in entries)).execute()
            live = {it["id"] for it in resp.get("items", [])
                    if it.get("status", {}).get("privacyStatus") in ("public", "unlisted")}
            for e in reversed(entries):
                if e["video_id"] in live:
                    return f"https://youtu.be/{e['video_id']}"
        except Exception as ex:
            log(f"[Funnel] could not check long-form status ({str(ex)[:80]}); "
                "set funnel_url in monetization.json to link it manually")
    return ""


def build_description(post, cfg=None, funnel_url=""):
    ordered = list(post["hashtags"])[:4]
    if "shorts" not in ordered:
        ordered.append("shorts")
    hashtags = " ".join("#" + h for h in ordered)
    parts = [post["description"],
             f"Next: {post['next_teaser']}" if post.get("next_teaser") else "",
             monetization_block(cfg or {}, funnel_url),
             f"{CHANNEL_CTA}.", hashtags]
    return "\n\n".join(p for p in parts if p).replace("<", "").replace(">", "")[:4800]


def build_tags(post):
    tags, size = [], 0
    for t in list(post["hashtags"]) + BASE_TAGS:
        if t not in tags and size + len(t) + 1 <= 450:
            tags.append(t)
            size += len(t) + 1
    return tags


def get_youtube():
    if not YOUTUBE_CREDENTIALS_JSON:
        raise RuntimeError("YOUTUBE_CREDENTIALS secret is missing")
    creds_dict = json.loads(YOUTUBE_CREDENTIALS_JSON)
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=creds_dict["refresh_token"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def refresh_stats(youtube, history):
    """Attach views/likes/comments to past videos. Needs a token with read access; skips if not."""
    ids = [h["video_id"] for h in history if h.get("video_id")][-50:]
    if not ids:
        return 0
    try:
        resp = youtube.videos().list(part="statistics", id=",".join(ids)).execute()
    except HttpError as e:
        log(f"[Stats] skipped ({e.resp.status}): this token cannot read video stats. To enable "
            "the learning loop, re-authorise with get_token.py (adds youtube.readonly).")
        return 0
    except Exception as e:
        log(f"[Stats] skipped: {str(e)[:120]}")
        return 0
    by_id = {it["id"]: it.get("statistics", {}) for it in resp.get("items", [])}
    n = 0
    for h in history:
        st = by_id.get(h.get("video_id"))
        if st is not None:
            h["views"] = int(st.get("viewCount", 0))
            h["likes"] = int(st.get("likeCount", 0))
            h["comments"] = int(st.get("commentCount", 0))
            n += 1
    log(f"[Stats] updated {n} videos")
    perf = format_performance(history)
    if perf:
        log("[Stats] median views by format: " + ", ".join(
            f"{k}={v:.0f}" for k, v in sorted(perf.items(), key=lambda x: -x[1])))
    return n


def upload_to_youtube(youtube, video_file_path, title, description, tags):
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
    ffmpeg_bin()                                           # fail fast if ffmpeg is missing
    history = load_history()

    youtube = None
    if YOUTUBE_CREDENTIALS_JSON:
        try:
            youtube = get_youtube()
        except Exception as e:
            if not DRY_RUN:
                raise
            log(f"[YouTube] auth failed (dry run continues): {e}")
    elif not DRY_RUN:
        raise RuntimeError("YOUTUBE_CREDENTIALS secret is missing")
    if youtube:
        refresh_stats(youtube, history)

    cfg = load_monetization()
    funnel_url = find_funnel_url(youtube, cfg)
    log(f"[Funnel] long-form link: {funnel_url or '(none yet)'}")

    log("=== 1) Generating script ===")
    post = generate_post(history)
    if funnel_url and _rng.random() < 0.35:      # on-screen nudge on roughly 1 in 3 Shorts
        post["cta_lines"] = ["WATCH THE FULL BREAKDOWN", "LINK IN DESCRIPTION"]
    log(f"Format : {post['format']} / ending: {post['ending']}")
    log(f"Title  : {post['title']}")
    log(f"Banner : {post['on_screen_hook']}")
    log(f"Script : {post['script']}")
    log(f"Next   : {post['next_teaser'] or '(none)'}")

    log("=== 2) Creating video ===")
    video_path, meta = create_video(post)

    if DRY_RUN:
        log(f"=== DRY RUN: video saved to {video_path}, skipping upload and history ===")
        return

    log("=== 3) Uploading to YouTube ===")
    video_id = upload_to_youtube(
        youtube, video_path, post["title"], build_description(post, cfg, funnel_url),
        build_tags(post))
    save_history(history, {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "video_id": video_id,
        "format": post["format"],
        "ending": post["ending"],
        "title": post["title"],
        "hook": post["hook"],
        "script": post["script"],
        "next_teaser": post["next_teaser"],
        "funnel": bool(funnel_url),
        **meta,
    })
    log("=== DONE ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise
