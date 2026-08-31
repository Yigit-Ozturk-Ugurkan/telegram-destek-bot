"""Uzman bekleme kuyruğu değerlendirme servisi.

Kuyruk AYRI BİR TABLO DEĞİLDİR: `status = eskalasyon_bekliyor` olan talepler
kuyruğu oluşturur. Kuyruk İKİ akıştan beslenir:
  - required_skill_code taşıyanlar  -> skill routing
  - uzmanlığı belirlenememiş ama pending_department taşıyanlar
    -> departman fallback (ACCEPTANCE FIX 2)

ACCEPTANCE REVISION — DAVRANIŞ DEĞİŞİKLİĞİ
------------------------------------------
Faz 5B'de aday bulunduğunda kullanıcıya teknisyen SEÇTİRİLİYORDU. Bu kaldırıldı.
Routing ve yük dağıtımı sistemin sorumluluğudur; kuyruktan çıkan bir talep
otomatik olarak PRIMARY atanır ve kullanıcıya yalnızca SONUÇ bildirilir.

ACCEPTANCE FIX 2
----------------
Uzmanlığı belirlenememiş talepler eskiden burada 'uzmanlik_tespit_edilmemis'
ile ELENİYORDU ve departman ataması ayrı, adaletsiz bir yoldan ("ilk müsait
kişi") yapılıyordu. Artık ikisi de routing.select_for_ticket() üzerinden
geçer; iş yükü ve least-recently-assigned kuralları HER İKİ YOLDA da aynıdır.

KRİTİK KURALLAR:
  - Seçim algoritması BURADA YAZILMAZ; routing.select_for_ticket()
    tek merkezi motordur (bot, worker, web aynı sırayı kullanır).
  - Atama ATOMİKTİR: db.assign_primary_if_unassigned() koşullu UPDATE'i
    sayesinde iki eş zamanlı worker aynı talebe iki PRIMARY atayamaz.
  - Atanmış / çözülmüş taleplere DOKUNULMAZ.
  - Aday yoksa talep kuyrukta KALIR; queue_entered_at korunur, talep kaybolmaz.

Bu modül BİLEREK Telegram gönderimi yapmaz. Yalnızca kararı ve atamayı üretir;
gönderimi çağıran taraf (queue_worker / bot) yapar. Böylece kuyruk mantığı ağ
bağımlılığı olmadan test edilebilir.
"""

import logging

import config
import db
import routing
import telegram_ui

logger = logging.getLogger(__name__)


def evaluate_ticket(ticket: dict):
    """Tek bir bekleyen talebi değerlendirir ve uygun aday varsa ATAR.

    Döner:
        {
          "ticket_id": int,
          "result": <routing sonucu> | None,   # "selected" alanı dahil
          "assigned": bool,                    # PRIMARY ataması BU çağrıda yapıldıysa
          "technician": <teknisyen dict> | None,
          "reason": str,
        }

    assigned=True yalnızca atomik UPDATE'i KAZANAN çağrıda döner. İki worker
    aynı talebi aynı anda görürse ikincisi 'zaten_atanmis' ile no-op olur —
    kullanıcıya çift bildirim gitmez, ticket iki kişiye atanmaz.
    """
    ticket_id = ticket["id"]

    # Worker yalnızca kuyruktaki talebe bakar; atanmış/çözülmüş olana dokunmaz.
    if ticket.get("status") != config.STATUS_ESCALATION_PENDING:
        return _decision(ticket_id, None, False, None, "kuyrukta_degil")
    if ticket.get("assigned_technician_id"):
        return _decision(ticket_id, None, False, None, "zaten_atanmis")

    # MERKEZİ motor — hangi yolun (skill / departman) geçerli olduğuna,
    # priority fallback'e, workload ve fairness sıralamasına ORADA karar verilir.
    # ACCEPTANCE FIX 2: uzmanlığı belirlenememiş talepler artık kuyrukta
    # ÖLMÜYOR; departman fallback'i de aynı adil sıralamayla değerlendiriliyor.
    result = routing.select_for_ticket(ticket)

    if result.get("mode") == routing.MODE_MANUAL_TRIAGE:
        # ACCEPTANCE UX + ROUTING REVISION §27: MANUAL_TRIAGE kuyruğunda
        # worker OTOMATİK ATAMA YAPMAZ. Sınıflandırma belirsiz kaldığı için
        # "uygun görünen birine" verilmesi rastgele atama olurdu. Talep
        # kuyrukta kalır ve admin panelinde 'Manuel sınıflandırma gerekiyor'
        # olarak görünür. GENEL DESTEĞE DE ÇEVRİLMEZ.
        db.touch_candidate_check(ticket_id)
        return _decision(ticket_id, result, False, None, "manuel_triyaj_bekliyor")

    if result.get("mode") == routing.MODE_NONE:
        # Ne uzmanlık ne departman var -> otomatik yönlendirilemez.
        # Talep kuyrukta kalır ve panelden manuel atanır.
        db.touch_candidate_check(ticket_id)
        return _decision(ticket_id, result, False, None,
                         "uzmanlik_ve_departman_yok")

    if not result["selected"]:
        # Aday yok: talep kuyrukta KALIR. Yalnızca 'son kontrol' damgası atılır.
        db.touch_candidate_check(ticket_id)
        return _decision(ticket_id, result, False, None, "aday_yok")

    selected = result["selected"]
    technician = db.get_technician(selected["technician_id"])
    if not technician:
        db.touch_candidate_check(ticket_id)
        return _decision(ticket_id, result, False, None, "teknisyen_bulunamadi")

    # --- ATOMİK PRIMARY ataması (concurrency koruması burada) ---
    # ai_mode = HUMAN_ASSISTED ve SLA başlangıcı bu fonksiyonun içinde korunur.
    assigned = db.assign_primary_if_unassigned(
        ticket_id, technician["id"], technician["name"],
        actor={"source": config.ASSIGN_SOURCE_SYSTEM,
               "label": "Otomatik uzman yönlendirmesi"},
    )
    if not assigned:
        # Yarışı başka bir worker/işlem kazandı -> NO-OP.
        db.touch_candidate_check(ticket_id)
        return _decision(ticket_id, result, False, None, "zaten_atanmis")

    db.add_message(
        ticket_id, "sistem", 0, "Sistem",
        f"{technician['name']} talebe otomatik olarak ana sorumlu atandı. "
        f"({routing.selection_reason(result)})",
        message_kind=config.MESSAGE_KIND_SYSTEM,
    )
    logger.info("Talep #%s otomatik atandi (teknisyen #%s, yol=%s).",
                ticket_id, technician["id"], result.get("mode"))
    return _decision(ticket_id, result, True, technician, "otomatik_atandi")


