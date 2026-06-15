"""
WorldCup YouTube Auto-Clipper — v2
------------------------------------
Novità rispetto a v1:
  - Supporto cookie YouTube per bypassare il blocco su server cloud
  - Musica di sottofondo automatica (miscelata con FFmpeg)
  - Musica a volume ridotto (30%) per non coprire l'audio originale

Endpoint:
  POST /clip
  Body JSON:
    {
      "youtube_url": "https://www.youtube.com/watch?v=XXXXX",
      "title": "Titolo del video",
      "description": "Descrizione opzionale",
      "tags": ["FIFA", "WorldCup2026", "goals"]
    }

  GET /health
    Verifica che il servizio sia attivo

Variabili d'ambiente richieste su Render:
  YOUTUBE_CLIENT_ID
  YOUTUBE_CLIENT_SECRET
  YOUTUBE_REFRESH_TOKEN
  CHANNEL_ID
  YT_COOKIES          ← NUOVO: contenuto del file cookies.txt in formato Netscape
"""

import os
import json
import math
import subprocess
import tempfile
import logging
import base64
from flask import Flask, request, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Configurazione da variabili d'ambiente ────────────────────────────────────
CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")
CHANNEL_ID    = os.environ.get("CHANNEL_ID")
YT_COOKIES    = os.environ.get("YT_COOKIES", "")   # contenuto cookies.txt

SHORTS_DURATION = 55          # secondi per ogni Short
MAX_CLIPS       = 10          # numero massimo di clip per video
UPLOAD_CATEGORY = "17"        # 17 = Sports su YouTube
MUSIC_VOLUME    = 0.30        # volume musica (0.30 = 30% rispetto all'audio originale)

# URL pubblici di brani royalty-free dalla YouTube Audio Library
# Questi sono file MP3 scaricabili liberamente e usabili senza copyright
MUSIC_TRACKS = [
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
]


# ── Helper: scrivi cookies su file temporaneo ─────────────────────────────────
def write_cookies_file(tmpdir: str) -> str | None:
    """
    Scrive il contenuto della variabile d'ambiente YT_COOKIES in un file
    temporaneo in formato Netscape (quello accettato da yt-dlp).
    Restituisce il percorso del file, o None se i cookie non sono configurati.
    """
    if not YT_COOKIES:
        log.warning("YT_COOKIES non configurata — download potrebbe fallire su cloud")
        return None

    cookies_path = os.path.join(tmpdir, "cookies.txt")
    with open(cookies_path, "w") as f:
        f.write(YT_COOKIES)
    log.info("File cookies.txt scritto correttamente")
    return cookies_path


# ── Helper: scarica brano musicale royalty-free ───────────────────────────────
def download_music(tmpdir: str) -> str | None:
    """
    Scarica uno dei brani royalty-free configurati in MUSIC_TRACKS.
    Prova ogni URL in sequenza finché uno funziona.
    Restituisce il percorso del file MP3, o None se tutti falliscono.
    """
    import random
    import urllib.request

    tracks = MUSIC_TRACKS.copy()
    random.shuffle(tracks)   # varia il brano ad ogni run

    music_path = os.path.join(tmpdir, "background_music.mp3")

    for url in tracks:
        try:
            log.info(f"Download musica: {url}")
            urllib.request.urlretrieve(url, music_path)
            log.info("Musica scaricata correttamente")
            return music_path
        except Exception as e:
            log.warning(f"Download musica fallito ({url}): {e}")
            continue

    log.warning("Nessun brano musicale disponibile — video senza musica")
    return None


# ── Helper: ottieni credenziali YouTube OAuth ─────────────────────────────────
def get_youtube_client():
    """Restituisce un client YouTube API autenticato tramite OAuth2."""
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        raise RuntimeError(
            "Variabili d'ambiente mancanti: "
            "YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN"
        )

    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    if creds.expired or not creds.valid:
        creds.refresh(Request())

    return build("youtube", "v3", credentials=creds)


