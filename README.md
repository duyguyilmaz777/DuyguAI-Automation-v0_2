# DuyguAI Automation v0.2

macOS masaüstü uygulaması: DuyguAI Studio müşteri içerik üretimini Gemini web Gem’i ve Chrome CDP üzerinden yürütür.

## Kurulum

Python 3.13 sanal ortamı projede `.venv313` olarak duruyor. Silmeyin.

```bash
cd /Users/duygu_mac/Projects/DuyguAI-Automation-v0_2
./start.command
```

veya:

```bash
. "$PWD/macos_python_env.sh"
.venv313/bin/python app.py
```

Homebrew Python 3.13, `pyexpat` için Homebrew `libexpat` ister; sanal ortamın `python` sarmalayıcısı bunu ayarlar. `pip`/`pytest` için de `.venv313/bin/python -m ...` kullanın.

Bağımlılık: `playwright==1.64.0`, geliştirme için `pytest` (`requirements.txt`).

Chrome, `~/Chrome-Automation-Profile` profili ve 9222 CDP portu ile açılır. Mevcut Google oturumunu sıfırlamayın.

## Güvenli test (önerilen)

1. Ayarlar’da Gem URL, Chrome CDP ve Drive klasör ID’sinin durduğunu kontrol edin.
2. Ana panelde **Güvenli test (dry_run)** işaretli kalsın (`config.json` içinde varsayılan `true`).
3. Brief, senaryo veya ek notlardan en az birini yazın. Referans görsel isteğe bağlıdır.
4. **Üretimi Başlat**.
5. Günlükte üretim isteğinin gönderilmediğini ve Drive yüklemesinin yapılmadığını görün.

Gerçek üretim için `dry_run` kapatılır; uygulama maliyet uyarısı ve onay sorar. Onaylanmazsa işlem başlamaz.

## Testler

Ücretli dış servis çağrısı yok. Testleri tek tıkla veya terminalden koşturabilirsiniz:

```bash
./test.command
```

veya:

```bash
. "$PWD/macos_python_env.sh"
.venv313/bin/pytest -v
```

veya standart unittest:

```bash
.venv313/bin/python -m unittest discover -s tests -v
```

## Bilinen sınırlamalar

- Tek Gemini web isteği gönderilir. Gem’in sahne sahne görsel, ayrı videolar, seslendirme, müzik ve kurguyu gerçekten ürettiği varsayılmaz.
- Yeni video, sayfadaki `video` kaynağının üretimden önce kaydedilen listede olmamasıyla doğrulanır. Eski çıktılar kabul edilmez.
- Drive teslimi, hedef klasörde dosya adı görünmeden başarılı sayılmaz.
- Giriş, CAPTCHA ve güvenlik onayları otomatik aşılmaz.
- İkizler Masal Durağı ve çok kiracılı kurulum bu sürümde yok; yalnızca DuyguAI Studio iş akışı vardır.
