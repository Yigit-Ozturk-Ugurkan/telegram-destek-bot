import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from contextlib import contextmanager

import config

# Skill kodu normalizasyonu için Türkçe harf eşlemesi.
# NFKD çoğu harfi zaten çözüyor ama noktasız ı / noktalı İ çözülmüyor.
_TR_CODE_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        # Blok içindeki BÜTÜN yazmalar tek transaction'dır; biri patlarsa
        # hiçbiri kalıcı olmaz (Faz 4 ownership transfer buna dayanır).
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column: str, ddl: str):
    """Var olan destek.db dosyalarını bozmadan yeni sütun eklemek için basit migration."""
    existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                subject TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'acik',
                assigned_admin_id INTEGER,
                assigned_admin_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,      -- 'kullanici' | 'yonetici' | 'sistem'
                sender_id INTEGER,
                sender_name TEXT,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS technicians (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                telegram_id INTEGER,
                department TEXT NOT NULL,
                available INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        # tickets tablosuna AI/eskalasyon alanlarını sonradan eklenen sistemler için ekle
        _add_column_if_missing(conn, "tickets", "category", "category TEXT")
        _add_column_if_missing(conn, "tickets", "ai_suggestion", "ai_suggestion TEXT")
        _add_column_if_missing(conn, "tickets", "escalated", "escalated INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "tickets", "pending_department", "pending_department TEXT")
        _add_column_if_missing(conn, "tickets", "assigned_technician_id", "assigned_technician_id INTEGER")
        _add_column_if_missing(conn, "tickets", "helper_name", "helper_name TEXT")
        _add_column_if_missing(conn, "technicians", "role", "role TEXT")

        # --- Faz 2: çözüm metadata'sı ve okunmamış mesaj takibi ---
        _add_column_if_missing(conn, "tickets", "resolved_at", "resolved_at TEXT")
        _add_column_if_missing(conn, "tickets", "resolved_by_user_id", "resolved_by_user_id INTEGER")
        _add_column_if_missing(conn, "tickets", "last_user_message_at", "last_user_message_at TEXT")
        _add_column_if_missing(conn, "tickets", "last_staff_message_at", "last_staff_message_at TEXT")

        # --- Faz 2: mesajın Telegram'a gerçekten ulaşıp ulaşmadığı ---
        _add_column_if_missing(conn, "messages", "sender_user_id", "sender_user_id INTEGER")
        _add_column_if_missing(conn, "messages", "delivery_status", "delivery_status TEXT")
        _add_column_if_missing(conn, "messages", "delivery_error", "delivery_error TEXT")

        # --- Faz 2: web paneli kullanıcıları (admin / technician) ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                full_name TEXT,
                technician_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (technician_id) REFERENCES technicians(id)
            )
        """)

        # --- Faz 2: ATOMİK idempotency deposu (çift POST koruması) ---
        # Cookie tabanlı token eş zamanlı iki isteği durduramıyordu; UNIQUE
        # PRIMARY KEY sayesinde aynı token için yalnızca ilk INSERT başarılı olur.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_form_tokens (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("DELETE FROM processed_form_tokens WHERE created_at < datetime('now', '-7 day')")

        # --- Faz 2.5: parola değiştirme zorunluluğu ---
        _add_column_if_missing(conn, "web_users", "must_change_password",
                               "must_change_password INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "web_users", "password_changed_at", "password_changed_at TEXT")

        # --- Faz 2.5: çözüm denetimi (kim, hangi kanaldan?) ---
        # Web kullanıcıları için resolved_by_user_id çalışmaya devam eder;
        # Telegram tarafında web hesabı eşleşmiyorsa external_id ile iz bırakılır.
        _add_column_if_missing(conn, "tickets", "resolved_by_source", "resolved_by_source TEXT")
        _add_column_if_missing(conn, "tickets", "resolved_by_external_id", "resolved_by_external_id TEXT")
        _add_column_if_missing(conn, "tickets", "resolved_by_label", "resolved_by_label TEXT")

        # --- Faz 3B: AI konuşma durumu ---
        # ai_attempt_count: AI'ın ürettiği GERÇEK çözüm denemesi sayısı
        #                   (kullanıcı mesajı sayısı DEĞİL, clarification DEĞİL)
        # ai_mode         : AI_HANDLING | HUMAN_ASSISTED
        # required_skill_code: Gemini'nin tespit ettiği ve DOĞRULANMIŞ skill kodu
        _add_column_if_missing(conn, "tickets", "ai_attempt_count",
                               "ai_attempt_count INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "tickets", "ai_mode", "ai_mode TEXT")
        _add_column_if_missing(conn, "tickets", "required_skill_code", "required_skill_code TEXT")
        # Hangi AI mesajı deneme, hangisi açıklayıcı soruydu?
        _add_column_if_missing(conn, "messages", "message_kind", "message_kind TEXT")

        # --- Faz 5B: öncelik, SLA ve uzman kuyruğu ---
        # Eski kayıtlar DEFAULT ile otomatik P3 olur; ayrı backfill gerekmez.
        _add_column_if_missing(conn, "tickets", "priority",
                               f"priority TEXT NOT NULL DEFAULT '{config.DEFAULT_PRIORITY}'")
        # SLA saati İNSAN eskalasyonunda başlar (AI_HANDLING süresi sayılmaz).
        _add_column_if_missing(conn, "tickets", "sla_started_at", "sla_started_at TEXT")
        _add_column_if_missing(conn, "tickets", "first_response_due_at", "first_response_due_at TEXT")
        _add_column_if_missing(conn, "tickets", "resolution_due_at", "resolution_due_at TEXT")
        _add_column_if_missing(conn, "tickets", "first_staff_response_at", "first_staff_response_at TEXT")
        # Uzman kuyruğu AYRI TABLO DEĞİL — status + required_skill_code'dan türetilir.
        _add_column_if_missing(conn, "tickets", "queue_entered_at", "queue_entered_at TEXT")
        _add_column_if_missing(conn, "tickets", "candidate_notified_at", "candidate_notified_at TEXT")
        _add_column_if_missing(conn, "tickets", "candidate_snapshot", "candidate_snapshot TEXT")
        _add_column_if_missing(conn, "tickets", "candidate_checked_at", "candidate_checked_at TEXT")

        # --- ACCEPTANCE UX + ROUTING REVISION: yönlendirme dayanağı (audit) ---
        # Talebin HANGİ SINIFLANDIRMA SONUCUYLA kuyruğa girdiği. Bu alan
        # olmadan "uzman bekliyor" ile "manuel sınıflandırma gerekiyor"
        # birbirinden ayrılamaz ve queue worker MANUAL_TRIAGE talebe yanlışlıkla
        # otomatik atama yapabilirdi.
        #
        # NULL = revizyondan ÖNCEKİ kayıt. Bu satırlar için dayanak mevcut
        # alanlardan TÜRETİLİR (bkz. routing.effective_routing_basis); geriye
        # dönük veri yazımı YAPILMAZ, eski taleplerin davranışı değişmez.
        _add_column_if_missing(conn, "tickets", "routing_basis", "routing_basis TEXT")

        # --- PRIORITY ROUTING REVISION: AI destekli P1-P4 önerisi ---
        # priority_source, önceliğe EN SON KİMİN karar verdiğini söyler:
        #   default -> hiç dokunulmadı (şema varsayılanı P3)
        #   ai      -> DOĞRULANMIŞ model önerisi uygulandı
        #   manual  -> bir insan elle belirledi  => AI BİR DAHA ÜZERİNE YAZAMAZ
        # Bu kolon olmadan "admin override edildi mi?" sorusu cevaplanamaz ve
        # AI, adminin kararını sessizce ezerdi (bkz. apply_ai_priority).
        #
        # NULL = revizyondan ÖNCEKİ kayıt. Geriye dönük veri yazımı YAPILMAZ;
        # okuma tarafı NULL'ı 'default' gibi ele alır (priority_source()).
        _add_column_if_missing(conn, "tickets", "priority_source",
                               "priority_source TEXT")
        # Modelin HAM önerisi + gerekçesi — DENETİM izidir, karar değildir.
        # ai_suggested_priority, uygulanan priority'den FARKLI olabilir
        # (admin override ettiyse). Gerekçe yalnızca iç panelde gösterilir,
        # son kullanıcıya ASLA gönderilmez.
        _add_column_if_missing(conn, "tickets", "ai_suggested_priority",
                               "ai_suggested_priority TEXT")
        _add_column_if_missing(conn, "tickets", "ai_priority_reason",
                               "ai_priority_reason TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status_basis "
                     "ON tickets(status, routing_basis)")

        # Faz 5B sorgu desenleri için indexler
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_priority_status ON tickets(priority, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status_skill ON tickets(status, required_skill_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_resolution_due ON tickets(resolution_due_at)")

        # --- Faz 6A: SLA cycle geçmişi ---
        # SOURCE OF TRUTH burasıdır; tickets.sla_* kolonları AKTİF cycle'ın
        # senkron kopyasıdır (bkz. _sync_ticket_sla_cache).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sla_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                cycle_number INTEGER NOT NULL,
                priority TEXT NOT NULL,
                started_at TEXT NOT NULL,
                first_response_due_at TEXT,
                resolution_due_at TEXT,
                first_staff_response_at TEXT,
                paused_at TEXT,
                total_paused_seconds INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT,
                closed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            )
        """)
        # Bir talepte AYNI ANDA tek AKTİF cycle olabilir.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sla_one_active_cycle "
            "ON sla_cycles(ticket_id) WHERE closed_at IS NULL"
        )
        # Aynı talepte aynı cycle numarası iki kez üretilemez (çift reopen koruması).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sla_cycle_number "
            "ON sla_cycles(ticket_id, cycle_number)"
        )

        # --- Faz 6A: legacy SLA backfill (idempotent) ---
        # SLA'sı başlamış ve henüz cycle kaydı olmayan talepler için Cycle #1.
        # Talep çözülmüşse cycle kapalı, değilse aktif olarak oluşturulur.
        conn.execute(
            "INSERT INTO sla_cycles "
            "(ticket_id, cycle_number, priority, started_at, first_response_due_at, "
            " resolution_due_at, first_staff_response_at, resolved_at, closed_at, "
            " created_at, updated_at) "
            "SELECT t.id, 1, COALESCE(t.priority, ?), t.sla_started_at, "
            "       t.first_response_due_at, t.resolution_due_at, t.first_staff_response_at, "
            "       t.resolved_at, "
            "       CASE WHEN t.status = ? THEN COALESCE(t.resolved_at, t.updated_at) END, "
            "       ?, ? "
            "FROM tickets t "
            "WHERE t.sla_started_at IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM sla_cycles c WHERE c.ticket_id = t.id)",
            (config.DEFAULT_PRIORITY, config.STATUS_CLOSED, now(), now()),
        )

        # --- Faz 6B: iş takvimi alanları ---
        # effective_started_at: SLA sayacının GERÇEKTEN işlemeye başladığı an.
        #   started_at gerçek eskalasyon zamanı olarak KALIR (audit); mesai
        #   dışında eskale edilen talepte sayaç ilk açılışta başlar.
        _add_column_if_missing(conn, "sla_cycles", "effective_started_at",
                               "effective_started_at TEXT")
        # total_paused_seconds'in ANLAMI DEĞİŞMEZ (wall-clock).
        # İş takvimiyle kesişen gerçek SLA etkisi ayrı alanda tutulur.
        _add_column_if_missing(conn, "sla_cycles", "total_sla_paused_seconds",
                               "total_sla_paused_seconds INTEGER NOT NULL DEFAULT 0")
        # Mevcut satırlar otomatik LEGACY olur; yeni cycle'lar açıkça BUSINESS yazar.
        _add_column_if_missing(conn, "sla_cycles", "calendar_mode",
                               f"calendar_mode TEXT NOT NULL DEFAULT '{config.CALENDAR_MODE_LEGACY}'")

        # --- Faz 6B: tatil takvimi ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sla_holidays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                holiday_date TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_holidays_active ON sla_holidays(active, holiday_date)")

        # --- Faz 6B.1: takvim revizyonu (süreçler arası cache tutarlılığı) ---
        # Web / bot / queue_worker AYRI süreçlerdir. Her biri kendi tatil
        # cache'ini tutar; bu tek satırlık sayaç sayesinde bir süreçteki
        # değişikliği diğerleri fark edebilir.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sla_calendar_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO sla_calendar_state (id, revision, updated_at) "
            "VALUES (1, 1, ?)", (now(),))

        # --- Faz 6B: duraklama olayları (SLA denetimi) ---
        # Her duraklama aralığı ayrı satır: wall-clock ve iş-zamanı karşılığı
        # birlikte saklanır, böylece toplam değer geriye dönük doğrulanabilir.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sla_pause_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                paused_at TEXT NOT NULL,
                resumed_at TEXT,
                wall_seconds INTEGER,
                business_seconds INTEGER,
                reason TEXT,
                FOREIGN KEY (cycle_id) REFERENCES sla_cycles(id)
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pause_one_open "
            "ON sla_pause_events(cycle_id) WHERE resumed_at IS NULL"
        )

        # --- Faz 5B: giriş denemeleri (brute-force koruması) ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(identifier, attempted_at)")
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-1 day')")

        # --- Faz 5A: AI ticket özeti ---
        # Talep başına YALNIZCA EN SON özet tutulur (ticket_id UNIQUE + upsert).
        # Böylece iki teknisyen aynı anda butona bassa bile duplicate/yarım
        # kayıt oluşmaz; last-write-wins atomik olur.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_ai_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL UNIQUE,
                summary_json TEXT NOT NULL,
                generated_by_user_id INTEGER,
                generated_at TEXT NOT NULL,
                source_last_message_id INTEGER,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id),
                FOREIGN KEY (generated_by_user_id) REFERENCES web_users(id)
            )
        """)

        # --- Faz 4: atama geçmişi (PRIMARY + COLLABORATOR) ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                technician_id INTEGER NOT NULL,
                assignment_role TEXT NOT NULL
                    CHECK (assignment_role IN ('PRIMARY', 'COLLABORATOR')),
                assigned_by_user_id INTEGER,
                assigned_by_source TEXT,
                assigned_by_external_id TEXT,
                assigned_by_label TEXT,
                assigned_at TEXT NOT NULL,
                removed_at TEXT,
                removal_reason TEXT,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id),
                FOREIGN KEY (technician_id) REFERENCES technicians(id),
                FOREIGN KEY (assigned_by_user_id) REFERENCES web_users(id)
            )
        """)
        # Bir talepte AYNI ANDA yalnızca BİR aktif PRIMARY olabilir.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ta_one_active_primary "
            "ON ticket_assignments(ticket_id) "
            "WHERE assignment_role = 'PRIMARY' AND removed_at IS NULL"
        )
        # Aynı kişi aynı talepte iki aktif atama satırına sahip olamaz.
        # (Bu aynı zamanda 'PRIMARY aynı anda COLLABORATOR olamaz'ı da garanti eder.)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ta_one_active_per_tech "
            "ON ticket_assignments(ticket_id, technician_id) WHERE removed_at IS NULL"
        )
        # Dashboard sorguları: teknisyenin aktif atamaları
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ta_tech_active "
            "ON ticket_assignments(technician_id, assignment_role, removed_at)"
        )

        # --- Faz 4: legacy backfill (idempotent) ---
        # Mevcut tickets.assigned_technician_id değerleri KAYBOLMAZ: aktif
        # PRIMARY satırı olmayan her atanmış talep için bir tane üretilir.
        conn.execute(
            "INSERT INTO ticket_assignments "
            "(ticket_id, technician_id, assignment_role, assigned_by_source, "
            " assigned_by_label, assigned_at) "
            "SELECT t.id, t.assigned_technician_id, 'PRIMARY', ?, ?, "
            "       COALESCE(t.updated_at, t.created_at) "
            "FROM tickets t "
            "WHERE t.assigned_technician_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM ticket_assignments a "
            "                  WHERE a.ticket_id = t.id "
            "                    AND a.assignment_role = 'PRIMARY' "
            "                    AND a.removed_at IS NULL)",
            (config.ASSIGN_SOURCE_MIGRATION, "Faz 4 geçişi"),
        )

        # --- Faz 3A: uzmanlık (skill) kataloğu ---
        # Skill, technicians.department'ın YERİNE GEÇMEZ. Department kaba
        # organizasyon bilgisi, skill ise routing için hassas yetenek bilgisidir.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # --- Faz 3A: teknisyen uzmanlıkları (1 / 2 / 3 öncelik) ---
        # İki uniqueness kuralı:
        #   UNIQUE(technician_id, skill_id) -> aynı skill bir kişiye iki kez verilemez
        #   UNIQUE(technician_id, priority) -> aynı kişide bir öncelik tek skill'e ait
        # Bu UNIQUE'ler SQLite'ta otomatik index üretir; ayrıca index açmıyoruz.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS technician_skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                technician_id INTEGER NOT NULL,
                skill_id INTEGER NOT NULL,
                priority INTEGER NOT NULL CHECK (priority IN (1, 2, 3)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (technician_id) REFERENCES technicians(id),
                FOREIGN KEY (skill_id) REFERENCES skills(id),
                UNIQUE (technician_id, skill_id),
                UNIQUE (technician_id, priority)
            )
        """)
        # Routing sorgusu skill_id + priority üzerinden tarar; yukarıdaki
        # UNIQUE'ler technician_id ile başladığı için bu erişim yolu kapsanmıyor.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tech_skills_skill_priority "
                     "ON technician_skills(skill_id, priority)")

        # --- Faz 2: indexler ---
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_tech_status ON tickets(assigned_technician_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_status ON tickets(user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ticket_created ON messages(ticket_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_technicians_available ON technicians(available)")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- Talep (ticket) işlemleri ----------

def create_ticket(user_id: int, username: str, full_name: str, subject: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tickets (user_id, username, full_name, subject, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, subject, config.STATUS_OPEN, now(), now()),
        )
        ticket_id = cur.lastrowid
        conn.execute(
            "INSERT INTO messages (ticket_id, sender_type, sender_id, sender_name, message_text, created_at) "
            "VALUES (?, 'kullanici', ?, ?, ?, ?)",
            (ticket_id, user_id, full_name, subject, now()),
        )
        return ticket_id


def get_ticket(ticket_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None


def get_open_ticket_for_user(user_id: int):
    """Kullanıcının hâlâ kapanmamış (çözülmemiş) talebi — Telegram'dan gelen yeni
    mesajın ekleneceği talep. Çözülmüş talepler DIŞARIDA bırakılır ki kullanıcı
    haftalar sonra yeni bir konu yazdığında yeni talep açılabilsin."""
    placeholders = ",".join("?" * len(config.UNRESOLVED_STATUSES))
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM tickets WHERE user_id = ? AND status IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            (user_id, *config.UNRESOLVED_STATUSES),
        ).fetchone()
        return dict(row) if row else None


def list_tickets(status: str = None):
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def list_tickets_for_user(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_status(ticket_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), ticket_id),
        )


def assign_ticket(ticket_id: int, admin_id: int, admin_name: str):
    """LEGACY (Faz 5A) — YENİ KODDA KULLANILMAZ. Yerine assign_primary_if_unassigned() kullanılır.

    tickets.assigned_admin_name'e yazan eski yollardan biridir."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET assigned_admin_id = ?, assigned_admin_name = ?, "
            "helper_name = NULL, status = ?, updated_at = ? WHERE id = ?",
            (admin_id, admin_name, config.STATUS_IN_PROGRESS, now(), ticket_id),
        )


