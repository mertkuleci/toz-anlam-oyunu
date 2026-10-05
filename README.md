# Contexto Turkce

## Calistirma

Windows'ta proje sanal ortami ile:

```powershell
venv\Scripts\python.exe -m streamlit run app.py
```

Gunun oyunu `data/year_game_data.json` takviminden secilir. Tarih Turkiye saatiyle hesaplanir; gun degistiginde uygulama, yeni hedeflerin SQLite siralamalarini ilk istekte otomatik olusturur.

Model dosyasi Git'e eklenmez. Canli ortamda dosya yoksa ilk acilista otomatik olarak yaklasik 1.1 GB'lik `wiki.tr.vec` indirilir; ardindan gunluk SQLite veritabani olusturulur. Ilk acilis baglanti hizina ve sunucu kaynaklarina gore uzun surebilir. Modelin ve DB'nin canli ortamda kalici saklanacagi garanti degildir; uygulama yeniden baslatildiginda model/DB tekrar indirilebilir veya uretilebilir.

## E-posta dogrulamasi

Kayit kodu gonderebilmek icin SMTP ayarlari gerekir. Degerleri ortama gizli olarak tanimla; kaynak koduna veya repoya ekleme:

```text
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM_EMAIL=...
```

Streamlit Secrets kullanirken ayni anahtarlari uygulama secrets ayarlarina ekle. 465 portu SSL, diger portlar STARTTLS kullanir. Dogrulama kodu 10 dakika gecerli kalir.

Gmail kullaniyorsan Google hesabinda iki adimli dogrulamayi acip bir Uygulama Sifresi olustur; normal hesap sifreni kullanma. Projede `.streamlit/secrets.toml` dosyasi olustur ve su alanlari kendi adresinle doldur:

```toml
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = "587"
SMTP_USER = "seninadresin@gmail.com"
SMTP_PASSWORD = "google-uygulama-sifren"
SMTP_FROM_EMAIL = "seninadresin@gmail.com"
```

Uygulama Sifresini koda veya sohbete yazma. Canli ortamda ayni TOML degerlerini Streamlit Cloud uygulamasinin **Settings > Secrets** alanina gir. `.streamlit/secrets.toml` Git disinda tutulur.

## Oyun hakki ve hesaplar

Kayitli hesap gun basina iki moddan yalnizca birini oynayabilir. Bu hak SQLite'ta tutulur ve oturum degisse de korunur. Anonim oyuncunun hakki tarayici oturumu boyunca uygulanir; anonim skorlar saklanmaz. Eski SHA-256 parola kayitlari basarili giriste PBKDF2'ye yukseltilir.

Yeni sifreler 1-8 karakter uzunlugunda olmali ve yalnizca kucuk harflerden olusmalidir.

## Yayin oncesi

`data/users.json` ve `data/game_data.db` yerel dosyalardir. Canli ortamda hesaplarin ve gunluk haklarin yeniden baslatmada kaybolmamasi icin kalici disk veya yonetilen veritabani kullan. Birden fazla uygulama kopyasi calistirilacaksa SQLite yerine paylasilan bir veritabani tercih et.
