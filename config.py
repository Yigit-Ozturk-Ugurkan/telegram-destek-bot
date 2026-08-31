import os
import secrets


# ---- .env yükleyici ----
# Faz 2.5: sırlar (bot token, API anahtarı, secret key, admin parolası) kaynak
# kodundan çıkarıldı. Ek bağımlılık getirmemek için küçük bir okuyucu kullanıyoruz.
def _load_dotenv(path=".env"):
    base = os.path.dirname(os.path.abspath(__file__))
    full = path if os.path.isabs(path) else os.path.join(base, path)
    if not os.path.exists(full):
        return
    with open(full, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Gerçek ortam değişkeni her zaman .env'i ezer.
            os.environ.setdefault(key, value)


_load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---- Telegram Bot Ayarları ----
# Token .env dosyasından ya da ortam değişkeninden gelir; kaynak kodda tutulmaz.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Yönetici Telegram kullanıcı ID'leri (birden fazla olabilir)
# ID öğrenmek için Telegram'da @userinfobot botuna mesaj atabilirsiniz
ADMIN_IDS = [
    6119546336,
]

# Yeni taleplerin bildirileceği grup/kanal chat ID'si (opsiyonel).
# Boş bırakılırsa bildirimler ADMIN_IDS listesindeki herkese tek tek gönderilir.
ADMIN_GROUP_ID = os.environ.get("ADMIN_GROUP_ID", "")
if ADMIN_GROUP_ID:
    ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)

# ---- Veritabanı ----
DB_PATH = os.environ.get("DB_PATH", "destek.db")

# ---- Web Paneli Ayarları ----
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", 5000))

# WEB_SECRET_KEY tanımsızsa süreç ömrü boyunca geçerli rastgele bir anahtar
# üretilir. Bu durumda sunucu yeniden başlayınca oturumlar düşer — üretimde
# .env üzerinden sabit bir anahtar verin.
WEB_SECRET_KEY = os.environ.get("WEB_SECRET_KEY") or secrets.token_urlsafe(48)
WEB_SECRET_KEY_IS_EPHEMERAL = not os.environ.get("WEB_SECRET_KEY")

# İlk admin hesabını oluşturmak için YALNIZCA bootstrap sırasında kullanılır.
# Hesap oluştuktan sonra .env'den kaldırılabilir. Kaynak kodda varsayılan YOKTUR.
WEB_ADMIN_USERNAME = os.environ.get("WEB_ADMIN_USERNAME", "admin")
WEB_ADMIN_PASSWORD = os.environ.get("WEB_ADMIN_PASSWORD")  # None ise bootstrap atlanır

# Teknisyen hesapları için ORTAK PAROLA YOKTUR (Faz 2.5). Her hesap, admin
# tarafından tek tek belirlenen geçici parolayla açılır ve ilk girişte
# değiştirilmesi zorunlu tutulur.

# Parola kuralları
PASSWORD_MIN_LENGTH = int(os.environ.get("PASSWORD_MIN_LENGTH", 10))

# ---- Oturum çerezi güvenliği ----
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# HTTPS arkasında 1 yapın. Development'ta HTTP localhost bozulmasın diye
# varsayılan 0'dır.
SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", False)

# ---- Gemini AI Ayarları ----
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# AI'ın talepleri sınıflandıracağı departmanlar. Teknik ekip üyeleri bu isimlerden
# birini "departman" olarak kullanmalı ki otomatik yönlendirme eşleşsin.
DEPARTMENTS = ["Ağ", "Donanım", "Yazılım", "Hesap/Erişim", "Diğer"]

