import argparse
import logging
import os
import json
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
import requests
from gensim.models import KeyedVectors
from zeyrek import MorphAnalyzer

DB_PATH = "data/game_data.db"
MODEL_PATH = "data/wiki.tr.vec"
YEARLY_JSON_PATH = "data/year_game_data.json"
CLEAN_ROOTS_PATH = "data/clean_roots.txt"
TR_TIMEZONE = timezone(timedelta(hours=3))
MAX_HINT_VOCABULARY = 60000

def tr_lower(text):
    text = text.replace("İ", "i").replace("I", "ı")
    return unicodedata.normalize("NFC", text.strip().lower())


def load_clean_words():
    with open(CLEAN_ROOTS_PATH, "r", encoding="utf-8") as file:
        return {tr_lower(line) for line in file if line.strip()}


def filter_clean_roots(words):
    analyzer = MorphAnalyzer()
    accepted_morphemes = {
        ("Noun", "A3sg"),
        ("Adj",),
        ("Adv",),
        ("Verb", "Inf1", "Noun", "A3sg")
    }
    analyzer_logger = logging.getLogger("zeyrek.rulebasedanalyzer")
    previous_log_level = analyzer_logger.level
    analyzer_logger.setLevel(logging.ERROR)
    try:
        clean_words = set()
        for word in words:
            analyses = analyzer._parse(word)
            if not analyses or any(
                tr_lower(analysis.dict_item.lemma) != word
                for analysis in analyses
            ):
                continue
            for analysis in analyses:
                if "ProperNoun" in str(analysis.dict_item.secondary_pos):
                    continue
                morphemes = tuple(morpheme[0].id_ for morpheme in analysis.morphemes)
                if morphemes in accepted_morphemes:
                    clean_words.add(word)
                    break
        return clean_words
    finally:
        analyzer_logger.setLevel(previous_log_level)

def fetch_tdk_definition(word):
    try:
        response = requests.get(
            "https://sozluk.gov.tr/gts",
            params={"ara": word},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5
        )
        response.raise_for_status()
        entries = response.json()
        if not isinstance(entries, list):
            return ""
        for entry in entries:
            for meaning in entry.get("anlamlarListe", []):
                definition = meaning.get("anlam", "").strip()
                if definition:
                    return definition
    except (requests.RequestException, ValueError, TypeError):
        return ""
    return ""

