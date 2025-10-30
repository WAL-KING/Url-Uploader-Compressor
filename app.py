import os
import asyncio
import time
import requests
import ffmpeg
import yt_dlp
import libtorrent as lt
import subprocess
import json
import uuid
import cv2  # ajouté
import shutil
from yt_dlp import YoutubeDL
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from pathlib import Path
import sqlite3
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError

# ==== CONFIGURATION ====
API_ID = 24110167
API_HASH = "ce324042b2a43e39ede63c7dbb8e301d"
BOT_TOKEN = "8063231211:AAG9SPpqCbXxoblwmgXINhjQSZs4KZff73Y"

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
def clean_downloads():
    """Supprime tous les fichiers du dossier downloads."""
    for p in DOWNLOAD_DIR.glob("*"):
        try:
            p.unlink()
        except Exception:
            pass

bot = TelegramClient("full_video_bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- Stats utilisateurs (JSON) & logging admin ---
STATS_FILE = "user_stats.json"

def load_stats():
    if not os.path.exists(STATS_FILE):
        return {}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_stats(stats):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Erreur save_stats:", e)

def increment_user_usage(user):
    """
    user: telethon.types.User
    On utilise le username si disponible sinon prenom.
    """
    stats = load_stats()
    key = user.username if getattr(user, "username", None) else (user.first_name or f"id_{user.id}")
    entry = stats.get(key, {"count": 0, "id": user.id, "name": f"@{user.username}" if user.username else user.first_name})
    entry["count"] = entry.get("count", 0) + 1
    stats[key] = entry
    save_stats(stats)

async def send_usage_log(user, video_link: str = None, srt_link: str = None, command: str = None):
    """
    Envoie un message à ADMIN_USERNAME avec liens cliquables 'Here' et 'ICI'.
    """
    try:
        uname = f"@{user.username}" if getattr(user, "username", None) else (user.first_name or str(user.id))
        vlink_md = f"[Here]({video_link})" if video_link else "—"
        slink_md = f"[ICI]({srt_link})" if srt_link else "—"
        text = (
            f"👤 Nouvel usage du bot\n"
            f"👥 Utilisateur : {uname}\n"
            f"📹 Lien vidéo : {vlink_md}\n"
            f"📜 Lien sous-titres : {slink_md}\n"
            f"🧭 Commande : {command or '—'}"
        )
        # envoi en Markdown pour rendre les liens cliquables
        await bot.send_message(ADMIN_USERNAME, text, parse_mode='md')
    except Exception as e:
        print("Erreur send_usage_log:", e)
        
# ==== BASE DE DONNÉES UTILISATEURS ====

DB_PATH = "users.db"

# Création auto si la DB n'existe pas
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT
)
""")
conn.commit()

ADMIN_USERNAME = "hokagesama02"

def add_user(user):
    """Ajoute un utilisateur dans la base si pas encore présent."""
    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()
        c.execute("SELECT id FROM users WHERE id = ?", (user.id,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO users (id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                (user.id, user.username, user.first_name, user.last_name),
            )
            db.commit()

async def log_to_admin(bot, text: str):
    """Envoie un log à l’admin."""
    try:
        admin = await bot.get_entity(ADMIN_USERNAME)
        await bot.send_message(admin, text)
    except Exception as e:
        print(f"Erreur log admin : {e}")
        
# ==== SYSTÈME D’ABONNEMENT OBLIGATOIRE ====

# 🔗 Mets ici le lien de ton dossier ou ton lien d’invitation (où se trouvent tous tes canaux)
JOIN_LINK = "https://t.me/addlist/HqIDW263Z5M0ZDg0"  # 🔧 à modifier

# 📢 Liste des canaux où l’utilisateur doit être abonné (remplace par les tiens)
REQUIRED_CHANNELS = [
    "https://t.me/mesjeuxemulator",
    "https://t.me/mesactujeuxvideo",
    "https://t.me/mesjeuxvideopc",
    "https://t.me/mesjeuxandroid",
    "https://t.me/mesjeuxvideo"
]

async def is_user_subscribed(user_id):
    """Vérifie si l’utilisateur est membre de tous les canaux obligatoires."""
    for channel in REQUIRED_CHANNELS:
        try:
            entity = await bot.get_entity(channel)
            await bot(GetParticipantRequest(entity, user_id))
        except UserNotParticipantError:
            return False
        except Exception:
            continue
    return True

async def check_subscription(event):
    """Vérifie l’abonnement avant d’exécuter les commandes."""
    user = await event.get_sender()
    subscribed = await is_user_subscribed(user.id)
    if not subscribed:
        buttons = [[Button.url("📢 Rejoindre les canaux", JOIN_LINK)]]
        await event.reply(
            "🚫 Tu n’es pas encore abonné à nos canaux pour utiliser le bot.\n\n"
            "Appuie sur le bouton ci-dessous pour les rejoindre 👇",
            buttons=buttons
        )
        return False
    return True
    
# ==== Barre de progression intelligente ====

# Variables globales pour conserver l’état
last_percent = 0
last_downloaded = 0
last_total = 0
last_speed = 0
last_eta = 0
detailed_mode = False  # État du mode d’affichage (normal / détaillé)

def make_progress_bar(percent: int) -> str:
    """Crée la barre visuelle selon le pourcentage."""
    bar_length = 20
    completed = int((percent / 100) * bar_length)
    remaining = bar_length - completed
    return f"{'█' * completed}{'░' * remaining}"

async def update_progress(
    msg,
    phase: str,
    percent: int,
    downloaded: float = 0,
    total: float = 0,
    speed: float = 0,
    eta: float = 0
):
    """
    Met à jour le message avec ou sans détails selon l’état du mode.
    """
    global last_percent, last_downloaded, last_total, last_speed, last_eta, detailed_mode
    last_percent = percent
    last_downloaded = downloaded
    last_total = total
    last_speed = speed
    last_eta = eta

    bar = make_progress_bar(percent)

    if not detailed_mode:
        # Vue simplifiée
        text = f"📥 {phase} en cours : {percent}/100%"
        buttons = [[Button.inline("Plus d'infos ℹ️", data=b'info_details')]]
    else:
        # Vue détaillée
        text = (
            f"📥 **{phase}...**\n\n"
            f"{bar} {percent}%\n\n"
            f"🗂️ {downloaded / (1024*1024):.2f} / {total / (1024*1024):.2f} MB\n"
            f"🚀 {speed / (1024*1024):.2f} MB/s\n"
            f"⏱️ ETA: {eta:.1f}s"
        )
        buttons = [[Button.inline("Masquer 🔙", data=b'info_hide')]]

    try:
        await msg.edit(text, buttons=buttons)
    except Exception:
        pass  # Ignore les erreurs de flood

async def handle_progress_callback(d, msg):
    """Hook appelé par yt-dlp pour mettre à jour la progression."""
    if d['status'] == 'downloading':
        downloaded = d.get('downloaded_bytes', 0)
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
        percent = int((downloaded / total) * 100)
        speed = d.get('speed', 0)
        eta = d.get('eta', 0)
        await update_progress(msg, "Téléchargement", percent, downloaded, total, speed, eta)

# ==== Gestion des boutons "Plus d’infos" / "Masquer" ====

@bot.on(events.CallbackQuery(pattern=b'info_details'))
async def show_details(event):
    """Affiche la version détaillée."""
    global detailed_mode
    detailed_mode = True
    await event.answer("ℹ️ Affichage détaillé activé.", alert=False)
    try:
        await update_progress(
            event.message,
            "Téléchargement",
            last_percent,
            last_downloaded,
            last_total,
            last_speed,
            last_eta
        )
    except Exception:
        pass

@bot.on(events.CallbackQuery(pattern=b'info_hide'))
async def hide_details(event):
    """Masque les détails et revient à la vue simple."""
    global detailed_mode
    detailed_mode = False
    await event.answer("🔙 Détails masqués.", alert=False)
    try:
        await update_progress(
            event.message,
            "Téléchargement",
            last_percent,
            last_downloaded,
            last_total,
            last_speed,
            last_eta
        )
    except Exception:
        pass



# ==== Téléchargement Magnet ====
def download_magnet(magnet_link: str):
    ses = lt.session()
    ses.listen_on(6881, 6891)
    params = {'save_path': str(DOWNLOAD_DIR), 'storage_mode': lt.storage_mode_t(2)}
    handle = lt.add_magnet_uri(ses, magnet_link, params)
    print("⏳ Téléchargement du torrent...")

    while not handle.has_metadata():
        time.sleep(1)
    while handle.status().state != lt.torrent_status.seeding:
        time.sleep(1)

    for f in handle.get_torrent_info().files():
        path = DOWNLOAD_DIR / f.path
        if path.suffix in [".mp4", ".mkv", ".avi"]:
            return str(path)
    return None

# ==== Téléchargement des sous-titres ====
def download_subtitle(url: str, user_id: int, uid: str = None):
    if uid is None:
        uid = uuid.uuid4().hex
    sub_path = str(DOWNLOAD_DIR / f"subtitle_{user_id}_{uid}.srt")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    with open(sub_path, "wb") as f:
        f.write(r.content)
    return sub_path

# ==== Simulation de progression (pour les étapes sans yt-dlp) ====
async def simulate_progress(msg, phase="Traitement", duration=20):
    for i in range(0, 101, 5):
        await update_progress(msg, phase, i)
        await asyncio.sleep(duration / 20)

# ==== Compression ====
async def compress_video(input_path: str, output_path: str, msg):
    await simulate_progress(msg, "Compression", duration=25)
    (
        ffmpeg
        .input(input_path)
        .output(
            output_path,
            vcodec='libx264',
            acodec='aac',
            crf=28,
            preset='veryfast',
            movflags='+faststart',
            pix_fmt='yuv420p'
        )
        .run(overwrite_output=True)
    )


def generate_thumbnail(video_path: str, thumb_path: str):
    """
    Extrait une miniature à 10% de la durée de la vidéo.
    """
    try:
        # obtenir durée totale
        probe_data = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", video_path
        ])
        metadata = json.loads(probe_data)
        duration = float(metadata["format"].get("duration", 0))
        capture_time = max(duration * 0.10, 1)

        subprocess.run([
            "ffmpeg", "-y", "-ss", str(capture_time), "-i", video_path,
            "-vframes", "1", "-q:v", "2", thumb_path
        ], check=True)
        return thumb_path
    except Exception as e:
        print(f"Erreur génération miniature : {e}")
        return None


def get_video_attributes(video_path: str):
    """
    Extrait la durée, largeur et hauteur pour Telegram.
    """
    try:
        probe = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path
        ])
        metadata = json.loads(probe)
        v_stream = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
        duration = float(v_stream.get("duration", 0))
        width = int(v_stream.get("width", 1280))
        height = int(v_stream.get("height", 720))
        return [DocumentAttributeVideo(
            duration=int(duration),
            w=width,
            h=height,
            supports_streaming=True
        )]
    except Exception as e:
        print(f"Erreur lecture metadata : {e}")
        return []
        
# ---------------------------
# Nouveaux helpers + handlers
# ---------------------------

async def download_from_url(url: str, user_id: int, msg, uid: str = None):
    """
    Télécharge une vidéo via yt-dlp ou via ffmpeg si m3u8.
    Retourne le chemin vers le fichier.
    """
    if uid is None:
        uid = uuid.uuid4().hex
    out_mp4 = str(DOWNLOAD_DIR / f"video_{user_id}_{uid}.mp4")

    # si c'est un m3u8, utiliser ffmpeg direct (plus fiable pour flux HLS)
    if ".m3u8" in url or url.lower().startswith("http") and "index.m3u8" in url:
        try:
            await update_progress(msg, "Téléchargement (m3u8)", 0)
            cmd = ["ffmpeg", "-y", "-i", url, "-c", "copy", out_mp4]
            subprocess.run(cmd, check=True)
            await update_progress(msg, "Téléchargement (m3u8)", 100)
            return out_mp4
        except Exception as e:
            try:
                await msg.edit(f"❌ Erreur téléchargement m3u8 via ffmpeg : {e}")
            except Exception:
                pass
            return None

    # sinon, tenter yt-dlp avec nom unique
    out_template = str(DOWNLOAD_DIR / f"video_{user_id}_{uid}.%(ext)s")

    def make_hook():
        def hook(d):
            loop = asyncio.get_event_loop()
            loop.create_task(handle_progress_callback(d, msg))
        return hook

    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [make_hook()],
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 3,
        "continuedl": True,
        # on laisse ffmpeg en fallback interne si besoin
    }

    try:
        await update_progress(msg, "Téléchargement", 0)
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            ext = info.get("ext", "mp4")
            file_path = str(DOWNLOAD_DIR / f"video_{user_id}_{uid}.{ext}")
            if not os.path.exists(file_path):
                candidates = sorted(
                    [p for p in DOWNLOAD_DIR.glob(f"video_{user_id}_{uid}.*")],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                file_path = str(candidates[0]) if candidates else None

            await update_progress(msg, "Téléchargement", 100)
            return file_path
    except Exception as e:
        try:
            await msg.edit(f"❌ Erreur téléchargement : {e}")
        except Exception:
            pass
        return None

async def download_m3u8(url: str, user_id: int, msg):
    """
    Télécharge un lien M3U8 via ffmpeg, et si échec → fallback yt-dlp.
    """
    uid = uuid.uuid4().hex
    output_path = f"downloads/video_{user_id}_{uid}.mp4"
    await update_progress(msg, "Téléchargement M3U8", 0)

    cmd = [
        "ffmpeg", "-y", "-headers",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
        "-i", url, "-c", "copy", "-movflags", "+faststart", output_path
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await update_progress(msg, "Téléchargement M3U8", 100)
        return output_path
    except subprocess.CalledProcessError:
        # ⚠️ fallback vers yt-dlp
        await msg.edit("⚠️ Échec via ffmpeg, tentative avec yt-dlp...")
        try:
            from yt_dlp import YoutubeDL
            ydl_opts = {
                "outtmpl": output_path,
                "format": "best",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "merge_output_format": "mp4"
            }
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            await update_progress(msg, "Téléchargement M3U8 (fallback)", 100)
            return output_path
        except Exception as e:
            await msg.edit(f"❌ Échec téléchargement M3U8 : {e}")
            return None


def burn_subtitles_ffmpeg(input_video: str, srt_path: str, output_path: str):
    """
    Brûle les sous-titres dans la vidéo (hardsubs) via ffmpeg.
    Utilise ffmpeg-python (wrapper) pour exécuter la commande.
    Cette fonction est synchrone — elle peut être appelée depuis une coroutine.
    """
    # Si path contient espaces, ffmpeg-python s'en occupe, mais filtre subtitles demande un chemin correct.
    # On tente d'utiliser le filtre "subtitles". Si échec (ex: format), fallback sur subprocess.
    try:
        (
            ffmpeg
            .input(input_video)
            .output(output_path, vf=f"subtitles={srt_path}", vcodec='libx264', acodec='aac', movflags='+faststart', pix_fmt='yuv420p')
            .run(overwrite_output=True, capture_stdout=False, capture_stderr=False)
        )
    except Exception:
        # fallback: appeler ffmpeg via subprocess (plus robuste pour certains builds)
        cmd = [
            "ffmpeg", "-y", "-i", input_video,
            "-vf", f"subtitles={srt_path}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-movflags", "+faststart",
            output_path
        ]
        subprocess.run(cmd, check=True)

async def merge_and_compress_with_subtitles(video_path: str, srt_path: str, user_id: int, msg):
    """
    1) Brûle les sous-titres -> file with subs
    2) Crée une version compressée
    Retourne tuple (path_with_subs, path_compressed)
    """
    base_with_subs = DOWNLOAD_DIR / f"with_subs_{user_id}.mp4"
    compressed = DOWNLOAD_DIR / f"compressed_with_subs_{user_id}.mp4"

    await simulate_progress(msg, "Fusion sous-titres", duration=6)
    try:
        # burn subtitles (synchronous)
        burn_subtitles_ffmpeg(video_path, srt_path, str(base_with_subs))
    except Exception as e:
        await msg.edit(f"❌ Erreur fusion sous-titres : {e}")
        return None, None

    # now compress the resulting file using your compress_video coroutine
    await compress_video(str(base_with_subs), str(compressed), msg)
    return str(base_with_subs), str(compressed)


# -------------------
# Command handlers
# -------------------

@bot.on(events.NewMessage(pattern=r"^/c$"))
async def handle_compress_command(event):
    if not await check_subscription(event):
        return

    if not event.is_reply:
        await event.reply("⚠️ Réponds à une vidéo avec /c.")
        return

    reply = await event.get_reply_message()
    if not reply.media:
        await event.reply("⚠️ Pas de vidéo détectée.")
        return

    user = await event.get_sender()
    increment_user_usage(user)
    uid = uuid.uuid4().hex

    # log to admin (no links available since user replied with file; we can include tg message link if needed)
    await send_usage_log(user, video_link="—", srt_link=None, command="/c")

    user_id = user.id
    msg = await event.reply("⏳ Téléchargement de la vidéo...")
    original_path = DOWNLOAD_DIR / f"input_{user_id}_{uid}.mp4"
    compressed_path = DOWNLOAD_DIR / f"compressed_{user_id}_{uid}.mp4"
    thumb_path = DOWNLOAD_DIR / f"thumb_{user_id}_{uid}.jpg"

    try:
        await bot.download_media(reply, str(original_path))
        await compress_video(str(original_path), str(compressed_path), msg)
        generate_thumbnail(str(compressed_path), str(thumb_path))
        attrs = get_video_attributes(str(compressed_path))

        await msg.edit("📤 Envoi de la vidéo compressée...")
        await bot.send_file(
            event.chat_id,
            str(compressed_path),
            caption="🗜️ Vidéo compressée",
            attributes=attrs,
            thumb=str(thumb_path),
            force_document=False,
            supports_streaming=True
        )
        await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Erreur : {e}")
    finally:
        clean_downloads()

@bot.on(events.NewMessage(pattern=r"^/cc(?:\s+(.+))?$"))
async def handle_add_subtitles_command(event):
    if not await check_subscription(event):
        return

    srt_link = event.pattern_match.group(1)
    if not event.is_reply:
        await event.reply("⚠️ Réponds à une vidéo avec `/cc <lien_srt>`.")
        return

    reply = await event.get_reply_message()
    user = await event.get_sender()
    increment_user_usage(user)
    uid = uuid.uuid4().hex

    # log usage (we might have video as reply; no external link)
    await send_usage_log(user, video_link="—", srt_link=srt_link or "—", command="/cc")

    user_id = user.id
    msg = await event.reply("⏳ Téléchargement...")

    original_path = DOWNLOAD_DIR / f"video_{user_id}_{uid}.mp4"

    try:
        await bot.download_media(reply, str(original_path))
        if not srt_link:
            await msg.edit("⚠️ Aucun lien de sous-titres fourni.")
            return

        srt_path = download_subtitle(srt_link, user_id, uid)

        # fusion -> version originale avec sous-titres
        with_subs = str(DOWNLOAD_DIR / f"with_subs_{user_id}_{uid}.mp4")
        await msg.edit("⏳ Fusion des sous-titres...")
        burn_subtitles_ffmpeg(str(original_path), srt_path, with_subs)

        # Envoi original avec sous-titres
        thumb1 = generate_thumbnail(with_subs, str(DOWNLOAD_DIR / f"thumb_subs_{uid}.jpg"))
        attrs1 = get_video_attributes(with_subs)
        await msg.edit("🎞️ Envoi de la vidéo avec sous-titres...")
        await bot.send_file(
            event.chat_id,
            with_subs,
            caption="🎞️ Vidéo avec sous-titres (originale)",
            attributes=attrs1,
            thumb=thumb1,
            force_document=False,
            supports_streaming=True
        )

        # Ensuite compresse et envoie la version compressée
        compressed_subs = str(DOWNLOAD_DIR / f"compressed_with_subs_{user_id}_{uid}.mp4")
        await msg.edit("⏳ Compression en cours...")
        await compress_video(with_subs, compressed_subs, msg)

        thumb2 = generate_thumbnail(compressed_subs, str(DOWNLOAD_DIR / f"thumb_csubs_{uid}.jpg"))
        attrs2 = get_video_attributes(compressed_subs)
        await msg.edit("📦 Envoi de la version compressée...")
        await bot.send_file(
            event.chat_id,
            compressed_subs,
            caption="📦 Vidéo compressée avec sous-titres",
            attributes=attrs2,
            thumb=thumb2,
            force_document=False,
            supports_streaming=True
        )

        await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Erreur : {e}")
    finally:
        clean_downloads()

# -------------------------
# Message handler pour liens
# -------------------------
@bot.on(events.NewMessage(pattern=r'(?s)^(https?://[^\s]+)(?:\s*\n\s*(https?://[^\s]+))?$'))
async def handle_links(event):
    if not await check_subscription(event):
        return

    user = await event.get_sender()
    increment_user_usage(user)
    uid = uuid.uuid4().hex

    lines = [l.strip() for l in event.message.message.splitlines() if l.strip()]
    video_link = lines[0]
    srt_link = lines[1] if len(lines) > 1 else None
    msg = await event.reply("⏳ Téléchargement en cours...")

    # log to admin (include real links)
    await send_usage_log(user, video_link=video_link, srt_link=srt_link or "—", command="link")

    try:
        video_path = await download_from_url(video_link, user.id, msg, uid=uid)
        if not video_path:
            await msg.edit("❌ Téléchargement échoué.")
            return

        # Si un sous-titre est fourni -> envoie original sous-titré d'abord
        if srt_link:
            srt_path = download_subtitle(srt_link, user.id, uid)
            with_subs = str(DOWNLOAD_DIR / f"with_subs_{user.id}_{uid}.mp4")

            await msg.edit("⏳ Fusion des sous-titres...")
            burn_subtitles_ffmpeg(video_path, srt_path, with_subs)

            # Envoi original with subs
            thumb1 = generate_thumbnail(with_subs, str(DOWNLOAD_DIR / f"thumb_with_{uid}.jpg"))
            attrs1 = get_video_attributes(with_subs)
            await msg.edit("🎞️ Envoi de la vidéo originale sous-titrée...")
            await bot.send_file(event.chat_id, with_subs, caption="🎞️ Vidéo originale avec sous-titres", attributes=attrs1, thumb=thumb1, force_document=False, supports_streaming=True)

            # Compression ensuite
            compressed_subs = str(DOWNLOAD_DIR / f"compressed_subs_{user.id}_{uid}.mp4")
            await msg.edit("⏳ Compression en cours...")
            await compress_video(with_subs, compressed_subs, msg)

            thumb2 = generate_thumbnail(compressed_subs, str(DOWNLOAD_DIR / f"thumb_comp_{uid}.jpg"))
            attrs2 = get_video_attributes(compressed_subs)
            await msg.edit("📦 Envoi de la version compressée avec sous-titres...")
            await bot.send_file(event.chat_id, compressed_subs, caption="📦 Vidéo compressée avec sous-titres", attributes=attrs2, thumb=thumb2, force_document=False, supports_streaming=True)

        else:
            # Pas de sous-titres : envoie originale puis compressée
            thumb1 = generate_thumbnail(video_path, str(DOWNLOAD_DIR / f"thumb_orig_{uid}.jpg"))
            attrs1 = get_video_attributes(video_path)

            await msg.edit("✅ Envoi de la vidéo originale...")
            await bot.send_file(event.chat_id, video_path, caption="📥 Vidéo originale", attributes=attrs1, thumb=thumb1, force_document=False, supports_streaming=True)

            compressed = str(DOWNLOAD_DIR / f"compressed_{user.id}_{uid}.mp4")
            await msg.edit("⏳ Compression en cours...")
            await compress_video(video_path, compressed, msg)

            thumb2 = generate_thumbnail(compressed, str(DOWNLOAD_DIR / f"thumb_comp2_{uid}.jpg"))
            attrs2 = get_video_attributes(compressed)
            await msg.edit("📦 Envoi de la version compressée...")
            await bot.send_file(event.chat_id, compressed, caption="🗜️ Vidéo compressée", attributes=attrs2, thumb=thumb2, force_document=False, supports_streaming=True)

        await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Erreur : {e}")
    finally:
        clean_downloads()
   
    
@bot.on(events.NewMessage(pattern=r"^/user$"))
async def handle_user_list(event):
    # Only admin can use
    sender = await event.get_sender()
    if getattr(sender, "username", None) != ADMIN_USERNAME:
        await event.reply("❌ Tu n'es pas autorisé à utiliser cette commande.")
        return

    stats = load_stats()
    if not stats:
        await event.reply("Aucun utilisateur enregistré.")
        return

    # Construire la liste triée par nom
    lines = []
    for key, v in sorted(stats.items(), key=lambda x: x[0].lower()):
        name = key
        count = v.get("count", 0)
        lines.append(f"{name} ({count})")
    text = "Utilisateurs\n\n" + "\n".join(lines)
    await event.reply(text)

# ==== COMMANDE /start — Menu interactif ====

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_menu(event):
    """Affiche le menu principal avec les boutons."""
    buttons = [
        [Button.inline("🗜️ Compression", data=b'help_compress')],
        [Button.inline("💬 Sous-titres", data=b'help_subtitles')],
        [Button.inline("🔗 Lien Vidéo", data=b'help_link')],
    ]
    text = (
        "👋 **Bienvenue sur le bot de téléchargement et compression vidéo !**\n\n"
        "Voici ce que tu peux faire :\n"
        "• 🗜️ **Compression** — Réduit la taille d'une vidéo.\n"
        "• 💬 **Sous-titres** — Ajoute des sous-titres à ta vidéo.\n"
        "• 🔗 **Lien Vidéo** — Télécharge une vidéo à partir d’un lien.\n\n"
        "Choisis une option ci-dessous 👇"
    )
    await event.reply(text, buttons=buttons)


# ==== Gestion des sous-menus ====

@bot.on(events.CallbackQuery(pattern=b'help_compress'))
async def help_compress(event):
    """Montre l’aide pour la compression."""
    text = (
        "🗜️ **Compression de vidéo**\n\n"
        "👉 Envoie une vidéo directement au bot.\n"
        "Puis réponds à cette même vidéo avec la commande :\n"
        "`/c`\n\n"
        "Le bot compressera et t’enverra la version allégée."
    )
    buttons = [[Button.inline("🔙 Retour", data=b'help_back')]]
    await event.edit(text, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b'help_subtitles'))
async def help_subtitles(event):
    """Montre l’aide pour les sous-titres."""
    text = (
        "💬 **Ajout de sous-titres**\n\n"
        "Tu peux ajouter des sous-titres de deux façons :\n"
        "1️⃣ Envoie un lien vidéo puis, sur la ligne suivante, le lien du fichier `.srt`\n"
        "   Exemple :\n"
        "   ```\n"
        "   https://youtube.com/xxxx\n"
        "   https://monsite.com/sous_titres.srt\n"
        "   ```\n\n"
        "2️⃣ Réponds à une vidéo avec :\n"
        "`/cc <lien_sous_titres>`\n\n"
        "Le bot enverra la version avec sous-titres et la version compressée."
    )
    buttons = [[Button.inline("🔙 Retour", data=b'help_back')]]
    await event.edit(text, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=b'help_link'))
async def help_link(event):
    """Montre l’aide pour les liens vidéos."""
    text = (
        "🔗 **Téléchargement via lien**\n\n"
        "Envoie simplement un lien vidéo (YouTube, mp4 direct, ou magnet). Le bot :\n"
        "1️⃣ Télécharge la vidéo\n"
        "2️⃣ Envoie la version originale\n"
        "3️⃣ Puis la version compressée.\n\n"
        "Tu peux aussi envoyer deux lignes :\n"
        "→ Ligne 1 : lien de la vidéo\n"
        "→ Ligne 2 : lien du sous-titre `.srt`\n"
        "Le bot fusionnera les deux avant d’envoyer."
    )
    buttons = [[Button.inline("🔙 Retour", data=b'help_back')]]
    await event.edit(text, buttons=buttons)


# ==== Bouton Retour ====

@bot.on(events.CallbackQuery(pattern=b'help_back'))
async def help_back(event):
    """Retour au menu principal."""
    buttons = [
        [Button.inline("🗜️ Compression", data=b'help_compress')],
        [Button.inline("💬 Sous-titres", data=b'help_subtitles')],
        [Button.inline("🔗 Lien Vidéo", data=b'help_link')],
    ]
    text = (
        "👋 **Menu principal**\n\n"
        "Voici les fonctions disponibles :\n"
        "• 🗜️ Compression de vidéo\n"
        "• 💬 Ajout de sous-titres\n"
        "• 🔗 Téléchargement depuis un lien\n\n"
        "Choisis une option 👇"
    )
    await event.edit(text, buttons=buttons)

print("🤖 Bot prêt !")
print("• Envoie un lien vidéo (YouTube, mp4, magnet, etc.)")
print("• Ou deux lignes : lien vidéo + lien sous-titres")

bot.run_until_disconnected()