# ==================================================================
# ACCEPTANCE UX + ROUTING REVISION — SINIFLANDIRMA SONUÇ MODELİ
# ==================================================================
#
# Sınıflandırmanın sonucu artık DÖRT mantıksal sonuçtan biridir. Bu ayrım
# ROUTING KARARININ TEK DİLİDİR: bot, queue worker, web paneli ve testler
# aynı sabitleri kullanır; kimse kendi string'ini uydurmaz.
#
#   SPECIFIC_SKILL  : geçerli ve AKTİF bir uzmanlık kodu üretildi (en yüksek öncelik)
#   DEPARTMENT      : uzmanlık üretilemedi ama geçerli bir departman var
#   GENERAL_SUPPORT : model AÇIKÇA "genel teknik destek" dedi
#   MANUAL_TRIAGE   : sınıflandırma başarısız / belirsiz / geçersiz
#
# ÇOK ÖNEMLİ: GENERAL_SUPPORT bir CATCH-ALL DEĞİLDİR. Belirsizlik,
# eşleşmeyen kod, JSON hatası veya "aday bulunamadı" durumları GENEL DESTEĞE
# DÖNÜŞMEZ (bkz. routing.resolve_classification ve routing.select_for_ticket).
ROUTING_BASIS_SPECIFIC_SKILL = "SPECIFIC_SKILL"
ROUTING_BASIS_DEPARTMENT = "DEPARTMENT"
ROUTING_BASIS_GENERAL_SUPPORT = "GENERAL_SUPPORT"
ROUTING_BASIS_MANUAL_TRIAGE = "MANUAL_TRIAGE"
ROUTING_BASES = (
    ROUTING_BASIS_SPECIFIC_SKILL,
    ROUTING_BASIS_DEPARTMENT,
    ROUTING_BASIS_GENERAL_SUPPORT,
    ROUTING_BASIS_MANUAL_TRIAGE,
)

# Admin panelinde "neden atanmadı?" sorusunun cevabı bu etiketlerden okunur.
ROUTING_BASIS_LABELS = {
    ROUTING_BASIS_SPECIFIC_SKILL: "Uzman bekliyor",
    ROUTING_BASIS_DEPARTMENT: "Departman bekliyor",
    ROUTING_BASIS_GENERAL_SUPPORT: "Genel destek bekliyor",
    ROUTING_BASIS_MANUAL_TRIAGE: "Manuel sınıflandırma gerekiyor",
}

# Modelin ÜRETMESİ BEKLENEN classification_type değerleri. Server bunları
# doğrular; model SOURCE OF TRUTH DEĞİLDİR.
CLASSIFICATION_SPECIFIC_SKILL = "specific_skill"
CLASSIFICATION_DEPARTMENT = "department"
CLASSIFICATION_GENERAL_SUPPORT = "general_support"
CLASSIFICATION_UNCERTAIN = "uncertain"
CLASSIFICATION_TYPES = (
    CLASSIFICATION_SPECIFIC_SKILL,
    CLASSIFICATION_DEPARTMENT,
    CLASSIFICATION_GENERAL_SUPPORT,
    CLASSIFICATION_UNCERTAIN,
)

# Genel teknik destek uzmanlığı. Katalogdaki NORMAL bir skill'dir: adminler
# bunu teknisyenlere 1/2/3 öncelik sistemi içinde atar ve seçim aynı adalet
# kurallarıyla yapılır. Bir teknisyenin bu skill'e sahip olması onu bütün
# taleplerin fallback'i YAPMAZ — yalnızca GERÇEKTEN general_support olarak
# sınıflandırılmış talepler bu kuyruğa girer.
GENERAL_SUPPORT_SKILL_CODE = "GENERAL_TECH_SUPPORT"
GENERAL_SUPPORT_SKILL_NAME = "Genel Teknik Destek"


# ---- Web paneli rolleri ----
ROLE_ADMIN = "admin"
ROLE_TECHNICIAN = "technician"
ROLES = (ROLE_ADMIN, ROLE_TECHNICIAN)

# ---- Talep Durumları ----
# DB'deki mevcut Türkçe değerler korunur (acik / islemde / kapatildi).
# Mantıksal model  ->  DB değeri
#   open            -> acik
#   assigned        -> atandi
#   in_progress     -> islemde
#   waiting_user    -> beklemede
#   resolved        -> kapatildi   (kullanıcıya "Çözüldü" olarak gösterilir)
#   reopened        -> yeniden_acildi
STATUS_OPEN = "acik"
STATUS_ASSIGNED = "atandi"
STATUS_IN_PROGRESS = "islemde"
STATUS_WAITING_USER = "beklemede"
STATUS_CLOSED = "kapatildi"
STATUS_REOPENED = "yeniden_acildi"
# Faz 3B: teknik ekibe aktarıldı ama uygun aday bulunamadı. Talep kaybolmasın
# diye atanmamış bir bekleme durumunda tutulur; admin manuel atayabilir.
STATUS_ESCALATION_PENDING = "eskalasyon_bekliyor"

