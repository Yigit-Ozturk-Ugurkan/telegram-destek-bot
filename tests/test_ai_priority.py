"""AI DESTEKLİ P1-P4 ÖNCELİK — kalıcı regression paketi (§B4).

AKIŞ:

    Gemini -> priority recommendation -> SERVER-SIDE VALIDATION -> ticket

Gemini priority için SOURCE OF TRUTH DEĞİLDİR. Bu paket doğrulama katmanını
(routing._validated_priority + db.apply_ai_priority) sınar.

GERÇEK GEMINI ÇAĞRISI YAPILMAZ: ai_assistant.classify_for_escalation'ın
DÖNDÜRDÜĞÜ sözlük elle kurulur; ağ katmanı hiç çalışmaz. Modelin gerçekten
çağrıldığı tek yer bot.start_escalation'dır ve o da bu pakette
ai_assistant._generate fail_on_network ile kilitlenerek korunur.
"""

from tests import _support

_support.setup_test_db("aipriority")

import unittest

import ai_assistant
import config
import db
import routing
from tests._support import DbTestCase, fail_on_network


class PriorityTestCase(DbTestCase):
    db = db
    config = config

    def setUp(self):
        super().setUp()
        # GÜVENLİK AĞI: bu pakette hiçbir yol gerçek Gemini'ye çıkmamalı.
        real = ai_assistant._generate
        ai_assistant._generate = fail_on_network
        self.addCleanup(setattr, ai_assistant, "_generate", real)

        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        self.general = self.make_skill(config.GENERAL_SUPPORT_SKILL_CODE,
                                       config.GENERAL_SUPPORT_SKILL_NAME)

    # ---------- Model çıktısı fixture'ları (AĞ YOK) ----------

    def model_says(self, priority, reason="etki analizi",
                   classification_type=config.CLASSIFICATION_SPECIFIC_SKILL,
                   skill="COMPUTER_VISION", category="Yazılım", ok=True):
        """ai_assistant.classify_for_escalation'ın DÖNDÜRECEĞİ sözlük."""
        return {"ok": ok, "classification_type": classification_type,
                "category": category, "required_skill": skill,
                "suggested_priority": priority, "priority_reason": reason}

    def decide(self, classification):
        return routing.resolve_classification(classification)


# ==================================================================
# B1 — PRIORITY SEMANTICS: doğru sınıflandırma doğru önceliğe düşer
# ==================================================================

class TestPrioritySemantics(PriorityTestCase):
    """Model doğru okuduğunda server o okumayı KORUR."""

    def test_1_standard_problem_stays_p3(self):
        decision = self.decide(self.model_says(
            "P3", "Tek kullanıcı, sınırlı etki, iş tamamen durmuş değil."))
        self.assertEqual(decision["priority"], config.PRIORITY_P3)

    def test_2_information_request_is_p4(self):
        decision = self.decide(self.model_says(
            "P4", "Bilgi talebi; kullanım sorusu, acil değil.",
            classification_type=config.CLASSIFICATION_GENERAL_SUPPORT,
            skill=config.GENERAL_SUPPORT_SKILL_CODE))
        self.assertEqual(decision["priority"], config.PRIORITY_P4)
        self.assertEqual(decision["routing_basis"], routing.BASIS_GENERAL_SUPPORT)

    def test_3_serious_single_user_impact_is_p2(self):
        decision = self.decide(self.model_says(
            "P2", "Kullanıcı işini yapamıyor, geçici çözüm yok."))
        self.assertEqual(decision["priority"], config.PRIORITY_P2)

    def test_4_company_wide_outage_is_p1(self):
        decision = self.decide(self.model_says(
            "P1", "Şirket çapında servis kesintisi, production durdu."))
        self.assertEqual(decision["priority"], config.PRIORITY_P1)

    def test_priority_is_independent_from_routing_basis(self):
        """Uzmanlık ile aciliyet AYRI eksenlerdir."""
        decision = self.decide(self.model_says("P1", skill="COMPUTER_VISION"))
        self.assertEqual(decision["routing_basis"], routing.BASIS_SPECIFIC_SKILL)
        self.assertEqual(decision["skill_code"], "COMPUTER_VISION")
        self.assertEqual(decision["priority"], config.PRIORITY_P1)

    def test_prompt_carries_the_impact_rules_and_the_urgency_warning(self):
        """§B2: 'ACİL yazmak tek başına P1 sebebi değildir' KURALI prompt'tadır."""
        rules = ai_assistant._CLASSIFY_RULES
        self.assertIn("{priority_semantics}", rules)
        self.assertIn("suggested_priority", rules)
        self.assertIn("priority_reason", rules)
        self.assertIn("TEK BAŞINA P1", rules)
        self.assertIn("ETKİ (impact)", rules)

    def test_priority_semantics_come_from_a_single_definition(self):
        text = ai_assistant._priority_semantics_text()
        for code in config.PRIORITIES:
            self.assertIn(code, text)
        self.assertIn("şirket çapında", text.lower())


