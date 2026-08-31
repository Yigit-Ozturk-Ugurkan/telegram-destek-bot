"""Telegram KULLANICI ARAYÜZÜ metinleri — TEK MERKEZ.

ACCEPTANCE UX + ROUTING REVISION §14-§21.

NEDEN AYRI MODÜL?
-----------------
Aynı talep etiketi (`🎫 Talep 42`) beş ayrı yerde kullanıcıya gösteriliyor:
taleplerim listesi, atama bildirimi, kuyruk bildirimi, çözüm bildirimi ve
yeniden açma. Bunlar iki ayrı süreçte yaşıyor (bot.py ve queue_worker.py ->
queue_service.py). Etiketi her iki tarafta ayrı ayrı yazmak, biçimin zamanla
ayrışmasına yol açardı; bu modül o biçimin TEK TANIMIDIR.

Bu modül BİLEREK `telegram` paketini IMPORT ETMEZ:
  - queue_service / queue_worker ağ ve kütüphane bağımlılığı olmadan kullanabilsin
  - metinler gerçek Telegram olmadan test edilebilsin
Klavye NESNELERİ (ReplyKeyboardMarkup / InlineKeyboardMarkup) bot.py'de,
BURADAKİ buton metinlerinden üretilir.

ADMIN / LOG TARAFI KAPSAM DIŞIDIR: terminal loglarında ve web panelinde
`#42` biçimi korunur. Burada değişen yalnızca SON KULLANICI SUNUMUDUR;
tickets.id ve veritabanı DEĞİŞMEZ.
"""

import config

# ==================================================================
# §18-§19: TALEP ETİKETİ — kullanıcıya `#42` GÖSTERİLMEZ
# ==================================================================

TICKET_LABEL_PREFIX = "🎫 Talep"


def format_ticket_label(ticket_id) -> str:
    """Kullanıcıya gösterilecek talep etiketi: `🎫 Talep 42`.

    `#` işareti KULLANILMAZ — geliştirici görünümü verdiği için acceptance
    testinde kaldırıldı. DB kimliği (tickets.id) DEĞİŞMEZ; değişen yalnızca
    sunumdur, bu yüzden numara birebir aktarılır.

    Kullanıcıya giden HER metinde bu fonksiyon kullanılmalıdır. Admin
    terminali / web paneli için `#42` biçimi serbesttir."""
    return f"{TICKET_LABEL_PREFIX} {ticket_id}"


# ==================================================================
# §14: ANA MENÜ — kalıcı ReplyKeyboard
# ==================================================================

BTN_NEW_TICKET = "🆕 Yeni Talep"
BTN_MY_TICKETS = "📋 Taleplerim"
BTN_HELP = "❓ Yardım"

#: Klavye düzeni (§14). bot.py bunu ReplyKeyboardMarkup'a çevirir.
#: [ 🆕 Yeni Talep ] [ 📋 Taleplerim ]
#: [ ❓ Yardım ]
MAIN_MENU_ROWS = [
    [BTN_NEW_TICKET, BTN_MY_TICKETS],
    [BTN_HELP],
]

#: Buton metinlerinin hızlı aranması için düz küme.
MAIN_MENU_BUTTONS = frozenset(
    label for row in MAIN_MENU_ROWS for label in row)


def is_menu_button(text: str) -> bool:
    """Gelen düz metin bir ana menü butonu mu?

    Kullanıcı butona bastığında Telegram bunu NORMAL BİR METİN MESAJI olarak
    gönderir; komut değildir. Bu yüzden metin akışının en başında ayrıştırmak
    gerekir — aksi hâlde '📋 Taleplerim' yazısı yeni bir destek talebi olarak
    kaydedilirdi."""
    return (text or "").strip() in MAIN_MENU_BUTTONS


# ==================================================================
# §17, §20: KULLANICIYA GÖRÜNEN DURUM ADLARI
# ==================================================================

#: Son kullanıcıya gösterilen durum adları. config.STATUS_LABELS panel/operasyon
#: dilidir ("Atandı", "Uzman Bekleniyor"); kullanıcıya iş akışının iç adımları
#: değil, ANLAMI gösterilir. DB değerleri DEĞİŞMEZ — bu yalnızca sunumdur.
USER_STATUS_LABELS = {
    config.STATUS_OPEN: "İnceleniyor",
    config.STATUS_ASSIGNED: "Teknik ekipte",
    config.STATUS_IN_PROGRESS: "Teknik ekipte",
    config.STATUS_WAITING_USER: "Sizden bilgi bekleniyor",
    config.STATUS_CLOSED: "Çözüldü",
    config.STATUS_REOPENED: "Yeniden açıldı",
    config.STATUS_ESCALATION_PENDING: "Teknik ekibe yönlendiriliyor",
}


