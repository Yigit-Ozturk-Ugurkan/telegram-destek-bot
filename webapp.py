import secrets
from datetime import datetime

import requests
from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort
)

import config
import db
import auth
import ai_assistant
import queue_service
import routing
import sla_calendar

app = Flask(__name__)
app.secret_key = config.WEB_SECRET_KEY

# Oturum çerezi güvenliği (Faz 2.5).
#   HTTPONLY -> JavaScript çerezi okuyamaz (XSS ile oturum çalınmasını zorlaştırır)
#   SAMESITE -> çapraz siteden gelen POST'larda çerez gönderilmez (CSRF'e ek katman)
#   SECURE   -> yalnızca HTTPS. Development'ta HTTP localhost bozulmasın diye
#               varsayılan kapalıdır, .env'den SESSION_COOKIE_SECURE=1 ile açılır.
app.config.update(
    SESSION_COOKIE_HTTPONLY=config.SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE=config.SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
)

# State-changing metodlar — CSRF doğrulaması bunlarda zorunludur.
CSRF_PROTECTED_METHODS = ("POST", "PUT", "PATCH", "DELETE")

TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"


def send_telegram_message(chat_id: int, text: str, reply_markup: dict = None):
    """Telegram'a düz bir HTTP isteğiyle mesaj gönderir.

    (Flask senkron çalıştığı için python-telegram-bot'un async Bot nesnesi burada
    kullanılmıyor — art arda gelen isteklerde bağlantı havuzu tıkanıyordu.)

    Faz 2: artık flash atmıyor, (ok, hata_metni) döner. Çağıran taraf sonucu
    mesaj satırına delivery_status olarak işler; böylece kullanıcıya ULAŞMAYAN
    bir yanıt panelde 'gönderildi' gibi görünmez."""
    try:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = requests.post(TELEGRAM_SEND_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)[:500]


AVATAR_COLORS = [
    "#8a5606", "#2f5fc4", "#1f6b41", "#a3438a", "#c4432f",
    "#5b4fc4", "#0a6f71", "#9c6b1f", "#3f5fd0", "#b5495b",
]


def avatar_initials(name: str) -> str:
    parts = (name or "").strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def avatar_color(name: str) -> str:
    return AVATAR_COLORS[sum(ord(c) for c in (name or "")) % len(AVATAR_COLORS)]


def legacy_primary_name(ticket: dict):
    """LEGACY FALLBACK (Faz 5A) — TEK okuma noktası.

    tickets.assigned_admin_name yalnızca Faz 4 öncesi kayıtlarda doludur ve
    hiçbir yerde YENİ DEĞER YAZILMAZ. Ana sorumlu adı normalde
    assigned_technician_id -> technicians.name üzerinden çözülür; bu helper
    yalnızca o ilişki boş kaldığında devreye girer ve legacy okumayı tek
    yerde izole eder."""
    return ticket.get("assigned_admin_name")


def elapsed_label(stamp) -> str:
    """'12dk' / '3sa' / '2g' — ticket kartındaki geçen süre etiketi.

    SUNUM helper'ıdır; SLA hesabıyla İLGİSİ YOKTUR. SLA süreleri iş takvimine
    göre db.get_sla_state() içinde hesaplanır ve bu fonksiyon oraya
    KARIŞMAZ."""
    if not stamp:
        return ""
    moment = db._parse_ts(stamp)
    if moment is None:
        return str(stamp)
    seconds = (datetime.now() - moment).total_seconds()
    if seconds < 60:
        return "şimdi"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}dk"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}sa"
    return f"{hours // 24}g"


# ---------------- İllüstratif ekip avatarları ----------------
#
# static/avatars/ altında sabit sayıda SVG durur. Bunlar PROFİL FOTOĞRAFI
# DEĞİLDİR: projede fotoğraf yükleme yoktur ve bu revizyonda eklenmedi.
#
# SEÇİM TAMAMEN SUNUM KATMANINDADIR:
#   - DB'ye avatar kolonu EKLENMEDİ
#   - avatar için sorgu ÇALIŞTIRILMAZ (N+1 yok)
#   - harici CDN / uzak URL kullanılmaz
# technician_id zaten elimizde olduğu için modulo ile deterministik eşleriz:
# aynı teknisyen her render'da, her ekranda AYNI avatarı görür.
AVATAR_ILLUSTRATION_COUNT = 12


def avatar_illustration(technician_id):
    """technician_id -> static/avatars/av-NN.svg (deterministik) veya None.

    None dönmesi bir hata değildir: teknisyen kaydına bağlanamayan bir satır
    (ör. yalnızca eski `assigned_admin_name` taşıyan legacy kayıt) illüstratif
    avatar yerine mevcut initials avatarını kullanır."""
    if technician_id is None or isinstance(technician_id, bool):
        return None
    try:
        index = int(technician_id) % AVATAR_ILLUSTRATION_COUNT
    except (TypeError, ValueError):
        return None
    return f"avatars/av-{index + 1:02d}.svg"


app.jinja_env.filters["avatar_initials"] = avatar_initials
app.jinja_env.filters["avatar_color"] = avatar_color
app.jinja_env.filters["elapsed"] = elapsed_label
app.jinja_env.globals["avatar_illustration"] = avatar_illustration


# ---------------- Form token (ATOMİK çift gönderim koruması) ----------------

def issue_form_token() -> str:
    """Her form render'ında tek kullanımlık bir token üretir.

    Token artık oturum çerezinde TUTULMAZ. Eski cookie tabanlı yaklaşım eş
    zamanlı iki isteği durduramıyordu (ikisi de tıklama öncesi çerezi taşıdığı
    için ikisi de geçiyordu). Artık POST anında DB'ye atomik INSERT yapılır."""
    return secrets.token_urlsafe(16)


def consume_form_token() -> bool:
    """Aynı token için yalnızca ilk POST True döner (SQLite UNIQUE garantisi).

    NOT: Bu CSRF koruması DEĞİLDİR — oturuma bağlı değildir. Gerçek CSRF
    kontrolü auth.validate_csrf() ile _csrf_protect içinde yapılır."""
    return db.claim_form_token(request.form.get("form_token", ""))


app.jinja_env.globals["form_token"] = issue_form_token
app.jinja_env.globals["csrf_token"] = auth.get_csrf_token
app.jinja_env.globals["password_min"] = config.PASSWORD_MIN_LENGTH
app.jinja_env.globals["STATUS_LABELS"] = config.STATUS_LABELS
app.jinja_env.globals["STATUS"] = {
    "OPEN": config.STATUS_OPEN,
    "ASSIGNED": config.STATUS_ASSIGNED,
    "IN_PROGRESS": config.STATUS_IN_PROGRESS,
    "WAITING": config.STATUS_WAITING_USER,
    "CLOSED": config.STATUS_CLOSED,
    "REOPENED": config.STATUS_REOPENED,
}


@app.before_request
def _load_user():
    auth.load_current_user()


@app.before_request
def _csrf_protect():
    """Bütün state-changing isteklerde oturuma bağlı CSRF doğrulaması.

    Tek tek rotalara güvenmek yerine merkezî kontrol yapıyoruz; böylece
    ileride eklenecek bir POST rotası yanlışlıkla korumasız kalamaz."""
    if request.method not in CSRF_PROTECTED_METHODS:
        return None
    if request.endpoint == "static":
        return None
    if not auth.validate_csrf():
        return render_template("csrf_error.html"), 400
    return None