def set_ticket_responder(ticket_id: int, name: str):
    """LEGACY (Faz 5A) — YENİ KODDA KULLANILMAZ. Faz 2.5'ten beri üretim kodunda çağrılmıyor.

    tickets.assigned_admin_name'e yazan eski yollardan biridir."""
    """
    Biri talebe yanıt yazdığında çağrılır:
    - Henüz kimse üstlenmemişse (Sorumlu boşsa), yanıtlayan otomatik Sorumlu olur.
    - Sorumlu zaten varsa ve yanıtlayan FARKLI biriyse, o kişi "Yardımcı Sorumlu" olarak işaretlenir
      (Sorumlu değişmez).
    - Yanıtlayan zaten Sorumlu'nun kendisiyse, varsa eski Yardımcı Sorumlu bilgisi temizlenir.
    """
    ticket = get_ticket(ticket_id)
    if not ticket:
        return
    with get_conn() as conn:
        if not ticket["assigned_admin_name"]:
            conn.execute(
                "UPDATE tickets SET assigned_admin_name = ?, status = ?, updated_at = ? WHERE id = ?",
                (name, config.STATUS_IN_PROGRESS, now(), ticket_id),
            )
        elif name != ticket["assigned_admin_name"]:
            conn.execute(
                "UPDATE tickets SET helper_name = ?, updated_at = ? WHERE id = ?",
                (name, now(), ticket_id),
            )
        else:
            conn.execute(
                "UPDATE tickets SET helper_name = NULL, updated_at = ? WHERE id = ?",
                (now(), ticket_id),
            )


def counts_by_status():
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) as c FROM tickets GROUP BY status").fetchall()
        result = {config.STATUS_OPEN: 0, config.STATUS_IN_PROGRESS: 0, config.STATUS_CLOSED: 0}
        for r in rows:
            result[r["status"]] = r["c"]
        return result


# ---------- Mesaj işlemleri ----------

def add_message(ticket_id: int, sender_type: str, sender_id: int, sender_name: str,
                message_text: str, sender_user_id: int = None, delivery_status: str = None,
                message_kind: str = None):
    """Mesajı ekler ve mesaj id'sini döner.

    Ayrıca talebin updated_at alanını ve gönderen tipine göre
    last_user_message_at / last_staff_message_at alanlarını günceller —
    teknisyen panelindeki 'yeni mesaj' göstergesi bu iki damgayı karşılaştırır."""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (ticket_id, sender_type, sender_id, sender_name, message_text, "
            "created_at, sender_user_id, delivery_status, message_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, sender_type, sender_id, sender_name, message_text,
             stamp, sender_user_id, delivery_status, message_kind),
        )
        if sender_type == "kullanici":
            conn.execute(
                "UPDATE tickets SET updated_at = ?, last_user_message_at = ? WHERE id = ?",
                (stamp, stamp, ticket_id),
            )
        elif sender_type in ("yonetici", "teknisyen"):
            conn.execute(
                "UPDATE tickets SET updated_at = ?, last_staff_message_at = ? WHERE id = ?",
                (stamp, stamp, ticket_id),
            )
        else:
            conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (stamp, ticket_id))
        return cur.lastrowid


