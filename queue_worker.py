"""Uzman bekleme kuyruğu worker'ı — Faz 5B.

TASARIM KARARI (spec §23): daemon / Celery / Redis EKLENMEDİ. Proje düz bir
Python + Flask + SQLite kurulumu; bağımlılık büyütmemek için basit bir CLI
komutu tercih edildi:

    python queue_worker.py            # bir kez çalışır ve çıkar (cron/Görev Zamanlayıcı)
    python queue_worker.py --loop     # süreç içinde periyodik çalışır

Neden bu seçildi:
  - Crash olursa talep KAYBOLMAZ; kuyruk DB'de (status = eskalasyon_bekliyor).
  - İki worker aynı anda çalışsa bile koşullu UPDATE
    (assign_primary_if_unassigned) yalnızca birine atama hakkı verir ->
    çift atama ve çift bildirim OLMAZ.
  - Bot süreci içine asyncio task gömmek, bot yeniden başlatıldığında kuyruğun
    da durmasına yol açardı; ayrı komut daha yalın ve gözlemlenebilir.

ACCEPTANCE REVISION — DAVRANIŞ DEĞİŞİKLİĞİ
------------------------------------------
Worker artık kullanıcıya aday teknisyen butonları GÖNDERMEZ. Aday bulunduğunda
merkezi seçim motoru (routing.select_best_technician_for_skill) kimin
seçileceğini belirler, queue_service atomik PRIMARY ataması yapar ve worker
yalnızca SONUCU bildirir.

Worker'ın YAPMADIKLARI:
  - talep çözmez, PRIMARY devretmez, ATANMIŞ talebe dokunmaz
  - AI yanıtı üretmez, available değerlerini değiştirmez
  - kendi fairness/sıralama algoritmasını YAZMAZ (tek merkez: routing.py)
  - kullanıcıya teknisyen SEÇTİRMEZ
"""

import argparse
import logging
import sys
import time

import requests

import config
import db
import queue_service

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("queue_worker")

TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"


