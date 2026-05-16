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
        url = f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": _TELEGRAM_CHAT_ID, "text": pesan, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"[NOTIF] Gagal kirim notif: {e}")

# =============================================================================
# CONFIG & LIMITS
# =============================================================================

HEALTH_SAMPLES = {
    'TikTok': 'https://www.tiktok.com/@tiktok/video/7106594312292453675'
}

DOWNLOAD_DIR = '/tmp'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

IP_LIMITS = {}
_LIMIT_LOCK = threading.Lock()

def check_rate_limit(ip):
    now = time.time()
    with _LIMIT_LOCK:
        if ip not in IP_LIMITS:
            IP_LIMITS[ip] = []
        IP_LIMITS[ip] = [t for t in IP_LIMITS[ip] if now - t < 60]
        if len(IP_LIMITS[ip]) >= 15:
            return False
        IP_LIMITS[ip].append(now)
        return True

# =============================================================================
# CORE ENGINE: TIKWM SCRIPT
# =============================================================================

def fetch_tikwm(url):
    try:
        res = requests.get(f"https://www.tikwm.com/api/?url={url}", timeout=15).json()
        if res.get('code') == 0 and 'data' in res:
            return {'status': 'success', 'data': res['data']}
        return {'status': 'error', 'msg': res.get('msg', 'Gagal mengambil data TikTok.')}
    except Exception as e:
        return {'status': 'error', 'msg': f"Koneksi TikWM error: {str(e)}"}

# =============================================================================
# FLASK SERVER SETUP
# =============================================================================

app = Flask(__name__)

@app.route('/')
def index():
    # Mengarah penuh ke file frontend svetiktok.html
    return send_file('svetiktok.html')

@app.route('/api/ping', methods=['GET'])
def api_ping():
    return jsonify({"status": "ok", "message": "SVETIKTOK backend online"}), 200

@app.route('/api/search', methods=['POST'])
def search_videos_api():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if not check_rate_limit(ip):
        return jsonify({"status": "error", "msg": "Terlalu banyak request. Tunggu 1 menit."}), 429
        
    data = request.json or {}
    keywords = data.get('keywords', '').strip()
    count = min(int(data.get('count', 10)), 20)
    
    if not keywords:
        return jsonify({"status": "error", "msg": "Kata kunci kosong."}), 400
        
    try:
        res = requests.post(
            "https://www.tikwm.com/api/feed/search",
            data={"keywords": keywords, "count": count},
            timeout=15
        ).json()
        
        if res.get('code') == 0 and 'data' in res:
            v_list = res['data'].get('videos', [])
            return jsonify({"status": "success", "videos": v_list})
        return jsonify({"status": "error", "msg": res.get('msg', 'Pencarian gagal.')}), 400
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if not check_rate_limit(ip):
        return jsonify({"status": "error", "msg": "Terlalu banyak request. Tunggu 1 menit."}), 429

    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"status": "error", "msg": "URL kosong."}), 400

    res = fetch_tikwm(url)
    if res['status'] == 'success':
        d = res['data']
        return jsonify({
            "status": "success",
            "title": d.get('title', 'SVETIKTOK Video'),
            "play": d.get('play', ''),
            "hdplay": d.get('hdplay', d.get('play', ''))
        })
    return jsonify({"status": "error", "msg": res['msg']}), 400

@app.route('/api/fast_mp3', methods=['POST'])
def fast_mp3_api():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if not check_rate_limit(ip):
        return jsonify({"status": "error", "msg": "Terlalu banyak request."}), 429

    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"status": "error", "msg": "URL kosong."}), 400

    res = fetch_tikwm(url)
    if res['status'] == 'success':
        d = res['data']
        return jsonify({
            "status": "success",
            "audio_url": d.get('music', {}).get('play', d.get('music_url', ''))
        })
    return jsonify({"status": "error", "msg": res['msg']}), 400

@app.route('/api/get_video', methods=['GET'])
def get_video_stream():
    target_url = request.args.get('url')
    title = request.args.get('title', 'svetiktok_download')
    if not target_url:
        return "URL tidak ditemukan", 400

    clean_title = re.sub(r'[^\w\-_.]', '_', title)[:50]
    filename = f"svetiktok_{clean_title}.mp4"

    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=30)
        
        def generate():
            for chunk in r.iter_content(chunk_size=1024*64):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            headers={
                'Content-Type': 'video/mp4',
                'Content-Disposition': f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        return f"Streaming Error: {str(e)}", 500

# =============================================================================
# AUTO RUN BACKGROUND TASKS
# =============================================================================

def _run_daily_health_check():
    now_str = time.strftime("%d/%m/%Y %H:%M")
    report = f"📊 <b>SVETIKTOK Analitik Kesehatan Sistem</b>\n🕒 {now_str}\n\n"
    try:
        resp = requests.get(f"https://www.tikwm.com/api/?url={HEALTH_SAMPLES['TikTok']}", timeout=15).json()
        if resp.get('code') == 0:
            report += "🎵 TikTok Scraper Engine: <b>AKTIF (TikWM OK)</b>"
        else:
            report += f"🎵 TikTok Scraper Engine: <b>ERROR ({resp.get('msg')})</b>"
    except Exception as e:
        report += f"🎵 TikTok Scraper Engine: <b>CRASH ({str(e)})</b>"
    
    kirim_notif(report)

def _orphan_cleanup_loop():
    while True:
        try:
            now = time.time()
            for f in os.listdir(DOWNLOAD_DIR):
                fp = os.path.join(DOWNLOAD_DIR, f)
                if os.path.isfile(fp) and (now - os.path.getmtime(fp) > 600):
                    os.remove(fp)
        except Exception: pass
        time.sleep(180)

def _scheduler_loop():
    _last_health_check_date = ""
    while True:
        time.sleep(30)
        try:
            from datetime import datetime
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
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
            if resp.get('code') == 0: kirim_notif(f"💓 SVETIKTOK Heartbeat\n🕒 {now_str}\\n\\n🎵 TikTok ✅ TikWM aktif")
            else: kirim_notif(f"⚠️ SVETIKTOK Heartbeat\n🕒 {now_str}\\n\\n🎵 TikTok ❌ Gagal.")
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
        _time.sleep(240)

# Init Threads
threading.Thread(target=_orphan_cleanup_loop, daemon=True).start()
threading.Thread(target=_scheduler_loop, daemon=True).start()
threading.Thread(target=_heartbeat_cookies_loop, daemon=True).start()
threading.Thread(target=_self_ping_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
