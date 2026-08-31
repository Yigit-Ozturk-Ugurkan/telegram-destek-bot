"""Uzmanlık (skill) tabanlı aday bulma ve OTOMATİK SEÇİM motoru.

Bu modül DETERMİNİSTİK çalışır ve Gemini ÇAĞIRMAZ.

Faz 3A'da yalnızca aday listesi üretiyordu; seçimi son kullanıcı yapıyordu.
ACCEPTANCE REVISION ile o davranış KALDIRILDI: teknik personel seçimi
kullanıcının değil SİSTEMİN sorumluluğudur.

TEK MERKEZİ ALGORİTMA: bot, queue worker ve web aynı fonksiyonu çağırır —
select_for_ticket(). Kimse kendi fairness algoritmasını yazmaz.

DÖRT SONUÇ, TEK ADALET KURALI (ACCEPTANCE UX + ROUTING REVISION)
----------------------------------------------------------------

Sınıflandırmanın sonucu dört mantıksal dayanaktan biridir:

    SPECIFIC_SKILL   geçerli + AKTİF uzmanlık kodu   (en yüksek öncelik)
    DEPARTMENT       uzmanlık yok ama departman var
    GENERAL_SUPPORT  model AÇIKÇA "genel teknik destek" dedi
    MANUAL_TRIAGE    sınıflandırma başarısız / belirsiz / geçersiz

GENERAL_TECH_SUPPORT BİR CATCH-ALL DEĞİLDİR. Belirsizlik, katalogda olmayan
kod, pasif uzmanlık, bozuk JSON, timeout ve "aday bulunamadı" durumlarının
HİÇBİRİ genel desteğe dönüşmez (bkz. resolve_classification, select_for_ticket).

ROUTING FALLTHROUGH YASAKTIR: aday bulunamaması ASLA başka bir dayanağa
geçiş sebebi değildir. Fallback yalnızca SINIFLANDIRMA seviyesinde yapılır.

A) SKILL ROUTING (required_skill üretildiyse — SPECIFIC_SKILL / GENERAL_SUPPORT):

    1. required_skill eşleşmesi
    2. technician available = 1
    3. EN İYİ skill priority (1 varsa 2 ve 3 HİÇ değerlendirilmez)
    ---- yalnızca en iyi priority seviyesindeki adaylar arasında ----
    4. active_ticket_count ASC          (en az yüklü)
    5. last_primary_assignment_at ASC   (en uzun süredir yeni ana iş almayan;
                                         hiç almamış olan en önde)
    6. technician_id ASC                (deterministik final tie-break)

B) DEPARTMAN ROUTING (ACCEPTANCE FIX 2 — required_skill üretilemediyse):

    1. department eşleşmesi
    2. technician available = 1
    3. active_ticket_count ASC
    4. last_primary_assignment_at ASC
    5. technician_id ASC

Yani 3-6 (skill) ile 3-5 (departman) AYNI fairness_key'dir; tek fark skill
yolunun önüne uzmanlık önceliğinin eklenmesidir. "Departmandaki ilk müsait
kişi" davranışı (db.find_available_technician) ARTIK HİÇBİR atama yolunda
kullanılmaz.

Bu modül ATAMA YAPMAZ — yalnızca "kim seçilmeli" sorusunu yanıtlar. Atomik
PRIMARY ataması db.assign_primary_if_unassigned() ile çağıran tarafta yapılır.
"""

import config
import db

# Hangi routing yolunun kullanıldığını ifade eder. Çağıran taraf (bot / worker /
# web) log ve bildirim metnini buna göre seçer; kendi if/else routing mantığını
# YAZMAZ.
MODE_SKILL = "skill"            # required_skill üretildi -> uzmanlık önceliği devrede
MODE_DEPARTMENT = "department"  # skill üretilemedi -> departman fallback
MODE_NONE = "none"              # ne skill ne departman -> talep kuyrukta kalır
# ACCEPTANCE UX + ROUTING REVISION: sınıflandırma başarısız/belirsiz.
# Otomatik atama YAPILMAZ; admin incelemesi beklenir.
MODE_MANUAL_TRIAGE = "manual_triage"

