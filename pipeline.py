import json
import os
import hashlib
import re
import unicodedata
import urllib.parse
import requests
from datetime import datetime
import numpy as np
from gensim.models import KeyedVectors

MODEL_PATH = "data/wiki.tr.vec"
CLEAN_ROOTS_PATH = "data/clean_roots.txt"
OUTPUT_JSON = "data/daily_game.json"

def tr_lower(text):
    text = text.replace("İ", "i").replace("I", "ı")
    return unicodedata.normalize("NFC", text.strip().lower())

def load_clean_words_set():
    if not os.path.exists(CLEAN_ROOTS_PATH):
        raise FileNotFoundError(f"'{CLEAN_ROOTS_PATH}' bulunamadı! Önce 'download_clean_words.py' çalıştırın.")
    with open(CLEAN_ROOTS_PATH, "r", encoding="utf-8") as f:
        return set(tr_lower(line) for line in f if line.strip())

def get_tdk_definition(target_word):
    try:
        encoded_word = urllib.parse.quote(target_word)
        url = f"https://sozluk.gov.tr/gts?ara={encoded_word}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5).json()
        
        if isinstance(res, list) and len(res) > 0 and "anlamlarListe" in res[0]:
            meaning = res[0]["anlamlarListe"][0]["anlam"]
            pattern = re.compile(re.escape(target_word), re.IGNORECASE)
            masked_meaning = pattern.sub("______", meaning)
            return masked_meaning.capitalize()
    except Exception:
        pass
    return "Tanım bulunamadı veya TDK servisine erişilemedi."

def get_daily_target_words(candidate_words, date_str, count=3):
    selected_words = []
    for i in range(count):
        seed_str = f"{date_str}_slot_{i}"
        hash_val = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest(), 16)
        word_index = hash_val % len(candidate_words)
        selected_words.append(candidate_words[word_index])
    return selected_words

def run_daily_pipeline(top_n_vocab=80000):
    today_str = datetime.now().strftime("%Y-%m-%d")
    test_date_seed = f"{today_str}_v5_final"
    print(f"[{today_str}] Boru hattı çalıştırılıyor...")
    
    clean_words_set = load_clean_words_set()
    
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"'{MODEL_PATH}' bulunamadı!")
        
    print("Vektör modeli yükleniyor ve TDK sözlüğü ile filtreleniyor...")
    model = KeyedVectors.load_word2vec_format(MODEL_PATH, limit=top_n_vocab)
    
    filtered_words = []
    filtered_indices = []
    seen = set()
    
    for idx, raw_w in enumerate(model.index_to_key):
        w = tr_lower(raw_w)
        if w in clean_words_set and w not in seen and w.isalpha() and len(w) >= 3:
            seen.add(w)
            filtered_words.append(w)
            filtered_indices.append(idx)
            
    print(f"Filtreleme sonrası aktif temiz kelime sayısı: {len(filtered_words)}")
    
    common_target_candidates = filtered_words[:6000]
    target_words = get_daily_target_words(common_target_candidates, test_date_seed, count=3)
    print(f"🎯 GÜNÜN KELİMELERİ: {target_words}")
    
    filtered_vecs = model.vectors[filtered_indices]
    norm_vecs = filtered_vecs / np.linalg.norm(filtered_vecs, axis=1, keepdims=True)
    
    games_data = {}
    
    for idx, target_word in enumerate(target_words, start=1):
        target_idx = filtered_words.index(target_word)
        target_vec = norm_vecs[target_idx]
        
        similarities = np.dot(norm_vecs, target_vec)
        word_sim_pairs = sorted(zip(filtered_words, similarities), key=lambda x: x[1], reverse=True)
        
        rank_data = {}
        for rank, (word, sim) in enumerate(word_sim_pairs, start=1):
            rank_data[word] = {
                "rank": rank,
                "similarity": round(float(sim) * 100, 2)
            }
        
        # 50., 100. ve 150. kelimeler (0-indexed: 49, 99, 149)
        word_50 = word_sim_pairs[min(49, len(word_sim_pairs) - 1)][0]
        word_100 = word_sim_pairs[min(99, len(word_sim_pairs) - 1)][0]
        word_150 = word_sim_pairs[min(149, len(word_sim_pairs) - 1)][0]
        
        tdk_def = get_tdk_definition(target_word)
        
        games_data[f"kelime_{idx}"] = {
            "target_word": target_word,
            "target_length": len(target_word),
            "first_letter": target_word[0].upper(),
            "tdk_definition": tdk_def,
            "word_50": word_50,
            "word_100": word_100,
            "word_150": word_150,
            "rank_data": rank_data
        }

    output_payload = {
        "date": today_str,
        "total_words_in_vocab": len(filtered_words),
        "games": games_data
    }
    
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)
        
    print(f"Pipeline tamamlandı! Çıktı: '{OUTPUT_JSON}'")

if __name__ == "__main__":
    run_daily_pipeline()