def set_message_delivery(message_id: int, status: str, error: str = None):
    """Telegram gönderiminin gerçek sonucunu mesaja işler."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET delivery_status = ?, delivery_error = ? WHERE id = ?",
            (status, error, message_id),
        )


def get_messages(ticket_id: int):
    """Mesajları, gönderen personelin GERÇEK kimliğiyle birlikte döner.

    Faz 2.5: personel adı artık tickets.assigned_admin_name gibi serbest metin
    alanlarından değil, messages.sender_user_id -> web_users ilişkisinden gelir.
    Eski kayıtlarda sender_user_id NULL'dur; o durumda kaydedilmiş sender_name
    legacy görüntüleme için kullanılır."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.*, w.full_name AS sender_full_name, w.username AS sender_username, "
            "w.role AS sender_role "
            "FROM messages m LEFT JOIN web_users w ON w.id = m.sender_user_id "
            "WHERE m.ticket_id = ? ORDER BY m.id ASC",
            (ticket_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- AI / eskalasyon işlemleri ----------

def set_ai_result(ticket_id: int, category: str, suggestion: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET category = ?, ai_suggestion = ?, updated_at = ? WHERE id = ?",
            (category, suggestion, now(), ticket_id),
        )


def mark_escalated(ticket_id: int, pending_department: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET escalated = 1, pending_department = ?, updated_at = ? WHERE id = ?",
            (pending_department, now(), ticket_id),
        )


def clear_pending(ticket_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET pending_department = NULL WHERE id = ?", (ticket_id,)
        )


def assign_technician_to_ticket(ticket_id: int, technician_id: int, technician_name: str):
    """LEGACY (Faz 5A) — YENİ KODDA KULLANILMAZ. Yerine assign_primary_if_unassigned() kullanılır.

    tickets.assigned_admin_name'e yazan eski yollardan biridir."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET assigned_technician_id = ?, assigned_admin_name = ?, "
            "status = ?, pending_department = NULL, updated_at = ? WHERE id = ?",
            (technician_id, technician_name, config.STATUS_IN_PROGRESS, now(), ticket_id),
        )


def get_pending_tickets_for_department(department: str):
    """LEGACY okuma yardımcısı — atama SIRASI için KULLANILMAZ.

    'ORDER BY id ASC' ile en eski talebi verir; hangi TEKNİSYENE gideceğine
    dair bir şey söylemez. Eskiden çağıranlar pending[0]'ı doğrudan müsait
    olan kişiye atıyordu (ACCEPTANCE FIX 2'de kaldırıldı).

    Bekleyen kuyruk artık queue_service üzerinden değerlendirilir; oradaki
    kaynak list_waiting_expert_tickets()'tır."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE pending_department = ? AND assigned_technician_id IS NULL "
            "AND status != ? ORDER BY id ASC",
            (department, config.STATUS_CLOSED),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Teknik ekip işlemleri ----------

def create_technician(name: str, telegram_id, department: str, role: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO technicians (name, telegram_id, department, role, available, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (name, telegram_id, department, role, now()),
        )
        return cur.lastrowid


def list_technicians():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM technicians ORDER BY department, name").fetchall()
        return [dict(r) for r in rows]


def list_available_technicians():
    """Yalnızca yeni iş almaya AÇIK (available = 1) teknisyenler.

    list_technicians_with_workload(only_available=True) üzerinden yardımcı
    aday listelerini besler."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM technicians WHERE available = 1 ORDER BY department, name"
        ).fetchall()
        return [dict(r) for r in rows]


def get_technician_by_telegram_id(telegram_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM technicians WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def get_technician(technician_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM technicians WHERE id = ?", (technician_id,)).fetchone()
        return dict(row) if row else None


def find_available_technician(department: str):
    """LEGACY — YENİ ATAMA MANTIĞINDA KULLANILMAZ (ACCEPTANCE FIX 2).

    'Departmandaki İLK müsait kişi' (id sırası) davranışıdır. İş yükünü ve
    adaleti GÖRMEZDİ: 5 aktif talebi olan kişi, boştaki meslektaşının önüne
    geçebiliyordu.

    Yerine routing.select_best_technician_for_department() kullanılır; o da
    list_department_candidates() üzerinden workload + least-recently-assigned
    sıralamasını uygular.

    Fonksiyon SİLİNMEDİ — dışarıdan çağıran bir script/entegrasyon kalmış
    olabilir. Ama hiçbir production atama yolu artık buraya girmez; bunu
    tests/test_routing_auto_assignment.py call-site taraması doğrular."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM technicians WHERE department = ? AND available = 1 ORDER BY id ASC LIMIT 1",
            (department,),
        ).fetchone()
        return dict(row) if row else None


def set_technician_availability(technician_id: int, available: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE technicians SET available = ? WHERE id = ?", (1 if available else 0, technician_id)
        )


# ---------- Faz 2: Web paneli kullanıcıları ----------

def create_web_user(username: str, password_hash: str, role: str,
                    full_name: str = None, technician_id: int = None,
                    must_change_password: bool = False) -> int:
    """Parola DAİMA hash olarak gelir; bu fonksiyon düz metin parola görmez."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO web_users (username, password_hash, role, full_name, technician_id, "
            "active, created_at, must_change_password) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (username, password_hash, role, full_name, technician_id, now(),
             1 if must_change_password else 0),
        )
        return cur.lastrowid


def get_web_user_by_username(username: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM web_users WHERE username = ? AND active = 1", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_web_user(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_web_users():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT w.*, t.name AS technician_name, t.department AS technician_department "
            "FROM web_users w LEFT JOIN technicians t ON t.id = w.technician_id "
            "ORDER BY w.role, w.username"
        ).fetchall()
        return [dict(r) for r in rows]


def get_web_user_for_technician(technician_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM web_users WHERE technician_id = ? AND active = 1", (technician_id,)
        ).fetchone()
        return dict(row) if row else None


# ---------- Faz 2: Atomik form token (çift gönderim koruması) ----------

def claim_form_token(token: str) -> bool:
    """Aynı token için YALNIZCA ilk çağrı True döner.

    UNIQUE PRIMARY KEY ihlali, eş zamanlı iki isteğin ikisinin birden
    geçmesini engeller — cookie tabanlı kontrolün yapamadığı şey buydu."""
    if not token:
        return False
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO processed_form_tokens (token, created_at) VALUES (?, ?)",
                (token, now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


# ---------- Faz 2: Durum grubu yardımcıları ----------

def _statuses_for_group(group: str):
    """'active' | 'waiting' | 'resolved' | 'all' -> DB durum değerleri."""
    if group == "active":
        return config.ACTIVE_STATUSES
    if group == "waiting":
        return config.WAITING_STATUSES
    if group == "resolved":
        return config.RESOLVED_STATUSES
    return None  # all


_TICKET_SELECT = """
    SELECT t.*,
           tech.name AS technician_name,
           tech.department AS technician_department,
           CASE WHEN t.last_user_message_at IS NOT NULL
                 AND (t.last_staff_message_at IS NULL
                      OR t.last_user_message_at > t.last_staff_message_at)
                THEN 1 ELSE 0 END AS has_unread
    FROM tickets t
    LEFT JOIN technicians tech ON tech.id = t.assigned_technician_id
"""


# ---------- Faz 2: Yetkilendirilmiş talep sorguları ----------

def list_admin_tickets(group: str = None):
    """Admin: bütün teknik personelin taleplerini görür."""
    statuses = _statuses_for_group(group)
    with get_conn() as conn:
        if statuses:
            ph = ",".join("?" * len(statuses))
            rows = conn.execute(
                _TICKET_SELECT + f" WHERE t.status IN ({ph}) ORDER BY t.updated_at DESC, t.id DESC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = conn.execute(
                _TICKET_SELECT + " ORDER BY t.updated_at DESC, t.id DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def list_technician_tickets(technician_id: int, group: str = None):
    """Teknisyen: YALNIZCA kendisine atanmış talepleri görür.

    Yetkilendirme SQL seviyesinde yapılır — template filtresine bırakılmaz."""
    statuses = _statuses_for_group(group)
    with get_conn() as conn:
        if statuses:
            ph = ",".join("?" * len(statuses))
            rows = conn.execute(
                _TICKET_SELECT + f" WHERE t.assigned_technician_id = ? AND t.status IN ({ph}) "
                "ORDER BY t.updated_at DESC, t.id DESC",
                (technician_id, *statuses),
            ).fetchall()
        else:
            rows = conn.execute(
                _TICKET_SELECT + " WHERE t.assigned_technician_id = ? "
                "ORDER BY t.updated_at DESC, t.id DESC",
                (technician_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def list_resolved_tickets(technician_id: int = None):
    """technician_id verilirse yalnızca o kişinin sorumlu olduğu çözülmüş talepler."""
    with get_conn() as conn:
        if technician_id is not None:
            rows = conn.execute(
                _TICKET_SELECT + " WHERE t.status = ? AND t.assigned_technician_id = ? "
                "ORDER BY t.resolved_at DESC, t.id DESC",
                (config.STATUS_CLOSED, technician_id),
            ).fetchall()
        else:
            rows = conn.execute(
                _TICKET_SELECT + " WHERE t.status = ? ORDER BY t.resolved_at DESC, t.id DESC",
                (config.STATUS_CLOSED,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_ticket_detail(ticket_id: int):
    """Talep + atanan teknisyen bilgisi (admin/teknisyen detay ekranı için)."""
    with get_conn() as conn:
        row = conn.execute(_TICKET_SELECT + " WHERE t.id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None


def get_ticket_for_technician(ticket_id: int, technician_id: int):
    """Talep YALNIZCA bu teknisyene atanmışsa döner, aksi hâlde None.

    URL'yi elle değiştirerek başka teknisyenin talebine erişimi engelleyen
    backend kontrolü budur."""
    with get_conn() as conn:
        row = conn.execute(
            _TICKET_SELECT + " WHERE t.id = ? AND t.assigned_technician_id = ?",
            (ticket_id, technician_id),
        ).fetchone()
        return dict(row) if row else None


def workload_subquery_sql() -> str:
    """Teknisyen iş yükünü veren TEK SQL tanımı.

    Hem count_active_tickets_by_technician() hem de Faz 3A routing sorgusu bunu
    kullanır; 'aktif talep' kavramı için ikinci bir SQL yazılmamalıdır.
    Parametre olarak config.WORKLOAD_STATUSES geçilmelidir."""
    ph = ",".join("?" * len(config.WORKLOAD_STATUSES))
    return (
        "SELECT assigned_technician_id AS tid, COUNT(*) AS c FROM tickets "
        f"WHERE assigned_technician_id IS NOT NULL AND status IN ({ph}) "
        "GROUP BY assigned_technician_id"
    )


def count_active_tickets_by_technician():
    """{technician_id: aktif_talep_sayisi} — TEK sorgu, N+1 yok.

    'Aktif talep' tanımı config.WORKLOAD_STATUSES'tan gelir ve bu proje içindeki
    TEK tanımdır: sidebar'daki sayı da, Faz 3'te yazılacak routing de buradan
    beslenmelidir. Aynı kavram için ikinci bir SQL yazmayın.

    NOT: müsaitlik (technicians.available) ile iş yükü AYRI kavramlardır —
    talep atanması available değerini değiştirmez."""
    with get_conn() as conn:
        rows = conn.execute(workload_subquery_sql(),
                            tuple(config.WORKLOAD_STATUSES)).fetchall()
        return {r["tid"]: r["c"] for r in rows}


def count_active_tickets_for_technician(technician_id: int) -> int:
    """Tek teknisyenin iş yükü. count_active_tickets_by_technician ile AYNI
    tanımı kullanır (config.WORKLOAD_STATUSES)."""
    return count_active_tickets_by_technician().get(technician_id, 0)


def list_technicians_with_workload(only_available: bool = False):
    """Faz 3 routing'in ihtiyaç duyacağı üç kavramı tek yerde birleştirir:
    available (yeni iş almaya açık mı), department, active_ticket_count.

    İki sorgu kullanır (teknisyenler + tek GROUP BY); N+1 yoktur."""
    technicians = list_available_technicians() if only_available else list_technicians()
    counts = count_active_tickets_by_technician()
    for t in technicians:
        t["active_ticket_count"] = counts.get(t["id"], 0)
    return technicians


def counts_by_group(technician_id: int = None):
    """Dashboard sayaçları: {'active': n, 'waiting': n, 'resolved': n} — TEK sorgu."""
    with get_conn() as conn:
        if technician_id is not None:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM tickets WHERE assigned_technician_id = ? "
                "GROUP BY status", (technician_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT status, COUNT(*) AS c FROM tickets GROUP BY status").fetchall()
    result = {"active": 0, "waiting": 0, "resolved": 0, "all": 0}
    for r in rows:
        result["all"] += r["c"]
        if r["status"] in config.ACTIVE_STATUSES:
            result["active"] += r["c"]
        elif r["status"] in config.WAITING_STATUSES:
            result["waiting"] += r["c"]
        elif r["status"] in config.RESOLVED_STATUSES:
            result["resolved"] += r["c"]
    return result


# ---------- Faz 2: Çözme / yeniden açma ----------

def resolve_ticket(ticket_id: int, web_user_id: int = None,
                   source: str = None, external_id=None, label: str = None):
    """Talebi çözüldü olarak işaretler ve KİM / NE ZAMAN / HANGİ KANAL bilgisini saklar.

    - web_user_id : web_users.id (web panelinden çözüldüyse, ya da Telegram
                    yöneticisi bir web hesabıyla eşleştirilebildiyse)
    - source      : 'web' | 'telegram' | 'system'
    - external_id : eşleşme yoksa dış kimlik (ör. Telegram kullanıcı ID'si)
    - label       : eşleşme yoksa gösterilecek ad (denetim izi için)"""
    stamp = now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET status = ?, resolved_at = ?, resolved_by_user_id = ?, "
            "resolved_by_source = ?, resolved_by_external_id = ?, resolved_by_label = ?, "
            "updated_at = ? WHERE id = ?",
            (config.STATUS_CLOSED, stamp, web_user_id, source,
             str(external_id) if external_id is not None else None,
             label, stamp, ticket_id),
        )
    # Faz 6A: aktif cycle kapanır (duraklama açıksa önce finalize edilir).
    close_sla_cycle(ticket_id, resolved_at=stamp)


# ---------- Faz 2.5: Parola yönetimi ----------

def set_web_user_password(user_id: int, password_hash: str, must_change: bool = False):
    """Parolayı DAİMA hash olarak saklar; düz metin hiçbir yerde tutulmaz."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE web_users SET password_hash = ?, must_change_password = ?, "
            "password_changed_at = ? WHERE id = ?",
            (password_hash, 1 if must_change else 0, now(), user_id),
        )


def set_web_user_active(user_id: int, active: bool):
    with get_conn() as conn:
        conn.execute("UPDATE web_users SET active = ? WHERE id = ?",
                     (1 if active else 0, user_id))


def clear_resolution_metadata_sql():
    """reopen_ticket içinde kullanılan alan listesi — tek yerde dursun diye."""
    return ("resolved_at = NULL, resolved_by_user_id = NULL, "
            "resolved_by_source = NULL, resolved_by_external_id = NULL, "
            "resolved_by_label = NULL")


def reopen_ticket(ticket_id: int):
    """Aynı talebi yeniden açar — YENİ TALEP OLUŞTURULMAZ, mesaj geçmişi korunur.

    Çözüm metadata'sı (resolved_at / resolved_by_user_id) temizlenir; çünkü bu
    alanlar 'talebin şu anki çözümü'nü tanımlar ve talep artık çözülmüş değildir.
    Kimin ne zaman çözdüğünün izi mesaj geçmişindeki sistem mesajında kalır."""
    stamp = now()
    with get_conn() as conn:
        conn.execute(
            f"UPDATE tickets SET status = ?, {clear_resolution_metadata_sql()}, "
            "first_staff_response_at = NULL, updated_at = ? WHERE id = ?",
            (config.STATUS_REOPENED, stamp, ticket_id),
        )
    # Faz 6A: reopen YENİ bir SLA cycle'ı başlatır. Eski cycle DEĞİŞTİRİLMEZ
    # (immutable geçmiş) ve yeni cycle'da ilk yanıt SLA'sı yeniden başlar.
    # Çift reopen: partial UNIQUE index ikinci aktif cycle'ı engeller.
    start_sla_cycle_business(ticket_id)


def apply_status_sla_effect(ticket_id: int, new_status: str):
    """Durum geçişinin SLA saatine etkisi — TEK KARAR NOKTASI.

    Bot ve web AYNI helper'ı kullanır; iki farklı pause/resume algoritması
    yazılmaz.

      beklemede (WAITING_USER) -> SLA DURAKLAR
      islemde / yeniden_acildi -> SLA DEVAM EDER (duraklamışsa)

    DİKKAT — duraklatMAYAN durumlar:
      * eskalasyon_bekliyor (uzman kuyruğu): uzman kapasitesi KURUMUN
        sorumluluğudur, saat işlemeye devam eder
      * dahili not, AI özeti, sistem mesajı, atama/devir: SLA'ya dokunmaz
    """
    if new_status == config.STATUS_WAITING_USER:
        return pause_sla_business(ticket_id)
    if new_status in (config.STATUS_IN_PROGRESS, config.STATUS_REOPENED,
                      config.STATUS_ASSIGNED):
        return resume_sla_business(ticket_id)
    return False


def set_ticket_status(ticket_id: int, status: str):
    """Durumu günceller. Geçiş geçerliliği ÇAĞIRAN tarafta config.can_transition
    ile kontrol edilir; burada sadece yazma işlemi yapılır.

    Faz 6A: yazmadan sonra SLA duraklama/devam etkisi TEK helper üzerinden
    uygulanır, böylece bot ve web aynı davranışı paylaşır."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), ticket_id),
        )
    apply_status_sla_effect(ticket_id, status)


# ==================================================================
# Faz 3A: Uzmanlık (skill) kataloğu ve teknisyen uzmanlıkları
# ==================================================================

VALID_PRIORITIES = (1, 2, 3)

# Başlangıç kataloğu. Seed idempotenttir; admin sonradan yeni skill ekleyebilir.
# Teknisyenlere OTOMATİK/RASTGELE skill atanmaz — uzmanlıkları admin belirler.
DEFAULT_SKILLS = [
    ("COMPUTER_VISION", "Görüntü İşleme"),
    ("MACHINE_LEARNING", "Machine Learning"),
    ("OCR", "OCR"),
    ("PYTHON_BACKEND", "Python / Backend"),
    ("NETWORK", "Ağ / Network"),
    ("SYSTEM_ADMIN", "Sistem Yönetimi"),
    ("HARDWARE", "Donanım"),
    ("DATABASE", "Veritabanı"),
    # ACCEPTANCE UX + ROUTING REVISION: genel teknik destek.
    # KATALOGDAKİ NORMAL BİR SKILL'DİR — catch-all DEĞİLDİR. Yalnızca model
    # AÇIKÇA classification_type = "general_support" dediğinde kullanılır.
    (config.GENERAL_SUPPORT_SKILL_CODE, config.GENERAL_SUPPORT_SKILL_NAME),
]


def normalize_skill_code(code: str) -> str:
    """'görüntü işleme' -> 'GORUNTU_ISLEME'. Machine-readable, UPPER_SNAKE."""
    text = (code or "").strip().translate(_TR_CODE_MAP)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return text


def create_skill(code: str, name: str):
    """(skill_id, hata_mesaji) döner. Aynı code ikinci kez oluşturulamaz."""
    code = normalize_skill_code(code)
    name = (name or "").strip()
    if not code:
        return None, "Uzmanlık kodu boş olamaz."
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,49}", code):
        return None, "Kod harfle başlamalı; yalnızca A-Z, 0-9 ve alt çizgi içerebilir (2-50 karakter)."
    if not name:
        return None, "Görünen ad boş olamaz."

    stamp = now()
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO skills (code, name, active, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (code, name, stamp, stamp),
            )
            return cur.lastrowid, None
        except sqlite3.IntegrityError:
            return None, f"'{code}' kodlu uzmanlık zaten var."


def list_skills(only_active: bool = False):
    with get_conn() as conn:
        if only_active:
            rows = conn.execute(
                "SELECT * FROM skills WHERE active = 1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skills ORDER BY active DESC, name COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]


def get_skill(skill_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        return dict(row) if row else None


def get_skill_by_code(code: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM skills WHERE code = ?", (normalize_skill_code(code),)
        ).fetchone()
        return dict(row) if row else None


def set_skill_active(skill_id: int, active: bool):
    """Skill SİLİNMEZ, pasifleştirilir — geçmiş atamalar bozulmasın diye."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE skills SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, now(), skill_id),
        )


def seed_default_skills():
    """Başlangıç kataloğunu idempotent şekilde ekler; tekrar çalışınca duplicate
    oluşturmaz ve mevcut kayıtların adını/durumunu DEĞİŞTİRMEZ."""
    created = 0
    for code, name in DEFAULT_SKILLS:
        if get_skill_by_code(code) is None:
            skill_id, _error = create_skill(code, name)
            if skill_id:
                created += 1
    return created


# ---------- Teknisyen uzmanlıkları ----------

def set_technician_skill(technician_id: int, skill_id: int, priority: int):
    """Teknisyene belirtilen öncelikte bir uzmanlık atar. (basarili, mesaj) döner.

    Upsert davranışı — iki uniqueness kuralı da korunur:
      - o öncelikte başka bir skill varsa yerini alır
      - aynı skill başka bir öncelikteyse oraya taşınır (kopya oluşmaz)

    Doğrulama: priority 1/2/3 olmalı, teknisyen var olmalı, skill var ve AKTİF
    olmalı. Formdan gelen değerlere güvenilmez."""
    if priority not in VALID_PRIORITIES:
        return False, "Öncelik yalnızca 1, 2 veya 3 olabilir."
    if get_technician(technician_id) is None:
        return False, "Teknisyen bulunamadı."
    skill = get_skill(skill_id)
    if skill is None:
        return False, "Uzmanlık bulunamadı."
    if not skill["active"]:
        return False, "Pasif bir uzmanlık atanamaz."

    stamp = now()
    with get_conn() as conn:
        # Her iki UNIQUE çakışmasını da temizleyip tek satır yazıyoruz.
        conn.execute(
            "DELETE FROM technician_skills WHERE technician_id = ? AND priority = ?",
            (technician_id, priority),
        )
        conn.execute(
            "DELETE FROM technician_skills WHERE technician_id = ? AND skill_id = ?",
            (technician_id, skill_id),
        )
        conn.execute(
            "INSERT INTO technician_skills (technician_id, skill_id, priority, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (technician_id, skill_id, priority, stamp, stamp),
        )
    return True, f"{skill['name']} → {priority}. uzmanlık olarak kaydedildi."


def remove_technician_skill(technician_id: int, priority: int):
    if priority not in VALID_PRIORITIES:
        return False, "Öncelik yalnızca 1, 2 veya 3 olabilir."
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM technician_skills WHERE technician_id = ? AND priority = ?",
            (technician_id, priority),
        )
    return True, f"{priority}. uzmanlık kaldırıldı."


