import json
import os
import hashlib
import hmac
import html
import gzip
import re
import secrets
import sqlite3
import smtplib
import ssl
import shutil
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import requests
import streamlit as st

st.set_page_config(
    page_title="T.Ö.Z. — Anlam Yakınlığı Oyunu",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="collapsed"
)

MAX_ATTEMPTS = 20
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
DB_FILE = os.path.join(DATA_DIR, "game_data.db")
YEARLY_JSON_FILE = os.path.join(DATA_DIR, "year_game_data.json")
TR_TIMEZONE = timezone(timedelta(hours=3))
PBKDF2_ITERATIONS = 310000
DAILY_BUILD_LOCK = threading.Lock()

# --- YARDIMCI FONKSİYONLAR ---
def tr_lower(text):
    text = text.replace("İ", "i").replace("I", "ı")
    return unicodedata.normalize("NFC", text.strip().lower())

def tr_title(text):
    if not text:
        return ""
    words = text.split()
    res = []
    for w in words:
        if not w:
            continue
        first = w[0].replace('i', 'İ').replace('ı', 'I').upper()
        rest = tr_lower(w[1:])
        res.append(first + rest)
    return " ".join(res)

def hash_password(password):
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${password_hash.hex()}"


def verify_password(stored_hash, password):
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt_hex, expected_hash = stored_hash.split("$", 3)
            actual_hash = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
            ).hex()
            return hmac.compare_digest(actual_hash, expected_hash)
        except (ValueError, TypeError):
            return False
    legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(stored_hash, legacy_hash)


def get_smtp_setting(key, default=""):
    value = os.getenv(key)
    if value:
        return value
    try:
        if key in st.secrets:
            return str(st.secrets[key])
        email_settings = st.secrets.get("email", {})
        return str(email_settings.get(key, default))
    except Exception:
        return default


def send_verification_email(email, code):
    host = get_smtp_setting("SMTP_HOST")
    username = get_smtp_setting("SMTP_USER")
    password = get_smtp_setting("SMTP_PASSWORD")
    sender = get_smtp_setting("SMTP_FROM_EMAIL", username)
    port = int(get_smtp_setting("SMTP_PORT", "587"))
    if not host or not sender:
        raise RuntimeError("E-posta doğrulaması henüz yapılandırılmamış.")

    message = EmailMessage()
    message["Subject"] = "T.Ö.Z. doğrulama kodu"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"T.Ö.Z. hesabını doğrulamak için kodun: {code}\n\n"
        "Kod 10 dakika içinde geçerliliğini yitirir. Bu isteği sen başlatmadıysan bu e-postayı yok say."
    )

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(message)


def email_is_valid(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email))


def password_is_valid(password):
    return 1 <= len(password) <= 8 and password.isalpha() and password.islower()


def username_is_valid(username):
    return bool(re.fullmatch(r"[\w.-]{3,24}", username, re.UNICODE))


def save_verified_user(username, email, password_hash):
    users = load_users()
    normalized_username = username.strip()
    normalized_email = email.strip().lower()
    if normalized_username in users:
        return False, "Bu kullanıcı adı zaten alınmış."
    if any(user.get("email", "").lower() == normalized_email for user in users.values()):
        return False, "Bu e-posta adresi zaten kullanılıyor."
    users[normalized_username] = {
        "email": normalized_email,
        "password": password_hash,
        "scores": []
    }
    save_users(users)
    return True, "E-posta doğrulandı. Şimdi giriş yapabilirsin."

# --- SQLITE VERİTABANI ERİŞİMİ ---
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def load_game_meta_db(game_key):
    if not os.path.exists(DB_FILE):
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM games WHERE game_key = ?", (game_key,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_word_info_db(game_key, word):
    if not os.path.exists(DB_FILE):
        return {"rank": 99999, "sim": 0.0}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT rank, similarity FROM word_ranks WHERE game_key = ? AND word = ?", (game_key, word))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"rank": row["rank"], "sim": row["similarity"]}
    return {"rank": 99999, "sim": 0.0}

def get_word_by_rank_db(game_key, rank):
    if not os.path.exists(DB_FILE):
        return "Bulunamadı"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT word FROM word_ranks WHERE game_key = ? AND rank = ?", (game_key, rank))
    row = cursor.fetchone()
    conn.close()
    return row["word"] if row else "Bulunamadı"


def get_word_by_hint_rank_db(game_key, minimum_rank):
    if not os.path.exists(DB_FILE):
        return "Bulunamadı"
    conn = get_db_connection()
    row = conn.execute(
        "SELECT word FROM clean_word_ranks WHERE game_key = ? AND clean_rank = ?",
        (game_key, minimum_rank)
    ).fetchone()
    conn.close()
    return row["word"] if row else "Bulunamadı"


def save_tdk_definition_db(game_key, definition):
    conn = get_db_connection()
    conn.execute(
        "UPDATE games SET tdk_definition = ? WHERE game_key = ?",
        (definition, game_key)
    )
    conn.commit()
    conn.close()


@st.cache_data(ttl=30, show_spinner=False)
def cached_tdk_definition(target_word):
    from generate_daily_db import fetch_tdk_definition
    return fetch_tdk_definition(target_word)