@app.before_request
def _force_password_change():
    """Geçici parolayla giren kullanıcı, parolasını değiştirmeden panelde
    dolaşamaz. Yalnızca parola değiştirme ve çıkış rotaları serbesttir."""
    user = auth.current_user()
    if user is None or not user.must_change_password:
        return None
    if request.endpoint in ("change_password", "logout", "static"):
        return None
    return redirect(url_for("change_password"))


@app.context_processor
def _inject_globals():
    """Sidebar ve sağ AKTİF EKİP paneli verisi.

    ACCEPTANCE REVISION: aktif ekip listesi admin sidebar'ından ÇIKARILDI ve
    sağdaki salt-okunur panele (team_rail) taşındı. Aynı panel teknisyene de
    gösterilir; teknisyen görünümü READ-ONLY'dir — yönetim aksiyonu içermez.
    Yetki kontrolü UI'da buton gizlemekle YAPILMAZ: yönetim rotaları
    @auth.admin_required ile korunur ve teknisyen POST ederse 403 alır.

    N+1 YOK: db.list_team_overview() sabit sayıda sorgu kullanır (teknisyenler +
    iş yükü + tüm uzmanlıklar + yardımcılık aggregate'i). Kişi başına ayrı
    skills / workload / son-atama sorgusu çalıştırılmaz."""
    user = auth.current_user()
    if user is None:
        return {"current_user": None}

    data = {"current_user": user}
    # Sağ panel hem admin hem teknisyen için AYNI veri kaynağından beslenir.
    data["team_rail"] = db.list_team_overview()
    data["team_priorities"] = db.VALID_PRIORITIES
    if user.is_admin:
        data["sidebar_counts"] = db.counts_by_group()
    else:
        data["sidebar_counts"] = db.counts_by_group(user.technician_id)
        data["sidebar_self"] = (
            db.get_technician(user.technician_id) if user.technician_id else None
        )
        # Teknisyen kendi uzmanlıklarını SALT OKUNUR görür; değiştiremez.
        data["sidebar_skills"] = (
            db.list_technician_skills(user.technician_id) if user.technician_id else []
        )
        # Faz 4: yardımcı olunan açık talep sayısı. PRIMARY iş yüküne
        # KARIŞTIRILMAZ — Faz 3 routing sıralaması değişmez.
        data["sidebar_collab_count"] = (
            db.count_collaborations(user.technician_id) if user.technician_id else 0
        )
    return data


@app.errorhandler(403)
def _forbidden(_e):
    return render_template("403.html"), 403


# ---------------- Kimlik doğrulama ----------------

def client_identifier() -> str:
    """Hız sınırı için istemci kimliği.

    X-Forwarded-For'a KÖRÜ KÖRÜNE GÜVENİLMEZ — yalnızca config'de
    TRUST_PROXY_HEADERS açıksa (yani gerçekten bir ters vekil arkasındaysanız)
    dikkate alınır. Aksi hâlde saldırgan başlığı uydurup limiti atlayabilirdi."""
    if config.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "bilinmeyen"