# `mode` teknik YOL'u (hangi SQL/sıralama kullanıldı) söyler; `routing_basis`
# ise İŞ KARARINI (talep neden bu kuyrukta) söyler. İkisi aynı şey DEĞİLDİR:
# GENERAL_SUPPORT teknik olarak bir skill yoludur (mode = MODE_SKILL) ama iş
# kararı olarak ayrı bir dayanaktır. Panel, kuyruk ve bildirimler basis'i
# kullanır; sıralama/sorgu mantığı mode'u kullanır.
BASIS_SPECIFIC_SKILL = config.ROUTING_BASIS_SPECIFIC_SKILL
BASIS_DEPARTMENT = config.ROUTING_BASIS_DEPARTMENT
BASIS_GENERAL_SUPPORT = config.ROUTING_BASIS_GENERAL_SUPPORT
BASIS_MANUAL_TRIAGE = config.ROUTING_BASIS_MANUAL_TRIAGE


def _basis_for_skill_code(skill_code: str) -> str:
    """Uzmanlık kodundan İŞ KARARINI türetir.

    GENERAL_TECH_SUPPORT bir skill'dir ama ayrı bir dayanaktır. Bunu koddan
    TÜRETİYORUZ, çağırandan parametre olarak ALMIYORUZ — böylece bir çağıran
    yanlışlıkla (ya da kasıtlı olarak) rastgele bir uzmanlığı "genel destek"
    diye etiketleyemez."""
    code = db.normalize_skill_code(skill_code)
    if code == config.GENERAL_SUPPORT_SKILL_CODE:
        return BASIS_GENERAL_SUPPORT
    return BASIS_SPECIFIC_SKILL


def _empty_result(skill=None, department=None, department_filter_applied=False,
                  basis=BASIS_SPECIFIC_SKILL):
    return {
        "mode": MODE_SKILL,
        "routing_basis": basis,
        "skill": skill,
        "matched_priority": None,
        "candidates": [],
        "recommended_technician_id": None,
        "department": department,
        "department_filter_applied": department_filter_applied,
    }


def _public_skill(skill: dict):
    """Sonuçta yalnızca anlamlı alanları taşı."""
    if not skill:
        return None
    return {"id": skill["id"], "code": skill["code"], "name": skill["name"]}


# ------------------------------------------------------------------
# Sıralama anahtarları — OTOMATİK SEÇİMİN TEK TANIMI
# ------------------------------------------------------------------

# Hiç PRIMARY iş almamış kişinin last_primary_assignment_at değeri NULL'dır.
# Bu kişi "en uzun süredir yeni iş almamış" kabul edilir ve eşit iş yükünde
# SIRANIN BAŞINA geçer. Bunu ifade etmek için karşılaştırmada önce 0/1 bayrağı
# kullanıyoruz (None ile str karşılaştırması Python'da hata verirdi).

def fairness_key(candidate: dict):
    """(iş yükü, hiç-almamış-mı, son atama, id) — ADALET SIRASININ TEK TANIMI.

    Hem skill routing hem departman fallback bunu kullanır. İki yolun tek
    farkı, skill yolunun önüne uzmanlık önceliğini eklemesidir; iş yükü ve
    least-recently-assigned davranışı İKİSİNDE DE AYNIDIR."""
    last = candidate.get("last_primary_assignment_at")
    return (
        candidate["active_ticket_count"],
        0 if last is None else 1,
        last or "",
        candidate["technician_id"],
    )


def selection_key(candidate: dict):
    """(priority, iş yükü, hiç-almamış-mı, son atama, id) — SKILL yolu.

    db.list_skill_candidates()'in ORDER BY'ı ile BİREBİR aynı sırayı üretir.
    İkisi birlikte değiştirilmelidir; ikinci bir sıralama tanımı yazmayın."""
    return (candidate["priority"],) + fairness_key(candidate)


