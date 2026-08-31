"""Regression test altyapısı — ortak kurulum (tests paketi).

TEK KURAL: testler GERÇEK Gemini veya GERÇEK Telegram çağrısı YAPMAZ.
  - Telegram: webapp.send_telegram_message ve queue_worker sender'ı enjekte edilir.
  - Gemini  : bu paketlerdeki hiçbir yol ai_assistant'ı çağırmaz; çağıran bir
              yol eklenirse fail_on_network() ile yakalanır.

Her test modülü kendi geçici SQLite dosyasını kullanır; projedeki destek.db
DOSYASINA DOKUNULMAZ. DB_PATH ortam değişkeni config import EDİLMEDEN ÖNCE
ayarlanmalıdır — bu yüzden setup_test_db() en üstte çağrılır.
"""

import os
import sys
import tempfile
import unittest


def setup_test_db(label: str) -> str:
    """config/db import edilmeden ÖNCE çağrılmalıdır.

    Gerçek destek.db'yi korumak için izole bir geçici dosya yolu ayarlar ve
    testlerin sır gerektirmemesi için sabit test değerleri verir."""
    if "config" in sys.modules:
        raise RuntimeError(
            "setup_test_db(), config import edilmeden ÖNCE çağrılmalıdır.")
    path = os.path.join(tempfile.gettempdir(), f"destek_test_{label}_{os.getpid()}.db")
    if os.path.exists(path):
        os.remove(path)
    os.environ["DB_PATH"] = path
    os.environ.setdefault("WEB_SECRET_KEY", "test-secret-key-not-a-real-secret")
    os.environ.setdefault("BOT_TOKEN", "")
    os.environ.setdefault("GEMINI_API_KEY", "")
    return path


class RecordingSender:
    """Telegram gönderimi yerine geçen kayıt tutucu.

    (ok, error) döner — gerçek send_telegram_message ile AYNI sözleşme.
    fail=True ile gönderim hatası simüle edilir."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def __call__(self, chat_id, text, reply_markup=None):
        self.calls.append({"chat_id": chat_id, "text": text,
                           "reply_markup": reply_markup})
        if self.fail:
            return False, "SimulatedNetworkError"
        return True, None

    @property
    def texts(self):
        return [c["text"] for c in self.calls]

    def markups(self):
        return [c["reply_markup"] for c in self.calls]

    def reset(self):
        self.calls.clear()


def fail_on_network(*args, **kwargs):
    """Testte çağrılmaması gereken ağ fonksiyonlarının yerine konur."""
    raise AssertionError(
        "Otomatik testte gerçek ağ çağrısı denendi (Gemini/Telegram). "
        "Bu çağrı mock'lanmalıdır.")


class DbTestCase(unittest.TestCase):
    """Her testten önce şemayı sıfırdan kuran temel sınıf.

    Testler birbirinin verisini görmemelidir; setUp her seferinde bütün
    tabloları temizler ve init_db()'yi yeniden çalıştırır."""

    #: alt sınıflar db modülünü buraya bağlar
    db = None
    config = None

    def setUp(self):
        self.db.init_db()
        self._wipe()
        self.db.init_db()

    def _wipe(self):
        with self.db.get_conn() as conn:
            tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchall()]
            for t in tables:
                conn.execute(f"DELETE FROM {t}")

    # ---------- Fixture yardımcıları ----------

    def make_skill(self, code, name=None):
        skill_id, error = self.db.create_skill(code, name or code.title())
        self.assertIsNone(error, f"skill olusturulamadi: {error}")
        return skill_id

    def make_technician(self, name, department="Yazılım", available=True,
                        skills=(), role=None, telegram_id=None):
        """skills: ((skill_id, priority), ...)"""
        tech_id = self.db.create_technician(name, telegram_id, department, role)
        if not available:
            self.db.set_technician_availability(tech_id, False)
        for skill_id, priority in skills:
            ok, message = self.db.set_technician_skill(tech_id, skill_id, priority)
            self.assertTrue(ok, message)
        return tech_id

    def make_ticket(self, user_id=1000, subject="Test talebi", skill_code=None,
                    status=None, category=None):
        ticket_id = self.db.create_ticket(user_id, "testuser", "Test Kullanıcı", subject)
        if skill_code or category:
            self.db.set_ai_classification(ticket_id, category=category,
                                          skill_code=skill_code)
        if status:
            with self.db.get_conn() as conn:
                conn.execute("UPDATE tickets SET status = ? WHERE id = ?",
                             (status, ticket_id))
        return ticket_id

    def queue_ticket(self, skill_code, user_id=1000, subject="Kuyruk talebi",
                     category=None):
        """Uzman bekleyen kuyruğa girmiş bir talep üretir."""
        ticket_id = self.make_ticket(user_id=user_id, subject=subject,
                                     skill_code=skill_code, category=category)
        self.db.mark_escalation_pending(ticket_id, department=category)
        return ticket_id

    def set_active_workload(self, technician_id, count):
        """Teknisyene 'count' adet AKTİF PRIMARY talep verir (iş yükü kurgusu)."""
        for i in range(count):
            ticket_id = self.make_ticket(subject=f"Yuk #{i}")
            ok = self.db.assign_primary_if_unassigned(
                ticket_id, technician_id, f"tech{technician_id}")
            self.assertTrue(ok)
        return count

    def set_last_primary_at(self, technician_id, stamp):
        """Son PRIMARY atama zamanını doğrudan atama geçmişine yazar.

        Gerçek kaynak ticket_assignments'tır; test de aynı tabloyu kullanır ki
        üretimdeki hesaplama yolu (MAX(assigned_at)) test edilmiş olsun."""
        with self.db.get_conn() as conn:
            conn.execute(
                "UPDATE ticket_assignments SET assigned_at = ? "
                "WHERE technician_id = ? AND assignment_role = 'PRIMARY'",
                (stamp, technician_id))

    def give_primary_history(self, technician_id, stamp):
        """Kişiye GEÇMİŞTE kapanmış bir PRIMARY ataması verir.

        Aktif iş yükünü ARTIRMAZ (talep çözülmüş sayılır) ama
        last_primary_assignment_at değerini doldurur."""
        ticket_id = self.make_ticket(subject="Gecmis is")
        self.db.assign_primary_if_unassigned(ticket_id, technician_id,
                                             f"tech{technician_id}")
        with self.db.get_conn() as conn:
            conn.execute("UPDATE tickets SET status = ? WHERE id = ?",
                         (self.config.STATUS_CLOSED, ticket_id))
            conn.execute(
                "UPDATE ticket_assignments SET assigned_at = ? WHERE ticket_id = ?",
                (stamp, ticket_id))
        return ticket_id