# ==================================================================
# B2 — SERVER VALIDATION: yalnızca P1-P4, aksi hâlde P3
# ==================================================================

class TestServerSideValidation(PriorityTestCase):

    def test_only_p1_to_p4_are_accepted(self):
        for code in config.PRIORITIES:
            with self.subTest(code=code):
                self.assertEqual(routing._validated_priority(code), code)

    def test_case_and_whitespace_are_normalised_not_invented(self):
        self.assertEqual(routing._validated_priority(" p1 "), config.PRIORITY_P1)
        self.assertEqual(routing._validated_priority("p4"), config.PRIORITY_P4)

    def test_6_invalid_model_priority_falls_back_to_p3(self):
        for bad in ("P0", "P5", "URGENT", "yüksek", "critical", "", "   ",
                    "P1;DROP TABLE tickets"):
            with self.subTest(bad=bad):
                self.assertEqual(routing._validated_priority(bad),
                                 config.DEFAULT_PRIORITY)

    def test_null_and_wrong_types_fall_back_to_p3(self):
        for bad in (None, 1, 5, True, {}, [], object()):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(routing._validated_priority(bad),
                                 config.DEFAULT_PRIORITY)

    def test_missing_priority_field_falls_back_to_p3(self):
        decision = self.decide({"ok": True,
                                "classification_type": config.CLASSIFICATION_SPECIFIC_SKILL,
                                "category": "Yazılım",
                                "required_skill": "COMPUTER_VISION"})
        self.assertEqual(decision["priority"], config.DEFAULT_PRIORITY)

    def test_7_failed_classification_falls_back_to_p3(self):
        """Gemini kapalı / timeout / bozuk JSON -> ok=False."""
        decision = self.decide(self.model_says("P1", ok=False))

        self.assertEqual(decision["priority"], config.DEFAULT_PRIORITY)
        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertIsNone(decision["priority_reason"],
                          "Başarısız sınıflandırmanın gerekçesine güvenilmez.")

    def test_broken_json_never_reaches_the_priority_field(self):
        """ai_assistant bozuk JSON'da priority alanlarını BOŞ döndürür."""
        ai_assistant._generate = lambda prompt: "bu JSON değil {{"
        result = ai_assistant.classify_for_escalation("konu", [], [])

        self.assertFalse(result["ok"])
        self.assertIsNone(result["suggested_priority"])
        self.assertEqual(self.decide(result)["priority"], config.DEFAULT_PRIORITY)

    def test_model_output_is_parsed_but_not_trusted(self):
        """ai_assistant HAM değeri döndürür; doğrulama routing'dedir."""
        ai_assistant._generate = lambda prompt: (
            '{"classification_type": "specific_skill", "category": "Yazılım",'
            ' "required_skill": "COMPUTER_VISION",'
            ' "suggested_priority": "P0", "priority_reason": "uydurma"}')
        result = ai_assistant.classify_for_escalation("konu", [], [])

        self.assertEqual(result["suggested_priority"], "P0")   # ham çıktı
        self.assertEqual(self.decide(result)["priority"],
                         config.DEFAULT_PRIORITY)               # doğrulanmış karar

    def test_5_saying_urgent_does_not_by_itself_create_p1(self):
        """Kullanıcı 'ACİL!!!' yazdı ama etki sınırlı -> model P3 der, server korur.

        Server tarafı kullanıcı metnini OKUMAZ; 'ACİL' kelimesini P1'e çeviren
        HİÇBİR kural yoktur. Bu testin görevi böyle bir kestirme yolun
        eklenmediğini kanıtlamaktır."""
        ticket_id = self.make_ticket(subject="ACİL!!! ÇOK ACİL!!! KRİTİK!!!")
        decision = self.decide(self.model_says(
            "P3", "Tek kullanıcı, yazıcı sorunu; etki sınırlı."))

        applied, priority = db.apply_ai_priority(
            ticket_id, decision["priority"], decision.get("priority_reason"))

        self.assertTrue(applied)
        self.assertEqual(priority, config.PRIORITY_P3)
        self.assertEqual(db.get_ticket(ticket_id)["priority"], config.PRIORITY_P3)

    def test_server_side_priority_never_reads_user_text(self):
        """'ACİL -> P1' kestirmesi KODDA YOKTUR.

        Doğrulayıcıya kullanıcı cümlesi verildiğinde bile sonuç P3'tür; yani
        öncelik kararı metin eşlemesiyle DEĞİL, modelin ürettiği ve server'da
        doğrulanan koda göre verilir."""
        for text in ("ACİL", "ÇOK ACİL!!!", "KRİTİK", "URGENT", "acil p1",
                     "Sunucu çöktü ACİL"):
            with self.subTest(text=text):
                self.assertEqual(routing._validated_priority(text),
                                 config.DEFAULT_PRIORITY)

    def test_no_keyword_to_priority_mapping_exists_in_the_server_modules(self):
        """Kaynak taraması: P1 sabitini bir kullanıcı kelimesiyle eşleyen
        ÇALIŞTIRILABİLİR satır bulunmamalı (yorumda anlatmak serbesttir)."""
        import inspect
        keywords = ("ACİL", "ACIL", "URGENT", "KRİTİK", "KRITIK")
        for module in (routing, db):
            for line in inspect.getsource(module).splitlines():
                code = line.split("#", 1)[0]
                if "PRIORITY_P1" not in code:
                    continue
                upper = code.upper()
                for word in keywords:
                    self.assertNotIn(
                        word, upper,
                        f"{module.__name__}: öncelik kullanıcı kelimesine "
                        f"bağlanmış görünüyor -> {line.strip()}")

    def test_defensive_validation_inside_apply_ai_priority(self):
        """Çağıran taraf hata yapsa bile uydurma öncelik DB'ye yazılamaz."""
        ticket_id = self.make_ticket()
        applied, priority = db.apply_ai_priority(ticket_id, "P9", "bozuk")

        self.assertEqual(priority, config.DEFAULT_PRIORITY)
        self.assertEqual(db.get_ticket(ticket_id)["priority"], config.DEFAULT_PRIORITY)

    def test_reason_is_trimmed_and_length_capped(self):
        long_reason = "x" * (config.PRIORITY_REASON_MAX_LENGTH + 500)
        decision = self.decide(self.model_says("P2", long_reason))

        self.assertEqual(len(decision["priority_reason"]),
                         config.PRIORITY_REASON_MAX_LENGTH)

    def test_reason_whitespace_is_collapsed(self):
        decision = self.decide(self.model_says("P2", "  çok\n\n  boşluklu   metin "))
        self.assertEqual(decision["priority_reason"], "çok boşluklu metin")

    def test_empty_reason_becomes_none(self):
        self.assertIsNone(routing._validated_priority_reason("   "))
        self.assertIsNone(routing._validated_priority_reason(None))
        self.assertIsNone(routing._validated_priority_reason(42))


