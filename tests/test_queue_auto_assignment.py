"""KUYRUK PAKETİ — otomatik atama ve concurrency (ACCEPTANCE REVISION §33).

Worker artık kullanıcıya teknisyen SEÇTİRMEZ; merkezi motorun seçtiği kişiye
atomik PRIMARY ataması yapar ve yalnızca sonucu bildirir.

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ — sender enjekte edilir.
"""

from tests import _support, project_file

_support.setup_test_db("queue")

import unittest

import config
import db
import queue_service
import queue_worker
import routing
from tests._support import DbTestCase, RecordingSender


class QueueTestCase(DbTestCase):
    db = db
    config = config

    def setUp(self):
        super().setUp()
        self.sender = RecordingSender()
        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")


# ==================================================================
# 25-28, 32: temel kuyruk davranışı
# ==================================================================

class TestQueueAutoAssignment(QueueTestCase):

    def test_25_ticket_stays_in_queue_when_no_candidate(self):
        """Spec §18: aday yoksa talep KAYBOLMAZ, kuyrukta kalır."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")

        stats = queue_worker.run_once(sender=self.sender)

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["status"], config.STATUS_ESCALATION_PENDING)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertIsNotNone(ticket["queue_entered_at"])
        self.assertEqual(stats["assigned"], 0)
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(self.sender.calls, [], "Aday yokken bildirim gitmemeli.")

    def test_25b_candidate_check_timestamp_is_updated(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        queue_worker.run_once(sender=self.sender)
        self.assertIsNotNone(db.get_ticket(ticket_id)["candidate_checked_at"])

    def test_26_worker_assigns_primary_when_candidate_appears(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        stats = queue_worker.run_once(sender=self.sender)

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["assigned_technician_id"], hakan)
        self.assertEqual(ticket["status"], config.STATUS_ASSIGNED)
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(stats["candidates"], 1)

    def test_27_worker_sends_no_choice_keyboard(self):
        """Spec §19: worker candidate callback butonu GÖNDERMEZ."""
        self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        queue_worker.run_once(sender=self.sender)

        self.assertTrue(self.sender.calls, "Kullanıcıya sonuç bildirilmeli.")
        for call in self.sender.calls:
            self.assertIsNone(call["reply_markup"],
                              "Kullanıcıya seçim butonu GÖNDERİLMEMELİ.")
            self.assertNotIn("Kiminle devam etmek", call["text"])
            self.assertNotIn("ister misiniz", call["text"])

    def test_27b_user_message_reports_assignment_result_only(self):
        """Spec §15: kullanıcıya iç operasyon detayı verilmez."""
        self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        queue_worker.run_once(sender=self.sender)
        user_text = self.sender.calls[0]["text"]

        self.assertIn("Atanan uzman: Hakan Bulut", user_text)
        self.assertIn("Gerekli uzmanlık: Görüntü İşleme", user_text)
        self.assertIn("aynı talep üzerinden", user_text)
        for leak in ("aktif talep", "öncelik", "iş yükü", "priority"):
            self.assertNotIn(leak, user_text,
                             f"Kullanıcıya iç operasyon detayı sızmamalı: {leak}")

    def test_28_worker_uses_same_central_selector(self):
        """Spec §20: worker aynı sıralamayı kullanır — kendi algoritmasını yazmaz."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((self.cv, 1),))
        self.set_active_workload(hakan, 2)      # Hakan daha yüklü -> Kaan seçilmeli

        expected = routing.select_best_technician_for_skill(
            "COMPUTER_VISION")["selected"]["technician_id"]
        queue_worker.run_once(sender=self.sender)

        self.assertEqual(expected, kaan)
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], kaan)

    def test_32_same_ticket_id_is_preserved(self):
        """Spec §19: atama sonrası AYNI talep devam eder, yeni talep açılmaz."""
        ticket_id = self.queue_ticket("COMPUTER_VISION", subject="Kamera bulanık")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        before = len(db.list_tickets())

        queue_worker.run_once(sender=self.sender)

        self.assertEqual(len(db.list_tickets()), before, "Yeni talep AÇILMAMALI.")
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["id"], ticket_id)
        self.assertEqual(ticket["subject"], "Kamera bulanık")

    def test_32b_ai_mode_becomes_human_assisted(self):
        """Spec §14: atama sonrası ai_mode = HUMAN_ASSISTED."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_ticket(ticket_id)["ai_mode"], config.AI_MODE_HUMAN)

    def test_32c_assignment_audit_row_is_written(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        queue_worker.run_once(sender=self.sender)

        assignments = db.list_ticket_assignments(ticket_id, active_only=True)
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["assignment_role"], config.ROLE_PRIMARY)
        self.assertEqual(assignments[0]["technician_id"], hakan)
        self.assertEqual(assignments[0]["assigned_by_source"],
                         config.ASSIGN_SOURCE_SYSTEM)

    def test_32d_sla_start_is_preserved_not_reset(self):
        """SLA saati eskalasyonda başlar (Faz 5B). Otomatik atama onu SIFIRLAMAZ —
        aksi hâlde kuyrukta bekleyen süre SLA'dan silinmiş olurdu."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        started_at = db.get_ticket(ticket_id)["sla_started_at"]
        self.assertIsNotNone(started_at, "SLA eskalasyonda başlamış olmalı.")

        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_ticket(ticket_id)["sla_started_at"], started_at)