def _decision(ticket_id, result, assigned, technician, reason):
    return {"ticket_id": ticket_id, "result": result, "assigned": assigned,
            "technician": technician, "reason": reason}


def reevaluate_waiting_tickets(skill_code: str = None, department: str = None,
                               basis: str = None):
    """Bekleyen talepleri yeniden değerlendirir; atama kararlarını döner.

    Filtre verilmezse bütün kuyruk taranır (queue worker turu böyle çalışır).

    skill_code -> yalnızca o uzmanlığı bekleyen talepler
                  (SPECIFIC_SKILL ve GENERAL_SUPPORT dayanakları)
    department -> yalnızca DEPARTMENT dayanağıyla o departmanı bekleyen talepler
    basis      -> yalnızca o yönlendirme dayanağındaki talepler

    ACCEPTANCE UX + ROUTING REVISION §27 — KUYRUKLAR AYRIKTIR:
      SPECIFIC_SKILL kuyruğu  -> yalnızca o uzmanlık yeniden değerlendirilir
      DEPARTMENT kuyruğu      -> yalnızca o departman yeniden değerlendirilir
      GENERAL_SUPPORT kuyruğu -> yalnızca GENERAL_TECH_SUPPORT değerlendirilir
      MANUAL_TRIAGE           -> otomatik atama YOK, admin incelemesi bekler

    Filtreler AYRIK kümeler döndürür (bkz. db.list_waiting_expert_tickets);
    aynı talep iki kez değerlendirilmez ve bir kuyruktaki talep aday
    bulunamadı diye BAŞKA bir kuyruğa geçmez.

    İŞLEME SIRASI (§C) — db.list_waiting_expert_tickets zaten
    priority severity -> queue_entered_at ASC -> ticket_id ASC ile getirir.
    Talepler TEK TEK, sırayla değerlendirilir; başta toplu bir teknisyen
    snapshot'ı ALINMAZ (§C4). Her evaluate_ticket() çağrısı routing motorunu
    yeniden çalıştırır, o da iş yükünü DB'den TAZE okur — böylece bir önceki
    talebin ataması bir sonrakinin adalet sıralamasına yansır."""
    decisions = []
    for ticket in db.list_waiting_expert_tickets(skill_code=skill_code,
                                                 department=department,
                                                 basis=basis):
        decisions.append(evaluate_ticket(ticket))
    return decisions


def collect_waiting_tickets(scans):
    """Birden fazla kuyruk taramasını TEK, deterministik sıraya indirger.

    scans: db.list_waiting_expert_tickets(**kwargs) çağrılarının sonuç listeleri.

    Neden gerekli: bir teknisyen müsait olduğunda kendi uzmanlıkları ve
    departmanı AYRI sorgularla taranır. Her sorgu kendi içinde doğru sırada
    gelir ama listeler PEŞ PEŞE eklenirse "ikinci uzmanlıktaki P1" birinci
    uzmanlıktaki P4'ün ARKASINA düşerdi. Burada hepsi birleştirilip
    db.queue_order_key ile yeniden sıralanır; sıralama tanımı TEK yerdedir.

    Aynı talep birden fazla taramada görünürse (kuyruklar ayrık olduğu için
    beklenmez, ama savunmacı davranıyoruz) yalnızca BİR KEZ değerlendirilir."""
    seen = set()
    tickets = []
    for scan in scans:
        for ticket in scan:
            if ticket["id"] in seen:
                continue
            seen.add(ticket["id"])
            tickets.append(ticket)
    tickets.sort(key=db.queue_order_key)
    return tickets