def list_technician_skills(technician_id: int):
    """Tek teknisyenin uzmanlıkları, öncelik sırasıyla."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts.priority, s.id AS skill_id, s.code AS skill_code, "
            "s.name AS skill_name, s.active AS skill_active "
            "FROM technician_skills ts JOIN skills s ON s.id = ts.skill_id "
            "WHERE ts.technician_id = ? ORDER BY ts.priority ASC",
            (technician_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_technicians_with_skills(only_available: bool = False):
    """Tüm teknisyenler + uzmanlıkları + iş yükü.

    N+1 YOK: teknisyen listesi, iş yükü ve TÜM uzmanlıklar üç sabit sorguyla
    çekilip Python tarafında birleştirilir. Kişi sayısı artınca sorgu sayısı
    artmaz."""
    technicians = list_technicians_with_workload(only_available=only_available)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts.technician_id, ts.priority, s.id AS skill_id, "
            "s.code AS skill_code, s.name AS skill_name, s.active AS skill_active "
            "FROM technician_skills ts JOIN skills s ON s.id = ts.skill_id "
            "ORDER BY ts.technician_id, ts.priority"
        ).fetchall()
    by_tech = {}
    for r in rows:
        by_tech.setdefault(r["technician_id"], []).append(dict(r))
    for t in technicians:
        t["skills"] = by_tech.get(t["id"], [])
    return technicians


def last_primary_assignment_subquery_sql() -> str:
    """'En son ne zaman YENİ ANA İŞ aldı?' sorusunun TEK SQL tanımı.

    Kaynak ticket_assignments'tır — ayrı bir sayaç kolonu TUTULMAZ. Yalnızca
    PRIMARY satırları sayılır; COLLABORATOR ataması 'yeni ana iş aldı'
    ANLAMINA GELMEZ (bkz. count_collaborations).

    removed_at FİLTRELENMEZ: sorulan şey kişinin şu an o işte olup olmadığı
    değil, en son ne zaman yeni bir ana iş ALDIĞIDIR. Devredilmiş bir talep de
    'iş almıştı' sayılır, aksi hâlde devir sonrası kişi haksız yere sıranın
    başına dönerdi.

    Hiç PRIMARY almamış kişi bu tabloda GÖRÜNMEZ; LEFT JOIN sonucu NULL kalır
    ve sıralamada 'en uzun süredir almamış' olarak en öne geçer."""
    return (
        "SELECT technician_id AS tid, MAX(assigned_at) AS last_primary_at "
        "FROM ticket_assignments WHERE assignment_role = 'PRIMARY' "
        "GROUP BY technician_id"
    )


def last_primary_assignment_map():
    """{technician_id: son_PRIMARY_atama_zamani} — TEK sorgu, N+1 yok.

    Aday başına ayrı MAX() sorgusu ÇALIŞTIRILMAZ."""
    with get_conn() as conn:
        rows = conn.execute(last_primary_assignment_subquery_sql()).fetchall()
        return {r["tid"]: r["last_primary_at"] for r in rows}


def count_collaborations_by_technician():
    """{technician_id: acik_yardimcilik_sayisi} — TEK sorgu, N+1 yok.

    DİKKAT: bu değer routing'e GİRMEZ. active_ticket_count yalnızca PRIMARY
    iş yükünü sayar; yardımcılık paneli bilgilendirme amaçlıdır ve otomatik
    atama sıralamasını DEĞİŞTİRMEZ (bkz. count_collaborations)."""
    ph = ",".join("?" * len(config.WORKLOAD_STATUSES))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT a.technician_id AS tid, COUNT(*) AS c FROM ticket_assignments a "
            f"JOIN tickets t ON t.id = a.ticket_id "
            f"WHERE a.removed_at IS NULL AND a.assignment_role = 'COLLABORATOR' "
            f"AND t.status IN ({ph}) GROUP BY a.technician_id",
            tuple(config.WORKLOAD_STATUSES),
        ).fetchall()
        return {r["tid"]: r["c"] for r in rows}


def list_team_overview():
    """AKTİF EKİP paneli için SALT OKUNUR ekip görünümü.

    Hem admin hem teknisyen sağ panelinin TEK veri kaynağıdır; iki ayrı sorgu
    seti yazılmaz. Kişi başına ayrı skills / workload / yardımcılık sorgusu
    ÇALIŞTIRILMAZ — list_technicians_with_skills (3 sorgu) + yardımcılık
    aggregate'i (1 sorgu) = SABİT 4 sorgu, N+1 yok.

    Sıralama: önce yeni iş alabilenler, sonra en az yüklü olan, sonra isim.
    Bu yalnızca GÖRÜNTÜLEME sırasıdır; otomatik atama sırası routing.py'dedir.

    Her satır: id, name, role (unvan), department, available,
    active_ticket_count, collaboration_count, skills (1/2/3 öncelikli)."""
    technicians = list_technicians_with_skills()
    collab_counts = count_collaborations_by_technician()
    for t in technicians:
        t["collaboration_count"] = collab_counts.get(t["id"], 0)
    technicians.sort(key=lambda t: (0 if t["available"] else 1,
                                    t["active_ticket_count"],
                                    (t["name"] or "").lower()))
    return technicians


def list_department_candidates(department: str):
    """Departmanı eşleşen ve YENİ İŞ ALABİLİR (available = 1) teknisyenler.

    ACCEPTANCE FIX 2: skill sınıflandırması başarısız olduğunda kullanılan
    DEPARTMAN FALLBACK'inin veri kaynağı. Eskiden bu yol
    find_available_technician() ile "ilk müsait kişi"yi seçiyordu; artık
    skill yolundaki ADİL sıralamanın aynısını kullanır.

    Skill priority BURADA YOKTUR — required_skill üretilememiştir. Geri kalan
    kriterler birebir aynıdır:

        active_ticket_count ASC -> en az yüklü
        last_primary_at ASC     -> en uzun süredir yeni ana iş almayan
                                   (hiç almamış = NULL, en önde)
        technician_id ASC       -> deterministik final tie-break

    İş yükü ve son-atama tanımları list_skill_candidates ile PAYLAŞILIR
    (workload_subquery_sql / last_primary_assignment_subquery_sql); 'aktif
    talep' ve 'son ana iş' için ikinci bir SQL yazılmaz."""
    sql = (
        "SELECT t.id AS technician_id, t.name AS name, t.department AS department, "
        "COALESCE(wl.c, 0) AS active_ticket_count, "
        "lp.last_primary_at AS last_primary_assignment_at "
        "FROM technicians t "
        f"LEFT JOIN ({workload_subquery_sql()}) wl ON wl.tid = t.id "
        f"LEFT JOIN ({last_primary_assignment_subquery_sql()}) lp ON lp.tid = t.id "
        "WHERE t.department = ? AND t.available = 1 "
        "ORDER BY active_ticket_count ASC, "
        "         CASE WHEN lp.last_primary_at IS NULL THEN 0 ELSE 1 END ASC, "
        "         lp.last_primary_at ASC, t.id ASC"
    )
    params = tuple(config.WORKLOAD_STATUSES) + (department,)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_skill_candidates(skill_id: int):
    """Bu uzmanlığa sahip ve YENİ İŞ ALABİLİR (available = 1) teknisyenler.

    TEK sorgu: technician_skills + technicians + iş yükü alt sorgusu +
    son PRIMARY atama alt sorgusu. İş yükü tanımı db.workload_subquery_sql(),
    son atama tanımı db.last_primary_assignment_subquery_sql() ile paylaşılır —
    routing, sidebar ve kuyruk aynı tanımları kullanır.

    Sıralama (otomatik atama politikası):
        priority ASC            -> en iyi uzmanlık seviyesi
        active_ticket_count ASC -> en az yüklü
        last_primary_at ASC     -> en uzun süredir yeni ana iş almayan
                                   (hiç almamış = NULL, en önde)
        technician_id ASC       -> deterministik final tie-break
    """
    sql = (
        "SELECT ts.priority AS priority, t.id AS technician_id, t.name AS name, "
        "t.department AS department, COALESCE(wl.c, 0) AS active_ticket_count, "
        "lp.last_primary_at AS last_primary_assignment_at "
        "FROM technician_skills ts "
        "JOIN technicians t ON t.id = ts.technician_id "
        f"LEFT JOIN ({workload_subquery_sql()}) wl ON wl.tid = t.id "
        f"LEFT JOIN ({last_primary_assignment_subquery_sql()}) lp ON lp.tid = t.id "
        "WHERE ts.skill_id = ? AND t.available = 1 "
        "ORDER BY ts.priority ASC, active_ticket_count ASC, "
        "         CASE WHEN lp.last_primary_at IS NULL THEN 0 ELSE 1 END ASC, "
        "         lp.last_primary_at ASC, t.id ASC"
    )
    params = tuple(config.WORKLOAD_STATUSES) + (skill_id,)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ==================================================================
# Faz 3B: AI konuşma durumu ve ATOMİK (yarış-güvenli) geçişler
# ==================================================================

def set_ai_mode(ticket_id: int, mode: str):
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET ai_mode = ?, updated_at = ? WHERE id = ?",
                     (mode, now(), ticket_id))


def set_ai_classification(ticket_id: int, category: str = None, skill_code: str = None):
    """Gemini'nin tespit ettiği kategori ve DOĞRULANMIŞ skill kodunu saklar.

    skill_code buraya yalnızca db.get_skill_by_code() ile doğrulandıktan sonra
    gelmelidir; ham model çıktısı doğrudan yazılmaz."""
    with get_conn() as conn:
        if category is not None:
            conn.execute("UPDATE tickets SET category = ?, updated_at = ? WHERE id = ?",
                         (category, now(), ticket_id))
        if skill_code is not None:
            conn.execute("UPDATE tickets SET required_skill_code = ?, updated_at = ? WHERE id = ?",
                         (skill_code, now(), ticket_id))


def set_ticket_routing_basis(ticket_id: int, basis: str):
    """Talebin YÖNLENDİRME DAYANAĞINI saklar (SPECIFIC_SKILL / DEPARTMENT /
    GENERAL_SUPPORT / MANUAL_TRIAGE).

    Yalnızca config.ROUTING_BASES içindeki değerler kabul edilir; model
    çıktısı ya da form değeri doğrudan yazılamaz. Geçersiz değer sessizce
    YOK SAYILIR — bir yazım hatası yüzünden talep kuyruk dışına düşmesin."""
    if basis not in config.ROUTING_BASES:
        return False
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET routing_basis = ?, updated_at = ? WHERE id = ?",
                     (basis, now(), ticket_id))
    return True


def set_ticket_routing(ticket_id: int, basis: str, skill_code: str = None,
                       department: str = None):
    """Sınıflandırma sonucunu TEK TRANSACTION'da yazar.

    skill_code buraya YALNIZCA db.get_skill_by_code() ile doğrulanmış hâlde
    gelmelidir (bkz. routing.resolve_classification). skill_code=None
    açıkça "uzmanlık yok" demektir ve alanı TEMİZLER — set_ai_classification
    ile arasındaki fark budur: orada None "dokunma" anlamına gelir, burada
    "sıfırla". MANUAL_TRIAGE ve DEPARTMENT dayanaklarında eski bir skill
    kodunun talebin üzerinde asılı kalmaması için bu gereklidir."""
    if basis not in config.ROUTING_BASES:
        return False
    stamp = now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET routing_basis = ?, required_skill_code = ?, "
            "category = COALESCE(?, category), pending_department = ?, "
            "updated_at = ? WHERE id = ?",
            (basis, skill_code, department, department, stamp, ticket_id),
        )
    return True


def claim_ai_attempt(ticket_id: int, expected_count: int) -> bool:
    """AI çözüm denemesi sayacını ATOMİK olarak artırır (compare-and-swap).

    Yalnızca sayaç hâlâ expected_count ise artar ve True döner. Kullanıcı aynı
    butona iki kez basarsa ikinci çağrı False döner; böylece tek tıklamayla
    sayaç iki kez artmaz ve kullanıcıya iki farklı çözüm gönderilmez."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tickets SET ai_attempt_count = ai_attempt_count + 1, updated_at = ? "
            "WHERE id = ? AND ai_attempt_count = ?",
            (now(), ticket_id, expected_count),
        )
        return cur.rowcount == 1


def assign_technician_if_unassigned(ticket_id: int, technician_id: int,
                                    technician_name: str) -> bool:
    # LEGACY (Faz 5A) — YENİ KODDA KULLANILMAZ.
    # Faz 4'te assign_primary_if_unassigned() ile değiştirildi.
    """PRIMARY atamayı ATOMİK yapar — yalnızca talep henüz atanmamışsa.

    İki eş zamanlı Telegram callback'i aynı talebi farklı teknisyenlere
    atayamaz: WHERE koşulu ilk yazanı kazandırır, ikincisi rowcount 0 alır.
    (Web tarafındaki form_token yalnızca web içindir; Telegram callback yarışı
    bu koşullu UPDATE ile çözülür.)"""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tickets SET assigned_technician_id = ?, assigned_admin_name = ?, "
            "status = ?, ai_mode = ?, pending_department = NULL, updated_at = ? "
            "WHERE id = ? AND assigned_technician_id IS NULL",
            (technician_id, technician_name, config.STATUS_ASSIGNED,
             config.AI_MODE_HUMAN, stamp, ticket_id),
        )
        return cur.rowcount == 1


def resolve_ticket_if_open(ticket_id: int, source: str, external_id=None,
                           label: str = None, web_user_id: int = None) -> bool:
    """Talebi ATOMİK olarak çözer — yalnızca hâlâ açıksa.

    Aynı 'Çözüldü' butonuna iki kez basmak ikinci kez hata üretmez; ikinci
    çağrı sadece False döner."""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tickets SET status = ?, resolved_at = ?, resolved_by_user_id = ?, "
            "resolved_by_source = ?, resolved_by_external_id = ?, resolved_by_label = ?, "
            "updated_at = ? WHERE id = ? AND status != ?",
            (config.STATUS_CLOSED, stamp, web_user_id, source,
             str(external_id) if external_id is not None else None,
             label, stamp, ticket_id, config.STATUS_CLOSED),
        )
        closed = cur.rowcount == 1
    if closed:
        # Faz 6A: yalnızca gerçekten çözüldüyse cycle kapatılır (idempotent).
        close_sla_cycle(ticket_id, resolved_at=stamp)
    return closed


def mark_escalation_pending(ticket_id: int, department: str = None):
    """Uygun aday bulunamadı: talep KAYBOLMAZ, atanmamış bekleme durumunda kalır.
    Admin panelinde görünür ve manuel atanabilir."""
    stamp = now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET status = ?, escalated = 1, ai_mode = ?, "
            "pending_department = ?, updated_at = ? WHERE id = ?",
            (config.STATUS_ESCALATION_PENDING, config.AI_MODE_HUMAN,
             department, stamp, ticket_id),
        )
        # Uzman kuyruğuna giriş zamanı (yalnızca ilk kez).
        conn.execute(
            "UPDATE tickets SET queue_entered_at = COALESCE(queue_entered_at, ?) WHERE id = ?",
            (stamp, ticket_id),
        )
    # Faz 5B: SLA saati İNSAN eskalasyonunda başlar.
    start_sla(ticket_id)


def get_conversation_history(ticket_id: int, limit: int = 20):
    """Gemini'ye verilecek konuşma geçmişi (en son N mesaj, kronolojik).

    Sistem mesajları dışarıda bırakılır; modele yalnızca gerçek diyalog gider.

    Faz 5A: DAHİLİ NOTLAR BU BAĞLAMA GİRMEZ. Kullanıcıya yanıt üretmek için
    kullanılan bu geçmiş, dahili notların kullanıcıya sızmasına yol açabilecek
    tek yer olurdu. Dahili notlar yalnızca get_summary_context() içinde ve
    AÇIKÇA etiketlenerek kullanılır."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sender_type, message_text, message_kind FROM messages "
            "WHERE ticket_id = ? AND sender_type != 'sistem' "
            "AND (message_kind IS NULL OR message_kind != ?) "
            "ORDER BY id DESC LIMIT ?",
            (ticket_id, config.MESSAGE_KIND_INTERNAL_NOTE, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def count_ai_solution_messages(ticket_id: int) -> int:
    """Denetim: geçmişte kaç AI mesajı gerçek çözüm denemesiydi?"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE ticket_id = ? AND message_kind = ?",
            (ticket_id, config.MESSAGE_KIND_AI_SOLUTION),
        ).fetchone()
        return row["c"]


# ==================================================================
# Faz 4: PRIMARY + COLLABORATOR atama modeli
# ==================================================================
#
# İKİ KAYNAK, TEK GERÇEK:
#   tickets.assigned_technician_id  -> talebin ŞU ANKİ PRIMARY teknisyeni
#   ticket_assignments              -> atama GEÇMİŞİ + aktif COLLABORATOR'lar
# İkisi birbirinden kopmamalıdır; PRIMARY değiştiren her işlem ikisini de
# AYNI transaction içinde günceller.


def _actor_columns(actor: dict):
    """Audit alanlarını tek yerden üretir (web / telegram / system / migration)."""
    actor = actor or {}
    return (
        actor.get("user_id"),
        actor.get("source") or config.ASSIGN_SOURCE_SYSTEM,
        str(actor["external_id"]) if actor.get("external_id") is not None else None,
        actor.get("label"),
    )


def _insert_assignment(conn, ticket_id, technician_id, role, actor):
    user_id, source, external_id, label = _actor_columns(actor)
    conn.execute(
        "INSERT INTO ticket_assignments "
        "(ticket_id, technician_id, assignment_role, assigned_by_user_id, "
        " assigned_by_source, assigned_by_external_id, assigned_by_label, assigned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, technician_id, role, user_id, source, external_id, label, now()),
    )


def _close_assignment(conn, ticket_id, technician_id=None, role=None, reason=None):
    sql = "UPDATE ticket_assignments SET removed_at = ?, removal_reason = ? " \
          "WHERE ticket_id = ? AND removed_at IS NULL"
    params = [now(), reason, ticket_id]
    if technician_id is not None:
        sql += " AND technician_id = ?"
        params.append(technician_id)
    if role is not None:
        sql += " AND assignment_role = ?"
        params.append(role)
    return conn.execute(sql, params).rowcount


# ---------- Okuma ----------

def list_ticket_assignments(ticket_id: int, active_only: bool = True):
    """Talebin atamaları (teknisyen adı ve departmanıyla birlikte) — TEK sorgu."""
    sql = (
        "SELECT a.*, t.name AS technician_name, t.department AS technician_department, "
        "       t.available AS technician_available, w.full_name AS assigned_by_name "
        "FROM ticket_assignments a "
        "JOIN technicians t ON t.id = a.technician_id "
        "LEFT JOIN web_users w ON w.id = a.assigned_by_user_id "
        "WHERE a.ticket_id = ?"
    )
    if active_only:
        sql += " AND a.removed_at IS NULL"
    sql += " ORDER BY CASE a.assignment_role WHEN 'PRIMARY' THEN 0 ELSE 1 END, a.id"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, (ticket_id,)).fetchall()]


def list_active_collaborators(ticket_id: int):
    return [a for a in list_ticket_assignments(ticket_id, active_only=True)
            if a["assignment_role"] == config.ROLE_COLLABORATOR]


def get_technician_ticket_role(ticket_id: int, technician_id: int):
    """'PRIMARY' | 'COLLABORATOR' | None — teknisyenin bu talepteki AKTİF rolü.

    Eski kayıtlar için tickets.assigned_technician_id fallback'i korunur."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT assignment_role FROM ticket_assignments "
            "WHERE ticket_id = ? AND technician_id = ? AND removed_at IS NULL",
            (ticket_id, technician_id),
        ).fetchone()
        if row:
            return row["assignment_role"]
        # Faz 4 öncesi / backfill dışı kalmış kayıtlar
        legacy = conn.execute(
            "SELECT 1 FROM tickets WHERE id = ? AND assigned_technician_id = ?",
            (ticket_id, technician_id),
        ).fetchone()
        return config.ROLE_PRIMARY if legacy else None


