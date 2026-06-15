"""
WorldCup YouTube Auto-Clipper
------------------------------
Espone un endpoint Flask /clip che:
1. Scarica un video YouTube con yt-dlp
2. Lo taglia in segmenti da 55 secondi (formato Shorts)
3. Carica ogni segmento sul canale YouTube configurato
4. Restituisce un JSON con i risultati

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

Variabili d'ambiente richieste (impostate su Render):
  YOUTUBE_CLIENT_ID
  YOUTUBE_CLIENT_SECRET
  YOUTUBE_REFRESH_TOKEN
  CHANNEL_ID
"""

import os
import json
import math
import subprocess
import tempfile
import logging
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

SHORTS_DURATION = 55          # secondi per ogni Short
MAX_CLIPS       = 10          # numero massimo di clip generate per video
UPLOAD_CATEGORY = "17"        # 17 = Sports su YouTube


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

    # Rinfresca automaticamente il token se scaduto
    if creds.expired or not creds.valid:
        creds.refresh(Request())

    return build("youtube", "v3", credentials=creds)


# ── Helper: scarica video con yt-dlp ─────────────────────────────────────────
def download_video(youtube_url: str, output_dir: str) -> str:
    """
    Scarica il video migliore disponibile (max 1080p) nella cartella output_dir.
    Restituisce il percorso completo del file scaricato.
    """
    output_template = os.path.join(output_dir, "source.%(ext)s")

    cmd = [
        "yt-dlp",
        "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-playlist",
        "--quiet",
        youtube_url,
    ]

    log.info(f"Download avviato: {youtube_url}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error(f"yt-dlp error: {result.stderr}")
        raise RuntimeError(f"Download fallito: {result.stderr[:300]}")

    # Trova il file scaricato nella cartella
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


# ── Helper: taglia il video in segmenti ──────────────────────────────────────
def clip_video(source_path: str, output_dir: str, segment_duration: int = SHORTS_DURATION) -> list:
    """
    Divide il video sorgente in segmenti da `segment_duration` secondi
    ottimizzati per YouTube Shorts (formato verticale 9:16, max 60s).
    Restituisce la lista dei percorsi dei clip generati.
    """
    total_duration = get_video_duration(source_path)
    num_clips = min(math.floor(total_duration / segment_duration), MAX_CLIPS)

    if num_clips == 0:
        # Video troppo corto: lo usiamo intero come unico Short
        num_clips = 1
        segment_duration = int(total_duration)

    log.info(f"Durata video: {total_duration:.0f}s — genero {num_clips} clip da {segment_duration}s")

    clip_paths = []

    for i in range(num_clips):
        start_time = i * segment_duration
        output_path = os.path.join(output_dir, f"clip_{i+1:02d}.mp4")

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", source_path,
            "-t", str(segment_duration),
            # Scala a 1080x1920 (verticale Shorts) con bande nere se necessario
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
            log.warning(f"ffmpeg clip {i+1} fallito: {result.stderr[:200]}")
            continue

        clip_paths.append(output_path)
        log.info(f"Clip {i+1}/{num_clips} generata: {output_path}")

    return clip_paths


# ── Helper: upload singolo clip su YouTube ────────────────────────────────────
def upload_clip(youtube, clip_path: str, title: str, description: str, tags: list, clip_index: int) -> dict:
    """
    Carica un singolo clip su YouTube come Short (unlisted durante il test,
    cambia privacy_status in 'public' quando sei pronto).
    Restituisce il dict con id e url del video pubblicato.
    """
    # Aggiungi numero clip al titolo se ci sono più segmenti
    clip_title = f"{title} #{clip_index}" if clip_index > 1 else title
    # Tronca a 100 caratteri (limite YouTube)
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
            # ⚠️  Cambia in "public" dopo aver verificato che tutto funziona
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        clip_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=5 * 1024 * 1024,  # chunk da 5MB
    )

    log.info(f"Upload in corso: {clip_title}")
    insert_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = insert_request.next_chunk()
        if status:
            log.info(f"  Progresso upload: {int(status.progress() * 100)}%")

    video_id  = response["id"]
    video_url = f"https://www.youtube.com/shorts/{video_id}"
    log.info(f"Pubblicato: {video_url}")

    return {"video_id": video_id, "url": video_url, "title": clip_title}


# ── Endpoint principale ───────────────────────────────────────────────────────
@app.route("/clip", methods=["POST"])
def clip_endpoint():
    """
    Riceve la richiesta da Creao, esegue download → clipping → upload.
    Body JSON atteso:
      {
        "youtube_url": "https://www.youtube.com/watch?v=...",
        "title": "Titolo ottimizzato SEO",
        "description": "Testo descrizione (opzionale)",
        "tags": ["tag1", "tag2"]
      }
    """
    data = request.get_json(force=True, silent=True)

    if not data or not data.get("youtube_url"):
        return jsonify({"error": "Campo 'youtube_url' obbligatorio"}), 400

    youtube_url = data["youtube_url"]
    title       = data.get("title", "FIFA World Cup 2026 Highlights #Shorts")
    description = data.get("description", "")
    tags        = data.get("tags", ["FIFA", "WorldCup2026", "goals", "highlights", "Shorts"])

    uploaded_videos = []
    errors          = []

    # Usa una cartella temporanea che viene pulita automaticamente
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # 1. Download
            source_path = download_video(youtube_url, tmpdir)

            # 2. Clipping
            clip_paths = clip_video(source_path, tmpdir)

            if not clip_paths:
                return jsonify({"error": "Nessuna clip generata dal video"}), 500

            # 3. Upload
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
    """Endpoint di verifica: Render lo usa per sapere che il servizio è vivo."""
    return jsonify({"status": "ok", "service": "worldcup-clipper"}), 200


# ── Avvio ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