@st.fragment(run_every="30s")
def render_tdk_definition(game_key, target_word, unlocked):
    current_game = load_game_meta_db(game_key) or {}
    definition = current_game.get("tdk_definition", "").strip()
    if "kavramı ile ilgili anlamsal ipucu." in definition:
        definition = ""

    if not definition:
        with st.spinner("TDK tanımı aranıyor..."):
            definition = cached_tdk_definition(target_word)
        if definition:
            save_tdk_definition_db(game_key, definition)

    cls = "hint-box-light unlocked" if unlocked else "hint-box-light"
    if unlocked and definition:
        value = f"<i>\"{html.escape(definition)}\"</i>"
    elif unlocked:
        value = "TDK yanıtı bekleniyor; otomatik olarak yeniden denenecek."
    else:
        value = "🔒 %90 Isıda Açılır"
    st.markdown(
        f"<div class='{cls}'><div class='hint-box-title'>TDK Tanımı</div><div class='hint-box-content'>{value}</div></div>",
        unsafe_allow_html=True
    )

    if unlocked and not definition and st.button("Hemen yeniden dene", key=f"tdk_retry_{game_key}"):
        cached_tdk_definition.clear(target_word)
        with st.spinner("TDK tanımı sorgulanıyor..."):
            definition = cached_tdk_definition(target_word)
        if definition:
            save_tdk_definition_db(game_key, definition)
            st.rerun(scope="fragment")
        else:
            st.warning("TDK servisi henüz yanıt vermedi; otomatik denemeler sürecek.")


def get_random_word_db(game_key):
    if not os.path.exists(DB_FILE):
        return "kelime"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT word FROM clean_word_ranks WHERE game_key = ? AND clean_rank BETWEEN 100 AND 2000 ORDER BY RANDOM() LIMIT 1", (game_key,))
    row = cursor.fetchone()
    conn.close()
    return row["word"] if row else "kelime"

# --- KULLANICI & LİDERLİK TABLOSU ---
def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users_data):
    os.makedirs("data", exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users_data, f, ensure_ascii=False, indent=2)

def authenticate_user(username, password):
    users = load_users()
    uname = username.strip()
    if uname not in users or not verify_password(users[uname].get("password", ""), password):
        return False
    if not users[uname]["password"].startswith("pbkdf2_sha256$"):
        users[uname]["password"] = hash_password(password)
        save_users(users)
    return True

