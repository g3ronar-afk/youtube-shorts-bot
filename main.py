import os
import random
import json
import requests
import asyncio
import edge_tts
import time
import numpy as np

from google import genai
from google.genai import errors as genai_errors
from google.auth.transport.requests import Request
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from moviepy import VideoFileClip, AudioFileClip, CompositeVideoClip, ColorClip, vfx, ImageClip

# ============================================================
# CONFIG
# ============================================================
GEMINI_API_KEY           = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY          = os.environ.get("PIXABAY_API_KEY")
YOUTUBE_CREDENTIALS_JSON = os.environ.get("YOUTUBE_CREDENTIALS")

client = genai.Client(api_key=GEMINI_API_KEY)

# ============================================================
# 1. GENERATE CONTENT (VIRAL HOOK + HASHTAGS SA PROMPT)
# ============================================================
def generate_content():
    prompt = (
        "You are a master of Stoic philosophy, dark psychology, and viral marketing. "
        "Write a powerful, cinematic message in STRICTLY ENGLISH about Stoicism, mental toughness, "
        "or understanding manipulative behaviors of a narcissist. "
        "RULES FOR VIRALITY:\n"
        "1. Start with a NEGATIVE or CONTRARIAN hook in the first sentence (e.g., 'Stop doing this...', 'Being nice is weak...').\n"
        "2. Keep it extremely short: STRICTLY between 18 to 22 words total.\n"
        "3. End with a subtle Call to Action (e.g., 'Watch the full video on my channel', 'Follow for the truth').\n"
        "4. DO NOT use Tagalog or any other language. English only.\n"
        "Format exactly like this:\n"
        "Title: [Catchy Short Title]\n"
        "Script: [The message]"
    )
    
    max_retries = 5
    base_delay = 2
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
            return response.text.strip()
        except genai_errors.APIError as e:
            if e.code in [503, 429]:
                if attempt == max_retries - 1:
                    raise
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
            else:
                raise

def parse_gemini_output(raw_text):
    title = "Unbreakable Mind #Shorts"
    script = raw_text
    for line in raw_text.splitlines():
        line = line.strip()
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].strip()
        elif line.lower().startswith("script:"):
            script = line.split(":", 1)[1].strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    return title[:100], script

# ============================================================
# 2. DOWNLOAD STOCK VIDEO
# ============================================================
def download_stock_video(query="dark cinematic vertical moody"):
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&per_page=20&safesearch=true"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    if not hits: raise RuntimeError("No video found.")
    
    random.shuffle(hits)
    for hit in hits:
        for quality in ("medium", "large", "small"):
            v = hit.get("videos", {}).get(quality)
            if v and v.get("url"):
                try:
                    video_data = requests.get(v["url"], timeout=60).content
                    if len(video_data) < 100000:
                        continue
                    with open("background.mp4", "wb") as f:
                        f.write(video_data)
                    test_clip = VideoFileClip("background.mp4")
                    test_clip.close()
                    return "background.mp4"
                except Exception:
                    continue
    raise RuntimeError("Nabigo ang lahat ng attempts na mag-download ng valid na video.")

