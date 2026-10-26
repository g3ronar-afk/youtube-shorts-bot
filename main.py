import time
import random

def generate_content():
    prompt = (
        "You are a master of Stoic philosophy and dark psychology. "
        "Write a powerful, cinematic, and deeply impactful message in STRICTLY ENGLISH about Stoicism, mental toughness, "
        "or understanding the manipulative behaviors of a narcissist. "
        "This is for a YouTube Short. Start with a strong 3-second hook. "
        "Keep it strictly between 30 to 45 words. DO NOT use Tagalog or any other language.\n"
        "Format exactly like this:\n"
        "Title: [Catchy Short Title]\n"
        "Script: [The message or quote]"
    )
    
    max_retries = 5
    base_delay = 2  # Start with 2 seconds
    
    for attempt in range(max_retries):
        try:
            # Gumamit ng 3.6-flash dahil mas stable ito
            response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
            return response.text.strip()
        except genai_errors.APIError as e:
            # Kung 503 (UNAVAILABLE) o 429 (RATE LIMIT), mag-retry
            if e.code in [503, 429]:
                if attempt == max_retries - 1:
                    print(f"[Gemini ERROR] Naubos ang retries pagkatapos ng {max_retries} attempts. Huling error: {e.code}")
                    raise
                # Exponential backoff + jitter (random delay)
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"[Gemini] Error {e.code}. Maghihintay ng {delay:.1f} seconds bago mag-retry (attempt {attempt+1}/{max_retries})...")
                time.sleep(delay)
            else:
                # Kung ibang error (tulad ng 400, 401, 403), huwag nang i-retry
                print(f"[Gemini ERROR] code={e.code} message={e.message}")
                raise