# ── Helper: scarica video con yt-dlp ─────────────────────────────────────────
def download_video(youtube_url: str, output_dir: str, cookies_path: str | None) -> str:
    """
    Scarica il video da YouTube usando yt-dlp.
    Se cookies_path è disponibile, lo passa a yt-dlp per bypassare il blocco cloud.
    """
    output_template = os.path.join(output_dir, "source.%(ext)s")

    cmd = [
        "yt-dlp",
        "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-playlist",
        "--quiet",
        # Simula un browser reale per evitare blocchi
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "--extractor-args", "youtube:player_client=web",
        "--sleep-interval", "2",      # pausa tra le richieste per non sembrare un bot
        "--max-sleep-interval", "5",
    ]

    # Aggiungi i cookie se disponibili
    if cookies_path:
        cmd += ["--cookies", cookies_path]
        log.info("yt-dlp: uso cookies per autenticazione")

    cmd.append(youtube_url)

    log.info(f"Download avviato: {youtube_url}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error(f"yt-dlp error: {result.stderr}")
        raise RuntimeError(f"Download fallito: {result.stderr[:400]}")

    for f in os.listdir(output_dir):
        if f.startswith("source") and f.endswith(".mp4"):
            path = os.path.join(output_dir, f)
            log.info(f"File scaricato: {path}")
            return path

    raise RuntimeError("File video non trovato dopo il download")


# ── Helper: ottieni durata del video ─────────────────────────────────────────
def get_video_duration(filepath: str) -> float:
    """Usa ffprobe per leggere la durata del video in secondi."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        filepath,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr[:200]}")

    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


# ── Helper: taglia il video e aggiunge musica ────────────────────────────────
def clip_video(source_path: str, output_dir: str, music_path: str | None,
               segment_duration: int = SHORTS_DURATION) -> list:
    """
    Divide il video in segmenti da `segment_duration` secondi.
    Se music_path è disponibile, aggiunge la musica come sottofondo a volume ridotto.
    Output: formato verticale 1080x1920 ottimizzato per YouTube Shorts.
    """
    total_duration = get_video_duration(source_path)
    num_clips = min(math.floor(total_duration / segment_duration), MAX_CLIPS)

    if num_clips == 0:
        num_clips = 1
        segment_duration = int(total_duration)

    log.info(f"Durata: {total_duration:.0f}s — genero {num_clips} clip da {segment_duration}s — musica: {'sì' if music_path else 'no'}")

    clip_paths = []

    for i in range(num_clips):
        start_time = i * segment_duration
        output_path = os.path.join(output_dir, f"clip_{i+1:02d}.mp4")

        if music_path:
            # ── Con musica ───────────────────────────────────────────────────
            # Usa FFmpeg per:
            # 1. Tagliare il segmento video
            # 2. Scalare a formato verticale Shorts (1080x1920)
            # 3. Miscelare la musica a volume ridotto (MUSIC_VOLUME)
            #    mantenendo l'audio originale a volume pieno
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", source_path,               # input 1: video sorgente
                "-stream_loop", "-1",            # ripeti la musica se più corta del clip
                "-i", music_path,                # input 2: brano musicale
                "-t", str(segment_duration),
                "-filter_complex",
                # Scala il video a formato verticale
                f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
                f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2[v];"
                # Audio originale a volume pieno + musica a MUSIC_VOLUME
                f"[0:a]volume=1.0[a_orig];"
                f"[1:a]volume={MUSIC_VOLUME}[a_music];"
                f"[a_orig][a_music]amix=inputs=2:duration=first[a_out]",
                "-map", "[v]",
                "-map", "[a_out]",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                output_path,
            ]
        else:
            # ── Senza musica (fallback) ──────────────────────────────────────
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", source_path,
                "-t", str(segment_duration),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                output_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.warning(f"FFmpeg clip {i+1} fallito: {result.stderr[:300]}")
            continue

        clip_paths.append(output_path)
        log.info(f"Clip {i+1}/{num_clips} pronta: {output_path}")

    return clip_paths


# ── Helper: upload singolo clip su YouTube ────────────────────────────────────
def upload_clip(youtube, clip_path: str, title: str, description: str,
                tags: list, clip_index: int) -> dict:
    """Carica un clip su YouTube come Short."""
    clip_title = f"{title} #{clip_index}" if clip_index > 1 else title
    clip_title = clip_title[:100]

    body = {
        "snippet": {
            "title": clip_title,
            "description": description or f"{title}\n\n#Shorts #FIFA #WorldCup2026",
            "tags": tags or ["FIFA", "WorldCup2026", "goals", "highlights", "Shorts"],
            "categoryId": UPLOAD_CATEGORY,
            "defaultLanguage": "en",
        },
        "status": {
            # ⚠️ Cambia in "public" dopo aver verificato che tutto funziona
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        clip_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=5 * 1024 * 1024,
    )

    log.info(f"Upload: {clip_title}")
    insert_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = insert_request.next_chunk()
        if status:
            log.info(f"  Upload progresso: {int(status.progress() * 100)}%")

    video_id  = response["id"]
    video_url = f"https://www.youtube.com/shorts/{video_id}"
    log.info(f"Pubblicato: {video_url}")
    return {"video_id": video_id, "url": video_url, "title": clip_title}


# ── Endpoint principale ───────────────────────────────────────────────────────
@app.route("/clip", methods=["POST"])
def clip_endpoint():
    data = request.get_json(force=True, silent=True)

    if not data or not data.get("youtube_url"):
        return jsonify({"error": "Campo 'youtube_url' obbligatorio"}), 400

    youtube_url = data["youtube_url"]
    title       = data.get("title", "FIFA World Cup 2026 Highlights #Shorts")
    description = data.get("description", "")
    tags        = data.get("tags", ["FIFA", "WorldCup2026", "goals", "highlights", "Shorts"])

    uploaded_videos = []
    errors          = []

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # 1. Scrivi cookies
            cookies_path = write_cookies_file(tmpdir)

            # 2. Scarica musica royalty-free
            music_path = download_music(tmpdir)

            # 3. Scarica video
            source_path = download_video(youtube_url, tmpdir, cookies_path)

            # 4. Taglia in clip con musica
            clip_paths = clip_video(source_path, tmpdir, music_path)

            if not clip_paths:
                return jsonify({"error": "Nessuna clip generata"}), 500

            # 5. Upload su YouTube
            youtube = get_youtube_client()

            for idx, clip_path in enumerate(clip_paths, start=1):
                try:
                    result = upload_clip(youtube, clip_path, title, description, tags, idx)
                    uploaded_videos.append(result)
                except Exception as e:
                    log.error(f"Upload clip {idx} fallito: {e}")
                    errors.append({"clip": idx, "error": str(e)})

        except Exception as e:
            log.error(f"Errore pipeline: {e}")
            return jsonify({"error": str(e)}), 500

    response_body = {
        "status":         "ok" if uploaded_videos else "error",
        "source_url":     youtube_url,
        "clips_uploaded": len(uploaded_videos),
        "videos":         uploaded_videos,
    }
    if errors:
        response_body["errors"] = errors

    return jsonify(response_body), 200


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "worldcup-clipper-v2"}), 200


# ── Avvio ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
