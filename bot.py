import asyncio
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
import db
import ai_assistant
import auth
import routing
import queue_service
import telegram_ui

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx her HTTP isteğini (getUpdates dahil) INFO seviyesinde loglar,
# bu da terminali her ~10 saniyede bir "200 OK" satırıyla doldurur.
# Bunu sadece uyarı ve üzeri seviyeye çekerek gereksiz gürültüyü azaltıyoruz.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def resolve_actor_for_telegram(telegram_user):
    """Telegram'da islem yapan kisiyi mumkunse bir web_users kaydiyla eslestirir.

    Secenek A (tercih edilen): kisi bir teknisyense ve web hesabi varsa gercek
    web_users.id dondurulur. Secenek B (yedek): eslesme yoksa kanal + dis kimlik
    + ad, tickets tablosundaki denetim alanlarina yazilir. Boylece
    "bu talebi kim, hangi kanaldan cozdu?" sorusu cevapsiz kalmaz."""
    web_user_id = None
    technician = db.get_technician_by_telegram_id(telegram_user.id)
    if technician:
        web_user = db.get_web_user_for_technician(technician["id"])
        if web_user:
            web_user_id = web_user["id"]
    return {
        "web_user_id": web_user_id,
        "source": config.RESOLVED_SOURCE_TELEGRAM,
        "external_id": telegram_user.id,
        "label": telegram_user.full_name,
    }


def primary_assignee_name(ticket: dict):
    """Talebin ana sorumlusunun adi.

    Faz 2.5: once GERCEK iliski (assigned_technician_id -> technicians.name)
    kullanilir. tickets.assigned_admin_name yalnizca eski kayitlar icin
    LEGACY FALLBACK'tir; yeni is mantiginda kimlik kaynagi degildir."""
    tech_id = ticket.get("assigned_technician_id")
    if tech_id:
        technician = db.get_technician(tech_id)
        if technician:
            return technician["name"]
    return ticket.get("assigned_admin_name")   # legacy fallback


def is_human_assisted(ticket: dict) -> bool:
    """Talep insan desteğine geçti mi?

    Ayrı bir kolon üretmiyoruz: mevcut veri modelinde 'escalated' bayrağı ya da
    atanmış bir teknisyen olması, mantıksal olarak AI mode = HUMAN_ASSISTED
    demektir. Bu durumda Gemini kullanıcıya OTOMATİK CEVAP VERMEZ; mesaj yalnızca
    talep geçmişine eklenir ve sorumlu teknisyene gösterilir."""
    mode = ticket.get("ai_mode")
    if mode == config.AI_MODE_HUMAN:
        return True
    if mode == config.AI_MODE_HANDLING:
        # AI yürütüyor; yine de atanmış bir teknisyen varsa insan devralmıştır.
        return ticket.get("assigned_technician_id") is not None
    # Faz 3B öncesi kayıtlar için eski türetilmiş kontrol.
    return bool(ticket.get("escalated")) or ticket.get("assigned_technician_id") is not None


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """§14: KALICI ana menü. Slash komut ezberi gerektirmeyen birincil UX.

    Buton metinleri telegram_ui'de tanımlıdır; burada yalnızca Telegram
    nesnesine sarılır (tek kaynak, iki temsil değil).

    resize_keyboard=True -> klavye tuş sayısına göre küçülür, sohbetin
    yarısını kaplamaz. one_time_keyboard KULLANILMAZ (§21): kullanıcı bir
    talebin ortasında serbest metin yazarken menünün kaybolup geri
    getirilememesi en sık görülen Telegram UX hatasıdır. Menü sürekli
    erişilebilir kalır; serbest metin yazmayı ENGELLEMEZ.

    §22: bu ReplyKeyboard NAVIGASYON içindir. Ticket'a özel aksiyonlar
    (Çözüldü / Devam ediyor / Teknik destek istiyorum) InlineKeyboard olarak
    KALIR; iki klavye türü birbirinin yerine geçmez."""
    return ReplyKeyboardMarkup(
        telegram_ui.MAIN_MENU_ROWS,
        resize_keyboard=True,
        input_field_placeholder="Sorununuzu yazın veya menüden seçin",
    )


def solution_choice_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """AI yanıtının altına iliştirilen üç seçenek.

    Kullanıcı üç denemeyi beklemek zorunda DEĞİLDİR; istediği anda
    'Teknik destek istiyorum' ile insana devredebilir."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Çözüldü", callback_data=f"solved:{ticket_id}")],
        [InlineKeyboardButton("🔄 Devam ediyor", callback_data=f"cont:{ticket_id}")],
        [InlineKeyboardButton("👨‍🔧 Teknik destek istiyorum", callback_data=f"esc:{ticket_id}")],
    ])


def escalate_only_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Çözüm denemesi hakkı bittiğinde gösterilir."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Çözüldü", callback_data=f"solved:{ticket_id}")],
        [InlineKeyboardButton("👨‍🔧 Teknik destek istiyorum", callback_data=f"esc:{ticket_id}")],
    ])


# ACCEPTANCE REVISION: candidate_keyboard / single_candidate_keyboard KALDIRILDI.
# Son kullanıcıya teknik personel SEÇTİRİLMEZ; routing ve yük dağıtımı sistemin
# sorumluluğudur. Yeni mesajlarda 'tech:<ticket>:<technician>' callback'i
# ÜRETİLMEZ. Daha önce gönderilmiş eski mesajlardaki butonlar için güvenli bir
# no-op yanıt vardır (bkz. handle_stale_candidate_callback).


def reopen_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Çözülmüş talep bildirimine iliştirilir.

    Yeniden açma YALNIZCA bu açık aksiyonla olur — kullanıcının serbest metnini
    LLM ile yorumlayıp 'herhalde aynı sorun' diye tahmin etmiyoruz."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Sorun hâlâ devam ediyor", callback_data=f"reopen:{ticket_id}"),
    ]])


async def notify_assigned_technician(context: ContextTypes.DEFAULT_TYPE, ticket: dict, text: str):
    """Talebe atanmış teknisyene (Telegram ID'si varsa) haber verir."""
    tech_id = ticket.get("assigned_technician_id")
    if not tech_id:
        return
    technician = db.get_technician(tech_id)
    if technician and technician.get("telegram_id"):
        try:
            await context.bot.send_message(chat_id=technician["telegram_id"], text=text)
        except Exception as e:
            logger.warning(f"Teknisyene bildirim gönderilemedi: {e}")


def ticket_summary_text(ticket: dict) -> str:
    status_label = config.STATUS_LABELS.get(ticket["status"], ticket["status"])
    lines = [
        f"🎫 Talep #{ticket['id']}",
        f"Durum: {status_label}",
        f"Gönderen: {ticket['full_name']} (@{ticket['username'] or '-'})",
        f"Konu: {ticket['subject']}",
    ]
    if ticket.get("category"):
        lines.append(f"Departman: {ticket['category']}")
    sorumlu = primary_assignee_name(ticket)
    if sorumlu:
        lines.append(f"Sorumlu: {sorumlu}")
    lines.append(f"Oluşturulma: {ticket['created_at']}")
    return "\n".join(lines)


async def notify_admins_new_ticket(context: ContextTypes.DEFAULT_TYPE, ticket: dict):
    text = "🆕 Yeni destek talebi!\n\n" + ticket_summary_text(ticket)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Üstlen", callback_data=f"assign:{ticket['id']}"),
                InlineKeyboardButton("🔒 Kapat", callback_data=f"close:{ticket['id']}"),
            ]
        ]
    )
    targets = [config.ADMIN_GROUP_ID] if config.ADMIN_GROUP_ID else list(config.ADMIN_IDS)
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Bildirim gönderilemedi ({chat_id}): {e}")