def get_ticket_for_technician_role(ticket_id: int, technician_id: int):
    """Teknisyen PRIMARY ya da aktif COLLABORATOR ise talebi döner, aksi hâlde None.

    Yetkilendirme SQL seviyesinde: EXISTS alt sorgusu + legacy fallback."""
    with get_conn() as conn:
        row = conn.execute(
            _TICKET_SELECT + """
            WHERE t.id = ?
              AND (t.assigned_technician_id = ?
                   OR EXISTS (SELECT 1 FROM ticket_assignments a
                              WHERE a.ticket_id = t.id
                                AND a.technician_id = ?
                                AND a.removed_at IS NULL))
            """,
            (ticket_id, technician_id, technician_id),
        ).fetchone()
        return dict(row) if row else None


def list_collaborator_tickets(technician_id: int, group: str = None):
    """Teknisyenin AKTİF COLLABORATOR olduğu talepler.

    PRIMARY olduğu talepler DAHİL EDİLMEZ; iki liste birbirine karışmaz.
    N+1 yok: tek JOIN sorgusu."""
    statuses = _statuses_for_group(group)
    sql = _TICKET_SELECT + (
        " JOIN ticket_assignments ca ON ca.ticket_id = t.id "
        " AND ca.technician_id = ? AND ca.removed_at IS NULL "
        " AND ca.assignment_role = 'COLLABORATOR' WHERE 1=1"
    )
    params = [technician_id]
    if statuses:
        sql += f" AND t.status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    sql += " ORDER BY t.updated_at DESC, t.id DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_collaborations(technician_id: int) -> int:
    """Yardımcı olunan AÇIK talep sayısı.

    DİKKAT: bu değer count_active_tickets_by_technician() ile KARIŞTIRILMAZ.
    Faz 3 routing'in workload sıralaması yalnızca PRIMARY taleplere dayanır ve
    bu fonksiyon oraya dahil EDİLMEZ."""
    ph = ",".join("?" * len(config.WORKLOAD_STATUSES))
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM ticket_assignments a "
            f"JOIN tickets t ON t.id = a.ticket_id "
            f"WHERE a.technician_id = ? AND a.removed_at IS NULL "
            f"AND a.assignment_role = 'COLLABORATOR' AND t.status IN ({ph})",
            (technician_id, *config.WORKLOAD_STATUSES),
        ).fetchone()
        return row["c"]


# ---------- PRIMARY atama ----------

def assign_primary_if_unassigned(ticket_id: int, technician_id: int,
                                 technician_name: str, actor: dict = None) -> bool:
    """Faz 3B'nin atomik atamasının Faz 4 karşılığı.

    Yalnızca talep henüz atanmamışsa başarılı olur (koşullu UPDATE) ve AYNI
    transaction içinde PRIMARY assignment satırını da yazar. Faz 3B'nin
    concurrency koruması korunur: iki eş zamanlı callback iki PRIMARY üretemez."""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            # Faz 5A: legacy assigned_admin_name'e ARTIK YAZILMIYOR.
            # Ana sorumlu adı assigned_technician_id -> technicians.name ile çözülür.
            "UPDATE tickets SET assigned_technician_id = ?, "
            "status = ?, ai_mode = ?, pending_department = NULL, updated_at = ? "
            "WHERE id = ? AND assigned_technician_id IS NULL",
            (technician_id, config.STATUS_ASSIGNED,
             config.AI_MODE_HUMAN, stamp, ticket_id),
        )
        if cur.rowcount != 1:
            return False
        # Hedef zaten yardımcıysa o satırı kapatıp PRIMARY'ye yükseltiyoruz.
        _close_assignment(conn, ticket_id, technician_id=technician_id,
                          reason="PRIMARY olarak atandı")
        _insert_assignment(conn, ticket_id, technician_id, config.ROLE_PRIMARY, actor)
    # Faz 5B: insan devraldı -> SLA saati başlar (yalnızca ilk kez).
    start_sla(ticket_id)
    return True


def transfer_primary(ticket_id: int, new_technician_id: int,
                     expected_current_id: int, reason: str, actor: dict = None):
    """Ana sorumluluğu devreder. (basarili, mesaj) döner.

    TEK TRANSACTION içinde:
      1. mevcut aktif PRIMARY assignment removed_at ile kapatılır
      2. hedefin aktif COLLABORATOR kaydı varsa kapatılır (duplicate olmasın)
      3. hedef için yeni PRIMARY assignment açılır
      4. tickets.assigned_technician_id güncellenir
    Herhangi biri patlarsa TAMAMI geri alınır.

    Yarış koruması: tickets üzerindeki koşullu UPDATE (compare-and-swap) —
    iki eş zamanlı transferden yalnızca biri başarılı olur.

    ESKİ PRIMARY OTOMATİK COLLABORATOR YAPILMAZ; bu gerçek bir ownership
    değişimidir. İstenirse ayrıca 'Yardımcı Ekle' kullanılır."""
    reason = (reason or "").strip()
    if len(reason) < config.TRANSFER_REASON_MIN_LENGTH:
        return False, (f"Devir gerekçesi zorunludur "
                       f"(en az {config.TRANSFER_REASON_MIN_LENGTH} karakter).")
    if new_technician_id == expected_current_id:
        return False, "Talep zaten bu kişiye ait."
    technician = get_technician(new_technician_id)
    if not technician:
        return False, "Hedef teknisyen bulunamadı."

    stamp = now()
    with get_conn() as conn:
        # CAS: PRIMARY hâlâ beklediğimiz kişide mi?
        cur = conn.execute(
            # Faz 5A: legacy assigned_admin_name'e ARTIK YAZILMIYOR.
            "UPDATE tickets SET assigned_technician_id = ?, updated_at = ? "
            "WHERE id = ? AND assigned_technician_id IS ?",
            (new_technician_id, stamp, ticket_id, expected_current_id),
        )
        if cur.rowcount != 1:
            return False, "Talebin ana sorumlusu bu sırada değişti. Sayfayı yenileyin."

        _close_assignment(conn, ticket_id, role=config.ROLE_PRIMARY, reason=reason)
        # Hedef yardımcıysa o kaydı kapat -> aktif çift atama oluşmaz.
        _close_assignment(conn, ticket_id, technician_id=new_technician_id,
                          reason="Ana sorumluluğa yükseltildi")
        _insert_assignment(conn, ticket_id, new_technician_id, config.ROLE_PRIMARY, actor)

    return True, f"Ana sorumluluk {technician['name']} adlı kişiye devredildi."


# ---------- COLLABORATOR ----------

def add_collaborator(ticket_id: int, technician_id: int, actor: dict = None,
                     require_available: bool = True):
    """Talebe yardımcı teknik personel ekler. (basarili, mesaj) döner.

    Doğrulamalar: teknisyen var mı, PRIMARY mi, zaten aktif yardımcı mı,
    (varsayılan olarak) yeni iş almaya açık mı?"""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return False, "Talep bulunamadı."
    technician = get_technician(technician_id)
    if not technician:
        return False, "Teknisyen bulunamadı."

    role = get_technician_ticket_role(ticket_id, technician_id)
    if role == config.ROLE_PRIMARY:
        return False, f"{technician['name']} bu talebin ana sorumlusu; yardımcı olarak eklenemez."
    if role == config.ROLE_COLLABORATOR:
        return False, f"{technician['name']} zaten bu talepte yardımcı olarak görevli."
    if require_available and not technician["available"]:
        return False, f"{technician['name']} şu anda yeni iş almaya kapalı."

    try:
        with get_conn() as conn:
            _insert_assignment(conn, ticket_id, technician_id,
                               config.ROLE_COLLABORATOR, actor)
    except sqlite3.IntegrityError:
        # Partial UNIQUE index yarışı yakaladı.
        return False, f"{technician['name']} zaten bu talepte görevli."
    return True, f"{technician['name']} yardımcı teknik personel olarak eklendi."


def remove_collaborator(ticket_id: int, technician_id: int, reason: str = None):
    """Yardımcıyı görevden çıkarır. SİLME YOK — removed_at doldurulur,
    atama geçmişi korunur."""
    technician = get_technician(technician_id)
    if not technician:
        return False, "Teknisyen bulunamadı."
    with get_conn() as conn:
        closed = _close_assignment(conn, ticket_id, technician_id=technician_id,
                                   role=config.ROLE_COLLABORATOR,
                                   reason=reason or "Görevden çıkarıldı")
    if not closed:
        return False, "Bu kişi talepte aktif yardımcı değil."
    return True, f"{technician['name']} yardımcı teknik personel görevinden çıkarıldı."


def list_collaborator_candidates(ticket_id: int, only_available: bool = True):
    """Yardımcı seçim ekranı: ad, departman, 1/2/3 uzmanlık, iş yükü, müsaitlik.

    N+1 YOK — Faz 3A'nın list_technicians_with_skills() helper'ı kullanılır
    (sabit sayıda sorgu), üstüne tek bir aktif-atama sorgusu eklenir."""
    technicians = list_technicians_with_skills(only_available=only_available)
    with get_conn() as conn:
        busy = {r["technician_id"] for r in conn.execute(
            "SELECT technician_id FROM ticket_assignments "
            "WHERE ticket_id = ? AND removed_at IS NULL", (ticket_id,)).fetchall()}
    ticket = get_ticket(ticket_id)
    if ticket and ticket.get("assigned_technician_id"):
        busy.add(ticket["assigned_technician_id"])
    return [t for t in technicians if t["id"] not in busy]


# ==================================================================
# Faz 5A: Dahili notlar ve AI özeti
# ==================================================================

def add_internal_note(ticket_id: int, sender_type: str, sender_name: str,
                      note_text: str, sender_user_id: int):
    """Yalnızca teknik ekibin gördüğü dahili not ekler.

    TELEGRAM İZOLASYONU: bu fonksiyon delivery_status'u KASITLI olarak
    doldurmaz ve hiçbir gönderim fonksiyonu çağırmaz. Dahili notun kullanıcıya
    ulaşabileceği tek yol public reply yolundan geçmektir; o yol ayrı bir
    fonksiyondur (webapp.reply) ve bu fonksiyonu asla çağırmaz."""
    return add_message(
        ticket_id, sender_type, 0, sender_name, note_text,
        sender_user_id=sender_user_id,
        delivery_status=None,                       # Telegram gönderimi YOK
        message_kind=config.MESSAGE_KIND_INTERNAL_NOTE,
    )


def is_internal(message: dict) -> bool:
    return message.get("message_kind") == config.MESSAGE_KIND_INTERNAL_NOTE