STATUS_LABELS = {
    STATUS_OPEN: "Açık",
    STATUS_ASSIGNED: "Atandı",
    STATUS_IN_PROGRESS: "İşlemde",
    STATUS_WAITING_USER: "Kullanıcı Bekleniyor",
    STATUS_CLOSED: "Çözüldü",
    STATUS_REOPENED: "Yeniden Açıldı",
    STATUS_ESCALATION_PENDING: "Uzman Bekleniyor",
}

# Durum grupları — sorgular ve filtreler tek yerden beslensin diye.
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS,
                   STATUS_REOPENED, STATUS_ESCALATION_PENDING)
WAITING_STATUSES = (STATUS_WAITING_USER,)
RESOLVED_STATUSES = (STATUS_CLOSED,)
# "Henüz kapanmamış" = kullanıcının Telegram'dan yazdığı mesajın ekleneceği talepler
UNRESOLVED_STATUSES = ACTIVE_STATUSES + WAITING_STATUSES

# ---- İŞ YÜKÜ (workload) ----
# Bir teknisyenin ÜZERİNDE olan, henüz kapanmamış talepler. Sidebar'daki
# "N aktif talep" sayısı da, Faz 3'teki routing de BU TEK TANIMDAN beslenir;
# aynı kavram için ikinci bir SQL tanımı oluşturulmamalıdır.
#
# 'acik' burada YOKTUR: atanmamış talebin sahibi de yoktur, dolayısıyla kimsenin
# iş yüküne yazılamaz. 'kapatildi' de yoktur — çözülmüş talep iş yükü değildir.
WORKLOAD_STATUSES = (
    STATUS_ASSIGNED,
    STATUS_IN_PROGRESS,
    STATUS_WAITING_USER,
    STATUS_REOPENED,
)

# Geçerli durum geçişleri. En önemli kural: kapatildi -> islemde DOĞRUDAN yapılamaz,
# önce yeniden_acildi durumuna geçilmelidir.
ALLOWED_TRANSITIONS = {
    STATUS_OPEN: {STATUS_ASSIGNED, STATUS_IN_PROGRESS, STATUS_WAITING_USER,
                  STATUS_CLOSED, STATUS_ESCALATION_PENDING},
    STATUS_ASSIGNED: {STATUS_IN_PROGRESS, STATUS_WAITING_USER, STATUS_CLOSED},
    STATUS_IN_PROGRESS: {STATUS_ASSIGNED, STATUS_WAITING_USER, STATUS_CLOSED},
    STATUS_WAITING_USER: {STATUS_IN_PROGRESS, STATUS_CLOSED},
    STATUS_CLOSED: {STATUS_REOPENED},
    STATUS_REOPENED: {STATUS_IN_PROGRESS, STATUS_WAITING_USER, STATUS_CLOSED,
                      STATUS_ESCALATION_PENDING},
    # Uzman bekleyen talep atanabilir ya da doğrudan çözülebilir.
    STATUS_ESCALATION_PENDING: {STATUS_ASSIGNED, STATUS_IN_PROGRESS,
                                STATUS_WAITING_USER, STATUS_CLOSED},
}


def can_transition(current: str, target: str) -> bool:
    """Geçersiz durum geçişlerini tek noktadan engeller."""
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())


# ---- Çözüm kaynağı (audit) ----
# "Bu talebi kim, hangi kanal üzerinden çözdü?" sorusunun cevabı kaybolmasın diye.
RESOLVED_SOURCE_WEB = "web"
RESOLVED_SOURCE_TELEGRAM = "telegram"
RESOLVED_SOURCE_SYSTEM = "system"
# Faz 3B: AI çözümünü kullanıcının kendisi doğruladı — teknisyen çözmüş gibi
# görünmemesi için ayrı bir kanal olarak tutulur.
RESOLVED_SOURCE_AI = "ai"
RESOLVED_SOURCE_LABELS = {
    RESOLVED_SOURCE_WEB: "Web paneli",
    RESOLVED_SOURCE_TELEGRAM: "Telegram",
    RESOLVED_SOURCE_SYSTEM: "Sistem",
    RESOLVED_SOURCE_AI: "AI / kullanıcı doğrulaması",
}


