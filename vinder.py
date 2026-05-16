import os
import re
import time
import uuid
import threading
import requests
import logging
import subprocess
from flask import Flask, request, jsonify, send_file, Response, stream_with_context

# =============================================================================
# SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def mask_url(url, keep=50):
    if not url:
        return '[empty url]'
    try:
        base = url.split('?')[0]
        if len(base) > keep:
            return base[:keep] + '...[masked]'
        return base
    except Exception:
        return '[url]' 

# =============================================================================
# TELEGRAM NOTIF
# =============================================================================

TELEGRAM_NOTIF_ENABLED = True 
_TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if TELEGRAM_NOTIF_ENABLED and (not _TELEGRAM_TOKEN or not _TELEGRAM_CHAT_ID):
    logger.warning(
        "[NOTIF] TELEGRAM_TOKEN atau TELEGRAM_CHAT_ID tidak ditemukan di env. "
        "Notif Telegram dinonaktifkan. Set env var untuk mengaktifkan."
    )
    TELEGRAM_NOTIF_ENABLED = False

def kirim_notif(pesan):
    if not TELEGRAM_NOTIF_ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": _TELEGRAM_CHAT_ID, "text": pesan},
            timeout=3
        )
    except Exception as e:
        logger.warning(f"[NOTIF] Gagal kirim notif Telegram: {e}")

# =============================================================================
# GROQ LOG ALERT HANDLER
# =============================================================================

class GroqAlertHandler(logging.Handler):
    COOLDOWN_SECONDS = 60

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._lock     = threading.Lock()
        self._last_sent = {}

    def _analisis_groq(self, level, pesan, func_name):
        groq_key = os.environ.get("OPENROUTER_API_KEY")
        if not groq_key:
            return "Analisis tidak tersedia (OPENROUTER_API_KEY tidak ada)."
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type":  "application/json"
                },
                json={
                    "model": "openai/gpt-oss-120b:free",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Kamu adalah analis sistem untuk aplikasi downloader bernama SVETIKTOK. "
                                "Tugasmu: analisis log error berikut, jelaskan penyebabnya, "
                                "dan berikan solusi konkret dalam bahasa Indonesia santai. "
                                "Maksimal 4 kalimat. Langsung ke poin, tidak perlu basa-basi."
                            )
                        },
                        {
                            "role": "user",
                            "content": f"Level: {level}\nFungsi: {func_name}\nPesan error:\n{pesan}"
                        }
                    ],
                    "max_tokens": 300,
                    "temperature": 0.5
                },
                timeout=15
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"Analisis Groq gagal: {e}"

    def emit(self, record):
        if getattr(record, '_from_groq_handler', False):
            return
        if not TELEGRAM_NOTIF_ENABLED:
            return

        try:
            level     = record.levelname
            pesan     = self.format(record)
            func_name = record.funcName or "unknown"

            cooldown_key = pesan[:120]
            now = time.time()
            with self._lock:
                last = self._last_sent.get(cooldown_key, 0)
                if now - last < self.COOLDOWN_SECONDS:
                    return
                self._last_sent[cooldown_key] = now

            analisis = self._analisis_groq(level, pesan, func_name)
            emoji = {"WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "🔴"}.get(level, "📋")

            notif = (
                f"{emoji} [{level}] Log Alert SVETIKTOK\n"
                f"🔧 Fungsi: {func_name}\n"
                f"📋 Log:\n{pesan[:400]}\n\n"
                f"🤖 Analisis AI:\n{analisis}"
            )

            requests.post(
                f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": _TELEGRAM_CHAT_ID, "text": notif},
                timeout=5
            )
        except Exception:
            pass

_groq_alert_handler = GroqAlertHandler()
logger.addHandler(_groq_alert_handler)
logger.info("[ALERT] GroqAlertHandler aktif — semua WARNING/ERROR/CRITICAL akan dianalisis AI.")

app = Flask(__name__, static_folder='static', static_url_path='')

_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5000,http://127.0.0.1:5000"
).split(",")

from flask_cors import CORS
CORS(app, origins=_ALLOWED_ORIGINS)

# =============================================================================
# RATE LIMITING
# =============================================================================
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)

