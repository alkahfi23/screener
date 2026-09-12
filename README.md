# Hybrid Early Gem Scanner

FastAPI + DexScreener. Alert Telegram hanya untuk filter hijau + score/confidence tinggi.

## Lokal

```
python -m pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=isi
set TELEGRAM_CHAT_ID=isi
python main.py
```

UI: http://127.0.0.1:8000/ui
Test TG: http://127.0.0.1:8000/alerts/test

## Telegram

1. Buka @BotFather, buat bot, copy token.
2. Chat bot itu dulu.
3. Buka `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Ambil `chat.id` (angka, bisa negatif kalau grup).
5. Set env `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`.

## GitHub

Di folder project:

```
git init
git add main.py dashboard.html requirements.txt render.yaml README.md .gitignore .env.example
git commit -m "gem scanner + telegram alerts"
git branch -M main
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

Jangan commit token.

## Render

1. New Web Service, connect repo.
2. Build: `pip install -r requirements.txt`
3. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Environment:
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_CHAT_ID
   - ENABLE_TELEGRAM=1
   - ALERT_MIN_SCORE=70
   - ALERT_MIN_CONF=70
   - ALERT_INTERVAL_SEC=180
5. Deploy. Buka `https://SERVICE.onrender.com/alerts/test`

Free Render tidur kalau idle. Alert jalan selama instance hidup. Pakai cron hit `/alerts/run` tiap 5 menit biar lebih rajin.
