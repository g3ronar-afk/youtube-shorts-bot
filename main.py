import os
import random
import json
import requests
import asyncio
import edge_tts

from google import genai
from google.genai import errors as genai_errors
from google.auth.transport.requests import Request
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from moviepy import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip

# ============================================================
# CONFIG
# ============================================================
GEMINI_API_KEY          = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY         = os.environ.get("PIXABAY_API_KEY")
YOUTUBE_CREDENTIALS_JSON = os.environ.get("YOUTUBE_CREDENTIALS")

if not all([GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS_JSON]):
    raise EnvironmentError("Missing env vars. Need GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS.")

client = genai.Client(api_key=GEMINI_API_KEY)

# ============================================================
# 1. GENERATE CONTENT WITH GEMINI
# ============================================================
def generate_content():
    prompt = (
        "Gumawa ng isang malakas at makabuluhang mensahe tungkol sa Stoicism, mental toughness, "
        "o kaya ay pag-unawa sa mga manipulatibong ugali ng isang narcissist. "
        "Gawin itong angkop para sa isang YouTube Short (mga 30 hanggang 45 words lang). "
        "Ibigay ito sa ganitong format:\n"
        "Title: [Maikling Pamagat na Catchy]\n"
        "Script: [Ang mensahe o quote na may malalim na aral]"
    )
    try:
        response = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
        return response.text.strip()
    except genai_errors.APIError as e:
        print(f"[Gemini ERROR] code={e.code} message={e.message}")
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
def download_stock_video(query="dark aesthetic moody cinematic"):
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&per_page=10&safesearch=true"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    if not hits: raise RuntimeError("No video found.")
    
    random.shuffle(hits)
    for hit in hits:
        for quality in ("medium", "small", "large", "tiny"):
            v = hit.get("videos", {}).get(quality)
            if v and v.get("url"):
                with open("background.mp4", "wb") as f:
                    f.write(requests.get(v["url"], timeout=60).content)
                return "background.mp4"
    raise RuntimeError("No valid video URL.")

# ============================================================
# 3. GENERATE VOICEOVER USING EDGE-TTS
# ============================================================
async def generate_voiceover(text, output_path="voiceover.mp3"):
    # Pwedeng palitan ang boses: "en-US-ChristopherNeural" (Lalaki) or "en-US-AriaNeural" (Babae)
    # Pwede ring "fil-PH-AngeloNeural" kung gusto mo ng Tagalog
    voice = "en-US-ChristopherNeural" 
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)
    print(f"[TTS] Voiceover saved: {output_path}")
    return output_path

# ============================================================
# 4. CREATE VIDEO (With Text and Audio)
# ============================================================
def create_video(script_text, output_path="final_output.mp4"):
    print("[Video] Downloading background...")
    bg_path = download_stock_video()
    
    print("[Video] Generating voiceover...")
    # Run the async TTS function
    asyncio.run(generate_voiceover(script_text, "voiceover.mp3"))

    print("[Video] Editing...")
    clip = VideoFileClip(bg_path)
    clip = clip.subclipped(0, min(15, clip.duration))
    
    audio = AudioFileClip("voiceover.mp3")
    
    # Kapag mas mahaba ang audio kaysa sa video, i-loop ang video
    # (Pero dahil 30-45 words lang, usually kasya ito sa 15 seconds)
    final_duration = max(clip.duration, audio.duration)
    
    # Resize/Crop to 9:16
    w, h = clip.size
    if w > h:
        new_w = int(h * 9 / 16)
        x1 = max(0, int(w / 2 - new_w / 2))
        x2 = min(w, x1 + new_w)
        clip = clip.cropped(x1=x1, y1=0, x2=x2, y2=h)

    # Add text overlay (Optional but highly recommended for Shorts)
    try:
        # Gumawa ng text box na naka-center sa video
        txt_clip = TextClip(
            text=script_text,
            fontsize=50,
            color='white',
            font='Arial-Bold',
            size=(clip.w - 100, None), # May margin sa gilid
            method='caption',
            text_align='center'
        ).with_position(('center', 'center')).with_duration(clip.duration)
        
        final_clip = CompositeVideoClip([clip, txt_clip])
    except Exception as e:
        print(f"[Warning] TextClip failed (baka walang font): {e}")
        final_clip = clip # Fallback kung walang font sa runner

    # Isaksak ang audio
    final_clip = final_clip.with_audio(audio)
    
    final_clip.write_videofile(
        output_path, 
        fps=24, 
        codec="libx264", 
        audio_codec="aac",
        logger=None
    )
    
    clip.close()
    audio.close()
    final_clip.close()
    
    return output_path

# ============================================================
# 5. UPLOAD TO YOUTUBE
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
            "tags": ["stoicism", "narcissism", "mindset", "shorts"],
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
    
    description = f"{script}\n\n#Stoicism #Narcissism #Mindset #Shorts"
    
    print("=== 2) Creating video ===")
    video_path = create_video(script)
    
    print("=== 3) Uploading to YouTube ===")
    upload_to_youtube(video_path, title, description)
    print("=== DONE ===")