async def notify_user_status_change(context: ContextTypes.DEFAULT_TYPE, ticket: dict, extra_note: str = ""):
    # §18-§19: kullanıcıya `#42` değil `🎫 Talep 42` gösterilir; durum adı da
    # panel dili yerine kullanıcı dilindedir (telegram_ui tek merkez).
    text = telegram_ui.status_change_notice(
        ticket["id"], ticket["status"], extra_note)
    try:
        await context.bot.send_message(chat_id=ticket["user_id"], text=text)
    except Exception as e:
        logger.warning(f"Kullanıcıya bildirim gönderilemedi: {e}")


# ---------------- Kullanıcı komutları ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """§14: /start sonrası KALICI ReplyKeyboard gönderilir."""
    await update.message.reply_text(
        telegram_ui.WELCOME_TEXT, reply_markup=main_menu_keyboard())


# ---------------- Ana menü aksiyonları (§16, §17) ----------------
#
# Buton ve slash komut AYNI iş mantığını paylaşır: aşağıdaki üç helper TEK
# uygulamadır, komut handler'ları da menü handler'ları da bunları çağırır.
# Aynı kod iki kez YAZILMAZ (§16).

async def open_new_ticket_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'🆕 Yeni Talep' ve argümansız /istek için ORTAK akış.

    §23: SAME-TICKET politikası korunur. Kullanıcının devam eden bir talebi
    varsa yeni kayıt AÇILMAZ; mevcut talebi hatırlatılır ve yazdıkları o
    talebe eklenmeye devam eder."""
    existing = db.get_open_ticket_for_user(update.effective_user.id)
    if existing:
        await update.message.reply_text(
            telegram_ui.existing_ticket_notice(existing),
            reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text(
        telegram_ui.NEW_TICKET_PROMPT, reply_markup=main_menu_keyboard())


async def show_my_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'📋 Taleplerim' ve /taleplerim için ORTAK akış (§17, §20)."""
    tickets = db.list_tickets_for_user(update.effective_user.id)

    # Uzman adları TEK seferde toplanır — liste uzadıkça talep başına sorgu
    # açılmaz (N+1 yok). Ad zaten sorguda geliyorsa DB'ye hiç gidilmez.
    assignee_names = {}
    seen = {}
    for t in tickets[:telegram_ui.MY_TICKETS_LIMIT]:
        tech_id = t.get("assigned_technician_id")
        if not tech_id:
            continue
        if tech_id not in seen:
            technician = db.get_technician(tech_id)
            seen[tech_id] = technician["name"] if technician else None
        if seen[tech_id]:
            assignee_names[t["id"]] = seen[tech_id]

    await update.message.reply_text(
        telegram_ui.my_tickets_text(tickets, assignee_names),
        reply_markup=main_menu_keyboard())


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'❓ Yardım' ve /yardim için ORTAK akış (§15)."""
    await update.message.reply_text(
        telegram_ui.HELP_TEXT, reply_markup=main_menu_keyboard())


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             text: str) -> bool:
    """Ana menü butonuna basıldıysa ilgili akışı çalıştırır ve True döner.

    Telegram, ReplyKeyboard butonunu NORMAL METİN olarak gönderir. Bu yüzden
    metin akışının EN BAŞINDA ayrıştırılmalıdır — aksi hâlde '📋 Taleplerim'
    yazısı yeni bir destek talebi olarak kaydedilirdi."""
    text = (text or "").strip()
    if text == telegram_ui.BTN_NEW_TICKET:
        await open_new_ticket_flow(update, context)
        return True
    if text == telegram_ui.BTN_MY_TICKETS:
        await show_my_tickets(update, context)
        return True
    if text == telegram_ui.BTN_HELP:
        await show_help(update, context)
        return True
    return False


async def route_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Kullanıcıdan gelen her metnin tek giriş noktası.

    Hem /istek komutu hem de düz metin mesajları buradan geçer: kullanıcının açık bir
    talebi varsa mesaj o talebe eklenir, yoksa yeni talep açılır. Böylece /istek, devam
    eden bir sohbeti bölüp ikinci bir talep açmaz.
    """
    user = update.effective_user
    existing = db.get_open_ticket_for_user(user.id)
    if existing:
        await append_to_open_ticket(update, context, existing, text)
    else:
        await handle_new_ticket_text(update, context, text)


async def create_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """§15: /istek KALDIRILMADI — geriye dönük uyumluluk için çalışmaya devam eder.

    Argümansız kullanımda '🆕 Yeni Talep' butonuyla AYNI helper'a düşer
    (open_new_ticket_flow); iş mantığı iki kez yazılmaz."""
    if not context.args:
        await open_new_ticket_flow(update, context)
        return
    await route_user_message(update, context, " ".join(context.args))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """§15: /yardim — '❓ Yardım' butonuyla aynı helper."""
    await show_help(update, context)


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # §14: ReplyKeyboard butonları düz metin olarak gelir. Talep açma/ekleme
    # akışından ÖNCE ayrıştırılmalıdır, yoksa buton yazısı talep konusu olur.
    # Admin kontrolünden de önce gelir: yönetici de bu butonları kullanabilir.
    if await handle_menu_button(update, context, update.message.text):
        return

    if is_admin(user.id):
        await update.message.reply_text(
            "Yönetici komutları:\n"
            "/liste [acik|islemde|kapatildi]\n"
            "/detay <id>\n"
            "/ata <id>\n"
            "/yanitla <id> <mesaj>\n"
            "/kapat <id>\n\n"
            "Teknik ekip:\n"
            "/ekip — listele\n"
            "/ekibeekle <isim> <departman> [telegram_id]\n"
            "/musaitim, /mesgulum — teknisyenin kendi müsaitlik durumu\n\n"
            "Kullanıcı komutları: /istek, /taleplerim, /yardim"
        )
        return

    await route_user_message(update, context, update.message.text)