# ---- Faz 5B: Ticket önceliği ----
# Priority, teknik UZMANLIK DEĞİLDİR — aciliyet ölçüsüdür.
# required_skill=COMPUTER_VISION + priority=P1 gibi kombinasyonlar normaldir;
# skill routing ile urgency birbirinden tamamen ayrıdır.
PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"
PRIORITY_P4 = "P4"
PRIORITIES = (PRIORITY_P1, PRIORITY_P2, PRIORITY_P3, PRIORITY_P4)
DEFAULT_PRIORITY = PRIORITY_P3

PRIORITY_LABELS = {
    PRIORITY_P1: "P1 Kritik",
    PRIORITY_P2: "P2 Yüksek",
    PRIORITY_P3: "P3 Normal",
    PRIORITY_P4: "P4 Düşük",
}

# ==================================================================
# PRIORITY ROUTING REVISION — AI DESTEKLİ P1-P4 ÖNERİSİ
# ==================================================================
#
# Yeni talebin önceliği pratikte her zaman DEFAULT_PRIORITY (P3) kalıyordu.
# Artık sınıflandırma turunda Gemini bir ÖNERİ üretir, ama:
#
#     Gemini -> priority recommendation -> SERVER-SIDE VALIDATION -> ticket
#
# Gemini SOURCE OF TRUTH DEĞİLDİR. Doğrulama routing._validated_priority()
# içindedir ve ZORUNLUDUR: geçersiz / null / bozuk JSON / timeout / hata
# durumlarının HEPSİ DEFAULT_PRIORITY'ye (P3) düşer.
#
# Önceliğin KAYNAĞI ayrıca saklanır (tickets.priority_source):
#   default -> hiç dokunulmadı, şema varsayılanı
#   ai      -> doğrulanmış model önerisi uygulandı
#   manual  -> bir insan (admin ya da ana sorumlu) elle belirledi
#
# manual KİLİTLER: AI bir daha üzerine YAZMAZ (bkz. db.apply_ai_priority).
PRIORITY_SOURCE_DEFAULT = "default"
PRIORITY_SOURCE_AI = "ai"
PRIORITY_SOURCE_MANUAL = "manual"
PRIORITY_SOURCES = (PRIORITY_SOURCE_DEFAULT, PRIORITY_SOURCE_AI,
                    PRIORITY_SOURCE_MANUAL)
PRIORITY_SOURCE_LABELS = {
    PRIORITY_SOURCE_DEFAULT: "Varsayılan",
    PRIORITY_SOURCE_AI: "AI önerisi (doğrulanmış)",
    PRIORITY_SOURCE_MANUAL: "Elle belirlendi",
}

# AI priority_reason METNİNİN UZUNLUK SINIRI. Model uzun bir deneme yazsa bile
# panelde tek satırda okunur kalsın ve DB şişmesin.
PRIORITY_REASON_MAX_LENGTH = int(os.environ.get("PRIORITY_REASON_MAX_LENGTH", 300))

# Modelin göreceği ÖNCELİK SEMANTİĞİ — TEK TANIM NOKTASI.
# Prompt metni burada durur ki "P2 nedir?" sorusunun tek bir cevabı olsun.
# ÖNEMLİ: kullanıcının "ACİL", "ÇOK ACİL", "KRİTİK" yazması TEK BAŞINA P1
# sebebi değildir; ETKİ (impact) analizi esastır.
PRIORITY_SEMANTICS = {
    PRIORITY_P1: (
        "KRİTİK — şirket çapında önemli bir servis kesintisi, production "
        "tamamen durmuş, kritik güvenlik olayı, çok sayıda kullanıcı "
        "etkileniyor ya da iş sürekliliği ciddi şekilde durmuş."
    ),
    PRIORITY_P2: (
        "YÜKSEK — önemli bir iş fonksiyonu ciddi şekilde etkilenmiş; "
        "kullanıcı ya da ekip işini yapamıyor veya çok ciddi engelleniyor; "
        "geçici çözüm (workaround) yok ya da çok sınırlı."
    ),
    PRIORITY_P3: (
        "NORMAL — standart teknik sorun, etkisi sınırlı, tek kullanıcı ya da "
        "küçük bir ekip etkileniyor, iş tamamen durmuş değil."
    ),
    PRIORITY_P4: (
        "DÜŞÜK — bilgi talebi, kullanım sorusu, küçük/kozmetik problem ya da "
        "düşük etkili, acil olmayan istek."
    ),
}