def get_last_message_id(ticket_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS mid FROM messages WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return row["mid"]


# ---------- AI özeti ----------

_SUMMARY_ROLE_LABELS = {
    "kullanici": "USER",
    "ai": "AI",
    "teknisyen": "TECHNICIAN",
    "yonetici": "TECHNICIAN",
    "sistem": "SYSTEM",
}


def get_summary_context(ticket_id: int, include_internal_notes: bool = None):
    """AI özeti için ROL ETİKETLİ konuşma dökümü.

    VERİ YÖNETİŞİMİ (Faz 5B.1): dahili notların bu bağlama girip girmeyeceği
    config.AI_SUMMARY_INCLUDE_INTERNAL_NOTES ile yönetilir.

      True  -> notlar 'INTERNAL_NOTE:' etiketiyle dahil edilir (varsayılan,
               Faz 5A davranışı korunur; model onları kullanıcı mesajı sanmaz)
      False -> notlar bu bağlama HİÇ girmez, yani Gemini'ye gönderilmez

    Bayrak YALNIZCA AI özetini etkiler: dahili notlar web panelinde yetkili
    teknik ekibe görünmeye devam eder ve hiçbir durumda Telegram'a gitmez.

    Bayrak ÇAĞRI ANINDA okunur (import anında değil); testler ve çalışan
    süreçler değeri güvenilir biçimde değiştirebilir."""
    if include_internal_notes is None:
        include_internal_notes = config.AI_SUMMARY_INCLUDE_INTERNAL_NOTES

    lines = []
    for m in get_messages(ticket_id):
        kind = m.get("message_kind")
        if kind == config.MESSAGE_KIND_INTERNAL_NOTE:
            if not include_internal_notes:
                continue          # kurum politikası: not modele gitmez
            label = "INTERNAL_NOTE"
        elif kind == config.MESSAGE_KIND_AI_SOLUTION:
            label = "AI_SOLUTION"
        elif kind == config.MESSAGE_KIND_AI_CLARIFICATION:
            label = "AI_CLARIFICATION"
        else:
            label = _SUMMARY_ROLE_LABELS.get(m.get("sender_type"), "SYSTEM")
        who = m.get("sender_full_name") or m.get("sender_name") or ""
        lines.append(f"{label} ({who}):\n{m.get('message_text', '')}")
    return "\n\n".join(lines)


def save_ai_summary(ticket_id: int, summary: dict, generated_by_user_id: int = None):
    """Özeti kaydeder (talep başına TEK satır, upsert).

    İki teknisyen aynı anda basarsa last-write-wins olur; UNIQUE(ticket_id) +
    ON CONFLICT sayesinde duplicate ya da yarım kayıt oluşmaz."""
    payload = json.dumps(summary, ensure_ascii=False)
    last_id = get_last_message_id(ticket_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ticket_ai_summaries "
            "(ticket_id, summary_json, generated_by_user_id, generated_at, "
            " source_last_message_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket_id) DO UPDATE SET "
            "  summary_json = excluded.summary_json, "
            "  generated_by_user_id = excluded.generated_by_user_id, "
            "  generated_at = excluded.generated_at, "
            "  source_last_message_id = excluded.source_last_message_id",
            (ticket_id, payload, generated_by_user_id, now(), last_id),
        )
    return last_id


def get_ai_summary(ticket_id: int):
    """Son özeti döner; 'is_stale' ile güncelliğini de bildirir.

    Özet üretildikten sonra yeni mesaj geldiyse (son mesaj id'si kayıtlı
    source_last_message_id'den büyükse) özet güncel değildir."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.*, w.full_name AS generated_by_name "
            "FROM ticket_ai_summaries s "
            "LEFT JOIN web_users w ON w.id = s.generated_by_user_id "
            "WHERE s.ticket_id = ?",
            (ticket_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["summary"] = json.loads(data["summary_json"])
    except Exception:
        # Bozuk kayıt paneli çökertmesin.
        data["summary"] = None
    last_id = get_last_message_id(ticket_id)
    data["is_stale"] = bool(
        last_id and data.get("source_last_message_id")
        and last_id > data["source_last_message_id"]
    )
    return data


# ==================================================================
# Faz 5B: Öncelik, SLA, uzman kuyruğu ve giriş hız sınırı
# ==================================================================

_TS = "%Y-%m-%d %H:%M:%S"


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS)
    except (ValueError, TypeError):
        return None


def _fmt_ts(dt):
    return dt.strftime(_TS) if dt else None


# ---------- Öncelik ----------

def priority_source(ticket: dict) -> str:
    """Talebin önceliğine EN SON kimin karar verdiği.

    Revizyondan ÖNCEKİ kayıtlarda kolon NULL'dır; bunlar 'default' sayılır —
    yani AI onlara bir kez öneri uygulayabilir. Geriye dönük veri yazımı
    YAPILMAZ."""
    value = (ticket or {}).get("priority_source")
    return value if value in config.PRIORITY_SOURCES else config.PRIORITY_SOURCE_DEFAULT


def set_ticket_priority(ticket_id: int, priority: str,
                        source: str = config.PRIORITY_SOURCE_MANUAL):
    """Önceliği değiştirir ve SLA son tarihlerini YENİDEN HESAPLAR.

    KURAL: sla_started_at DEĞİŞMEZ. Önceliği yükseltmek gecikmeyi gizlemez —
    P3'te 10:00'da başlayan bir talep 10:20'de P1 yapılırsa yeni son tarih
    yine 10:00 + P1 penceresidir. (basarili, mesaj) döner.

    PRIORITY ROUTING REVISION: `source` VARSAYILAN OLARAK 'manual'dır.
    Web panelinden (admin ya da ana sorumlu) gelen her değişiklik insan
    kararıdır ve AI'ın bir daha üzerine yazmasını KİLİTLER. AI yolu bu
    fonksiyonu doğrudan çağırmaz; apply_ai_priority() üzerinden geçer."""
    if priority not in config.PRIORITIES:
        return False, "Geçersiz öncelik."
    if source not in config.PRIORITY_SOURCES:
        source = config.PRIORITY_SOURCE_MANUAL
    ticket = get_ticket(ticket_id)
    if not ticket:
        return False, "Talep bulunamadı."

    started = ticket.get("sla_started_at")
    policy = config.sla_policy_for(priority)
    first_due = resolution_due = None
    if started:
        base = _parse_ts(started)
        if base:
            first_due = _fmt_ts(base + timedelta(minutes=policy["first_response_minutes"]))
            resolution_due = _fmt_ts(base + timedelta(minutes=policy["resolution_minutes"]))

    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET priority = ?, priority_source = ?, "
            "first_response_due_at = ?, resolution_due_at = ?, updated_at = ? "
            "WHERE id = ?",
            (priority, source, first_due, resolution_due, now(), ticket_id),
        )
    # Faz 6A: yalnızca AKTİF cycle güncellenir; kapalı cycle'lar IMMUTABLE.
    apply_priority_business(ticket_id, priority)
    return True, f"Öncelik {config.PRIORITY_LABELS[priority]} olarak güncellendi."


def apply_ai_priority(ticket_id: int, priority: str, reason: str = None):
    """DOĞRULANMIŞ AI öncelik önerisini uygular — ADMIN KARARINI EZMEZ.

    PRIORITY ROUTING REVISION §B3. Akışın tamamı:

        Gemini -> routing._validated_priority() -> BU FONKSİYON -> ticket

    Buraya gelen `priority` ZATEN doğrulanmıştır; yine de son bir kez
    kontrol edilir (savunmacı katman — çağıran taraf hata yapsa bile
    uydurma bir değer DB'ye yazılamaz) ve geçersizse P3 uygulanır.

    KİLİT KURALI: priority_source == 'manual' ise öncelik DEĞİŞTİRİLMEZ.
    Admin/ana sorumlu bir kez elle karar verdiyse AI onu geri alamaz. Modelin
    HAM önerisi ve gerekçesi yine de denetim için kaydedilir; böylece panelde
    "AI P1 önerdi, admin P3'te bıraktı" görünebilir.

    SLA: uygulanan öncelik set_ticket_priority() üzerinden geçer, yani mevcut
    priority-change davranışı (başlangıç sabit, son tarihler yeniden hesap,
    aktif cycle güncellenir, kapalı cycle IMMUTABLE) AYNEN korunur.

    Döner: (uygulandi_mi, yururlukteki_oncelik)"""
    if priority not in config.PRIORITIES:
        priority = config.DEFAULT_PRIORITY
    ticket = get_ticket(ticket_id)
    if not ticket:
        return False, config.DEFAULT_PRIORITY

    # Denetim izi: modelin ham önerisi HER ZAMAN saklanır (uygulansa da
    # uygulanmasa da). Bu alanlar routing kararı DEĞİLDİR, yalnızca kayıttır.
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET ai_suggested_priority = ?, ai_priority_reason = ? "
            "WHERE id = ?",
            (priority, reason, ticket_id),
        )

    if priority_source(ticket) == config.PRIORITY_SOURCE_MANUAL:
        # ADMIN OVERRIDE KORUNUR — AI üzerine yazmaz.
        return False, ticket.get("priority") or config.DEFAULT_PRIORITY

    ok, _message = set_ticket_priority(ticket_id, priority,
                                       source=config.PRIORITY_SOURCE_AI)
    return bool(ok), priority


def get_ai_priority_advice(ticket_id: int):
    """Panelde gösterilecek AI öncelik önerisi + gerekçesi.

    İÇ OPERASYON verisidir: yalnızca admin/teknik ekip panelinde gösterilir,
    Telegram kullanıcısına gönderilen HİÇBİR metin bu alanları kullanmaz.
    Öneri yoksa None döner."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return None
    suggested = ticket.get("ai_suggested_priority")
    if suggested not in config.PRIORITIES:
        return None
    return {
        "suggested_priority": suggested,
        "reason": ticket.get("ai_priority_reason"),
        "source": priority_source(ticket),
        "applied": priority_source(ticket) == config.PRIORITY_SOURCE_AI,
        "overridden": (priority_source(ticket) == config.PRIORITY_SOURCE_MANUAL
                       and ticket.get("priority") != suggested),
    }


# ---------- SLA ----------

def start_sla(ticket_id: int):
    """SLA saatini başlatır — YALNIZCA insan eskalasyonunda ve YALNIZCA bir kez.

    AI_HANDLING aşaması SLA'ya dahil edilmez: AI'ın kendi başına çözdüğü
    talepler teknik ekibin SLA metriğini bozmamalıdır."""
    ticket = get_ticket(ticket_id)
    if not ticket or ticket.get("sla_started_at"):
        return False
    # Faz 6B: yeni cycle'lar İŞ TAKVİMİNE göre açılır (BUSINESS_HOURS_V1).
    return start_sla_cycle_business(ticket_id) is not None


def mark_first_staff_response(ticket_id: int) -> bool:
    """İlk PUBLIC personel yanıtını damgalar — yalnızca NULL ise.

    Dahili not, sistem mesajı ve AI mesajı ilk yanıt SAYILMAZ; bu fonksiyon
    yalnızca webapp.reply (kullanıcıya giden yanıt) yolundan çağrılır."""
    # Faz 6A: damga AKTİF cycle'a atılır (reopen sonrası yeniden başlar).
    if mark_cycle_first_response(ticket_id):
        return True
    # Cycle yoksa (SLA hiç başlamamış) legacy davranışa düş.
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tickets SET first_staff_response_at = ? "
            "WHERE id = ? AND first_staff_response_at IS NULL",
            (now(), ticket_id),
        )
        return cur.rowcount == 1


def _deadline_state(due_at, done_at, reference, window_minutes):
    """(durum, kalan_dakika, seviye) döner.

    durum : pending | met | overdue
    seviye: normal | warning | overdue   (UI rengi için; metin de gösterilir)"""
    due = _parse_ts(due_at)
    if not due:
        return config.SLA_STATE_PENDING, None, config.SLA_LEVEL_NORMAL

    done = _parse_ts(done_at)
    if done:
        # Tamamlanmış: son tarihe göre karşılandı mı?
        state = config.SLA_STATE_MET if done <= due else config.SLA_STATE_OVERDUE
        level = config.SLA_LEVEL_NORMAL if state == config.SLA_STATE_MET else config.SLA_LEVEL_OVERDUE
        return state, int((due - done).total_seconds() // 60), level

    remaining = int((due - reference).total_seconds() // 60)
    if remaining < 0:
        return config.SLA_STATE_OVERDUE, remaining, config.SLA_LEVEL_OVERDUE
    if window_minutes and remaining <= window_minutes * config.SLA_WARNING_RATIO:
        return config.SLA_STATE_PENDING, remaining, config.SLA_LEVEL_WARNING
    return config.SLA_STATE_PENDING, remaining, config.SLA_LEVEL_NORMAL


def get_sla_state(ticket: dict, reference: datetime = None):
    """Talebin SLA durumunu runtime'da hesaplar (DB'de tutulmaz).

    Çözülmüş talepte çözüm SLA'sı resolved_at üzerinden değerlendirilir."""
    # Faz 6A: AKTİF cycle varsa onun üzerinden hesapla (duraklama dahil).
    cycle = get_active_cycle(ticket["id"]) if ticket.get("id") else None
    if cycle:
        return get_cycle_sla_state(cycle, reference)
    # Aktif cycle yoksa (çözülmüş talep) SON KAPANAN cycle'ın sonucu gösterilir.
    if ticket.get("id"):
        history = list_sla_cycles(ticket["id"])
        if history:
            return get_cycle_sla_state(history[0], reference)

    reference = reference or datetime.now()
    priority = ticket.get("priority") or config.DEFAULT_PRIORITY
    policy = config.sla_policy_for(priority)
    started = ticket.get("sla_started_at")

    fr_state, fr_left, fr_level = _deadline_state(
        ticket.get("first_response_due_at"), ticket.get("first_staff_response_at"),
        reference, policy["first_response_minutes"])
    res_state, res_left, res_level = _deadline_state(
        ticket.get("resolution_due_at"), ticket.get("resolved_at"),
        reference, policy["resolution_minutes"])

    return {
        "priority": priority,
        "priority_label": config.PRIORITY_LABELS.get(priority, priority),
        "started": bool(started),
        "started_at": started,
        "first_response_due_at": ticket.get("first_response_due_at"),
        "resolution_due_at": ticket.get("resolution_due_at"),
        "first_response_status": fr_state,
        "first_response_level": fr_level,
        "minutes_to_first_response": fr_left,
        "resolution_status": res_state,
        "resolution_level": res_level,
        "minutes_to_resolution": res_left,
    }


def _sla_where_overdue():
    """Geciken talepler için WHERE parçası.

    Faz 6A: sayım YALNIZCA AKTİF cycle üzerinden yapılır (kapanmış geçmiş
    cycle'lar dashboard'a karışmaz) ve DURAKLAMIŞ cycle'lar gecikmiş
    SAYILMAZ — kullanıcı beklenirken saat işlemez."""
    ph = ",".join("?" * len(config.UNRESOLVED_STATUSES))
    return (
        f"t.status IN ({ph}) AND EXISTS ("
        "  SELECT 1 FROM sla_cycles c WHERE c.ticket_id = t.id AND c.closed_at IS NULL "
        "    AND c.paused_at IS NULL AND ("
        "      (c.first_staff_response_at IS NULL AND c.first_response_due_at IS NOT NULL "
        "       AND c.first_response_due_at < ?) OR "
        "      (c.resolution_due_at IS NOT NULL AND c.resolution_due_at < ?)))"
    )


def operations_counts():
    """Admin operasyon sayaçları — SABİT sayıda sorgu, N+1 yok."""
    stamp = now()
    unresolved = tuple(config.UNRESOLVED_STATUSES)
    ph = ",".join("?" * len(unresolved))
    with get_conn() as conn:
        base = dict(counts_by_group())
        p1 = conn.execute(
            f"SELECT COUNT(*) AS c FROM tickets WHERE priority = ? AND status IN ({ph})",
            (config.PRIORITY_P1, *unresolved),
        ).fetchone()["c"]
        overdue = conn.execute(
            f"SELECT COUNT(*) AS c FROM tickets t WHERE {_sla_where_overdue()}",
            (*unresolved, stamp, stamp),
        ).fetchone()["c"]
        waiting_expert = conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE status = ?",
            (config.STATUS_ESCALATION_PENDING,),
        ).fetchone()["c"]
        # Yaklaşan: son tarihi gelmemiş ama uyarı eşiğine girmiş olanlar.
        # Eşik önceliğe bağlı olduğu için satırları çekip Python'da süzüyoruz;
        # yalnızca ÇÖZÜLMEMİŞ ve SLA'sı başlamış talepler taranır.
        # Yaklaşan: yalnızca AKTİF cycle'lar. Eşik önceliğe bağlı olduğu için
        # satırlar çekilip Python'da süzülür; talep başına ek sorgu YOK.
        rows = conn.execute(
            f"SELECT c.* FROM sla_cycles c JOIN tickets t ON t.id = c.ticket_id "
            f"WHERE c.closed_at IS NULL AND t.status IN ({ph})",
            unresolved,
        ).fetchall()

    warning = 0
    for r in rows:
        state = get_cycle_sla_state(dict(r))
        if state and config.SLA_LEVEL_WARNING in (state["first_response_level"],
                                                  state["resolution_level"]):
            warning += 1

    base.update({"p1": p1, "sla_overdue": overdue,
                 "sla_warning": warning, "waiting_expert": waiting_expert})
    return base


def list_admin_tickets_filtered(group: str = None, priority: str = None, sla: str = None):
    """Admin listesi: durum + öncelik + SLA filtreleri. Mevcut davranışı bozmaz."""
    tickets = list_admin_tickets(group)
    if priority in config.PRIORITIES:
        tickets = [t for t in tickets if (t.get("priority") or config.DEFAULT_PRIORITY) == priority]
    if sla in (config.SLA_LEVEL_WARNING, config.SLA_LEVEL_OVERDUE):
        out = []
        for t in tickets:
            state = get_sla_state(t)
            if sla in (state["first_response_level"], state["resolution_level"]):
                out.append(t)
        tickets = out
    return tickets


# ---------- Uzman kuyruğu ----------

def enter_expert_queue(ticket_id: int):
    """Talebi uzman kuyruğuna alır (queue_entered_at yalnızca ilk kez yazılır)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET queue_entered_at = COALESCE(queue_entered_at, ?), "
            "updated_at = ? WHERE id = ?",
            (now(), now(), ticket_id),
        )


#: Skill yoluyla (uzmanlık kodu üzerinden) değerlendirilen dayanaklar.
#: GENERAL_SUPPORT da bir SKILL kuyruğudur — GENERAL_TECH_SUPPORT kodu ile.
_SKILL_QUEUE_BASES = (config.ROUTING_BASIS_SPECIFIC_SKILL,
                      config.ROUTING_BASIS_GENERAL_SUPPORT)


# ==================================================================
# QUEUE PRIORITY + FIFO — KUYRUK İŞLEME SIRASININ TEK TANIMI
# ==================================================================
#
# PRIORITY ROUTING REVISION §C. Mesai dışında (özellikle hafta sonu) biriken
# talepler Pazartesi DETERMİNİSTİK bir sırayla işlenir:
#
#     1. priority severity      P1 -> P2 -> P3 -> P4
#     2. queue_entered_at ASC   aynı öncelik içinde FIFO (en eski önce)
#     3. ticket_id ASC          zaman damgası eşitse deterministik tie-break
#
# BU YALNIZCA İŞLEME SIRASIDIR — TEKNİSYEN SEÇİMİ DEĞİLDİR (§C2).
# Hangi teknisyenin atanacağına routing.py karar verir ve o algoritma
# DEĞİŞMEDİ: uzmanlık önceliği -> müsaitlik -> iş yükü -> en uzun süredir
# ana iş almayan -> technician_id.
#
# queue_entered_at NULL olabilen tek durum, kuyruğa normal yoldan girmemiş
# (doğrudan SQL ile durum yazılmış) satırlardır; COALESCE ile created_at'e
# düşülür, böylece NULL bir talep sırayı bozup en öne geçemez.

#: Öncelik SEVERITY sırası. Alfabetik sıralama tesadüfen doğru sonuç verir
#: (P1<P2<P3<P4) ama bu bir TESADÜFTÜR — öncelik kodları ileride değişirse
#: sessizce bozulurdu. Bu yüzden sıra AÇIKÇA config.PRIORITIES'ten türetilir.
PRIORITY_RANK = {code: index for index, code in enumerate(config.PRIORITIES)}
_UNKNOWN_PRIORITY_RANK = len(config.PRIORITIES)


def _priority_rank_sql(column: str = "t.priority") -> str:
    """CASE ifadesi: öncelik kodunu severity sırasına çevirir.

    Geçersiz/NULL öncelik EN SONA düşer — bozuk bir satır kuyruğun başını
    kapatamaz."""
    whens = " ".join(
        f"WHEN '{code}' THEN {rank}" for code, rank in PRIORITY_RANK.items())
    return f"CASE {column} {whens} ELSE {_UNKNOWN_PRIORITY_RANK} END"


#: SQL ve Python tarafı AYNI sırayı üretmek ZORUNDADIR. İkisini birlikte
#: değiştirin; ikinci bir sıralama tanımı yazmayın.
QUEUE_ORDER_BY_SQL = (
    f" ORDER BY {_priority_rank_sql()} ASC,"
    " COALESCE(t.queue_entered_at, t.created_at) ASC, t.id ASC"
)


def queue_order_key(ticket: dict):
    """(severity, kuyruğa giriş, ticket_id) — QUEUE_ORDER_BY_SQL'in Python ikizi.

    Birden fazla kuyruk taraması birleştirildiğinde (örn. bir teknisyen müsait
    olunca kendi uzmanlıkları + departmanı taranır) sonuç listesi bellekte bu
    anahtarla yeniden sıralanır. Böylece hangi yoldan gelinirse gelinsin
    işleme sırası AYNIDIR."""
    priority = (ticket or {}).get("priority")
    entered = (ticket.get("queue_entered_at") or ticket.get("created_at") or "")
    return (
        PRIORITY_RANK.get(priority, _UNKNOWN_PRIORITY_RANK),
        entered,
        ticket.get("id") or 0,
    )


def list_waiting_expert_tickets(skill_code: str = None, department: str = None,
                                basis: str = None):
    """Uzman bekleyen talepler. AYRI KUYRUK TABLOSU YOK — kuyruk
    status + routing_basis + (required_skill_code | pending_department)
    ile türetilir.

    Filtreler:
        skill_code  -> yalnızca o uzmanlığı bekleyen talepler
                       (SPECIFIC_SKILL ve GENERAL_SUPPORT dayanakları)
        department  -> DEPARTMENT dayanağıyla o departmanı bekleyen talepler
        basis       -> yalnızca o yönlendirme dayanağındaki talepler
                       (admin panelindeki kuyruk ayrımı için)

    ACCEPTANCE UX + ROUTING REVISION — KUYRUKLAR AYRIKTIR:
      * skill_code taraması DEPARTMENT ve MANUAL_TRIAGE taleplerini GÖRMEZ.
      * department taraması SPECIFIC_SKILL / GENERAL_SUPPORT taleplerini GÖRMEZ;
        bir talebin uzmanlığı biliniyorsa o talep departman kuyruğuna KAÇMAZ.
      * MANUAL_TRIAGE talepleri HİÇBİR otomatik taramaya girmez — worker onlara
        rastgele atama yapmaz, admin incelemesi bekler.
    Böylece aynı talep iki farklı taramada iki kez işlenmez ve "aday yok"
    durumu bir talebi asla başka bir kuyruğa kaydırmaz.

    GERİYE DÖNÜK UYUM: routing_basis NULL olan (revizyon öncesi) kayıtlar
    mevcut alanlarından türetilir — skill kodu varsa skill kuyruğunda, yoksa
    departman kuyruğunda değerlendirilir. Eski taleplerin davranışı değişmez.

    pending_department boşsa category'ye düşülür — eski kayıtlarda
    pending_department yazılmamış olabilir."""
    sql = _TICKET_SELECT + " WHERE t.status = ?"
    params = [config.STATUS_ESCALATION_PENDING]

    if skill_code:
        ph = ",".join("?" * len(_SKILL_QUEUE_BASES))
        sql += (f" AND t.required_skill_code = ?"
                f" AND (t.routing_basis IN ({ph}) OR t.routing_basis IS NULL)")
        params.append(skill_code)
        params.extend(_SKILL_QUEUE_BASES)

    if department:
        sql += (" AND (t.routing_basis = ?"
                "      OR (t.routing_basis IS NULL"
                "          AND (t.required_skill_code IS NULL"
                "               OR t.required_skill_code = '')))"
                " AND COALESCE(t.pending_department, t.category) = ?")
        params.append(config.ROUTING_BASIS_DEPARTMENT)
        params.append(department)

    if basis:
        sql += " AND t.routing_basis = ?"
        params.append(basis)

    # QUEUE PRIORITY + FIFO (§C): önce severity, sonra kuyruğa giriş, sonra id.
    # Tanım TEK yerdedir (QUEUE_ORDER_BY_SQL) — burada yeniden yazılmaz.
    sql += QUEUE_ORDER_BY_SQL
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_waiting_by_routing_basis():
    """Kuyruktaki taleplerin dayanağa göre dağılımı — TEK aggregate sorgu.

    Admin panelinde "neden atanmadı?" ayrımını göstermek içindir; talep başına
    ek sorgu ÇALIŞTIRILMAZ. routing_basis NULL olan eski kayıtlar
    config.ROUTING_BASIS_MANUAL_TRIAGE'a değil, ayrı bir None anahtarına
    düşer — geçmiş veriyi yeniden yorumlamıyoruz."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT routing_basis AS basis, COUNT(*) AS c FROM tickets "
            "WHERE status = ? GROUP BY routing_basis",
            (config.STATUS_ESCALATION_PENDING,),
        ).fetchall()
    return {r["basis"]: r["c"] for r in rows}


# ACCEPTANCE REVISION: claim_candidate_notification / clear_candidate_snapshot
# KALDIRILDI. Bunlar "aynı aday kümesi için kullanıcıya iki kez seçim sunma"
# koruması içindi; kullanıcıya artık seçim SUNULMUYOR. Kuyruktaki bir talep
# aday çıkar çıkmaz atanıp kuyruktan çıkar, dolayısıyla tekrar-bildirim
# problemi ortadan kalktı. Concurrency koruması artık tek yerdedir:
# assign_primary_if_unassigned()'ın koşullu UPDATE'i.
#
# tickets.candidate_snapshot / candidate_notified_at KOLONLARI DURUYOR —
# eski kayıtların denetim izi silinmesin diye. candidate_checked_at hâlâ
# yazılıyor (touch_candidate_check) ve panelde "son kontrol" olarak görünür.


def touch_candidate_check(ticket_id: int):
    """Kuyruktaki talep DEĞERLENDİRİLDİ ama atanmadı: 'son kontrol' damgası.

    Operasyon görünürlüğü içindir — admin panelinde 'Son kontrol' sütununda
    gösterilir, böylece bir talebin unutulup unutulmadığı anlaşılır.
    Talebin durumunu veya atamasını DEĞİŞTİRMEZ."""
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET candidate_checked_at = ? WHERE id = ?",
                     (now(), ticket_id))