def init_db(conn):
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS games (
        game_key TEXT PRIMARY KEY,
        target_word TEXT,
        target_length INTEGER,
        tdk_definition TEXT,
        first_letter TEXT,
        word_50 TEXT,
        word_100 TEXT,
        word_150 TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS word_ranks (
        game_key TEXT,
        word TEXT,
        rank INTEGER,
        similarity REAL,
        PRIMARY KEY (game_key, word)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_word_lookup ON word_ranks(game_key, word)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rank_lookup ON word_ranks(game_key, rank)")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clean_word_ranks (
        game_key TEXT,
        word TEXT,
        clean_rank INTEGER,
        model_rank INTEGER,
        similarity REAL,
        PRIMARY KEY (game_key, clean_rank),
        UNIQUE (game_key, word)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_clean_rank_lookup ON clean_word_ranks(game_key, clean_rank)")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_plays (
        username TEXT NOT NULL,
        game_date TEXT NOT NULL,
        mode TEXT NOT NULL,
        played_at TEXT NOT NULL,
        PRIMARY KEY (username, game_date)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    conn.commit()

def build_daily_database(target_date=None):
    os.makedirs("data", exist_ok=True)

    # 1. 365 Günlük JSON dosyasından bugünün kelimelerini al
    if not os.path.exists(YEARLY_JSON_PATH):
        raise FileNotFoundError(
            f"'{YEARLY_JSON_PATH}' bulunamadı. Önce generate_year_json.py çalıştırın."
        )

    with open(YEARLY_JSON_PATH, "r", encoding="utf-8") as f:
        yearly_data = json.load(f)

    today_str = target_date or datetime.now(TR_TIMEZONE).strftime("%Y-%m-%d")
    if today_str not in yearly_data:
        first_date = next(iter(yearly_data))
        last_date = next(reversed(yearly_data))
        if target_date is None and today_str < first_date:
            print(f"⚠️ Havuz {first_date} tarihinde başlıyor; ilk gün için veritabanı hazırlanıyor.")
            today_str = first_date
        else:
            raise ValueError(f"{today_str} havuzda yok. Desteklenen aralık: {first_date} - {last_date}")

    games_config = yearly_data[today_str]

    daily_targets = list(games_config.values())

    print(f"📅 Günün Tarihi: {today_str}")
    print(f"🎯 JSON'dan Seçilen Günün Kelimeleri: {daily_targets}")

    # 2. Vektör Modelini Yükle
    print(f"🧠 Vektör modeli yükleniyor ({MODEL_PATH})...")
    try:
        model = KeyedVectors.load_word2vec_format(MODEL_PATH, binary=False)
    except Exception as e:
        raise RuntimeError(f"Vektör modeli yüklenemedi: {e}") from e

    print(f"✅ Model yüklendi. Toplam kelime sayısı: {len(model.index_to_key)}")
    clean_words_set = load_clean_words()
    frequent_words = {tr_lower(word) for word in model.index_to_key[:MAX_HINT_VOCABULARY]}
    hint_candidates = clean_words_set & frequent_words
    print(f"📚 {len(hint_candidates)} kök aday biçimbilim filtresinden geçiriliyor...")
    clean_words_set = filter_clean_roots(hint_candidates)
    print(f"✅ {len(clean_words_set)} yalın ve yaygın ipucu kelimesi hazır.")

    missing_targets = [target for target in daily_targets if target not in model]
    if missing_targets:
        raise ValueError(f"Vektör modelinde bulunmayan hedef kelimeler: {missing_targets}")

    # Tahmin havuzu için modeldeki kelimeler
    all_candidates = [tr_lower(w) for w in model.index_to_key if tr_lower(w).isalpha()]
    all_candidates = list(dict.fromkeys(all_candidates))

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    cursor = conn.cursor()
    active_game_keys = tuple(games_config)
    placeholders = ", ".join("?" for _ in active_game_keys)
    for table in ("games", "word_ranks", "clean_word_ranks"):
        cursor.execute(
            f"DELETE FROM {table} WHERE game_key NOT IN ({placeholders})",
            active_game_keys
        )
    conn.commit()

    for game_key, target in games_config.items():
        print(f"\n⚙️ '{game_key}' ({target}) için tüm model sıralanıyor...")

        tdk_def = fetch_tdk_definition(target)

        all_words_sim = []
        for word in all_candidates:
            if word in model:
                sim = float(model.similarity(target, word)) * 100.0
                all_words_sim.append((word, sim))

        # Benzerliğe göre sırala
        all_words_sim.sort(key=lambda x: x[1], reverse=True)

        clean_ranked_data = []
        for model_rank, (word, sim) in enumerate(all_words_sim, start=1):
            if word in clean_words_set:
                clean_rank = len(clean_ranked_data) + 1
                clean_ranked_data.append(
                    (game_key, word, clean_rank, model_rank, round(float(sim), 2))
                )

        word_50 = clean_ranked_data[49][1] if len(clean_ranked_data) >= 50 else ""
        word_100 = clean_ranked_data[99][1] if len(clean_ranked_data) >= 100 else ""
        word_150 = clean_ranked_data[149][1] if len(clean_ranked_data) >= 150 else ""

        ranked_data = []
        for rank, (w, sim) in enumerate(all_words_sim, start=1):
            ranked_data.append((game_key, w, rank, round(sim, 2)))

        cursor.execute("""
        INSERT OR REPLACE INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game_key,
            target,
            len(target),
            tdk_def,
            target[0],
            word_50,
            word_100,
            word_150
        ))

        cursor.executemany("""
        INSERT OR REPLACE INTO word_ranks VALUES (?, ?, ?, ?)
        """, ranked_data)

        cursor.executemany("""
        INSERT OR REPLACE INTO clean_word_ranks VALUES (?, ?, ?, ?, ?)
        """, clean_ranked_data)

        conn.commit()
        print(f"✅ '{game_key}' ({target}) tamamlandı! İpuçları: #50={word_50}, #100={word_100}, #150={word_150}")

    cursor.execute(
        "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
        ("daily_game_date", today_str)
    )
    conn.commit()
    conn.close()
    print("\n🎉 Veritabanı bugünün kelimeleriyle başarıyla oluşturuldu!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Günlük Contexto veritabanını oluşturur.")
    parser.add_argument("--date", help="Havuzdan kullanılacak gün (YYYY-MM-DD).")
    args = parser.parse_args()
    build_daily_database(args.date)