# ---- Faz 5B: SLA politikası ----
# TEK TANIM NOKTASI. Kodun başka yerine hardcode edilmez; ileride DB/admin
# ayarına taşınacaksa yalnızca bu sözlüğün kaynağı değişir.
SLA_POLICY = {
    PRIORITY_P1: {"first_response_minutes": 15,  "resolution_minutes": 120},
    PRIORITY_P2: {"first_response_minutes": 30,  "resolution_minutes": 480},
    PRIORITY_P3: {"first_response_minutes": 120, "resolution_minutes": 1440},
    PRIORITY_P4: {"first_response_minutes": 480, "resolution_minutes": 4320},
}

# ---- Faz 6B: SLA çalışma takvimi ----
# SLA hesabı timezone-aware'dir. Saat dilimi TEK yerde tanımlanır; koda
# sabit UTC offset YAZILMAZ (DST'yi zoneinfo yönetir).
SLA_TIMEZONE = os.environ.get("SLA_TIMEZONE", "Europe/Istanbul")

# Haftalık çalışma saatleri. Anahtar: Python weekday (0=Pazartesi ... 6=Pazar).
# Değer: ("HH:MM", "HH:MM") ya da None (kapalı).
# Bu tablo TEK doğruluk kaynağıdır; saatler koda dağıtılmaz.
# Bu fazda gün başına TEK aralık yeterlidir (split shift yok).
BUSINESS_HOURS = {
    0: ("08:00", "18:00"),   # Pazartesi
    1: ("08:00", "18:00"),   # Salı
    2: ("08:00", "18:00"),   # Çarşamba
    3: ("08:00", "18:00"),   # Perşembe
    4: ("08:00", "18:00"),   # Cuma
    5: None,                 # Cumartesi — kapalı
    6: None,                 # Pazar — kapalı
}

# SLA_POLICY dakikaları artık İŞ DAKİKASI (business minutes) olarak yorumlanır.
# Örn. P3 resolution = 1440 iş dakikası; 10 saatlik iş gününde ~2.4 iş günü eder,
# 24 wall-clock saat DEĞİL.

# Faz 6B.1: süreçler arası takvim tutarlılığı.
# Her SLA rozetinde DB'ye gidilmesin diye revizyon kontrolü bu süre boyunca
# atlanır. Düşük değer = değişiklik daha hızlı yayılır, biraz daha çok sorgu.
CALENDAR_REVISION_TTL_SECONDS = float(
    os.environ.get("CALENDAR_REVISION_TTL_SECONDS", 5.0))

# Cycle takvim modu — eski kayıtların SLA sözleşmesi geriye dönük değişmesin diye.
CALENDAR_MODE_LEGACY = "LEGACY_WALL_CLOCK"
CALENDAR_MODE_BUSINESS = "BUSINESS_HOURS_V1"

# Kalan sürenin bu oranın altına inmesi "yaklaşıyor" (warning) sayılır.
SLA_WARNING_RATIO = float(os.environ.get("SLA_WARNING_RATIO", 0.2))

SLA_STATE_PENDING = "pending"
SLA_STATE_MET = "met"
SLA_STATE_OVERDUE = "overdue"
SLA_LEVEL_NORMAL = "normal"
SLA_LEVEL_WARNING = "warning"
SLA_LEVEL_OVERDUE = "overdue"


def sla_policy_for(priority: str):
    """Geçersiz/boş öncelik güvenli varsayılana düşer."""
    return SLA_POLICY.get(priority or DEFAULT_PRIORITY, SLA_POLICY[DEFAULT_PRIORITY])


# ---- Faz 5B: Giriş hız sınırı (brute-force koruması) ----
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", 5))
LOGIN_LOCKOUT_MINUTES = int(os.environ.get("LOGIN_LOCKOUT_MINUTES", 10))
# Ters vekil (reverse proxy) arkasında değilseniz KAPALI kalmalı; açıkken
# X-Forwarded-For'un ilk değeri kullanılır. Körü körüne güvenilmez.
TRUST_PROXY_HEADERS = _env_flag("TRUST_PROXY_HEADERS", False)