def on_rate_limit_exceeded(e):
    ip   = get_remote_address()
    path = request.path
    batas_map = {
        '/api/search':       '10x/menit',
        '/api/download_url': '20x/menit',
        '/api/fast_mp3':     '15x/menit',
    }
    batas = batas_map.get(path, 'batas limit')
    kirim_notif(
        f"⚠️ Rate Limit Terlampaui!\n"
        f"User IP: {ip}\n"
        f"Endpoint: {path}\n"
        f"Melebihi batas {batas}"
    )
    return "Terlalu banyak permintaan. Silakan tunggu sebentar.", 429

app.register_error_handler(429, on_rate_limit_exceeded)

TIKTOK_UA = (
    "com.zhiliaoapp.musically/2022505030 "
    "(Linux; U; Android 12; en_US; Pixel 6; Build/SQ3A.220705.004; Cronet/58.0.2991.0)"
)

DEFAULT_HEADERS = {
    "User-Agent":      TIKTOK_UA,
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection":      "keep-alive",
}

TIKTOK_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer":         "https://www.tiktok.com/",
    "Origin":          "https://www.tiktok.com",
    "Accept-Encoding": "identity",
}

session = requests.Session()
from requests.adapters import HTTPAdapter
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
session.mount('https://', _adapter)
session.mount('http://',  _adapter)

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def format_durasi(detik):
    if detik is None: return "?"
    try:
        m, s = divmod(int(detik), 60)
        return f"{m}m{s:02d}s"
    except Exception:
        return "?"

def parse_filter_durasi(filter_str):
    if not filter_str: return None, None
    try:
        f = filter_str.strip().lower()
        match = re.match(r'^([<>])\s*(\d+(?:\.\d+)?)\s*([smh])$', f)
        if not match: return None, None
        op, angka, satuan = match.group(1), float(match.group(2)), match.group(3)
        multiplier = {'s': 1, 'm': 60, 'h': 3600}[satuan]
        return op, angka * multiplier
    except Exception:
        return None, None

def lolos_filter(durasi_detik, op, batas_detik):
    if op is None or durasi_detik is None: return True
    try:
        d = float(durasi_detik)
        if op == '<': return d < batas_detik
        if op == '>': return d > batas_detik
    except Exception:
        pass
    return True

def resolve_tiktok_url(url):
    try:
        r = session.head(url, allow_redirects=True, timeout=10)
        return r.url
    except Exception as e:
        logger.warning(f"[WARN] Gagal resolve URL: {e}")
        return url

def safe_filename(title, max_len=60):
    cleaned = re.sub(r'[\\/:*?"<>|]', '', title)
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    cleaned = re.sub(r'[#@]\S*', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_len] or 'svetiktok'

def make_content_disposition(filename):
    from urllib.parse import quote
    ascii_fallback = filename.encode('ascii', errors='replace').decode('ascii').replace('?', '_')
    utf8_encoded = quote(filename, safe=" !()\'~")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_encoded}"

def do_cleanup(out_tmpl):
    suffixes = ['.mp3', '.mp3.raw', '_cover.jpg', '.ready']
    for suffix in suffixes:
        path = out_tmpl + suffix
        if os.path.exists(path):
            try: os.remove(path)
            except Exception: pass

def orphan_cleanup_loop():
    MAX_AGE_SECONDS = 60 * 60
    INTERVAL        = 10 * 60
    while True:
        try:
            now = time.time()
            deleted = 0
            for fname in os.listdir('/tmp'):
                if not fname.startswith('svetiktok_'): continue
                fpath = os.path.join('/tmp', fname)
                try:
                    age = now - os.path.getmtime(fpath)
                    if age > MAX_AGE_SECONDS:
                        os.remove(fpath)
                        deleted += 1
                except Exception:
                    pass
            if deleted:
                logger.info(f"[CLEANUP] Orphan cleanup: {deleted} file temp dihapus dari /tmp")
        except Exception as e:
            logger.warning(f"[CLEANUP] Orphan cleanup error: {e}")
        time.sleep(INTERVAL)

_cleanup_thread = threading.Thread(target=orphan_cleanup_loop, daemon=True)
_cleanup_thread.start()

# =============================================================================
# VIDEO / AUDIO FUNCTIONS
# =============================================================================

