"""ROUTING PAKETİ — otomatik teknisyen seçimi (ACCEPTANCE REVISION §32).

Test edilen davranış:

    BEST SKILL PRIORITY
      -> AVAILABLE
      -> LOWEST ACTIVE PRIMARY WORKLOAD
      -> LEAST RECENTLY ASSIGNED
      -> TECHNICIAN ID

DEPARTMAN FALLBACK (ACCEPTANCE FIX 2) — skill üretilemediğinde:

    AVAILABLE
      -> LOWEST ACTIVE PRIMARY WORKLOAD
      -> LEAST RECENTLY ASSIGNED
      -> TECHNICIAN ID

Son kullanıcıya teknik personel SEÇTİRİLMEZ; seçim sistemindir.
Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ.
"""

from tests import _support, project_file

_support.setup_test_db("routing")

import unittest

import config
import db
import routing
from tests._support import DbTestCase


class RoutingTestCase(DbTestCase):
    db = db
    config = config


# ==================================================================
# 10-12, 17-20: aday seçimi ve uzmanlık önceliği
# ==================================================================

class TestSkillPriorityFirst(RoutingTestCase):
    """Uzmanlık önceliği HER ZAMAN ilk kriterdir (Faz 3A kuralı korunur)."""

    def test_10_single_candidate_is_selected_automatically(self):
        cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertIsNotNone(result["selected"])
        self.assertEqual(result["selected"]["technician_id"], hakan)
        self.assertEqual(len(result["candidates"]), 1)

    def test_11_result_contains_no_user_choice_payload(self):
        """Sonuç sözlüğü kullanıcıya sunulacak bir 'seçenek listesi' üretmez.

        Aday listesi iç operasyon verisidir; 'selected' tek karardır."""
        cv = self.make_skill("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", skills=((cv, 1),))
        self.make_technician("Kaan Polat", skills=((cv, 1),))

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertIn("selected", result)
        self.assertIsNotNone(result["selected"])
        # Aday sayısı ne olursa olsun TEK bir karar üretilir.
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["selected"], result["candidates"][0])

    def test_12_multiple_candidates_resolved_without_user_input(self):
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        self.make_technician("Kaan Polat", skills=((cv, 1),))
        self.make_technician("Derya Uçar", skills=((cv, 1),))

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        # Hepsi eşit: en küçük id deterministik olarak kazanır.
        self.assertEqual(result["selected"]["technician_id"], hakan)

    def test_17_priority_1_excludes_priority_2(self):
        """Priority 1 aday varken priority 2 HİÇ değerlendirilmez —
        priority 1 kişi çok daha yüklü olsa bile."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 2),))
        self.set_active_workload(hakan, 5)      # Hakan çok yüklü
        # Kaan boş ama priority 2 -> yine de seçilmez.

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["matched_priority"], 1)
        self.assertEqual(result["selected"]["technician_id"], hakan)
        self.assertNotIn(kaan, [c["technician_id"] for c in result["candidates"]])

    def test_18_priority_2_fallback_when_no_priority_1(self):
        cv = self.make_skill("COMPUTER_VISION")
        kaan = self.make_technician("Kaan Polat", skills=((cv, 2),))
        self.make_technician("Derya Uçar", skills=((cv, 3),))

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["matched_priority"], 2)
        self.assertEqual(result["selected"]["technician_id"], kaan)

    def test_19_priority_3_fallback_when_no_1_or_2(self):
        cv = self.make_skill("COMPUTER_VISION")
        derya = self.make_technician("Derya Uçar", skills=((cv, 3),))

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["matched_priority"], 3)
        self.assertEqual(result["selected"]["technician_id"], derya)

    def test_20_unavailable_technician_is_never_selected(self):
        cv = self.make_skill("COMPUTER_VISION")
        self.make_technician("Hakan Bulut", available=False, skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 2),))

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        # available=0 kişi priority 1 olsa bile havuza girmez; priority 2'ye düşülür.
        self.assertEqual(result["selected"]["technician_id"], kaan)
        self.assertEqual(result["matched_priority"], 2)

    def test_20b_no_candidate_returns_none_without_error(self):
        self.make_skill("COMPUTER_VISION")
        result = routing.select_best_technician_for_skill("COMPUTER_VISION")
        self.assertIsNone(result["selected"])
        self.assertEqual(result["candidates"], [])

    def test_20c_unknown_skill_returns_empty_result(self):
        result = routing.select_best_technician_for_skill("YOK_BOYLE_BIR_SKILL")
        self.assertIsNone(result["selected"])
        self.assertIsNone(result["skill"])


# ==================================================================
# 13, 23: iş yükü, last-assignment'tan ÖNCE gelir
# ==================================================================

class TestWorkloadBeatsRecency(RoutingTestCase):

    def test_13_lowest_active_ticket_count_wins(self):
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 1),))
        self.set_active_workload(hakan, 3)
        self.set_active_workload(kaan, 1)

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["selected"]["technician_id"], kaan)
        self.assertEqual(result["selected"]["active_ticket_count"], 1)

    def test_23_workload_outranks_last_assignment(self):
        """Spec §23: Hakan active=3 + daha ESKİ son atama,
        Kaan active=1 + daha YENİ son atama -> Kaan kazanır."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 1),))

        self.set_active_workload(hakan, 3)
        self.set_last_primary_at(hakan, "2026-08-29 09:00:00")   # daha eski
        self.set_active_workload(kaan, 1)
        self.set_last_primary_at(kaan, "2026-08-29 11:00:00")    # daha yeni

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["selected"]["technician_id"], kaan,
                         "Iş yükü, son-atama kriterinden ÖNCE gelmelidir.")

    def test_24_skill_priority_outranks_workload(self):
        """Spec §24: priority 1 + active=5 kişi, priority 2 + active=0 kişiyi yener."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        self.make_technician("Kaan Polat", skills=((cv, 2),))
        self.set_active_workload(hakan, 5)

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["selected"]["technician_id"], hakan)


# ==================================================================
# 14-16, 22: fairness — least recently assigned
# ==================================================================

class TestLeastRecentlyAssignedFairness(RoutingTestCase):

    def test_14_equal_workload_least_recently_assigned_wins(self):
        """Spec §22: ikisi de active=0. Hakan 10:00, Kaan 10:20 -> Hakan."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 1),))

        self.give_primary_history(hakan, "2026-08-29 10:00:00")
        self.give_primary_history(kaan, "2026-08-29 10:20:00")

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["selected"]["technician_id"], hakan)

    def test_15_never_assigned_technician_goes_first(self):
        """Hiç PRIMARY almamış kişi 'en uzun süredir almamış' sayılır."""
        cv = self.make_skill("COMPUTER_VISION")
        veteran = self.make_technician("A Veteran", skills=((cv, 1),))
        rookie = self.make_technician("Z Rookie", skills=((cv, 1),))
        # veteran'ın id'si daha küçük; sadece id tie-break olsaydı o kazanırdı.
        self.give_primary_history(veteran, "2026-08-29 10:00:00")

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["selected"]["technician_id"], rookie)
        self.assertIsNone(result["selected"]["last_primary_assignment_at"])

    def test_16_full_tie_falls_back_to_technician_id(self):
        cv = self.make_skill("COMPUTER_VISION")
        first = self.make_technician("Z Sonuncu", skills=((cv, 1),))
        second = self.make_technician("A Birinci", skills=((cv, 1),))
        # İsim sırası tersine; belirleyici olan technician_id olmalı.

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["selected"]["technician_id"], min(first, second))

    def test_16b_selection_is_deterministic_across_calls(self):
        cv = self.make_skill("COMPUTER_VISION")
        for i in range(5):
            self.make_technician(f"Tekniker {i}", skills=((cv, 1),))
        picks = {routing.select_best_technician_for_skill(
            "COMPUTER_VISION")["selected"]["technician_id"] for _ in range(10)}
        self.assertEqual(len(picks), 1, "Seçim deterministik olmalı.")

    def test_22_fair_rotation_over_two_assignments(self):
        """Spec §22: sıra Hakan -> Kaan olarak dönmelidir."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 1),))

        self.give_primary_history(hakan, "2026-08-29 10:00:00")
        self.give_primary_history(kaan, "2026-08-29 10:20:00")

        # --- 1. yeni CV talebi -> Hakan ---
        first = routing.select_best_technician_for_skill("COMPUTER_VISION")
        self.assertEqual(first["selected"]["technician_id"], hakan)

        ticket_a = self.make_ticket(subject="CV #1", skill_code="COMPUTER_VISION")
        self.assertTrue(db.assign_primary_if_unassigned(ticket_a, hakan, "Hakan Bulut"))
        # Hakan artık 10:30'da iş almış olsun; iş yükü eşitlensin diye kapatıyoruz.
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET status = ? WHERE id = ?",
                         (config.STATUS_CLOSED, ticket_a))
            conn.execute("UPDATE ticket_assignments SET assigned_at = ? WHERE ticket_id = ?",
                         ("2026-08-29 10:30:00", ticket_a))

        # --- 2. yeni CV talebi -> sıra Kaan'da (10:20 < 10:30) ---
        second = routing.select_best_technician_for_skill("COMPUTER_VISION")
        self.assertEqual(second["selected"]["technician_id"], kaan,
                         "Eşit iş yükünde sıra dönmeli (fair round-robin).")


# ==================================================================
# 21, 25-26: semantik korumaları
# ==================================================================

class TestSemanticsPreserved(RoutingTestCase):

    def test_21_assignment_does_not_mutate_available(self):
        """Spec §25: PRIMARY ataması available'ı 0 YAPMAZ.

        available = 'yeni iş almayı kabul ediyor'
        active_ticket_count = 'kaç aktif PRIMARY işi var'  -> AYRI kavramlar."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        ticket_id = self.make_ticket(skill_code="COMPUTER_VISION")

        self.assertTrue(db.assign_primary_if_unassigned(ticket_id, hakan, "Hakan Bulut"))

        self.assertEqual(db.get_technician(hakan)["available"], 1)
        self.assertEqual(db.count_active_tickets_for_technician(hakan), 1)

    def test_26_collaborator_work_does_not_enter_primary_workload(self):
        """Spec §26: COLLABORATOR atamaları routing iş yükünü BOZMAZ."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 1),))

        # Hakan'a 3 yardımcılık ver -> routing'i etkilememeli.
        for i in range(3):
            ticket_id = self.make_ticket(subject=f"Baskasinin isi {i}")
            db.assign_primary_if_unassigned(ticket_id, kaan, "Kaan Polat")
            ok, message = db.add_collaborator(ticket_id, hakan)
            self.assertTrue(ok, message)

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        # Kaan'ın 3 PRIMARY işi var, Hakan'ın 0 -> Hakan seçilmeli.
        self.assertEqual(result["selected"]["technician_id"], hakan)
        self.assertEqual(result["selected"]["active_ticket_count"], 0)
        self.assertEqual(db.count_collaborations(hakan), 3)

    def test_26b_collaborator_assignment_not_counted_as_last_primary(self):
        """COLLABORATOR ataması 'yeni ana iş aldı' SAYILMAZ."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", skills=((cv, 1),))

        ticket_id = self.make_ticket()
        db.assign_primary_if_unassigned(ticket_id, kaan, "Kaan Polat")
        db.add_collaborator(ticket_id, hakan)

        last_map = db.last_primary_assignment_map()
        self.assertIn(kaan, last_map)
        self.assertNotIn(hakan, last_map,
                         "Yardımcılık son-PRIMARY hesabına GİRMEMELİ.")