# ==================================================================
# B3 — ADMIN OVERRIDE KORUNUR
# ==================================================================

class TestAdminOverride(PriorityTestCase):

    def test_ai_priority_is_applied_when_nobody_decided_yet(self):
        ticket_id = self.make_ticket()
        self.assertEqual(db.priority_source(db.get_ticket(ticket_id)),
                         config.PRIORITY_SOURCE_DEFAULT)

        applied, priority = db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "outage")

        self.assertTrue(applied)
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["priority"], config.PRIORITY_P1)
        self.assertEqual(db.priority_source(ticket), config.PRIORITY_SOURCE_AI)

    def test_manual_change_marks_the_ticket_as_human_decided(self):
        ticket_id = self.make_ticket()
        ok, _ = db.set_ticket_priority(ticket_id, config.PRIORITY_P2)

        self.assertTrue(ok)
        self.assertEqual(db.priority_source(db.get_ticket(ticket_id)),
                         config.PRIORITY_SOURCE_MANUAL)

    def test_8_ai_never_overwrites_an_admin_decision(self):
        ticket_id = self.make_ticket()
        db.set_ticket_priority(ticket_id, config.PRIORITY_P3)   # admin kararı

        applied, priority = db.apply_ai_priority(ticket_id, config.PRIORITY_P1,
                                                 "AI kritik diyor")

        self.assertFalse(applied, "AI admin kararını EZMEMELİ.")
        self.assertEqual(priority, config.PRIORITY_P3)
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["priority"], config.PRIORITY_P3)
        self.assertEqual(db.priority_source(ticket), config.PRIORITY_SOURCE_MANUAL)

    def test_repeated_ai_attempts_still_cannot_overwrite(self):
        ticket_id = self.make_ticket()
        db.set_ticket_priority(ticket_id, config.PRIORITY_P4)

        for _ in range(3):
            db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "yine dener")

        self.assertEqual(db.get_ticket(ticket_id)["priority"], config.PRIORITY_P4)

    def test_admin_can_still_change_priority_after_ai_applied_it(self):
        """Kilit tek yönlüdür: insan AI'ı ezer, AI insanı ezemez."""
        ticket_id = self.make_ticket()
        db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "outage")

        ok, _ = db.set_ticket_priority(ticket_id, config.PRIORITY_P3)

        self.assertTrue(ok)
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["priority"], config.PRIORITY_P3)
        self.assertEqual(db.priority_source(ticket), config.PRIORITY_SOURCE_MANUAL)

    def test_ai_suggestion_is_recorded_even_when_not_applied(self):
        """Denetim izi: 'AI P1 önerdi, admin P3'te bıraktı' görünebilmeli."""
        ticket_id = self.make_ticket()
        db.set_ticket_priority(ticket_id, config.PRIORITY_P3)
        db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "şirket çapında kesinti")

        advice = db.get_ai_priority_advice(ticket_id)
        self.assertEqual(advice["suggested_priority"], config.PRIORITY_P1)
        self.assertEqual(advice["reason"], "şirket çapında kesinti")
        self.assertFalse(advice["applied"])
        self.assertTrue(advice["overridden"])

    def test_legacy_rows_with_null_source_are_treated_as_default(self):
        """Revizyondan ÖNCEKİ kayıtlar: geriye dönük veri yazımı YAPILMAZ."""
        ticket_id = self.make_ticket()
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET priority_source = NULL WHERE id = ?",
                         (ticket_id,))

        self.assertEqual(db.priority_source(db.get_ticket(ticket_id)),
                         config.PRIORITY_SOURCE_DEFAULT)
        applied, _ = db.apply_ai_priority(ticket_id, config.PRIORITY_P2, "x")
        self.assertTrue(applied)

    def test_advice_is_none_when_the_model_never_suggested_anything(self):
        ticket_id = self.make_ticket()
        self.assertIsNone(db.get_ai_priority_advice(ticket_id))