def fetch_video_stream(url, fallback_url=None):
    headers = DEFAULT_HEADERS.copy()
    if "tiktok.com" in url or "ttwstatic.com" in url:
        headers["Referer"] = "https://www.tiktok.com/"
        headers["Origin"]  = "https://www.tiktok.com"
    else:
        domain = re.search(r'https?://([^/]+)', url)
        if domain:
            headers["Origin"]  = f"https://{domain.group(1)}"
            headers["Referer"] = f"https://{domain.group(1)}/"
    headers.update({"Accept-Encoding": "identity", "Range": "bytes=0-"})

    try:
        r = session.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        content_type   = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type or 'application/json' in content_type:
            if fallback_url:
                return session.get(fallback_url, stream=True, timeout=30, headers=headers, allow_redirects=True), True
            return None, False
        return r, False
    except Exception as e:
        logger.error(f"Stream Error: {e}")
        if fallback_url:
            return session.get(fallback_url, stream=True, timeout=30, headers=headers, allow_redirects=True), True
        raise

def get_meta_via_tikwm(tiktok_url, retries=3, for_audio=False):
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(f"https://www.tikwm.com/api/?url={tiktok_url}", timeout=15)
            data = resp.json()
            if data.get('code') == 0:
                v         = data['data']
                video_url = (v.get('wmplay') or v.get('play')) if for_audio else (v.get('hdplay') or v.get('play'))
                cover_url = v.get('origin_cover') or v.get('cover')
                title     = v.get('title', 'audio')
                return video_url, cover_url, title
        except Exception as e:
            pass
        if attempt < retries: time.sleep(1.5 * attempt)
    return None, None, None

def detect_audio_bitrate(url, headers):
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-select_streams', 'a:0', url],
            capture_output=True, timeout=15,
            env={**__import__('os').environ, 'FFPROBE_USER_AGENT': headers.get('User-Agent', '')},
        )
        import json
        data = json.loads(probe.stdout.decode())
        streams = data.get('streams', [])
        if streams:
            br = streams[0].get('bit_rate')
            if br:
                kbps = int(br) // 1000
                for std in [64, 96, 128, 160, 192]:
                    if kbps <= std: return f"{std}k"
                return "192k"
    except Exception:
        pass
    return "128k"

def download_audio_direct(audio_url, out_mp3):
    headers = TIKTOK_HEADERS.copy()
    headers["Range"] = "bytes=0-"
    bitrate = detect_audio_bitrate(audio_url, headers)
    r = session.get(audio_url, stream=True, timeout=60, headers=headers, allow_redirects=True)
    r.raise_for_status()

    cmd = [
        'ffmpeg', '-y', '-i', 'pipe:0', '-vn', '-acodec', 'libmp3lame',
        '-ab', bitrate, '-ar', '44100', out_mp3,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if chunk: proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    proc.wait(timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("Gagal memproses audio, silakan coba lagi.")

def download_cover(cover_url, cover_path):
    try:
        cr = session.get(cover_url, timeout=15)
        cr.raise_for_status()
        if len(cr.content) > 1000:
            with open(cover_path, 'wb') as f:
                f.write(cr.content)
            return True
    except Exception:
        pass
    return False

def embed_cover(mp3_path, cover_path):
    thumb_path = cover_path + '.thumb.jpg'
    try:
        subprocess.run([
            'ffmpeg', '-y', '-i', cover_path,
            '-vf', 'scale=500:500:force_original_aspect_ratio=decrease,pad=500:500:(ow-iw)/2:(oh-ih)/2',
            '-q:v', '6', thumb_path,
        ], check=True, capture_output=True, timeout=15)
        
        from mutagen.id3 import ID3, APIC, error as ID3Error
        with open(thumb_path, 'rb') as img_f:
            img_data = img_f.read()
        try: tags = ID3(mp3_path)
        except ID3Error: tags = ID3()
        tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img_data))
        tags.save(mp3_path, v2_version=3)
    except Exception:
        pass
    finally:
        if os.path.exists(thumb_path):
            try: os.remove(thumb_path)
            except Exception: pass