async def append_to_open_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket: dict, text: str):
    """Kullanıcının hâlihazırda açık olan talebine yeni mesajı ekler.

    KRİTİK: burada asla yeni talep açılmaz. Bir problem çözülene/kapatılana
    kadar bütün mesajlar aynı ticket_id altında birikir.

    Kullanıcının ikinci mesaj yazması OTOMATİK ESKALASYON DEĞİLDİR; AI
    gerekirse açıklayıcı soru sorar, gerekirse yeni bir çözüm dener."""
    user = update.effective_user
    ticket_id = ticket["id"]
    db.add_message(ticket_id, "kullanici", user.id, user.full_name, text,
                   message_kind=config.MESSAGE_KIND_NORMAL)

    # Faz 6A: kullanıcı beklenirken gelen PUBLIC mesaj SLA'yı devam ettirir.
    # Talep çalışma durumuna döner; duraklama süresi son tarihlere eklenir.
    # (Dahili not / sistem mesajı / AI özeti bunu TETİKLEMEZ.)
    if ticket.get("status") == config.STATUS_WAITING_USER:
        if config.can_transition(config.STATUS_WAITING_USER, config.STATUS_IN_PROGRESS):
            db.set_ticket_status(ticket_id, config.STATUS_IN_PROGRESS)
            ticket = db.get_ticket(ticket_id)

    if is_human_assisted(ticket):
        # HUMAN_ASSISTED: Gemini devrede değil. Mesaj geçmişe eklendi ve sorumluya iletilir.
        sorumlu = primary_assignee_name(ticket)
        await update.message.reply_text(
            telegram_ui.message_added_notice(ticket_id, sorumlu))
        await notify_assigned_technician(
            context, ticket,
            f"💬 Talep #{ticket_id} — {user.full_name} yeni mesaj yazdı:\n{text}",
        )
        targets = [config.ADMIN_GROUP_ID] if config.ADMIN_GROUP_ID else list(config.ADMIN_IDS)
        for chat_id in targets:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"💬 Talep #{ticket_id} için yeni mesaj:\n{text}",
                )
            except Exception:
                pass
        return

    # AI hâlâ yürütüyor: yeni bilgiyle bir tur daha.
    await run_ai_turn(context, ticket_id, user.id)


async def escalate_ticket(context: ContextTypes.DEFAULT_TYPE, ticket_id: int, department: str):
    """DEPARTMAN ROUTING (§5) — uzmanlık üretilemedi ama departman GEÇERLİ.

    ACCEPTANCE FIX 2: bu yol eskiden db.find_available_technician() ile
    "departmandaki İLK müsait kişi"yi seçiyordu. Artık merkezi motor
    (routing.select_best_technician_for_department) kullanılıyor:

        department -> available -> active_ticket_count ASC
                   -> last_primary_assignment_at ASC -> technician_id ASC

    Skill priority yoktur (required_skill üretilememiştir); iş yükü ve
    least-recently-assigned kuralları skill yoluyla AYNIDIR.

    §5 + §12 — DEPARTMAN + 0 ADAY = DEPARTMAN KUYRUĞU:
    Uygun kimse yoksa talep KAYBOLMAZ ve GENEL DESTEĞE DÜŞMEZ. Kendi
    departman kuyruğunda bekler; biri müsait olduğunda queue worker aynı
    merkezi motorla atar. 'Bu departmanda kimse yok, öyleyse genel destek
    baksın' YOLU KODDA YOKTUR."""
    db.mark_escalated(ticket_id, pending_department=department)
    db.add_message(ticket_id, "sistem", 0, "Sistem",
                   f"Talep '{department}' teknik ekibine aktarıldı.")

    result = routing.select_best_technician_for_department(department)
    selected = result.get("selected")
    ticket = db.get_ticket(ticket_id)

    if not selected:
        # DEPARTMAN KUYRUĞU: talep KAYBOLMAZ, başka bir dayanağa KAYDIRILMAZ.
        db.mark_escalation_pending(ticket_id, department=department)
        db.set_ticket_routing_basis(ticket_id, routing.BASIS_DEPARTMENT)
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=telegram_ui.queue_notice(ticket_id, department),
        )
        targets = [config.ADMIN_GROUP_ID] if config.ADMIN_GROUP_ID else list(config.ADMIN_IDS)
        for chat_id in targets:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏳ Talep #{ticket_id} '{department}' departmanına yönlendirilmek istendi "
                        f"ama şu an müsait teknisyen yok. Kuyrukta bekliyor."
                    ),
                )
            except Exception:
                pass
        return

    technician = db.get_technician(selected["technician_id"])
    if not technician:
        db.mark_escalation_pending(ticket_id, department=department)
        db.set_ticket_routing_basis(ticket_id, routing.BASIS_DEPARTMENT)
        return

    # --- ATOMİK atama: iki eş zamanlı akış iki PRIMARY üretemez ---
    if not db.assign_primary_if_unassigned(
            ticket_id, technician["id"], technician["name"],
            actor={"source": config.ASSIGN_SOURCE_SYSTEM,
                   "label": "Departman yönlendirmesi"}):
        current = db.get_ticket(ticket_id)
        current_name = primary_assignee_name(current) or "bir ekip üyesi"
        await context.bot.send_message(
            chat_id=current["user_id"],
            text=telegram_ui.already_assigned_notice(ticket_id, current_name),
        )
        return

    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"{technician['name']} talebe otomatik olarak ana sorumlu atandı. "
        f"({routing.selection_reason(result)})",
        message_kind=config.MESSAGE_KIND_SYSTEM,
    )

    ticket = db.get_ticket(ticket_id)
    # Kullanıcıya YALNIZCA sonuç: iş yükü / sıralama gerekçesi GÖSTERİLMEZ.
    await context.bot.send_message(
        chat_id=ticket["user_id"],
        text=telegram_ui.assignment_notice(ticket_id, department,
                                           technician["name"],
                                           is_department=True),
    )
    if technician.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=technician["telegram_id"],
                text="🆕 Size yeni bir talep atandı!\n\n" + ticket_summary_text(ticket),
            )
        except Exception as e:
            logger.warning(f"Teknisyene bildirim gönderilemedi: {type(e).__name__}")


async def handle_new_ticket_text(update: Update, context: ContextTypes.DEFAULT_TYPE, subject: str):
    """Yeni talep açar ve İLK AI çözüm denemesini üretir."""
    user = update.effective_user
    ticket_id = db.create_ticket(user.id, user.username, user.full_name, subject)
    db.set_ai_mode(ticket_id, config.AI_MODE_HANDLING)

    await update.message.reply_text(
        telegram_ui.ticket_created_notice(ticket_id),
        reply_markup=main_menu_keyboard())
    await run_ai_turn(context, ticket_id, user.id)

    ticket = db.get_ticket(ticket_id)
    await notify_admins_new_ticket(context, ticket)