@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.current_user() is not None:
        return redirect(url_for("index"))
    if request.method == "POST":
        identifier = client_identifier()
        # Brute-force koruması: kilitliyken parola DOĞRULANMAZ bile.
        if db.is_login_blocked(identifier):
            flash(f"Çok fazla başarısız deneme. Lütfen "
                  f"{config.LOGIN_LOCKOUT_MINUTES} dakika sonra tekrar deneyin.", "error")
            return render_template("login.html"), 429

        user_row = auth.authenticate(
            request.form.get("username", ""), request.form.get("password", "")
        )
        if user_row:
            db.clear_login_failures(identifier)   # başarılı girişte sayaç sıfırlanır
            auth.login_user(user_row)
            return redirect(url_for("index"))
        # Parola ASLA loglanmaz/saklanmaz; yalnızca deneme zaman damgası tutulur.
        db.record_login_failure(identifier)
        flash("Kullanıcı adı veya şifre hatalı.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@auth.login_required
def logout():
    auth.logout_user()
    return redirect(url_for("login"))


@app.route("/sifre-degistir", methods=["GET", "POST"])
@auth.login_required
def change_password():
    """Şifre değiştirme. Admin hiçbir kullanıcının mevcut şifresini GÖREMEZ;
    yalnızca yeni bir geçici şifre atayabilir."""
    user = auth.current_user()
    if request.method == "POST":
        ok, message = auth.change_password(
            user.id,
            request.form.get("current_password", ""),
            request.form.get("new_password", ""),
            request.form.get("confirm_password", ""),
        )
        if ok:
            # Parola değişti; oturumu tazeleyip CSRF token'ını döndürüyoruz.
            auth.rotate_csrf_token()
            flash(message, "success")
            return redirect(url_for("index"))
        flash(message, "error")
    return render_template("change_password.html", forced=user.must_change_password)


# ---------------- Ana yönlendirme ----------------

@app.route("/")
@auth.login_required
def index():
    user = auth.current_user()
    if user.is_technician:
        return redirect(url_for("my_tickets"))
    return redirect(url_for("admin_tickets"))


# ---------------- ADMIN: bütün talepler ----------------

@app.route("/tickets")
@auth.admin_required
def admin_tickets():
    group = request.args.get("status") or "all"
    # Faz 5B: uzman bekleyen kuyruğu da bir durum filtresi olarak sunulur.
    if group == config.STATUS_ESCALATION_PENDING:
        # §28: kuyruk dayanağa göre süzülebilir; admin "neden atanmadı?"
        # sorusunu tek bakışta ayırt edebilsin.
        basis_filter = request.args.get("basis")
        if basis_filter not in config.ROUTING_BASES:
            basis_filter = None
        tickets = db.list_waiting_expert_tickets(basis=basis_filter)
        # Dayanak etiketi satır bazında hesaplanır. queue_service.waiting_reason
        # yalnızca SUNUM üretir; kuyruk kararı routing.py'dedir.
        for t in tickets:
            t["waiting"] = queue_service.waiting_reason(t)
    else:
        if group not in ("all", "active", "waiting", "resolved"):
            group = "all"
        tickets = db.list_admin_tickets_filtered(
            None if group == "all" else group,
            priority=request.args.get("priority"),
            sla=request.args.get("sla"),
        )
    # SLA durumu satır bazında runtime hesaplanır (DB'de tutulmaz).
    for t in tickets:
        t["sla"] = db.get_sla_state(t)
    return render_template(
        "admin_tickets.html",
        tickets=tickets,
        group=group,
        priority_filter=request.args.get("priority"),
        sla_filter=request.args.get("sla"),
        counts=db.operations_counts(),
        waiting_status=config.STATUS_ESCALATION_PENDING,
        # §28: dayanak dağılımı — TEK aggregate sorgu, talep başına ek sorgu yok.
        basis_counts=db.count_waiting_by_routing_basis(),
        basis_filter=request.args.get("basis"),
        ROUTING_BASES=config.ROUTING_BASES,
        ROUTING_BASIS_LABELS=config.ROUTING_BASIS_LABELS,
        MANUAL_TRIAGE=config.ROUTING_BASIS_MANUAL_TRIAGE,
    )


# ---------------- TECHNICIAN: yalnızca kendi talepleri ----------------

@app.route("/my-tickets")
@auth.technician_required
def my_tickets():
    user = auth.current_user()
    group = request.args.get("status") or "active"
    if group not in ("active", "waiting", "resolved", "all"):
        group = "active"

    if user.technician_id is None:
        flash("Kullanıcınız bir teknisyen kaydına bağlı değil. Yöneticinize başvurun.", "error")
        return render_template(
            "my_tickets.html", tickets=[], group=group,
            counts={"active": 0, "waiting": 0, "resolved": 0, "all": 0},
        )

    if group == "resolved":
        tickets = db.list_resolved_tickets(technician_id=user.technician_id)
    else:
        tickets = db.list_technician_tickets(
            user.technician_id, None if group == "all" else group
        )
    for t in tickets:
        t["sla"] = db.get_sla_state(t)
    return render_template(
        "my_tickets.html",
        tickets=tickets,
        group=group,
        counts=db.counts_by_group(user.technician_id),
    )


# ---------------- Talep detayı ----------------

def _ticket_sidebar_context(user):
    """Talep detayındaki SOL LİSTE için veri — YETKİYE GÖRE SUNUCUDA süzülür.

    Admin      -> bütün talepler (db.list_admin_tickets_filtered)
    Technician -> YALNIZCA kendi ana sorumlusu olduğu talepler
                  (db.list_technician_tickets)

    Yeni bir yetkilendirme yolu açılmaz; mevcut sorgular yeniden kullanılır.
    Teknisyen yalnızca YARDIMCI olduğu bir talebi açtığında o talep bu listede
    görünmeyebilir — konuşma yine orta kolonda açılır, liste ise 'Destek
    Verdiklerim' ekranındadır."""
    group = request.args.get("status") or "active"
    if group not in ("all", "active", "resolved"):
        group = "active"

    if user.is_admin:
        tickets = db.list_admin_tickets_filtered(
            None if group == "all" else group)
        counts = db.counts_by_group()
        context = {"sidebar_endpoint": "admin_tickets",
                   "sidebar_title": "Tüm talepler",
                   "sidebar_subtitle": "Bütün teknik personelin talepleri"}
    elif user.technician_id is not None:
        if group == "resolved":
            tickets = db.list_resolved_tickets(technician_id=user.technician_id)
        else:
            tickets = db.list_technician_tickets(
                user.technician_id, None if group == "all" else group)
        counts = db.counts_by_group(user.technician_id)
        context = {"sidebar_endpoint": "my_tickets",
                   "sidebar_title": "Benim taleplerim",
                   "sidebar_subtitle": "Yalnızca size atanmış talepler"}
    else:
        tickets, counts = [], {"all": 0, "active": 0, "resolved": 0}
        context = {"sidebar_endpoint": "my_tickets",
                   "sidebar_title": "Benim taleplerim",
                   "sidebar_subtitle": None}

    for t in tickets:
        t["sla"] = db.get_sla_state(t)
    context.update({"sidebar_tickets": tickets, "sidebar_group": group,
                    "sidebar_group_counts": counts})
    return context


@app.route("/ticket/<int:ticket_id>")
@auth.login_required
def ticket_detail(ticket_id):
    # authorize_ticket: admin -> her talep; technician -> yalnızca kendi talebi (aksi hâlde 403)
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))
    user = auth.current_user()

    # Çözen kişi: önce gerçek ilişki (web_users), yoksa denetim etiketi.
    resolver_name = None
    if ticket.get("resolved_by_user_id"):
        row = db.get_web_user(ticket["resolved_by_user_id"])
        if row:
            resolver_name = row.get("full_name") or row["username"]
    if not resolver_name:
        resolver_name = ticket.get("resolved_by_label")

    # Faz 3B: AI durumu. ai_mode boşsa (eski kayıt) türetilmiş değer gösterilir.
    ai_mode = ticket.get("ai_mode")
    if not ai_mode:
        ai_mode = (config.AI_MODE_HUMAN
                   if (ticket.get("escalated") or ticket.get("assigned_technician_id"))
                   else config.AI_MODE_HANDLING)

    required_skill_name = None
    if ticket.get("required_skill_code"):
        skill = db.get_skill_by_code(ticket["required_skill_code"])
        if skill:
            required_skill_name = skill["name"]

    # §13: yönlendirme denetimi. Dayanak + hedef, mevcut alanların YERİNE
    # geçmez; required_skill_code ve category olduğu gibi korunur.
    routing_basis = routing.effective_routing_basis(ticket)

    # FRONTEND DESIGN REVISION: talep detayı artık Talepler ekranının SEÇİLİ
    # hâlidir; soldaki liste yerinde kalır. Liste YETKİYE GÖRE SUNUCUDA
    # süzülür — admin bütün talepleri, teknisyen YALNIZCA kendi ana sorumlusu
    # olduğu talepleri görür. Şablon filtresi kullanılmaz, mevcut sorgular
    # (list_admin_tickets_filtered / list_technician_tickets) yeniden
    # kullanılır; yeni bir yetkilendirme yolu AÇILMAZ.
    sidebar_context = _ticket_sidebar_context(user)

    return render_template(
        "detail.html",
        ticket=ticket,
        **sidebar_context,
        # PRIORITY ROUTING REVISION §B3: AI öncelik önerisi + gerekçesi.
        # İÇ OPERASYON verisidir; Telegram'a giden hiçbir metin kullanmaz.
        ai_priority=db.get_ai_priority_advice(ticket_id),
        messages=db.get_messages(ticket_id),
        resolver_name=resolver_name,
        resolved_source_label=config.RESOLVED_SOURCE_LABELS.get(
            ticket.get("resolved_by_source")),
        ai_mode=ai_mode,
        ai_mode_label=config.AI_MODE_LABELS.get(ai_mode, ai_mode),
        max_ai_attempts=config.MAX_AI_SOLUTION_ATTEMPTS,
        required_skill_name=required_skill_name,
        routing_basis=routing_basis,
        routing_basis_label=(routing.basis_label(routing_basis)
                             if routing_basis else None),
        routing_summary=routing.routing_summary(ticket),
        AI_MODE_HUMAN=config.AI_MODE_HUMAN,
        # §8: manuel triyaj taleplerinde admin uzmanlık/departman atayabilir.
        skills=db.list_skills(only_active=True) if auth.current_user().is_admin else [],
        DEPARTMENTS=config.DEPARTMENTS,
        MANUAL_TRIAGE=config.ROUTING_BASIS_MANUAL_TRIAGE,
        technicians=db.list_technicians() if auth.current_user().is_admin else [],
        # --- Faz 4: atama modeli ---
        assignments=db.list_ticket_assignments(ticket_id, active_only=True),
        collaborators=db.list_active_collaborators(ticket_id),
        my_role=auth.ticket_role(ticket_id),
        can_manage=auth.can_manage_ticket(ticket_id),
        # Yardımcı adayları: N+1 yok (Faz 3A helper'ı + tek atama sorgusu)
        collaborator_candidates=(db.list_collaborator_candidates(ticket_id)
                                 if auth.can_manage_ticket(ticket_id) else []),
        # --- Faz 6B ---
        calendar_status=sla_calendar.calendar_status(),
        # --- Faz 6A ---
        sla_cycles=db.list_sla_cycles(ticket_id),
        cycle_states=[db.get_cycle_sla_state(c) for c in db.list_sla_cycles(ticket_id)],
        # --- Faz 5B ---
        sla=db.get_sla_state(ticket),
        PRIORITIES=config.PRIORITIES,
        PRIORITY_LABELS=config.PRIORITY_LABELS,
        # --- Faz 5A ---
        ai_summary=db.get_ai_summary(ticket_id),
        INTERNAL_NOTE_KIND=config.MESSAGE_KIND_INTERNAL_NOTE,
        ROLE_PRIMARY=config.ROLE_PRIMARY,
        ROLE_COLLABORATOR=config.ROLE_COLLABORATOR,
        ROLE_LABELS=config.ASSIGNMENT_ROLE_LABELS,
        transfer_reason_min=config.TRANSFER_REASON_MIN_LENGTH,
    )


