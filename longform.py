"""
Long-form pipeline: one 6-8 minute 1080p video per run.

WHY: YouTube's Partner Program counts public LONG-FORM watch hours; Shorts watch time does not
count towards them. This script turns the topics that already win on your Shorts into
searchable, chaptered explainers that can accumulate those hours.

Run from the repo root (next to main.py):  python longform.py

  TOPIC      picked from your best Shorts + live YouTube search suggestions; a title
             tournament (6 candidates, critic picks one) like the hook tournament in main.py.
  SCRIPT     outline -> one Gemini call per chapter (so chapters do not blur together). Structure
             rotates (mechanism+scripts / myth-busting / case file / Stoic lens / warning signs).
             Statistics, "studies show" and expert claims are stripped: this is advice content,
             invented facts would hurt viewers and the channel.
  VOICE      Kokoro-82M (Apache-2.0, open source) when installed, otherwise Edge TTS.
  PICTURE    landscape stock footage (Pexels + Pixabay), measured motion so static shots are
             avoided, slow push-ins, phrase subtitles, chapter cards, dip-to-black between chapters.
  PACKAGE    thumbnail (1280x720), chapter timestamps in the description, free-guide and
             affiliate block from monetization.json, safety note.
  UPLOAD     PRIVATE by default (LONGFORM_PRIVACY): review it in Studio, add your own take if you
             like, then publish. Automated, unreviewed long-form is exactly what YouTube's
             "inauthentic content" rules target; a 30-second human check is the cheap insurance.

Environment (all optional except the first three, which main.py already uses)
  GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS
  PEXELS_API_KEY      better landscape footage
  DRY_RUN=true        render only, no upload
  LONGFORM_PRIVACY    private (default) | unlisted | public
  TOPIC               force a topic (otherwise chosen automatically)
  MY_TAKE             1-3 sentences of YOUR opinion/story; woven into the script as the point of view
  TTS_ENGINE          auto (default) | kokoro | edge
  KOKORO_VOICE        default am_michael
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
import subprocess
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageChops

import main as core

# ============================================================
# CONFIG
# ============================================================
LW, LH, LFPS = 1920, 1080, 30
WORK = Path("work_long")
LONGFORM_FILE = core.LONGFORM_FILE
DRY_RUN = core.DRY_RUN
PRIVACY = os.environ.get("LONGFORM_PRIVACY", "private").strip().lower()
if PRIVACY not in ("private", "unlisted", "public"):
    PRIVACY = "private"
TOPIC = os.environ.get("TOPIC", "").strip()
MY_TAKE = os.environ.get("MY_TAKE", "").strip()
TTS_ENGINE = os.environ.get("TTS_ENGINE", "auto").strip().lower()
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "am_michael").strip()
EDGE_VOICE = os.environ.get("LONG_EDGE_VOICE", "en-US-AndrewNeural").strip()
EDGE_RATE = "+2%"
CHAPTER_GAP = 0.35
WIDE_CROP = "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)'"

SEED_QUERIES = [
    "how to deal with a narcissist", "signs of emotional manipulation",
    "gaslighting how to respond", "how to set boundaries with a narcissist",
    "stoicism toxic people", "narcissist silent treatment what to do",
    "how to stop explaining yourself", "guilt tripping how to respond",
]
STRUCTURES = [
    {"name": "mechanism_scripts",
     "brief": "Explain how the tactic works (the mechanism), then 4-5 exact scripts to respond, "
              "then the mistakes that make it worse, then a simple 7-day practice plan."},
    {"name": "myth_busting",
     "brief": "Bust common myths about handling manipulative people. Each chapter: the myth, "
              "why it feels true, what actually happens, and the better move with exact words."},
    {"name": "case_file",
     "brief": "Walk through ONE realistic conversation (clearly a made-up composite, not a real "
              "person) line by line: what they said, what it was really doing, what to say instead."},
    {"name": "stoic_lens",
     "brief": "Each chapter applies one Stoic idea (paraphrased, never an invented quote) to a "
              "specific moment with a manipulative person, with a practical exercise."},
    {"name": "warning_signs",
     "brief": "The escalating stages of a manipulative dynamic, the signs at each stage, the right "
              "response at each stage, ending on how to decide whether to stay or step back."},
]
LONG_FALLBACK_QUERIES = [
    "storm clouds timelapse", "city night rain", "person alone window night", "silhouette walking fog",
    "chess game", "candle flame dark", "clock ticking", "empty street night", "mountain fog",
    "ocean waves dark", "hands on table", "man looking out window",
]
BASE_TAGS = ["narcissist", "narcissistic abuse", "gaslighting", "emotional manipulation",
             "boundaries", "stoicism", "stoic wisdom", "toxic relationships", "self improvement"]
SAFETY_NOTE = (
    "If you are in danger or being abused, you are not alone. In the US call 911 in an emergency "
    "or the National Domestic Violence Hotline at 1-800-799-7233 (thehotline.org). This channel is "
    "educational and is not a substitute for therapy, medical or legal advice."
)

_rng = random.Random()
_KOKORO = None
_clip_counter = [0]


def log(msg):
    core.log(msg)


# ============================================================
# 1. HISTORY
# ============================================================
def load_long_history():
    try:
        data = json.loads(LONGFORM_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_long_history(history, entry):
    history.append(entry)
    LONGFORM_FILE.write_text(json.dumps(history[-100:], indent=2, ensure_ascii=False),
                             encoding="utf-8")


# ============================================================
# 2. TOPIC + SCRIPT
# ============================================================
def youtube_suggest(seed):
    """What people actually type into YouTube search (unofficial autocomplete endpoint)."""
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search",
                         params={"client": "firefox", "ds": "yt", "q": seed, "hl": "en", "gl": "us"},
                         timeout=10)
        data = r.json()
        return [s for s in data[1] if isinstance(s, str)][:10]
    except Exception as e:
        log(f"[Topic] suggestions for '{seed}' unavailable ({str(e)[:60]})")
        return []


def collect_suggestions():
    out = []
    for seed in _rng.sample(SEED_QUERIES, k=3):
        for s in youtube_suggest(seed):
            if s not in out:
                out.append(s)
    return out[:25]


def prompt_titles(suggestions, recent, topic_hint):
    sug = "\n".join(f"- {s}" for s in suggestions) or "- (none available)"
    rec = "\n".join(f"- {r}" for r in recent if r) or "- (none yet)"
    win = "\n".join(f"- {t}" for t in core.WINNING_SHORTS)
    return (
        "You plan long-form YouTube videos (6-8 minutes) for a channel about narcissistic "
        "manipulation, emotional boundaries and Stoic resilience (US audience). Long-form is "
        "judged on watch time, so every title must promise ONE specific, useful outcome that a "
        "person would actually search for.\n\n"
        "This channel's most-viewed Shorts (what its audience responds to):\n" + win + "\n\n"
        "Real YouTube search suggestions (what people type):\n" + sug + "\n\n"
        + (f"REQUIRED TOPIC from the channel owner: {topic_hint}\n\n" if topic_hint else "")
        + "Write 6 DIFFERENT video titles, 45-70 characters each. Put the main search phrase near "
        "the front. Promise a concrete outcome (exact scripts, signs, a method). Numbers are fine. "
        "No all-caps words, no emojis, no hashtags, no false promises such as 'guaranteed', and "
        "never diagnose anyone.\n"
        "Do not repeat or paraphrase these recent long-form titles:\n" + rec + "\n\n"
        'Return ONLY valid JSON: {"titles": ["...", "..."]}'
    )


def ideate_titles(suggestions, recent, topic_hint):
    data = core.extract_json(core.call_gemini(prompt_titles(suggestions, recent, topic_hint), 1.0))
    raw = data.get("titles") if isinstance(data, dict) else data
    titles = []
    recent_keys = {" ".join(r.lower().split()[:4]) for r in recent if r}
    for t in raw or []:
        t = core.clean_text(core._strip_hashtags(str(t))).strip('" ')
        if not (25 <= len(t) <= 90):
            continue
        if " ".join(t.lower().split()[:4]) in recent_keys or t in titles:
            continue
        titles.append(t)
    if len(titles) < 2:
        raise ValueError(f"only {len(titles)} usable titles")
    return titles[:6]


def pick_best_title(titles):
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    prompt = (
        "You are a YouTube strategist. Score each long-form title from 1 to 10 on: SEARCH DEMAND "
        "(would people type this?), CLARITY OF PROMISE, CURIOSITY, FIT with a channel about "
        "manipulation, boundaries and Stoicism, and HONESTY (not clickbait). Be harsh.\n\n"
        f"TITLES:\n{numbered}\n\n"
        'Return ONLY valid JSON: {"scores": [one number per title, same order], "best": <index>}'
    )
    data = core.extract_json(core.call_gemini(prompt, 0.2))
    best = None
    scores = data.get("scores") if isinstance(data, dict) else None
    if isinstance(scores, list) and len(scores) == len(titles):
        try:
            sc = [float(s) for s in scores]
            best = max(range(len(titles)), key=lambda i: sc[i])
        except (TypeError, ValueError):
            best = None
    if best is None and isinstance(data, dict):
        try:
            best = int(data.get("best"))
        except (TypeError, ValueError):
            best = None
    if best is None or not (0 <= best < len(titles)):
        best = 0
    return titles[best]


def prompt_outline(title, structure, n_ch, take):
    return (
        f'Plan a 6-8 minute YouTube video titled: "{title}".\n'
        f"STRUCTURE: {structure['name']} - {structure['brief']}\n"
        f"Use exactly {n_ch} chapters.\n"
        + (f"THE CREATOR'S OWN TAKE (make it the video's point of view, first person where "
           f"natural): {take}\n" if take else "")
        + "\nThis must be an ORIGINAL, genuinely useful video with a clear narrative arc: a "
        "specific promise in the first 20 seconds, a logical progression, concrete examples (exact "
        "phrases a manipulator says and exact phrases the viewer can reply) and a closing that "
        "ties back to the opening promise. Not a list of generic tips.\n\n"
        "Return ONLY valid JSON with these keys:\n"
        '- "hook": the spoken opening, 35-50 words. No greeting, no "welcome", no "in this '
        'video". Start with a specific situation or claim, then promise what the viewer gets.\n'
        f'- "chapters": array of exactly {n_ch} objects {{"heading": 3-6 words Title Case, '
        '"key_points": array of 3 short strings, "visual_queries": array of 4 stock-footage '
        'searches of 2-4 plain words each (people, emotion, symbolic objects; concrete, nothing '
        'abstract)}\n'
        '- "intro_queries": array of 3 stock-footage searches for the opening\n'
        '- "outro": the spoken closing, 40-55 words: a one-sentence recap, ONE question for the '
        "comments, and a short reminder that this is educational, not therapy. No begging for "
        "likes; at most one soft 'subscribe if this helped'.\n"
        '- "description": 2 sentences with natural search keywords, no hashtags\n'
        '- "hashtags": array of 3 single-word lowercase hashtags without the # symbol\n'
        '- "thumbnail_text": 2 to 4 words, ALL CAPS, a curiosity gap or a direct claim\n'
    )


def _clean_queries(qs, limit):
    out = []
    for q in qs or []:
        q = core.clean_text(str(q)).lower()[:40]
        if q and q not in out:
            out.append(q)
    return out[:limit]


def normalize_outline(data, n_ch):
    if not isinstance(data, dict):
        raise ValueError("outline is not an object")
    chapters = []
    for ch in data.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        heading = core.clean_text(str(ch.get("heading", ""))).strip(" .:-")
        if not heading:
            continue
        points = [core.clean_text(str(p)) for p in (ch.get("key_points") or []) if str(p).strip()]
        chapters.append({
            "heading": " ".join(heading.split()[:7]),
            "key_points": points[:4] or [heading],
            "visual_queries": _clean_queries(ch.get("visual_queries"), 4),
        })
    if len(chapters) < 4:
        raise ValueError(f"only {len(chapters)} chapters")
    hook = core.clean_text(core._strip_hashtags(str(data.get("hook", ""))))
    outro = core.clean_text(core._strip_hashtags(str(data.get("outro", ""))))
    if len(hook.split()) < 15 or len(outro.split()) < 15:
        raise ValueError("hook/outro too short")
    tags = []
    for t in data.get("hashtags") or []:
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())
        if t and t not in tags:
            tags.append(t)
    thumb = core.clean_text(str(data.get("thumbnail_text", ""))).upper().strip(" .")
    return {
        "hook": hook, "outro": outro, "chapters": chapters[:n_ch + 1],
        "intro_queries": _clean_queries(data.get("intro_queries"), 3),
        "description": core.clean_text(core._strip_hashtags(str(data.get("description", "")))),
        "hashtags": tags[:3] or ["narcissist", "boundaries", "stoicism"],
        "thumbnail_text": " ".join(thumb.split()[:4]),
    }


def prompt_chapter(title, structure, ch, idx, n, prev_tail, earlier, take, extra=""):
    return (
        f'You are writing chapter {idx} of {n} of the NARRATION for the YouTube video "{title}".\n'
        f"Structure: {structure['name']} - {structure['brief']}\n"
        f'Chapter heading: "{ch["heading"]}"\n'
        f"Key points: {'; '.join(ch['key_points'])}\n"
        + (f'The previous chapter ended with: "{prev_tail}"\n' if prev_tail else "")
        + (f"Chapters already covered (do not repeat them): {', '.join(earlier)}\n" if earlier else "")
        + (f"The creator's own point of view, respect it: {take}\n" if take else "")
        + "\nWrite 150-190 words of SPOKEN narration. Rules:\n"
        "1. Short sentences (average 8 words, none over 16). Speak to 'you'. Plain words.\n"
        "2. Open with a one-sentence mini-hook, then teach. Include at least one concrete "
        "example: an exact phrase the manipulator might say AND an exact phrase the viewer can "
        "say back.\n"
        "3. NO statistics, percentages, studies, surveys, 'research shows' or named experts. "
        "Stoic ideas may be paraphrased ('Marcus Aurelius wrote about...') but never put invented "
        "quotes in anyone's mouth.\n"
        "4. Never diagnose a real person; say 'a manipulator' or 'someone'. No revenge, "
        "harassment or self-harm advice.\n"
        "5. No filler: no 'in this video', 'welcome back', 'let's dive in', 'journey', 'unlock'.\n"
        + ("6. End with a one-sentence bridge to the next chapter.\n" if idx < n else
           "6. This is the LAST chapter: end on the payoff that answers the video's promise.\n")
        + extra
        + 'Return ONLY valid JSON: {"narration": "..."}'
    )


CLAIM_RE = re.compile(
    r"(\b\d{1,3}\s?%|\bpercent\b|\b\d+ (?:out of|in) \d+\b|\bstud(?:y|ies)\b|\bresearch(?:ers)?\b"
    r"|\baccording to\b|\bscientists?\b|\bsurveys?\b|\bexperts? (?:say|agree)\b"
    r"|\btherapists? (?:say|agree)\b)", re.I)


def claims_flagged(text):
    return bool(CLAIM_RE.search(text or ""))


def strip_claims(text):
    keep = [s for s in core.split_sentences(text) if not CLAIM_RE.search(s)]
    return " ".join(keep)


def write_chapter(title, structure, ch, idx, n, prev_tail, earlier, take):
    extra, best = "", ""
    for attempt in range(1, 4):
        try:
            data = core.extract_json(core.call_gemini(
                prompt_chapter(title, structure, ch, idx, n, prev_tail, earlier, take, extra), 0.9))
            text = core.clean_text(core._strip_hashtags(str((data or {}).get("narration", ""))))
        except Exception as e:
            log(f"[Script] chapter {idx} attempt {attempt}: unusable ({str(e)[:70]})")
            continue
        words = len(text.split())
        if words < 90:
            log(f"[Script] chapter {idx} attempt {attempt}: too short ({words} words)")
            extra = "The last draft was too short. Write at least 150 words.\n"
            continue
        if claims_flagged(text):
            log(f"[Script] chapter {idx} attempt {attempt}: contains statistics/research claims")
            extra = ("IMPORTANT: the last draft contained statistics or research claims. Remove "
                     "every one of them; teach only through examples and exact phrases.\n")
            best = best or text
            continue
        return text
    text = strip_claims(best)
    if len(text.split()) >= 70:
        return text
    raise RuntimeError(f"could not write chapter {idx}")


def generate_plan(history):
    recent_titles = [h.get("title", "") for h in history[-12:]]
    recent_structs = [h.get("structure") for h in history[-2:]]
    pool = [s for s in STRUCTURES if s["name"] not in recent_structs] or STRUCTURES
    structure = _rng.choice(pool)
    n_ch = _rng.randint(5, 7)

    title = None
    try:
        cands = ideate_titles(collect_suggestions(), recent_titles, TOPIC)
        title = pick_best_title(cands)
    except Exception as e:
        log(f"[Topic] title tournament skipped ({str(e)[:80]})")
    if not title:
        title = TOPIC or _rng.choice(core.WINNING_SHORTS)
    log(f"[Topic] '{title}' | structure={structure['name']} | {n_ch} chapters")

    outline = None
    for attempt in range(1, 4):
        try:
            data = core.extract_json(core.call_gemini(
                prompt_outline(title, structure, n_ch, MY_TAKE), 0.9))
            outline = normalize_outline(data, n_ch)
            break
        except Exception as e:
            log(f"[Script] outline attempt {attempt}: {str(e)[:90]}")
    if not outline:
        raise RuntimeError("Gemini did not return a usable outline")

    n = len(outline["chapters"])
    prev_tail, earlier = "", []
    for i, ch in enumerate(outline["chapters"], 1):
        ch["narration"] = write_chapter(title, structure, ch, i, n, prev_tail, earlier, MY_TAKE)
        prev_tail = " ".join(core.split_sentences(ch["narration"])[-2:])
        earlier.append(ch["heading"])
        log(f"[Script] chapter {i}/{n} '{ch['heading']}': {len(ch['narration'].split())} words")

    hook = strip_claims(outline["hook"]) or outline["hook"]
    outro = strip_claims(outline["outro"]) or outline["outro"]
    words = len(hook.split()) + len(outro.split()) + sum(len(c["narration"].split()) for c in outline["chapters"])
    if words < 600:
        raise RuntimeError(f"script too short ({words} words)")
    thumb = outline["thumbnail_text"] or " ".join(title.upper().split()[:3])
    return {
        "title": title[:100], "structure": structure["name"], "hook": hook, "outro": outro,
        "chapters": outline["chapters"], "intro_queries": outline["intro_queries"],
        "description": outline["description"], "hashtags": outline["hashtags"],
        "thumbnail_text": thumb, "words": words,
    }


# ============================================================
# 3. NARRATION
# ============================================================
def _get_kokoro():
    global _KOKORO
    if _KOKORO is None:
        from kokoro import KPipeline          # pip install kokoro  (Apache-2.0)
        _KOKORO = KPipeline(lang_code="a")    # American English
    return _KOKORO


def write_wav_mono(path, arr, sr):
    pcm = (np.clip(arr, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def kokoro_segment(text, out_path):
    """Synthesise one segment sentence-by-sentence so sentence timings are exact."""
    pipe = _get_kokoro()
    sr = 24000
    chunks, sents, t = [], [], 0.0
    for item in pipe("\n".join(core.split_sentences(text)), voice=KOKORO_VOICE, speed=1.0):
        # kokoro yields (graphemes, phonemes, audio) tuples; newer versions wrap them in a Result
        gs = getattr(item, "graphemes", None) or item[0]
        audio = getattr(item, "audio", None)
        if audio is None:
            audio = item[2]
        if audio is None:                       # a chunk with nothing to say
            continue
        arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        arr = np.asarray(arr, dtype=np.float32).reshape(-1)
        dur = len(arr) / sr
        sents.append({"text": str(gs), "start": t, "end": t + dur})
        chunks.append(arr)
        t += dur
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    audio = np.concatenate(chunks)
    words = max(1, len(text.split()))
    wps = words / max(t, 0.01)
    if not (1.6 <= wps <= 4.2) or float(np.abs(audio).max()) < 0.05:     # wrong rate / silence
        raise RuntimeError(f"Kokoro output failed sanity check ({wps:.1f} words/s)")
    write_wav_mono(out_path, audio, sr)
    return [], sents


def edge_segment(text, out_path):
    return asyncio.run(core.generate_voiceover(text, EDGE_VOICE, str(out_path)))


def synthesize_segments(texts, outdir):
    """[(audio_path, word_events, sentence_events)], engine_name. One voice for the whole video."""
    if TTS_ENGINE in ("auto", "kokoro"):
        try:
            res = []
            for i, txt in enumerate(texts):
                p = Path(outdir) / f"seg_{i}.wav"
                wev, sev = kokoro_segment(txt, p)
                res.append((p, wev, sev))
            log("[TTS] Kokoro narrator")
            return res, "kokoro"
        except Exception as e:
            log(f"[TTS] Kokoro not used ({str(e)[:110]}); falling back to Edge TTS")
    res = []
    for i, txt in enumerate(texts):
        p = Path(outdir) / f"seg_{i}.mp3"
        wev, sev = edge_segment(txt, p)
        res.append((p, wev, sev))
    log("[TTS] Edge TTS narrator")
    return res, "edge"


def assemble_voice(seg_audio, texts, out_wav, sr=44100):
    """Trim silence, join segments with a short pause. Returns (word_timings, seg_starts, total)."""
    pieces, words, starts, t = [], [], [], 0.0
    for (path, wev, sev), text in zip(seg_audio, texts):
        pcm, _ = core.decode_pcm(path, sr)
        lead, end = core.speech_bounds(pcm, sr, thr=0.012)
        keep1 = min(len(pcm) / sr, end + 0.12)
        chunk = pcm[int(lead * sr): int(keep1 * sr)]
        for w in core.build_word_timings(text, wev, sev, lead, end):
            words.append({"word": w["word"], "start": t + (w["start"] - lead),
                          "end": t + (w["end"] - lead)})
        starts.append(t)
        pieces += [chunk, np.zeros(int(CHAPTER_GAP * sr), np.float32)]
        t += len(chunk) / sr + CHAPTER_GAP
    total = t - CHAPTER_GAP
    write_wav_mono(out_wav, np.concatenate(pieces[:-1]), sr)
    return words, starts, total


# ============================================================
# 4. FOOTAGE
# ============================================================
def search_pexels_landscape(query):
    if not core.PEXELS_API_KEY:
        return []
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": core.PEXELS_API_KEY},
                         params={"query": query, "orientation": "landscape", "size": "medium",
                                 "per_page": 20}, timeout=30)
        if r.status_code != 200:
            return []
        out = []
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and f.get("link")
                     and int(f.get("width") or 0) >= 1280]
            if not files or float(v.get("duration", 0) or 0) < 6:
                continue
            f = min(files, key=lambda x: abs(int(x["width"]) - 1920))
            out.append({"id": f"px{v['id']}", "url": f["link"], "w": int(f["width"]),
                        "h": int(f["height"]), "dur": float(v["duration"]),
                        "portrait": False, "src": "pexels"})
        return out
    except Exception as e:
        log(f"[Pexels] '{query}' failed: {str(e)[:60]}")
        return []


def search_stock(query):
    cands = search_pexels_landscape(query)
    cands += [c for c in core.search_pixabay(query) if not c["portrait"] and c["dur"] >= 6]
    # prefer longer clips (more distinct windows) but keep it random so videos differ
    cands.sort(key=lambda c: -min(c["dur"], 30) * _rng.uniform(0.6, 1.0))
    return cands


def motion_profile_wide(path):
    cmd = [core.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-t", "60", "-i", str(path),
           "-vf", f"fps={core.PROFILE_FPS},{WIDE_CROP},scale=128:72,format=gray",
           "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True).stdout
    fsz = 128 * 72
    n = len(out) // fsz
    if n < 4:
        return None
    frames = np.frombuffer(out[: n * fsz], np.uint8).reshape(n, 72, 128).astype(np.int16)
    prof = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)).astype(np.float64)
    return np.minimum(prof, np.percentile(prof, 90))


def fetch_pool(queries, want, used):
    clips = []
    for q in list(queries) + _rng.sample(LONG_FALLBACK_QUERIES, k=len(LONG_FALLBACK_QUERIES)):
        if len(clips) >= want:
            break
        for c in search_stock(q)[:5]:
            if c["id"] in used:
                continue
            dest = WORK / f"clip_{_clip_counter[0]}.mp4"
            _clip_counter[0] += 1
            try:
                core.download_file(c["url"], dest)
                w, h, dur = core.probe(dest)
                if w < 960 or dur < 5:
                    raise RuntimeError(f"unusable ({w}x{h}, {dur:.1f}s)")
            except Exception as e:
                log(f"[Stock] download failed ({str(e)[:60]})")
                continue
            used.add(c["id"])
            clips.append({"path": str(dest), "w": w, "h": h, "dur": dur, "id": c["id"],
                          "src": c["src"], "profile": motion_profile_wide(dest)})
            log(f"[Stock] '{q}' via {c['src']} ({w}x{h}, {dur:.0f}s)")
            break
    return clips


def fetch_all_clips(plan):
    """One pool per segment: intro, each chapter, outro."""
    used, pools, allc = set(), [], []
    jobs = [(plan["intro_queries"], 2)]
    jobs += [(ch["visual_queries"], 3) for ch in plan["chapters"]]
    last = plan["chapters"][-1]["visual_queries"]
    jobs += [(last[-2:] + plan["intro_queries"][:1], 2)]
    for queries, want in jobs:
        pool = fetch_pool(queries, want, used)
        pools.append(pool)
        allc += pool
    if not allc:
        raise RuntimeError("could not download any stock footage")
    for i, pool in enumerate(pools):          # a segment with no footage borrows from the rest
        if not pool:
            pools[i] = allc[:3]
    return pools, allc


# ============================================================
# 5. EDIT PLAN
# ============================================================
def phrase_groups(timings, max_words=9, max_chars=46, pause=0.30):
    groups, cur, chars = [], [], 0
    for i, w in enumerate(timings):
        L = len(w["word"])
        prev = timings[i - 1] if i else None
        gap = (w["start"] - prev.get("raw_end", prev["end"])) if prev else 0.0
        if cur and (len(cur) >= max_words or chars + 1 + L > max_chars or gap > pause
                    or prev["word"][-1:] in ".!?"
                    or (prev["word"][-1:] in ",;:" and len(cur) >= 5)):
            groups.append(cur)
            cur, chars = [], 0
        cur.append(w)
        chars += L + (1 if len(cur) > 1 else 0)
    if cur:
        groups.append(cur)
    return groups


def plan_scenes(total, seg_starts, phrase_starts, rng):
    """Scene cuts: 3.5-5 s in the first 20 s (fast open), then 5.5-9 s. Cuts land on chapter
    starts and on phrase starts."""
    bounds = list(seg_starts) + [total]
    scenes = []
    for si in range(len(seg_starts)):
        a, b = bounds[si], bounds[si + 1]
        starts, t = [a], a
        while True:
            nxt = t + (rng.uniform(3.5, 5.0) if t < 20 else rng.uniform(5.5, 9.0))
            if b - nxt < 3.0:
                break
            snapped = min(phrase_starts, key=lambda s: abs(s - nxt)) if phrase_starts else nxt
            if abs(snapped - nxt) > 1.2 or snapped - t < 3.0 or b - snapped < 2.2:
                snapped = nxt
            starts.append(snapped)
            t = snapped
        for s, e in zip(starts, starts[1:] + [b]):
            scenes.append({"start": s, "end": e, "seg": si})
    return scenes


def assign_scenes(scenes, pools, allc, rng):
    index = {id(c): i for i, c in enumerate(allc)}
    taken = {i: [] for i in range(len(allc))}
    prev, rr = None, {}
    for k, sc in enumerate(scenes):
        pool = pools[sc["seg"]]
        seg_len = sc["end"] - sc["start"]
        if k == 0:                          # opening shot = most dynamic moment of the intro pool
            cands = []
            for c in pool:
                off, score = core.best_window(c, seg_len, taken[index[id(c)]])
                cands.append((score, index[id(c)], off))
            score, ci, off = max(cands)
        else:
            n = rr.get(sc["seg"], 0)
            rr[sc["seg"]] = n + 1
            c = pool[n % len(pool)]
            if len(pool) > 1 and index[id(c)] == prev:
                c = pool[(n + 1) % len(pool)]
                rr[sc["seg"]] = n + 2
            ci = index[id(c)]
            off, score = core.best_window(c, seg_len, taken[ci])
        taken[ci].append((off, off + seg_len))
        prev = ci
        zoom_in = k % 2 == 0
        sc.update({"clip": ci, "offset": off,
                   "loop": off + seg_len + 0.15 > allc[ci]["dur"],
                   "z0": 1.0 if zoom_in else 1.08, "z1": 1.08 if zoom_in else 1.0,
                   "pan": rng.uniform(-0.05, 0.05)})
    return scenes


# ============================================================
# 6. TEXT + FRAMES
# ============================================================
THEMES = core.THEMES


def fit_lines(text, font_path, max_w, max_lines, start, minimum, step=6):
    size = start
    while True:
        font = core.load_font(font_path, size)
        lines = core._wrap(text, font, max_w)
        widest = max((font.getlength(l) for l in lines), default=0)
        if (len(lines) <= max_lines and widest <= max_w) or size <= minimum:
            return font, lines
        size -= step


def render_subtitle(text, font_path):
    font, lines = fit_lines(text, font_path, LW * 0.76, 2, 60, 40)
    stroke = 5
    asc, desc = font.getmetrics()
    lh = asc + desc + 4
    img = Image.new("RGBA", (LW, lh * len(lines) + 2 * stroke + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        w = font.getlength(ln)
        d.text(((LW - w) / 2, 10 + stroke + i * lh), ln, font=font, fill=(255, 255, 255, 255),
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return img


def render_banner(text, font_path, theme):
    font, lines = fit_lines(text.upper(), font_path, LW * 0.8, 2, 120, 64)
    asc, desc = font.getmetrics()
    pad_x, pad_y, gap = 36, 16, 14
    box_h = asc + desc + 2 * pad_y
    img = Image.new("RGBA", (LW, len(lines) * box_h + (len(lines) - 1) * gap), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 0
    for ln in lines:
        lw = font.getlength(ln)
        x = (LW - lw) / 2
        d.rounded_rectangle([x - pad_x, y, x + lw + pad_x, y + box_h], radius=22, fill=theme["hook_bg"])
        d.text((x, y + pad_y), ln, font=font, fill=theme["hook_fg"])
        y += box_h + gap
    return img


def render_chapter_card(idx, n, heading, font_path, theme):
    small = core.load_font(font_path, 34)
    label = f"CHAPTER {idx} OF {n}"
    font, lines = fit_lines(heading.upper(), font_path, 900, 2, 62, 40)
    asc, desc = font.getmetrics()
    lh = asc + desc
    w = int(max([font.getlength(l) for l in lines] + [small.getlength(label)]) + 64)
    h = 22 + 44 + lh * len(lines) + 18
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w, h], radius=14, fill=(0, 0, 0, 175))
    d.rounded_rectangle([0, 0, 10, h], radius=5, fill=theme["hook_bg"])
    d.text((34, 20), label, font=small, fill=theme["hook_bg"])
    y = 22 + 44
    for ln in lines:
        d.text((34, y), ln, font=font, fill=(255, 255, 255))
        y += lh
    return img


def render_end_card(font_path, theme):
    text = "SUBSCRIBE FOR THE NEXT BREAKDOWN"
    font = core.load_font(font_path, 46)
    lw = font.getlength(text)
    asc, desc = font.getmetrics()
    pad_x, pad_y = 44, 20
    w, h = int(lw + 2 * pad_x), asc + desc + 2 * pad_y
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=(0, 0, 0, 190),
                        outline=theme["hook_bg"], width=4)
    d.text((pad_x, pad_y), text, font=font, fill=(255, 255, 255))
    return img


def build_shade_long():
    y = np.linspace(-1, 1, LH)[:, None]
    x = np.linspace(-1, 1, LW)[None, :]
    r = np.sqrt((x * 0.8) ** 2 + y ** 2)
    vign = 1.0 - 0.40 * np.clip(r - 0.45, 0, 1) ** 1.4
    base = np.clip(0.74 * vign, 0.25, 1.0)
    rgb = np.dstack([base * 0.95, base * 0.98, base * 1.0])
    return Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))


def zoom_box_long(z, cx):
    cw, ch = LW / z, LH / z
    x0 = min(max(cx * LW - cw / 2, 0), LW - cw)
    y0 = min(max(0.5 * LH - ch / 2, 0), LH - ch)
    return (x0, y0, x0 + cw, y0 + ch)


def scene_frames(clip, sc, n_frames):
    fsz = LW * LH * 3
    cmd = [core.ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if sc["loop"]:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-ss", f"{0.0 if sc['loop'] and sc['offset'] <= 0 else sc['offset']:.3f}",
            "-i", clip["path"],
            "-vf", f"{WIDE_CROP},scale={LW}:{LH}:flags=lanczos,fps={LFPS},format=rgb24",
            "-frames:v", str(n_frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=fsz * 2)
    last = None
    try:
        for _ in range(n_frames):
            buf = proc.stdout.read(fsz)
            if len(buf) == fsz:
                last = np.frombuffer(buf, np.uint8).reshape(LH, LW, 3)
            elif last is None:
                raise RuntimeError("could not decode frames from stock clip")
            yield last
    finally:
        try:
            proc.stdout.close()
        finally:
            proc.kill()
            proc.wait()


def build_long_composer(plan, timings, seg_starts, total, theme, font_path):
    shade = build_shade_long()
    groups = phrase_groups(timings)
    sub_starts = [g[0]["start"] for g in groups]
    sub_ends = [g[-1]["end"] for g in groups]
    sub_imgs = [render_subtitle(" ".join(w["word"] for w in g), font_path) for g in groups]
    n = len(plan["chapters"])
    cards = [(seg_starts[i + 1], seg_starts[i + 1] + 3.4,
              render_chapter_card(i + 1, n, ch["heading"], font_path, theme))
             for i, ch in enumerate(plan["chapters"])]
    chapter_starts = [seg_starts[i + 1] for i in range(n)]
    hook_img = render_banner(plan["thumbnail_text"], font_path, theme)
    end_img = render_end_card(font_path, theme)
    end_t0 = max(total - 9.0, total * 0.9)
    black = Image.new("RGB", (LW, LH), (0, 0, 0))
    sub_y = int(LH * 0.80)

    def compose(base, t, sc):
        sl = max(sc["end"] - sc["start"], 0.1)
        p = min(max((t - sc["start"]) / sl, 0.0), 1.0)
        z = sc["z0"] + (sc["z1"] - sc["z0"]) * p
        img = Image.fromarray(base)
        if z > 1.001:
            img = img.resize((LW, LH), Image.Resampling.BILINEAR,
                             box=zoom_box_long(z, 0.5 + sc["pan"] * (p - 0.5) * 2))
        img = ImageChops.multiply(img, shade)
        for cs in chapter_starts:                       # dip to black marks each new chapter
            d = t - cs
            if 0 <= d < 0.25:
                img = Image.blend(img, black, 0.85 * (1 - d / 0.25))
        if t < 4.0:
            img.paste(hook_img, (0, int(LH * 0.16)), hook_img)
        for t0, t1, card in cards:
            if t0 <= t < t1:
                img.paste(card, (80, 70), card)
        si = bisect.bisect_right(sub_starts, t) - 1
        if si >= 0 and t < sub_ends[si] + 0.15:
            im = sub_imgs[si]
            img.paste(im, (0, sub_y), im)
        if t >= end_t0:
            img.paste(end_img, ((LW - end_img.width) // 2, int(LH * 0.60)), end_img)
        if t > total - 0.6:
            img = Image.blend(img, black, min(1.0, (t - (total - 0.6)) / 0.6))
        return img

    return compose


def render_long(scenes, allc, compose, total, mix_path, output_path):
    total_frames = int(round(total * LFPS))
    vf = core.pick_encoder_vf()
    tags = (["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
            if "bt709" in vf else [])
    log_path = WORK / "encoder.log"
    cmd = [core.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{LW}x{LH}", "-r", str(LFPS), "-i", "-",
           "-i", str(mix_path), "-map", "0:v", "-map", "1:a",
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
           "-maxrate", "9M", "-bufsize", "18M", "-profile:v", "high", *tags, "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-t", f"{total:.3f}", str(output_path)]
    last_report = time.time()
    with open(log_path, "wb") as logf:
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=logf)
        try:
            for si, sc in enumerate(scenes):
                f0 = int(round(sc["start"] * LFPS))
                f1 = total_frames if si == len(scenes) - 1 else int(round(sc["end"] * LFPS))
                for j, base in enumerate(scene_frames(allc[sc["clip"]], sc, max(1, f1 - f0))):
                    enc.stdin.write(compose(base, (f0 + j) / LFPS, sc).tobytes())
                if time.time() - last_report > 60:
                    last_report = time.time()
                    log(f"[Video] rendering... {100 * f1 / total_frames:.0f}%")
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


def long_soundtrack(seg_starts, total, out_path):
    """A different drone for every chapter, cross-faded, with an impact at each chapter start."""
    sr = 44100
    n = int((total + 1.0) * sr)
    mix = np.zeros((n, 2), np.float32)
    bounds = list(seg_starts) + [total]
    for i in range(len(seg_starts)):
        a, b = bounds[i], bounds[i + 1]
        seg_len = (b - a) + 1.5
        tmp = WORK / f"st_{i}.wav"
        core.synth_soundtrack(seg_len, [], tmp, seed=1000 + i)
        with wave.open(str(tmp), "rb") as wf:
            arr = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").reshape(-1, 2) / 32768.0
        fade = int(1.5 * sr)
        if len(arr) > fade:
            arr = arr.astype(np.float32)
            arr[-fade:] *= np.linspace(1, 0, fade)[:, None]
        i0 = int(a * sr)
        seg = arr[: max(0, n - i0)]
        mix[i0:i0 + len(seg)] += seg
    mix = np.clip(mix, -1, 1)
    pcm = (mix * 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return str(out_path)


# ============================================================
# 7. THUMBNAIL + DESCRIPTION
# ============================================================
def make_thumbnail(text, frame_png, theme, font_path, channel_name, out_jpg):
    base = Image.open(frame_png).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
    y = np.linspace(-1, 1, 720)[:, None]
    x = np.linspace(-1, 1, 1280)[None, :]
    vign = 1.0 - 0.45 * np.clip(np.sqrt(x ** 2 + y ** 2) - 0.4, 0, 1)
    shade = Image.fromarray((np.clip(0.58 * vign, 0, 1)[:, :, None].repeat(3, 2) * 255).astype(np.uint8))
    img = ImageChops.multiply(base, shade)
    d = ImageDraw.Draw(img)
    font, lines = fit_lines(text.upper(), font_path, 1120, 3, 190, 90, step=10)
    asc, desc = font.getmetrics()
    lh = asc + desc - 10
    y0 = (720 - lh * len(lines)) // 2 - 10
    for i, ln in enumerate(lines):
        w = font.getlength(ln)
        fill = theme["hook_bg"] if i == len(lines) - 1 else (255, 255, 255)
        d.text(((1280 - w) / 2, y0 + i * lh), ln, font=font, fill=fill,
               stroke_width=9, stroke_fill=(0, 0, 0))
    d.rectangle([60, 640, 420, 648], fill=theme["hook_bg"])
    tag = core.load_font(font_path, 34)
    d.text((60, 650), (channel_name or "").upper(), font=tag, fill=(255, 255, 255),
           stroke_width=3, stroke_fill=(0, 0, 0))
    q = 90
    while True:
        img.save(out_jpg, "JPEG", quality=q)
        if Path(out_jpg).stat().st_size < 1_900_000 or q <= 60:
            return out_jpg
        q -= 10


def fmt_ts(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def chapter_lines(plan, seg_starts, total=None):
    """YouTube only shows chapters if the first starts at 00:00, there are 3+, and each is 10 s+."""
    heads = ["Intro"] + [ch["heading"] for ch in plan["chapters"]] + ["Final thoughts"]
    starts = list(seg_starts)
    if len(starts) > 1 and starts[1] - starts[0] < 10.5:        # intro too short: fold it into chapter 1
        heads, starts = heads[1:], [0.0] + starts[2:]
    if total is not None and len(starts) > 1 and total - starts[-1] < 10.5:
        heads, starts = heads[:-1], starts[:-1]                 # outro too short: fold into the last chapter
    return [f"{fmt_ts(s)} {hd}" for s, hd in zip(starts, heads)]


def build_description(plan, seg_starts, cfg, total=None):
    chapters = chapter_lines(plan, seg_starts, total)
    tags = " ".join("#" + h for h in plan["hashtags"][:3])
    parts = [plan["description"], "In this video:\n" + "\n".join(chapters),
             core.monetization_block(cfg), SAFETY_NOTE, tags]
    return "\n\n".join(p for p in parts if p).replace("<", "").replace(">", "")[:4800]


def build_tags(plan):
    tags, size = [], 0
    for t in list(plan["hashtags"]) + BASE_TAGS:
        if t not in tags and size + len(t) + 1 <= 450:
            tags.append(t)
            size += len(t) + 1
    return tags


# ============================================================
# 8. BUILD + UPLOAD
# ============================================================
def build_long_video(plan, cfg, output_path="longform_output.mp4"):
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(exist_ok=True)
    theme = _rng.choice(THEMES)
    font_path = core.get_font_path()

    texts = [plan["hook"]] + [c["narration"] for c in plan["chapters"]] + [plan["outro"]]
    seg_audio, engine = synthesize_segments(texts, WORK)
    raw_words, seg_starts, total = assemble_voice(seg_audio, texts, WORK / "voice.wav")
    timings = core.finalize_timings(raw_words, 0.0, total)
    log(f"[Video] narration {total / 60:.1f} min via {engine}; {len(timings)} words timed")
    if total < 240:
        raise RuntimeError(f"video would be only {total:.0f}s long; refusing to publish")

    pools, allc = fetch_all_clips(plan)
    groups = phrase_groups(timings)
    scenes = plan_scenes(total, seg_starts, [g[0]["start"] for g in groups], _rng)
    scenes = assign_scenes(scenes, pools, allc, _rng)
    log(f"[Video] {len(scenes)} scenes over {len(allc)} clips")

    music = long_soundtrack(seg_starts, total, WORK / "music.wav")
    mix_path = WORK / "mix.wav"
    core.mix_audio(str(WORK / "voice.wav"), 0.0, music, total, mix_path)

    compose = build_long_composer(plan, timings, seg_starts, total, theme, font_path)
    log(f"[Video] rendering 1920x1080 ({int(total * LFPS)} frames) ...")
    t0 = time.time()
    render_long(scenes, allc, compose, total, mix_path, output_path)
    log(f"[Video] rendered in {(time.time() - t0) / 60:.1f} min")

    thumb = None
    try:
        first = allc[scenes[0]["clip"]]
        frame_png = WORK / "thumb_frame.png"
        core.run_ffmpeg(["-ss", f"{scenes[0]['offset'] + 1.0:.2f}", "-i", first["path"],
                         "-vf", f"{WIDE_CROP},scale=1280:720", "-frames:v", "1", frame_png])
        thumb = make_thumbnail(plan["thumbnail_text"], frame_png, theme, font_path,
                               cfg.get("channel_name", ""), "thumbnail.jpg")
    except Exception as e:
        log(f"[Thumbnail] skipped ({str(e)[:90]})")
    return output_path, thumb, seg_starts, {"engine": engine, "duration": round(total, 1),
                                           "theme": theme["name"]}


def upload_long(youtube, path, title, description, tags, privacy):
    body = {
        "snippet": {"title": title.replace("<", "").replace(">", "")[:100],
                    "description": description, "tags": tags, "categoryId": "27",
                    "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = core.MediaFileUpload(path, chunksize=16 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log(f"[YouTube] Uploaded {int(status.progress() * 100)}%")
        except core.HttpError as e:
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


def set_thumbnail(youtube, video_id, jpg):
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=core.MediaFileUpload(jpg, mimetype="image/jpeg")).execute()
        log("[Thumbnail] uploaded")
    except Exception as e:
        log(f"[Thumbnail] not set ({str(e)[:100]}). Custom thumbnails need a verified channel; "
            "upload thumbnail.jpg manually in Studio if needed.")


def main():
    core.ffmpeg_bin()
    core.VOICE_RATE = EDGE_RATE                       # slightly calmer pace than the Shorts
    history = load_long_history()
    cfg = core.load_monetization()

    youtube = None
    if core.YOUTUBE_CREDENTIALS_JSON:
        try:
            youtube = core.get_youtube()
        except Exception as e:
            if not DRY_RUN:
                raise
            log(f"[YouTube] auth failed (dry run continues): {e}")
    elif not DRY_RUN:
        raise RuntimeError("YOUTUBE_CREDENTIALS secret is missing")

    log("=== 1) Planning the video ===")
    plan = generate_plan(history)
    log(f"Title   : {plan['title']}")
    log(f"Words   : {plan['words']} | thumbnail text: {plan['thumbnail_text']}")

    log("=== 2) Building the video ===")
    path, thumb, seg_starts, meta = build_long_video(plan, cfg)
    description = build_description(plan, seg_starts, cfg, meta["duration"])

    if DRY_RUN:
        Path("longform_description.txt").write_text(
            plan["title"] + "\n\n" + description, encoding="utf-8")
        log(f"=== DRY RUN: {path}, thumbnail.jpg and longform_description.txt saved; "
            "nothing uploaded ===")
        return

    log(f"=== 3) Uploading as {PRIVACY.upper()} ===")
    video_id = upload_long(youtube, path, plan["title"], description, build_tags(plan), PRIVACY)
    if thumb:
        set_thumbnail(youtube, video_id, thumb)
    save_long_history(history, {
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "video_id": video_id, "title": plan["title"], "structure": plan["structure"],
        "chapters": [c["heading"] for c in plan["chapters"]], "privacy": PRIVACY, **meta,
    })
    if PRIVACY == "private":
        log("Uploaded PRIVATE. Open YouTube Studio, watch it, then set it to Public to publish.")
    log("=== DONE ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"FATAL: {exc}")
        raise