# ==================================================================
# 29-31: concurrency ve dokunulmazlık
# ==================================================================

class TestQueueSafety(QueueTestCase):

    def test_29_two_workers_cannot_double_assign(self):
        """Spec §21: iki worker aynı talebi görürse yalnızca biri atar."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        first = queue_worker.run_once(sender=self.sender)
        second_sender = RecordingSender()
        second = queue_worker.run_once(sender=second_sender)

        self.assertEqual(first["assigned"], 1)
        self.assertEqual(second["assigned"], 0, "İkinci worker NO-OP olmalı.")
        self.assertEqual(second_sender.calls, [],
                         "İkinci worker kullanıcıya mesaj göndermemeli.")

        active = db.list_ticket_assignments(ticket_id, active_only=True)
        primaries = [a for a in active if a["assignment_role"] == config.ROLE_PRIMARY]
        self.assertEqual(len(primaries), 1, "Talepte tek aktif PRIMARY olmalı.")

    def test_29b_concurrent_evaluate_on_same_ticket_assigns_once(self):
        """Aynı ticket sözlüğü iki kez değerlendirilirse ikincisi no-op olur."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        snapshot = db.get_ticket(ticket_id)

        first = queue_service.evaluate_ticket(snapshot)
        # İkinci worker ELİNDEKİ ESKİ görüntüyle gelir (yarış senaryosu).
        second = queue_service.evaluate_ticket(snapshot)

        self.assertTrue(first["assigned"])
        self.assertFalse(second["assigned"])
        self.assertEqual(second["reason"], "zaten_atanmis")

    def test_30_resolved_ticket_is_not_assigned(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET status = ? WHERE id = ?",
                         (config.STATUS_CLOSED, ticket_id))

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 0)
        self.assertIsNone(db.get_ticket(ticket_id)["assigned_technician_id"])

    def test_30b_evaluate_refuses_non_queue_ticket(self):
        ticket_id = self.make_ticket(skill_code="COMPUTER_VISION")   # status = acik
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"])
        self.assertEqual(decision["reason"], "kuyrukta_degil")

    def test_31_already_assigned_ticket_is_not_changed(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((self.cv, 1),))
        db.assign_primary_if_unassigned(ticket_id, kaan, "Kaan Polat")

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 0)
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], kaan)
        self.assertNotEqual(db.get_ticket(ticket_id)["assigned_technician_id"], hakan)

    def test_31b_ticket_without_skill_is_left_alone(self):
        ticket_id = self.queue_ticket(None)
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 0)
        self.assertIsNone(db.get_ticket(ticket_id)["assigned_technician_id"])

    def test_delivery_failure_does_not_roll_back_assignment(self):
        """Telegram hatası gerçek iş dağıtımını GERİ ALMAZ."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        failing = RecordingSender(fail=True)

        stats = queue_worker.run_once(sender=failing)

        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], hakan)

    def test_available_semantics_unchanged_by_queue_assignment(self):
        """Spec §25: kuyruk ataması da available'ı DEĞİŞTİRMEZ."""
        self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_technician(hakan)["available"], 1)


