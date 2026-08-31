"""KORUNAN DAVRANIŞ PAKETİ — ACCEPTANCE REVISION §35.

Bu revizyon YALNIZCA routing/UX değiştirdi. Aşağıdaki mevcut davranışların
bozulmadığını doğrular:

    AI attempt sayacı · clarification attempt tüketmez · same-ticket conversation
    skill classification kaydı · assignment audit · PRIMARY/COLLABORATOR
    ownership transfer · internal note (Telegram izolasyonu) · AI Summary
    AI Summary privacy flag · P1-P4 · SLA cycle · WAITING_USER pause/resume
    business hours · hafta sonu · tatil · timezone · calendar revision
    resolve/reopen · reopen -> yeni SLA cycle · structured AI parsing
    invalid skill fallback · rate limit · password/session güvenliği
    delivery status

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ.
"""

from tests import _support, project_file

_support.setup_test_db("preserved")

import unittest

import config
import db
import sla_calendar
from tests._support import DbTestCase


class PreservedTestCase(DbTestCase):
    db = db
    config = config


# ==================================================================
# AI davranışı
# ==================================================================

class TestAiBehaviorPreserved(PreservedTestCase):

    def test_max_three_real_solution_attempts(self):
        """Gemini en fazla 3 GERÇEK çözüm denemesi yapar.

        Sayacı db.claim_ai_attempt ATOMİK olarak yürütür; ÜST SINIR kontrolü
        bot.run_ai_turn içindedir ('attempt >= max_attempts -> yeni çözüm
        üretilmez, eskalasyon teklif edilir'). İkisi birlikte doğrulanır."""
        ticket_id = self.make_ticket()
        self.assertEqual(config.MAX_AI_SOLUTION_ATTEMPTS, 3)

        for expected in range(config.MAX_AI_SOLUTION_ATTEMPTS):
            self.assertTrue(db.claim_ai_attempt(ticket_id, expected),
                            f"{expected}. deneme hakkı alınabilmeliydi.")

        self.assertEqual(db.get_ticket(ticket_id)["ai_attempt_count"],
                         config.MAX_AI_SOLUTION_ATTEMPTS)

        # Üst sınır kapısı hâlâ yerinde mi?
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("if attempt >= max_attempts:", source,
                      "4. çözüm denemesini engelleyen kapı KALDIRILMAMALI.")

    def test_claim_attempt_is_race_safe(self):
        """Aynı beklenen sayaçla iki eş zamanlı çağrıdan yalnızca biri kazanır."""
        ticket_id = self.make_ticket()
        self.assertTrue(db.claim_ai_attempt(ticket_id, 0))
        self.assertFalse(db.claim_ai_attempt(ticket_id, 0),
                         "Aynı sayaç değeri iki kez tüketilemez.")

    def test_clarification_does_not_consume_attempt(self):
        """Açıklayıcı soru çözüm denemesi SAYILMAZ."""
        ticket_id = self.make_ticket()
        db.claim_ai_attempt(ticket_id, 0)
        before = db.get_ticket(ticket_id)["ai_attempt_count"]

        db.add_message(ticket_id, "ai", 0, "AI", "Hangi cihazı kullanıyorsunuz?",
                       message_kind=config.MESSAGE_KIND_AI_CLARIFICATION)

        self.assertEqual(db.get_ticket(ticket_id)["ai_attempt_count"], before)
        self.assertEqual(db.count_ai_solution_messages(ticket_id), 0,
                         "Clarification, çözüm mesajı olarak sayılmamalı.")

    def test_only_solution_messages_are_counted(self):
        ticket_id = self.make_ticket()
        db.add_message(ticket_id, "ai", 0, "AI", "Çözüm önerisi",
                       message_kind=config.MESSAGE_KIND_AI_SOLUTION)
        db.add_message(ticket_id, "ai", 0, "AI", "Soru?",
                       message_kind=config.MESSAGE_KIND_AI_CLARIFICATION)
        self.assertEqual(db.count_ai_solution_messages(ticket_id), 1)

    def test_ai_goes_silent_after_assignment(self):
        """Atama sonrası ai_mode = HUMAN_ASSISTED -> Gemini susar."""
        tech_id = self.make_technician("Hakan Bulut")
        ticket_id = self.make_ticket()
        db.set_ai_mode(ticket_id, config.AI_MODE_HANDLING)

        db.assign_primary_if_unassigned(ticket_id, tech_id, "Hakan Bulut")

        self.assertEqual(db.get_ticket(ticket_id)["ai_mode"], config.AI_MODE_HUMAN)

    def test_skill_classification_is_stored(self):
        skill_id = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        ticket_id = self.make_ticket()

        db.set_ai_classification(ticket_id, category="Yazılım",
                                 skill_code="COMPUTER_VISION")

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["required_skill_code"], "COMPUTER_VISION")
        self.assertEqual(ticket["category"], "Yazılım")
        self.assertEqual(db.get_skill(skill_id)["code"], "COMPUTER_VISION")

    def test_same_ticket_conversation_is_preserved(self):
        """Konuşma AYNI talep üzerinde ilerler; yeni talep açılmaz."""
        ticket_id = self.make_ticket(user_id=555)
        # create_ticket, konuyu ilk kullanıcı mesajı olarak da yazar.
        opening = len(db.get_messages(ticket_id))

        db.add_message(ticket_id, "kullanici", 555, "Kullanıcı", "İlk mesaj")
        db.add_message(ticket_id, "ai", 0, "AI", "Öneri")
        db.add_message(ticket_id, "kullanici", 555, "Kullanıcı", "İkinci mesaj")

        self.assertEqual(db.get_open_ticket_for_user(555)["id"], ticket_id)
        self.assertEqual(len(db.get_messages(ticket_id)), opening + 3)
        self.assertEqual(len(db.list_tickets_for_user(555)), 1,
                         "Konuşma devam ederken YENİ talep açılmamalı.")


