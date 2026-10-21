import os
import random
import json
import requests

from google import genai
from google.genai import errors as genai_errors
from google.auth.transport.requests import Request
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from moviepy import VideoFileClip  # MoviePy 2.x import (editor is removed)


# ============================================================
# CONFIG — pulled from GitHub Actions secrets / env vars
# ============================================================
GEMINI_API_KEY          = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY         = os.environ.get("PIXABAY_API_KEY")
YOUTUBE_CREDENTIALS_JSON = os.environ.get("YOUTUBE_CREDENTIALS")

if not all([GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS_JSON]):
    raise EnvironmentError(
        "Missing env vars. Need GEMINI_API_KEY, PIXABAY_API_KEY, YOUTUBE_CREDENTIALS."
    )

client = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
# 1. GENERATE CONTENT WITH GEMINI
# ============================================================
def generate_content():
    """Generates Stoicism / Narcissism short-form content via Gemini."""
    prompt = (
        "Gumawa ng isang malakas at makabuluhang mensahe tungkol sa Stoicism, mental toughness, "
        "o kaya ay pag-unawa sa mga manipulatibong ugali ng isang narcissist. "
        "Gawin itong angkop para sa isang YouTube Short (mga 30 hanggang 45 words lang). "
        "Ibigay ito sa ganitong format:\n"
        "Title: [Maikling Pamagat na Catchy]\n"
        "Script: [Ang mensahe o quote na may malalim na aral]"
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
        )
        text = response.text.strip()
        print(f"[Gemini] Raw output:\n{text}\n")
        return text
    except genai_errors.APIError as e:
        print(f"[Gemini ERROR] code={e.code} message={e.message}")
        raise


def parse_gemini_output(raw_text):
    """Extract Title and Script from Gemini's formatted response."""
    title = "Unbreakable Mind #Shorts"
    script = raw_text

    for line in raw_text.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("title:"):
            title = line.split(":", 1)[1].strip()
        elif low.startswith("script:"):
            script = line.split(":", 1)[1].strip()

    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    title = title[:100]  # YouTube limit
    return title, script


# ============================================================
# 2. DOWNLOAD STOCK VIDEO FROM PIXABAY
# ============================================================
def download_stock_video(query="dark aesthetic moody cinematic"):
    url = (
        f"https://pixabay.com/api/videos/"
        f"?key={PIXABAY_API_KEY}&q={query}&per_page=10&safesearch=true"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    hits = data.get("hits", [])
    if not hits:
        raise RuntimeError("Walang nakuhang stock video mula sa Pixabay.")

    random.shuffle(hits)
    for hit in hits:
        videos = hit.get("videos", {})
        for quality in ("medium", "small", "large", "tiny"):
            v = videos.get(quality)
            if v and v.get("url"):
                video_bytes = requests.get(v["url"], timeout=60).content
                with open("background.mp4", "wb") as f:
                    f.write(video_bytes)
                print(f"[Pixabay] Downloaded {quality}: {v['url']}")
                return "background.mp4"

    raise RuntimeError("Walang valid na video URL sa Pixabay results.")


# ============================================================
# 3. CREATE THE VIDEO
# ============================================================
def create_video(script_text, output_path="final_output.mp4"):
    bg_path = download_stock_video("dark moody cinematic")

    clip = VideoFileClip(bg_path)
    clip = clip.subclipped(0, min(15, clip.duration))  # MoviePy 2.x method

    w, h = clip.size
    # Crop to 9:16 vertical if video is landscape
    if w > h:
        new_w = int(h * 9 / 16)
        x1 = max(0, int(w / 2 - new_w / 2))
        x2 = min(w, x1 + new_w)
        clip = clip.cropped(x1=x1, y1=0, x2=x2, y2=h)

    clip.write_videofile(
        output_path,
        fps=24,
        codec="libx264",
        audio=False,        # Pixabay clips have no audio → avoid aac warnings
        logger=None,
    )
    clip.close()
    print(f"[MoviePy] Video saved: {output_path}")
    return output_path


# ============================================================
# 4. UPLOAD TO YOUTUBE
# ============================================================
def upload_to_youtube(video_file_path, title, description):
    creds_dict = json.loads(YOUTUBE_CREDENTIALS_JSON)

    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=creds_dict["refresh_token"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        token_uri=creds_dict.get(
            "token_uri", "https://oauth2.googleapis.com/token"
        ),
    )
    creds.refresh(Request())  # CRITICAL — needed for headless CI

    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [
                "stoicism", "narcissism", "mindset",
                "shorts", "unbreakablearmoredmind",
            ],
            "categoryId": "27",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[YouTube] Uploaded {int(status.progress() * 100)}%")

    print(f"[YouTube] Success! Video ID: {response['id']}")


# ============================================================
# 5. MAIN
# ============================================================
if __name__ == "__main__":
    print("=== 1) Generating script with Gemini ===")
    raw = generate_content()
    title, script = parse_gemini_output(raw)
    print(f"Title: {title}\nScript: {script}\n")

    description = (
        f"{script}\n\n"
        f"#Stoicism #Narcissism #Mindset #Shorts #UnbreakableArmoredMind"
    )

    print("=== 2) Creating video ===")
    video_path = create_video(script)

    print("=== 3) Uploading to YouTube ===")
    upload_to_youtube(video_path, title, description)

    print("=== DONE ===")
