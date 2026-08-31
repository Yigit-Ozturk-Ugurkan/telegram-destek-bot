"""
Gemini API ile destek konuşmasını yürüten katman.

Faz 3B'de yapılandırılmış (structured) çıktıya geçildi:

  get_conversation_response(...) -> {"response_type", "message", "category", "required_skill"}
  classify_for_escalation(...)   -> {"ok", "classification_type", "category",
                                     "required_skill"}

ACCEPTANCE UX + ROUTING REVISION: sınıflandırma artık DÖRT sonuç tipinden
birini beyan eder (specific_skill / department / general_support / uncertain).
Model SOURCE OF TRUTH DEĞİLDİR — bu modül ham çıktıyı döndürür, kararı
routing.resolve_classification() verir. Başarısız çağrı ok=False ile bildirilir
ve GENEL DESTEK tahminine ÇEVRİLMEZ.

Kritik ayrım:
  clarification    = açıklayıcı soru  -> çözüm denemesi SAYILMAZ
  solution_attempt = yeni çözüm adımı -> çözüm denemesi SAYILIR

Bu modül tamamen izole: Gemini çağrısı başarısız olursa (ağ, kota, hatalı
anahtar, bozuk JSON) bot çökmez; güvenli bir fallback döner. Kullanıcıya ham
JSON asla gösterilmez.
"""

import json
import logging

import config

logger = logging.getLogger(__name__)

_client = None
_client_init_failed = False


def _get_client():
    """Gemini istemcisini tembel (lazy) şekilde oluşturur, tekrar tekrar başlatmaz."""
    global _client, _client_init_failed
    if _client is not None or _client_init_failed:
        return _client
    if not config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY tanımlı değil, AI önerileri devre dışı.")
        _client_init_failed = True
        return None
    try:
        from google import genai
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Gemini istemcisi başlatılamadı: {e}")
        _client_init_failed = True
        _client = None
    return _client


def _generate(prompt: str):
    """Ham metin döner; hata durumunda None. Anahtar/token asla loglanmaz."""
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL, contents=prompt
        )
        return (response.text or "").strip()
    except Exception as e:
        logger.error(f"Gemini yanıtı alınamadı: {type(e).__name__}")
        return None


def _parse_json(text: str):
    """Model bazen ```json ... ``` bloğuna sarıyor; temizleyip parse eder."""
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    # İlk { ... son } arasını al (model önüne açıklama koyarsa kurtarır)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"Gemini JSON parse edilemedi: {type(e).__name__}")
        return None


def _priority_semantics_text() -> str:
    """P1-P4 semantiğini prompt'a gömülecek biçimde üretir.

    Tanım config.PRIORITY_SEMANTICS'tedir; burada YENİDEN YAZILMAZ. Böylece
    "P2 nedir?" sorusunun tek bir cevabı olur ve prompt ile server tarafındaki
    doğrulama aynı sözlüğü paylaşır."""
    return "\n".join(
        f"- {code}: {text}" for code, text in config.PRIORITY_SEMANTICS.items()
    )


def _format_history(history):
    """Konuşma geçmişini modele okunur biçimde verir."""
    lines = []
    for m in history or []:
        who = "Kullanıcı" if m.get("sender_type") == "kullanici" else "Asistan"
        kind = m.get("message_kind")
        suffix = ""
        if kind == config.MESSAGE_KIND_AI_SOLUTION:
            suffix = "  [ÖNCEKİ ÇÖZÜM DENEMESİ — TEKRARLAMA]"
        elif kind == config.MESSAGE_KIND_AI_CLARIFICATION:
            suffix = "  [açıklayıcı soru]"
        lines.append(f"{who}: {m.get('message_text', '')}{suffix}")
    return "\n".join(lines) if lines else "(henüz mesaj yok)"


# ---------------- Konuşma yanıtı ----------------

