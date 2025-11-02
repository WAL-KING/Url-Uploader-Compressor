import os
import asyncio
print(asyncio)
print(dir(asyncio)[:15])
import time
import requests
import ffmpeg
import yt_dlp
import libtorrent as lt
import subprocess
import json
import uuid
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from pathlib import Path
import sqlite3
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError

# ==== CONFIGURATION ====
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")


DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
def clean_downloads(user_id: int = None, uid: str = None):
    try:
        if user_id and uid:
            pattern = f"*_{user_id}_{uid}*"
        elif user_id:
            pattern = f"*_{user_id}_*"
        else:
            pattern = "*"
        for p in DOWNLOAD_DIR.glob(pattern):
            if p.suffix.lower() in [".mp4", ".mkv", ".avi", ".jpg", ".srt", ".torrent"]:
                try:
                    p.unlink()
                except Exception as e:
                    print(f"Erreur suppression {p.name}: {e}")
    except Exception as e:
        print(f"Erreur clean_downloads: {e}")



bot = TelegramClient("full_video_bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

def increment_user_usage(user):
    """Incrémente le compteur d'utilisation pour un utilisateur."""
    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()
        c.execute("""
            INSERT INTO users (id, username, first_name, last_name, usage_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET usage_count = usage_count + 1
        """, (user.id, user.username, user.first_name, user.last_name))
        db.commit()


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

# 🔧 Vérifie si la colonne 'usage_count' existe, sinon l’ajoute
cur.execute("PRAGMA table_info(users)")
columns = [c[1] for c in cur.fetchall()]
if "usage_count" not in columns:
    cur.execute("ALTER TABLE users ADD COLUMN usage_count INTEGER DEFAULT 0")
conn.commit()


# 🔧 Vérifie si la colonne 'usage_count' existe, sinon l’ajoute
cur.execute("PRAGMA table_info(users)")
columns = [c[1] for c in cur.fetchall()]
if "usage_count" not in columns:
    cur.execute("ALTER TABLE users ADD COLUMN usage_count INTEGER DEFAULT 0")
conn.commit()



ADMIN_USERNAME = "hokagesama02"

def add_user(user):
    """Ajoute un utilisateur dans la base s'il n'existe pas encore."""
    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()
        c.execute("SELECT id FROM users WHERE id = ?", (user.id,))
        if not c.fetchone():
            c.execute(
                """
                INSERT INTO users (id, username, first_name, last_name, usage_count)
                VALUES (?, ?, ?, ?, 0)
                """,
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

# Dictionnaire global : clé = message_id, valeur = dict d'état
progress_states = {}

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
    Met à jour le message avec ou sans détails selon l’état du message spécifique.
    """
    msg_id = msg.id
    state = progress_states.setdefault(msg_id, {
        "percent": 0,
        "downloaded": 0,
        "total": 0,
        "speed": 0,
        "eta": 0,
        "detailed_mode": False,
    })

    state.update({
        "percent": percent,
        "downloaded": downloaded,
        "total": total,
        "speed": speed,
        "eta": eta,
    })

    bar = make_progress_bar(percent)

    if not state["detailed_mode"]:
        # Vue simplifiée
        text = f"📥 {phase} en cours : {percent}%"
        buttons = [[Button.inline("Plus d'infos ℹ️", data=f'info_details_{msg_id}'.encode())]]
    else:
        # Vue détaillée
        text = (
            f"📥 **{phase}...**\n\n"
            f"{bar} {percent}%\n\n"
            f"🗂️ {downloaded / (1024*1024):.2f} / {total / (1024*1024):.2f} MB\n"
            f"🚀 {speed / (1024*1024):.2f} MB/s\n"
            f"⏱️ ETA: {eta:.1f}s"
        )
        buttons = [[Button.inline("Masquer 🔙", data=f'info_hide_{msg_id}'.encode())]]

    try:
        await msg.edit(text, buttons=buttons)
    except Exception:
        pass  # ignore les erreurs flood ou édition concurrente

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

@bot.on(events.CallbackQuery(pattern=rb'info_details_(\d+)'))
async def show_details(event):
    """Affiche la version détaillée pour un message spécifique."""
    msg_id = int(event.pattern_match.group(1).decode())
    state = progress_states.get(msg_id)
    if not state:
        await event.answer("Aucune progression trouvée pour ce message.", alert=True)
        return
    state["detailed_mode"] = True
    await event.answer("ℹ️ Affichage détaillé activé.", alert=False)

    try:
        msg = await event.get_message()
        await update_progress(
            msg,
            "Téléchargement",
            state["percent"],
            state["downloaded"],
            state["total"],
            state["speed"],
            state["eta"]
        )
    except Exception:
        pass

@bot.on(events.CallbackQuery(pattern=rb'info_hide_(\d+)'))
async def hide_details(event):
    """Masque les détails pour un message spécifique."""
    msg_id = int(event.pattern_match.group(1).decode())
    state = progress_states.get(msg_id)
    if not state:
        await event.answer("Aucune progression trouvée pour ce message.", alert=True)
        return
    state["detailed_mode"] = False
    await event.answer("🔙 Détails masqués.", alert=False)

    try:
        msg = await event.get_message()
        await update_progress(
            msg,
            "Téléchargement",
            state["percent"],
            state["downloaded"],
            state["total"],
            state["speed"],
            state["eta"]
        )
    except Exception:
        pass




# ==== Téléchargement Magnet ====
async def download_magnet(link: str, msg=None):
    """
    Télécharge un fichier depuis un lien magnet ou .torrent.
    Utilise libtorrent, avec mise à jour de progression non bloquante.
    """
    def _download_sync(loop):
        ses = lt.session()
        ses.listen_on(6881, 6891)
        params = {'save_path': str(DOWNLOAD_DIR)}

        # Magnet ou fichier .torrent
        if link.startswith("magnet:?"):
            handle = lt.add_magnet_uri(ses, link, params)
        else:
            torrent_path = DOWNLOAD_DIR / f"temp_{uuid.uuid4().hex}.torrent"
            try:
                r = requests.get(link, timeout=30)
                r.raise_for_status()
                with open(torrent_path, "wb") as f:
                    f.write(r.content)
                info = lt.torrent_info(str(torrent_path))
                handle = ses.add_torrent({'ti': info, 'save_path': str(DOWNLOAD_DIR)})
            except Exception as e:
                print(f"Erreur téléchargement .torrent : {e}")
                return None

        # Attente des métadonnées (max 60s)
        start_time = time.time()
        last_update = 0
        while not handle.has_metadata():
            if time.time() - start_time > 60:
                # 🔴 Timeout → message au user
                if msg:
                    asyncio.run_coroutine_threadsafe(
                        msg.edit("❌ Impossible d’obtenir les métadonnées du torrent (timeout 60s)."),
                        loop
                    )
                return None

            # 🔄 Info utilisateur toutes les 5 secondes
            if msg and time.time() - last_update > 5:
                asyncio.run_coroutine_threadsafe(
                    msg.edit("⏳ Récupération des métadonnées du torrent..."),
                    loop
                )
                last_update = time.time()
            time.sleep(1)

        print("📦 Métadonnées reçues — téléchargement lancé !")

        download_start = time.time()
        last_update = 0

        while True:
            s = handle.status()

            # Timeout global (30 minutes)
            if time.time() - download_start > 1800:
                print("⚠️ Timeout global torrent atteint (30 min).")
                if msg:
                    asyncio.run_coroutine_threadsafe(
                        msg.edit("⚠️ Téléchargement annulé : timeout global atteint (30 min)."),
                        loop
                    )
                return None

            progress = int(s.progress * 100)
            eta = (s.total_wanted - s.total_done) / s.download_rate if s.download_rate > 0 else 0

            # Mise à jour toutes les 3 secondes
            if msg and time.time() - last_update > 3:
                try:
                    asyncio.run_coroutine_threadsafe(
                        update_progress(
                            msg,
                            "Téléchargement Torrent",
                            progress,
                            s.total_done,
                            s.total_wanted,
                            s.download_rate,
                            eta
                        ),
                        loop
                    )
                except Exception as e:
                    print(f"[Torrent] Erreur mise à jour : {e}")
                last_update = time.time()

            if s.is_seeding:
                print("✅ Téléchargement torrent terminé.")
                if msg:
                    asyncio.run_coroutine_threadsafe(
                        msg.edit("✅ Téléchargement torrent terminé."),
                        loop
                    )
                break

            time.sleep(1)

        # Recherche du fichier vidéo téléchargé
        for f in handle.get_torrent_info().files():
            path = DOWNLOAD_DIR / f.path
            if path.suffix.lower() in [".mp4", ".mkv", ".avi"]:
                return str(path)

        print("⚠️ Aucun fichier vidéo trouvé dans le torrent.")
        return None

    # ✅ Exécution dans un thread séparé (non bloquant)
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(_download_sync, loop)
    except Exception as e:
        print(f"Erreur thread torrent : {e}")
        return None




async def get_video_attributes(video_path: str):
    """
    Extrait la durée, largeur et hauteur pour Telegram (async non bloquant).
    """
    def _extract():
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
    return await asyncio.to_thread(_extract)

# ==== Fonctions utilitaires manquantes ====

async def compress_video(input_path: str, output_path: str, msg):
    """
    Compresse une vidéo avec ffmpeg en gardant une qualité correcte.
    """
    try:
        await update_progress(msg, "Compression", 0)

        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vcodec", "libx264", "-preset", "medium", "-crf", "28",
            "-acodec", "aac", "-movflags", "+faststart", "-pix_fmt", "yuv420p",
            output_path
        ]

        # Exécute ffmpeg sans bloquer la boucle
        await asyncio.to_thread(subprocess.run, cmd, check=True)

        await update_progress(msg, "Compression", 100)

    except subprocess.CalledProcessError as e:
        await msg.edit(f"❌ Erreur de compression : {e}")
    except Exception as e:
        await msg.edit(f"❌ Erreur inattendue pendant la compression : {e}")


async def generate_thumbnail(video_path: str, output_thumb: str):
    """
    Génère une miniature (frame à 1s) pour Telegram.
    """
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", "00:00:01.000",
            "-i", video_path, "-vframes", "1", output_thumb
        ]
        await asyncio.to_thread(subprocess.run, cmd, check=True)
        return output_thumb
    except Exception as e:
        print(f"Erreur miniature : {e}")
        return None


async def download_subtitle(url: str, user_id: int, uid: str):
    """
    Télécharge un fichier .srt depuis une URL.
    """
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        srt_path = DOWNLOAD_DIR / f"subtitle_{user_id}_{uid}.srt"
        with open(srt_path, "wb") as f:
            f.write(r.content)
        return str(srt_path)
    except Exception as e:
        print(f"Erreur téléchargement sous-titres : {e}")
        return None


async def simulate_progress(msg, phase: str, duration: int = 5):
    """
    Simule une progression visuelle sur une durée donnée.
    (utile pour les étapes où la progression réelle n'est pas mesurable)
    """
    steps = 10
    for i in range(steps + 1):
        percent = int(i / steps * 100)
        await update_progress(msg, phase, percent)
        await asyncio.sleep(duration / steps)



# ---------------------------
# Nouveaux helpers + handlers
# ---------------------------

# ==== Téléchargement depuis URL (yt-dlp + ffmpeg) ====
async def download_from_url(url: str, user_id: int, msg, uid: str = None):
    """
    Télécharge une vidéo via yt-dlp ou via ffmpeg si m3u8.
    Retourne le chemin vers le fichier.
    """
    if uid is None:
        uid = uuid.uuid4().hex
    out_mp4 = str(DOWNLOAD_DIR / f"video_{user_id}_{uid}.mp4")

    # si c'est un flux m3u8 -> utiliser ffmpeg
    if ".m3u8" in url or (url.lower().startswith("http") and "index.m3u8" in url):
        try:
            await update_progress(msg, "Téléchargement (m3u8)", 0)

            async def _ffmpeg_download():
                cmd = ["ffmpeg", "-y", "-i", url, "-c", "copy", out_mp4]
                subprocess.run(cmd, check=True)

            await asyncio.to_thread(_ffmpeg_download)
            await update_progress(msg, "Téléchargement (m3u8)", 100)
            return out_mp4
        except Exception as e:
            try:
                await msg.edit(f"❌ Erreur téléchargement m3u8 via ffmpeg : {e}")
            except Exception:
                pass
            return None

    # sinon, utiliser yt-dlp
    out_template = str(DOWNLOAD_DIR / f"video_{user_id}_{uid}.%(ext)s")

    # ✅ On récupère la boucle principale ici
    loop = asyncio.get_running_loop()

    def make_hook():
        def hook(d):
            try:
                # Envoie la mise à jour vers la boucle principale depuis le thread
                asyncio.run_coroutine_threadsafe(handle_progress_callback(d, msg), loop)
            except Exception as e:
                print(f"[yt-dlp hook] Erreur de progression : {e}")
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
    }

    try:
        await update_progress(msg, "Téléchargement", 0)

        def _download_sync():
            with YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        # Exécute yt-dlp dans un thread séparé (non bloquant)
        info = await asyncio.to_thread(_download_sync)

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



# ==== Téléchargement des flux M3U8 ====
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
        # ffmpeg lancé dans un thread (non bloquant)
        await asyncio.to_thread(
            subprocess.run,
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        await update_progress(msg, "Téléchargement M3U8", 100)
        return output_path

    except subprocess.CalledProcessError:
        await msg.edit("⚠️ Échec via ffmpeg, tentative avec yt-dlp...")

        def _ydl_download():
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

        try:
            await asyncio.to_thread(_ydl_download)
            await update_progress(msg, "Téléchargement M3U8 (fallback)", 100)
            return output_path
        except Exception as e:
            await msg.edit(f"❌ Échec téléchargement M3U8 : {e}")
            return None


async def burn_subtitles_ffmpeg(input_video: str, srt_path: str, output_path: str):
    """
    Brûle les sous-titres dans la vidéo (hardsubs) via ffmpeg.
    Version asynchrone (non bloquante) utilisant asyncio.to_thread.
    """
    try:
        # Exécute ffmpeg-python dans un thread séparé pour éviter de bloquer l'event loop
        await asyncio.to_thread(
            lambda: (
                ffmpeg
                .input(input_video)
                .output(
                    output_path,
                    vf=f"subtitles={srt_path}",
                    vcodec='libx264',
                    acodec='aac',
                    movflags='+faststart',
                    pix_fmt='yuv420p'
                )
                .run(overwrite_output=True, capture_stdout=False, capture_stderr=False)
            )
        )
    except Exception:
        # fallback subprocess aussi dans un thread pour éviter le blocage
        cmd = [
            "ffmpeg", "-y", "-i", input_video,
            "-vf", f"subtitles={srt_path}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-movflags", "+faststart",
            output_path
        ]
        await asyncio.to_thread(subprocess.run, cmd, check=True)

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
        await burn_subtitles_ffmpeg(video_path, srt_path, str(base_with_subs))
    except Exception as e:
        await msg.edit(f"❌ Erreur fusion sous-titres : {e}")
        return None, None

    # now compress the resulting file using your compress_video coroutine
    await compress_video(str(base_with_subs), str(compressed), msg)
    return str(base_with_subs), str(compressed)


# -------------------
# Command handlers
# -------------------

@bot.on(events.NewMessage(pattern=r"^/c($|\s)"))
async def handle_compress_command(event):
    user = await event.get_sender()
    reply = await event.get_reply_message()
    msg = await event.respond("⏳ Préparation de la compression...")

    if not reply or not (reply.video or reply.document):
        await msg.edit("⚠️ Réponds à une vidéo pour la compresser.")
        return

    uid = uuid.uuid4().hex
    original_path = DOWNLOAD_DIR / f"original_{user.id}_{uid}.mp4"
    compressed_path = DOWNLOAD_DIR / f"compressed_{user.id}_{uid}.mp4"

    try:
        await msg.edit("📥 Téléchargement de la vidéo...")
        await bot.download_media(reply, str(original_path))

        if not os.path.exists(original_path):
            await msg.edit("❌ Erreur : téléchargement échoué.")
            return

        await msg.edit("🗜️ Compression en cours...")
        await compress_video(str(original_path), str(compressed_path), msg)


        thumb_path = await generate_thumbnail(str(compressed_path), str(DOWNLOAD_DIR / f"thumb_{uid}.jpg"))
        attrs = await get_video_attributes(str(compressed_path))

        await msg.edit("📦 Envoi de la vidéo compressée...")
        await bot.send_file(
            event.chat_id,
            str(compressed_path),
            caption="🗜️ Vidéo compressée",
            attributes=attrs,
            thumb=thumb_path,
            force_document=False,
            supports_streaming=True
        )

        await msg.delete()

    except Exception as e:
        await msg.edit(f"❌ Erreur pendant la compression : {e}")

    finally:
        clean_downloads(user.id, uid)



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
        # Téléchargement de la vidéo envoyée en réponse
        await bot.download_media(reply, str(original_path))
        if not srt_link:
            await msg.edit("⚠️ Aucun lien de sous-titres fourni.")
            return

        # Téléchargement du fichier .srt
        srt_path = await download_subtitle(srt_link, user_id, uid)

        # Fusion des sous-titres (burn)
        with_subs = str(DOWNLOAD_DIR / f"with_subs_{user_id}_{uid}.mp4")
        await msg.edit("⏳ Fusion des sous-titres...")
        await burn_subtitles_ffmpeg(str(original_path), srt_path, with_subs)

        # Génération miniature + attributs
        thumb1 = await generate_thumbnail(with_subs, str(DOWNLOAD_DIR / f"thumb_subs_{uid}.jpg"))
        attrs1 = await get_video_attributes(with_subs)

        # Envoi de la vidéo originale avec sous-titres
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

        # Compression et envoi de la version compressée
        compressed_subs = str(DOWNLOAD_DIR / f"compressed_with_subs_{user_id}_{uid}.mp4")
        await msg.edit("⏳ Compression en cours...")
        await compress_video(with_subs, compressed_subs, msg)

        thumb2 = await generate_thumbnail(compressed_subs, str(DOWNLOAD_DIR / f"thumb_csubs_{uid}.jpg"))
        attrs2 = await get_video_attributes(compressed_subs)
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
        clean_downloads(user.id, uid)



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

    # log admin
    await send_usage_log(user, video_link=video_link, srt_link=srt_link or "—", command="link")

    try:
        # Sélectionne méthode de téléchargement
        if "magnet:?" in video_link or video_link.endswith(".torrent"):
            await msg.edit("🌀 Téléchargement du torrent en cours...")
            video_path = await download_magnet(video_link, msg)
        else:
            video_path = await download_from_url(video_link, user.id, msg, uid=uid)

        if not video_path:
            await msg.edit("❌ Téléchargement échoué.")
            return

    except Exception as e:
        try:
            await msg.edit(f"❌ Erreur téléchargement : {e}")
        except Exception:
            pass
        return None




        if srt_link:
            srt_path = await download_subtitle(srt_link, user.id, uid)
            with_subs = str(DOWNLOAD_DIR / f"with_subs_{user.id}_{uid}.mp4")

            await msg.edit("⏳ Fusion des sous-titres...")
            await burn_subtitles_ffmpeg(video_path, srt_path, with_subs)

            thumb1 = await generate_thumbnail(with_subs, str(DOWNLOAD_DIR / f"thumb_with_{uid}.jpg"))
            attrs1 = await get_video_attributes(with_subs)
            await msg.edit("🎞️ Envoi de la vidéo originale sous-titrée...")
            await bot.send_file(
                event.chat_id,
                with_subs,
                caption="🎞️ Vidéo originale avec sous-titres",
                attributes=attrs1,
                thumb=thumb1,
                force_document=False,
                supports_streaming=True
            )

            # Compression ensuite
            compressed_subs = str(DOWNLOAD_DIR / f"compressed_subs_{user.id}_{uid}.mp4")
            await msg.edit("⏳ Compression en cours...")
            await compress_video(with_subs, compressed_subs, msg)

            thumb2 = await generate_thumbnail(compressed_subs, str(DOWNLOAD_DIR / f"thumb_comp_{uid}.jpg"))
            attrs2 = await get_video_attributes(compressed_subs)
            await msg.edit("📦 Envoi de la version compressée avec sous-titres...")
            await bot.send_file(
                event.chat_id,
                compressed_subs,
                caption="📦 Vidéo compressée avec sous-titres",
                attributes=attrs2,
                thumb=thumb2,
                force_document=False,
                supports_streaming=True
            )

        else:
            # Pas de sous-titre
            thumb1 = await generate_thumbnail(video_path, str(DOWNLOAD_DIR / f"thumb_orig_{uid}.jpg"))
            attrs1 = await get_video_attributes(video_path)
            await msg.edit("✅ Envoi de la vidéo originale...")
            await bot.send_file(
                event.chat_id,
                video_path,
                caption="📥 Vidéo originale",
                attributes=attrs1,
                thumb=thumb1,
                force_document=False,
                supports_streaming=True
            )

            compressed = str(DOWNLOAD_DIR / f"compressed_{user.id}_{uid}.mp4")
            await msg.edit("⏳ Compression en cours...")
            await compress_video(video_path, compressed, msg)

            thumb2 = await generate_thumbnail(compressed, str(DOWNLOAD_DIR / f"thumb_comp2_{uid}.jpg"))
            attrs2 = await get_video_attributes(compressed)
            await msg.edit("📦 Envoi de la version compressée...")
            await bot.send_file(
                event.chat_id,
                compressed,
                caption="🗜️ Vidéo compressée",
                attributes=attrs2,
                thumb=thumb2,
                force_document=False,
                supports_streaming=True
            )

        await msg.delete()

    except Exception as e:
        await msg.edit(f"❌ Erreur : {e}")

    finally:
        clean_downloads(user.id, uid)



@bot.on(events.NewMessage(pattern=r"^/user$"))
async def handle_user_list(event):
    sender = await event.get_sender()
    if getattr(sender, "username", None) != ADMIN_USERNAME:
        await event.reply("❌ Tu n'es pas autorisé à utiliser cette commande.")
        return

    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()
        c.execute("SELECT username, first_name, usage_count FROM users ORDER BY usage_count DESC")
        rows = c.fetchall()

    if not rows:
        await event.reply("Aucun utilisateur enregistré.")
        return

    lines = []
    for username, first_name, count in rows:
        name = f"@{username}" if username else first_name
        lines.append(f"{name} ({count})")
    await event.reply("👥 Utilisateurs :\n\n" + "\n".join(lines))


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