# ==================================================================
# Kuyruk adaleti — çoklu talep dağılımı
# ==================================================================

class TestQueueFairness(QueueTestCase):

    def test_queue_distributes_work_fairly(self):
        """Spec §20: iki eşit teknisyene iki talep -> ikisi de birer tane alır."""
        self.queue_ticket("COMPUTER_VISION", user_id=1001, subject="Talep A")
        self.queue_ticket("COMPUTER_VISION", user_id=1002, subject="Talep B")
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((self.cv, 1),))

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 2)
        assigned_to = sorted(t["assigned_technician_id"]
                             for t in db.list_tickets()
                             if t["assigned_technician_id"])
        self.assertEqual(assigned_to, sorted([hakan, kaan]),
                         "İkinci talep iş yükü nedeniyle diğer kişiye gitmeli.")

    def test_technician_becoming_available_triggers_reevaluation(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", available=False,
                                     skills=((self.cv, 1),))

        # Meşgulken atanmaz.
        queue_worker.run_once(sender=self.sender)
        self.assertIsNone(db.get_ticket(ticket_id)["assigned_technician_id"])

        db.set_technician_availability(hakan, True)
        decisions = queue_service.reevaluate_for_technician(hakan)

        self.assertTrue(any(d["assigned"] for d in decisions))
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], hakan)

    def test_reevaluation_picks_best_not_the_toggling_person(self):
        """Müsait olan kişi otomatik olarak SEÇİLEN kişi değildir."""
        ticket_id = self.queue_ticket("COMPUTER_VISION")
        idle = self.make_technician("A Bosta", skills=((self.cv, 1),))
        busy = self.make_technician("B Yuklu", available=False,
                                    skills=((self.cv, 1),))
        # idle zaten müsait ve boş; busy müsait olunca yine idle seçilmeli.
        db.set_technician_availability(busy, True)
        self.set_active_workload(busy, 3)

        queue_service.reevaluate_for_technician(busy)

        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], idle)


# ==================================================================
# ACCEPTANCE FIX 2 — DEPARTMAN FALLBACK KUYRUĞU
# ==================================================================

class TestDepartmentFallbackQueue(QueueTestCase):
    """Uzmanlığı belirlenememiş talepler artık kuyrukta ÖLMÜYOR.

    Önceden queue_service 'uzmanlik_tespit_edilmemis' deyip bunları eliyordu;
    departman ataması ayrı ve adaletsiz bir yoldan yapılıyordu."""

    def test_department_only_ticket_is_assigned_by_worker(self):
        ticket_id = self.queue_ticket(None, category="Yazılım",
                                      subject="Uzmanlik tespit edilemedi")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], hakan)

    def test_department_queue_uses_fair_ordering(self):
        """Yüklü kişi değil, boştaki kişi seçilmeli."""
        ticket_id = self.queue_ticket(None, category="Yazılım")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        kaan = self.make_technician("Kaan Polat", department="Yazılım")
        self.set_active_workload(hakan, 4)

        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], kaan)

    def test_department_assignment_message_names_the_team_not_a_skill(self):
        self.queue_ticket(None, category="Yazılım")
        self.make_technician("Hakan Bulut", department="Yazılım")

        queue_worker.run_once(sender=self.sender)
        user_text = self.sender.calls[0]["text"]

        self.assertIn("İlgili ekip: Yazılım", user_text)
        self.assertIn("Atanan uzman: Hakan Bulut", user_text)
        self.assertNotIn("Gerekli uzmanlık", user_text)
        self.assertIsNone(self.sender.calls[0]["reply_markup"])

    def test_ticket_with_neither_skill_nor_department_stays_queued(self):
        ticket_id = self.queue_ticket(None)
        self.make_technician("Hakan Bulut", department="Yazılım")

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 0)
        self.assertIsNone(db.get_ticket(ticket_id)["assigned_technician_id"])
        self.assertEqual(db.get_ticket(ticket_id)["status"],
                         config.STATUS_ESCALATION_PENDING)

    def test_department_ticket_stays_queued_when_nobody_available(self):
        ticket_id = self.queue_ticket(None, category="Yazılım")
        self.make_technician("Hakan Bulut", department="Yazılım", available=False)

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 0)
        self.assertEqual(db.get_ticket(ticket_id)["status"],
                         config.STATUS_ESCALATION_PENDING)
        self.assertIsNotNone(db.get_ticket(ticket_id)["queue_entered_at"])

    def test_skill_ticket_is_not_picked_up_by_department_scan(self):
        """Aynı talep iki kuyruk taramasında İKİ KEZ işlenmemeli."""
        self.queue_ticket("COMPUTER_VISION", category="Yazılım")

        by_department = db.list_waiting_expert_tickets(department="Yazılım")
        by_skill = db.list_waiting_expert_tickets(skill_code="COMPUTER_VISION")

        self.assertEqual(by_department, [], "Skill'i olan talep departman "
                                            "taramasına GİRMEMELİ.")
        self.assertEqual(len(by_skill), 1)

    def test_department_scan_matches_on_pending_department(self):
        self.queue_ticket(None, category="Yazılım")
        self.assertEqual(len(db.list_waiting_expert_tickets(department="Yazılım")), 1)
        self.assertEqual(db.list_waiting_expert_tickets(department="Ağ"), [])