async def run_ai_turn(context: ContextTypes.DEFAULT_TYPE, ticket_id: int, chat_id: int):
    """AI'ın sırası: açıklayıcı soru mu, yeni çözüm denemesi mi?

    ÇÖZÜM DENEMESİ SAYACI burada yönetilir:
      - clarification    -> sayaç ARTMAZ
      - solution_attempt -> sayaç ATOMİK olarak artar (claim_ai_attempt)
      - hak dolduysa      -> yeni çözüm üretilmez, eskalasyon önerilir
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return
    attempt = ticket.get("ai_attempt_count") or 0
    max_attempts = config.MAX_AI_SOLUTION_ATTEMPTS

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Hak dolduysa 4. çözümü ÜRETMEYİZ; doğrudan eskalasyon teklifi.
    if attempt >= max_attempts:
        await offer_escalation(context, ticket_id, chat_id)
        return

    history = db.get_conversation_history(ticket_id)
    # Gemini senkron bir kütüphane; event loop'u kilitlememek için ayrı thread.
    result = await asyncio.to_thread(
        ai_assistant.get_conversation_response,
        ticket["subject"], history, attempt, max_attempts,
    )

    if result is None:
        # AI kapalı / hata / bozuk JSON -> güvenli fallback.
        # Kullanıcıya ham JSON veya hata detayı GÖSTERİLMEZ.
        await context.bot.send_message(
            chat_id=chat_id,
            text=telegram_ui.ai_unavailable_notice(ticket_id),
            reply_markup=escalate_only_keyboard(ticket_id),
        )
        return

    rtype = result["response_type"]

    if rtype == config.AI_RESPONSE_ESCALATION:
        await offer_escalation(context, ticket_id, chat_id, note=result["message"])
        return

    if rtype == config.AI_RESPONSE_SOLUTION:
        # ATOMİK sayaç: aynı butona iki kez basılırsa ikinci çağrı False döner
        # ve kullanıcıya ikinci bir çözüm gönderilmez.
        if not db.claim_ai_attempt(ticket_id, attempt):
            logger.info(f"Talep #{ticket_id}: eş zamanlı AI turu bastırıldı.")
            return
        kind = config.MESSAGE_KIND_AI_SOLUTION
        attempt += 1
    else:
        kind = config.MESSAGE_KIND_AI_CLARIFICATION

    db.add_message(ticket_id, "ai", 0, "AI Asistan", result["message"], message_kind=kind)

    if kind == config.MESSAGE_KIND_AI_SOLUTION:
        footer = f"\n\n(Çözüm denemesi {attempt}/{max_attempts})"
        keyboard = (solution_choice_keyboard(ticket_id) if attempt < max_attempts
                    else escalate_only_keyboard(ticket_id))
    else:
        # Açıklayıcı soru: sayaç harcanmadı, kullanıcı serbestçe yanıtlayabilir.
        footer = ""
        keyboard = solution_choice_keyboard(ticket_id)

    await context.bot.send_message(
        chat_id=chat_id, text=result["message"] + footer, reply_markup=keyboard
    )


async def offer_escalation(context: ContextTypes.DEFAULT_TYPE, ticket_id: int,
                           chat_id: int, note: str = None):
    """Çözüm hakkı bitti ya da AI devrini istiyor: kullanıcıya teklif sunulur."""
    text = note.strip() + "\n\n" if note else ""
    text += ("Bu sorunu ilk seviyede çözemedim. Talebinizi teknik ekibe "
             "aktarmamı ister misiniz?")
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=escalate_only_keyboard(ticket_id)
    )


# ---------------- Eskalasyon: sınıflandırma + Faz 3A routing ----------------

async def start_escalation(context: ContextTypes.DEFAULT_TYPE, ticket_id: int, chat_id: int):
    """Konuşmayı sınıflandırır ve talebi DOĞRU KUYRUĞA yerleştirir.

    ACCEPTANCE UX + ROUTING REVISION §3, §11 — DÖRT SONUÇ TİPİ:

      1) Gemini structured sınıflandırması
         (classification_type + category + required_skill)
      2) SERVER-SIDE DOĞRULAMA — routing.resolve_classification().
         Model SOURCE OF TRUTH DEĞİLDİR: uydurulmuş kod, pasif uzmanlık,
         geçersiz departman ve bozuk JSON burada elenir.
      3) Doğrulanmış dayanağa göre TEK yol seçilir ve DURULUR:

           SPECIFIC_SKILL  -> skill routing   (§4)
           GENERAL_SUPPORT -> genel destek    (§6, §9)  [YALNIZCA açık beyanla]
           DEPARTMENT      -> departman       (§5)
           MANUAL_TRIAGE   -> admin incelemesi (§8)

    §12 — FALLTHROUGH YASAK: aday bulunamaması burada başka bir dayanağa
    geçiş sebebi DEĞİLDİR. Talep kendi kuyruğunda bekler.
    §7 — Belirsizlik, geçersiz kod veya sınıflandırma hatası GENEL DESTEĞE
    ÇEVRİLMEZ; MANUAL_TRIAGE'a gider.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        return
    if is_human_assisted(ticket):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"{telegram_ui.format_ticket_label(ticket_id)} zaten teknik "
                  "ekibe aktarılmış durumda."))
        return

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    catalog = db.list_skills(only_active=True)
    history = db.get_conversation_history(ticket_id)
    classification = await asyncio.to_thread(
        ai_assistant.classify_for_escalation, ticket["subject"], history, catalog
    )

    # --- SERVER-SIDE DOĞRULAMA: model çıktısına körü körüne güvenilmez ---
    # Bu TEK çağrı, dört sonuç tipinden hangisinin geçerli olduğunu belirler.
    # Bot kendi if/else sınıflandırma mantığını YAZMAZ.
    decision = routing.resolve_classification(
        classification, fallback_department=ticket.get("category"))
    basis = decision["routing_basis"]
    department = decision["department"]
    skill_code = decision["skill_code"]

    if basis == routing.BASIS_MANUAL_TRIAGE:
        logger.info("Talep #%s manuel triyaja alindi (%s).", ticket_id,
                    decision.get("reason"))

    # Doğrulanmış sonuç TEK transaction'da yazılır. skill_code None ise
    # required_skill_code TEMİZLENİR — MANUAL_TRIAGE/DEPARTMENT talebinin
    # üzerinde eski bir uzmanlık kodu asılı kalmaz.
    db.set_ticket_routing(ticket_id, basis, skill_code=skill_code,
                          department=department)
    db.set_ai_mode(ticket_id, config.AI_MODE_HUMAN)
    db.mark_escalated(ticket_id, pending_department=department)

    # --- PRIORITY ROUTING REVISION §B: AI destekli P1-P4 ---
    # decision["priority"] ZATEN doğrulanmıştır (routing._validated_priority);
    # geçersiz/eksik/başarısız her durum P3'e düşmüştür. apply_ai_priority
    # yalnızca insan henüz elle karar VERMEMİŞSE uygular — admin override
    # edilmiş bir talebin önceliği AI tarafından geri alınmaz.
    #
    # SIRA ÖNEMLİ: öncelik SLA saati başlamadan ÖNCE yazılır ki ilk yanıt /
    # çözüm son tarihleri doğrudan doğru pencereyle hesaplansın.
    db.apply_ai_priority(ticket_id, decision["priority"],
                         reason=decision.get("priority_reason"))

    # Faz 5B: AI_HANDLING süresi SLA'ya dahil DEĞİLDİR; saat tam burada başlar.
    db.start_sla(ticket_id)
    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"Talep teknik ekibe aktarıldı. Yönlendirme: {routing.basis_label(basis)}"
        + (f" · {routing.skill_display_name(skill_code)}" if skill_code else "")
        + (f" · Kategori: {department}" if department else ""),
        message_kind=config.MESSAGE_KIND_SYSTEM,
    )

    # --- §8: MANUEL TRİYAJ — otomatik atama YOK, GENEL DESTEĞE DÜŞÜŞ YOK ---
    if basis == routing.BASIS_MANUAL_TRIAGE:
        await queue_without_candidate(context, ticket_id, chat_id, basis,
                                      department=department)
        return

    # --- §5: DEPARTMAN ROUTING ---
    if basis == routing.BASIS_DEPARTMENT:
        await escalate_ticket(context, ticket_id, department)
        return

    # --- §4 / §6: SPECIFIC_SKILL veya GENERAL_SUPPORT ---
    # İkisi de skill yolunu kullanır; farkları hangi kuyruğa ait olduklarıdır.
    # Kullanıcıya aday listesi SUNULMAZ; sistem en uygun kişiyi kendisi seçer.
    ticket = db.get_ticket(ticket_id)
    result = routing.select_for_ticket(ticket)
    await auto_assign_after_escalation(context, ticket_id, chat_id, result)


