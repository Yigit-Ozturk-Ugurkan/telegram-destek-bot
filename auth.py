"""Web paneli kimlik doğrulama, yetkilendirme ve CSRF altyapısı.

Faz 2'de eklendi (panelde tek bir paylaşılan şifre vardı, kimlik yoktu).
Faz 2.5'te sertleştirildi:

  - session-bound GERÇEK CSRF koruması (idempotency token'dan AYRI kavram)
  - teknisyenler için ORTAK PAROLA KALDIRILDI; admin tek tek geçici parola verir
  - ilk girişte parola değiştirme zorunluluğu (must_change_password)
  - login'de session fixation'a karşı oturum sıfırlama
"""

import re
import secrets
import unicodedata
from functools import wraps

from flask import g, session, redirect, url_for, abort, request
from werkzeug.security import generate_password_hash, check_password_hash

import config
import db

CSRF_SESSION_KEY = "csrf_token"


class CurrentUser:
    """İstek boyunca geçerli kullanıcı. Kimlik BURADAN okunur, asla formdan değil."""

    def __init__(self, row: dict):
        self.id = row["id"]
        self.username = row["username"]
        self.role = row["role"]
        self.full_name = row.get("full_name") or row["username"]
        self.technician_id = row.get("technician_id")
        self.must_change_password = bool(row.get("must_change_password"))

    @property
    def is_admin(self) -> bool:
        return self.role == config.ROLE_ADMIN

    @property
    def is_technician(self) -> bool:
        return self.role == config.ROLE_TECHNICIAN

    def __repr__(self):
        # Parola/hash ASLA repr'e girmez.
        return f"<CurrentUser {self.username} ({self.role})>"


# ---------------- Oturum yaşam döngüsü ----------------

def load_current_user():
    """before_request kancası: oturumdaki id'den kullanıcıyı yükler."""
    g.current_user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    row = db.get_web_user(user_id)
    if row and row.get("active"):
        g.current_user = CurrentUser(row)
    else:
        # Kullanıcı silinmiş/pasifleştirilmişse oturumu düşür.
        session.clear()


def current_user():
    return getattr(g, "current_user", None)


def login_user(user_row: dict):
    """Session fixation'a karşı: mevcut oturum tamamen atılır, yeni kimlik ve
    YENİ bir CSRF token'ı ile temiz bir oturum kurulur."""
    session.clear()
    session["user_id"] = user_row["id"]
    rotate_csrf_token()
    g.current_user = CurrentUser(user_row)


def logout_user():
    session.clear()
    g.current_user = None


def authenticate(username: str, password: str):
    """Kullanıcı adı + şifre doğruysa kullanıcı satırını, aksi hâlde None döner."""
    row = db.get_web_user_by_username((username or "").strip())
    if not row:
        # Kullanıcı yoksa da hash karşılaştırması yaparak zamanlama farkını azalt.
        check_password_hash(
            "pbkdf2:sha256:600000$dummy$0000000000000000000000000000000"
            "000000000000000000000000000000000", password or "")
        return None
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    return row


# ---------------- CSRF (session-bound, gerçek CSRF koruması) ----------------
#
# DİKKAT: Bu, webapp.py'deki form_token ile AYNI ŞEY DEĞİLDİR.
#   csrf_token  -> oturuma bağlı, oturum boyunca sabit, sahte-site isteklerini
#                  engeller. Başka bir oturumun token'ı geçersizdir.
#   form_token  -> oturumdan bağımsız, tek kullanımlık, aynı isteğin İKİ KEZ
#                  işlenmesini (çift tıklama / F5) engeller.
# İkisi birlikte kullanılır; biri diğerinin yerine geçmez.