def process_mp3_pipeline(url, title, out_tmpl, progress_cb=None):
    def emit(pct, msg):
        if progress_cb: progress_cb(pct, msg)
        logger.info(f"[{pct}%] {msg}")

    out_mp3 = out_tmpl + '.mp3'
    is_tiktok = any(x in url for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com'])

    if is_tiktok:
        emit(20, "Memproses video TikTok...")
        video_url, cover_url, tikwm_title = get_meta_via_tikwm(url, for_audio=True)
        final_title = tikwm_title or title
        if not video_url:
            raise RuntimeError("Gagal mengambil video, silakan coba lagi.")
        
        emit(35, "Mengunduh audio...")
        download_audio_direct(video_url, out_mp3)

        if cover_url:
            cover_path = out_tmpl + '_cover.jpg'
            emit(88, "Menyiapkan file...")
            if download_cover(cover_url, cover_path):
                embed_cover(out_mp3, cover_path)
    else:
        raise RuntimeError("Platform tidak didukung. Harap gunakan link TikTok.")

    return out_mp3, final_title

@app.route('/')
def index():
    return send_file('svetiktok.html')

@app.route('/api/ping')
def ping():
    try: session.head('https://www.tikwm.com', timeout=5)
    except Exception: pass
    return '', 204

@app.route('/api/search', methods=['POST'])
@limiter.limit('10 per minute')
def search_videos_api():
    data       = request.json
    keyword    = data.get('keyword')
    limit      = max(1, min(int(data.get('limit', 10)), 20))
    filter_str = data.get('filter', '').strip()

    filter_op, filter_detik = parse_filter_durasi(filter_str)

    try:
        resp = session.post(
            "https://www.tikwm.com/api/feed/search",
            data={"keywords": keyword, "count": limit, "HD": 1},
            timeout=30,
        )
        resp.raise_for_status()
        json_data = resp.json()

        if json_data.get('code') != 0:
            return jsonify({"status": "error", "msg": f"TikWM API: {json_data.get('msg')}"})

        videos  = json_data.get('data', {}).get('videos', [])
        results = []

        for v in videos:
            durasi_detik = v.get('duration')
            if not lolos_filter(durasi_detik, filter_op, filter_detik): continue

            cover_url  = v.get('origin_cover') or v.get('cover') or ''
            size_bytes = v.get('size', 0)
            size_mb    = round(size_bytes / (1024 * 1024), 2) if size_bytes else "?"
            author     = v.get('author', {})

            results.append({
                'title':     v.get('title', 'Video TikTok'),
                'duration':  format_durasi(durasi_detik),
                'play':      v.get('play', ''),
                'hdplay':    v.get('hdplay', '') or v.get('play', ''),
                'cover':     cover_url,
                'size':      f"{size_mb} MB",
                'video_id':  v.get('id', ''),
                'author_id': author.get('id', '') if isinstance(author, dict) else '',
            })

        return jsonify({"status": "success", "data": results})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

SUPPORTED_PLATFORMS = ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com']

import ipaddress
from urllib.parse import urlparse

def is_safe_external_url(url):
    if not url: return False
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'): return False
        hostname = parsed.hostname or ''
        if hostname in ('localhost', ''): return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved: return False
        except ValueError:
            pass
        return True
    except Exception:
        return False

def is_supported_url(url):
    if not url: return False
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
        return any(netloc == p or netloc.endswith("." + p) for p in SUPPORTED_PLATFORMS)
    except Exception:
        return False

@app.route('/api/download_url', methods=['POST'])
@limiter.limit('20 per minute')
def download_url_api():
    data      = request.json
    url_input = data.get('url', '').strip()

    if not is_supported_url(url_input):
        return jsonify({"status": "error", "msg": "Platform tidak didukung. SVETIKTOK murni untuk TikTok."})

    try:
        resp = session.get(f"https://www.tikwm.com/api/?url={url_input}", timeout=15).json()
        if resp.get('code') == 0:
            v = resp['data']
            images = v.get('images') or []
            if images:
                return jsonify({
                    "status":       "slideshow",
                    "title":        v.get('title', 'TikTok Slideshow'),
                    "cover":        v.get('origin_cover') or v.get('cover'),
                    "author":       v.get('author', {}).get('nickname', 'User'),
                    "duration":     f"{v.get('duration', 0)}s",
                    "size":         f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                    "image_count":  len(images),
                })
            return jsonify({
                "status":   "success",
                "title":    v.get('title', 'TikTok Video'),
                "cover":    v.get('origin_cover') or v.get('cover'),
                "author":   v.get('author', {}).get('nickname', 'User'),
                "duration": f"{v.get('duration', 0)}s",
                "size":     f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                "play":     v.get('play'),
                "hdplay":   v.get('hdplay'),
            })
        return jsonify({"status": "error", "msg": "Video tidak ditemukan di TikTok."})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/get_video')
def get_video_api():
    video_url    = request.args.get('url')
    fallback_url = request.args.get('fallback')
    title        = request.args.get('title', 'video')

    if not video_url or not is_safe_external_url(video_url):
        return "URL tidak valid atau tidak diizinkan.", 400

    try:
        r, _ = fetch_video_stream(video_url, fallback_url)
        if r is None or r.status_code >= 400:
            return "Video tidak ditemukan atau link sudah kadaluarsa.", 403

        content_type = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            return "Video tidak dapat diakses, silakan coba lagi.", 403

        fname = f'[SVETIKTOK].{safe_filename(title)}.mp4'
        return Response(
            stream_with_context(r.iter_content(chunk_size=1024 * 1024)),
            headers={
                'Content-Type':        content_type,
                'Content-Disposition': make_content_disposition(fname),
                'Cache-Control':       'no-cache',
            }
        )
    except Exception as e:
        logger.error(f"get_video error: {str(e)}")
        return "Terjadi kesalahan saat memproses video. Silakan coba lagi.", 500

@app.route('/api/mp3_progress')
def mp3_progress_api():
    tiktok_url = request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not is_safe_external_url(tiktok_url) or not is_supported_url(tiktok_url):
        return "URL tidak valid atau platform tidak didukung.", 400

    def generate():
        def send(pct, msg): return f"data: {pct}|{msg}\n\n"
        uid      = str(uuid.uuid4())
        out_tmpl = f'/tmp/svetiktok_{uid}'
        import queue
        q = queue.Queue()

        def emit_sse(pct, msg): q.put(send(pct, msg))

        def run_pipeline():
            try:
                emit_sse(5, "Memeriksa link video...")
                url = resolve_tiktok_url(tiktok_url) if 'vt.tiktok' in tiktok_url or 'vm.tiktok' in tiktok_url else tiktok_url
                out_mp3, final_title = process_mp3_pipeline(url, title, out_tmpl, progress_cb=emit_sse)

                if not os.path.exists(out_mp3):
                    q.put(send(-1, "Gagal memproses audio."))
                    do_cleanup(out_tmpl)
                    return
                fname = f"[SVETIKTOK].{safe_filename(final_title)}.mp3"
                emit_sse(95, "Menyiapkan file untuk diunduh...")
                with open(out_tmpl + '.ready', 'w') as f: f.write(fname)
                q.put(send(100, f"[OK] DONE|{uid}|{fname}"))
            except Exception as e:
                logger.error(f"SSE MP3 Error: {e}")
                do_cleanup(out_tmpl)
                q.put(send(-1, "Gagal memproses audio."))
            finally:
                q.put(None)

        threading.Thread(target=run_pipeline, daemon=True).start()
        while True:
            try: item = q.get(timeout=120)
            except queue.Empty:
                yield send(-1, "Proses terlalu lama."); break
            if item is None: break
            yield item

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache'})

@app.route('/api/get_mp3_file')
def get_mp3_file_api():
    uid = request.args.get('uid', '')
    if not uid or not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', uid):
        return "UID tidak valid", 400

    out_tmpl  = f'/tmp/svetiktok_{uid}'
    out_mp3   = out_tmpl + '.mp3'
    done_flag = out_tmpl + '.ready'

    if not os.path.exists(out_mp3) or not os.path.exists(done_flag):
        return "File tidak ditemukan", 404

    with open(done_flag) as f: filename = f.read().strip()
    def generate_mp3_file():
        with open(out_mp3, 'rb') as audio_f:
            while True:
                chunk = audio_f.read(512 * 1024)
                if not chunk: break
                yield chunk
        do_cleanup(out_tmpl)

    return Response(stream_with_context(generate_mp3_file()), headers={'Content-Type': 'audio/mpeg', 'Content-Disposition': make_content_disposition(filename)})

@app.route('/api/fast_mp3', methods=['GET', 'POST'])
@limiter.limit('15 per minute')
def fast_mp3_api():
    import tempfile
    data       = request.get_json(force=True) if request.method == 'POST' else request.args
    tiktok_url = data.get('url', '').strip()
    title      = data.get('title', 'audio')

    if not is_safe_external_url(tiktok_url) or not is_supported_url(tiktok_url):
        return "URL tidak valid atau platform tidak didukung.", 400

    try:
        url = resolve_tiktok_url(tiktok_url) if 'vt.tiktok' in tiktok_url or 'vm.tiktok' in tiktok_url else tiktok_url
        vid_url, _, tikwm_title = get_meta_via_tikwm(url, for_audio=True)
        if not vid_url: return "Gagal mengambil URL audio dari TikTok.", 500

        _fd, tmp_base = tempfile.mkstemp(prefix='svetiktok_fast_')
        os.close(_fd)
        os.remove(tmp_base)
        out_mp3 = tmp_base + '.mp3'

        download_audio_direct(vid_url, out_mp3)

        if not os.path.exists(out_mp3): return "Gagal memproses audio.", 500

        filename  = f"[SVETIKTOK].{safe_filename(tikwm_title or title)}.mp3"
        file_size = os.path.getsize(out_mp3)

        def generate_tiktok_mp3():
            try:
                with open(out_mp3, 'rb') as f:
                    while True:
                        chunk = f.read(512 * 1024)
                        if not chunk: break
                        yield chunk
            finally:
                try: os.remove(out_mp3)
                except Exception: pass

        return Response(stream_with_context(generate_tiktok_mp3()), headers={
            'Content-Type': 'audio/mpeg',
            'Content-Disposition': make_content_disposition(filename),
            'Content-Length': str(file_size)
        })
    except Exception as e:
        logger.error(f"fast_mp3 error: {e}")
        return "Terjadi kesalahan saat memproses audio.", 500

@app.route('/api/mp4_info', methods=['POST'])
@limiter.limit('20 per minute')
def mp4_info_api():
    return jsonify({"status": "error", "msg": "Platform tidak didukung. Gunakan fitur URL Downloader khusus TikTok."}), 400

@app.route('/api/download_mp4', methods=['POST'])
@limiter.limit('10 per minute')
def download_mp4_api():
    return jsonify({"status": "error", "msg": "Platform tidak didukung. Gunakan fitur URL Downloader khusus TikTok."}), 400

# =============================================================================
# DAILY HEALTH + AI MESSAGE
# =============================================================================

_GROQ_API_KEY = os.environ.get("OPENROUTER_API_KEY")
HEALTH_SAMPLES = {"TikTok": "https://vt.tiktok.com/ZSxLdGQbS/"}

def _run_daily_health_check():
    import random as _random
    from datetime import datetime as _datetime
    logger.info("[DAILY] Mulai health check harian TikTok.")

    tiktok_error = None
    try:
        resp = requests.get(f"https://www.tikwm.com/api/?url={HEALTH_SAMPLES['TikTok']}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0 or not (data.get("data", {}).get("play") or data.get("data", {}).get("hdplay")):
            tiktok_error = "TikWM gagal atau link kosong."
    except Exception as e:
        tiktok_error = str(e)

    now_str = _datetime.now().strftime("%d/%m/%Y %H:%M")
    if tiktok_error:
        kirim_notif(f"❌ SVETIKTOK Health Check GAGAL!\n🕒 {now_str}\n\n[TikTok] ❌ {tiktok_error}")
    else:
        kirim_notif(f"✅ SVETIKTOK Health Check PASSED!\n🕒 {now_str}\n\n[TikTok] ✅ TikWM OK.")

def _daily_health_loop():
    import time as _time
    from datetime import datetime as _datetime
    _time.sleep(5)
    _last_health_check_date = None
    while True:
        _time.sleep(60)
        try:
            now, today = _datetime.now(), _datetime.now().date()
            if now.hour == 15 and _last_health_check_date != today:
                _last_health_check_date = today
                _run_daily_health_check()
        except Exception: pass

def _heartbeat_cookies_loop():
    import time as _time
    from datetime import datetime as _datetime
    import random
    _time.sleep(90)
    while True:
        now_str = _datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            resp = requests.get(f"https://www.tikwm.com/api/?url={HEALTH_SAMPLES['TikTok']}", timeout=15).json()
            if resp.get('code') == 0: kirim_notif(f"💓 SVETIKTOK Heartbeat\n🕒 {now_str}\n\n🎵 TikTok ✅ TikWM aktif")
            else: kirim_notif(f"⚠️ SVETIKTOK Heartbeat\n🕒 {now_str}\n\n🎵 TikTok ❌ Gagal.")
        except Exception as e:
            pass
        _time.sleep((8 * 60 * 60) + random.randint(60, 2700))

def _self_ping_loop():
    import time as _time
    _time.sleep(60)
    port = int(os.environ.get('PORT', 5000))
    url  = f"http://127.0.0.1:{port}/api/ping"
    while True:
        try: requests.get(url, timeout=10)
        except Exception: pass
        _time.sleep(4 * 60)

if __name__ == "__main__":
    threading.Thread(target=_self_ping_loop, daemon=True).start()
    threading.Thread(target=_daily_health_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_cookies_loop, daemon=True).start()
    kirim_notif("Sistem SVETIKTOK Berhasil ON!")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)