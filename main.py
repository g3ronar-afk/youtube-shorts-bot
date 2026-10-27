import os
import random
import json
import requests
import asyncio
import edge_tts
import time
import numpy as np
from PIL import Image, ImageDraw, ImageFont

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
# 1. GENERATE CONTENT (Pattern: Bold Claim + Specific Enemy)
# ============================================================
def generate_content():
    prompt = (
        "You are a Stoic philosopher and dark psychology expert writing for a US audience. "
        "Write a 20-second YouTube Short script in STRICTLY ENGLISH about narcissistic manipulation or Stoic resilience. "
        "HOOK RULE (first sentence): Use a BOLD CLAIM that confronts the viewer's behavior. "
        "Examples: 'Most people try to defeat a narcissist through arguments. That's exactly what they want.' "
        "'The reason you keep attracting narcissists isn't bad luck. It's this.' "
        "'Stop explaining yourself to a narcissist. They already know. They just don't care.' "
        "STRUCTURE: Hook (bold claim) + One sharp insight + End with a final statement (no CTA in script). "
        "Keep it 18-22 words total. DO NOT use Tagalog. English only. "
        "Format:\nTitle: [Keyword]: [Hook]\nScript: [The message]"
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
    title = "Narcissist Manipulation: The Trap You Don't See"
    script = raw_text
    for line in raw_text.splitlines():
        line = line.strip()
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].strip()
        elif line.lower().startswith("script:"):
            script = line.split(":", 1)[1].strip()
    
    if "narcissist" not in title.lower() and "stoic" not in title.lower():
        title = f"Narcissist Manipulation: {title}"
    
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    return title[:100], script

