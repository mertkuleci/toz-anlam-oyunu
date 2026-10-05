import urllib.request
import os

MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/vectors-wiki/wiki.tr.vec"
OUTPUT_PATH = "data/wiki.tr.vec"
TEMP_PATH = f"{OUTPUT_PATH}.download"


def download_model():
    if os.path.exists(OUTPUT_PATH) and os.path.getsize(OUTPUT_PATH) > 0:
        return OUTPUT_PATH

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
    print("Türkçe vektör modeli indiriliyor (~1.1 GB)...")
    try:
        with urllib.request.urlopen(request, timeout=30) as response, open(TEMP_PATH, "wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if os.path.getsize(TEMP_PATH) == 0:
            raise RuntimeError("İndirilen vektör modeli boş.")
        os.replace(TEMP_PATH, OUTPUT_PATH)
    except Exception as error:
        if os.path.exists(TEMP_PATH):
            os.remove(TEMP_PATH)
        raise RuntimeError(f"Türkçe vektör modeli indirilemedi: {error}") from error

    print("Vektör modeli indirildi.")
    return OUTPUT_PATH


if __name__ == "__main__":
    download_model()