_CONVERSATION_RULES = """Sen bir kurumsal BT destek masasının ilk seviye asistanısın.

KURALLAR:
- Daha önce verdiğin çözümü ASLA tekrar etme; geçmişte [ÖNCEKİ ÇÖZÜM DENEMESİ] ile
  işaretli adımları yeniden önerme.
- Konuşma geçmişini dikkate al; kullanıcı zaten yaptığını söylediği şeyi isteme.
- Yalnızca güvenli ve uygulanabilir adımlar öner. Bilmediğin teknik detayı UYDURMA.
- Eksik bilgi varsa çözüm önermek yerine TEK bir açıklayıcı soru sor.
- En fazla {max_attempts} çözüm denemesi hakkın var; şu an {attempt_count} tanesini
  kullandın. Hakkın dolduysa yeni çözüm önerme, insana devri öner.
- Kısa yaz: en fazla 4 cümle. Madde madde yazabilirsin.

response_type değerleri:
- "clarification"    : açıklayıcı soru soruyorsun (çözüm denemesi SAYILMAZ)
- "solution_attempt" : yeni bir çözüm/teşhis adımı veriyorsun (SAYILIR)
- "escalation_ready" : bu sorun ilk seviyede çözülemez, teknik ekip gerekir

SADECE şu JSON'u döndür, başka hiçbir şey yazma:
{{"response_type": "clarification|solution_attempt|escalation_ready", "message": "kullanıcıya gidecek Türkçe metin"}}
"""


def get_conversation_response(subject: str, history=None, attempt_count: int = 0,
                              max_attempts: int = None):
    """Konuşmanın bir sonraki AI yanıtını yapılandırılmış biçimde üretir.

    Dönen sözlük:
        {"response_type": clarification|solution_attempt|escalation_ready,
         "message": str, "category": None, "required_skill": None}

    Gemini kapalı/başarısız/bozuk JSON ise None döner — çağıran taraf güvenli
    fallback uygular (kullanıcıya ham JSON GÖSTERİLMEZ)."""
    if max_attempts is None:
        max_attempts = config.MAX_AI_SOLUTION_ATTEMPTS

    prompt = (
        _CONVERSATION_RULES.format(max_attempts=max_attempts, attempt_count=attempt_count)
        + f"\n\nTalep konusu: {subject}\n\nKonuşma geçmişi:\n{_format_history(history)}\n"
    )

    data = _parse_json(_generate(prompt))
    if not data:
        return None

    message = (data.get("message") or "").strip()
    if not message:
        return None

    response_type = (data.get("response_type") or "").strip()
    if response_type not in (config.AI_RESPONSE_CLARIFICATION,
                             config.AI_RESPONSE_SOLUTION,
                             config.AI_RESPONSE_ESCALATION):
        # Tanınmayan tip -> en güvenli varsayım: çözüm denemesi saymayalım.
        logger.warning("Gemini bilinmeyen response_type döndürdü; clarification sayılıyor.")
        response_type = config.AI_RESPONSE_CLARIFICATION

    # Hak dolmuşsa modelin yeni çözüm üretmesine izin verme.
    if response_type == config.AI_RESPONSE_SOLUTION and attempt_count >= max_attempts:
        response_type = config.AI_RESPONSE_ESCALATION

    return {
        "response_type": response_type,
        "message": message,
        "category": None,
        "required_skill": None,
    }


# ---------------- Eskalasyon sınıflandırması ----------------