# ==================================================================
# B4/9 — MEVCUT SLA CYCLE DAVRANIŞI KORUNUR
# ==================================================================

class TestSlaBehaviourPreserved(PriorityTestCase):

    def escalated_ticket(self):
        ticket_id = self.make_ticket(skill_code="COMPUTER_VISION",
                                     category="Yazılım")
        db.mark_escalation_pending(ticket_id, department="Yazılım")
        return ticket_id

    def test_9_priority_change_does_not_move_the_sla_start(self):
        ticket_id = self.escalated_ticket()
        started_at = db.get_ticket(ticket_id)["sla_started_at"]
        self.assertIsNotNone(started_at)

        db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "kritik")

        self.assertEqual(db.get_ticket(ticket_id)["sla_started_at"], started_at,
                         "Önceliği yükseltmek gecikmeyi GİZLEMEZ.")

    def test_priority_change_recalculates_the_deadlines(self):
        ticket_id = self.escalated_ticket()
        before = db.get_ticket(ticket_id)["first_response_due_at"]

        db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "kritik")

        after = db.get_ticket(ticket_id)["first_response_due_at"]
        self.assertNotEqual(before, after, "Son tarihler yeniden hesaplanmalı.")

    def test_ai_priority_updates_the_active_cycle_only(self):
        ticket_id = self.escalated_ticket()
        db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "kritik")

        cycles = db.list_sla_cycles(ticket_id)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["priority"], config.PRIORITY_P1)

    def test_closed_cycles_stay_immutable(self):
        ticket_id = self.escalated_ticket()
        db.resolve_ticket_if_open(ticket_id, source=config.ASSIGN_SOURCE_SYSTEM)
        closed = db.list_sla_cycles(ticket_id)[0]
        self.assertIsNotNone(closed["closed_at"])

        db.reopen_ticket(ticket_id)
        db.apply_ai_priority(ticket_id, config.PRIORITY_P1, "kritik")

        # list_sla_cycles EN YENİ ÖNCE döner.
        cycles = db.list_sla_cycles(ticket_id)
        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0]["priority"], config.PRIORITY_P1,
                         "Aktif cycle yeni önceliği almalı.")
        self.assertEqual(cycles[-1]["priority"], closed["priority"],
                         "Kapalı cycle IMMUTABLE olmalı.")
        self.assertIsNotNone(cycles[-1]["closed_at"])

    def test_10_no_real_gemini_call_is_made_in_this_package(self):
        """Ağ kilidi gerçekten çalışıyor mu? (meta-test)"""
        with self.assertRaises(AssertionError):
            ai_assistant._generate("herhangi bir prompt")