# ============================================================
# 2. DOWNLOAD STOCK VIDEO (Statue + Rain = Visual Signature)
# ============================================================
def download_stock_video(query="marble statue rain dark cinematic"):
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&per_page=20&safesearch=true"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    if not hits:
        # Fallback sa dark cinematic kung walang statue
        query = "dark cinematic vertical moody"
        url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&per_page=20&safesearch=true"
        resp = requests.get(url, timeout=30)
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
# 3. GENERATE VOICEOVER + TIMINGS
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
# 4. KARAOKE CAPTIONS
# ============================================================
def create_karaoke_captions(word_timings, script_text, total_duration, video_size, font_path, max_words_per_line=3):
    clips = []
    stroke_width = 4
    max_width = int(video_size[0] * 0.85)
    base_font_size = 80
    
    if not word_timings:
        words = script_text.split()
        if not words: return []
        time_per_word = total_duration / len(words)
        word_timings = [{"word": w, "start": i*time_per_word, "end": (i+1)*time_per_word} for i, w in enumerate(words)]

    for i in range(0, len(word_timings), max_words_per_line):
        group = word_timings[i:i + max_words_per_line]
        if not group: continue
        
        for j, active_word in enumerate(group):
            word_start = active_word["start"]
            word_end = active_word["end"]
            
            img = Image.new("RGBA", video_size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            
            font_size = base_font_size
            total_text_width = 9999
            while total_text_width > max_width and font_size > 20:
                try: font = ImageFont.truetype(font_path, font_size)
                except: font = ImageFont.load_default(); break
                test_text = " ".join(w["word"] for w in group)
                bbox = draw.textbbox((0, 0), test_text, font=font)
                total_text_width = bbox[2] - bbox[0]
                if total_text_width > max_width:
                    font_size -= 5

            try: pop_font = ImageFont.truetype(font_path, font_size + 10)
            except: pop_font = font
            
            total_text = " ".join(w["word"] for w in group)
            bbox = draw.textbbox((0, 0), total_text, font=font)
            start_x = (video_size[0] - (bbox[2] - bbox[0])) // 2
            y_pos = video_size[1] // 2 - 50
            
            current_x = start_x
            for k, w in enumerate(group):
                word_text = w["word"] + (" " if k < len(group) - 1 else "")
                color = (255, 214, 10) if k == j else (255, 255, 255)
                current_font = pop_font if k == j else font
                current_y = y_pos - (10 if k == j else 0)
                
                for dx in [-stroke_width, 0, stroke_width]:
                    for dy in [-stroke_width, 0, stroke_width]:
                        draw.text((current_x + dx, current_y + dy), word_text, font=current_font, fill=(0, 0, 0, 255))
                
                draw.text((current_x, current_y), word_text, font=current_font, fill=color + (255,))
                
                bbox_w = draw.textbbox((0, 0), word_text, font=font)
                current_x += bbox_w[2] - bbox_w[0]
            
            frame = np.array(img)
            clip = ImageClip(frame).with_start(word_start).with_duration(word_end - word_start)
            clips.append(clip)
    
    return clips

# ============================================================
# 5. CREATE VIDEO (With Stronger End-Card CTA)
# ============================================================
def create_video(script_text, output_path="final_output.mp4"):
    print("[Video] Downloading background...")
    bg_path = download_stock_video()
    
    print("[Video] Generating voiceover...")
    voice_path, word_timings = asyncio.run(generate_voiceover_with_timings(script_text))

    print("[Video] Editing...")
    clip = VideoFileClip(bg_path)
    voice_audio = AudioFileClip(voice_path)
    
    duration = voice_audio.duration + 0.5
    clip = clip.subclipped(0, min(duration, clip.duration))
    if clip.duration < duration:
        clip = clip.with_effects([vfx.Loop(duration=duration)])

    w, h = clip.size
    if w > h:
        new_w = int(h * 9 / 16)
        x1 = max(0, int(w / 2 - new_w / 2))
        x2 = min(w, x1 + new_w)
        clip = clip.cropped(x1=x1, y1=0, x2=x2, y2=h)

    clip = clip.with_effects([vfx.Resize(lambda t: 1 + 0.18 * (t / duration) + random.uniform(-0.005, 0.005))])
    overlay = ColorClip(size=clip.size, color=(5, 15, 25)).with_opacity(0.6).with_duration(duration)

    progress_bar = ColorClip(size=(clip.w, 8), color=(255, 0, 0)).with_duration(duration)
    progress_bar = progress_bar.with_effects([vfx.Resize(lambda t: (clip.w * (t / duration) + 1, 8))])
    progress_bar = progress_bar.with_position(("left", "bottom"))

    print("[Video] Building karaoke captions...")
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    caption_clips = create_karaoke_captions(word_timings, script_text, duration, clip.size, font_path)

    # --- STRONGER END CARD ---
    end_card_img = Image.new("RGBA", clip.size, (0, 0, 0, 0))
    draw_end = ImageDraw.Draw(end_card_img)
    try:
        end_font = ImageFont.truetype(font_path, 45)
    except:
        end_font = ImageFont.load_default()
    end_text = "Save this. Then watch the full breakdown."
    bbox = draw_end.textbbox((0, 0), end_text, font=end_font)
    x_pos = (clip.size[0] - (bbox[2] - bbox[0])) // 2
    y_pos = clip.size[1] - 150
    for dx in [-2, 0, 2]:
        for dy in [-2, 0, 2]:
            draw_end.text((x_pos + dx, y_pos + dy), end_text, font=end_font, fill=(0, 0, 0, 255))
    draw_end.text((x_pos, y_pos), end_text, font=end_font, fill=(255, 255, 255, 255))
    
    end_card_clip = ImageClip(np.array(end_card_img)).with_start(duration - 2.5).with_duration(2.5)

    final_clip = CompositeVideoClip([clip, overlay] + caption_clips + [progress_bar, end_card_clip])
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
# 6. UPLOAD TO YOUTUBE
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
                     "motivation", "mental toughness", "stoic wisdom", "narcissist", 
                     "toxic relationship", "self improvement", "emotional control"],
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
    
    description = (
        f"{script}\n\n"
        f"Watch the full breakdown on my channel!\n\n"
        f"#Stoicism #Narcissist #EmotionalControl #Mindset #Shorts #DarkPsychology #MentalToughness #ToxicRelationships #SelfImprovement"
    )
    
    print("=== 2) Creating video ===")
    video_path = create_video(script)
    
    print("=== 3) Uploading to YouTube ===")
    upload_to_youtube(video_path, title, description)
    print("=== DONE ===")