# ==================================================================
# 30: performans — sabit sorgu sayısı, N+1 yok
# ==================================================================

class TestNoNPlusOne(RoutingTestCase):

    def _count_queries(self, fn):
        """sqlite3 trace callback'i ile çalıştırılan SELECT sayısını ölçer."""
        import sqlite3
        statements = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        sqlite3.connect = traced_connect
        try:
            fn()
        finally:
            sqlite3.connect = original_connect
        return [s for s in statements if s.strip().upper().startswith("SELECT")]

    def test_30_routing_query_count_is_constant(self):
        cv = self.make_skill("COMPUTER_VISION")
        for i in range(3):
            self.make_technician(f"Tekniker {i}", skills=((cv, 1),))
        small = len(self._count_queries(
            lambda: routing.select_best_technician_for_skill("COMPUTER_VISION")))

        for i in range(3, 20):
            self.make_technician(f"Tekniker {i}", skills=((cv, 1),))
        large = len(self._count_queries(
            lambda: routing.select_best_technician_for_skill("COMPUTER_VISION")))

        self.assertEqual(small, large,
                         "Aday sayısı arttıkça sorgu sayısı ARTMAMALI (N+1 yok).")

    def test_30b_team_overview_query_count_is_constant(self):
        cv = self.make_skill("COMPUTER_VISION")
        for i in range(3):
            self.make_technician(f"Tekniker {i}", skills=((cv, 1),))
        small = len(self._count_queries(db.list_team_overview))

        for i in range(3, 20):
            self.make_technician(f"Tekniker {i}", skills=((cv, 1),))
        large = len(self._count_queries(db.list_team_overview))

        self.assertEqual(small, large,
                         "Aktif ekip paneli kişi başına sorgu ÇALIŞTIRMAMALI.")