# ---------------- Yanıt yazma ----------------

@app.route("/ticket/<int:ticket_id>/reply", methods=["POST"])
@auth.login_required
def reply(ticket_id):
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))

    user = auth.current_user()
    message_text = request.form.get("message", "").strip()

    if not message_text:
        flash("Boş mesaj gönderilemez. Lütfen iletilecek yanıtı yazın.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    # Çözülmüş talebe doğrudan yanıt yazmak durumu geçersiz şekilde geri
    # döndürürdü (resolved -> in_progress). Önce yeniden açılması gerekir.
    if ticket["status"] == config.STATUS_CLOSED:
        flash("Çözülmüş talebe yanıt yazmak için önce 'Talebi Yeniden Aç' deyin.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if not consume_form_token():
        flash("Bu yanıt zaten gönderildi; kullanıcıya tekrar iletilmedi.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    # Kimlik current_user'dan gelir; formdan gelen isim ASLA kullanılmaz.
    sender_type = "teknisyen" if user.is_technician else "yonetici"
    message_id = db.add_message(
        ticket_id, sender_type, 0, user.full_name, message_text,
        sender_user_id=user.id, delivery_status="gonderiliyor",
    )

    # Atanmış / açık / bekleyen bir talebe ilk yanıt gelince "İşlemde"ye taşı.
    if ticket["status"] != config.STATUS_IN_PROGRESS \
            and config.can_transition(ticket["status"], config.STATUS_IN_PROGRESS):
        db.set_ticket_status(ticket_id, config.STATUS_IN_PROGRESS)

    # Faz 5B: İLK PUBLIC personel yanıtı SLA first response'udur.
    # Dahili not / sistem mesajı / AI mesajı bu damgayı ATMAZ.
    db.mark_first_staff_response(ticket_id)

    # Faz 2.5: tickets.assigned_admin_name'e ARTIK YAZMIYORUZ. Mesajı kimin
    # yazdığı messages.sender_user_id -> web_users ilişkisinden çözülür;
    # talebin ana sorumlusu ise assigned_technician_id'dir.

    ok, error = send_telegram_message(
        ticket["user_id"], f"💬 {user.full_name} (Teknik Ekip):\n{message_text}"
    )
    if ok:
        db.set_message_delivery(message_id, "gonderildi")
        flash("Yanıt kullanıcıya iletildi.", "success")
    else:
        db.set_message_delivery(message_id, "basarisiz", error)
        flash(
            f"Yanıt kaydedildi ANCAK Telegram'a iletilemedi ({error}). "
            "Mesaj geçmişinde 'iletilemedi' olarak işaretlendi.",
            "error",
        )
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Durum değiştirme ----------------

@app.route("/ticket/<int:ticket_id>/status", methods=["POST"])
@auth.login_required
def change_status(ticket_id):
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))

    # Faz 4: yardımcı (COLLABORATOR) ana sorumlu değildir — durum değiştiremez.
    auth.require_manage_ticket(ticket_id)

    user = auth.current_user()
    new_status = request.form.get("status")

    if new_status not in config.STATUS_LABELS:
        flash("Geçersiz durum.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    # Teknisyen yalnızca kendi iş akışındaki durumları kullanabilir;
    # atama (atandi) admin işidir.
    if user.is_technician and new_status not in (
        config.STATUS_IN_PROGRESS, config.STATUS_WAITING_USER, config.STATUS_CLOSED
    ):
        abort(403)

    if not config.can_transition(ticket["status"], new_status):
        flash(
            f"Geçersiz durum geçişi: {config.STATUS_LABELS[ticket['status']]} → "
            f"{config.STATUS_LABELS[new_status]}. Çözülmüş bir talep önce yeniden açılmalıdır.",
            "error",
        )
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if not consume_form_token():
        flash(f"Talep #{ticket_id} durumu zaten güncellenmişti; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if new_status == config.STATUS_CLOSED:
        # Denetim izi: kim (web_users.id) + hangi kanal (web).
        db.resolve_ticket(ticket_id, web_user_id=user.id,
                          source=config.RESOLVED_SOURCE_WEB, label=user.full_name)
        db.add_message(
            ticket_id, "sistem", 0, "Sistem",
            f"Talep {user.full_name} tarafından çözüldü olarak işaretlendi.",
        )
        note = "Talebiniz çözümlendi. Sorun devam ediyorsa bu talebe yazmanız yeterli."
    else:
        db.set_ticket_status(ticket_id, new_status)
        note = ""

    # Yeniden açma YALNIZCA bu açık aksiyonla olur; kullanıcının serbest metnini
    # LLM ile yorumlayıp tahmini reopen YAPILMAZ.
    reopen_button = None
    if new_status == config.STATUS_CLOSED:
        reopen_button = {"inline_keyboard": [[{
            "text": "🔄 Sorun hâlâ devam ediyor",
            "callback_data": f"reopen:{ticket_id}",
        }]]}

    ok, error = send_telegram_message(
        ticket["user_id"],
        f"ℹ️ Talebiniz #{ticket_id} güncellendi.\nYeni durum: {config.STATUS_LABELS[new_status]}"
        + (f"\n\n{note}" if note else ""),
        reply_markup=reopen_button,
    )
    if ok:
        flash(f"Talep #{ticket_id} durumu güncellendi.", "success")
    else:
        flash(f"Durum güncellendi ancak kullanıcıya bildirim iletilemedi ({error}).", "error")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Yeniden açma ----------------

@app.route("/ticket/<int:ticket_id>/reopen", methods=["POST"])
@auth.login_required
def reopen(ticket_id):
    """Çözülmüş talebi AYNI ID ile yeniden açar — yeni talep oluşturulmaz."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))

    # Faz 4: yardımcı yeniden açamaz.
    auth.require_manage_ticket(ticket_id)

    if ticket["status"] != config.STATUS_CLOSED:
        flash("Yalnızca çözülmüş talepler yeniden açılabilir.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if not consume_form_token():
        flash(f"Talep #{ticket_id} zaten yeniden açılmıştı; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    user = auth.current_user()
    db.reopen_ticket(ticket_id)
    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"Talep {user.full_name} tarafından yeniden açıldı.",
    )
    ok, error = send_telegram_message(
        ticket["user_id"],
        f"🔄 Talebiniz #{ticket_id} yeniden açıldı. Ekibimiz konuyla ilgilenmeye devam ediyor.",
    )
    if ok:
        flash(f"Talep #{ticket_id} yeniden açıldı.", "success")
    else:
        flash(f"Talep yeniden açıldı ancak kullanıcıya bildirim iletilemedi ({error}).", "error")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Teknisyene atama (yalnızca admin) ----------------

@app.route("/ticket/<int:ticket_id>/assign", methods=["POST"])
@auth.admin_required
def assign(ticket_id):
    ticket = db.get_ticket_detail(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))

    raw_id = request.form.get("technician_id", "")
    if not raw_id.isdigit():
        flash("Geçerli bir teknisyen seçin.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    technician = db.get_technician(int(raw_id))
    if not technician:
        flash("Teknisyen bulunamadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if ticket["status"] == config.STATUS_CLOSED:
        flash("Çözülmüş talep atanamaz. Önce yeniden açın.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if not consume_form_token():
        flash(f"Talep #{ticket_id} zaten atanmıştı; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    # Faz 4: TEK atama yolu — bot ve web aynı fonksiyonu kullanır.
    user = auth.current_user()
    actor = {"user_id": user.id, "source": config.ASSIGN_SOURCE_WEB,
             "label": user.full_name}
    if ticket.get("assigned_technician_id"):
        # Zaten bir ana sorumlu var -> bu bir DEVİR işlemidir, gerekçe ister.
        flash("Talebin zaten bir ana sorumlusu var. 'Ana Sorumluluğu Devret' "
              "bölümünü kullanın (gerekçe zorunludur).", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    if not db.assign_primary_if_unassigned(
            ticket_id, technician["id"], technician["name"], actor=actor):
        flash("Talep bu sırada başka birine atandı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"Talep {technician['name']} adlı teknisyene ana sorumlu olarak atandı.",
        message_kind=config.MESSAGE_KIND_SYSTEM,
    )
    send_telegram_message(
        ticket["user_id"],
        f"🔧 Talebiniz #{ticket_id} {technician['name']} tarafından üstlenildi.",
    )
    if technician.get("telegram_id"):
        send_telegram_message(
            technician["telegram_id"],
            f"📋 Size bir talep atandı: Talep #{ticket_id} — {ticket['subject']}",
        )
    flash(f"Talep #{ticket_id} {technician['name']} adlı teknisyene atandı.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- §8: Manuel yönlendirme (yalnızca admin) ----------------

@app.route("/ticket/<int:ticket_id>/yonlendirme", methods=["POST"])
@auth.admin_required
def set_routing(ticket_id):
    """Sınıflandırma belirsiz kalmış talebi ADMIN elle yönlendirir.

    §8: MANUAL_TRIAGE talebi otomatik olarak GENEL DESTEĞE gitmez; admin
    burada ya bir uzmanlık ya da bir departman seçer. Seçim yapıldığı anda
    talep ilgili kuyruğa girer ve merkezi motor (routing.select_for_ticket)
    uygun kişi varsa hemen atar.

    Uzmanlık ve departman AYNI ANDA gönderilirse UZMANLIK kazanır — §4'teki
    'specific skill en yüksek öncelik' kuralı burada da geçerlidir.

    Admin GENERAL_TECH_SUPPORT'u da seçebilir; bu AÇIK bir karardır ve
    dayanak GENERAL_SUPPORT olarak kaydedilir. Otomatik düşüş DEĞİLDİR."""
    ticket = db.get_ticket_detail(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))
    if ticket["status"] == config.STATUS_CLOSED:
        flash("Çözülmüş talebin yönlendirmesi değiştirilemez.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    if not consume_form_token():
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    raw_skill = (request.form.get("skill_code") or "").strip()
    raw_department = (request.form.get("department") or "").strip()

    # SERVER-SIDE DOĞRULAMA: form değerine güvenilmez. Aynı doğrulama
    # fonksiyonları model çıktısı için de kullanılıyor (tek tanım).
    skill_code = routing._validated_skill_code(raw_skill) if raw_skill else None
    department = raw_department if raw_department in config.DEPARTMENTS else None

    if raw_skill and not skill_code:
        flash("Seçilen uzmanlık geçersiz ya da pasif.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    if not skill_code and not department:
        flash("Bir uzmanlık ya da departman seçmelisiniz.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if skill_code:
        basis = (config.ROUTING_BASIS_GENERAL_SUPPORT
                 if skill_code == config.GENERAL_SUPPORT_SKILL_CODE
                 else config.ROUTING_BASIS_SPECIFIC_SKILL)
    else:
        basis = config.ROUTING_BASIS_DEPARTMENT

    db.set_ticket_routing(ticket_id, basis, skill_code=skill_code,
                          department=department or ticket.get("category"))
    user = auth.current_user()
    target = (routing.skill_display_name(skill_code) if skill_code
              else department)
    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"{user.full_name} talebi elle yönlendirdi. "
        f"{routing.basis_label(basis)}: {target}",
        message_kind=config.MESSAGE_KIND_SYSTEM,
    )

    # Talep kuyruktaysa hemen yeniden değerlendirilir: uygun kişi varsa
    # merkezi motor atar, yoksa KENDİ kuyruğunda bekler (GENEL'e düşmez).
    if ticket["status"] == config.STATUS_ESCALATION_PENDING:
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))
        _notify_auto_assignment(decision)
        if decision.get("assigned"):
            flash(f"Talep #{ticket_id} yönlendirildi ve "
                  f"{decision['technician']['name']} adlı teknisyene atandı.",
                  "success")
            return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    flash(f"Talep #{ticket_id} yönlendirmesi güncellendi: "
          f"{routing.basis_label(basis)} — {target}", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Ekip yönetimi (yalnızca admin) ----------------

@app.route("/ekip")
@auth.admin_required
def team():
    # Tek merkezi tanım: available (yeni iş almaya açık mı) ile
    # active_ticket_count (üzerindeki kapanmamış talep sayısı) AYRI kavramlar.
    # list_technicians_with_skills: sabit sayıda sorgu, N+1 yok.
    technicians = db.list_technicians_with_skills()
    return render_template(
        "team.html",
        technicians=technicians,
        departments=config.DEPARTMENTS,
        active_skills=db.list_skills(only_active=True),
        priorities=db.VALID_PRIORITIES,
        suggest_username=auth.suggest_username_for,
        password_min=config.PASSWORD_MIN_LENGTH,
        web_users={u["technician_id"]: u for u in db.list_web_users() if u["technician_id"]},
    )


@app.route("/ekip/ekle", methods=["POST"])
@auth.admin_required
def team_add():
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    department = request.form.get("department", "")
    telegram_id_raw = request.form.get("telegram_id", "").strip()

    if not name or department not in config.DEPARTMENTS:
        flash("İsim ve geçerli bir departman gerekli.", "error")
        return redirect(url_for("team"))

    if not consume_form_token():
        flash("Bu ekip üyesi zaten eklendi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("team"))

    telegram_id = int(telegram_id_raw) if telegram_id_raw.isdigit() else None
    db.create_technician(name, telegram_id, department, role or None)
    # Web hesabı OTOMATİK açılmaz: ortak parola kullanılmaması için admin,
    # kullanıcı adını ve geçici parolayı ekip sayfasından tek tek belirler.
    flash(
        f"{name} ekibe eklendi. Panele girebilmesi için 'Web hesabı oluştur' "
        "ile kullanıcı adı ve geçici şifre belirleyin.",
        "success",
    )
    return redirect(url_for("team"))


@app.route("/ekip/<int:technician_id>/hesap", methods=["POST"])
@auth.admin_required
def team_create_account(technician_id):
    """Admin, teknisyen için kullanıcı adı + GEÇİCİ parola belirler.

    Parola yalnızca hash olarak saklanır; hiçbir yerde geri gösterilmez ve
    loglanmaz. Hesap, ilk girişte parola değiştirme zorunluluğuyla açılır."""
    if not consume_form_token():
        flash("Bu hesap zaten oluşturuldu; işlem tekrarlanmadı.", "error")
        return redirect(url_for("team"))

    ok, message = auth.create_technician_web_user(
        technician_id,
        request.form.get("username", ""),
        request.form.get("temp_password", ""),
    )
    flash(message, "success" if ok else "error")
    return redirect(url_for("team"))


@app.route("/ekip/<int:technician_id>/gecici-sifre", methods=["POST"])
@auth.admin_required
def team_reset_password(technician_id):
    """Admin yeni bir geçici parola atar (mevcut parolayı GÖREMEZ)."""
    web_user = db.get_web_user_for_technician(technician_id)
    if not web_user:
        flash("Bu teknisyenin web hesabı yok.", "error")
        return redirect(url_for("team"))

    if not consume_form_token():
        flash("Bu şifre zaten sıfırlandı; işlem tekrarlanmadı.", "error")
        return redirect(url_for("team"))

    ok, message = auth.set_temporary_password(
        web_user["id"], request.form.get("temp_password", "")
    )
    flash(message, "success" if ok else "error")
    return redirect(url_for("team"))


@app.route("/ekip/<int:technician_id>/toggle", methods=["POST"])
@auth.admin_required
def team_toggle(technician_id):
    """Admin bir teknisyenin müsaitliğini açar/kapatır.

    ACCEPTANCE FIX 2: müsait yapıldığında eskiden departmandaki EN ESKİ
    bekleyen talep DOĞRUDAN bu kişiye atanıyordu. O yol kaldırıldı; artık
    kuyruk merkezi motorla yeniden değerlendirilir:

        required_skill_code varsa -> skill selector
        yoksa departman varsa     -> departman fallback selector

    DİKKAT: müsait yapılan kişi otomatik olarak atanan kişi DEĞİLDİR. Daha az
    yüklü ya da daha uzun süredir iş almamış bir meslektaşı seçilebilir; bu
    kasıtlıdır ve adaletin gereğidir."""
    technician = db.get_technician(technician_id)
    if not technician:
        flash("Teknisyen bulunamadı.", "error")
        return redirect(url_for("team"))

    if not consume_form_token():
        flash(f"{technician['name']} için durum zaten değiştirildi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("team"))

    new_available = not technician["available"]
    db.set_technician_availability(technician_id, new_available)

    if not new_available:
        flash(f"{technician['name']} meşgul olarak işaretlendi.", "success")
        return redirect(url_for("team"))

    # Kişinin uzmanlıklarını VE departmanını bekleyen kuyruklar taranır.
    assigned = []
    for decision in queue_service.reevaluate_for_technician(technician_id):
        if not decision.get("assigned"):
            continue
        _notify_auto_assignment(decision)
        assigned.append((decision["ticket_id"], decision["technician"]["name"]))

    if not assigned:
        flash(f"{technician['name']} müsait olarak işaretlendi.", "success")
    else:
        details = ", ".join(f"#{ticket_id} → {name}" for ticket_id, name in assigned)
        flash(f"{technician['name']} müsait yapıldı. Kuyruk yeniden değerlendirildi "
              f"ve şu talep(ler) atandı: {details}.", "success")
    return redirect(url_for("team"))


# ---------------- Teknisyenin kendi müsaitliği ----------------

# ---------------- Uzmanlık kataloğu (yalnızca admin) ----------------

@app.route("/uzmanliklar")
@auth.admin_required
def skills():
    return render_template("skills.html", skills=db.list_skills())


@app.route("/uzmanliklar/ekle", methods=["POST"])
@auth.admin_required
def skill_add():
    if not consume_form_token():
        flash("Bu uzmanlık zaten eklendi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("skills"))

    skill_id, error = db.create_skill(
        request.form.get("code", ""), request.form.get("name", "")
    )
    if error:
        flash(error, "error")
    else:
        flash(f"'{db.get_skill(skill_id)['code']}' uzmanlığı oluşturuldu.", "success")
    return redirect(url_for("skills"))


@app.route("/uzmanliklar/<int:skill_id>/durum", methods=["POST"])
@auth.admin_required
def skill_toggle(skill_id):
    """Uzmanlık SİLİNMEZ, pasifleştirilir — geçmiş atamalar bozulmasın."""
    skill = db.get_skill(skill_id)
    if not skill:
        flash("Uzmanlık bulunamadı.", "error")
        return redirect(url_for("skills"))

    if not consume_form_token():
        flash("Bu uzmanlığın durumu zaten değiştirildi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("skills"))

    new_active = not skill["active"]
    db.set_skill_active(skill_id, new_active)
    flash(f"{skill['name']} {'aktif' if new_active else 'pasif'} yapıldı.", "success")
    return redirect(url_for("skills"))


# ---------------- Teknisyen uzmanlıkları (yalnızca admin) ----------------

@app.route("/ekip/<int:technician_id>/uzmanlik", methods=["POST"])
@auth.admin_required
def technician_skill_set(technician_id):
    """Teknisyenin 1./2./3. uzmanlığını belirler.

    YETKİ: yalnızca admin. Teknisyen kendi ya da başkasının uzmanlığını
    değiştiremez — @auth.admin_required bunu backend'de engeller.
    Formdan gelen priority ve skill_id değerlerine GÜVENİLMEZ; db katmanı
    ayrıca doğrular (geçerli öncelik, var olan ve AKTİF skill)."""
    if not consume_form_token():
        flash("Bu uzmanlık zaten kaydedildi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("team"))

    raw_priority = request.form.get("priority", "")
    raw_skill = request.form.get("skill_id", "")

    if not raw_priority.isdigit():
        flash("Geçersiz öncelik.", "error")
        return redirect(url_for("team"))
    priority = int(raw_priority)

    # Boş seçim = bu önceliği kaldır
    if raw_skill == "":
        ok, message = db.remove_technician_skill(technician_id, priority)
    elif raw_skill.isdigit():
        ok, message = db.set_technician_skill(technician_id, int(raw_skill), priority)
    else:
        ok, message = False, "Geçersiz uzmanlık."

    flash(message, "success" if ok else "error")
    return redirect(url_for("team"))


# ---------------- Faz 6B: SLA takvimi / tatiller (yalnızca admin) ----------------

@app.route("/sla-takvimi")
@auth.admin_required
def sla_calendar_page():
    """Tatil yönetimi. Teknisyen bu sayfaya ERİŞEMEZ (admin_required)."""
    return render_template(
        "sla_calendar.html",
        holidays=db.list_holidays(),
        business_hours=config.BUSINESS_HOURS,
        timezone_name=config.SLA_TIMEZONE,
        calendar_status=sla_calendar.calendar_status(),
    )


@app.route("/sla-takvimi/ekle", methods=["POST"])
@auth.admin_required
def holiday_add():
    if not consume_form_token():
        flash("Bu tatil zaten eklendi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("sla_calendar_page"))

    holiday_id, error = db.create_holiday(
        request.form.get("holiday_date", ""), request.form.get("name", ""))
    if error:
        flash(error, "error")
    else:
        flash("Tatil eklendi. SLA sayacı o gün işlemeyecek.", "success")
    return redirect(url_for("sla_calendar_page"))


@app.route("/sla-takvimi/<int:holiday_id>/durum", methods=["POST"])
@auth.admin_required
def holiday_toggle(holiday_id):
    """Tatil SİLİNMEZ, pasifleştirilir — geçmiş hesaplar izlenebilir kalır."""
    holiday = db.get_holiday(holiday_id)
    if not holiday:
        flash("Tatil bulunamadı.", "error")
        return redirect(url_for("sla_calendar_page"))
    if not consume_form_token():
        flash("Bu tatilin durumu zaten değiştirildi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("sla_calendar_page"))

    new_active = not holiday["active"]
    db.set_holiday_active(holiday_id, new_active)
    flash(f"{holiday['name']} {'aktif' if new_active else 'pasif'} yapıldı.", "success")
    return redirect(url_for("sla_calendar_page"))


# ---------------- Faz 5B: Öncelik ----------------

@app.route("/ticket/<int:ticket_id>/oncelik", methods=["POST"])
@auth.login_required
def change_priority(ticket_id):
    """Talep önceliğini değiştirir.

    YETKİ: admin veya mevcut ANA SORUMLU. Yardımcı (COLLABORATOR) önceliği
    DEĞİŞTİREMEZ — require_manage_ticket bunu backend'de engeller.

    SLA: başlangıç zamanı DEĞİŞMEZ, yalnızca son tarihler yeni politikaya göre
    yeniden hesaplanır (öncelik yükseltmek gecikmeyi gizlemez)."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))
    auth.require_manage_ticket(ticket_id)

    if not consume_form_token():
        flash("Bu öncelik zaten güncellendi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    # PRIORITY ROUTING REVISION §B3 — ADMIN OVERRIDE.
    # Bu yoldan gelen her değişiklik İNSAN kararıdır: priority_source 'manual'
    # olarak damgalanır ve AI bir daha üzerine YAZAMAZ
    # (bkz. db.apply_ai_priority). set_ticket_priority'nin varsayılan source'u
    # zaten 'manual'dır; niyeti gizlememek için açıkça geçiyoruz.
    priority = request.form.get("priority", "")
    ok, message = db.set_ticket_priority(
        ticket_id, priority, source=config.PRIORITY_SOURCE_MANUAL)
    if ok:
        db.add_message(
            ticket_id, "sistem", 0, "Sistem",
            f"Öncelik {config.PRIORITY_LABELS[priority]} olarak değiştirildi "
            f"({auth.current_user().full_name}).",
            message_kind=config.MESSAGE_KIND_SYSTEM,
        )
    flash(message, "success" if ok else "error")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Faz 5A: Dahili not (INTERNAL NOTE) ----------------

@app.route("/ticket/<int:ticket_id>/dahili-not", methods=["POST"])
@auth.login_required
def internal_note_add(ticket_id):
    """Yalnızca teknik ekibin gördüğü dahili not ekler.

    ⚠️ TELEGRAM İZOLASYONU: bu fonksiyon send_telegram_message() ÇAĞIRMAZ ve
    delivery_status yazmaz. Kullanıcıya giden yol (reply) tamamen AYRI bir
    rotadır; buradan oraya hiçbir dallanma yoktur. Bir boolean hatasıyla
    kullanıcıya gitme riski bu yüzden yapısal olarak yoktur.

    YETKİ: authorize_ticket -> admin, ana sorumlu veya AKTİF yardımcı.
    İlgisiz ya da görevden çıkarılmış teknisyen 403 alır.
    Not yazmak durum değiştirme yetkisi VERMEZ."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))

    note_text = request.form.get("note", "").strip()
    if not note_text:
        flash("Boş dahili not kaydedilemez.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if not consume_form_token():
        flash("Bu not zaten eklendi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    user = auth.current_user()
    db.add_internal_note(
        ticket_id,
        "yonetici" if user.is_admin else "teknisyen",
        user.full_name, note_text, sender_user_id=user.id,
    )
    flash("Dahili not eklendi. Bu not talep sahibine gönderilmez.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Faz 5A: AI ticket özeti ----------------

@app.route("/ticket/<int:ticket_id>/ozet", methods=["POST"])
@auth.login_required
def ai_summary_generate(ticket_id):
    """Talep geçmişini teknik ekip için özetler.

    OTOMATİK ÇALIŞMAZ — yalnızca butona basıldığında. Özet:
      - talebin durumunu DEĞİŞTİRMEZ
      - ai_mode'a DOKUNMAZ
      - Telegram kullanıcısına HİÇBİR ŞEY göndermez
    Gemini başarısız olursa mevcut özet KORUNUR."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))

    if not consume_form_token():
        flash("Özet zaten oluşturuldu; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    user = auth.current_user()
    summary = ai_assistant.summarize_ticket(
        ticket["subject"], db.get_summary_context(ticket_id)
    )
    if summary is None:
        # Hata detayı/anahtar panele SIZDIRILMAZ; ticket ve eski özet sağlam kalır.
        app.logger.warning("Talep #%s AI ozeti uretilemedi.", ticket_id)
        flash("AI özeti şu anda oluşturulamadı. Talep ve mevcut özet etkilenmedi.",
              "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    db.save_ai_summary(ticket_id, summary, generated_by_user_id=user.id)
    flash("AI özeti oluşturuldu.", "success")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Faz 4: Yardımcı teknik personel (COLLABORATOR) ----------------

@app.route("/ticket/<int:ticket_id>/yardimci", methods=["POST"])
@auth.login_required
def collaborator_add(ticket_id):
    """Talebe yardımcı ekler.

    YETKİ: admin veya mevcut ANA SORUMLU. Yardımcı başka yardımcı ekleyemez —
    require_manage_ticket bunu backend'de engeller."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))
    auth.require_manage_ticket(ticket_id)

    if not consume_form_token():
        flash("Bu yardımcı zaten eklendi; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    raw = request.form.get("technician_id", "")
    if not raw.isdigit():
        flash("Geçerli bir teknik personel seçin.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    user = auth.current_user()
    ok, message = db.add_collaborator(
        ticket_id, int(raw),
        actor={"user_id": user.id, "source": config.ASSIGN_SOURCE_WEB,
               "label": user.full_name},
    )
    if ok:
        technician = db.get_technician(int(raw))
        db.add_message(
            ticket_id, "sistem", 0, "Sistem",
            f"{technician['name']} talebe yardımcı teknik personel olarak eklendi.",
            message_kind=config.MESSAGE_KIND_SYSTEM,
        )
        # Bildirim başarısızlığı ATAMAYI GERİ ALMAZ, sadece loglanır/flash'lanır.
        if technician.get("telegram_id"):
            primary = ticket.get("technician_name") or "—"
            sent, error = send_telegram_message(
                technician["telegram_id"],
                (f"🤝 Yeni bir talebe yardımcı teknik personel olarak eklendiniz.\n\n"
                 f"Talep #{ticket_id}\nAna sorumlu: {primary}\n"
                 f"Konu: {ticket['subject']}\n\nGeçmişin tamamı web panelinde."),
            )
            if not sent:
                app.logger.warning(
                    "Talep #%s yardimci bildirimi gonderilemedi: %s", ticket_id, error)
                message += " (Telegram bildirimi iletilemedi.)"
    flash(message, "success" if ok else "error")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.route("/ticket/<int:ticket_id>/yardimci/<int:technician_id>/kaldir", methods=["POST"])
@auth.login_required
def collaborator_remove(ticket_id, technician_id):
    """Yardımcıyı görevden çıkarır (kayıt SİLİNMEZ, removed_at doldurulur).

    YETKİ: admin veya mevcut ANA SORUMLU. Yardımcı kendini de başkasını da
    kaldıramaz."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))
    auth.require_manage_ticket(ticket_id)

    if not consume_form_token():
        flash("Bu işlem zaten yapıldı; tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    ok, message = db.remove_collaborator(
        ticket_id, technician_id,
        reason=(request.form.get("reason") or "").strip() or None,
    )
    if ok:
        technician = db.get_technician(technician_id)
        db.add_message(
            ticket_id, "sistem", 0, "Sistem",
            f"{technician['name']} yardımcı teknik personel görevinden çıkarıldı.",
            message_kind=config.MESSAGE_KIND_SYSTEM,
        )
    flash(message, "success" if ok else "error")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Faz 4: Ana sorumluluğu devretme ----------------

@app.route("/ticket/<int:ticket_id>/devret", methods=["POST"])
@auth.login_required
def transfer_primary(ticket_id):
    """Ownership transfer — 'Yardımcı Ekle' DEĞİLDİR.

    YETKİ: admin veya mevcut ANA SORUMLU. Gerekçe ZORUNLUDUR.
    Eski ana sorumlu OTOMATİK yardımcı YAPILMAZ."""
    ticket = auth.authorize_ticket(ticket_id)
    if not ticket:
        flash("Talep bulunamadı.", "error")
        return redirect(url_for("index"))
    auth.require_manage_ticket(ticket_id)

    raw = request.form.get("technician_id", "")
    reason = (request.form.get("reason") or "").strip()

    if not raw.isdigit():
        flash("Geçerli bir teknik personel seçin.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    if len(reason) < config.TRANSFER_REASON_MIN_LENGTH:
        flash(f"Devir gerekçesi zorunludur (en az {config.TRANSFER_REASON_MIN_LENGTH} karakter).",
              "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    if not consume_form_token():
        flash("Bu devir zaten yapıldı; işlem tekrarlanmadı.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    user = auth.current_user()
    # Ana sorumlu adı GERÇEK ilişkiden (assigned_technician_id -> technicians.name)
    # gelir; assigned_admin_name yalnızca Faz 4 öncesi kayıtlar için legacy
    # fallback'tir ve buraya YENİ DEĞER YAZILMAZ.
    old_name = ticket.get("technician_name") or legacy_primary_name(ticket) or "—"
    ok, message = db.transfer_primary(
        ticket_id, int(raw), ticket.get("assigned_technician_id"), reason,
        actor={"user_id": user.id, "source": config.ASSIGN_SOURCE_WEB,
               "label": user.full_name},
    )
    if ok:
        technician = db.get_technician(int(raw))
        db.add_message(
            ticket_id, "sistem", 0, "Sistem",
            f"Ana sorumluluk {old_name} kişisinden {technician['name']} kişisine "
            f"devredildi. Sebep: {reason}",
            message_kind=config.MESSAGE_KIND_SYSTEM,
        )
        # Ticket ID ve konuşma DEĞİŞMEZ; AI yeniden devreye girmez.
        send_telegram_message(
            ticket["user_id"],
            f"ℹ️ Talebinizin ana sorumlusu güncellendi.\nYeni sorumlu: {technician['name']}",
        )
        if technician.get("telegram_id"):
            sent, error = send_telegram_message(
                technician["telegram_id"],
                (f"🔁 Talep #{ticket_id} ana sorumluluğu size devredildi.\n\n"
                 f"Konu: {ticket['subject']}\nSebep: {reason}\n\n"
                 "Geçmişin tamamı web panelinde."),
            )
            if not sent:
                app.logger.warning(
                    "Talep #%s devir bildirimi gonderilemedi: %s", ticket_id, error)
    flash(message, "success" if ok else "error")
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


# ---------------- Faz 4: Yardımcı Olduklarım ----------------

@app.route("/collaborations")
@auth.technician_required
def my_collaborations():
    """Teknisyenin YARDIMCI olduğu talepler.

    Ana sorumlu olduğu talepler bu listede GÖRÜNMEZ (PRIMARY aynı anda
    COLLABORATOR olamaz)."""
    user = auth.current_user()
    group = request.args.get("status") or "active"
    if group not in ("active", "waiting", "resolved", "all"):
        group = "active"

    if user.technician_id is None:
        return render_template("my_collaborations.html", tickets=[], group=group)

    tickets = db.list_collaborator_tickets(
        user.technician_id, None if group == "all" else group
    )
    for t in tickets:
        t["sla"] = db.get_sla_state(t)
    return render_template("my_collaborations.html", tickets=tickets, group=group)


def _notify_auto_assignment(decision: dict):
    """Kuyruktan OTOMATİK atanan talebi ilgili taraflara bildirir.

    Bildirim hatası ATAMAYI GERİ ALMAZ — atama gerçek bir iş dağıtımıdır;
    Telegram'ın anlık hatası yüzünden teknisyenin işi elinden alınmaz."""
    if not decision.get("assigned"):
        return
    ticket = db.get_ticket(decision["ticket_id"])
    technician = decision.get("technician")
    if not ticket or not technician:
        return

    # Kullanıcıya YALNIZCA sonuç bildirilir; iç operasyon detayı gösterilmez.
    ok, error = send_telegram_message(
        ticket["user_id"],
        queue_service.assignment_text(ticket, decision["result"], technician),
    )
    if not ok:
        app.logger.warning("Talep #%s atama bildirimi gonderilemedi: %s",
                           ticket["id"], error)
    if technician.get("telegram_id"):
        send_telegram_message(
            technician["telegram_id"],
            queue_service.technician_notification_text(ticket, decision["result"]),
        )


@app.route("/musaitlik", methods=["POST"])
@auth.technician_required
def toggle_own_availability():
    user = auth.current_user()
    if user.technician_id is None:
        abort(403)
    if not consume_form_token():
        return redirect(url_for("my_tickets"))
    technician = db.get_technician(user.technician_id)
    if technician:
        becoming_available = not technician["available"]
        db.set_technician_availability(user.technician_id, becoming_available)
        if becoming_available:
            # Yalnızca BU kişinin uzmanlıklarını bekleyen talepler taranır.
            # ACCEPTANCE REVISION: kuyruk artık kullanıcıya seçim sunmaz; merkezi
            # motor en uygun kişiyi seçer ve atomik PRIMARY ataması yapılır.
            # DİKKAT: müsait olan kişi otomatik olarak SEÇİLEN kişi değildir —
            # sıralamayı routing.py belirler.
            for decision in queue_service.reevaluate_for_technician(user.technician_id):
                _notify_auto_assignment(decision)
    return redirect(url_for("my_tickets"))


def bootstrap():
    """Uygulama açılışı: şema + ilk admin + eski ortak parolaların iptali."""
    db.init_db()
    auth.bootstrap_admin()
    # Başlangıç uzmanlık kataloğu (idempotent). Teknisyenlere OTOMATİK skill
    # atanmaz — uzmanlıkları admin belirler.
    created_skills = db.seed_default_skills()
    if created_skills:
        print(f"[kurulum] {created_skills} yeni uzmanlik katalogu kaydi olusturuldu.")
    revoked = auth.revoke_shared_password_accounts()
    if revoked:
        print(f"[guvenlik] Ortak parolayla acilmis {revoked} teknisyen hesabinin "
              "parolasi iptal edildi. Ekip sayfasindan yeni gecici parola verin.")
    if config.WEB_SECRET_KEY_IS_EPHEMERAL:
        print("[uyari] WEB_SECRET_KEY tanimli degil; gecici anahtar uretildi. "
              "Sunucu yeniden baslayinca oturumlar dusecek (.env'e ekleyin).")


if __name__ == "__main__":
    bootstrap()
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)