# ==================================================================
# Atama modeli
# ==================================================================

class TestAssignmentModelPreserved(PreservedTestCase):

    def setUp(self):
        super().setUp()
        self.hakan = self.make_technician("Hakan Bulut")
        self.kaan = self.make_technician("Kaan Polat")
        self.ticket_id = self.make_ticket()

    def test_primary_assignment_is_atomic(self):
        self.assertTrue(db.assign_primary_if_unassigned(
            self.ticket_id, self.hakan, "Hakan Bulut"))
        self.assertFalse(db.assign_primary_if_unassigned(
            self.ticket_id, self.kaan, "Kaan Polat"),
            "İkinci PRIMARY ataması reddedilmeli.")
        self.assertEqual(db.get_ticket(self.ticket_id)["assigned_technician_id"],
                         self.hakan)

    def test_primary_cannot_also_be_collaborator(self):
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan, "Hakan Bulut")
        ok, message = db.add_collaborator(self.ticket_id, self.hakan)
        self.assertFalse(ok, message)

    def test_ownership_transfer_requires_reason(self):
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan, "Hakan Bulut")

        ok, _ = db.transfer_primary(self.ticket_id, self.kaan, self.hakan, "kısa")
        self.assertFalse(ok, "Kısa gerekçe reddedilmeli.")

        ok, message = db.transfer_primary(self.ticket_id, self.kaan, self.hakan,
                                          "Vardiya değişimi nedeniyle devrediliyor")
        self.assertTrue(ok, message)
        self.assertEqual(db.get_ticket(self.ticket_id)["assigned_technician_id"],
                         self.kaan)

    def test_transfer_does_not_auto_demote_to_collaborator(self):
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan, "Hakan Bulut")
        db.transfer_primary(self.ticket_id, self.kaan, self.hakan,
                            "Vardiya değişimi nedeniyle devrediliyor")

        collaborators = db.list_active_collaborators(self.ticket_id)
        self.assertEqual(collaborators, [],
                         "Eski ana sorumlu OTOMATİK yardımcı YAPILMAZ.")

    def test_transfer_uses_compare_and_swap(self):
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan, "Hakan Bulut")
        other = self.make_technician("Derya Uçar")

        # expected_current_id yanlış -> devir reddedilir.
        ok, message = db.transfer_primary(self.ticket_id, other, self.kaan,
                                          "Yanlış beklenen sahiple devir denemesi")
        self.assertFalse(ok, message)
        self.assertEqual(db.get_ticket(self.ticket_id)["assigned_technician_id"],
                         self.hakan)

    def test_assignment_audit_trail_is_complete(self):
        db.assign_primary_if_unassigned(
            self.ticket_id, self.hakan, "Hakan Bulut",
            actor={"source": config.ASSIGN_SOURCE_WEB, "label": "Admin"})
        db.add_collaborator(self.ticket_id, self.kaan)
        db.remove_collaborator(self.ticket_id, self.kaan, reason="Görev bitti")

        history = db.list_ticket_assignments(self.ticket_id, active_only=False)
        self.assertEqual(len(history), 2, "Atama geçmişi SİLİNMEZ.")
        removed = [a for a in history if a["removed_at"]]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["removal_reason"], "Görev bitti")

    def test_collaborator_workload_stays_separate(self):
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan, "Hakan Bulut")
        db.add_collaborator(self.ticket_id, self.kaan)

        self.assertEqual(db.count_active_tickets_for_technician(self.hakan), 1)
        self.assertEqual(db.count_active_tickets_for_technician(self.kaan), 0)
        self.assertEqual(db.count_collaborations(self.kaan), 1)


# ==================================================================
# Dahili not / AI özeti
# ==================================================================