# ============================================================
# 3. GENERATE VOICEOVER + WORD TIMESTAMPS
# ============================================================
async def generate_voiceover_with_timings(text, output_path="voiceover.mp3"):
    voice = "en-US-AndrewNeural"
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            communicate = edge_tts.Communicate(text, voice, rate="-8%", pitch="-2Hz")
            timings = []
            with open(output_path, "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        start = chunk["offset"] / 1e7
                        dur = chunk["duration"] / 1e7
                        timings.append({"word": chunk["text"], "start": start, "end": start + dur})
            
            print(f"[TTS] Voiceover saved: {output_path} ({len(timings)} words timed)")
            return output_path, timings
        except Exception as e:
            print(f"[TTS Warning] attempt {attempt+1}/{max_retries}: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(5)

# ============================================================
# 4. CREATE KARAOKE CAPTIONS
# ============================================================
def create_karaoke_captions(word_timings, video_size, font_path, max_words_per_line=3):
    """Gumagawa ng ImageClip per word group na may highlight sa kasalukuyang salita."""
    from PIL import Image, ImageDraw, ImageFont
    
    clips = []
    font_size = 65
    stroke_width = 3
    
    # Group words into lines of 3
    for i in range(0, len(word_timings), max_words_per_line):
        group = word_timings[i:i + max_words_per_line]
        if not group:
            continue
        
        start_time = group[0]["start"]
        end_time = group[-1]["end"]
        
        # Para sa bawat salita sa group, gumawa ng frame na naka-highlight ito
        for j, active_word in enumerate(group):
            word_start = active_word["start"]
            word_end = active_word["end"]
            
            # Gumawa ng image gamit ang PIL
            img = Image.new("RGBA", video_size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            
            try:
                font = ImageFont.truetype(font_path, font_size)
            except:
                font = ImageFont.load_default()
            
            # Kalkulahin ang posisyon ng bawat salita
            total_text = " ".join(w["word"] for w in group)
            bbox = draw.textbbox((0, 0), total_text, font=font)
            total_width = bbox[2] - bbox[0]
            start_x = (video_size[0] - total_width) // 2
            y_pos = video_size[1] // 2 - 50
            
            current_x = start_x
            for k, w in enumerate(group):
                word_text = w["word"] + (" " if k < len(group) - 1 else "")
                color = (255, 214, 10) if k == j else (255, 255, 255)  # Dilaw kung active
                
                # Outline
                for dx in [-stroke_width, 0, stroke_width]:
                    for dy in [-stroke_width, 0, stroke_width]:
                        draw.text((current_x + dx, y_pos + dy), word_text, font=font, fill=(0, 0, 0))
                
                draw.text((current_x, y_pos), word_text, font=font, fill=color)
                bbox_w = draw.textbbox((0, 0), word_text, font=font)
                current_x += bbox_w[2] - bbox_w[0]
            
            # Gawing ImageClip
            frame = np.array(img)
            clip = ImageClip(frame).with_start(word_start).with_duration(word_end - word_start)
            clips.append(clip)
    
    return clips

# ============================================================
# 5. CREATE VIDEO (With Ken Burns, Dark Overlay, Karaoke Captions)
# ============================================================
def create_video(script_text, output_path="final_output.mp4"):
    print("[Video] Downloading background...")
    bg_path = download_stock_video()
    
    print("[Video] Generating voiceover with word timings...")
    voice_path, word_timings = asyncio.run(generate_voiceover_with_timings(script_text))

    print("[Video] Editing...")
    clip = VideoFileClip(bg_path)
    voice_audio = AudioFileClip(voice_path)
    
    duration = voice_audio.duration + 0.5
    clip = clip.subclipped(0, min(duration, clip.duration))
    if clip.duration < duration:
        clip = clip.with_effects([vfx.Loop(duration=duration)])

    # Crop to 9:16
    w, h = clip.size
    if w > h:
        new_w = int(h * 9 / 16)
        x1 = max(0, int(w / 2 - new_w / 2))
        x2 = min(w, x1 + new_w)
        clip = clip.cropped(x1=x1, y1=0, x2=x2, y2=h)

    # Ken Burns
    clip = clip.with_effects([vfx.Resize(lambda t: 1 + 0.18 * (t / duration))])

    # Dark Overlay
    overlay = ColorClip(size=clip.size, color=(0, 0, 0)).with_opacity(0.5).with_duration(duration)

    # Karaoke Captions
    print("[Video] Building karaoke captions...")
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    caption_clips = create_karaoke_captions(word_timings, clip.size, font_path)

    final_clip = CompositeVideoClip([clip, overlay] + caption_clips)
    final_clip = final_clip.with_audio(voice_audio)

    final_clip.write_videofile(
        output_path, 
        fps=30, 
        codec="libx264", 
        audio_codec="aac",
        logger=None
    )
    
    clip.close()
    voice_audio.close()
    final_clip.close()
    return output_path

# ============================================================
# 6. UPLOAD TO YOUTUBE (With Better SEO)
# ============================================================
def upload_to_youtube(video_file_path, title, description):
    creds_dict = json.loads(YOUTUBE_CREDENTIALS_JSON)
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=creds_dict["refresh_token"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
    )
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["stoicism", "narcissism", "mindset", "shorts", "darkpsychology", 
                     "motivation", "mental toughness", "stoic wisdom"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }

    media = MediaFileUpload(video_file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[YouTube] Uploaded {int(status.progress() * 100)}%")
    print(f"[YouTube] Success! Video ID: {response['id']}")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=== 1) Generating script with Gemini ===")
    raw = generate_content()
    title, script = parse_gemini_output(raw)
    
    # SEO-optimized description
    description = (
        f"{script}\n\n"
        f"Watch the full breakdown on my channel!\n\n"
        f"#Stoicism #Narcissism #Mindset #Shorts #DarkPsychology #MentalToughness"
    )
    
    print("=== 2) Creating video ===")
    video_path = create_video(script)
    
    print("=== 3) Uploading to YouTube ===")
    upload_to_youtube(video_path, title, description)
    print("=== DONE ===")
