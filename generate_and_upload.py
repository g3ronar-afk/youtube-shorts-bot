import os
import json
import asyncio
import requests
import edge_tts
from google import genai
from google.genai import types
from moviepy.editor import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# ================= ====================
# 1. GENERATE SCRIPT & METADATA VIA GEMINI
# ======================================
def generate_content():
    print("Generating script and metadata with Gemini...")
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    
    prompt = """
    Gumawa ng isang short, engaging YouTube Shorts script (30-45 seconds) tungkol sa Stoicism o Personal Growth.
    Magbigay din ng angkop na Title, Description (may hashtags), at Keyword search query para sa background video (in English).
    
    Mag-respond sa JSON format na may eksaktong keys na ito:
    {
      "title": "Title ng Short",
      "script": "Ang mismong babasahing text/script sa Tagalog o English",
      "description": "Short description with #Shorts #Stoicism",
      "search_query": "nature background dark dramatic"
    }
    """
    
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    return json.loads(response.text)

# ================= ====================
# 2. GENERATE VOICEOVER (EDGE-TTS)
# ======================================
async def create_voiceover(text, output_file="voiceover.mp3"):
    print("Creating voiceover with Edge-TTS...")
    # Pwedeng baguhin ang boses: "tl-PH-AngeloNeural" (Filipino) o "en-US-ChristopherNeural" (English)
    communicate = edge_tts.Communicate(text, "tl-PH-AngeloNeural")
    await communicate.save(output_file)

# ================= ====================
# 3. DOWNLOAD BACKGROUND VIDEO (PEXELS)
# ======================================
def download_background_video(query, output_file="background.mp4"):
    print(f"Searching stock video for: {query}...")
    headers = {"Authorization": os.environ.get("PEXELS_API_KEY")}
    url = f"https://api.pexels.com/videos/search?query={query}&per_page=5&orientation=portrait"
    
    response = requests.get(url, headers=headers).json()
    if not response.get("videos"):
        # Fallback search kung walang nahanap
        url = "https://api.pexels.com/videos/search?query=nature&per_page=5&orientation=portrait"
        response = requests.get(url, headers=headers).json()
        
    video_files = response["videos"][0]["video_files"]
    # Kunin ang HD video link
    video_url = next(f["link"] for f in video_files if f.get("height") and f["height"] >= 1080)
    
    print("Downloading background video...")
    video_data = requests.get(video_url).content
    with open(output_file, "wb") as f:
        f.write(video_data)

# ================= ====================
# 4. RENDER FINAL VIDEO (MOVIEPY)
# ======================================
def render_video(data):
    print("Rendering final video...")
    audio = AudioFileClip("voiceover.mp3")
    video = VideoFileClip("background.mp4")
    
    # I-crop/i-loop ang video batay sa haba ng audio
    if video.duration < audio.duration:
        video = video.loop(duration=audio.duration)
    else:
        video = video.subclip(0, audio.duration)
        
    video = video.set_audio(audio)
    
    # Dagdagan ng Overlay Text / Subtitle (Opsyonal)
    txt_clip = TextClip(
        data["title"], 
        fontsize=40, 
        color='white', 
        font='Arial-Bold',
        method='caption',
        size=(video.w * 0.8, None)
    ).set_position(('center', 'center')).set_duration(audio.duration)
    
    final_clip = CompositeVideoClip([video, txt_clip])
    final_clip.write_videofile("final_short.mp4", fps=24, codec="libx264")

# ================= ====================
# 5. UPLOAD TO YOUTUBE DATA API
# ======================================
def upload_to_youtube(data):
    print("Uploading to YouTube...")
    # Ang YOUTUBE_CREDENTIALS ay nakasave bilang JSON string sa Secrets
    creds_json = json.loads(os.environ.get("YOUTUBE_CREDENTIALS"))
    creds = Credentials.from_authorized_user_info(creds_json)
    
    youtube = build("youtube", "v3", credentials=creds)
    
    body = {
        "snippet": {
            "title": data["title"],
            "description": data["description"],
            "tags": ["Shorts", "Stoicism", "Motivation"],
            "categoryId": "22" # People & Blogs
        },
        "status": {
            "privacyStatus": "public", # Pwedeng "private" muna para sa test
            "selfDeclaredMadeForKids": False
        }
    }
    
    media = MediaFileUpload("final_short.mp4", chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%")
            
    print(f"Video uploaded successfully! Video ID: {response['id']}")

# ================= ====================
# MAIN EXECUTION PIPELINE
# ======================================
if __name__ == "__main__":
    # Step 1: Generate Text Content
    content = generate_content()
    
    # Step 2: Create Audio
    asyncio.run(create_voiceover(content["script"]))
    
    # Step 3: Fetch Video
    download_background_video(content["search_query"])
    
    # Step 4: Combine & Render
    render_video(content)
    
    # Step 5: Upload
    upload_to_youtube(content)