async def queue_without_candidate(context: ContextTypes.DEFAULT_TYPE, ticket_id: int,
                                  chat_id: int, basis: str, department: str = None,
                                  waiting_for: str = None):
    """Talep ATANMADAN kuyrukta bırakılır — KENDİ dayanağıyla.

    §4, §5, §9: 'uygun kişi yok' bir yönlendirme hatası değildir; talep kendi
    kuyruğunda bekler. §12: BAŞKA bir dayanağa (özellikle genel desteğe)
    KAYDIRILMAZ. §8: MANUAL_TRIAGE'da da talep kaybolmaz, admin görür.

    Kullanıcıya teknik itiraf yapılmaz; talebin ekipte olduğu bildirilir."""
    db.mark_escalation_pending(ticket_id, department=department)
    db.set_ticket_routing_basis(ticket_id, basis)

    if basis == routing.BASIS_MANUAL_TRIAGE:
        user_text = telegram_ui.manual_triage_notice(ticket_id)
    else:
        user_text = telegram_ui.queue_notice(ticket_id, waiting_for)
    await context.bot.send_message(chat_id=chat_id, text=user_text)

    # Admin bildirimi: NEDEN atanmadığı açıkça yazılır (§28 ile aynı dil).
    detail = f" — {waiting_for}" if waiting_for else ""
    admin_text = (f"⏳ Talep #{ticket_id} · {routing.basis_label(basis)}{detail}. "
                  "Kuyrukta bekliyor.")
    if basis == routing.BASIS_MANUAL_TRIAGE:
        admin_text = (f"🧭 Talep #{ticket_id} · {routing.basis_label(basis)}. "
                      "Otomatik atama yapılmadı; panelden uzmanlık/departman "
                      "atayabilir ya da ana sorumlu belirleyebilirsiniz.")
    targets = [config.ADMIN_GROUP_ID] if config.ADMIN_GROUP_ID else list(config.ADMIN_IDS)
    for admin_chat in targets:
        try:
            await context.bot.send_message(chat_id=admin_chat, text=admin_text)
        except Exception:
            pass


async def auto_assign_after_escalation(context: ContextTypes.DEFAULT_TYPE, ticket_id: int,
                                       chat_id: int, result: dict):
    """Eskalasyon sonrası OTOMATİK ana sorumlu ataması.

    ACCEPTANCE REVISION: kullanıcıya aday listesi veya onay sorulmaz — tek aday
    olsa bile. Seçimi routing.select_best_technician_for_skill yapmıştır;
    burada yalnızca ATOMİK atama ve bilgilendirme vardır.

    §4, §9, §12 — 0 ADAY BAŞKA KUYRUĞA GEÇİŞ SEBEBİ DEĞİLDİR:
    Aday yoksa talep KAYBOLMAZ ve GENEL DESTEĞE/başka departmana DÜŞMEZ;
    status = eskalasyon_bekliyor ile KENDİ dayanağının kuyruğunda kalır ve
    queue worker uygun kişi çıktığında aynı merkezi motorla atar."""
    selected = result.get("selected")
    basis = result.get("routing_basis") or routing.BASIS_SPECIFIC_SKILL
    skill_name = result["skill"]["name"] if result.get("skill") else "teknik destek"

    # --- 0 aday: talep KENDİ kuyruğunda bekler ---
    if not selected:
        await queue_without_candidate(
            context, ticket_id, chat_id, basis,
            department=result.get("department"), waiting_for=skill_name)
        return

    technician = db.get_technician(selected["technician_id"])
    if not technician:
        await queue_without_candidate(
            context, ticket_id, chat_id, basis,
            department=result.get("department"), waiting_for=skill_name)
        return

    ticket = db.get_ticket(ticket_id)

    # --- ATOMİK atama: iki eş zamanlı akış iki PRIMARY üretemez ---
    actor = {"source": config.ASSIGN_SOURCE_SYSTEM,
             "label": "Otomatik uzman yönlendirmesi"}
    if not db.assign_primary_if_unassigned(ticket_id, technician["id"],
                                           technician["name"], actor=actor):
        current = db.get_ticket(ticket_id)
        current_name = primary_assignee_name(current) or "bir ekip üyesi"
        await context.bot.send_message(
            chat_id=chat_id,
            text=telegram_ui.already_assigned_notice(ticket_id, current_name),
        )
        return

    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"{technician['name']} talebe otomatik olarak ana sorumlu atandı. "
        f"({routing.selection_reason(result)})",
        message_kind=config.MESSAGE_KIND_SYSTEM,
    )

    # --- Kullanıcıya YALNIZCA sonuç: iş yükü / öncelik / seçim gerekçesi YOK ---
    await context.bot.send_message(
        chat_id=chat_id,
        text=telegram_ui.assignment_notice(ticket_id, skill_name,
                                           technician["name"]),
    )

    # --- Teknisyene KISA bildirim (tüm konuşma Telegram'a dökülmez) ---
    if technician.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=technician["telegram_id"],
                text=(f"🆕 Yeni talep #{ticket_id}\n\n"
                      f"Konu: {ticket['subject']}\n"
                      f"Uzmanlık: {skill_name}\n"
                      f"Kullanıcı: {ticket['full_name']}\n\n"
                      "Geçmişin tamamı web panelinde."),
            )
        except Exception as e:
            logger.warning(f"Teknisyene bildirim gönderilemedi: {type(e).__name__}")