# ==================================================================
# ACCEPTANCE FIX 2 §6-B/C/D: DEPARTMAN FALLBACK ADALETİ
# ==================================================================

class TestDepartmentFallbackFairness(RoutingTestCase):
    """Skill sınıflandırması BAŞARISIZ olduğunda departman fallback'i.

    Eski davranış db.find_available_technician() ile 'departmandaki İLK
    müsait kişi' idi ve iş yükünü görmüyordu. Artık skill yolundaki adalet
    kuralları burada da geçerli."""

    def test_B_lowest_workload_wins_in_department(self):
        """Spec §6-B: Hakan active=4, Kaan active=0 -> Kaan."""
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        kaan = self.make_technician("Kaan Polat", department="Yazılım")
        self.set_active_workload(hakan, 4)

        result = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(result["selected"]["technician_id"], kaan)
        self.assertEqual(result["mode"], routing.MODE_DEPARTMENT)

    def test_B2_legacy_first_available_would_have_picked_the_busy_one(self):
        """Eski yolun GERÇEKTEN farklı seçtiğini gösterir (regresyon kanıtı).

        find_available_technician id sırasıyla ilk müsait kişiyi verir; Hakan
        4 aktif işiyle ilk sırada. Yeni motor onu SEÇMEMELİDİR."""
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        kaan = self.make_technician("Kaan Polat", department="Yazılım")
        self.set_active_workload(hakan, 4)

        legacy = db.find_available_technician("Yazılım")
        modern = routing.select_best_technician_for_department("Yazılım")["selected"]

        self.assertEqual(legacy["id"], hakan, "Eski yol yüklü kişiyi seçerdi.")
        self.assertEqual(modern["technician_id"], kaan, "Yeni yol adil seçmeli.")
        self.assertNotEqual(legacy["id"], modern["technician_id"])

    def test_C_equal_workload_least_recently_assigned_wins(self):
        """Spec §6-C: Hakan 10:00, Kaan 10:20, iş yükü eşit -> Hakan."""
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        kaan = self.make_technician("Kaan Polat", department="Yazılım")
        self.give_primary_history(hakan, "2026-08-29 10:00:00")
        self.give_primary_history(kaan, "2026-08-29 10:20:00")

        result = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(result["selected"]["technician_id"], hakan)

    def test_D_never_assigned_goes_first(self):
        """Spec §6-D: hiç PRIMARY almamış kişi önce seçilir."""
        veteran = self.make_technician("A Veteran", department="Yazılım")
        rookie = self.make_technician("Z Rookie", department="Yazılım")
        self.give_primary_history(veteran, "2026-08-29 10:00:00")

        result = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(result["selected"]["technician_id"], rookie)
        self.assertIsNone(result["selected"]["last_primary_assignment_at"])

    def test_workload_outranks_recency_in_department_too(self):
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        kaan = self.make_technician("Kaan Polat", department="Yazılım")
        self.set_active_workload(hakan, 3)
        self.set_last_primary_at(hakan, "2026-08-29 09:00:00")   # daha eski
        self.set_active_workload(kaan, 1)
        self.set_last_primary_at(kaan, "2026-08-29 11:00:00")    # daha yeni

        result = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(result["selected"]["technician_id"], kaan)

    def test_unavailable_technician_is_excluded(self):
        self.make_technician("Hakan Bulut", department="Yazılım", available=False)
        kaan = self.make_technician("Kaan Polat", department="Yazılım")

        result = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(result["selected"]["technician_id"], kaan)

    def test_other_department_is_excluded(self):
        self.make_technician("Derya Uçar", department="Ağ")
        result = routing.select_best_technician_for_department("Yazılım")
        self.assertIsNone(result["selected"])
        self.assertEqual(result["candidates"], [])

    def test_full_tie_falls_back_to_technician_id(self):
        first = self.make_technician("Z Sonuncu", department="Yazılım")
        second = self.make_technician("A Birinci", department="Yazılım")

        result = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(result["selected"]["technician_id"], min(first, second))

    def test_empty_department_returns_no_selection(self):
        for department in (None, ""):
            with self.subTest(department=department):
                result = routing.select_best_technician_for_department(department)
                self.assertIsNone(result["selected"])

    def test_department_sql_order_matches_python_key(self):
        a = self.make_technician("A", department="Yazılım")
        b = self.make_technician("B", department="Yazılım")
        c = self.make_technician("C", department="Yazılım")
        self.set_active_workload(b, 2)
        self.give_primary_history(a, "2026-08-29 12:00:00")
        self.give_primary_history(c, "2026-08-29 08:00:00")

        result = routing.find_candidates_for_department("Yazılım")
        sql_order = [x["technician_id"] for x in result["candidates"]]
        python_order = [x["technician_id"] for x in
                        sorted(result["candidates"], key=routing.fairness_key)]
        self.assertEqual(sql_order, python_order)

    def test_department_and_skill_share_the_same_fairness_rule(self):
        """İki yolun iş yükü + recency sıralaması BİREBİR aynı olmalı."""
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     skills=((cv, 1),))
        kaan = self.make_technician("Kaan Polat", department="Yazılım",
                                    skills=((cv, 1),))
        self.set_active_workload(hakan, 2)

        by_skill = routing.select_best_technician_for_skill("COMPUTER_VISION")
        by_department = routing.select_best_technician_for_department("Yazılım")

        self.assertEqual(by_skill["selected"]["technician_id"], kaan)
        self.assertEqual(by_department["selected"]["technician_id"], kaan)