# ---------- Giriş hız sınırı ----------

# Rate-limit kayıtlarının saklama süresi. Pencereden birkaç kat uzun tutuyoruz
# ki sınır davranışı hiç etkilenmesin; tablo yine de sınırsız büyümesin.
LOGIN_ATTEMPT_RETENTION_MINUTES_FACTOR = 6
# Global süpürmenin çalışma olasılığı (1/N). Her istekte ağır DELETE çalıştırmak
# yerine amortize edilmiş maliyet.
_LOGIN_SWEEP_ODDS = 50


def _login_retention_cutoff():
    minutes = config.LOGIN_LOCKOUT_MINUTES * LOGIN_ATTEMPT_RETENTION_MINUTES_FACTOR
    return _fmt_ts(datetime.now() - timedelta(minutes=minutes))


def purge_old_login_attempts():
    """Saklama süresini aşan bütün kayıtları siler. Döndürdüğü sayı silinen satır."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?",
                           (_login_retention_cutoff(),))
        return cur.rowcount


def record_login_failure(identifier: str):
    """Başarısız denemeyi kaydeder. PAROLA ASLA SAKLANMAZ — yalnızca kimlik
    ve zaman damgası.

    Aynı işlemde iki kademeli temizlik yapılır:
      1. Bu kimliğe ait eski satırlar silinir (indexli, çok ucuz) — tek bir
         kaynağın satırları sınırsız birikemez.
      2. Nadiren (1/N olasılıkla) global süpürme yapılır — IP değiştirerek
         çok sayıda kimlik üreten bir saldırgan da tabloyu şişiremez.
    Böylece her istekte ağır DELETE çalıştırmadan tablo sınırlı kalır."""
    cutoff = _login_retention_cutoff()
    with get_conn() as conn:
        conn.execute("INSERT INTO login_attempts (identifier, attempted_at) VALUES (?, ?)",
                     (identifier, now()))
        conn.execute("DELETE FROM login_attempts WHERE identifier = ? AND attempted_at < ?",
                     (identifier, cutoff))
    if secrets.randbelow(_LOGIN_SWEEP_ODDS) == 0:
        purge_old_login_attempts()


def clear_login_failures(identifier: str):
    """Başarılı giriş: bu kimliğin sayacı sıfırlanır.

    Başarılı girişler nadir olduğu için global süpürmeyi de burada
    çalıştırmak güvenli ve ucuzdur."""
    with get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE identifier = ?", (identifier,))
    purge_old_login_attempts()


def count_recent_login_failures(identifier: str) -> int:
    cutoff = _fmt_ts(datetime.now() - timedelta(minutes=config.LOGIN_LOCKOUT_MINUTES))
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM login_attempts "
            "WHERE identifier = ? AND attempted_at >= ?",
            (identifier, cutoff),
        ).fetchone()
        return row["c"]


def is_login_blocked(identifier: str) -> bool:
    return count_recent_login_failures(identifier) >= config.LOGIN_MAX_ATTEMPTS


# ==================================================================
# Faz 6A: SLA cycle motoru
# ==================================================================
#
# SOURCE OF TRUTH : sla_cycles
# UYUMLULUK KOPYASI: tickets.sla_started_at / first_response_due_at /
#                    resolution_due_at / first_staff_response_at
#
# Legacy kolonlar DROP EDİLMEDİ; AKTİF cycle'ın değerleri aynı transaction
# içinde oraya da yazılır (_sync_ticket_sla_cache). Böylece Faz 5B'den gelen
# sorgular (operations_counts, admin filtreleri) hiç değişmeden çalışır ve
# otomatik olarak YALNIZCA aktif cycle'ı görür.


def _cycle_deadlines(started_at: str, priority: str, paused_seconds: int = 0):
    """Başlangıç + politika + biriken duraklama -> (ilk_yanit_due, cozum_due)."""
    base = _parse_ts(started_at)
    if not base:
        return None, None
    policy = config.sla_policy_for(priority)
    shift = timedelta(seconds=paused_seconds or 0)
    return (
        _fmt_ts(base + timedelta(minutes=policy["first_response_minutes"]) + shift),
        _fmt_ts(base + timedelta(minutes=policy["resolution_minutes"]) + shift),
    )


def _sync_ticket_sla_cache(conn, ticket_id: int, cycle: dict):
    """Aktif cycle değerlerini tickets tablosundaki uyumluluk kolonlarına yazar."""
    conn.execute(
        "UPDATE tickets SET sla_started_at = ?, first_response_due_at = ?, "
        "resolution_due_at = ?, first_staff_response_at = ?, updated_at = ? WHERE id = ?",
        (cycle.get("started_at"), cycle.get("first_response_due_at"),
         cycle.get("resolution_due_at"), cycle.get("first_staff_response_at"),
         now(), ticket_id),
    )


def get_active_cycle(ticket_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL",
            (ticket_id,),
        ).fetchone()
        return dict(row) if row else None


def list_sla_cycles(ticket_id: int):
    """Cycle geçmişi — en yeni önce."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? ORDER BY cycle_number DESC",
            (ticket_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def start_sla_cycle(ticket_id: int, priority: str = None):
    """Yeni SLA cycle açar. AYNI ANDA TEK AKTİF CYCLE garantisi partial UNIQUE
    index ile DB seviyesindedir; ikinci eş zamanlı çağrı IntegrityError alır ve
    sessizce None döner (idempotent)."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return None
    priority = priority or ticket.get("priority") or config.DEFAULT_PRIORITY
    stamp = now()
    first_due, res_due = _cycle_deadlines(stamp, priority)

    try:
        with get_conn() as conn:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(cycle_number), 0) + 1 AS n FROM sla_cycles WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO sla_cycles (ticket_id, cycle_number, priority, started_at, "
                "first_response_due_at, resolution_due_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, nxt, priority, stamp, first_due, res_due, stamp, stamp),
            )
            _sync_ticket_sla_cache(conn, ticket_id, {
                "started_at": stamp, "first_response_due_at": first_due,
                "resolution_due_at": res_due, "first_staff_response_at": None,
            })
    except sqlite3.IntegrityError:
        # Aktif cycle zaten var ya da eş zamanlı ikinci çağrı — yeni cycle açılmaz.
        return None
    return get_active_cycle(ticket_id)


def pause_sla(ticket_id: int) -> bool:
    """Kullanıcı beklenirken SLA saatini durdurur.

    IDEMPOTENT: zaten duraklamışsa koşullu UPDATE 0 satır etkiler ve False
    döner; ikinci kez paused_at yazılmaz."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sla_cycles SET paused_at = ?, updated_at = ? "
            "WHERE ticket_id = ? AND closed_at IS NULL AND paused_at IS NULL",
            (now(), now(), ticket_id),
        )
        return cur.rowcount == 1


def resume_sla(ticket_id: int) -> bool:
    """Duraklamayı bitirir, geçen süreyi biriktirir ve son tarihleri O KADAR öteler.

    IDEMPOTENT: duraklamış değilse hiçbir şey yapmaz ve False döner; aynı resume
    iki kez işlenirse süre iki kez EKLENMEZ."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL "
            "AND paused_at IS NOT NULL", (ticket_id,),
        ).fetchone()
        if not row:
            return False
        cycle = dict(row)
        paused = _parse_ts(cycle["paused_at"])
        elapsed = max(0, int((datetime.now() - paused).total_seconds())) if paused else 0
        total = (cycle["total_paused_seconds"] or 0) + elapsed

        # Duraklama süresi kadar ötele. Karşılanmış ilk yanıt SLA'sı YENİDEN AÇILMAZ.
        first_due = cycle["first_response_due_at"]
        if not cycle["first_staff_response_at"] and first_due:
            first_due = _fmt_ts(_parse_ts(first_due) + timedelta(seconds=elapsed))
        res_due = cycle["resolution_due_at"]
        if res_due:
            res_due = _fmt_ts(_parse_ts(res_due) + timedelta(seconds=elapsed))

        # KRİTİK: rowcount kontrol edilmezse eş zamanlı iki resume duraklama
        # süresini İKİ KEZ eklerdi. Koşullu UPDATE yalnızca ilk çağrıyı geçirir.
        cur = conn.execute(
            "UPDATE sla_cycles SET paused_at = NULL, total_paused_seconds = ?, "
            "first_response_due_at = ?, resolution_due_at = ?, updated_at = ? "
            "WHERE id = ? AND paused_at IS NOT NULL",
            (total, first_due, res_due, now(), cycle["id"]),
        )
        if cur.rowcount != 1:
            return False          # başka bir çağrı bu duraklamayı zaten kapattı
        cycle.update({"first_response_due_at": first_due, "resolution_due_at": res_due})
        _sync_ticket_sla_cache(conn, ticket_id, cycle)
    return True


def close_sla_cycle(ticket_id: int, resolved_at: str = None) -> bool:
    """Aktif cycle'ı kapatır. Duraklama açıksa ÖNCE süreyi finalize eder,
    böylece cycle asla 'duraklamış ama kapalı' durumda kalmaz."""
    stamp = now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL",
            (ticket_id,),
        ).fetchone()
        if not row:
            return False
        cycle = dict(row)
        total = cycle["total_paused_seconds"] or 0
        if cycle["paused_at"]:
            paused = _parse_ts(cycle["paused_at"])
            if paused:
                total += max(0, int((datetime.now() - paused).total_seconds()))
        conn.execute(
            "UPDATE sla_cycles SET paused_at = NULL, total_paused_seconds = ?, "
            "resolved_at = ?, closed_at = ?, updated_at = ? "
            "WHERE id = ? AND closed_at IS NULL",
            (total, resolved_at or stamp, stamp, stamp, cycle["id"]),
        )
        return True


def mark_cycle_first_response(ticket_id: int) -> bool:
    """İlk PUBLIC personel yanıtını AKTİF cycle'a damgalar (yalnızca NULL ise).

    Reopen sonrası yeni cycle'da damga yeniden boştur; Cycle 1'de karşılanmış
    olması Cycle 2'yi otomatik karşılanmış SAYMAZ."""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sla_cycles SET first_staff_response_at = ?, updated_at = ? "
            "WHERE ticket_id = ? AND closed_at IS NULL AND first_staff_response_at IS NULL",
            (stamp, stamp, ticket_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE tickets SET first_staff_response_at = COALESCE(first_staff_response_at, ?) "
            "WHERE id = ?", (stamp, ticket_id))
        return True


