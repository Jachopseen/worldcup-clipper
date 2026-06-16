"""
WorldCup YouTube Auto-Clipper — v3
------------------------------------
Novità rispetto a v2:
  - Gemini Vision analizza il video e identifica i timestamp esatti
    dei momenti salienti (gol, esultanze, azioni spettacolari)
  - FFmpeg taglia esattamente quei momenti, non meccanicamente ogni 55s
  - 3 cluster distinti per ogni partita:
      CLUSTER 1: highlights partita recente (momenti salienti reali)
      CLUSTER 2: gol storico iconico sfide passate tra le due nazionali
      CLUSTER 3: goal/skill del campione più famoso della squadra

Variabili d'ambiente richieste su Render:
  YOUTUBE_CLIENT_ID
  YOUTUBE_CLIENT_SECRET
  YOUTUBE_REFRESH_TOKEN
  CHANNEL_ID
  YT_COOKIES          — contenuto del file cookies.txt in formato Netscape
  GEMINI_API_KEY      — NUOVO: chiave API Google AI Studio (gratuita)
"""

import os
import json
import subprocess
import tempfile
import logging
import urllib.request
import random
import re
import time
import google.generativeai as genai
from flask import Flask, request, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Configurazione ─────────────────────────────────────────────────────────────
CLIENT_ID      = os.environ.get("YOUTUBE_CLIENT_ID")
CLIENT_SECRET  = os.environ.get("YOUTUBE_CLIENT_SECRET")
REFRESH_TOKEN  = os.environ.get("YOUTUBE_REFRESH_TOKEN")
CHANNEL_ID     = os.environ.get("CHANNEL_ID")
YT_COOKIES     = os.environ.get("YT_COOKIES", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

UPLOAD_CATEGORY = "17"       # Sports
MUSIC_VOLUME    = 0.28       # volume musica sottofondo (28%)
MAX_CLIPS       = 5          # massimo clip per video sorgente
SHORT_DURATION  = 55         # durata massima Short in secondi

# Brani royalty-free energici per highlights sportivi
MUSIC_TRACKS = [
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
]

# Configura Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# GEMINI VISION — analisi intelligente dei momenti salienti
# ─────────────────────────────────────────────────────────────────────────────

def analyze_video_with_gemini(video_path: str, cluster: str) -> list:
    """
    Manda il video a Gemini Flash e chiede di identificare i timestamp
    esatti dei momenti più spettacolari in base al cluster richiesto.

    Restituisce una lista di dict con:
      [{"start": "02:14", "end": "02:58", "description": "Gol di testa spettacolare"}, ...]

    Se Gemini non è disponibile o fallisce, ritorna lista vuota
    e il sistema usa il fallback di taglio meccanico.
    """
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY non configurata — uso taglio meccanico")
        return []

    # Prompt diverso per ogni cluster
    prompts = {
        "highlights": """
            Analyze this football/soccer match highlights video carefully.
            Identify the TOP 3-5 most spectacular moments: goals, amazing saves,
            key celebrations, penalty kicks, VAR decisions, or outstanding skills.
            For each moment, give me the EXACT timestamps where it starts
            (2-3 seconds before the action) and ends (2-3 seconds after the celebration).
            Each clip should be between 20 and 55 seconds long.
            Respond ONLY with a valid JSON array, no other text:
            [{"start": "MM:SS", "end": "MM:SS", "description": "brief description in English"}]
        """,
        "historical": """
            Analyze this football/soccer video about historic matches between two national teams.
            Identify the TOP 3 most iconic and memorable moments: legendary goals,
            famous celebrations, historic victories, or legendary player moments.
            For each moment, give me the EXACT timestamps where it starts
            (2-3 seconds before the action) and ends (2-3 seconds after the celebration).
            Each clip should be between 20 and 55 seconds long.
            Respond ONLY with a valid JSON array, no other text:
            [{"start": "MM:SS", "end": "MM:SS", "description": "brief description in English"}]
        """,
        "player": """
            Analyze this football/soccer video about a star player's goals and skills.
            Identify the TOP 3-4 most impressive and viral-worthy moments:
            the most spectacular goals, unbelievable skills, or iconic celebrations.
            Prioritize moments that would make someone stop scrolling on social media.
            For each moment, give me the EXACT timestamps where it starts
            (2-3 seconds before the action) and ends (2-3 seconds after the celebration).
            Each clip should be between 20 and 55 seconds long.
            Respond ONLY with a valid JSON array, no other text:
            [{"start": "MM:SS", "end": "MM:SS", "description": "brief description in English"}]
        """
    }

    prompt = prompts.get(cluster, prompts["highlights"])

    try:
        log.info(f"Gemini Vision: analisi video per cluster '{cluster}'...")

        model = genai.GenerativeModel("gemini-2.5-flash-lite")  # usa Flash-Lite (1500 req/giorno gratis)

        # Carica il file video su Gemini File API
        log.info("Upload video su Gemini File API...")
        video_file = genai.upload_file(path=video_path, mime_type="video/mp4")

        # Aspetta che il file sia processato
        while video_file.state.name == "PROCESSING":
            log.info("  Gemini sta processando il video...")
            time.sleep(5)
            video_file = genai.get_file(video_file.name)

        if video_file.state.name == "FAILED":
            raise RuntimeError("Gemini: elaborazione video fallita")

        log.info("Video processato — invio prompt a Gemini...")
        response = model.generate_content([video_file, prompt])

        # Pulisci la risposta e parsifica il JSON
        raw = response.text.strip()
        # Rimuovi eventuali backtick markdown
        raw = re.sub(r"```json|```", "", raw).strip()

        moments = json.loads(raw)
        log.info(f"Gemini ha identificato {len(moments)} momenti salienti")

        # Cancella il file da Gemini per non occupare spazio
        genai.delete_file(video_file.name)

        return moments

    except Exception as e:
        log.error(f"Gemini Vision fallito: {e} — uso taglio meccanico come fallback")
        return []


def timestamp_to_seconds(ts: str) -> float:
    """Converte 'MM:SS' in secondi float."""
    parts = ts.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD VIDEO
# ─────────────────────────────────────────────────────────────────────────────

def write_cookies_file(tmpdir: str) -> str | None:
    if not YT_COOKIES:
        log.warning("YT_COOKIES non configurata")
        return None
    cookies_path = os.path.join(tmpdir, "cookies.txt")
    with open(cookies_path, "w") as f:
        f.write(YT_COOKIES)
    return cookies_path


def download_video(youtube_url: str, output_dir: str, cookies_path: str | None) -> str:
    output_template = os.path.join(output_dir, "source.%(ext)s")

    cmd = [
        "yt-dlp",
        "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_template,
        "--no-playlist",
        "--quiet",
        # Usa Android + iOS come client: non richiedono PO Token a differenza di web
        "--extractor-args", "youtube:player_client=android,ios",
        "--sleep-interval", "2",
        "--max-sleep-interval", "5",
        # Aggiorna yt-dlp internamente prima di ogni download per avere sempre
        # le ultime patch anti-blocco di YouTube
        "--no-update",
    ]

    if cookies_path:
        cmd += ["--cookies", cookies_path]

    cmd.append(youtube_url)

    log.info(f"Download: {youtube_url}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp fallito: {result.stderr[:400]}")

    for f in os.listdir(output_dir):
        if f.startswith("source") and f.endswith(".mp4"):
            return os.path.join(output_dir, f)

    raise RuntimeError("File video non trovato dopo il download")


def get_video_duration(filepath: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", filepath,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


# ─────────────────────────────────────────────────────────────────────────────
# TAGLIO INTELLIGENTE CON GEMINI + FALLBACK MECCANICO
# ─────────────────────────────────────────────────────────────────────────────

def clip_video_intelligent(source_path: str, output_dir: str,
                           music_path: str | None, cluster: str,
                           title: str) -> list:
    """
    Taglia il video in modo intelligente usando i timestamp di Gemini.
    Se Gemini non è disponibile, usa il fallback meccanico ogni 55 secondi.
    Aggiunge musica di sottofondo e formattazione verticale per Shorts.
    """
    total_duration = get_video_duration(source_path)

    # 1. Chiedi a Gemini i momenti salienti
    moments = analyze_video_with_gemini(source_path, cluster)

    # 2. Se Gemini ha risposto, usa i suoi timestamp
    if moments:
        segments = []
        for m in moments[:MAX_CLIPS]:
            start_sec = timestamp_to_seconds(m.get("start", "0:00"))
            end_sec   = timestamp_to_seconds(m.get("end", "0:55"))
            duration  = min(end_sec - start_sec, SHORT_DURATION)

            if duration < 10:  # clip troppo corta, salta
                continue

            segments.append({
                "start":       start_sec,
                "duration":    duration,
                "description": m.get("description", ""),
            })
        log.info(f"Gemini: {len(segments)} segmenti identificati")
    else:
        # Fallback: taglio meccanico ogni 55 secondi
        log.info("Fallback: taglio meccanico ogni 55 secondi")
        num_clips = min(int(total_duration // SHORT_DURATION), MAX_CLIPS)
        if num_clips == 0:
            num_clips = 1
        segments = [
            {"start": i * SHORT_DURATION, "duration": SHORT_DURATION, "description": ""}
            for i in range(num_clips)
        ]

    # 3. Genera ogni clip con FFmpeg
    clip_paths = []

    for idx, seg in enumerate(segments, start=1):
        start    = seg["start"]
        duration = seg["duration"]
        out_path = os.path.join(output_dir, f"clip_{idx:02d}.mp4")

        if music_path:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", source_path,
                "-stream_loop", "-1",
                "-i", music_path,
                "-t", str(duration),
                "-filter_complex",
                f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
                f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2[v];"
                f"[0:a]volume=1.0[a_orig];"
                f"[1:a]volume={MUSIC_VOLUME}[a_music];"
                f"[a_orig][a_music]amix=inputs=2:duration=first[a_out]",
                "-map", "[v]",
                "-map", "[a_out]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", source_path,
                "-t", str(duration),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path,
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.warning(f"FFmpeg clip {idx} fallito: {result.stderr[:200]}")
            continue

        clip_paths.append({
            "path":        out_path,
            "description": seg.get("description", ""),
        })
        log.info(f"Clip {idx}/{len(segments)} pronta")

    return clip_paths


# ─────────────────────────────────────────────────────────────────────────────
# MUSICA
# ─────────────────────────────────────────────────────────────────────────────

def download_music(tmpdir: str) -> str | None:
    tracks = MUSIC_TRACKS.copy()
    random.shuffle(tracks)
    music_path = os.path.join(tmpdir, "music.mp3")
    for url in tracks:
        try:
            urllib.request.urlretrieve(url, music_path)
            return music_path
        except Exception as e:
            log.warning(f"Musica fallita ({url}): {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# YOUTUBE UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

def get_youtube_client():
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


def upload_clip(youtube, clip_path: str, title: str, description: str,
                tags: list, clip_index: int, moment_description: str = "") -> dict:

    # Arricchisci il titolo con la descrizione del momento se disponibile
    if moment_description and clip_index == 1:
        clip_title = f"{title} — {moment_description}"[:100]
    elif clip_index > 1:
        clip_title = f"{title} #{clip_index}"[:100]
    else:
        clip_title = title[:100]

    body = {
        "snippet": {
            "title":           clip_title,
            "description":     description or f"{title}\n\n#Shorts #FIFA #WorldCup2026 #Football",
            "tags":            tags or ["FIFA", "WorldCup2026", "goals", "highlights", "Shorts", "football"],
            "categoryId":      UPLOAD_CATEGORY,
            "defaultLanguage": "en",
        },
        "status": {
            # ⚠️  Cambia in "public" quando sei pronto per andare live
            "privacyStatus":            "private",
            "selfDeclaredMadeForKids":  False,
        },
    }

    media = MediaFileUpload(clip_path, mimetype="video/mp4", resumable=True, chunksize=5 * 1024 * 1024)

    log.info(f"Upload: {clip_title}")
    insert_req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = insert_req.next_chunk()
        if status:
            log.info(f"  {int(status.progress() * 100)}%")

    video_id = response["id"]
    url = f"https://www.youtube.com/shorts/{video_id}"
    log.info(f"Pubblicato: {url}")
    return {"video_id": video_id, "url": url, "title": clip_title}


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT PRINCIPALE
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/clip", methods=["POST"])
def clip_endpoint():
    """
    Body JSON atteso:
    {
      "youtube_url":  "https://www.youtube.com/watch?v=...",
      "title":        "Titolo ottimizzato SEO",
      "description":  "Testo descrizione (opzionale)",
      "tags":         ["FIFA", "WorldCup2026", "goals"],
      "cluster":      "highlights" | "historical" | "player"
    }
    """
    data = request.get_json(force=True, silent=True)

    if not data or not data.get("youtube_url"):
        return jsonify({"error": "Campo 'youtube_url' obbligatorio"}), 400

    youtube_url = data["youtube_url"]
    title       = data.get("title",       "FIFA World Cup 2026 #Shorts")
    description = data.get("description", "")
    tags        = data.get("tags",        ["FIFA", "WorldCup2026", "goals", "highlights", "Shorts"])
    cluster     = data.get("cluster",     "highlights")   # default: highlights

    if cluster not in ("highlights", "historical", "player"):
        cluster = "highlights"

    uploaded_videos = []
    errors          = []

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            cookies_path = write_cookies_file(tmpdir)
            music_path   = download_music(tmpdir)
            source_path  = download_video(youtube_url, tmpdir, cookies_path)

            clips = clip_video_intelligent(source_path, tmpdir, music_path, cluster, title)

            if not clips:
                return jsonify({"error": "Nessuna clip generata"}), 500

            youtube = get_youtube_client()

            for idx, clip_info in enumerate(clips, start=1):
                try:
                    result = upload_clip(
                        youtube,
                        clip_info["path"],
                        title,
                        description,
                        tags,
                        idx,
                        clip_info.get("description", ""),
                    )
                    uploaded_videos.append(result)
                except Exception as e:
                    log.error(f"Upload clip {idx} fallito: {e}")
                    errors.append({"clip": idx, "error": str(e)})

        except Exception as e:
            log.error(f"Errore pipeline: {e}")
            return jsonify({"error": str(e)}), 500

    body = {
        "status":         "ok" if uploaded_videos else "error",
        "cluster":        cluster,
        "source_url":     youtube_url,
        "clips_uploaded": len(uploaded_videos),
        "videos":         uploaded_videos,
    }
    if errors:
        body["errors"] = errors

    return jsonify(body), 200


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    gemini_status = "configured" if GEMINI_API_KEY else "missing"
    return jsonify({
        "status":  "ok",
        "service": "worldcup-clipper-v3",
        "gemini":  gemini_status,
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