def record_score(username, mode, score, words):
    if not username or username == "Anonim":
        return
    users = load_users()
    if username in users:
        users[username]["scores"].append({
            "mode": mode,
            "score": score,
            "words": words,
            "date": datetime.now(TR_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        })
        save_users(users)

def get_leaderboard(mode):
    users = load_users()
    board = []
    for uname, udata in users.items():
        for s in udata.get("scores", []):
            if s.get("mode") == mode:
                board.append({
                    "Oyuncu": uname,
                    "Skor": s["score"],
                    "Kelimeler": ", ".join([tr_title(w) for w in s.get("words", [])]),
                    "Tarih": s.get("date", "")
                })
    board.sort(key=lambda x: x["Skor"], reverse=True)
    return board[:10]


def initialize_daily_play_storage():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS daily_plays (
        username TEXT NOT NULL,
        game_date TEXT NOT NULL,
        mode TEXT NOT NULL,
        played_at TEXT NOT NULL,
        PRIMARY KEY (username, game_date)
    )
    """)
    conn.commit()
    conn.close()


def has_played_today(username, game_date):
    if st.session_state.get("session_played_date") == game_date:
        return True
    if not username:
        return False
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT 1 FROM daily_plays WHERE username = ? AND game_date = ?",
        (username, game_date)
    ).fetchone()
    conn.close()
    return row is not None


def reserve_daily_play(username, mode, game_date):
    if has_played_today(username, game_date):
        return False
    if username:
        conn = sqlite3.connect(DB_FILE)
        try:
            conn.execute(
                "INSERT INTO daily_plays (username, game_date, mode, played_at) VALUES (?, ?, ?, ?)",
                (username, game_date, mode, datetime.now(TR_TIMEZONE).isoformat(timespec="seconds"))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return False
        conn.close()
    st.session_state.session_played_date = game_date
    return True


def scheduled_game_date():
    with open(YEARLY_JSON_FILE, "r", encoding="utf-8") as file:
        yearly_data = json.load(file)
    today = datetime.now(TR_TIMEZONE).strftime("%Y-%m-%d")
    if today in yearly_data:
        return today
    first_date = next(iter(yearly_data))
    if today < first_date:
        return first_date
    return None


def database_file_matches(database_path, game_date, expected_keys):
    if not os.path.exists(database_path):
        return False
    conn = sqlite3.connect(database_path)
    try:
        row = conn.execute(
            "SELECT value FROM app_metadata WHERE key = 'daily_game_date'"
        ).fetchone()
        game_keys = {
            item[0] for item in conn.execute("SELECT game_key FROM games").fetchall()
        }
        clean_rank_counts = dict(conn.execute(
            "SELECT game_key, COUNT(*) FROM clean_word_ranks GROUP BY game_key"
        ).fetchall())
        return (
            row is not None and row[0] == game_date and game_keys == expected_keys
            and all(clean_rank_counts.get(key, 0) >= 150 for key in expected_keys)
        )
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def download_prebuilt_database(game_date, expected_keys):
    asset_name = f"game_data_{game_date}.db.gz"
    asset_url = (
        "https://github.com/mertkuleci/toz-anlam-oyunu/releases/download/"
        f"daily-databases/{asset_name}"
    )
    os.makedirs(DATA_DIR, exist_ok=True)
    archive_fd, archive_path = tempfile.mkstemp(suffix=".db.gz", dir=DATA_DIR)
    os.close(archive_fd)
    database_fd, database_temp_path = tempfile.mkstemp(suffix=".db", dir=DATA_DIR)
    os.close(database_fd)

    try:
        with requests.get(
            asset_url,
            headers={"User-Agent": "TOZ-daily-database/1.0"},
            stream=True,
            timeout=(10, 120)
        ) as response:
            if response.status_code == 404:
                return False, ""
            response.raise_for_status()
            with open(archive_path, "wb") as archive_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        archive_file.write(chunk)

        with gzip.open(archive_path, "rb") as archive_file, open(database_temp_path, "wb") as database_file:
            shutil.copyfileobj(archive_file, database_file, length=4 * 1024 * 1024)

        if not database_file_matches(database_temp_path, game_date, expected_keys):
            return False, "İndirilen günlük veritabanı doğrulamadan geçemedi."

        os.replace(database_temp_path, DB_FILE)
        return True, ""
    except (requests.RequestException, OSError, EOFError) as error:
        return False, f"Önceden hazırlanmış günlük veritabanı indirilemedi: {error}"
    finally:
        for temporary_path in (archive_path, database_temp_path):
            if os.path.exists(temporary_path):
                os.remove(temporary_path)


def ensure_daily_database(game_date):
    with DAILY_BUILD_LOCK:
        with open(YEARLY_JSON_FILE, "r", encoding="utf-8") as file:
            expected_keys = set(json.load(file)[game_date])
        if database_file_matches(DB_FILE, game_date, expected_keys):
            return True, ""

        downloaded, download_error = download_prebuilt_database(game_date, expected_keys)
        if downloaded:
            return True, ""
        if download_error:
            return False, download_error
        return False, (
            f"{game_date} için günlük veritabanı henüz üretilmedi. "
            "GitHub Actions'ta 'Build daily database' işinin tamamlanmasını bekleyin."
        )

# ==========================================
# T.Ö.Z. VISUAL SYSTEM
# ==========================================
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@500;600;700&display=swap');

    :root {
        --ink: #17251F;
        --muted: #6B7771;
        --line: #DDE4DE;
        --paper: #F5F7F3;
        --surface: #FFFFFF;
        --green: #1D5B48;
        --green-soft: #EAF2ED;
        --coral: #C95E43;
        --gold: #B8842F;
        --shadow: 0 14px 38px rgba(28, 48, 38, 0.07);
    }

    #MainMenu, footer, header, [data-testid="stHeader"] { display: none !important; }

    * { font-family: 'DM Sans', sans-serif !important; }

    .block-container {
        max-width: 1180px !important;
        padding: 42px 32px 64px !important;
    }

    html, body, [data-testid="stAppViewContainer"] {
        background-color: var(--paper) !important;
        background-image: radial-gradient(#D9E1DA 0.7px, transparent 0.7px) !important;
        background-size: 22px 22px !important;
        color: var(--ink) !important;
    }

    [data-testid="stAppViewContainer"] .main {
        animation: page-arrive 520ms cubic-bezier(.2,.75,.25,1) both;
    }

    @keyframes page-arrive {
        from { opacity: 0; transform: translateY(10px); }
        to { opacity: 1; transform: translateY(0); }
    }

    .brand-lockup {
        position: relative;
        display: flex;
        flex-direction: column;
        align-items: center;
        padding: 16px 0 34px;
        text-align: center;
    }
    .brand-lockup::after {
        content: '';
        width: 46px;
        height: 3px;
        margin-top: 18px;
        background: var(--coral);
        animation: brand-line 700ms 320ms cubic-bezier(.2,.75,.25,1) both;
        transform-origin: center;
    }
    .game-brand {
        display: flex;
        align-items: baseline;
        gap: 14px;
        margin-bottom: 22px;
        padding-bottom: 13px;
        border-bottom: 1px solid var(--line);
    }
    .game-brand strong {
        color: var(--ink);
        font-family: 'Fraunces', Georgia, serif !important;
        font-size: 1.55rem;
        font-weight: 600;
    }
    .game-brand span { color: var(--muted); font-size: 0.84rem; }
    .brand-kicker {
        color: var(--green);
        font-size: 0.72rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0;
        animation: word-reveal 500ms 80ms both;
    }
    .app-title {
        margin: 10px 0 0;
        color: var(--ink);
        font-family: 'Fraunces', Georgia, serif !important;
        font-size: 72px;
        font-weight: 600;
        line-height: 1;
        letter-spacing: 0;
    }
    .app-title span, .app-title i {
        display: inline-block;
        font-family: 'Fraunces', Georgia, serif !important;
        font-style: normal;
        animation: word-reveal 550ms cubic-bezier(.2,.75,.25,1) both;
    }
    .app-title span:nth-child(1) { animation-delay: 120ms; }
    .app-title i:nth-child(2) { animation-delay: 180ms; color: var(--coral); }
    .app-title span:nth-child(3) { animation-delay: 240ms; }
    .app-title i:nth-child(4) { animation-delay: 300ms; color: var(--gold); }
    .app-title span:nth-child(5) { animation-delay: 360ms; }
    .app-title i:nth-child(6) { animation-delay: 420ms; color: var(--green); }
    .app-sub {
        margin: 8px 0 0;
        color: var(--muted);
        font-size: 0.94rem;
        font-weight: 500;
        animation: word-reveal 600ms 320ms both;
    }

    @keyframes word-reveal {
        from { opacity: 0; transform: translateY(9px); }
        to { opacity: 1; transform: translateY(0); }
    }
    @keyframes brand-line {
        from { opacity: 0; transform: scaleX(.15); }
        to { opacity: 1; transform: scaleX(1); }
    }

    .section-title {
        margin: 0 0 9px;
        color: var(--ink);
        font-family: 'Fraunces', Georgia, serif !important;
        font-size: 1.34rem;
        font-weight: 600;
        letter-spacing: 0;
    }
    .mono-num { font-family: 'IBM Plex Mono', monospace !important; font-weight: 600; }

    [data-testid="stHorizontalBlock"] { gap: 26px; }
    [data-testid="stCaptionContainer"] { color: var(--muted); }
    [data-testid="stDivider"] { border-color: var(--line); }

    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 18px;
        border-bottom: 1px solid var(--line);
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        height: 42px;
        padding: 0 2px;
        color: var(--muted);
        font-size: 0.9rem;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        color: var(--green) !important;
        border-bottom-color: var(--green) !important;
    }

    div[data-testid="stForm"] {
        padding: 17px 18px 8px;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: rgba(255,255,255,.74);
    }
    .stTextInput > div > div > input {
        min-height: 44px;
        border: 1px solid var(--line) !important;
        border-radius: 7px !important;
        background: var(--surface) !important;
        color: var(--ink) !important;
        font-family: 'DM Sans', sans-serif !important;
        font-size: 0.95rem !important;
        box-shadow: none !important;
    }
    .stTextInput > div > div > input:focus {
        border-color: var(--green) !important;
        box-shadow: 0 0 0 3px rgba(29,91,72,.12) !important;
    }
    .stButton > button, [data-testid="stFormSubmitButton"] > button {
        min-height: 42px;
        border: 1px solid var(--line) !important;
        border-radius: 7px !important;
        background: var(--surface) !important;
        color: var(--ink) !important;
        font-family: 'DM Sans', sans-serif !important;
        font-size: 0.9rem !important;
        font-weight: 600 !important;
        box-shadow: none !important;
        transition: transform 160ms ease, background-color 160ms ease, border-color 160ms ease !important;
    }
    [data-testid="stFormSubmitButton"] > button {
        border-color: var(--green) !important;
        background: var(--green) !important;
        color: #FFFFFF !important;
    }
    .stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {
        transform: translateY(-1px);
        border-color: var(--green) !important;
        background: var(--green-soft) !important;
        color: var(--green) !important;
    }
    [data-testid="stFormSubmitButton"] > button:hover { background: #174B3B !important; color: #FFFFFF !important; }
    .stButton > button:disabled { opacity: .48; transform: none; }

    [data-testid="stDataFrame"] {
        overflow: hidden;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: var(--surface);
    }

    .v-heat-card {
        min-height: 370px;
        padding: 22px 14px;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--surface);
        box-shadow: var(--shadow);
        text-align: center;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: space-between;
    }
    .v-heat-title, .hint-box-title {
        color: var(--muted);
        font-size: 0.76rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0;
    }
    .v-heat-score { margin-top: 8px; color: var(--coral); font-size: 1.4rem; font-weight: 700; }
    .v-heat-track-container {
        position: relative;
        display: flex;
        flex-grow: 1;
        align-items: center;
        justify-content: center;
        margin: 16px 0;
        padding: 0 25px 0 10px;
    }
    .v-heat-track {
        position: relative;
        width: 22px;
        height: 230px;
        overflow: hidden;
        border: 1px solid #D7DFD9;
        border-radius: 99px;
        background: #EDF1ED;
    }
    .v-heat-fill {
        position: absolute;
        bottom: 0;
        left: 0;
        width: 100%;
        border-radius: 99px;
        background: linear-gradient(0deg, #488A6E 0%, #D5A849 58%, #C95E43 100%);
        transition: height 650ms cubic-bezier(.16,1,.3,1);
    }
    .v-marker { position: absolute; left: 0; z-index: 3; width: 100%; border-top: 1px dashed rgba(23,37,31,.34); pointer-events: none; }
    .v-marker span {
        position: absolute;
        top: -9px;
        right: -36px;
        padding: 1px 4px;
        border: 1px solid var(--line);
        border-radius: 4px;
        background: var(--paper);
        color: var(--muted);
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.65rem;
        font-weight: 600;
    }
    .v-heat-sub { color: var(--muted); font-size: 0.8rem; font-weight: 500; }

    .hint-box-light {
        min-height: 100px;
        padding: 14px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: rgba(255,255,255,.72);
        transition: border-color 180ms ease, background-color 180ms ease;
    }
    .hint-box-light.unlocked { border-color: #B5CEBF; background: #ECF3EE; }
    .hint-box-title { margin-bottom: 7px; font-size: 0.7rem; letter-spacing: 0; }
    .hint-box-light.unlocked .hint-box-title { color: var(--green); }
    .hint-box-content { color: var(--ink); font-size: 0.9rem; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }

    .guess-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 6px;
        padding: 10px 14px;
        border: 1px solid var(--line);
        border-radius: 7px;
        background: var(--surface);
        font-size: 0.92rem;
        font-weight: 600;
    }
    .badge-volcano { border-left: 4px solid #C95E43; background: #FBEEEA; }
    .badge-hot { border-left: 4px solid #D08439; background: #FBF2E5; }
    .badge-warm { border-left: 4px solid #C29A39; background: #F7F3E5; }
    .badge-cold { border-left: 4px solid #5B8791; background: #EAF1F2; }
    .badge-ice { border-left: 4px solid #83948A; background: #EFF2EF; }

    @media (max-width: 760px) {
        .block-container { padding: 26px 16px 40px !important; }
        .app-title { font-size: 58px; }
        .brand-lockup { padding-bottom: 26px; }
        [data-testid="stHorizontalBlock"] { gap: 16px; }
        .v-heat-card { min-height: 300px; }
        .v-heat-track { height: 180px; }
        .hint-box-light { min-height: 82px; padding: 11px; }
        .guess-row { align-items: flex-start; flex-direction: column; }
    }

    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            scroll-behavior: auto !important;
            animation-duration: 0.01ms !important;
            animation-iteration-count: 1 !important;
            transition-duration: 0.01ms !important;
        }
    }
</style>
""", unsafe_allow_html=True)