# ==================================================================
# ACCEPTANCE FIX 2 §6-A: skill yolu DEĞİŞMEDİ
# ==================================================================

class TestSkillPathUnchangedByFix2(RoutingTestCase):

    def test_A_skill_priority_workload_recency_still_apply(self):
        """Spec §6-A: skill classification başarılıysa davranış aynı."""
        cv = self.make_skill("COMPUTER_VISION")
        p1_busy = self.make_technician("P1 Yuklu", skills=((cv, 1),))
        self.make_technician("P2 Bos", skills=((cv, 2),))
        self.set_active_workload(p1_busy, 5)

        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["mode"], routing.MODE_SKILL)
        self.assertEqual(result["matched_priority"], 1)
        self.assertEqual(result["selected"]["technician_id"], p1_busy)

    def test_select_for_ticket_uses_skill_when_available(self):
        cv = self.make_skill("COMPUTER_VISION")
        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     skills=((cv, 1),))
        self.make_technician("Kaan Polat", department="Yazılım")

        ticket = {"required_skill_code": "COMPUTER_VISION",
                  "category": "Yazılım", "pending_department": "Yazılım"}
        result = routing.select_for_ticket(ticket)

        self.assertEqual(result["mode"], routing.MODE_SKILL)
        self.assertEqual(result["selected"]["technician_id"], hakan,
                         "Skill varsa departman fallback'e DÜŞÜLMEMELİ.")

    def test_select_for_ticket_falls_back_to_department(self):
        self.make_technician("Hakan Bulut", department="Yazılım")

        ticket = {"required_skill_code": None, "category": "Yazılım",
                  "pending_department": "Yazılım"}
        result = routing.select_for_ticket(ticket)

        self.assertEqual(result["mode"], routing.MODE_DEPARTMENT)
        self.assertIsNotNone(result["selected"])

    def test_select_for_ticket_prefers_pending_department_over_category(self):
        yazilim = self.make_technician("Hakan Bulut", department="Yazılım")
        self.make_technician("Derya Uçar", department="Ağ")

        ticket = {"required_skill_code": "", "category": "Ağ",
                  "pending_department": "Yazılım"}
        result = routing.select_for_ticket(ticket)

        self.assertEqual(result["selected"]["technician_id"], yazilim)

    def test_select_for_ticket_returns_mode_none_without_skill_or_department(self):
        self.make_technician("Hakan Bulut", department="Yazılım")

        result = routing.select_for_ticket(
            {"required_skill_code": None, "category": None,
             "pending_department": None})

        self.assertEqual(result["mode"], routing.MODE_NONE)
        self.assertIsNone(result["selected"])

    def test_unknown_skill_does_not_silently_fall_back(self):
        """Katalogda olmayan skill -> skill yolu boş döner.

        Departman fallback'ine düşme kararı select_for_ticket'a AİT DEĞİLDİR;
        oraya ancak required_skill_code hiç yazılmamışsa girilir. Bot zaten
        geçersiz skill'i DB'ye yazmaz (bkz. start_escalation doğrulaması)."""
        self.make_technician("Hakan Bulut", department="Yazılım")

        result = routing.select_for_ticket(
            {"required_skill_code": "YOK_BOYLE", "category": "Yazılım"})

        self.assertEqual(result["mode"], routing.MODE_SKILL)
        self.assertIsNone(result["selected"])