def find_candidates_for_skill(skill_code: str, department: str = None):
    """Verilen uzmanlık için uygun teknisyen adaylarını bulur.

    Akış:
        required_skill
              -> skill var mı ve AKTİF mi?
              -> bu skill'e sahip teknisyenler
              -> available = 1 (yeni iş alabilir)
              -> en iyi (en küçük) priority seviyesini bul
              -> YALNIZCA o seviyedeki adayları döndür
              -> selection_key ile sırala (iş yükü -> son atama -> id)

    ALT SEVİYE API'DİR: adayları sıralı döndürür ama seçimi ifade etmez.
    Çağıran taraflar select_best_technician_for_skill() kullanmalıdır —
    böylece "kim seçilir" sorusunun tek bir yanıtı olur.

    department (opsiyonel) YUMUŞAK filtredir: en iyi priority seviyesinde
    departmana uyan aday varsa liste onlara daraltılır; hiç yoksa departman
    filtresi UYGULANMAZ ve adaylar korunur. Böylece aynı uzmanlık başka
    departmanda da bulunuyorsa aday tamamen kaybedilmez.

    Dönen sözlük:
        {
          "skill": {"id":.., "code":.., "name":..} | None,
          "matched_priority": 1|2|3|None,
          "candidates": [ {technician_id, name, department, skill_code,
                           priority, active_ticket_count,
                           last_primary_assignment_at}, ... ],
          "recommended_technician_id": int|None,   # == candidates[0], geriye dönük ad
          "department": <istenen departman|None>,
          "department_filter_applied": bool
        }
    """
    # Dayanak İSTENEN koddan türetilir: skill katalogda bulunamasa bile
    # sonucun hangi kuyruğa ait olduğu bilinir ve talep başka bir kuyruğa
    # KAYMAZ (bkz. select_for_ticket — fallthrough yoktur).
    basis = _basis_for_skill_code(skill_code)

    skill = db.get_skill_by_code(skill_code)
    if skill is None or not skill["active"]:
        # Bilinmeyen ya da PASİF uzmanlık -> boş sonuç (hata fırlatmıyoruz).
        # ÖNEMLİ: bu durum GENEL DESTEĞE ya da departmana DÜŞÜŞ DEĞİLDİR;
        # talep kendi kuyruğunda bekler, admin manuel yönlendirir.
        return _empty_result(_public_skill(skill), department, basis=basis)

    # TEK sorgu: skill eşleşmesi + available filtresi + iş yükü (JOIN/GROUP BY).
    rows = db.list_skill_candidates(skill["id"])
    if not rows:
        return _empty_result(_public_skill(skill), department, basis=basis)

    # --- ÖNCELİK FALLBACK: 1 -> 2 -> 3 ---
    # En iyi seviyede en az bir aday varsa, alt seviyeler HİÇ dahil edilmez.
    best_priority = min(r["priority"] for r in rows)
    pool = [r for r in rows if r["priority"] == best_priority]

    # --- Departman: yumuşak daraltma ---
    department_filter_applied = False
    if department:
        same_department = [r for r in pool if r["department"] == department]
        if same_department:
            pool = same_department
            department_filter_applied = True

    candidates = [
        {
            "technician_id": r["technician_id"],
            "name": r["name"],
            "department": r["department"],
            "skill_code": skill["code"],
            "priority": r["priority"],
            "active_ticket_count": r["active_ticket_count"],
            "last_primary_assignment_at": r.get("last_primary_assignment_at"),
        }
        for r in pool
    ]

    # SQL zaten aynı sırayla getiriyor; departman daraltmasından sonra da sıranın
    # korunduğunu Python tarafında GARANTİ ediyoruz (tek sıralama tanımı).
    candidates.sort(key=selection_key)

    # candidates[0] = otomatik atanacak kişi. Kullanıcıya seçim SUNULMAZ.
    recommended = candidates[0]["technician_id"] if candidates else None

    return {
        "mode": MODE_SKILL,
        "routing_basis": basis,
        "skill": _public_skill(skill),
        "matched_priority": best_priority,
        "candidates": candidates,
        "recommended_technician_id": recommended,
        "department": department,
        "department_filter_applied": department_filter_applied,
    }


def select_best_technician_for_skill(skill_code: str, department: str = None):
    """MERKEZİ OTOMATİK SEÇİM — bot, queue worker ve web bunu kullanır.

    Yük dağıtımı sistemin sorumluluğudur; son kullanıcıya teknik personel
    SEÇTİRİLMEZ. Bu fonksiyon ATAMA YAPMAZ, yalnızca kimin seçileceğini söyler;
    atomik PRIMARY ataması çağıran tarafta db.assign_primary_if_unassigned()
    ile yapılır (böylece concurrency koruması tek yerde kalır).

    Dönen sözlük find_candidates_for_skill() sonucunun ÜSTÜNE şunu ekler:
        "selected": <seçilen aday dict'i> | None

    Aday yoksa selected None'dır ve çağıran taraf talebi kuyrukta bırakır
    (status = eskalasyon_bekliyor). Talep KAYBOLMAZ."""
    result = find_candidates_for_skill(skill_code, department=department)
    result["selected"] = result["candidates"][0] if result["candidates"] else None
    return result