_CLASSIFY_RULES = """Aşağıdaki destek konuşmasını oku ve talebi teknik ekibe
yönlendirmek için sınıflandır.

Departmanlar: {departments}

Uzmanlık kataloğu (YALNIZCA bu kodlardan birini seçebilirsin):
{skills}

Önce classification_type seç:

- "specific_skill"  : Talep belirli bir teknik uzmanlık gerektiriyor ve bu
                      uzmanlık yukarıdaki katalogda VAR.
                      required_skill = o kod.
- "department"      : Belirli bir uzmanlık kodu seçemiyorsun ama talebin hangi
                      departmana ait olduğu açık. required_skill = null.
- "general_support" : Talep BELİRLİ bir teknik uzmanlık GEREKTİRMİYOR; genel
                      masaüstü desteği, temel kullanıcı bilgisayarı sorunu,
                      basit uygulama/kullanım problemi ya da genel bir IT
                      sorusu. required_skill = "{general_code}".
                      Bunu SADECE gerçekten genel destek işiyse seç.
- "uncertain"       : Emin değilsin ya da konuşmada karar verecek kadar bilgi
                      yok. required_skill = null.

ÇOK ÖNEMLİ:
- "{general_code}" bir ÇÖP KUTUSU DEĞİLDİR. Emin olmadığın talepleri oraya
  ATMA — emin değilsen "uncertain" yaz.
- Katalogda olmayan bir kod UYDURMA.
- category, yukarıdaki departman listesinden biri olmalı; bilmiyorsan null yaz.

Sonra talebin ACİLİYETİNİ (priority) değerlendir. Bu teknik uzmanlıktan
BAĞIMSIZ bir ölçüdür — "hangi uzman baksın" ile "ne kadar acil" ayrı sorulardır.

{priority_semantics}

ÖNCELİK KURALLARI:
- Kararı ETKİ (impact) belirler: kaç kişi etkileniyor, iş duruyor mu, geçici
  çözüm var mı, güvenlik/production riski var mı.
- Kullanıcının "ACİL", "ÇOK ACİL", "KRİTİK", "!!!" yazması TEK BAŞINA P1
  SEBEBİ DEĞİLDİR. Tek kullanıcının sınırlı etkili sorunu, ne kadar acil
  yazılırsa yazılsın P3'tür.
- Emin değilsen "{default_priority}" yaz.
- priority_reason: en fazla bir cümlelik TEKNİK gerekçe (etki analizi).
  Kullanıcıya gösterilmez; teknik ekip içindir.

SADECE şu JSON'u döndür:
{{"classification_type": "specific_skill|department|general_support|uncertain",
  "category": "<departman veya null>",
  "required_skill": "<SKILL_CODE veya null>",
  "suggested_priority": "P1|P2|P3|P4",
  "priority_reason": "<kısa teknik gerekçe>"}}
"""


def classify_for_escalation(subject: str, history=None, skill_catalog=None):
    """Konuşmadan sınıflandırma tipi + kategori + gerekli uzmanlık kodunu çıkarır.

    skill_catalog: [{"code":..., "name":...}, ...] AKTİF skill listesi.

    Dönen sözlük:
        {"ok": bool, "classification_type": str|None,
         "category": str|None, "required_skill": str|None}

    ok=False, sınıflandırmanın TEKNİK olarak başarısız olduğunu söyler
    (Gemini kapalı, timeout, boş yanıt, bozuk JSON). Bu durum bir departman
    ya da genel destek tahmini ANLAMINA GELMEZ — çağıran taraf bunu
    MANUAL_TRIAGE olarak ele almalıdır.

    DİKKAT: dönen required_skill / category / classification_type DOĞRULANMAMIŞ
    model çıktısıdır. Model SOURCE OF TRUTH DEĞİLDİR; server-side doğrulama
    routing.resolve_classification() içindedir ve ZORUNLUDUR."""
    catalog = skill_catalog or []
    skills_text = "\n".join(f"- {s['code']}: {s['name']}" for s in catalog) or "(katalog boş)"

    prompt = (
        _CLASSIFY_RULES.format(
            departments=", ".join(config.DEPARTMENTS), skills=skills_text,
            general_code=config.GENERAL_SUPPORT_SKILL_CODE,
            priority_semantics=_priority_semantics_text(),
            default_priority=config.DEFAULT_PRIORITY,
        )
        + f"\n\nTalep konusu: {subject}\n\nKonuşma geçmişi:\n{_format_history(history)}\n"
    )

    data = _parse_json(_generate(prompt))
    if not data:
        # Ağ/kota/parse hatası. BELİRSİZ ≠ GENEL DESTEK: ok=False ile
        # bildirilir, tahmin ÜRETİLMEZ.
        logger.warning("Siniflandirma basarisiz; manuel triyaj gerekecek.")
        # Priority alanları da BOŞ döner. "Sınıflandırma başarısız" bir
        # öncelik tahmini DEĞİLDİR; doğrulama katmanı bunu P3'e düşürür.
        return {"ok": False, "classification_type": None,
                "category": None, "required_skill": None,
                "suggested_priority": None, "priority_reason": None}

    category = (data.get("category") or "").strip()
    if category not in config.DEPARTMENTS:
        category = None

    raw_skill = data.get("required_skill")
    required_skill = str(raw_skill).strip().upper() if raw_skill else None
    if required_skill in ("NULL", "NONE", ""):
        required_skill = None

    raw_type = data.get("classification_type")
    classification_type = str(raw_type).strip().lower() if raw_type else None
    if classification_type not in config.CLASSIFICATION_TYPES:
        # Tanınmayan tip -> UYDURMUYORUZ. None bırakılır; doğrulama katmanı
        # bunu "tip beyan edilmedi" olarak ele alır ve genel desteğe ÇEVİRMEZ.
        classification_type = None

    # DİKKAT: suggested_priority / priority_reason burada DOĞRULANMAZ.
    # Ham model çıktısıdır; geçerli P1-P4'e indirgeme ve P3 fallback'i
    # routing._validated_priority() içindedir (server SOURCE OF TRUTH).
    raw_priority = data.get("suggested_priority")
    suggested_priority = str(raw_priority).strip() if raw_priority else None

    raw_reason = data.get("priority_reason")
    priority_reason = str(raw_reason).strip() if raw_reason else None

    return {"ok": True, "classification_type": classification_type,
            "category": category, "required_skill": required_skill,
            "suggested_priority": suggested_priority,
            "priority_reason": priority_reason}