# ==================================================================
# ACCEPTANCE FIX 2 §7: CALL-SITE AUDIT
# ==================================================================

class TestNoLegacyAssignmentPaths(RoutingTestCase):
    """'İlk müsait technician' yolunun production'da KALMADIĞINI doğrular.

    Metin araması YAPILMAZ — docstring ve yorumlarda helper adının geçmesi
    false-positive üretirdi (bu modüller o adı zaten 'artık kullanılmıyor'
    diye anıyor). Bunun yerine kaynak AST'ye çevrilip GERÇEK ÇAĞRI
    düğümleri (db.<helper>(...)) sayılır."""

    PRODUCTION_MODULES = ("bot.py", "webapp.py", "queue_service.py",
                          "queue_worker.py", "routing.py")

    #: Yeni otomatik atama akışında bulunmaması gereken çağrılar
    BANNED_DB_CALLS = (
        "find_available_technician",          # departmandaki İLK müsait kişi
        "get_pending_tickets_for_department",  # kuyruktan sıra gözetmeden atama
        "assign_technician_if_unassigned",     # Faz 5A legacy atama
        "assign_technician_to_ticket",         # Faz 5A legacy atama
        "assign_ticket",                       # Faz 1 legacy atama
    )

    def _db_calls(self, module_name):
        """Modüldeki bütün `db.<attr>(...)` çağrılarının adları."""
        import ast
        with open(project_file(module_name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=module_name)

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "db"):
                found.append(func.attr)
        return found

    def test_no_production_module_calls_legacy_assignment_helpers(self):
        for name in self.PRODUCTION_MODULES:
            calls = set(self._db_calls(name))
            for banned in self.BANNED_DB_CALLS:
                with self.subTest(module=name, call=banned):
                    self.assertNotIn(
                        banned, calls,
                        f"{name} hâlâ legacy atama yolunu ÇAĞIRIYOR: db.{banned}()")

    def test_audit_detector_actually_detects_a_call(self):
        """Meta-test: dedektör gerçekten çağrı bulabiliyor mu?

        Aksi hâlde yukarıdaki test sessizce hiçbir şey doğrulamıyor olurdu."""
        calls = self._db_calls("queue_service.py")
        self.assertIn("assign_primary_if_unassigned", calls)
        self.assertIn("get_technician", calls)

    def test_atomic_primary_assignment_is_the_only_assignment_call(self):
        """Otomatik atama yapan modüller YALNIZCA atomik helper'ı kullanır."""
        for name in ("bot.py", "webapp.py", "queue_service.py"):
            with self.subTest(module=name):
                calls = set(self._db_calls(name))
                assignment_calls = {c for c in calls if c.startswith("assign")}
                self.assertEqual(
                    assignment_calls, {"assign_primary_if_unassigned"},
                    f"{name} beklenmeyen atama helper'ı kullanıyor: "
                    f"{assignment_calls}")

    def test_legacy_helpers_still_exist_and_are_marked(self):
        """Silinmediler ama LEGACY olarak işaretli (spec §3)."""
        self.assertTrue(hasattr(db, "find_available_technician"))
        self.assertTrue(hasattr(db, "get_pending_tickets_for_department"))
        self.assertIn("LEGACY", db.find_available_technician.__doc__)
        self.assertIn("LEGACY", db.get_pending_tickets_for_department.__doc__)

    def test_legacy_helper_still_works_for_external_callers(self):
        """Deprecated ama BOZUK DEĞİL — dışarıdan çağıran bir script kırılmasın."""
        hakan = self.make_technician("Hakan Bulut", department="Yazılım")
        self.assertEqual(db.find_available_technician("Yazılım")["id"], hakan)
        self.assertIsNone(db.find_available_technician("Ağ"))

    def test_every_automatic_assignment_path_goes_through_routing(self):
        """Otomatik atama yapan her modül merkezi motoru kullanır."""
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            bot_source = fh.read()
        with open(project_file("webapp.py"), encoding="utf-8") as fh:
            webapp_source = fh.read()

        with open(project_file("queue_service.py"), encoding="utf-8") as fh:
            queue_source = fh.read()

        self.assertIn("import routing", bot_source)
        self.assertIn("routing.select_for_ticket", queue_source)
        # webapp otomatik atamayı queue_service üzerinden yapar.
        self.assertIn("queue_service.reevaluate_for_technician", webapp_source)


