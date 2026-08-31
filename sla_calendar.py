"""SLA çalışma takvimi — Faz 6B.

Bütün iş-zamanı (business time) aritmetiğinin TEK DOĞRULUK KAYNAĞI.
db.py / webapp.py / bot.py bu algoritmayı tekrar yazmaz; hepsi buradaki
helper'ları çağırır.

Kapsam:
  - timezone-aware hesap (zoneinfo; sabit UTC offset YOK, DST desteklenir)
  - haftalık çalışma saatleri (config.BUSINESS_HOURS)
  - hafta sonu ve tatil hariç tutma (aynı kod yolunda birleşik)
  - iş dakikası ekleme / iki an arasındaki iş dakikasını sayma

Zaman gösterimi sözleşmesi
--------------------------
DB'de zaman damgaları "%Y-%m-%d %H:%M:%S" biçiminde NAIVE metin olarak durur
(Faz 1'den beri). Bu değerler SLA_TIMEZONE yerel saati kabul edilir:
okurken localize edilir, yazarken tekrar naive metne çevrilir. Böylece mevcut
şema ve tüm eski kayıtlar bozulmadan timezone-aware hesap yapılabilir.
"""

import logging
import threading
import time as _time
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_DAY_SCAN = 800          # sonsuz döngü koruması (tatil/kapalı gün zinciri)

# ---------------- Tatil cache'i (süreçler arası tutarlı) ----------------
#
# web / bot / queue_worker AYRI süreçlerdir. Her biri kendi tatil cache'ini
# tutar, ama DB'deki merkezi `sla_calendar_state.revision` sayacı sayesinde
# başka bir süreçteki değişikliği fark eder:
#
#   1. Cache TTL içindeyse hiç DB'ye gidilmez (SLA rozetinde N+1 YOK).
#   2. TTL dolunca YALNIZCA revizyon okunur (tek küçük sorgu).
#   3. Revizyon değiştiyse tatil listesi yeniden yüklenir.
#
# Redis/Celery gibi dağıtık cache bağımlılığı EKLENMEZ.
REVISION_CHECK_TTL_SECONDS = float(
    getattr(config, "CALENDAR_REVISION_TTL_SECONDS", 5.0))

_cache_lock = threading.RLock()
_cache = {
    "revision": None,      # cache'in ait olduğu revizyon
    "holidays": None,      # None = hiç yüklenmedi
    "checked_at": 0.0,     # son revizyon kontrolü (monotonic)
}


class CalendarUnavailableError(RuntimeError):
    """Takvim verisi hiç yüklenemedi.

    Güvenli hata davranışı: elde HİÇ geçerli cache yokken DB okunamıyorsa
    'tatil yok' varsayıp SESSİZCE YANLIŞ SLA hesaplamak yerine açıkça hata
    veriyoruz. Bir kez yüklendikten sonra geçici DB hataları son güvenilir
    cache ile tolere edilir."""


# ---------------- Timezone ----------------