def get_csrf_token() -> str:
    """Oturumun CSRF token'ını döner, yoksa üretir."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf() -> bool:
    """İstekteki token, BU oturumun token'ı ile birebir eşleşmeli.

    Başka bir oturumdan alınan token burada eşleşmez, çünkü karşılaştırma
    sunucu tarafındaki oturum değerine karşı yapılır."""
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        return False
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not provided:
        return False
    return secrets.compare_digest(str(expected), str(provided))


# ---------------- Dekoratörler ----------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Yalnızca admin. Teknisyen erişirse 403 — link gizlemek yeterli değildir."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login"))
        if not user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def technician_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login"))
        if not user.is_technician:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ---------------- Talep sahipliği (authorization) ----------------

def authorize_ticket(ticket_id: int):
    """Talebi, giriş yapan kullanıcının GÖRMEYE YETKİLİ olduğu şekilde döner.

    Faz 4: erişim artık yalnızca assigned_technician_id'ye dayanmaz.
    - admin      : her talebi görebilir
    - technician : ana sorumlu (PRIMARY) VEYA aktif yardımcı (COLLABORATOR) ise

    Yetkisiz erişimde 403 ile durur. Bu kontrol backend'dedir; template
    filtresine veya gizlenmiş linklere güvenilmez."""
    user = current_user()
    if user is None:
        abort(401)

    if user.is_admin:
        return db.get_ticket_detail(ticket_id)

    if user.technician_id is None:
        abort(403)

    ticket = db.get_ticket_for_technician_role(ticket_id, user.technician_id)
    if ticket is None:
        # Talep var ama ilgisiz -> 403. Hiç yoksa da 403 döneriz ki
        # talep numaralarının varlığı sızmasın.
        abort(403)
    return ticket


def ticket_role(ticket_id: int):
    """Giriş yapan kullanıcının bu talepteki rolü: 'admin' | 'PRIMARY' |
    'COLLABORATOR' | None."""
    user = current_user()
    if user is None:
        return None
    if user.is_admin:
        return "admin"
    if user.technician_id is None:
        return None
    return db.get_technician_ticket_role(ticket_id, user.technician_id)


def can_manage_ticket(ticket_id: int) -> bool:
    """Durum yönetimi (çözme / yeniden açma / atama / yardımcı yönetimi) yetkisi.

    YALNIZCA admin ve mevcut ANA SORUMLU. Yardımcı (COLLABORATOR) ana sorumlu
    DEĞİLDİR: talebi görebilir ve mesaj yazabilir, ama durumunu değiştiremez,
    devredemez, başka yardımcı ekleyemez."""
    user = current_user()
    if user is None:
        return False
    if user.is_admin:
        return True
    return ticket_role(ticket_id) == config.ROLE_PRIMARY


def require_manage_ticket(ticket_id: int):
    if not can_manage_ticket(ticket_id):
        abort(403)


# ---------------- Parola kuralları ----------------

def validate_new_password(new_password: str, confirm: str):
    """(gecerli_mi, hata_mesaji) döner. Parola metni loglanmaz/döndürülmez."""
    if not new_password:
        return False, "Yeni şifre boş olamaz."
    if len(new_password) < config.PASSWORD_MIN_LENGTH:
        return False, f"Yeni şifre en az {config.PASSWORD_MIN_LENGTH} karakter olmalı."
    if new_password != confirm:
        return False, "Yeni şifreler birbiriyle eşleşmiyor."
    return True, None


def change_password(user_id: int, current_password: str, new_password: str, confirm: str):
    """(basarili_mi, mesaj) döner.

    Mevcut şifre doğrulanır; yeni şifre yalnızca hash olarak saklanır."""
    row = db.get_web_user(user_id)
    if not row:
        return False, "Kullanıcı bulunamadı."
    if not check_password_hash(row["password_hash"], current_password or ""):
        return False, "Mevcut şifreniz hatalı."
    if check_password_hash(row["password_hash"], new_password or ""):
        return False, "Yeni şifre mevcut şifreyle aynı olamaz."

    ok, error = validate_new_password(new_password, confirm)
    if not ok:
        return False, error

    db.set_web_user_password(user_id, generate_password_hash(new_password), must_change=False)
    return True, "Şifreniz güncellendi."


def set_temporary_password(user_id: int, temp_password: str):
    """Admin tarafından geçici parola atanır; kullanıcı ilk girişte değiştirmek
    ZORUNDA kalır. Ortak/sabit parola KULLANILMAZ."""
    ok, error = validate_new_password(temp_password, temp_password)
    if not ok:
        return False, error
    db.set_web_user_password(user_id, generate_password_hash(temp_password), must_change=True)
    return True, "Geçici şifre atandı. Kullanıcı ilk girişte değiştirmek zorunda."


# ---------------- Kullanıcı adı üretimi ----------------

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slugify_username(name: str) -> str:
    """'Ayşe Demir' -> 'ayse.demir'"""
    text = (name or "").translate(_TR_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", ".", text).strip(".").lower()
    return text or "kullanici"


def unique_username(base: str) -> str:
    candidate = base
    suffix = 2
    while db.get_web_user_by_username(candidate):
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def suggest_username_for(name: str) -> str:
    return unique_username(slugify_username(name))


# ---------------- Hesap oluşturma ----------------

def create_technician_web_user(technician_id: int, username: str, temp_password: str):
    """Admin, teknisyen için web hesabı açar.

    Kullanıcı adı ve GEÇİCİ parola admin tarafından belirlenir; kaynak kodda
    ortak bir varsayılan parola YOKTUR. Hesap must_change_password=1 ile açılır.
    (basarili_mi, mesaj) döner."""
    technician = db.get_technician(technician_id)
    if not technician:
        return False, "Teknisyen bulunamadı."
    if db.get_web_user_for_technician(technician_id):
        return False, "Bu teknisyenin zaten bir web hesabı var."

    username = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        return False, "Kullanıcı adı 3-40 karakter olmalı (a-z, 0-9, nokta, tire, alt çizgi)."
    if db.get_web_user_by_username(username):
        return False, "Bu kullanıcı adı zaten kullanılıyor."

    ok, error = validate_new_password(temp_password, temp_password)
    if not ok:
        return False, error

    db.create_web_user(
        username=username,
        password_hash=generate_password_hash(temp_password),
        role=config.ROLE_TECHNICIAN,
        full_name=technician["name"],
        technician_id=technician_id,
        must_change_password=True,
    )
    return True, f"{technician['name']} için '{username}' hesabı oluşturuldu."


# ---------------- Bootstrap ----------------

def bootstrap_admin():
    """İlk admin hesabını YALNIZCA ortam değişkenlerinden oluşturur.

    WEB_ADMIN_PASSWORD tanımlı değilse hiçbir şey yapılmaz — kaynak kodda
    varsayılan parola yoktur. Hesap zaten varsa parolası ASLA sıfırlanmaz.
    Dönen değer sadece 'oluşturuldu mu' bilgisidir; parola döndürülmez."""
    if not config.WEB_ADMIN_PASSWORD:
        return False
    if db.get_web_user_by_username(config.WEB_ADMIN_USERNAME):
        return False
    db.create_web_user(
        username=config.WEB_ADMIN_USERNAME,
        password_hash=generate_password_hash(config.WEB_ADMIN_PASSWORD),
        role=config.ROLE_ADMIN,
        full_name="Panel Yöneticisi",
    )
    return True


def revoke_shared_password_accounts():
    """Faz 2'de ORTAK parolayla açılmış teknisyen hesaplarını etkisizleştirir.

    Bu hesapların parolası kullanılamaz rastgele bir değerle değiştirilir ve
    must_change_password=1 yapılır; böylece eski ortak parola hiçbir hesapta
    çalışmaz. Admin, ilgili kişiye ekip sayfasından yeni bir geçici parola verir.

    Yalnızca password_changed_at NULL olan (yani hiç parola değiştirmemiş)
    teknisyen hesaplarına dokunur; idempotenttir."""
    revoked = 0
    for row in db.list_web_users():
        if row["role"] != config.ROLE_TECHNICIAN:
            continue
        if row.get("password_changed_at"):
            continue          # kullanıcı kendi parolasını belirlemiş, dokunma
        if row.get("must_change_password"):
            continue          # zaten etkisizleştirilmiş
        db.set_web_user_password(
            row["id"], generate_password_hash(secrets.token_urlsafe(32)), must_change=True
        )
        revoked += 1
    return revoked