def user_status_label(status: str) -> str:
    """Bilinmeyen durum için panel etiketine, o da yoksa ham değere düşer."""
    return USER_STATUS_LABELS.get(
        status, config.STATUS_LABELS.get(status, status))


# ==================================================================
# §16: YENİ TALEP
# ==================================================================

NEW_TICKET_PROMPT = (
    "Yeni talebinizi yazabilirsiniz.\n\n"
    "Sorununuzu kendi cümlelerinizle anlatmanız yeterli; "
    "gerekirse birkaç soru sorup ardından teknik ekibe yönlendireceğim."
)


def existing_ticket_notice(ticket: dict) -> str:
    """§23: aynı anda ikinci talep açılamaz — mevcut politika korunur.

    Kullanıcı aktif bir talebi varken '🆕 Yeni Talep'e basarsa yeni kayıt
    AÇILMAZ; mevcut talebe yazmaya devam etmesi istenir (same-ticket)."""
    return (
        "Devam eden bir talebiniz bulunuyor.\n\n"
        f"{format_ticket_label(ticket['id'])}\n"
        f"{ticket.get('subject') or ''}\n"
        f"Durum: {user_status_label(ticket.get('status'))}\n\n"
        "Yazacağınız mesajlar bu talebe eklenecek. Yeni bir konu için önce "
        "mevcut talebinizin kapanmasını bekleyin."
    )


# ==================================================================
# §14-§15: KARŞILAMA VE YARDIM
# ==================================================================

WELCOME_TEXT = (
    "Merhaba! 👋\n\n"
    "Aşağıdaki menüden işlem seçebilirsiniz:\n"
    f"• {BTN_NEW_TICKET} — destek talebi oluşturun\n"
    f"• {BTN_MY_TICKETS} — taleplerinizi görüntüleyin\n"
    f"• {BTN_HELP} — nasıl çalıştığını öğrenin\n\n"
    "Dilerseniz doğrudan sorununuzu da yazabilirsiniz."
)

#: §15: slash komutlar KALDIRILMADI. Buton ezberi gerektirmeyen ana akış
#: ReplyKeyboard'dır; komutlar eski kullanıcılar ve power-user'lar için durur.
HELP_TEXT = (
    "❓ Nasıl çalışır?\n\n"
    f"1) {BTN_NEW_TICKET} ile sorununuzu yazarsınız.\n"
    "2) Önce otomatik asistan yardımcı olmayı dener.\n"
    "3) Çözülmezse talebiniz teknik ekibe yönlendirilir ve size uygun uzman "
    "atanır.\n"
    "4) Görüşme hep AYNI talep üzerinden devam eder; yeni yazdığınız mesajlar "
    "aynı talebe eklenir.\n\n"
    f"{BTN_MY_TICKETS} ile durumunu her zaman görebilirsiniz.\n\n"
    "Komutları kullanmayı tercih ederseniz bunlar da çalışmaya devam eder:\n"
    "/istek — yeni talep\n"
    "/taleplerim — taleplerim\n"
    "/yardim — bu mesaj"
)


# ==================================================================
# §20: TALEPLERİM GÖRSEL YAPISI
# ==================================================================

MY_TICKETS_EMPTY = (
    "Henüz bir talebiniz bulunmuyor.\n\n"
    f"{BTN_NEW_TICKET} ile ilk talebinizi oluşturabilirsiniz."
)

#: Kartlar arası ayraç. Telegram'da uzun çizgi satırı taşırmasın diye kısa.
_SEPARATOR = "────────────"

#: Listede konu satırı bu uzunlukta kesilir — metin aşırı uzun olmasın (§20).
_SUBJECT_LIMIT = 60

#: Tek mesajda gösterilen en fazla talep sayısı. Telegram mesaj sınırına
#: (4096 karakter) takılmamak ve listeyi okunur tutmak için.
MY_TICKETS_LIMIT = 10


def _clip(text: str, limit: int = _SUBJECT_LIMIT) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def my_tickets_text(tickets, assignee_names=None) -> str:
    """§20: 'Talepleriniz' listesi.

    tickets        : db.list_tickets_for_user() satırları
    assignee_names : {ticket_id: "Ad Soyad"} — atanan uzman adları. Çağıran
                     taraf tek seferde hazırlar; burada DB'ye GİDİLMEZ, böylece
                     liste uzadıkça N+1 sorgu oluşmaz.

    Uzman adı yalnızca VARSA gösterilir; 'atanmadı' gibi iç operasyon durumu
    kullanıcıya yazılmaz."""
    if not tickets:
        return MY_TICKETS_EMPTY

    assignee_names = assignee_names or {}
    shown = list(tickets)[:MY_TICKETS_LIMIT]

    blocks = []
    for t in shown:
        lines = [format_ticket_label(t["id"]), _clip(t.get("subject"))]
        lines.append(f"Durum: {user_status_label(t.get('status'))}")
        name = assignee_names.get(t["id"])
        if name:
            lines.append(f"Uzman: {name}")
        blocks.append("\n".join(line for line in lines if line))

    text = "📋 Talepleriniz\n\n" + f"\n\n{_SEPARATOR}\n\n".join(blocks)
    remaining = len(tickets) - len(shown)
    if remaining > 0:
        text += f"\n\n… ve {remaining} talep daha."
    return text