# ---------------- Faz 5A: Ticket özeti (yalnızca teknik ekip için) ----------------

_SUMMARY_RULES = """Aşağıda bir teknik destek talebinin tam geçmişi var. Her satır
rolüyle etiketlenmiştir:

  USER              : talebi açan kullanıcı
  AI_SOLUTION       : asistanın denediği çözüm adımı
  AI_CLARIFICATION  : asistanın sorduğu açıklayıcı soru
  TECHNICIAN        : teknik ekibin kullanıcıya gönderdiği yanıt
  INTERNAL_NOTE     : teknik ekibin KENDİ ARASINDAKİ dahili notu (kullanıcı görmez)
  SYSTEM            : atama/devir gibi sistem olayları

KURALLAR:
- INTERNAL_NOTE satırlarını kullanıcı mesajı SANMA; bunlar ekip içi bilgidir.
- Geçmişte olmayan bilgi UYDURMA. Bilinmiyorsa alanı boş bırak.
- Türkçe, kısa ve teknik yaz.

SADECE şu JSON'u döndür, başka hiçbir şey yazma:
{"problem": "...", "attempted_solutions": ["..."], "user_feedback": "...",
 "current_status": "...", "technical_findings": ["..."], "recommended_next_step": "..."}
"""


def summarize_ticket(subject: str, context_text: str):
    """Talep geçmişinden yapılandırılmış özet üretir.

    Başarısızlıkta None döner — çağıran taraf mevcut özeti KORUR, ticket'a
    dokunmaz ve kullanıcıya hiçbir şey göndermez. API hatası/anahtar detayı
    panele sızdırılmaz."""
    prompt = f"{_SUMMARY_RULES}\n\nTalep konusu: {subject}\n\nGeçmiş:\n{context_text}\n"
    data = _parse_json(_generate(prompt))
    if not data:
        return None

    def _text(key):
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    def _list(key):
        value = data.get(key)
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    summary = {
        "problem": _text("problem"),
        "attempted_solutions": _list("attempted_solutions"),
        "user_feedback": _text("user_feedback"),
        "current_status": _text("current_status"),
        "technical_findings": _list("technical_findings"),
        "recommended_next_step": _text("recommended_next_step"),
    }
    # Tamamen boş bir özet üretilmişse başarısız say.
    if not any(summary.values()):
        return None
    return summary


# ---------------- Legacy (Faz 1/2 uyumluluğu) ----------------

def get_ai_response(subject: str):
    """Eski tek atışlık arayüz: {"category":..., "suggestion":...} veya None.

    Faz 3B akışı get_conversation_response() kullanır; bu fonksiyon geriye dönük
    uyumluluk ve department fallback'i için korunmuştur."""
    departments = ", ".join(config.DEPARTMENTS)
    prompt = (
        "Sen bir teknik destek masasının ilk seviye asistanısın. Aşağıdaki kullanıcı "
        "talebini değerlendir ve SADECE şu JSON formatında yanıt ver:\n"
        f'{{"category": "<{departments} listesinden biri>", '
        '"suggestion": "<2-4 cümlelik, adım adım, Türkçe ilk çözüm önerisi>"}\n\n'
        f'Kullanıcı talebi: "{subject}"\n'
    )
    data = _parse_json(_generate(prompt))
    if not data:
        return None

    category = (data.get("category") or "").strip()
    suggestion = (data.get("suggestion") or "").strip()
    if category not in config.DEPARTMENTS:
        category = "Diğer"
    if not suggestion:
        return None
    return {"category": category, "suggestion": suggestion}