def _empty_department_result(department=None):
    return {
        "mode": MODE_DEPARTMENT,
        "routing_basis": BASIS_DEPARTMENT,
        "skill": None,
        "matched_priority": None,
        "department": department,
        "candidates": [],
        "selected": None,
        "recommended_technician_id": None,
        "department_filter_applied": bool(department),
    }


def find_candidates_for_department(department: str):
    """DEPARTMAN FALLBACK adayları — skill sınıflandırması başarısız olduğunda.

    ACCEPTANCE FIX 2: bu yol eskiden db.find_available_technician() ile
    "departmandaki İLK müsait kişi"yi seçiyordu. O davranış KALDIRILDI.

    Akış:
        department
              -> departmanı eşleşen teknisyenler
              -> available = 1 (yeni iş alabilir)
              -> fairness_key ile sırala

    Skill priority YOKTUR (required_skill üretilememiştir), ama iş yükü ve
    least-recently-assigned kuralları skill yoluyla BİREBİR AYNIDIR."""
    if not department:
        return _empty_department_result(department)

    rows = db.list_department_candidates(department)
    if not rows:
        return _empty_department_result(department)

    candidates = [
        {
            "technician_id": r["technician_id"],
            "name": r["name"],
            "department": r["department"],
            "skill_code": None,
            # Departman yolunda uzmanlık seviyesi yoktur. None bırakmak
            # selection_key'i kullanılamaz hâle getirirdi; bu yüzden bu yol
            # KASITLI olarak fairness_key kullanır.
            "priority": None,
            "active_ticket_count": r["active_ticket_count"],
            "last_primary_assignment_at": r["last_primary_assignment_at"],
        }
        for r in rows
    ]

    # SQL zaten aynı sırayla getiriyor; Python tarafında da garanti ediyoruz.
    candidates.sort(key=fairness_key)

    return {
        "mode": MODE_DEPARTMENT,
        "routing_basis": BASIS_DEPARTMENT,
        "skill": None,
        "matched_priority": None,
        "department": department,
        "candidates": candidates,
        "selected": candidates[0],
        "recommended_technician_id": candidates[0]["technician_id"],
        "department_filter_applied": True,
    }


def select_best_technician_for_department(department: str):
    """MERKEZİ DEPARTMAN FALLBACK SEÇİMİ.

    select_best_technician_for_skill() ile aynı sözleşme: ATAMA YAPMAZ,
    yalnızca "kim seçilmeli" sorusunu yanıtlar. Atomik PRIMARY ataması
    çağıran tarafta db.assign_primary_if_unassigned() ile yapılır."""
    return find_candidates_for_department(department)


# ==================================================================
# ACCEPTANCE UX + ROUTING REVISION — SINIFLANDIRMA DOĞRULAMASI
# ==================================================================

def _manual_triage_result(reason: str = None, department: str = None):
    """Otomatik yönlendirilemeyen talep. ADAY ARANMAZ, ATAMA YAPILMAZ.

    Bu bir "hata" değil, geçerli bir sonuçtur: talep eskalasyon_bekliyor
    durumunda kalır ve admin panelinde 'Manuel sınıflandırma gerekiyor'
    olarak görünür. GENEL DESTEĞE ÇEVRİLMEZ."""
    return {
        "mode": MODE_MANUAL_TRIAGE,
        "routing_basis": BASIS_MANUAL_TRIAGE,
        "skill": None,
        "matched_priority": None,
        "department": department,
        "candidates": [],
        "selected": None,
        "recommended_technician_id": None,
        "department_filter_applied": False,
        "reason": reason,
    }