def send_telegram_message(chat_id: int, text: str, reply_markup: dict = None):
    """(ok, hata) döner. Anahtar/token loglanmaz."""
    try:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = requests.post(TELEGRAM_SEND_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True, None
    except Exception as e:
        return False, type(e).__name__


def run_once(sender=send_telegram_message):
    """Bir tur kuyruk değerlendirmesi. İstatistik sözlüğü döner.

    sender enjekte edilebilir — testler gerçek Telegram'a çıkmaz.

    Dönen alanlar:
        checked    : değerlendirilen bekleyen talep sayısı
        candidates : en az bir aday bulunan talep sayısı
        assigned   : otomatik PRIMARY ataması yapılan talep sayısı
        unchanged  : değişiklik olmayan (aday yok / zaten atanmış) talep
        errors     : bildirimi gönderilemeyen talep sayısı

    ÖNEMLİ: bildirim gönderilemezse ATAMA GERİ ALINMAZ. Atama gerçek bir iş
    dağıtımıdır; Telegram'ın anlık hatası yüzünden teknisyenin işi elinden
    alınmaz. Talep panelde atanmış görünür, hata loglanır ve talebe sistem
    mesajı düşülür. Kullanıcı mesaj içeriği, token veya secret LOGLANMAZ.

    TEK bir gönderim hatası turu fatal YAPMAZ."""
    stats = {"checked": 0, "candidates": 0, "assigned": 0, "unchanged": 0, "errors": 0}

    for decision in queue_service.reevaluate_waiting_tickets():
        stats["checked"] += 1
        result = decision.get("result")
        if result and result.get("candidates"):
            stats["candidates"] += 1

        if not decision["assigned"]:
            # aday_yok / zaten_atanmis (yarisi baskasi kazandi) -> NO-OP.
            stats["unchanged"] += 1
            continue

        stats["assigned"] += 1
        ticket = db.get_ticket(decision["ticket_id"])
        technician = decision["technician"]
        if not ticket or not technician:
            continue

        logger.info("Talep #%s otomatik olarak atandi.", ticket["id"])

        # Kullaniciya SONUC bildirimi — secim butonu YOK, reply_markup YOK.
        ok, error = sender(
            ticket["user_id"],
            queue_service.assignment_text(ticket, result, technician),
        )
        if not ok:
            stats["errors"] += 1
            db.add_message(
                ticket["id"], "sistem", 0, "Sistem",
                "Atama yapıldı ancak kullanıcı bildirimi Telegram üzerinden iletilemedi.",
                message_kind=config.MESSAGE_KIND_SYSTEM,
            )
            logger.warning("Talep #%s atama bildirimi gonderilemedi (%s); atama gecerli.",
                           ticket["id"], error)

        # Teknisyene kisa bildirim (Telegram ID'si varsa). Hata atamayi bozmaz.
        if technician.get("telegram_id"):
            sent, tech_error = sender(
                technician["telegram_id"],
                queue_service.technician_notification_text(ticket, result),
            )
            if not sent:
                logger.warning("Talep #%s teknisyen bildirimi gonderilemedi (%s).",
                               ticket["id"], tech_error)
    return stats


def format_stats(stats: dict, one_line: bool = False) -> str:
    """Tur özeti. Secret / mesaj içeriği İÇERMEZ."""
    if one_line:
        return ("kuyruk turu: kontrol={checked} aday={candidates} atama={assigned} "
                "degisiklik_yok={unchanged} hata={errors}").format(**stats)
    return "\n".join([
        f"  Kontrol edilen bekleyen talep : {stats['checked']}",
        f"  Aday bulunan                  : {stats['candidates']}",
        f"  Otomatik atanan               : {stats['assigned']}",
        f"  Değişiklik olmayan            : {stats['unchanged']}",
        f"  Bildirim hatası               : {stats['errors']}",
    ])


# Çıkış kodları
EXIT_OK = 0
EXIT_FATAL = 2          # başlangıç / config / DB hatası


def main():
    """Çıkış kodu sözleşmesi:
        0 -> tur başarıyla değerlendirildi (tek tek gönderim hataları dahil)
        2 -> FATAL: şema/DB/config hazırlanamadı, hiçbir değerlendirme yapılmadı
    Tek bir Telegram gönderiminin başarısız olması worker'ı fatal YAPMAZ."""
    parser = argparse.ArgumentParser(description="Uzman bekleme kuyrugu worker'i")
    parser.add_argument("--loop", action="store_true",
                        help="Surekli calis (varsayilan: bir kez calis ve cik)")
    parser.add_argument("--interval", type=int, default=config.QUEUE_WORKER_INTERVAL_SECONDS,
                        help="--loop ile tur araligi (saniye)")
    args = parser.parse_args()

    # FATAL kategorisi: şema/DB hazırlanamıyorsa değerlendirme hiç başlamaz.
    try:
        db.init_db()
    except Exception as e:
        logger.error("FATAL: veritabani hazirlanamadi (%s).", type(e).__name__)
        return EXIT_FATAL

    if not args.loop:
        try:
            stats = run_once()
        except Exception as e:
            logger.error("FATAL: kuyruk turu calistirilamadi (%s).", type(e).__name__)
            return EXIT_FATAL
        logger.info("Kuyruk turu tamamlandi:\n%s", format_stats(stats))
        return EXIT_OK

    logger.info("Kuyruk worker'i baslatildi (her %s saniyede bir).", args.interval)
    while True:
        try:
            stats = run_once()
            # --loop modunda tek satirlik, gurultusuz ozet.
            logger.info("%s", format_stats(stats, one_line=True))
        except Exception as e:
            # Bir tur patlasa bile worker durmaz; talepler DB'de kalir.
            logger.error("Kuyruk turu basarisiz: %s", type(e).__name__)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