async def handle_stale_candidate_callback(query, ticket_id: int):
    """ESKİ 'tech:<ticket>:<technician>' butonları için güvenli no-op.

    Bu callback biçimi ARTIK ÜRETİLMİYOR. Ancak revizyondan önce gönderilmiş
    Telegram mesajları kullanıcıların sohbetinde duruyor olabilir. Böyle bir
    butona basılırsa:
      - RASTGELE / stale bir atama KESİNLİKLE YAPILMAZ
      - talebin mevcut durumu neyse o bildirilir
    """
    ticket = db.get_ticket(ticket_id)
    if ticket and ticket.get("assigned_technician_id"):
        current_name = primary_assignee_name(ticket) or "bir ekip üyesi"
        await query.edit_message_text(
            "Bu seçim artık geçerli değil.\n"
            + telegram_ui.already_assigned_notice(ticket_id, current_name)
        )
        return
    await query.edit_message_text(
        "Bu seçim artık geçerli değil; talep otomatik olarak yönlendiriliyor.\n"
        "Uygun uzman hazır olduğunda size bilgi verilecek."
    )


async def my_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """§15, §17: /taleplerim KALDIRILMADI — '📋 Taleplerim' butonuyla AYNI
    helper'a düşer (show_my_tickets); liste biçimi tek yerde tanımlıdır."""
    await show_my_tickets(update, context)


# ---------------- Yönetici komutları ----------------

async def admin_only_guard(update: Update) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Bu komutu kullanma yetkiniz yok.")
        return False
    return True


async def list_tickets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    status_filter = None
    if context.args:
        mapping = {s: s for s in config.STATUS_LABELS}
        mapping["cozuldu"] = config.STATUS_CLOSED
        status_filter = mapping.get(context.args[0].lower())
    tickets = db.list_tickets(status_filter)
    if not tickets:
        await update.message.reply_text("Talep bulunamadı.")
        return
    lines = []
    for t in tickets[:30]:
        status_label = config.STATUS_LABELS.get(t["status"], t["status"])
        sorumlu = primary_assignee_name(t)
        assigned = f" | 👤 {sorumlu}" if sorumlu else ""
        lines.append(f"#{t['id']} [{status_label}]{assigned} - {t['subject'][:40]}")
    await update.message.reply_text("\n".join(lines))


async def ticket_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /detay <talep_id>")
        return
    try:
        ticket_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçersiz talep numarası.")
        return
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("Talep bulunamadı.")
        return
    messages = db.get_messages(ticket_id)
    text = ticket_summary_text(ticket) + "\n\n--- Mesajlar ---\n"
    for m in messages:
        sender = "👤" if m["sender_type"] == "kullanici" else "🛠️"
        text += f"{sender} {m['sender_name']}: {m['message_text']}\n"
    await update.message.reply_text(text)


async def assign_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /ata <talep_id>")
        return
    try:
        ticket_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçersiz talep numarası.")
        return
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("Talep bulunamadı.")
        return
    admin = update.effective_user
    # Faz 5A: legacy assigned_admin_name'e yazmıyoruz. Ana sorumlu bir TEKNİSYEN
    # kaydı olmalı; Telegram yöneticisi bir teknisyene eşleşmiyorsa atama web
    # panelinden yapılmalıdır.
    technician = db.get_technician_by_telegram_id(admin.id)
    if not technician:
        await update.message.reply_text(
            "Telegram hesabınız bir teknik ekip kaydına bağlı değil. "
            "Atama işlemini web panelinden yapabilirsiniz.")
        return
    if not db.assign_primary_if_unassigned(
            ticket_id, technician["id"], technician["name"],
            actor={"source": config.ASSIGN_SOURCE_TELEGRAM,
                   "external_id": admin.id, "label": admin.full_name}):
        await update.message.reply_text(
            f"Talep #{ticket_id} zaten bir ana sorumluya atanmış.")
        return
    ticket = db.get_ticket(ticket_id)
    await update.message.reply_text(f"Talep #{ticket_id} size atandı.")
    await notify_user_status_change(
        context, ticket, f"Talebiniz {technician['name']} tarafından üstlenildi.")


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: /yanitla <talep_id> <mesaj>")
        return
    try:
        ticket_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçersiz talep numarası.")
        return
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("Talep bulunamadı.")
        return
    message_text = " ".join(context.args[1:])
    admin = update.effective_user
    # Faz 5A: set_ticket_responder ARTIK ÇAĞRILMIYOR — legacy assigned_admin_name'e
    # yazıyordu. Mesajı kimin yazdığı messages kaydından okunur.
    db.add_message(ticket_id, "yonetici", admin.id, admin.full_name, message_text,
                   message_kind=config.MESSAGE_KIND_NORMAL)
    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=f"💬 {admin.full_name}: {message_text}",
        )
        await update.message.reply_text("Yanıt kullanıcıya iletildi.")
    except Exception as e:
        await update.message.reply_text(f"Yanıt gönderilemedi: {e}")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /kapat <talep_id>")
        return
    try:
        ticket_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçersiz talep numarası.")
        return
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await update.message.reply_text("Talep bulunamadı.")
        return
    actor = resolve_actor_for_telegram(update.effective_user)
    db.resolve_ticket(ticket_id, web_user_id=actor["web_user_id"],
                      source=actor["source"], external_id=actor["external_id"],
                      label=actor["label"])
    db.add_message(ticket_id, "sistem", 0, "Sistem",
                   f"Talep {actor['label']} tarafından Telegram üzerinden çözüldü olarak işaretlendi.")
    ticket = db.get_ticket(ticket_id)
    await update.message.reply_text(f"Talep #{ticket_id} çözüldü olarak kapatıldı.")
    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=(f"✅ {telegram_ui.format_ticket_label(ticket_id)} çözümlendi.\n"
                  "Sorun devam ediyorsa aşağıdaki düğmeyle AYNI talebi yeniden açabilirsiniz."),
            reply_markup=reopen_keyboard(ticket_id),
        )
    except Exception as e:
        logger.warning(f"Kullanıcıya kapanış bildirimi gönderilemedi: {e}")