# --- OTURUM BAŞLATMA ---
today_str = scheduled_game_date()
if today_str is None:
    st.error("Kelime takvimi sona ermiş. Yeni bir yıllık kelime havuzu oluşturulmalı.")
    st.stop()

initialize_daily_play_storage()
with st.spinner("Günün oyunu hazırlanıyor..."):
    database_ready, database_error = ensure_daily_database(today_str)
if not database_ready:
    st.error(f"Günün oyunu hazırlanamadı: {database_error}")
    st.stop()

if "user" not in st.session_state:
    st.session_state.user = None
if "page" not in st.session_state:
    st.session_state.page = "welcome"
if "game_date" not in st.session_state or st.session_state.game_date != today_str:
    st.session_state.game_date = today_str
    st.session_state.page = "welcome"

def get_tier_info(item):
    rank = item.get("rank", 99999)
    sim = item.get("sim", 0.0)
    
    if rank == 1:
        return "🏆", "Zafer", 100.0, "badge-volcano"
    elif rank <= 10 or sim >= 55.0:
        return "💥", "Yanardağ", 30.0, "badge-volcano"
    elif rank <= 100 or sim >= 45.0:
        return "🔥", "Çok Sıcak", 20.0, "badge-hot"
    elif rank <= 300 or sim >= 38.0:
        return "☀️", "Sıcak", 15.0, "badge-hot"
    elif rank <= 800 or sim >= 30.0:
        return "🌤️️", "Ilık", 12.0, "badge-warm"
    elif rank <= 2000 or sim >= 22.0:
        return "⛅", "Ortalama", 9.0, "badge-warm"
    elif rank <= 6000 or sim >= 15.0:
        return "🌦️", "Serin", 6.0, "badge-cold"
    elif rank <= 15000 or sim >= 10.0:
        return "🌧️", "Soğuk", 4.0, "badge-cold"
    elif rank <= 30000 or sim >= 5.0:
        return "❄️", "Çok Soğuk", 3.0, "badge-ice"
    else:
        return "🧊", "Buz Gibi", 2.0, "badge-ice"

