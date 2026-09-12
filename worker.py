"""Background alerter. Jalanin terpisah atau biarkan thread di main.py."""
import time
from main import run_alert_pass, TG_TOKEN, TG_CHAT, ALERT_INTERVAL, send_telegram


def main():
    if not TG_TOKEN or not TG_CHAT:
        print("Set TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID dulu.")
        return
    send_telegram("Worker alert LIVE.")
    while True:
        try:
            print(time.strftime("%H:%M:%S"), run_alert_pass())
        except Exception as e:
            print("worker error:", e)
        time.sleep(max(60, ALERT_INTERVAL))


if __name__ == "__main__":
    main()