# ---------------- Teknik ekip komutları ----------------

async def team_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    technicians = db.list_technicians()
    if not technicians:
        await update.message.reply_text(
            "Henüz teknik ekip üyesi yok.\n"
            "Eklemek için: /ekibeekle <isim> <departman> [telegram_id]\n"
            f"Departmanlar: {', '.join(config.DEPARTMENTS)}"
        )
        return
    lines = ["👥 Teknik Ekip:\n"]
    for t in technicians:
        status = "🟢 Müsait" if t["available"] else "🔴 Meşgul"
        lines.append(f"#{t['id']} {t['name']} — {t['department']} — {status}")
    await update.message.reply_text("\n".join(lines))


async def team_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only_guard(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım: /ekibeekle <isim> <departman> [telegram_id]\n"
            f"Departmanlar: {', '.join(config.DEPARTMENTS)}\n"
            "telegram_id verirsen o kişiye otomatik bildirim gönderilir; "
            "ayrıca kendisi /musaitim yazarak müsaitliğini değiştirebilir."
        )
        return

    # Son argüman sayısal ise telegram_id'dir, geri kalanı isim+departman olarak ayrıştırılır
    args = context.args
    telegram_id = None
    if args[-1].isdigit():
        telegram_id = int(args[-1])
        args = args[:-1]

    if len(args) < 2:
        await update.message.reply_text("Departman belirtmelisiniz. Örnek: /ekibeekle Ahmet Yılmaz Ağ")
        return

    department = args[-1]
    name = " ".join(args[:-1])

    if department not in config.DEPARTMENTS:
        await update.message.reply_text(f"Geçersiz departman. Kullanılabilir: {', '.join(config.DEPARTMENTS)}")
        return

    tech_id = db.create_technician(name, telegram_id, department)
    await update.message.reply_text(f"✅ {name} ({department}) ekibe eklendi. (#{tech_id})")


async def available_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teknisyenin kendisi bota /musaitim yazarak müsaitliğini açar.

    ACCEPTANCE FIX 2: eskiden bu komut departmandaki EN ESKİ bekleyen talebi
    doğrudan komutu yazan kişiye atıyordu ("butona son basan işi kapar").
    Artık kuyruk merkezi motorla yeniden değerlendirilir — iş yükü daha düşük
    ya da daha uzun süredir iş almamış BİR BAŞKASI seçilebilir."""
    user = update.effective_user
    technician = db.get_technician_by_telegram_id(user.id)
    if not technician:
        await update.message.reply_text("Kayıtlı bir teknik ekip üyesi olarak bulunamadınız.")
        return

    db.set_technician_availability(technician["id"], True)
    await update.message.reply_text(f"🟢 Müsait olarak işaretlendiniz ({technician['department']}).")

    # Kişinin uzmanlıklarını VE departmanını bekleyen kuyruklar taranır.
    for decision in queue_service.reevaluate_for_technician(technician["id"]):
        await notify_auto_assignment(context, decision)


async def notify_auto_assignment(context: ContextTypes.DEFAULT_TYPE, decision: dict):
    """Kuyruktan OTOMATİK atanan talebi ilgili taraflara bildirir.

    Bildirim hatası ATAMAYI GERİ ALMAZ — atama gerçek bir iş dağıtımıdır."""
    if not decision.get("assigned"):
        return
    ticket = db.get_ticket(decision["ticket_id"])
    technician = decision.get("technician")
    if not ticket or not technician:
        return

    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=queue_service.assignment_text(ticket, decision["result"], technician),
        )
    except Exception as e:
        logger.warning("Talep #%s kullanici bildirimi gonderilemedi: %s",
                       ticket["id"], type(e).__name__)

    if technician.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=technician["telegram_id"],
                text=queue_service.technician_notification_text(ticket, decision["result"]),
            )
        except Exception as e:
            logger.warning("Talep #%s teknisyen bildirimi gonderilemedi: %s",
                           ticket["id"], type(e).__name__)


async def available_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Teknisyenin kendisi bota /mesgulum yazarak müsaitliğini kapatır."""
    user = update.effective_user
    technician = db.get_technician_by_telegram_id(user.id)
    if not technician:
        await update.message.reply_text("Kayıtlı bir teknik ekip üyesi olarak bulunamadınız.")
        return

    db.set_technician_availability(technician["id"], False)
    await update.message.reply_text(f"🔴 Meşgul olarak işaretlendiniz ({technician['department']}).")