class TestNotesAndSummaryPreserved(PreservedTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.make_ticket()

    def test_internal_note_is_never_delivered_to_user(self):
        """Dahili not delivery_status ALMAZ — Telegram'a giden yol ayrı rotadır."""
        message_id = db.add_internal_note(
            self.ticket_id, "teknisyen", "Hakan", "Müşteriye söyleme", 1)

        note = next(m for m in db.get_messages(self.ticket_id) if m["id"] == message_id)
        self.assertEqual(note["message_kind"], config.MESSAGE_KIND_INTERNAL_NOTE)
        self.assertIsNone(note["delivery_status"])
        self.assertTrue(db.is_internal(note))

    def test_summary_context_respects_privacy_flag(self):
        """AI Summary privacy flag: dahili notlar isteğe bağlı hariç tutulur."""
        db.add_message(self.ticket_id, "kullanici", 1, "Kullanıcı", "Açık mesaj")
        db.add_internal_note(self.ticket_id, "teknisyen", "Hakan", "GİZLİ NOT", 1)

        with_notes = db.get_summary_context(self.ticket_id, include_internal_notes=True)
        without_notes = db.get_summary_context(self.ticket_id, include_internal_notes=False)

        self.assertIn("GİZLİ NOT", str(with_notes))
        self.assertNotIn("GİZLİ NOT", str(without_notes))
        self.assertIn("Açık mesaj", str(without_notes))

    def test_ai_summary_round_trip(self):
        summary = {"summary": "Kamera bulanık görüntü veriyor.",
                   "key_points": ["Lens kirli olabilir"],
                   "next_steps": ["Lens temizliği"]}
        db.save_ai_summary(self.ticket_id, summary, generated_by_user_id=None)

        stored = db.get_ai_summary(self.ticket_id)
        self.assertIsNotNone(stored)
        self.assertIn("Kamera bulanık", str(stored))

    def test_delivery_status_is_recorded(self):
        message_id = db.add_message(self.ticket_id, "teknisyen", 1, "Hakan", "Yanıt")
        db.set_message_delivery(message_id, "basarisiz", "Timeout")

        message = next(m for m in db.get_messages(self.ticket_id) if m["id"] == message_id)
        self.assertEqual(message["delivery_status"], "basarisiz")
        self.assertEqual(message["delivery_error"], "Timeout")


# ==================================================================
# Öncelik / SLA / takvim
# ==================================================================

class TestSlaPreserved(PreservedTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.make_ticket()

    def test_priority_levels_p1_to_p4(self):
        self.assertEqual(tuple(config.PRIORITIES), ("P1", "P2", "P3", "P4"))
        for priority in config.PRIORITIES:
            ok, message = db.set_ticket_priority(self.ticket_id, priority)
            self.assertTrue(ok, message)
            self.assertEqual(db.get_ticket(self.ticket_id)["priority"], priority)

    def test_invalid_priority_is_rejected(self):
        ok, _ = db.set_ticket_priority(self.ticket_id, "P9")
        self.assertFalse(ok)

    def test_sla_cycle_is_created_and_closed(self):
        db.start_sla_cycle_business(self.ticket_id, "P2")
        cycles = db.list_sla_cycles(self.ticket_id)
        self.assertEqual(len(cycles), 1)
        self.assertIsNotNone(db.get_active_cycle(self.ticket_id))

        self.assertTrue(db.close_sla_cycle(self.ticket_id))
        self.assertIsNone(db.get_active_cycle(self.ticket_id))

    def test_waiting_user_pauses_and_resumes_sla(self):
        db.start_sla_cycle_business(self.ticket_id, "P2")

        self.assertTrue(db.pause_sla_business(self.ticket_id, reason="Kullanıcı bekleniyor"))
        paused = db.get_active_cycle(self.ticket_id)
        self.assertIsNotNone(paused["paused_at"])

        self.assertTrue(db.resume_sla_business(self.ticket_id))
        resumed = db.get_active_cycle(self.ticket_id)
        self.assertIsNone(resumed["paused_at"])

    def test_status_change_drives_sla_pause(self):
        db.start_sla_cycle_business(self.ticket_id, "P2")
        db.apply_status_sla_effect(self.ticket_id, config.STATUS_WAITING_USER)
        self.assertIsNotNone(db.get_active_cycle(self.ticket_id)["paused_at"])

        db.apply_status_sla_effect(self.ticket_id, config.STATUS_IN_PROGRESS)
        self.assertIsNone(db.get_active_cycle(self.ticket_id)["paused_at"])

    def test_business_hours_configuration_exists(self):
        self.assertTrue(config.BUSINESS_HOURS)
        status = sla_calendar.calendar_status()
        self.assertIn("state", status)
        self.assertIn("label", status)

    def test_holiday_calendar_and_revision_bump(self):
        before = db.get_calendar_revision()

        holiday_id, error = db.create_holiday("2026-12-31", "Yılbaşı arifesi")
        self.assertIsNone(error)
        # list_active_holiday_dates date nesnesi döner (SQLite date adapter'ı).
        active = {str(d) for d in db.list_active_holiday_dates()}
        self.assertIn("2026-12-31", active)
        self.assertGreater(db.get_calendar_revision(), before,
                           "Takvim değişikliği revision'ı artırmalı.")

        db.set_holiday_active(holiday_id, False)
        self.assertNotIn("2026-12-31",
                         {str(d) for d in db.list_active_holiday_dates()})
        self.assertIsNotNone(db.get_holiday(holiday_id),
                            "Tatil SİLİNMEZ, pasifleştirilir.")

    def test_invalid_holiday_date_is_rejected(self):
        _, error = db.create_holiday("31/12/2026", "Hatalı format")
        self.assertIsNotNone(error)

    def test_priority_change_recomputes_deadlines_not_start(self):
        db.start_sla_cycle_business(self.ticket_id, "P4")
        cycle = db.get_active_cycle(self.ticket_id)
        started_at, old_due = cycle["started_at"], cycle["resolution_due_at"]

        db.apply_priority_business(self.ticket_id, "P1")
        updated = db.get_active_cycle(self.ticket_id)

        self.assertEqual(updated["started_at"], started_at, "Başlangıç DEĞİŞMEZ.")
        self.assertNotEqual(updated["resolution_due_at"], old_due,
                            "Son tarih yeni önceliğe göre yeniden hesaplanmalı.")


# ==================================================================
# Güvenlik: rate limit, parola, uzmanlık kataloğu
# ==================================================================

class TestSecurityPreserved(PreservedTestCase):

    def test_login_rate_limit_blocks_after_threshold(self):
        identifier = "test-client-1"
        for _ in range(config.LOGIN_MAX_ATTEMPTS):
            db.record_login_failure(identifier)

        self.assertTrue(db.is_login_blocked(identifier))
        self.assertGreaterEqual(db.count_recent_login_failures(identifier),
                                config.LOGIN_MAX_ATTEMPTS)

    def test_successful_login_clears_failures(self):
        identifier = "test-client-2"
        for _ in range(config.LOGIN_MAX_ATTEMPTS):
            db.record_login_failure(identifier)
        db.clear_login_failures(identifier)

        self.assertFalse(db.is_login_blocked(identifier))

    def test_form_token_is_single_use(self):
        self.assertTrue(db.claim_form_token("tekil-token"))
        self.assertFalse(db.claim_form_token("tekil-token"),
                         "Aynı form token ikinci kez kullanılamaz.")

    def test_empty_form_token_is_rejected(self):
        self.assertFalse(db.claim_form_token(""))

    def test_password_is_only_stored_as_hash(self):
        import auth
        from werkzeug.security import generate_password_hash

        user_id = db.create_web_user(
            username="hashtest",
            password_hash=generate_password_hash("GizliParola123!"),
            role=config.ROLE_TECHNICIAN, full_name="Hash Test")

        row = db.get_web_user(user_id)
        self.assertNotIn("GizliParola123!", row["password_hash"])
        self.assertIsNotNone(auth.authenticate("hashtest", "GizliParola123!"))
        self.assertIsNone(auth.authenticate("hashtest", "YanlisParola"))

    def test_temporary_password_forces_change(self):
        import auth
        from werkzeug.security import generate_password_hash

        user_id = db.create_web_user(
            username="temptest",
            password_hash=generate_password_hash("EskiParola123!"),
            role=config.ROLE_TECHNICIAN, full_name="Temp Test")

        ok, message = auth.set_temporary_password(user_id, "GeciciParola123!")
        self.assertTrue(ok, message)
        self.assertTrue(db.get_web_user(user_id)["must_change_password"])

    def test_skill_is_deactivated_not_deleted(self):
        skill_id = self.make_skill("LEGACY_SKILL", "Eski Uzmanlık")
        db.set_skill_active(skill_id, False)

        self.assertIsNotNone(db.get_skill(skill_id), "Uzmanlık SİLİNMEZ.")
        self.assertFalse(db.get_skill(skill_id)["active"])
        self.assertNotIn(skill_id, [s["id"] for s in db.list_skills(only_active=True)])

    def test_inactive_skill_produces_no_candidates(self):
        import routing
        skill_id = self.make_skill("PASIF_SKILL", "Pasif")
        self.make_technician("Hakan Bulut", skills=((skill_id, 1),))
        db.set_skill_active(skill_id, False)

        result = routing.select_best_technician_for_skill("PASIF_SKILL")
        self.assertIsNone(result["selected"])

    def test_same_skill_cannot_occupy_two_priorities(self):
        skill_id = self.make_skill("COMPUTER_VISION")
        tech_id = self.make_technician("Hakan Bulut")

        self.assertTrue(db.set_technician_skill(tech_id, skill_id, 1)[0])
        db.set_technician_skill(tech_id, skill_id, 2)

        skills = db.list_technician_skills(tech_id)
        codes = [s["skill_code"] for s in skills]
        self.assertEqual(len(codes), len(set(codes)),
                         "Aynı uzmanlık iki öncelikte bulunamaz.")


# ==================================================================
# Veritabanı bütünlüğü
# ==================================================================

class TestDatabaseIntegrity(PreservedTestCase):

    def test_only_one_active_primary_per_ticket(self):
        import sqlite3
        hakan = self.make_technician("Hakan Bulut")
        kaan = self.make_technician("Kaan Polat")
        ticket_id = self.make_ticket()
        db.assign_primary_if_unassigned(ticket_id, hakan, "Hakan Bulut")

        with self.assertRaises(sqlite3.IntegrityError,
                               msg="Partial UNIQUE index ikinci PRIMARY'yi engellemeli"):
            with db.get_conn() as conn:
                db._insert_assignment(conn, ticket_id, kaan,
                                      config.ROLE_PRIMARY, None)

    def test_integrity_check_passes(self):
        tech_id = self.make_technician("Hakan Bulut")
        ticket_id = self.make_ticket()
        db.assign_primary_if_unassigned(ticket_id, tech_id, "Hakan Bulut")
        db.add_message(ticket_id, "teknisyen", 1, "Hakan", "Yanıt")

        with db.get_conn() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual([dict(r) for r in violations], [],
                         "Yabancı anahtar ihlali olmamalı.")

    def test_schema_migration_is_idempotent(self):
        db.init_db()
        db.init_db()
        with db.get_conn() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for expected in ("tickets", "messages", "technicians", "web_users",
                         "skills", "technician_skills", "ticket_assignments"):
            self.assertIn(expected, tables)


# ==================================================================
# ACCEPTANCE FIX 2 §5: yapılandırılmış AI çıktısı ve skill doğrulaması
#
# Gerçek Gemini ÇAĞRILMAZ — ai_assistant._generate mock'lanır.
# ==================================================================

class TestStructuredAiParsing(PreservedTestCase):

    def setUp(self):
        super().setUp()
        import ai_assistant
        self.ai = ai_assistant
        # Ağa çıkan TEK fonksiyon budur; testte asla gerçek çağrı olmaz.
        self._real_generate = ai_assistant._generate
        self.addCleanup(setattr, ai_assistant, "_generate", self._real_generate)

    def _respond_with(self, text):
        self.ai._generate = lambda prompt: text

    # ---------- _parse_json ----------

    def test_plain_json_is_parsed(self):
        self.assertEqual(
            self.ai._parse_json('{"response_type": "clarification", "message": "Selam"}'),
            {"response_type": "clarification", "message": "Selam"})

    def test_markdown_fenced_json_is_parsed(self):
        fenced = '```json\n{"response_type": "solution_attempt", "message": "Dene"}\n```'
        parsed = self.ai._parse_json(fenced)
        self.assertEqual(parsed["response_type"], "solution_attempt")

    def test_json_with_leading_prose_is_recovered(self):
        noisy = 'Tabii, işte yanıt:\n{"response_type": "clarification", "message": "Hangi cihaz?"}'
        parsed = self.ai._parse_json(noisy)
        self.assertEqual(parsed["message"], "Hangi cihaz?")

    def test_broken_json_returns_none_instead_of_raising(self):
        for bad in ("{bozuk", "", None, "sadece düz metin", "[1, 2, 3]"):
            with self.subTest(payload=bad):
                self.assertIsNone(self.ai._parse_json(bad))

    # ---------- get_conversation_response ----------

    def test_conversation_response_returns_structured_dict(self):
        self._respond_with('{"response_type": "solution_attempt", "message": "Yeniden başlatın"}')
        result = self.ai.get_conversation_response("Yazıcı çalışmıyor", [], 0, 3)

        self.assertEqual(result["response_type"], config.AI_RESPONSE_SOLUTION)
        self.assertEqual(result["message"], "Yeniden başlatın")

    def test_unknown_response_type_degrades_to_clarification(self):
        """Tanınmayan tip çözüm denemesi SAYILMAZ — hak boşa harcanmaz."""
        self._respond_with('{"response_type": "uydurma_tip", "message": "Bir şey"}')
        result = self.ai.get_conversation_response("Konu", [], 0, 3)

        self.assertEqual(result["response_type"], config.AI_RESPONSE_CLARIFICATION)

    def test_solution_is_forced_to_escalation_when_attempts_exhausted(self):
        """Hak dolduysa model 'çözüm' dese bile 4. çözüm ÜRETİLMEZ."""
        self._respond_with('{"response_type": "solution_attempt", "message": "Dördüncü"}')
        result = self.ai.get_conversation_response("Konu", [], attempt_count=3, max_attempts=3)

        self.assertEqual(result["response_type"], config.AI_RESPONSE_ESCALATION)

    def test_broken_output_returns_none_for_safe_fallback(self):
        """Bozuk çıktıda None döner; kullanıcıya ham JSON GÖSTERİLMEZ."""
        self._respond_with("bu JSON değil")
        self.assertIsNone(self.ai.get_conversation_response("Konu", [], 0, 3))

    def test_empty_message_returns_none(self):
        self._respond_with('{"response_type": "clarification", "message": "   "}')
        self.assertIsNone(self.ai.get_conversation_response("Konu", [], 0, 3))

    # ---------- classify_for_escalation ----------

    def test_classification_returns_category_and_skill(self):
        self._respond_with('{"category": "Yazılım", "required_skill": "computer_vision"}')
        result = self.ai.classify_for_escalation("Kamera bulanık", [], [])

        self.assertEqual(result["category"], "Yazılım")
        self.assertEqual(result["required_skill"], "COMPUTER_VISION",
                         "Skill kodu büyük harfe normalize edilmeli.")

    def test_unknown_category_is_discarded(self):
        self._respond_with('{"category": "Uzay Mühendisliği", "required_skill": null}')
        result = self.ai.classify_for_escalation("Konu", [], [])

        self.assertIsNone(result["category"], "Katalog dışı departman kabul edilmemeli.")

    def test_null_like_skill_values_become_none(self):
        for raw in ("null", "NONE", '""'):
            with self.subTest(raw=raw):
                value = "null" if raw == "null" else raw.strip('"')
                self._respond_with(
                    '{"category": "Yazılım", "required_skill": "%s"}' % value)
                self.assertIsNone(self.ai.classify_for_escalation("K", [], [])["required_skill"])

    def test_classification_failure_is_reported_as_not_ok(self):
        """ACCEPTANCE UX + ROUTING REVISION §7: bozuk JSON bir TAHMİN değildir.

        Eskiden başarısızlık {"category": None, "required_skill": None} ile
        temsil ediliyordu ve çağıran taraf bunu 'skill yok' sanıp departmana
        düşüyordu. Artık ok=False AÇIKÇA bildirilir; bu MANUAL_TRIAGE demektir,
        genel destek ya da departman tahmini DEĞİL."""
        self._respond_with("çöp")
        result = self.ai.classify_for_escalation("Konu", [], [])

        self.assertFalse(result["ok"], "Parse hatası ok=False ile bildirilmeli.")
        self.assertIsNone(result["category"])
        self.assertIsNone(result["required_skill"])
        self.assertIsNone(result["classification_type"])

    def test_successful_classification_is_reported_as_ok(self):
        self._respond_with('{"classification_type": "specific_skill", '
                           '"category": "Yazılım", "required_skill": "OCR"}')
        result = self.ai.classify_for_escalation("Konu", [], [])

        self.assertTrue(result["ok"])
        self.assertEqual(result["classification_type"], "specific_skill")

    def test_unknown_classification_type_is_not_invented(self):
        """Tanınmayan tip None olur; 'general_support' varsayılmaz."""
        self._respond_with('{"classification_type": "her_neyse", '
                           '"category": "Yazılım", "required_skill": null}')
        result = self.ai.classify_for_escalation("Konu", [], [])

        self.assertTrue(result["ok"])
        self.assertIsNone(result["classification_type"])


class TestInvalidSkillFallback(PreservedTestCase):
    """Model uydurma skill döndürürse DB'ye YAZILMAZ ve departmana düşülür."""

    def test_unknown_skill_code_is_not_stored(self):
        import routing
        self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        ticket_id = self.make_ticket()

        # bot.start_escalation'ın doğrulama adımı: katalogda var mı, aktif mi?
        raw_skill = "UYDURMA_SKILL"
        skill = db.get_skill_by_code(raw_skill)
        validated = skill["code"] if (skill and skill["active"]) else None

        self.assertIsNone(validated, "Geçersiz skill doğrulamayı geçmemeli.")

        db.set_ai_classification(ticket_id, category="Yazılım", skill_code=validated)
        self.assertIsNone(db.get_ticket(ticket_id)["required_skill_code"])

    def test_inactive_skill_does_not_pass_validation(self):
        skill_id = self.make_skill("PASIF", "Pasif Uzmanlık")
        db.set_skill_active(skill_id, False)

        skill = db.get_skill_by_code("PASIF")
        self.assertIsNotNone(skill)
        self.assertFalse(skill["active"], "Pasif skill routing'e girmemeli.")

    def test_ticket_without_skill_routes_by_department(self):
        import routing
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        ticket_id = self.make_ticket(category="Yazılım")
        db.mark_escalation_pending(ticket_id, department="Yazılım")

        result = routing.select_for_ticket(db.get_ticket(ticket_id))

        self.assertEqual(result["mode"], routing.MODE_DEPARTMENT)
        self.assertEqual(result["selected"]["technician_id"], hakan)

    def test_skill_validation_lives_in_routing_and_bot_uses_it(self):
        """Doğrulama adımının kaynakta DURDUĞUNU garanti eder.

        ACCEPTANCE UX + ROUTING REVISION: doğrulama bot.py'den routing.py'ye
        TAŞINDI (tek merkez). Kural gevşemedi, aksine güçlendi — artık aynı
        doğrulayıcıyı web paneli de kullanıyor. Bot'un modele körü körüne
        güvenmediğini, doğrulanmış kararı kullandığını doğruluyoruz."""
        with open(project_file("routing.py"), encoding="utf-8") as fh:
            routing_source = fh.read()
        self.assertIn("def _validated_skill_code", routing_source)
        self.assertIn("db.get_skill_by_code(code)", routing_source)
        self.assertIn('if skill and skill["active"]:', routing_source)

        with open(project_file("bot.py"), encoding="utf-8") as fh:
            bot_source = fh.read()
        self.assertIn("routing.resolve_classification(", bot_source)
        # Ham model çıktısı DB'ye doğrudan yazılmamalı.
        self.assertNotIn('skill_code=classification.get("required_skill")',
                         bot_source)

    def test_validator_rejects_unknown_and_inactive_codes(self):
        """Doğrulayıcının GERÇEKTEN elediğini kanıtlar (kaynak taraması yetmez)."""
        import routing
        self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        pasif = self.make_skill("PASIF", "Pasif Uzmanlık")
        db.set_skill_active(pasif, False)

        self.assertEqual(routing._validated_skill_code("computer_vision"),
                         "COMPUTER_VISION")
        self.assertIsNone(routing._validated_skill_code("UYDURMA_SKILL"))
        self.assertIsNone(routing._validated_skill_code("PASIF"))
        self.assertIsNone(routing._validated_skill_code(None))


# ==================================================================
# ACCEPTANCE FIX 2 §5: resolve / reopen ve yeni SLA cycle
# ==================================================================

class TestResolveReopenPreserved(PreservedTestCase):

    def setUp(self):
        super().setUp()
        self.hakan = self.make_technician("Hakan Bulut")
        self.ticket_id = self.make_ticket()
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan, "Hakan Bulut")

    def test_resolve_is_idempotent(self):
        first = db.resolve_ticket_if_open(self.ticket_id,
                                          source=config.RESOLVED_SOURCE_AI)
        second = db.resolve_ticket_if_open(self.ticket_id,
                                           source=config.RESOLVED_SOURCE_AI)

        self.assertTrue(first)
        self.assertFalse(second, "İkinci çözme çağrısı NO-OP olmalı.")
        self.assertEqual(db.get_ticket(self.ticket_id)["status"], config.STATUS_CLOSED)

    def test_resolve_records_audit_source(self):
        db.resolve_ticket_if_open(self.ticket_id, source=config.ASSIGN_SOURCE_TELEGRAM,
                                  external_id=12345, label="Hakan Bulut")
        ticket = db.get_ticket(self.ticket_id)

        self.assertEqual(ticket["resolved_by_source"], config.ASSIGN_SOURCE_TELEGRAM)
        self.assertEqual(ticket["resolved_by_label"], "Hakan Bulut")
        self.assertIsNotNone(ticket["resolved_at"])

    def test_reopen_keeps_same_ticket_and_history(self):
        db.add_message(self.ticket_id, "kullanici", 1, "Kullanıcı", "İlk mesaj")
        before = len(db.get_messages(self.ticket_id))
        db.resolve_ticket_if_open(self.ticket_id, source=config.RESOLVED_SOURCE_AI)

        db.reopen_ticket(self.ticket_id)

        ticket = db.get_ticket(self.ticket_id)
        self.assertEqual(ticket["id"], self.ticket_id, "YENİ talep açılmamalı.")
        self.assertEqual(ticket["status"], config.STATUS_REOPENED)
        self.assertEqual(len(db.get_messages(self.ticket_id)), before,
                         "Mesaj geçmişi korunmalı.")

    def test_reopen_clears_resolution_metadata(self):
        db.resolve_ticket_if_open(self.ticket_id, source=config.RESOLVED_SOURCE_AI,
                                  label="Çözen")
        db.reopen_ticket(self.ticket_id)
        ticket = db.get_ticket(self.ticket_id)

        self.assertIsNone(ticket["resolved_at"])
        self.assertIsNone(ticket["first_staff_response_at"],
                          "İlk yanıt SLA'sı yeniden başlamalı.")

    def test_reopen_starts_a_new_sla_cycle(self):
        db.start_sla_cycle_business(self.ticket_id, "P2")
        db.close_sla_cycle(self.ticket_id)
        db.resolve_ticket_if_open(self.ticket_id, source=config.RESOLVED_SOURCE_AI)

        db.reopen_ticket(self.ticket_id)

        cycles = db.list_sla_cycles(self.ticket_id)
        self.assertEqual(len(cycles), 2, "Reopen YENİ cycle açmalı.")
        self.assertIsNotNone(db.get_active_cycle(self.ticket_id))

    def test_reopen_does_not_mutate_the_old_cycle(self):
        """Geçmiş cycle IMMUTABLE — eski SLA sözleşmesi değişmez."""
        db.start_sla_cycle_business(self.ticket_id, "P2")
        first = db.get_active_cycle(self.ticket_id)
        first_id, first_due = first["id"], first["resolution_due_at"]
        db.close_sla_cycle(self.ticket_id)
        db.resolve_ticket_if_open(self.ticket_id, source=config.RESOLVED_SOURCE_AI)

        db.reopen_ticket(self.ticket_id)

        old = next(c for c in db.list_sla_cycles(self.ticket_id) if c["id"] == first_id)
        self.assertEqual(old["resolution_due_at"], first_due)
        self.assertIsNotNone(old["closed_at"])

    def test_double_reopen_cannot_create_two_active_cycles(self):
        db.resolve_ticket_if_open(self.ticket_id, source=config.RESOLVED_SOURCE_AI)
        db.reopen_ticket(self.ticket_id)
        db.reopen_ticket(self.ticket_id)

        active = [c for c in db.list_sla_cycles(self.ticket_id) if not c["closed_at"]]
        self.assertEqual(len(active), 1, "Aynı anda tek aktif cycle olmalı.")


# ==================================================================
# ACCEPTANCE FIX 2 §5: iş saatleri, hafta sonu, tatil, timezone
# ==================================================================

class TestBusinessCalendarPreserved(PreservedTestCase):

    def setUp(self):
        super().setUp()
        import sla_calendar
        self.calendar = sla_calendar
        sla_calendar.invalidate_cache()
        self.addCleanup(sla_calendar.invalidate_cache)

    def _local(self, year, month, day, hour=12, minute=0):
        from datetime import datetime
        return datetime(year, month, day, hour, minute,
                        tzinfo=self.calendar.get_timezone())

    def test_timezone_is_configured_and_resolvable(self):
        tz = self.calendar.get_timezone()
        self.assertIsNotNone(tz)
        self.assertEqual(str(tz), config.SLA_TIMEZONE)
        self.assertIsNotNone(self.calendar.now_local().tzinfo,
                             "Yerel zaman timezone-aware olmalı.")

    def test_weekday_inside_hours_is_business_time(self):
        # 2026-08-26 Çarşamba
        self.assertTrue(self.calendar.is_business_time(self._local(2026, 8, 26, 10)))

    def test_weekday_before_and_after_hours_is_not_business_time(self):
        self.assertFalse(self.calendar.is_business_time(self._local(2026, 8, 26, 7, 59)))
        self.assertFalse(self.calendar.is_business_time(self._local(2026, 8, 26, 18, 1)))

    def test_weekend_is_never_business_time(self):
        # 2026-08-29 Cumartesi, 2026-08-30 Pazar
        for day in (29, 30):
            with self.subTest(day=day):
                self.assertFalse(
                    self.calendar.is_business_time(self._local(2026, 8, day, 10)))

    def test_weekend_has_no_business_window(self):
        from datetime import date
        self.assertIsNone(self.calendar.business_window(date(2026, 8, 29)))
        self.assertIsNotNone(self.calendar.business_window(date(2026, 8, 26)))

    def test_holiday_is_not_business_time(self):
        from datetime import date
        db.create_holiday("2026-08-26", "Test tatili")   # normalde Çarşamba
        self.calendar.invalidate_cache()

        self.assertTrue(self.calendar.is_holiday(date(2026, 8, 26)))
        self.assertFalse(self.calendar.is_business_time(self._local(2026, 8, 26, 10)))

    def test_next_business_time_skips_weekend(self):
        # Cuma 18:30 -> Pazartesi sabahına atlamalı
        nxt = self.calendar.next_business_time(self._local(2026, 8, 28, 18, 30))
        self.assertEqual(nxt.weekday(), 0, "Pazartesiye atlamalı.")
        self.assertTrue(self.calendar.is_business_time(nxt))

    def test_business_minutes_ignore_closed_hours(self):
        """Cuma 17:00 -> Pazartesi 09:00 arası 60+60 = 120 iş dakikası."""
        start = self._local(2026, 8, 28, 17, 0)     # Cuma
        end = self._local(2026, 8, 31, 9, 0)        # Pazartesi
        self.assertEqual(self.calendar.business_minutes_between(start, end), 120)

    def test_add_business_minutes_lands_inside_working_hours(self):
        deadline = self.calendar.add_business_minutes(
            self._local(2026, 8, 28, 17, 30), 120)   # Cuma 17:30 + 2 iş saati
        self.assertEqual(deadline.weekday(), 0, "Pazartesiye taşmalı.")
        self.assertTrue(self.calendar.is_business_time(deadline))

    def test_calendar_status_reports_state(self):
        status = self.calendar.calendar_status(self._local(2026, 8, 29, 10))
        self.assertIn("state", status)
        self.assertIn("label", status)

    def test_holiday_change_bumps_revision_and_invalidates_cache(self):
        from datetime import date
        before = db.get_calendar_revision()
        holiday_id, error = db.create_holiday("2026-09-01", "Yeni tatil")
        self.assertIsNone(error)

        self.assertGreater(db.get_calendar_revision(), before)
        self.calendar.invalidate_cache()
        self.assertTrue(self.calendar.is_holiday(date(2026, 9, 1)))

        db.set_holiday_active(holiday_id, False)
        self.calendar.invalidate_cache()
        self.assertFalse(self.calendar.is_holiday(date(2026, 9, 1)))

    def test_duplicate_holiday_is_rejected(self):
        db.create_holiday("2026-09-02", "Tatil")
        _, error = db.create_holiday("2026-09-02", "Aynı gün tekrar")
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