# ---- Faz 5B: Kuyruk değerlendirme ----
QUEUE_WORKER_INTERVAL_SECONDS = int(os.environ.get("QUEUE_WORKER_INTERVAL_SECONDS", 60))

# ---- Faz 5B: AI özeti veri yönetişimi ----
# Dahili notların AI özetine dahil edilip edilmeyeceği. Varsayılan AÇIK —
# Faz 5A'daki mevcut davranış korunur. Kurum politikası dahili notların
# üçüncü taraf modele gitmesini yasaklıyorsa 0 yapılabilir.
AI_SUMMARY_INCLUDE_INTERNAL_NOTES = _env_flag("AI_SUMMARY_INCLUDE_INTERNAL_NOTES", True)


# ---- Faz 4: Atama rolleri ----
# PRIMARY ve COLLABORATOR AYRI kavramlardır. "Yardımcı ekle" bir ownership
# transfer DEĞİLDİR; ana sorumluluk PRIMARY'de kalır.
ROLE_PRIMARY = "PRIMARY"
ROLE_COLLABORATOR = "COLLABORATOR"
ASSIGNMENT_ROLES = (ROLE_PRIMARY, ROLE_COLLABORATOR)
ASSIGNMENT_ROLE_LABELS = {
    ROLE_PRIMARY: "Ana Sorumlu",
    ROLE_COLLABORATOR: "Yardımcı Teknik Personel",
}

# Atamanın hangi kanaldan yapıldığı (mevcut audit yaklaşımıyla uyumlu)
ASSIGN_SOURCE_WEB = "web"
ASSIGN_SOURCE_TELEGRAM = "telegram"
ASSIGN_SOURCE_SYSTEM = "system"
ASSIGN_SOURCE_MIGRATION = "migration"

# Ownership transfer için gerekçe zorunludur.
TRANSFER_REASON_MIN_LENGTH = int(os.environ.get("TRANSFER_REASON_MIN_LENGTH", 5))


# ---- Faz 3B: AI konuşma durumu ----
AI_MODE_HANDLING = "AI_HANDLING"        # Gemini kullanıcıyla konuşuyor
AI_MODE_HUMAN = "HUMAN_ASSISTED"        # Teknik ekip devraldı, Gemini susar
AI_MODE_LABELS = {
    AI_MODE_HANDLING: "AI yürütüyor",
    AI_MODE_HUMAN: "Teknik ekip yürütüyor",
}

# Kullanıcı mesajı sayısı DEĞİL, AI'ın ürettiği GERÇEK ÇÖZÜM DENEMESİ sayısıdır.
# Açıklayıcı soru (clarification) bu sayacı artırmaz.
MAX_AI_SOLUTION_ATTEMPTS = int(os.environ.get("MAX_AI_SOLUTION_ATTEMPTS", 3))

# AI yanıt tipleri (structured output)
AI_RESPONSE_CLARIFICATION = "clarification"
AI_RESPONSE_SOLUTION = "solution_attempt"
AI_RESPONSE_ESCALATION = "escalation_ready"

# messages.message_kind — hangi AI mesajının deneme, hangisinin soru olduğu
MESSAGE_KIND_NORMAL = "normal"
MESSAGE_KIND_AI_SOLUTION = "ai_solution"
MESSAGE_KIND_AI_CLARIFICATION = "ai_clarification"
MESSAGE_KIND_SYSTEM = "system"
# Faz 5A: yalnızca teknik ekibin gördüğü dahili not.
# Görünürlük için AYRI bir 'visibility' kolonu AÇILMADI — aynı kavramı iki
# kolonda tekrar etmemek için message_kind tek doğruluk kaynağıdır.
MESSAGE_KIND_INTERNAL_NOTE = "internal_note"

# Telegram kullanıcısına GİDEBİLECEK mesaj türleri. Dahili not bu listede
# OLMADIĞI için hiçbir gönderim yoluna giremez.
PUBLIC_MESSAGE_KINDS = (
    MESSAGE_KIND_NORMAL,
    MESSAGE_KIND_AI_SOLUTION,
    MESSAGE_KIND_AI_CLARIFICATION,
    MESSAGE_KIND_SYSTEM,
)