# ---------------- Inline buton callback'leri ----------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bütün inline buton aksiyonları.

    GÜVENLİK: callback_data'ya asla körü körüne güvenilmez. Her aksiyonda
      - talep gerçekten var mı?
      - Telegram kullanıcısı talebin SAHİBİ mi? (forward edilmiş callback koruması)
      - talep uygun durumda mı?
    kontrol edilir. Atama ve çözme işlemleri ATOMİK yapılır (idempotent).
    """
    query = update.callback_query
    actor = update.effective_user

    parts = query.data.split(":")
    action = parts[0]
    try:
        ticket_id = int(parts[1])
    except (IndexError, ValueError):
        await query.answer("Geçersiz işlem.", show_alert=True)
        return

    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await query.answer("Talep bulunamadı.", show_alert=True)
        return

    # --- Talep sahibinin kendi aksiyonları ---
    if action in ("solved", "cont", "esc", "tech", "cancel", "reopen"):
        if ticket["user_id"] != actor.id:
            await query.answer("Bu talep size ait değil.", show_alert=True)
            return
        await query.answer()

        if action == "solved":
            # ATOMİK: ikinci tıklama hata üretmez, sessizce yok sayılır.
            resolved = db.resolve_ticket_if_open(
                ticket_id,
                source=config.RESOLVED_SOURCE_AI,
                external_id=actor.id,
                label=actor.full_name,
            )
            if not resolved:
                await query.edit_message_text(
                    telegram_ui.already_resolved_notice(ticket_id),
                    reply_markup=reopen_keyboard(ticket_id),
                )
                return
            db.add_message(
                ticket_id, "sistem", 0, "Sistem",
                "Kullanıcı AI çözümünün sorunu giderdiğini doğruladı.",
                message_kind=config.MESSAGE_KIND_SYSTEM,
            )
            await query.edit_message_text(
                telegram_ui.resolved_notice(ticket_id),
                reply_markup=reopen_keyboard(ticket_id),
            )
            return

        if action == "cont":
            if is_human_assisted(ticket):
                await query.edit_message_text(
                    f"{telegram_ui.format_ticket_label(ticket_id)} teknik ekibe "
                    "aktarılmış durumda.")
                return
            if ticket["status"] == config.STATUS_CLOSED:
                await query.edit_message_text(
                    f"{telegram_ui.format_ticket_label(ticket_id)} kapatılmış durumda.")
                return
            # Butonları kaldır ki aynı mesajdan ikinci kez tetiklenmesin.
            await query.edit_message_reply_markup(reply_markup=None)
            await run_ai_turn(context, ticket_id, actor.id)
            return

        if action == "esc":
            # Kullanıcı ÜÇ DENEMEYİ BEKLEMEDEN de bunu seçebilir.
            if is_human_assisted(ticket):
                await query.edit_message_text(
                    f"{telegram_ui.format_ticket_label(ticket_id)} zaten teknik "
                    "ekibe aktarılmış durumda.")
                return
            await query.edit_message_text(
                telegram_ui.escalating_notice(ticket_id))
            await start_escalation(context, ticket_id, actor.id)
            return

        if action == "tech":
            # ESKİ (revizyon öncesi) aday seçim butonu. Yeni mesajlarda
            # üretilmiyor; stale callback RASTGELE ATAMA YAPMAZ.
            await handle_stale_candidate_callback(query, ticket_id)
            return

        if action == "cancel":
            # ESKİ aday listesindeki 'Vazgeç' butonu. Yeni akışta üretilmiyor.
            # Talep zaten atanmışsa durumu bozmaz; kuyruktaysa kuyrukta kalır.
            if ticket.get("assigned_technician_id"):
                current_name = primary_assignee_name(ticket) or "bir ekip üyesi"
                await query.edit_message_text(
                    telegram_ui.already_assigned_notice(ticket_id, current_name))
                return
            await query.edit_message_text(
                f"{telegram_ui.format_ticket_label(ticket_id)} teknik ekip "
                "kuyruğunda bekliyor. Uygun uzman hazır olduğunda otomatik "
                "olarak yönlendirilecek; buraya yazmaya devam edebilirsiniz."
            )
            db.mark_escalation_pending(ticket_id, department=ticket.get("category"))
            return

        if action == "reopen":
            if ticket["status"] != config.STATUS_CLOSED:
                await query.edit_message_text(
                    f"{telegram_ui.format_ticket_label(ticket_id)} zaten açık durumda.")
                return
            # AYNI talep yeniden açılır — yeni ID oluşturulmaz, geçmiş korunur.
            db.reopen_ticket(ticket_id)
            db.add_message(
                ticket_id, "sistem", 0, "Sistem",
                "Kullanıcı sorunun devam ettiğini bildirdi; talep yeniden açıldı.",
                message_kind=config.MESSAGE_KIND_SYSTEM,
            )
            reopened = db.get_ticket(ticket_id)
            await query.edit_message_text(telegram_ui.reopened_notice(ticket_id))
            await notify_assigned_technician(
                context, reopened,
                f"🔄 Talep #{ticket_id} kullanıcı tarafından yeniden açıldı.",
            )
            targets = [config.ADMIN_GROUP_ID] if config.ADMIN_GROUP_ID else list(config.ADMIN_IDS)
            for chat_id in targets:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔄 Talep #{ticket_id} kullanıcı tarafından yeniden açıldı.",
                    )
                except Exception:
                    pass
            return

    # --- Yönetici aksiyonları ---
    if not is_admin(actor.id):
        await query.answer("Yetkiniz yok.", show_alert=True)
        return

    await query.answer()

    if action == "assign":
        technician = db.get_technician_by_telegram_id(actor.id)
        if not technician:
            await query.edit_message_text(
                "Telegram hesabınız bir teknik ekip kaydına bağlı değil. "
                "Atamayı web panelinden yapabilirsiniz.")
            return
        if not db.assign_primary_if_unassigned(
                ticket_id, technician["id"], technician["name"],
                actor={"source": config.ASSIGN_SOURCE_TELEGRAM,
                       "external_id": actor.id, "label": actor.full_name}):
            await query.edit_message_text(f"Talep #{ticket_id} zaten atanmış.")
            return
        ticket = db.get_ticket(ticket_id)
        await notify_user_status_change(
            context, ticket, f"Talebiniz {technician['name']} tarafından üstlenildi.")
        await query.edit_message_text(ticket_summary_text(ticket) + "\n\n✅ Üstlenildi.")
    elif action == "close":
        audit = resolve_actor_for_telegram(actor)
        db.resolve_ticket_if_open(ticket_id, source=audit["source"],
                                  external_id=audit["external_id"], label=audit["label"],
                                  web_user_id=audit["web_user_id"])
        db.add_message(ticket_id, "sistem", 0, "Sistem",
                       f"Talep {audit['label']} tarafından Telegram üzerinden çözüldü olarak işaretlendi.",
                       message_kind=config.MESSAGE_KIND_SYSTEM)
        ticket = db.get_ticket(ticket_id)
        await notify_user_status_change(context, ticket, "Talebiniz çözümlendi ve kapatıldı.")
        await query.edit_message_text(ticket_summary_text(ticket) + "\n\n🔒 Çözüldü.")


def main():
    db.init_db()
    auth.bootstrap_admin()
    db.seed_default_skills()
    auth.revoke_shared_password_accounts()
    application = Application.builder().token(config.BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("istek", create_ticket_command))
    application.add_handler(CommandHandler("taleplerim", my_tickets))
    # §15: slash komutlar GERİYE DÖNÜK UYUMLU kalır. ReplyKeyboard birincil
    # UX'tir ama eski kullanıcılar ve power-user'lar komutları kullanmaya
    # devam edebilir. /yardim, '❓ Yardım' butonuyla aynı helper'a düşer.
    application.add_handler(CommandHandler("yardim", help_command))

    application.add_handler(CommandHandler("liste", list_tickets_command))
    application.add_handler(CommandHandler("detay", ticket_detail_command))
    application.add_handler(CommandHandler("ata", assign_command))
    application.add_handler(CommandHandler("yanitla", reply_command))
    application.add_handler(CommandHandler("kapat", close_command))

    application.add_handler(CommandHandler("ekip", team_list_command))
    application.add_handler(CommandHandler("ekibeekle", team_add_command))
    application.add_handler(CommandHandler("musaitim", available_on_command))
    application.add_handler(CommandHandler("mesgulum", available_off_command))

    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    logger.info("Bot polling modunda başlatılıyor...")
    application.run_polling()


if __name__ == "__main__":
    main()
