"""data/wiki.tr.vec'in en sık kelimelerinden data/clean_roots.txt aday listesini üretir.

generate_daily_db.py bu listeyi önce modelin ilk 60000 kelimesiyle kesiştirir, sonra zeyrek ile
yalın kök/isim/sıfat/zarf filtresinden geçirir; yani burada kaba bir ön eleme yeterlidir.

Kullanım:
    python download_model.py      # model yoksa (~1.1 GB)
    python make_clean_roots.py
Üretilen data/clean_roots.txt dosyasını Git'e ekleyin (.gitignore'da yok).
"""
import unicodedata
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
MODEL_PATH = DATA_DIR / "wiki.tr.vec"
OUTPUT_PATH = DATA_DIR / "clean_roots.txt"
TOP_N = 60000
MIN_LEN, MAX_LEN = 3, 14


def tr_lower(text):
    text = text.replace("İ", "i").replace("I", "ı")
    return unicodedata.normalize("NFC", text.strip().lower())


def main():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"{MODEL_PATH} yok. Önce download_model.py çalıştırın.")

    words = set()
    with open(MODEL_PATH, "r", encoding="utf-8", errors="ignore") as file:
        next(file, None)
        for index, line in enumerate(file):
            if index >= TOP_N:
                break
            token = line.split(" ", 1)[0]
            # Büyük harfle başlayan/büyük harf içeren jetonlar çoğunlukla özel isim veya kısaltma
            if token != tr_lower(token) and token != token.lower():
                continue
            if token.isupper() or any(ch.isupper() for ch in token):
                continue
            word = tr_lower(token)
            if word.isalpha() and MIN_LEN <= len(word) <= MAX_LEN:
                words.add(word)

    OUTPUT_PATH.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    print(f"{len(words)} aday kök yazıldı: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()