def start_new_game(mode):
    if mode == "1_word":
        game_keys = ["mode_1_word_kelime_1"]
    else:
        game_keys = ["mode_3_word_kelime_1", "mode_3_word_kelime_2", "mode_3_word_kelime_3"]

    if not reserve_daily_play(st.session_state.user, mode, st.session_state.game_date):
        return False

    st.session_state.game_mode = mode
    st.session_state.max_steps = len(game_keys)
    st.session_state.active_game_keys = game_keys
    st.session_state.current_step = 1
    st.session_state.game_states = {}
    st.session_state.score_recorded = False

    for idx, key in enumerate(game_keys, start=1):
        st.session_state.game_states[idx] = {
            "game_key": key,
            "guesses": [],
            "won": False,
            "joker_first_letter": False,
            "joker_last_letter": False,
            "joker_top_words": False,
            "failed_final": False
        }
    st.session_state.page = "playing"
    return True

# ==========================================
# 1. MENÜ VE HESAP EKRANI (WELCOME PAGE)
# ==========================================
if st.session_state.page == "welcome":
    st.markdown(f"""
    <div class="brand-lockup">
        <div class="brand-kicker">GÜNLÜK ANLAM OYUNU · {today_str}</div>
        <h1 class="app-title"><span>T</span><i>.</i><span>Ö</span><i>.</i><span>Z</span><i>.</i></h1>
        <p class="app-sub">Sözcüklerin birbirine yaklaştığı yer.</p>
    </div>
    """, unsafe_allow_html=True)
    
    col_left, col_right = st.columns([1.05, 0.95], gap="large")
    
    with col_left:
        st.markdown("<div class='section-title'>🎮 Oyun Modu Seçin</div>", unsafe_allow_html=True)
        st.caption("Günün gizli kelimelerini çözmek için modunuzu seçin:")
        played_today = has_played_today(st.session_state.user, today_str)
        if played_today:
            st.info("Bugünün oyun hakkı kullanıldı. Yeni oyun yarın açılacak.")

        c_btn1, c_btn2 = st.columns(2)
        with c_btn1:
            if st.button("Tek Kelime\n(1 Kelime)", width="stretch", disabled=played_today):
                if start_new_game("1_word"):
                    st.rerun()
                
        with c_btn2:
            if st.button("Üç Kelime\n(3 Kelime)", width="stretch", disabled=played_today):
                if start_new_game("3_word"):
                    st.rerun()
        st.divider()
        st.markdown("<div class='section-title'>👤 Oyuncu Hesabı</div>", unsafe_allow_html=True)
        if st.session_state.user:
            st.write(f"Aktif Oturum: **{st.session_state.user}**")
            if st.button("Çıkış Yap"):
                st.session_state.user = None
                st.rerun()
        else:
            t_login, t_reg = st.tabs(["Giriş Yap", "Kayıt Ol"])
            with t_login:
                with st.form("form_login"):
                    u_in = st.text_input("Kullanıcı Adı")
                    p_in = st.text_input("Şifre", type="password")
                    if st.form_submit_button("Giriş Yap", width="stretch"):
                        if authenticate_user(u_in, p_in):
                            st.session_state.user = u_in
                            st.toast("Giriş yapıldı.", icon="✅")
                            st.rerun()
                        else:
                            st.error("Kullanıcı adı veya şifre hatalı.")
            with t_reg:
                pending = st.session_state.get("pending_registration")
                if pending:
                    st.markdown("#### E-posta doğrulaması")
                    st.caption(f"Doğrulama kodu {pending['email']} adresine gönderildi.")
                    with st.form("form_verify_registration"):
                        verification_code = st.text_input(
                            "E-postandaki 6 haneli kod", max_chars=6, placeholder="000000"
                        )
                        verify_submitted = st.form_submit_button("Doğrula ve kaydol", width="stretch")
                    if verify_submitted:
                        if time.time() > pending["expires_at"]:
                            st.session_state.pop("pending_registration", None)
                            st.error("Kodun süresi doldu. Yeniden kayıt başlat.")
                        elif not re.fullmatch(r"\d{6}", verification_code):
                            st.error("Altı haneli doğrulama kodunu gir.")
                        elif hmac.compare_digest(
                            hashlib.sha256(verification_code.encode("utf-8")).hexdigest(),
                            pending["code_hash"]
                        ):
                            ok, message = save_verified_user(
                                pending["username"], pending["email"], pending["password_hash"]
                            )
                            st.session_state.pop("pending_registration", None)
                            if ok:
                                st.success(message)
                            else:
                                st.error(message)
                        else:
                            pending["attempts"] += 1
                            if pending["attempts"] >= 5:
                                st.session_state.pop("pending_registration", None)
                                st.error("Çok fazla hatalı kod girildi. Yeniden kayıt başlat.")
                            else:
                                st.session_state.pending_registration = pending
                                st.error("Doğrulama kodu hatalı.")

                    if st.button("Kodu yeniden gönder", width="stretch"):
                        if time.time() - pending["sent_at"] < 60:
                            st.warning("Yeni kod istemeden önce bir dakika bekle.")
                        else:
                            new_code = f"{secrets.randbelow(1000000):06d}"
                            try:
                                send_verification_email(pending["email"], new_code)
                                pending["code_hash"] = hashlib.sha256(new_code.encode("utf-8")).hexdigest()
                                pending["sent_at"] = time.time()
                                pending["expires_at"] = pending["sent_at"] + 600
                                pending["attempts"] = 0
                                st.session_state.pending_registration = pending
                                st.success("Yeni doğrulama kodu gönderildi.")
                            except smtplib.SMTPAuthenticationError:
                                st.error("SMTP kimlik doğrulaması reddedildi. Kullanıcı adını ve uygulama parolasını kontrol et.")
                            except (smtplib.SMTPConnectError, OSError):
                                st.error("SMTP sunucusuna bağlanılamadı. Sunucu adresi ve portu kontrol et.")
                            except RuntimeError as error:
                                st.error(str(error))
                            except Exception as error:
                                st.error(f"E-posta gönderimi başarısız ({type(error).__name__}).")
                else:
                    with st.form("form_reg"):
                        email_input = st.text_input("E-posta", placeholder="ornek@eposta.com")
                        username_input = st.text_input("Kullanıcı adı")
                        password_input = st.text_input("Şifre", type="password")
                        password_repeat = st.text_input("Şifre tekrar", type="password")
                        send_code = st.form_submit_button("Doğrulama kodu gönder", width="stretch")
                    if send_code:
                        normalized_email = email_input.strip().lower()
                        normalized_username = username_input.strip()
                        existing_users = load_users()
                        email_exists = any(
                            user.get("email", "").lower() == normalized_email
                            for user in existing_users.values()
                        )
                        if not email_is_valid(normalized_email):
                            st.error("Geçerli bir e-posta adresi gir.")
                        elif not username_is_valid(normalized_username):
                            st.error("Kullanıcı adı 3-24 karakter olmalı; harf, sayı, nokta, tire veya alt çizgi kullan.")
                        elif not password_is_valid(password_input):
                            st.error("Şifre 1-8 karakter olmalı ve yalnızca küçük harf içermeli.")
                        elif password_input != password_repeat:
                            st.error("Şifreler eşleşmiyor.")
                        elif normalized_username in existing_users:
                            st.error("Bu kullanıcı adı zaten alınmış.")
                        elif email_exists:
                            st.error("Bu e-posta adresi zaten kullanılıyor.")
                        else:
                            verification_code = f"{secrets.randbelow(1000000):06d}"
                            try:
                                send_verification_email(normalized_email, verification_code)
                                sent_at = time.time()
                                st.session_state.pending_registration = {
                                    "email": normalized_email,
                                    "username": normalized_username,
                                    "password_hash": hash_password(password_input),
                                    "code_hash": hashlib.sha256(verification_code.encode("utf-8")).hexdigest(),
                                    "sent_at": sent_at,
                                    "expires_at": sent_at + 600,
                                    "attempts": 0
                                }
                                st.rerun()
                            except smtplib.SMTPAuthenticationError:
                                st.error("SMTP kimlik doğrulaması reddedildi. Kullanıcı adını ve uygulama parolasını kontrol et.")
                            except (smtplib.SMTPConnectError, OSError):
                                st.error("SMTP sunucusuna bağlanılamadı. Sunucu adresi ve portu kontrol et.")
                            except RuntimeError as error:
                                st.error(str(error))
                            except Exception as error:
                                st.error(f"E-posta gönderimi başarısız ({type(error).__name__}).")
    with col_right:
        st.markdown("<div class='section-title'>🏆 Liderlik Tablosu</div>", unsafe_allow_html=True)
        tab_l1, tab_l2 = st.tabs(["Tek Kelime", "Üç Kelime"])
        with tab_l1:
            d1 = get_leaderboard("1_word")
            if d1: st.dataframe(d1, width="stretch", hide_index=True)
            else: st.caption("Henüz skor kaydı bulunmuyor.")
        with tab_l2:
            d3 = get_leaderboard("3_word")
            if d3: st.dataframe(d3, width="stretch", hide_index=True)
            else: st.caption("Henüz skor kaydı bulunmuyor.")