def resolve_classification(classification: dict, fallback_department: str = None):
    """MODEL ÇIKTISINI -> DOĞRULANMIŞ YÖNLENDİRME DAYANAĞINA çevirir.

    SERVER-SIDE VALIDATION ZORUNLUDUR. Model SOURCE OF TRUTH DEĞİLDİR:
    uzmanlık kodu katalogda aranır ve AKTİF olup olmadığına bakılır,
    departman config.DEPARTMENTS'a karşı doğrulanır, classification_type
    bilinen değerlere indirgenir. Uydurulmuş kod DB'ye YAZILMAZ.

    KARAR ZİNCİRİ (§11) — her adım STOP'lar, aşağı düşme YOKTUR:

      A) Geçerli ve AKTİF, GENEL OLMAYAN bir uzmanlık kodu varsa
         -> SPECIFIC_SKILL. Bu EN YÜKSEK önceliktir: model tipi ne derse
            desin, elimizde gerçek bir uzmanlık varsa onu kullanırız.

      B) Model AÇIKÇA classification_type = "general_support" dediyse ve
         GENERAL_TECH_SUPPORT katalogda AKTİF ise
         -> GENERAL_SUPPORT. (Skill katalogda yoksa/pasifse MANUAL_TRIAGE;
            rastgele bir departmana KAÇIRILMAZ.)

      C) Geçerli bir departman varsa -> DEPARTMENT.

      D) Aksi hâlde -> MANUAL_TRIAGE.

    ÇOK ÖNEMLİ — ŞUNLARIN HİÇBİRİ GENERAL_TECH_SUPPORT ANLAMINA GELMEZ:
        required_skill = null         -> B'ye değil, C/D'ye gider
        katalogda olmayan kod         -> doğrulama düşer, C/D'ye gider
        pasif uzmanlık                -> doğrulama düşer, C/D'ye gider
        JSON parse hatası / timeout   -> ok=False -> doğrudan MANUAL_TRIAGE
        classification_type=uncertain -> doğrudan MANUAL_TRIAGE
    "Model emin değil" ile "model genel destek dedi" AYNI ŞEY DEĞİLDİR.

    fallback_department: talebin ÜZERİNDE zaten duran kategori. Model geçerli
    bir kategori üretmediyse bu kullanılır — ama YALNIZCA sınıflandırma
    teknik olarak başarılıysa. Başarısız sınıflandırma eski kategoriyle
    "kurtarılmaz"; o talep manuel triyaja gider.

    PRIORITY ROUTING REVISION — ÖNCELİK DE BURADA DOĞRULANIR:
    dönen "priority" HER ZAMAN geçerli bir P1-P4 değeridir. Model geçersiz,
    boş ya da hiç değer üretmediyse config.DEFAULT_PRIORITY (P3) döner.
    Sınıflandırma teknik olarak başarısızsa (ok=False) öncelik ÖNERİSİ DE
    DİKKATE ALINMAZ — bozuk bir yanıtın bir yarısına güvenip diğerine
    güvenmemek tutarsız olurdu.

    Döner:
        {"routing_basis", "skill_code", "department", "classification_type",
         "reason", "priority", "priority_reason"}
    skill_code her zaman DOĞRULANMIŞ katalog kodudur ya da None'dır."""
    classification = classification or {}

    department = _valid_department(classification.get("category"))
    ctype = _valid_classification_type(classification.get("classification_type"))

    # Sınıflandırma teknik olarak başarısız (Gemini kapalı / timeout / bozuk
    # JSON / boş yanıt). Eski kategoriye tutunup departman routing'i YAPMAYIZ.
    if not classification.get("ok", True):
        # Öncelik de fallback'e (P3) düşer; gerekçe YAZILMAZ.
        return _decision(BASIS_MANUAL_TRIAGE, None, None, ctype,
                         "classification_failed")

    if department is None:
        department = _valid_department(fallback_department)

    # SERVER-SIDE PRIORITY VALIDATION. Model burada SOURCE OF TRUTH DEĞİLDİR:
    # geçersiz/eksik değer sessizce P3'e düşer, uydurma öncelik DB'ye YAZILMAZ.
    priority = _validated_priority(classification.get("suggested_priority"))
    priority_reason = _validated_priority_reason(
        classification.get("priority_reason"))

    # --- A) SPECIFIC SKILL — en yüksek öncelik ---
    validated = _validated_skill_code(classification.get("required_skill"))
    if validated and validated != config.GENERAL_SUPPORT_SKILL_CODE:
        return _decision(BASIS_SPECIFIC_SKILL, validated, department, ctype,
                         "validated_specific_skill", priority, priority_reason)

    # --- Model AÇIKÇA "emin değilim" dedi -> departmana bile düşürmeyiz ---
    if ctype == config.CLASSIFICATION_UNCERTAIN:
        return _decision(BASIS_MANUAL_TRIAGE, None, department, ctype,
                         "model_uncertain", priority, priority_reason)

    # --- B) GENERAL SUPPORT — YALNIZCA açık beyanla ---
    if ctype == config.CLASSIFICATION_GENERAL_SUPPORT:
        general = db.get_skill_by_code(config.GENERAL_SUPPORT_SKILL_CODE)
        if general and general["active"]:
            return _decision(BASIS_GENERAL_SUPPORT, general["code"], department,
                             ctype, "explicit_general_support",
                             priority, priority_reason)
        # Genel destek uzmanlığı katalogda yok/pasif: talebi rastgele bir
        # departmana atmak yerine admin incelemesine bırakırız.
        return _decision(BASIS_MANUAL_TRIAGE, None, department, ctype,
                         "general_support_skill_unavailable",
                         priority, priority_reason)

    # --- C) DEPARTMAN ---
    if department:
        return _decision(BASIS_DEPARTMENT, None, department, ctype,
                         "validated_department", priority, priority_reason)

    # --- D) MANUEL TRİYAJ ---
    return _decision(BASIS_MANUAL_TRIAGE, None, None, ctype,
                     "no_skill_no_department", priority, priority_reason)