def get_timezone() -> ZoneInfo:
    """Yapılandırılmış saat dilimi. Geçersizse UTC'ye düşer (asla çökmez)."""
    try:
        return ZoneInfo(config.SLA_TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(get_timezone())


def to_aware(value) -> datetime:
    """Naive metin / naive datetime -> SLA saat diliminde aware datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, TS_FORMAT)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=get_timezone())
    return value.astimezone(get_timezone())


def to_naive_str(dt: datetime) -> str:
    """Aware datetime -> DB'de saklanan naive yerel metin."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(get_timezone())
    return dt.strftime(TS_FORMAT)


# ---------------- Tatiller (cache'li) ----------------

def invalidate_cache():
    """Yerel cache'i düşürür.

    Tatili DEĞİŞTİREN süreç bunu çağırır ve anında güncel veriyi görür.
    Diğer süreçler değişikliği revizyon kontrolünde fark eder."""
    with _cache_lock:
        _cache["revision"] = None
        _cache["holidays"] = None
        _cache["checked_at"] = 0.0


def cache_state() -> dict:
    """Teşhis/test için cache durumunun kopyası."""
    with _cache_lock:
        return {"revision": _cache["revision"],
                "loaded": _cache["holidays"] is not None,
                "size": len(_cache["holidays"] or ())}


def _reload(reason: str):
    """Tatil listesini + revizyonu TEK okumada yeniler."""
    import db
    revision, holidays = db.load_calendar_snapshot()
    _cache["revision"] = revision
    _cache["holidays"] = holidays
    _cache["checked_at"] = _time.monotonic()
    logger.debug("SLA takvimi yeniden yuklendi (%s): revizyon=%s", reason, revision)
    return holidays


def _holidays() -> set:
    """Aktif tatil günleri (date kümesi).

    db import'u fonksiyon içinde yapılır: sla_calendar <-> db döngüsel
    import'unu önler."""
    with _cache_lock:
        cached = _cache["holidays"]
        fresh = (cached is not None and
                 (_time.monotonic() - _cache["checked_at"]) < REVISION_CHECK_TTL_SECONDS)
        if fresh:
            return cached          # DB'ye HİÇ gidilmez

        try:
            if cached is None:
                return _reload("ilk yukleme")

            # Yalnızca revizyonu oku (tek küçük sorgu).
            import db
            revision = db.get_calendar_revision()
            if revision == _cache["revision"]:
                _cache["checked_at"] = _time.monotonic()
                return cached      # değişmemiş -> tatil listesi TEKRAR ÇEKİLMEZ
            return _reload(f"revizyon {_cache['revision']} -> {revision}")

        except Exception as e:
            # Bir kez yüklenmişse: son güvenilir cache ile devam et.
            # Hata TİPİ loglanır; veri içeriği veya sır loglanmaz.
            if cached is not None:
                _cache["checked_at"] = _time.monotonic()   # DB'yi dövmeyelim
                logger.warning("SLA takvimi revizyonu okunamadi (%s); "
                               "son bilinen tatil listesi kullaniliyor.",
                               type(e).__name__)
                return cached
            # Hiç cache yok + DB okunamıyor -> SESSİZCE yanlış hesaplama YAPMA.
            if _is_missing_table(e):
                # İlk migration öncesi: tablo henüz yok, tatilsiz devam edilir.
                return set()
            raise CalendarUnavailableError(
                f"SLA takvimi yuklenemedi ({type(e).__name__})") from e


def _is_missing_table(exc) -> bool:
    """Şema henüz oluşturulmadı mı? (init_db öncesi çağrı)"""
    return "no such table" in str(exc).lower()


def is_holiday(day: date) -> bool:
    return day in _holidays()


# ---------------- Günlük çalışma penceresi ----------------

def _parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def business_window(day: date):
    """O gün için (acilis, kapanis) aware datetime çifti; kapalıysa None.

    Hafta sonu VE tatil kontrolü BURADA birleşir — çağıranlar ayrı kod yolu
    yazmaz."""
    if is_holiday(day):
        return None
    hours = config.BUSINESS_HOURS.get(day.weekday())
    if not hours:
        return None
    tz = get_timezone()
    start_t, end_t = _parse_hhmm(hours[0]), _parse_hhmm(hours[1])
    if start_t >= end_t:
        return None
    # datetime.combine + tzinfo: DST geçişlerinde doğru yerel offset uygulanır.
    return (datetime.combine(day, start_t, tzinfo=tz),
            datetime.combine(day, end_t, tzinfo=tz))


def is_business_time(dt: datetime) -> bool:
    """Verilen an çalışma saatleri içinde mi?"""
    dt = to_aware(dt)
    if dt is None:
        return False
    window = business_window(dt.date())
    if not window:
        return False
    return window[0] <= dt < window[1]


def next_business_time(dt: datetime) -> datetime:
    """Verilen andan itibaren İLK çalışma anı.

    Zaten çalışma saatindeyse aynı an döner; değilse bir sonraki açılış."""
    dt = to_aware(dt)
    if dt is None:
        return None
    day = dt.date()
    for _ in range(_MAX_DAY_SCAN):
        window = business_window(day)
        if window:
            if dt < window[0]:
                return window[0]
            if dt < window[1]:
                return dt
        day += timedelta(days=1)
        dt = datetime.combine(day, time(0, 0), tzinfo=get_timezone())
    return dt


# ---------------- İş zamanı aritmetiği ----------------

def add_business_minutes(start: datetime, minutes: float) -> datetime:
    """Başlangıçtan itibaren `minutes` İŞ DAKİKASI sonrasını döner.

    Hafta sonu, tatil ve mesai dışı saatler sayılmaz.
    Örn. Pzt-Cuma 08:00-18:00 iken Cuma 17:30 + 120 iş dk = Pazartesi 09:30."""
    cursor = next_business_time(start)
    if cursor is None:
        return None
    remaining = timedelta(minutes=max(0, minutes))
    if remaining.total_seconds() == 0:
        return cursor

    for _ in range(_MAX_DAY_SCAN):
        window = business_window(cursor.date())
        if window:
            segment_end = window[1]
            available = segment_end - cursor
            if available > timedelta(0):
                if remaining <= available:
                    return cursor + remaining
                remaining -= available
        # Sonraki günün açılışına atla.
        cursor = next_business_time(
            datetime.combine(cursor.date() + timedelta(days=1), time(0, 0),
                             tzinfo=get_timezone()))
        if cursor is None:
            return None
    return cursor


def business_seconds_between(start: datetime, end: datetime) -> int:
    """[start, end] aralığının çalışma saatleriyle KESİŞİMİ (saniye).

    start > end ise NEGATİF döner — 'ne kadar gecikti' hesabı için."""
    start, end = to_aware(start), to_aware(end)
    if start is None or end is None:
        return 0
    if start == end:
        return 0
    sign = 1
    if start > end:
        start, end, sign = end, start, -1

    total = 0
    day = start.date()
    for _ in range(_MAX_DAY_SCAN):
        if day > end.date():
            break
        window = business_window(day)
        if window:
            overlap_start = max(window[0], start)
            overlap_end = min(window[1], end)
            if overlap_end > overlap_start:
                total += int((overlap_end - overlap_start).total_seconds())
        day += timedelta(days=1)
    return sign * total


def business_minutes_between(start: datetime, end: datetime) -> int:
    """İki an arasındaki iş dakikası (işaretli)."""
    return int(business_seconds_between(start, end) // 60)


# ---------------- Bilgi amaçlı durum ----------------

CALENDAR_OPEN = "open"
CALENDAR_CLOSED = "closed"
CALENDAR_HOLIDAY = "holiday"


def calendar_status(dt: datetime = None) -> dict:
    """Panelde gösterilecek küçük bilgi: mesai içinde / dışında / tatil."""
    dt = to_aware(dt) if dt else now_local()
    if is_holiday(dt.date()):
        return {"state": CALENDAR_HOLIDAY,
                "label": "Tatil — sayaç duruyor",
                "next_open": to_naive_str(next_business_time(dt))}
    if is_business_time(dt):
        window = business_window(dt.date())
        return {"state": CALENDAR_OPEN,
                "label": "Mesai içinde",
                "closes_at": to_naive_str(window[1])}
    return {"state": CALENDAR_CLOSED,
            "label": "Mesai dışında — sayaç bir sonraki çalışma saatinde devam edecek",
            "next_open": to_naive_str(next_business_time(dt))}