# ==========================================
# 2. OYUN EKRANI (PLAYING PAGE)
# ==========================================
elif st.session_state.page == "playing":
    step = st.session_state.current_step
    max_steps = st.session_state.max_steps
    current_state = st.session_state.game_states[step]
    game_key = current_state["game_key"]
    
    game_meta = load_game_meta_db(game_key)
    if not game_meta:
        st.error(f"'{game_key}' verileri yüklenemedi. Lütfen 'python generate_daily_db.py' çalıştırın.")
        st.stop()

    word_rank_5 = get_word_by_hint_rank_db(game_key, 5)
    word_rank_10 = get_word_by_hint_rank_db(game_key, 10)

    # Üst Gezinti
    c_nav1, c_nav2 = st.columns([3, 1])
    with c_nav1:
        p_name = st.session_state.user if st.session_state.user else "Anonim Oyuncu"
        mode_label = "Tek kelime" if st.session_state.game_mode == "1_word" else "Üç kelime"
        st.markdown(
            f"<div class='game-brand'><strong>T.Ö.Z.</strong><span>{p_name} · {mode_label} · {today_str}</span></div>",
            unsafe_allow_html=True
        )
    with c_nav2:
        if st.button("← Ana Menü", width="stretch"):
            st.session_state.page = "welcome"
            st.rerun()

    # Bölüm Adımları
    step_cols = st.columns(max_steps)
    for i in range(1, max_steps + 1):
        s_data = st.session_state.game_states[i]
        attempts_used = len(s_data["guesses"])
        failed = (attempts_used >= MAX_ATTEMPTS or s_data.get("failed_final", False)) and not s_data["won"]
        
        with step_cols[i - 1]:
            if s_data["won"]:
                st.success(f"Kelime {i}: Tamamlandı")
            elif failed:
                st.error(f"Kelime {i}: Bitti")
            elif i == step:
                st.info(f"Kelime {i}: Oynanıyor")
            else:
                st.caption(f"Kelime {i}: Kilitli")

    # Isı Hesabı
    total_heat = sum(get_tier_info(item)[2] for item in current_state["guesses"])
    heat_score = min(100.0, total_heat)
    is_final_chance = (heat_score >= 100.0) and not current_state["won"] and not current_state.get("failed_final", False)
    attempts_left = MAX_ATTEMPTS - len(current_state["guesses"])

    # ANA OYUN ALANI
    col_thermometer, col_main_game = st.columns([1, 3.2], gap="medium")

    with col_thermometer:
        st.markdown(f"""
        <div class='v-heat-card'>
            <div class='v-heat-title'>Semantik Isı</div>
            <div class='v-heat-score mono-num'>%{heat_score:.1f}</div>
            <div class='v-heat-track-container'>
                <div class='v-heat-track'>
                    <div class='v-heat-fill' style='height: {heat_score}%;'></div>
                    <div class='v-marker' style='bottom: 90%;'><span>90%</span></div>
                    <div class='v-marker' style='bottom: 60%;'><span>60%</span></div>
                    <div class='v-marker' style='bottom: 30%;'><span>30%</span></div>
                </div>
            </div>
            <div class='v-heat-sub'>Kalan Hak: <span class='mono-num'>{attempts_left}</span></div>
        </div>
        """, unsafe_allow_html=True)

    with col_main_game:
        # İpucu Seviyeleri
        st.markdown("<div class='section-title'>💡 İpucu Seviyeleri</div>", unsafe_allow_html=True)
        col_i1, col_i2, col_i3 = st.columns(3)
        
        with col_i1:
            unlocked = heat_score >= 30
            cls = "hint-box-light unlocked" if unlocked else "hint-box-light"
            val = f"#50: <b>{tr_title(game_meta['word_50'])}</b><br>#100: <b>{tr_title(game_meta['word_100'])}</b><br>#150: <b>{tr_title(game_meta['word_150'])}</b>" if unlocked else "🔒 %30 Isıda Açılır"
            st.markdown(f"<div class='{cls}'><div class='hint-box-title'>50., 100. ve 150. Yakın Sözcük</div><div class='hint-box-content'>{val}</div></div>", unsafe_allow_html=True)

        with col_i2:
            unlocked = heat_score >= 60
            cls = "hint-box-light unlocked" if unlocked else "hint-box-light"
            val = f"<b>{game_meta['target_length']}</b> Harfli Kelime" if unlocked else "🔒 %60 Isıda Açılır"
            st.markdown(f"<div class='{cls}'><div class='hint-box-title'>Harf Sayısı</div><div class='hint-box-content'>{val}</div></div>", unsafe_allow_html=True)

        with col_i3:
            unlocked = heat_score >= 90
            render_tdk_definition(game_key, game_meta["target_word"], unlocked)

        # Aktif Jokerler
        if current_state.get("joker_first_letter", False):
            st.info(f"🔤 **İlk Harf Jokeri:** Hedef kelime **{game_meta['first_letter'].upper()}** harfi ile başlıyor.")

        if current_state.get("joker_last_letter", False):
            target_last_letter = game_meta['target_word'][-1].upper()
            st.info(f"🔤 **Son Harf Jokeri:** Hedef kelime **{target_last_letter}** harfi ile bitiyor.")
            
        if current_state.get("joker_top_words", False):
            st.info(f"🎯 **5. ve 10. Kelime Jokeri:** En yakın #5: **{tr_title(word_rank_5)}** | En yakın #10: **{tr_title(word_rank_10)}**")

        st.write("")

        # Tahmin Input ve Jokerler
        is_disabled = bool(current_state["won"] or attempts_left <= 0 or current_state.get("failed_final", False))

        col_j1, col_j2, col_j3, col_j4 = st.columns(4)
        with col_j1:
            if st.button("🎲 Rastgele Öner", disabled=is_disabled, width="stretch"):
                suggested = get_random_word_db(game_key)
                st.session_state[f"input_val_{step}"] = tr_title(suggested)
                st.toast(f"Öneri: {tr_title(suggested)}", icon="💡")

        with col_j2:
            if st.button("🔤 İlk Harf (-5 Pn)", disabled=bool(is_disabled or current_state.get("joker_first_letter", False)), width="stretch"):
                current_state["joker_first_letter"] = True
                st.rerun()

        with col_j3:
            if st.button("🔤 Son Harf (-5 Pn)", disabled=bool(is_disabled or current_state.get("joker_last_letter", False)), width="stretch"):
                current_state["joker_last_letter"] = True
                st.rerun()

        with col_j4:
            if st.button("🎯 5. & 10. Kelime (-5 Pn)", disabled=bool(is_disabled or current_state.get("joker_top_words", False)), width="stretch"):
                current_state["joker_top_words"] = True
                st.rerun()

        input_default = st.session_state.get(f"input_val_{step}", "")

        with st.form(key=f"form_step_{step}", clear_on_submit=True):
            guess_raw = st.text_input("Tahmininizi yazın:", value=input_default, disabled=is_disabled, key=f"input_{step}", placeholder="Bir kelime yazın...")
            submit_btn = st.form_submit_button("Tahmin Et", disabled=is_disabled, width="stretch")

        if submit_btn and guess_raw:
            guess_input = tr_lower(guess_raw)
            st.session_state[f"input_val_{step}"] = ""
            
            already_guessed = any(g["word"] == guess_input for g in current_state["guesses"])
            hint_words = [
                tr_lower(game_meta.get("word_50", "")),
                tr_lower(game_meta.get("word_100", "")),
                tr_lower(game_meta.get("word_150", ""))
            ]
            
            if already_guessed:
                st.warning("Bu kelimeyi zaten denediniz.")
            elif heat_score >= 30 and guess_input in hint_words:
                st.warning("⚠️ Bu kelime ipucu olarak zaten verildi.")
            else:
                info = get_word_info_db(game_key, guess_input)
                
                current_state["guesses"].append({
                    "word": guess_input,
                    "rank": info["rank"],
                    "sim": info["sim"]
                })

                if info["rank"] == 1:
                    current_state["won"] = True
                    st.balloons()
                elif is_final_chance:
                    current_state["failed_final"] = True
                elif info["rank"] == 99999:
                    st.toast(f"'{tr_title(guess_input)}' eklendi (+%2 Isı).", icon="💡")
                    
                st.rerun()

        # Bitiş Mesajı
        if (attempts_left <= 0 or current_state.get("failed_final", False)) and not current_state["won"]:
            target_w = tr_title(game_meta["target_word"])
            st.error(f"❌ Haklarınız bitti. Aranan kelime: **{target_w}**")

        # Tahmin Akışı
        if current_state["guesses"]:
            st.markdown("<div class='section-title' style='margin-top:16px;'>Tahmin Akışı</div>", unsafe_allow_html=True)
            sorted_guesses = sorted(current_state["guesses"], key=lambda x: x["rank"])

            for item in sorted_guesses:
                word = tr_title(item["word"])
                sim = item["sim"]
                rank = item["rank"]
                icon, label, points, badge_css = get_tier_info(item)
                
                if rank == 1:
                    st.success(f"🎉 **{word}** — Doğru Kelime! (Sıralama: #1)")
                elif rank > 30000:
                    st.markdown(f"""
                    <div class='guess-row {badge_css}'>
                        <span>{icon} <b>{word}</b></span>
                        <span style='color:#475569; font-size:0.85rem;'>{label} • <span class='mono-num'>%{sim:.1f}</span> (+%{points:.0f} Isı)</span>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.markdown(f"""
                    <div class='guess-row {badge_css}'>
                        <span>{icon} <b>{word}</b> <span class='mono-num' style='color:#94A3B8; font-size:0.8rem; margin-left:6px;'>#{rank}</span></span>
                        <span style='color:#475569; font-size:0.85rem;'>{label} • <span class='mono-num'>%{sim:.1f}</span> (+%{points:.0f} Isı)</span>
                    </div>
                    """, unsafe_allow_html=True)

    # Bitiş Akışı
    is_step_finished = current_state["won"] or attempts_left <= 0 or current_state.get("failed_final", False)

    if is_step_finished:
        st.divider()
        
        if current_state["won"]:
            rem_att = max(0, MAX_ATTEMPTS - len(current_state["guesses"]))
            base_score = 50
            joker_penalty = (
                (5 if current_state.get("joker_first_letter", False) else 0) +
                (5 if current_state.get("joker_last_letter", False) else 0) +
                (5 if current_state.get("joker_top_words", False) else 0)
            )
            step_score = max(5, base_score + (rem_att * 2) - joker_penalty)
        else:
            step_score = 0
            
        current_state["score"] = step_score
        st.info(f"📊 Bölüm Puanınız: **{step_score} Puan**")

        if step < max_steps:
            if st.button(f"Sonraki Kelime ({step + 1}/{max_steps}) →", type="primary", width="stretch"):
                st.session_state.current_step += 1
                st.rerun()
        else:
            all_finished = all(
                st.session_state.game_states[s]["won"] or 
                len(st.session_state.game_states[s]["guesses"]) >= MAX_ATTEMPTS or 
                st.session_state.game_states[s].get("failed_final", False) 
                for s in range(1, max_steps + 1)
            )
            if all_finished:
                total_game_score = sum(st.session_state.game_states[s].get("score", 0) for s in range(1, max_steps + 1))
                st.balloons()
                st.success(f"🏁 Oyun Tamamlandı! Toplam Puanınız: **{total_game_score}**")
                
                guessed_words = []
                for s in range(1, max_steps + 1):
                    gk = st.session_state.game_states[s]["game_key"]
                    m = load_game_meta_db(gk)
                    if m:
                        guessed_words.append(m["target_word"])
                
                if not st.session_state.get("score_recorded", False):
                    if st.session_state.user:
                        record_score(st.session_state.user, st.session_state.game_mode, total_game_score, guessed_words)
                        st.toast("Skorunuz kaydedildi.", icon="🏆")
                    else:
                        st.warning("Anonim modda olduğunuz için skor kaydedilmedi.")
                    st.session_state.score_recorded = True
                    
                if st.button("Ana Menüye Dön"):
                    st.session_state.page = "welcome"
                    st.rerun()