def _decision(basis, skill_code, department, classification_type, reason,
              priority=None, priority_reason=None):
    """Doğrulanmış yönlendirme kararı.

    priority ALANI HER ZAMAN DOLUDUR ve HER ZAMAN geçerli bir config.PRIORITIES
    değeridir — çağıran taraf None kontrolü yapmak zorunda kalmasın diye
    doğrulama burada, tek yerde bitirilir."""
    return {
        "routing_basis": basis,
        "skill_code": skill_code,
        "department": department,
        "classification_type": classification_type,
        "reason": reason,
        # PRIORITY ROUTING REVISION: model önerisi DOĞRULANMIŞ hâliyle taşınır.
        "priority": priority or config.DEFAULT_PRIORITY,
        "priority_reason": priority_reason,
    }


def _valid_department(value):
    """config.DEPARTMENTS dışındaki her şey None'dır. Model uyduramaz."""
    value = (value or "").strip()
    return value if value in config.DEPARTMENTS else None


def _valid_classification_type(value):
    value = (value or "").strip().lower()
    return value if value in config.CLASSIFICATION_TYPES else None


def _validated_priority(raw_priority):
    """MODEL ÖNCELİK ÖNERİSİNİ -> GEÇERLİ P1-P4'e indirger.

    SERVER-SIDE VALIDATION — PRIORITY ROUTING REVISION §B2.
    Gemini priority için SOURCE OF TRUTH DEĞİLDİR. Yalnızca P1/P2/P3/P4
    kabul edilir; aşağıdakilerin HEPSİ config.DEFAULT_PRIORITY'ye (P3) düşer:

        None / boş                 -> P3
        "P0", "URGENT", "yüksek"   -> P3   (katalog dışı değer)
        5, {}, []                  -> P3   (tip hatası)
        bozuk JSON / timeout       -> zaten None gelir -> P3

    Küçük harf ve baştaki/sondaki boşluk TOLERE EDİLİR ("p1" -> "P1"); bu bir
    biçim düzeltmesidir, uydurma değer üretmez."""
    if raw_priority is None:
        return config.DEFAULT_PRIORITY
    if not isinstance(raw_priority, str):
        return config.DEFAULT_PRIORITY
    code = raw_priority.strip().upper()
    return code if code in config.PRIORITIES else config.DEFAULT_PRIORITY


def _validated_priority_reason(raw_reason):
    """Model gerekçesini panelde gösterilebilir tek satıra indirger.

    İÇ OPERASYON metnidir: yalnızca admin/teknik ekip panelinde gösterilir,
    son kullanıcıya (Telegram) GÖNDERİLMEZ. Uzunluk sınırı DB şişmesini ve
    panelde taşan metni engeller."""
    if not isinstance(raw_reason, str):
        return None
    reason = " ".join(raw_reason.split())
    if not reason:
        return None
    return reason[:config.PRIORITY_REASON_MAX_LENGTH]


def _validated_skill_code(raw_skill):
    """Ham model çıktısını katalog + aktiflik kontrolünden geçirir.

    Katalogda yoksa ya da pasifse None döner — ve bu None, GENEL DESTEK
    ANLAMINA GELMEZ (bkz. resolve_classification)."""
    if not raw_skill:
        return None
    code = str(raw_skill).strip()
    if code.upper() in ("NULL", "NONE", ""):
        return None
    skill = db.get_skill_by_code(code)
    if skill and skill["active"]:
        return skill["code"]
    return None