class TestSingleCentralEngine(RoutingTestCase):

    def test_queue_service_uses_central_selector(self):
        """Queue worker kendi fairness algoritmasını YAZMAZ (spec §20)."""
        import inspect
        import queue_service
        import queue_worker

        service_source = inspect.getsource(queue_service)
        worker_source = inspect.getsource(queue_worker)

        # ACCEPTANCE FIX 2: tek giriş noktası select_for_ticket()
        self.assertIn("routing.select_for_ticket", service_source)
        # Worker sıralama/seçim yapmaz; kararı servise bırakır.
        for banned in ("active_ticket_count", "sort(", "sorted("):
            self.assertNotIn(banned, worker_source,
                             f"queue_worker kendi seçim mantığını içermemeli: {banned}")

    def test_bot_uses_central_selector(self):
        """Bot da aynı merkezi motoru kullanır ve aday butonu üretmez."""
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("routing.select_best_technician_for_skill", source)
        self.assertIn("routing.select_best_technician_for_department", source)
        self.assertNotIn("def candidate_keyboard", source)
        self.assertNotIn("def single_candidate_keyboard", source)

    def test_selection_key_matches_sql_order(self):
        """Python sıralaması ile SQL ORDER BY aynı sonucu üretmelidir."""
        cv = self.make_skill("COMPUTER_VISION")
        a = self.make_technician("A", skills=((cv, 1),))
        b = self.make_technician("B", skills=((cv, 1),))
        c = self.make_technician("C", skills=((cv, 1),))
        self.set_active_workload(b, 2)
        self.give_primary_history(a, "2026-08-29 12:00:00")
        self.give_primary_history(c, "2026-08-29 08:00:00")

        result = routing.find_candidates_for_skill("COMPUTER_VISION")
        sql_order = [x["technician_id"] for x in result["candidates"]]
        python_order = [x["technician_id"] for x in
                        sorted(result["candidates"], key=routing.selection_key)]
        self.assertEqual(sql_order, python_order)

    def test_selection_key_is_fairness_key_plus_priority(self):
        """İki anahtar tek tanımı paylaşır; ikinci bir sıralama kuralı yok."""
        candidate = {"priority": 2, "active_ticket_count": 3,
                     "last_primary_assignment_at": "2026-08-29 10:00:00",
                     "technician_id": 7}
        self.assertEqual(routing.selection_key(candidate),
                         (2,) + routing.fairness_key(candidate))

    def test_department_query_count_is_constant(self):
        """Departman fallback de N+1 üretmemeli."""
        for i in range(3):
            self.make_technician(f"Tekniker {i}", department="Yazılım")
        small = len(TestNoNPlusOne._count_queries(
            self, lambda: routing.select_best_technician_for_department("Yazılım")))

        for i in range(3, 20):
            self.make_technician(f"Tekniker {i}", department="Yazılım")
        large = len(TestNoNPlusOne._count_queries(
            self, lambda: routing.select_best_technician_for_department("Yazılım")))

        self.assertEqual(small, large)


if __name__ == "__main__":
    unittest.main(verbosity=2)