def reevaluate_for_technician(technician_id: int):
    """Bir teknisyen müsait hâle geldiğinde ÇAĞRILIR.

    İKİ kuyruk taranır:
      1. kişinin AKTİF uzmanlıklarını bekleyen talepler (skill yolu)
      2. kişinin departmanını bekleyen, uzmanlığı belirlenememiş talepler
         (ACCEPTANCE FIX 2 — departman fallback yolu)

    İlgisiz kuyruk satırları taranmaz; bütün DB gezilmez.

    DİKKAT: müsait olan kişi otomatik olarak SEÇİLEN kişi DEĞİLDİR. Kuyruk
    yeniden değerlendirilir ve merkezi sıralama kimi seçiyorsa o atanır —
    aksi hâlde 'butona son basan işi kapar' gibi adaletsiz bir davranış olurdu.
    Bu, ACCEPTANCE FIX 2'nin team_toggle maddesinin özüdür.

    İŞLEME SIRASI (§C): iki tarama önce TOPLANIR, sonra tek bir
    priority + FIFO sırasına indirgenir. Eskiden listeler peş peşe
    değerlendiriliyordu; o hâlde ikinci uzmanlıktaki bir P1, birinci
    uzmanlıktaki bir P4'ten SONRA işlenirdi. Kuyruklar hâlâ AYRIKTIR —
    yalnızca hangi talebin önce ele alınacağı ortak kurala bağlanır.

    §C4: her talep tek tek değerlendirilir; iş yükü her atamadan sonra
    yeniden okunur, başta toplu snapshot alınmaz."""
    scans = []
    for skill in db.list_technician_skills(technician_id):
        if not skill.get("skill_active"):
            continue
        scans.append(db.list_waiting_expert_tickets(skill_code=skill["skill_code"]))

    technician = db.get_technician(technician_id)
    if technician and technician.get("department"):
        scans.append(db.list_waiting_expert_tickets(
            department=technician["department"]))

    return [evaluate_ticket(ticket) for ticket in collect_waiting_tickets(scans)]


def routing_basis_label(result: dict) -> str:
    """Kullanıcıya gösterilecek yönlendirme dayanağı.

    Skill yolunda uzmanlık adı (GENEL destek dahil — 'Genel Teknik Destek'),
    departman yolunda departman adı. İÇ OPERASYON DETAYI (iş yükü, sıralama
    gerekçesi) BURAYA GİRMEZ."""
    result = result or {}
    if result.get("mode") == routing.MODE_DEPARTMENT:
        return result.get("department") or "teknik destek"
    return (result.get("skill") or {}).get("name") or "teknik destek"


def assignment_text(ticket: dict, result: dict, technician: dict) -> str:
    """Kullanıcıya gidecek BİLGİLENDİRME metni. AYNI TALEP kullanılır.

    Kullanıcıya seçim SUNULMAZ ve iç operasyon detayı (iş yükü, öncelik
    seviyesi, neden bu kişi seçildi) GÖSTERİLMEZ.

    §19: talep etiketi telegram_ui.format_ticket_label() ile üretilir —
    kullanıcıya `#42` GÖSTERİLMEZ."""
    is_department = (result or {}).get("mode") == routing.MODE_DEPARTMENT
    return telegram_ui.assignment_notice(
        ticket["id"], routing_basis_label(result), technician["name"],
        is_department=is_department)


def technician_notification_text(ticket: dict, result: dict) -> str:
    """Atanan teknisyene giden KISA bildirim (tüm konuşma Telegram'a dökülmez).

    Teknisyen İÇ OPERASYON tarafındadır; burada `#42` biçimi korunur."""
    is_department = (result or {}).get("mode") == routing.MODE_DEPARTMENT
    label = "Departman" if is_department else "Uzmanlık"
    return (
        f"🆕 Yeni talep #{ticket['id']} size atandı.\n\n"
        f"Konu: {ticket['subject']}\n"
        f"{label}: {routing_basis_label(result)}\n"
        f"Kullanıcı: {ticket.get('full_name') or '—'}\n\n"
        "Geçmişin tamamı web panelinde."
    )


def waiting_reason(ticket: dict) -> dict:
    """§28: admin panelinde 'neden atanmadı?' ayrımı.

    Döner: {"basis", "label", "target", "text"}
        basis  -> SPECIFIC_SKILL | DEPARTMENT | GENERAL_SUPPORT | MANUAL_TRIAGE
        label  -> 'Uzman bekliyor' / 'Departman bekliyor' / ...
        target -> beklenen uzmanlık ya da departman adı ('' olabilir)
        text   -> panelde gösterilecek tam satır

    Bu, kuyruk mantığını DEĞİL yalnızca SUNUMU üretir; karar
    routing.effective_routing_basis() içindedir."""
    basis = routing.effective_routing_basis(ticket) or routing.BASIS_MANUAL_TRIAGE
    label = routing.basis_label(basis)
    target = routing.routing_target_name(ticket)
    return {"basis": basis, "label": label, "target": target,
            "text": f"{label}: {target}" if target else label}