def effective_routing_basis(ticket: dict) -> str:
    """Talebin YÜRÜRLÜKTEKİ yönlendirme dayanağı.

    tickets.routing_basis yazılıysa o kullanılır. Revizyondan ÖNCEKİ
    kayıtlarda bu alan NULL'dır; onlar için dayanak mevcut alanlardan
    TÜRETİLİR (skill kodu -> skill kuyruğu, yoksa departman -> departman
    kuyruğu). Eski taleplerin davranışı değişmez, geriye dönük veri yazımı
    yapılmaz.

    Türetilemeyen eski kayıt için None döner — çağıran taraf bunu MODE_NONE
    olarak ele alır (mevcut davranış korunur)."""
    stored = (ticket.get("routing_basis") or "").strip().upper()
    if stored in config.ROUTING_BASES:
        return stored

    skill_code = (ticket.get("required_skill_code") or "").strip()
    if skill_code:
        return _basis_for_skill_code(skill_code)
    if ticket.get("pending_department") or ticket.get("category"):
        return BASIS_DEPARTMENT
    return None


def select_for_ticket(ticket: dict):
    """TALEP İÇİN TEK GİRİŞ NOKTASI — bot, queue worker ve web bunu kullanır.

    Hangi yolun geçerli olduğuna BURADA karar verilir; çağıranlar kendi
    if/else routing mantığını YAZMAZ.

    ROUTING FALLTHROUGH YASAKTIR (§12)
    ----------------------------------
    Bu fonksiyon TEK BİR dayanağı değerlendirir ve DURUR. Aday bulunamaması
    ASLA başka bir dayanağa geçiş sebebi değildir:

        SPECIFIC_SKILL  + 0 aday -> SPECIFIC_SKILL kuyruğunda bekler
        DEPARTMENT      + 0 aday -> DEPARTMENT kuyruğunda bekler
        GENERAL_SUPPORT + 0 aday -> GENERAL kuyruğunda bekler
        MANUAL_TRIAGE            -> hiç aday aranmaz, admin bekler

    Yani "COMPUTER_VISION uzmanı yok" cümlesi "öyleyse genel destek baksın"
    ya da "öyleyse departmandan biri baksın" anlamına GELMEZ. Talebin hangi
    uzmanlığa ait olduğu biliniyorsa, o uzman müsait olana kadar bekler.
    Fallback YALNIZCA sınıflandırma seviyesinde (resolve_classification)
    yapılır — aday bulunamaması seviyesinde DEĞİL.

    Dönen sözlükte:
        "mode"          -> teknik yol (MODE_SKILL | MODE_DEPARTMENT |
                           MODE_MANUAL_TRIAGE | MODE_NONE)
        "routing_basis" -> iş kararı (SPECIFIC_SKILL | DEPARTMENT |
                           GENERAL_SUPPORT | MANUAL_TRIAGE)"""
    basis = effective_routing_basis(ticket)

    if basis == BASIS_MANUAL_TRIAGE:
        return _manual_triage_result(
            "manual_triage_basis",
            department=ticket.get("pending_department") or ticket.get("category"))

    if basis in (BASIS_SPECIFIC_SKILL, BASIS_GENERAL_SUPPORT):
        skill_code = (ticket.get("required_skill_code") or "").strip()
        if not skill_code:
            # Dayanak skill diyor ama kod yok: veri tutarsız. Rastgele bir
            # kuyruğa DÜŞÜRMEK yerine admin incelemesine bırakılır.
            return _manual_triage_result(
                "skill_basis_without_code",
                department=ticket.get("pending_department") or ticket.get("category"))
        # GENERAL_SUPPORT'ta departman YUMUŞAK FİLTRESİ UYGULANMAZ: genel
        # destek adaleti §26'da departmandan bağımsız tanımlıdır.
        department = (None if basis == BASIS_GENERAL_SUPPORT
                      else ticket.get("category"))
        return select_best_technician_for_skill(skill_code, department=department)

    if basis == BASIS_DEPARTMENT:
        department = ticket.get("pending_department") or ticket.get("category")
        if department:
            return select_best_technician_for_department(department)

    # Dayanak türetilemedi (ne uzmanlık ne departman). Mevcut davranış korunur:
    # talep kuyrukta kalır, otomatik atama yapılmaz.
    result = _empty_department_result(None)
    result["mode"] = MODE_NONE
    result["routing_basis"] = None
    return result


# ------------------------------------------------------------------
# Sunum yardımcıları — panel ve bildirim metinleri tek yerden
# ------------------------------------------------------------------