# ==================================================================
# Bot eskalasyon akışı — uçtan uca (AĞ YOK)
# ==================================================================

class TestEscalationAppliesPriority(PriorityTestCase):
    """bot.start_escalation'ın DB tarafındaki sözleşmesi.

    Asenkron bot handler'ını çalıştırmak yerine aynı çağrı zincirini
    kuruyoruz: resolve_classification -> set_ticket_routing ->
    apply_ai_priority -> start_sla. Böylece Telegram/asyncio bağımlılığı
    olmadan sıralamanın doğruluğu kanıtlanır."""

    def escalate(self, classification, ticket_id):
        decision = routing.resolve_classification(
            classification, fallback_department="Yazılım")
        db.set_ticket_routing(ticket_id, decision["routing_basis"],
                              skill_code=decision["skill_code"],
                              department=decision["department"])
        db.set_ai_mode(ticket_id, config.AI_MODE_HUMAN)
        db.mark_escalated(ticket_id, pending_department=decision["department"])
        db.apply_ai_priority(ticket_id, decision["priority"],
                             reason=decision.get("priority_reason"))
        db.start_sla(ticket_id)
        return decision

    def test_new_ticket_is_no_longer_stuck_at_default_p3(self):
        ticket_id = self.make_ticket(subject="Tüm ofis internetsiz")
        self.assertEqual(db.get_ticket(ticket_id)["priority"], config.DEFAULT_PRIORITY)

        self.escalate(self.model_says("P1", "Şirket çapında kesinti."), ticket_id)

        self.assertEqual(db.get_ticket(ticket_id)["priority"], config.PRIORITY_P1)

    def test_priority_is_written_before_the_sla_clock_starts(self):
        """Son tarihler doğrudan doğru pencereyle hesaplansın."""
        ticket_id = self.make_ticket()
        self.escalate(self.model_says("P1", "kritik"), ticket_id)

        cycle = db.get_active_cycle(ticket_id)
        self.assertEqual(cycle["priority"], config.PRIORITY_P1)

    def test_failed_classification_leaves_p3_and_manual_triage(self):
        ticket_id = self.make_ticket()
        decision = self.escalate(self.model_says("P1", ok=False), ticket_id)

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["priority"], config.DEFAULT_PRIORITY)
        self.assertIsNone(ticket["ai_priority_reason"])

    def test_routing_decision_and_priority_are_independent(self):
        ticket_id = self.make_ticket()
        decision = self.escalate(
            self.model_says("P4", "kullanım sorusu", skill="COMPUTER_VISION"),
            ticket_id)

        self.assertEqual(decision["routing_basis"], routing.BASIS_SPECIFIC_SKILL)
        self.assertEqual(db.get_ticket(ticket_id)["priority"], config.PRIORITY_P4)
        self.assertEqual(db.get_ticket(ticket_id)["required_skill_code"],
                         "COMPUTER_VISION")

    def test_bot_source_wires_apply_ai_priority_into_escalation(self):
        """bot.py gerçekten bu yolu çağırıyor mu? (kaynak taraması)"""
        from tests import project_file
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("db.apply_ai_priority(ticket_id, decision[\"priority\"]", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