# ==================================================================
# Kullanıcıya giden diğer bildirimler — hepsi AYNI etiketi kullanır
# ==================================================================

def assignment_notice(ticket_id, basis_label_text: str, technician_name: str,
                      is_department: bool = False) -> str:
    """Talep teknik ekibe aktarıldı ve uzman atandı.

    İÇ OPERASYON DETAYI GÖSTERİLMEZ: iş yükü, öncelik seviyesi ve seçim
    gerekçesi kullanıcıyı ilgilendirmez (mevcut kural korunur)."""
    label = "İlgili ekip" if is_department else "Gerekli uzmanlık"
    return (
        f"{format_ticket_label(ticket_id)} teknik ekibe aktarıldı.\n\n"
        f"{label}: {basis_label_text}\n"
        f"Atanan uzman: {technician_name}\n\n"
        "Destek görüşmeniz aynı talep üzerinden devam edecek; "
        "buraya yazmaya devam edebilirsiniz."
    )


def queue_notice(ticket_id, waiting_for: str = None) -> str:
    """Talep kuyrukta bekliyor — uygun uzman çıkınca otomatik atanacak.

    waiting_for verilirse kullanıcıya NE beklendiği söylenir (uzmanlık ya da
    ekip adı). Talebin KAYBOLMADIĞI açıkça belirtilir."""
    target = f"\n\nBeklenen: {waiting_for}" if waiting_for else ""
    return (
        f"{format_ticket_label(ticket_id)} teknik ekibe aktarıldı."
        f"{target}\n\n"
        "Şu anda uygun bir uzman müsait değil. Ekipten biri hazır olduğunda "
        "talebiniz otomatik olarak atanacak ve size bilgi verilecek. "
        "Aynı talep üzerinden yazmaya devam edebilirsiniz."
    )


def manual_triage_notice(ticket_id) -> str:
    """§8: sınıflandırma belirsiz -> talep KAYBOLMAZ, ekip elle yönlendirir.

    Kullanıcıya 'sınıflandıramadım' gibi teknik bir itiraf yapılmaz; talebin
    ekipte olduğu ve inceleneceği söylenir."""
    return (
        f"{format_ticket_label(ticket_id)} teknik ekibe aktarıldı.\n\n"
        "Talebiniz ekibimiz tarafından incelenip doğru uzmana yönlendirilecek. "
        "Aynı talep üzerinden yazmaya devam edebilirsiniz."
    )


def already_assigned_notice(ticket_id, technician_name: str) -> str:
    return (f"{format_ticket_label(ticket_id)} {technician_name} adlı ekip "
            "üyemize atanmış durumda.")


def message_added_notice(ticket_id, technician_name: str = None) -> str:
    who = (f"{technician_name} en kısa sürede yanıtlayacaktır."
           if technician_name else "Teknik ekibimiz en kısa sürede yanıtlayacaktır.")
    return f"Mesajınız {format_ticket_label(ticket_id)} kaydınıza eklendi.\n{who}"


def resolved_notice(ticket_id) -> str:
    return (f"✅ {format_ticket_label(ticket_id)} çözüldü olarak kapatıldı.\n"
            "Sorun tekrar ederse aşağıdaki düğmeyle aynı talebi yeniden "
            "açabilirsiniz.")


def already_resolved_notice(ticket_id) -> str:
    return f"{format_ticket_label(ticket_id)} zaten çözüldü olarak kapatılmıştı."


def reopened_notice(ticket_id) -> str:
    return (f"🔄 {format_ticket_label(ticket_id)} yeniden açıldı. "
            "Ekibimiz konuyla ilgilenecek.")


def status_change_notice(ticket_id, status: str, extra_note: str = "") -> str:
    text = (f"ℹ️ {format_ticket_label(ticket_id)} güncellendi.\n"
            f"Yeni durum: {user_status_label(status)}")
    if extra_note:
        text += f"\n\n{extra_note}"
    return text


def ticket_created_notice(ticket_id) -> str:
    return f"✅ {format_ticket_label(ticket_id)} alındı. Bakıyorum…"


def escalating_notice(ticket_id) -> str:
    return f"👨‍🔧 {format_ticket_label(ticket_id)} teknik ekibe aktarılıyor…"


def ai_unavailable_notice(ticket_id) -> str:
    return (f"{format_ticket_label(ticket_id)} kaydedildi ancak şu anda "
            "otomatik öneri üretemiyorum. Dilerseniz doğrudan teknik ekibe "
            "aktarabilirim.")