# ==================================================================
# ACCEPTANCE FIX 2 §6-E: team_toggle / müsaitlik yeniden değerlendirmesi
# ==================================================================

class TestAvailabilityReevaluation(QueueTestCase):

    def test_E_toggle_assigns_queued_skill_ticket_via_central_selector(self):
        ticket_id = self.queue_ticket("COMPUTER_VISION", category="Yazılım")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     available=False, skills=((self.cv, 1),))

        db.set_technician_availability(hakan, True)
        decisions = queue_service.reevaluate_for_technician(hakan)

        self.assertTrue(any(d["assigned"] for d in decisions))
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], hakan)

    def test_E2_toggle_assigns_queued_department_ticket(self):
        """Uzmanlığı olmayan talep de müsaitlik açılınca değerlendirilmeli."""
        ticket_id = self.queue_ticket(None, category="Yazılım")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     available=False)

        db.set_technician_availability(hakan, True)
        decisions = queue_service.reevaluate_for_technician(hakan)

        self.assertTrue(any(d["assigned"] for d in decisions))
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], hakan)

    def test_E3_toggled_person_is_not_automatically_the_winner(self):
        """Müsait YAPILAN kişi, seçilen kişi olmak ZORUNDA DEĞİLDİR.

        Eski davranış 'butona son basan işi kapar' idi. Artık kuyruk merkezi
        sıralamayla değerlendirilir; daha az yüklü meslektaş kazanır."""
        ticket_id = self.queue_ticket(None, category="Yazılım")
        idle = self.make_technician("A Bosta", department="Yazılım")
        loaded = self.make_technician("B Yuklu", department="Yazılım",
                                      available=False)
        self.set_active_workload(loaded, 3)

        db.set_technician_availability(loaded, True)
        queue_service.reevaluate_for_technician(loaded)

        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], idle,
                         "Müsait olan kişi değil, EN UYGUN kişi seçilmeli.")

    def test_E4_reevaluation_ignores_other_departments(self):
        ticket_id = self.queue_ticket(None, category="Ağ")
        yazilimci = self.make_technician("Hakan Bulut", department="Yazılım")

        decisions = queue_service.reevaluate_for_technician(yazilimci)

        self.assertFalse(any(d["assigned"] for d in decisions))
        self.assertIsNone(db.get_ticket(ticket_id)["assigned_technician_id"])

    def test_E5_web_team_toggle_route_uses_central_reevaluation(self):
        """webapp.team_toggle artık departman kuyruğundan doğrudan atamıyor."""
        import webapp
        from werkzeug.security import generate_password_hash

        webapp.app.config.update(TESTING=True)
        real_sender = webapp.send_telegram_message
        webapp.send_telegram_message = RecordingSender()
        self.addCleanup(setattr, webapp, "send_telegram_message", real_sender)

        ticket_id = self.queue_ticket(None, category="Yazılım")
        idle = self.make_technician("A Bosta", department="Yazılım")
        loaded = self.make_technician("B Yuklu", department="Yazılım",
                                      available=False)
        self.set_active_workload(loaded, 3)

        admin_id = db.create_web_user(
            username="admin", password_hash=generate_password_hash("Parola12345!"),
            role=config.ROLE_ADMIN, full_name="Admin")

        client = webapp.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = admin_id
            session["csrf_token"] = "t"
        response = client.post(f"/ekip/{loaded}/toggle",
                               data={"csrf_token": "t", "form_token": "tog-1"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_technician(loaded)["available"], 1)
        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], idle,
                         "team_toggle merkezi seçiciyi kullanmalı.")

    def test_E6_toggle_off_does_not_assign_anything(self):
        ticket_id = self.queue_ticket(None, category="Yazılım")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")

        db.set_technician_availability(hakan, False)

        self.assertIsNone(db.get_ticket(ticket_id)["assigned_technician_id"])