def apply_priority_to_active_cycle(ticket_id: int, priority: str):
    """Öncelik değişince AKTİF cycle'ın son tarihlerini yeniden hesaplar.

    started_at DEĞİŞMEZ, biriken duraklama korunur. KAPALI cycle'lara
    DOKUNULMAZ — geçmiş immutable'dır."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL",
            (ticket_id,),
        ).fetchone()
        if not row:
            return False
        cycle = dict(row)
        first_due, res_due = _cycle_deadlines(
            cycle["started_at"], priority, cycle["total_paused_seconds"] or 0)
        conn.execute(
            "UPDATE sla_cycles SET priority = ?, first_response_due_at = ?, "
            "resolution_due_at = ?, updated_at = ? WHERE id = ?",
            (priority, first_due, res_due, now(), cycle["id"]),
        )
        cycle.update({"first_response_due_at": first_due, "resolution_due_at": res_due})
        _sync_ticket_sla_cache(conn, ticket_id, cycle)
        return True


def get_cycle_sla_state(cycle: dict, reference: datetime = None):
    """Bir cycle'ın SLA durumu.

    DURAKLAMA: cycle duraklamışsa referans zaman paused_at'tir — yani kalan
    süre donar ve kullanıcı beklenirken ilerlemez."""
    if not cycle:
        return None
    reference = reference or datetime.now()
    if cycle.get("paused_at"):
        paused = _parse_ts(cycle["paused_at"])
        if paused:
            reference = paused

    priority = cycle.get("priority") or config.DEFAULT_PRIORITY
    policy = config.sla_policy_for(priority)
    # Faz 6B: BUSINESS cycle'lar iş zamanıyla, LEGACY cycle'lar eski
    # wall-clock mantığıyla değerlendirilir (geçmiş raporlar değişmesin).
    import sla_calendar
    if cycle.get("calendar_mode") == config.CALENDAR_MODE_BUSINESS:
        ref = sla_calendar.to_aware(reference)
        state_fn = _business_deadline_state
    else:
        # LEGACY yol naive datetime ile çalışır. Çağıran aware bir referans
        # verirse (business kod yollarından gelebilir) yerel saate çevirip
        # tzinfo'yu düşürüyoruz — aware/naive karışımı TypeError üretirdi.
        ref = reference
        if ref is not None and ref.tzinfo is not None:
            ref = ref.astimezone(sla_calendar.get_timezone()).replace(tzinfo=None)
        state_fn = _deadline_state
    fr_state, fr_left, fr_level = state_fn(
        cycle.get("first_response_due_at"), cycle.get("first_staff_response_at"),
        ref, policy["first_response_minutes"])
    res_state, res_left, res_level = state_fn(
        cycle.get("resolution_due_at"), cycle.get("resolved_at"),
        ref, policy["resolution_minutes"])

    return {
        "cycle_number": cycle.get("cycle_number"),
        "priority": priority,
        "priority_label": config.PRIORITY_LABELS.get(priority, priority),
        "started": True,
        "started_at": cycle.get("started_at"),
        "first_response_due_at": cycle.get("first_response_due_at"),
        "resolution_due_at": cycle.get("resolution_due_at"),
        "first_response_status": fr_state,
        "first_response_level": fr_level,
        "minutes_to_first_response": fr_left,
        "resolution_status": res_state,
        "resolution_level": res_level,
        "minutes_to_resolution": res_left,
        "is_paused": bool(cycle.get("paused_at")),
        "paused_at": cycle.get("paused_at"),
        "total_paused_seconds": cycle.get("total_paused_seconds") or 0,
        "total_sla_paused_seconds": cycle.get("total_sla_paused_seconds") or 0,
        "calendar_mode": cycle.get("calendar_mode") or config.CALENDAR_MODE_LEGACY,
        "effective_started_at": cycle.get("effective_started_at"),
        "closed_at": cycle.get("closed_at"),
        "is_active": cycle.get("closed_at") is None,
    }


# ==================================================================
# Faz 6B: Tatil takvimi + iş-zamanı SLA
# ==================================================================

def get_calendar_revision() -> int:
    """Merkezi takvim revizyonu. Süreçler bunu karşılaştırarak cache'lerinin
    bayatlayıp bayatlamadığını anlar."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT revision FROM sla_calendar_state WHERE id = 1").fetchone()
        return row["revision"] if row else 0


def _bump_calendar_revision(conn):
    """ATOMİK artırma — read-modify-write YAPILMAZ.

    İki admin aynı anda tatil değiştirse bile 'revision = revision + 1'
    tek ifadede çalıştığı için hiçbir artış kaybolmaz."""
    conn.execute(
        "UPDATE sla_calendar_state SET revision = revision + 1, updated_at = ? "
        "WHERE id = 1", (now(),))


def load_calendar_snapshot():
    """(revizyon, aktif_tatil_kumesi) — TEK bağlantıda okunur.

    Revizyon ile tatil listesi aynı okumadan geldiği için araya giren bir
    değişiklik yüzünden 'yeni revizyon + eski liste' karışımı oluşamaz."""
    from datetime import date as _date
    out = set()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT revision FROM sla_calendar_state WHERE id = 1").fetchone()
        revision = row["revision"] if row else 0
        rows = conn.execute(
            "SELECT holiday_date FROM sla_holidays WHERE active = 1").fetchall()
    for r in rows:
        try:
            y, m, d = str(r["holiday_date"]).split("-")
            out.add(_date(int(y), int(m), int(d)))
        except Exception:
            continue
    return revision, out


def list_active_holiday_dates():
    """Aktif tatil günleri (date kümesi). sla_calendar bunu cache'ler."""
    from datetime import date as _date
    out = set()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT holiday_date FROM sla_holidays WHERE active = 1").fetchall()
    for r in rows:
        try:
            y, m, d = str(r["holiday_date"]).split("-")
            out.add(_date(int(y), int(m), int(d)))
        except Exception:
            continue
    return out


def list_holidays():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sla_holidays ORDER BY holiday_date DESC").fetchall()
        return [dict(r) for r in rows]


def create_holiday(holiday_date: str, name: str):
    """(id, hata) döner. Aynı tarih ikinci kez eklenemez (UNIQUE)."""
    import sla_calendar
    holiday_date = (holiday_date or "").strip()
    name = (name or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", holiday_date):
        return None, "Tarih YYYY-AA-GG biçiminde olmalı."
    try:
        datetime.strptime(holiday_date, "%Y-%m-%d")
    except ValueError:
        return None, "Geçersiz tarih."
    if not name:
        return None, "Tatil adı boş olamaz."
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO sla_holidays (holiday_date, name, active, created_at) "
                "VALUES (?, ?, 1, ?)", (holiday_date, name, now()))
            new_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return None, f"{holiday_date} zaten tatil olarak tanımlı."
        # AYNI transaction: tatil ve revizyon birlikte kalıcı olur.
        _bump_calendar_revision(conn)
    # Değişikliği yapan süreç ANINDA güncellensin; diğerleri revizyondan görür.
    sla_calendar.invalidate_cache()
    return new_id, None


def set_holiday_active(holiday_id: int, active: bool):
    """Tatil SİLİNMEZ, pasifleştirilir. Cache invalidate edilir."""
    import sla_calendar
    with get_conn() as conn:
        conn.execute("UPDATE sla_holidays SET active = ? WHERE id = ?",
                     (1 if active else 0, holiday_id))
        _bump_calendar_revision(conn)
    sla_calendar.invalidate_cache()


def get_holiday(holiday_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sla_holidays WHERE id = ?",
                           (holiday_id,)).fetchone()
        return dict(row) if row else None


# ---------- İş-zamanı deadline hesabı ----------

def _business_deadlines(effective_start: str, priority: str, sla_paused_seconds: int = 0):
    """İŞ TAKVİMİ üzerinden (ilk_yanit_due, cozum_due).

    SLA_POLICY dakikaları İŞ DAKİKASI olarak yorumlanır. Biriken SLA
    duraklaması da iş dakikası olarak eklenir."""
    import sla_calendar
    base = sla_calendar.to_aware(effective_start)
    if not base:
        return None, None
    policy = config.sla_policy_for(priority)
    pause_minutes = (sla_paused_seconds or 0) / 60.0
    return (
        sla_calendar.to_naive_str(sla_calendar.add_business_minutes(
            base, policy["first_response_minutes"] + pause_minutes)),
        sla_calendar.to_naive_str(sla_calendar.add_business_minutes(
            base, policy["resolution_minutes"] + pause_minutes)),
    )


def start_sla_cycle_business(ticket_id: int, priority: str = None):
    """Faz 6B: İŞ TAKVİMİNE göre yeni SLA cycle açar.

    started_at           = gerçek eskalasyon anı (AUDIT — değişmez)
    effective_started_at = sayacın gerçekten işlemeye başladığı ilk mesai anı

    Mesai dışında eskalasyon: Cuma 20:00'de açılan P1 talebin ilk yanıt
    son tarihi Pazartesi 08:15 olur."""
    import sla_calendar
    ticket = get_ticket(ticket_id)
    if not ticket:
        return None
    priority = priority or ticket.get("priority") or config.DEFAULT_PRIORITY
    stamp = now()
    effective = sla_calendar.to_naive_str(
        sla_calendar.next_business_time(sla_calendar.to_aware(stamp)))
    first_due, res_due = _business_deadlines(effective, priority)

    try:
        with get_conn() as conn:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(cycle_number), 0) + 1 AS n FROM sla_cycles "
                "WHERE ticket_id = ?", (ticket_id,)).fetchone()["n"]
            conn.execute(
                "INSERT INTO sla_cycles (ticket_id, cycle_number, priority, started_at, "
                "effective_started_at, first_response_due_at, resolution_due_at, "
                "calendar_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, nxt, priority, stamp, effective, first_due, res_due,
                 config.CALENDAR_MODE_BUSINESS, stamp, stamp),
            )
            _sync_ticket_sla_cache(conn, ticket_id, {
                "started_at": stamp, "first_response_due_at": first_due,
                "resolution_due_at": res_due, "first_staff_response_at": None,
            })
    except sqlite3.IntegrityError:
        return None
    return get_active_cycle(ticket_id)


def pause_sla_business(ticket_id: int, reason: str = None) -> bool:
    """Duraklamayı başlatır ve AÇIK bir pause olayı kaydeder.

    IDEMPOTENT: zaten duraklamışsa hem koşullu UPDATE hem partial UNIQUE index
    ikinci kaydı engeller."""
    stamp = now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sla_cycles SET paused_at = ?, updated_at = ? "
            "WHERE ticket_id = ? AND closed_at IS NULL AND paused_at IS NULL",
            (stamp, stamp, ticket_id),
        )
        if cur.rowcount != 1:
            return False
        cycle = conn.execute(
            "SELECT id FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL",
            (ticket_id,)).fetchone()
        if cycle:
            try:
                conn.execute(
                    "INSERT INTO sla_pause_events (cycle_id, paused_at, reason) "
                    "VALUES (?, ?, ?)",
                    (cycle["id"], stamp, reason or config.STATUS_WAITING_USER))
            except sqlite3.IntegrityError:
                pass
        return True


def _close_pause_event(conn, cycle_id: int, paused_at: str, resumed_at: str,
                       wall_seconds: int, business_seconds: int):
    conn.execute(
        "UPDATE sla_pause_events SET resumed_at = ?, wall_seconds = ?, "
        "business_seconds = ? WHERE cycle_id = ? AND resumed_at IS NULL",
        (resumed_at, wall_seconds, business_seconds, cycle_id),
    )


def resume_sla_business(ticket_id: int) -> bool:
    """Duraklamayı bitirir.

    KRİTİK FARK (Faz 6B): son tarihe eklenen süre wall-clock DEĞİL, duraklama
    aralığının ÇALIŞMA SAATLERİYLE KESİŞİMİDİR.

    Örn. Cuma 17:30 duraklat -> Pazartesi 08:30 devam et:
        wall-clock  = 63 saat
        SLA etkisi  = 30 dk (Cuma) + 30 dk (Pzt) = 60 iş dakikası
    Hafta sonu zaten SLA işletmediği için ikinci kez düşülmez.

    IDEMPOTENT: koşullu UPDATE rowcount kontrolüyle çift sayım engellenir."""
    import sla_calendar
    stamp = now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL "
            "AND paused_at IS NOT NULL", (ticket_id,)).fetchone()
        if not row:
            return False
        cycle = dict(row)
        paused_aware = sla_calendar.to_aware(cycle["paused_at"])
        now_aware = sla_calendar.to_aware(stamp)
        wall = max(0, int((now_aware - paused_aware).total_seconds())) if paused_aware else 0
        business = max(0, sla_calendar.business_seconds_between(paused_aware, now_aware))

        total_wall = (cycle["total_paused_seconds"] or 0) + wall
        total_sla = (cycle["total_sla_paused_seconds"] or 0) + business

        # Son tarihler YALNIZCA iş-zamanı kesişimi kadar ötelenir.
        first_due = cycle["first_response_due_at"]
        if not cycle["first_staff_response_at"] and first_due:
            first_due = sla_calendar.to_naive_str(sla_calendar.add_business_minutes(
                sla_calendar.to_aware(first_due), business / 60.0))
        res_due = cycle["resolution_due_at"]
        if res_due:
            res_due = sla_calendar.to_naive_str(sla_calendar.add_business_minutes(
                sla_calendar.to_aware(res_due), business / 60.0))

        cur = conn.execute(
            "UPDATE sla_cycles SET paused_at = NULL, total_paused_seconds = ?, "
            "total_sla_paused_seconds = ?, first_response_due_at = ?, "
            "resolution_due_at = ?, updated_at = ? "
            "WHERE id = ? AND paused_at IS NOT NULL",
            (total_wall, total_sla, first_due, res_due, stamp, cycle["id"]),
        )
        if cur.rowcount != 1:
            return False
        _close_pause_event(conn, cycle["id"], cycle["paused_at"], stamp, wall, business)
        cycle.update({"first_response_due_at": first_due, "resolution_due_at": res_due})
        _sync_ticket_sla_cache(conn, ticket_id, cycle)
    return True


def apply_priority_business(ticket_id: int, priority: str):
    """Öncelik değişince AKTİF cycle'ı yeni politika + İŞ TAKVİMİ ile hesaplar.

    effective_started_at DEĞİŞMEZ; biriken SLA duraklaması korunur.
    KAPALI cycle'lara dokunulmaz (geçmiş immutable)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sla_cycles WHERE ticket_id = ? AND closed_at IS NULL",
            (ticket_id,)).fetchone()
        if not row:
            return False
        cycle = dict(row)
        if cycle.get("calendar_mode") == config.CALENDAR_MODE_BUSINESS:
            base = cycle.get("effective_started_at") or cycle["started_at"]
            first_due, res_due = _business_deadlines(
                base, priority, cycle.get("total_sla_paused_seconds") or 0)
        else:
            # Legacy cycle: eski wall-clock mantığı korunur.
            first_due, res_due = _cycle_deadlines(
                cycle["started_at"], priority, cycle.get("total_paused_seconds") or 0)
        conn.execute(
            "UPDATE sla_cycles SET priority = ?, first_response_due_at = ?, "
            "resolution_due_at = ?, updated_at = ? WHERE id = ?",
            (priority, first_due, res_due, now(), cycle["id"]))
        cycle.update({"first_response_due_at": first_due, "resolution_due_at": res_due})
        _sync_ticket_sla_cache(conn, ticket_id, cycle)
        return True


def _business_deadline_state(due_at, done_at, reference, window_minutes):
    """İŞ ZAMANI tabanlı SLA durumu.

    Mesai dışında kalan süre AZALMAZ: iki an arasındaki iş dakikası sayılır."""
    import sla_calendar
    due = sla_calendar.to_aware(due_at)
    if not due:
        return config.SLA_STATE_PENDING, None, config.SLA_LEVEL_NORMAL

    done = sla_calendar.to_aware(done_at)
    if done:
        state = config.SLA_STATE_MET if done <= due else config.SLA_STATE_OVERDUE
        level = (config.SLA_LEVEL_NORMAL if state == config.SLA_STATE_MET
                 else config.SLA_LEVEL_OVERDUE)
        return state, sla_calendar.business_minutes_between(done, due), level

    remaining = sla_calendar.business_minutes_between(reference, due)
    if reference > due:
        return config.SLA_STATE_OVERDUE, -abs(remaining), config.SLA_LEVEL_OVERDUE
    if window_minutes and remaining <= window_minutes * config.SLA_WARNING_RATIO:
        return config.SLA_STATE_PENDING, remaining, config.SLA_LEVEL_WARNING
    return config.SLA_STATE_PENDING, remaining, config.SLA_LEVEL_NORMAL