def basis_label(basis: str) -> str:
    """'Uzman bekliyor' / 'Departman bekliyor' / ... — admin kuyruk etiketi."""
    return config.ROUTING_BASIS_LABELS.get(
        basis, config.ROUTING_BASIS_LABELS[BASIS_MANUAL_TRIAGE])


def skill_display_name(skill_code: str) -> str:
    """Uzmanlık kodunun GÖRÜNEN adı; katalogda yoksa kodun kendisi."""
    if not skill_code:
        return ""
    skill = db.get_skill_by_code(skill_code)
    return skill["name"] if skill else skill_code


def routing_target_name(ticket: dict) -> str:
    """Talebin NEYİ beklediği: uzmanlık adı, departman adı ya da boş.

    Admin panelinde 'Uzman bekliyor: Görüntü İşleme' satırının ikinci
    yarısıdır. MANUAL_TRIAGE'da hedef yoktur, boş döner."""
    basis = effective_routing_basis(ticket)
    if basis in (BASIS_SPECIFIC_SKILL, BASIS_GENERAL_SUPPORT):
        return skill_display_name(ticket.get("required_skill_code"))
    if basis == BASIS_DEPARTMENT:
        return ticket.get("pending_department") or ticket.get("category") or ""
    return ""


def routing_summary(ticket: dict) -> str:
    """Talep detayındaki 'Yönlendirme:' satırı.

    Örnekler:
        'Görüntü İşleme'
        'Genel Teknik Destek'
        'Sistem Yönetimi (departman)'
        'Manuel sınıflandırma gerekiyor'"""
    basis = effective_routing_basis(ticket)
    target = routing_target_name(ticket)
    if basis == BASIS_MANUAL_TRIAGE or basis is None:
        return config.ROUTING_BASIS_LABELS[BASIS_MANUAL_TRIAGE]
    if basis == BASIS_DEPARTMENT:
        return f"{target} (departman)" if target else basis_label(basis)
    return target or basis_label(basis)


def selection_reason(result: dict) -> str:
    """Seçimin İÇ OPERASYON gerekçesi — panel/log içindir.

    Son kullanıcıya GÖSTERİLMEZ: kullanıcıyı teknik personelin iş yükü,
    öncelik seviyesi veya sıralama gerekçesi ilgilendirmez."""
    result = result or {}
    selected = result.get("selected")
    if not selected:
        return "Uygun aday yok."

    routing_basis = result.get("routing_basis")
    if result.get("mode") == MODE_DEPARTMENT:
        basis = f"departman routing ({result.get('department') or '—'})"
    elif routing_basis == BASIS_GENERAL_SUPPORT:
        basis = (f"genel teknik destek, {result.get('matched_priority')}. "
                 "öncelik uzmanlık")
    else:
        basis = f"{result.get('matched_priority')}. öncelik uzmanlık"

    return (f"{selected['name']} seçildi — {basis}, "
            f"{selected['active_ticket_count']} aktif ana iş, "
            f"son ana iş: {selected.get('last_primary_assignment_at') or 'hiç almamış'} "
            f"({len(result['candidates'])} aday arasından).")


def candidate_snapshot(result: dict) -> str:
    """Aday kümesinin DETERMİNİSTİK parmak izi.

    Aynı aday kümesi için aynı dizgeyi üretir; kümede kişi değişirse (veya
    öncelik seviyesi kayarsa) değişir.

    ARTIK KULLANICIYA BİLDİRİM GÖNDERMEK İÇİN KULLANILMAZ — otomatik atamaya
    geçildiği için kuyruktaki bir talep aday bulunur bulunmaz atanır ve
    kuyruktan çıkar. Gözlemlenebilirlik/denetim amacıyla korunuyor."""
    if not result or not result.get("candidates"):
        return ""
    ids = sorted(c["technician_id"] for c in result["candidates"])
    skill = (result.get("skill") or {}).get("code", "")
    return f"{skill}:{result.get('matched_priority')}:" + ",".join(str(i) for i in ids)


def describe_result(result: dict) -> str:
    """Panelde/logda gösterim için kısa özet (Faz 3B'de işe yarayacak)."""
    if not result.get("skill"):
        return "Tanımsız veya pasif uzmanlık."
    if not result["candidates"]:
        return f"{result['skill']['name']}: uygun aday yok."
    names = ", ".join(c["name"] for c in result["candidates"])
    return (f"{result['skill']['name']} — {result['matched_priority']}. öncelik "
            f"({len(result['candidates'])} aday): {names}")
