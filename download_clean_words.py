import urllib.request
import os
import sys

# Windows terminalinde Türkçe karakterlerin düzgün basılmasını sağla
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Çalışan açık kaynak Türkçe sözlük raw adresleri (Yedekli)
URLS = [
    "https://raw.githubusercontent.com/mertemin/turkish-word-list/master/words.txt",
    "https://raw.githubusercontent.com/CanNuhlar/Turkce-Kelime-Listesi/master/turkce_kelime_listesi.txt"
]

OUTPUT_PATH = "data/clean_roots.txt"
os.makedirs("data", exist_ok=True)

print("Temiz Türkçe kök kelime listesi indiriliyor...")

words = []
for url in URLS:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            content = response.read().decode('utf-8')
            downloaded = [line.strip().lower() for line in content.splitlines() if line.strip()]
            if len(downloaded) > 1000:
                words = downloaded
                print(f"Kaynak başarıyla çekildi!")
                break
    except Exception as e:
        print(f"Alternatif kaynak deneniyor... ({e})")

# Sadece alfabetik, en az 4 harfli ve çekim ekleri barındırmayan kelimeleri süz
filtered_words = [
    w for w in words 
    if len(w) >= 4 and w.isalpha() and not w.endswith(("da", "de", "dan", "den", "lar", "ler", "nın", "nin", "ım", "im"))
]

if filtered_words:
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(set(filtered_words))))
    print(f"İşlem başarılı! {len(filtered_words)} adet temiz Türkçe kelime '{OUTPUT_PATH}' dosyasına kaydedildi.")
else:
    print("Kelime listesi indirilemedi. İnternet bağlantınızı kontrol edin.")