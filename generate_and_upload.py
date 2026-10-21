import os
import random
from google import genai
from moviepy.editor import VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import google.oauth2.credentials
import json

# Initialize APIs
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")
YOUTUBE_CREDENTIALS_JSON = os.environ.get("YOUTUBE_CREDENTIALS")

client = genai.Client(api_key=GEMINI_API_KEY)

def generate_content():
    """Gumagamit ng gemini-2.0-flash at nagbibigay ng Stoicism / Narcissism niche content para sa YouTube Shorts"""
    prompt = (
        "Gumawa ng isang malakas at makabuluhang mensahe tungkol sa Stoicism, mental toughness, "
        "o kaya ay pag-unawa sa mga manipulatibong ugali ng isang narcissist. "
        "Gawin itong angkop para sa isang YouTube Short (mga 30 hanggang 45 words lang). "
        "Ibigay ito sa ganitong format:\n"
        "Title: [Maikling Pamagat na Catchy]\n"
        "Script: [Ang mensahe o quote na may malalim na aral]"
    )
    
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt,
    )
    return response.text

def download_stock_video(query="dark aesthetic moody cinematic"):
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&per_page=5"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        if data['hits']:
            # Pumili ng random video para iba-iba ang itsura kada upload
            chosen_hit = random.choice(data['hits'])
            video_url = chosen_hit['videos']['medium']['url']
            video_data = requests.get(video_url).content
            with open("background.mp4", "wb") as f:
                f.write(video_data)
            return "background.mp4"
    raise Exception("Hindi makakuha ng stock video mula sa Pixabay.")

def create_video(script_text):
    # I-download ang background video na may dark/moody aesthetic para sa stoic niche
    bg_video_path = download_stock_video("dark moody background")
    
    # Mag-load ng video at i-crop para maging vertical (YouTube Short size 9:16)
    clip = VideoFileClip(bg_video_path)
    if clip.duration > 15:
        clip = clip.subclip(0, 15) # Kunin ang unang 15 segundo
        
    # I-resize para maging vertical kung kinakailangan
    w, h = clip.size
    if w > h:
        new_w = int(h * 9 / 16)
        x_center = w / 2
        clip = clip.crop(x1=x_center - new_w/2, y1=0, x2=x_center + new_w/2, y2=h)

    # I-save ang final video
    output_path = "final_output.mp4"
    clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")
    return output_path

def upload_to_youtube(video_file_path, title, description):
    creds_dict = json.loads(YOUTUBE_CREDENTIALS_JSON)
    creds = google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=creds_dict.get("refresh_token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        token_uri=creds_dict.get("token_uri")
    )
    
    youtube = build("youtube", "v3", credentials=creds)
    
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["stoicism", "narcissism", "mindset", "shorts", "unbreakablearmoredmind"],
            "categoryId": "27"  # Education / People & Blogs
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }
    
    media = MediaFileUpload(video_file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%.")
            
    print("Tagumpay na na-upload ang video sa YouTube!")

if __name__ == "__main__":
    print("Generating Stoic/Narcissism script with Gemini...")
    content = generate_content()
    print("Script generated successfully.")
    
    # Gamitin ang dynamic tags at title para sa channel niche
    title = "Unbreakable Mind #Shorts"
    description = f"{content}\n\n#Stoicism #Narcissism #Mindset #UnbreakableArmoredMind"
    
    print("Creating video...")
    video_path = create_video(content)
    
    print("Uploading to YouTube...")
    upload_to_youtube(video_path, title, description)