# ==================================================================
# ACCEPTANCE FIX 2 §6-F: çift PRIMARY imkânsız
# ==================================================================

class TestNoDoubleAssignmentAcrossPaths(QueueTestCase):

    def test_F_two_reevaluations_cannot_double_assign(self):
        ticket_id = self.queue_ticket(None, category="Yazılım")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        kaan = self.make_technician("Kaan Polat", department="Yazılım")

        first = queue_service.reevaluate_for_technician(hakan)
        second = queue_service.reevaluate_for_technician(kaan)

        self.assertEqual(sum(1 for d in first if d["assigned"]), 1)
        self.assertEqual(sum(1 for d in second if d["assigned"]), 0)

        primaries = [a for a in db.list_ticket_assignments(ticket_id, active_only=True)
                     if a["assignment_role"] == config.ROLE_PRIMARY]
        self.assertEqual(len(primaries), 1)

    def test_F2_worker_and_reevaluation_cannot_double_assign(self):
        """İki FARKLI yol (worker turu + müsaitlik tetiklemesi) çakışırsa."""
        ticket_id = self.queue_ticket(None, category="Yazılım")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")

        stats = queue_worker.run_once(sender=self.sender)
        decisions = queue_service.reevaluate_for_technician(hakan)

        self.assertEqual(stats["assigned"], 1)
        self.assertFalse(any(d["assigned"] for d in decisions))

        primaries = [a for a in db.list_ticket_assignments(ticket_id, active_only=True)
                     if a["assignment_role"] == config.ROLE_PRIMARY]
        self.assertEqual(len(primaries), 1)

    def test_F3_stale_ticket_snapshot_cannot_reassign(self):
        """Elinde ESKİ ticket görüntüsü olan ikinci çağrı no-op olur."""
        ticket_id = self.queue_ticket(None, category="Yazılım")
        self.make_technician("Hakan Bulut", department="Yazılım")
        snapshot = db.get_ticket(ticket_id)

        first = queue_service.evaluate_ticket(snapshot)
        second = queue_service.evaluate_ticket(snapshot)

        self.assertTrue(first["assigned"])
        self.assertFalse(second["assigned"])
        self.assertEqual(second["reason"], "zaten_atanmis")

    def test_F4_department_path_preserves_human_assisted_and_ticket_id(self):
        ticket_id = self.queue_ticket(None, category="Yazılım",
                                      subject="Departman fallback talebi")
        self.make_technician("Hakan Bulut", department="Yazılım")
        before = len(db.list_tickets())

        queue_worker.run_once(sender=self.sender)

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(len(db.list_tickets()), before, "Yeni talep AÇILMAMALI.")
        self.assertEqual(ticket["subject"], "Departman fallback talebi")
        self.assertEqual(ticket["ai_mode"], config.AI_MODE_HUMAN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
