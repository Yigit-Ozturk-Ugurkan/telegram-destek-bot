"""KUYRUK ÖNCELİK + FIFO — kalıcı regression paketi (§C6).

KESİN İŞLEME SIRASI:

    1. priority severity      P1 -> P2 -> P3 -> P4
    2. queue_entered_at ASC   aynı öncelik içinde FIFO
    3. ticket_id ASC          zaman damgası eşitse deterministik tie-break

BU YALNIZCA İŞLEME SIRASIDIR — TEKNİSYEN SEÇİMİ DEĞİLDİR (§C2).
Kimin atanacağına routing.py karar verir ve o algoritma DEĞİŞMEDİ.

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ — sender enjekte edilir.
"""

from tests import _support

_support.setup_test_db("queuefifo")

import unittest
from datetime import datetime

import config
import db
import queue_service
import queue_worker
import routing
import sla_calendar
from tests._support import DbTestCase, RecordingSender


# ==================================================================
# SABİT HAFTA SONU SENARYOSU
#
# Bu paketteki takvim kontrolleri MAKİNENİN TARİHİNDEN BAĞIMSIZDIR:
# aşağıdaki damgalar sabittir ve üretim kodundaki now() yerine doğrudan
# sla_calendar'a girdi olarak verilir.
#
#   2026-08-29 Cumartesi  — BUSINESS_HOURS[5] = None  (kapalı)
#   2026-08-30 Pazar      — BUSINESS_HOURS[6] = None  (kapalı)
#   2026-08-31 Pazartesi  — BUSINESS_HOURS[0] = 08:00-18:00 (açık)
# ==================================================================

WEEKEND_SATURDAY = "2026-08-29"
WEEKEND_SUNDAY = "2026-08-30"
FOLLOWING_MONDAY = "2026-08-31"
MONDAY_OPENS_AT = (8, 0)


class QueueOrderTestCase(DbTestCase):
    db = db
    config = config

    def setUp(self):
        super().setUp()
        self.sender = RecordingSender()
        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        self.net = self.make_skill("NETWORK", "Network")
        self.general = self.make_skill(config.GENERAL_SUPPORT_SKILL_CODE,
                                       config.GENERAL_SUPPORT_SKILL_NAME)

    # ---------- Kuyruk fixture'ı ----------

    def enqueue(self, subject, priority=config.PRIORITY_P3, entered_at=None,
                skill_code="COMPUTER_VISION", department=None,
                basis=config.ROUTING_BASIS_SPECIFIC_SKILL):
        """Belirli bir öncelik ve kuyruğa giriş zamanıyla bekleyen talep üretir.

        queue_entered_at doğrudan yazılır: hafta sonu birikmesini gerçek
        zamanla beklemeden kurgulayabilmek için."""
        ticket_id = self.make_ticket(subject=subject, skill_code=skill_code,
                                     category=department)
        db.mark_escalation_pending(ticket_id, department=department)
        db.set_ticket_routing(ticket_id, basis, skill_code=skill_code,
                              department=department)
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET priority = ? WHERE id = ?",
                         (priority, ticket_id))
            if entered_at:
                conn.execute("UPDATE tickets SET queue_entered_at = ? WHERE id = ?",
                             (entered_at, ticket_id))
        return ticket_id

    def queue_order(self, **filters):
        """Kuyruğun İŞLENME SIRASI — ticket id listesi."""
        return [t["id"] for t in db.list_waiting_expert_tickets(**filters)]

    def subjects_in_order(self, **filters):
        return [t["subject"] for t in db.list_waiting_expert_tickets(**filters)]


# ==================================================================
# C — TEMEL SIRALAMA
# ==================================================================

class TestPriorityOrdering(QueueOrderTestCase):

    def test_1_p1_is_processed_before_an_older_p3(self):
        old_p3 = self.enqueue("eski P3", config.PRIORITY_P3, "2026-08-29 10:00:00")
        new_p1 = self.enqueue("yeni P1", config.PRIORITY_P1, "2026-08-31 09:00:00")

        self.assertEqual(self.queue_order(), [new_p1, old_p3])

    def test_2_p2_is_processed_before_p3(self):
        p3 = self.enqueue("P3", config.PRIORITY_P3, "2026-08-29 08:00:00")
        p2 = self.enqueue("P2", config.PRIORITY_P2, "2026-08-30 08:00:00")

        self.assertEqual(self.queue_order(), [p2, p3])

    def test_full_severity_order_p1_p2_p3_p4(self):
        p4 = self.enqueue("P4", config.PRIORITY_P4, "2026-08-29 08:00:00")
        p2 = self.enqueue("P2", config.PRIORITY_P2, "2026-08-29 08:00:00")
        p1 = self.enqueue("P1", config.PRIORITY_P1, "2026-08-29 08:00:00")
        p3 = self.enqueue("P3", config.PRIORITY_P3, "2026-08-29 08:00:00")

        self.assertEqual(self.queue_order(), [p1, p2, p3, p4])

    def test_3_same_priority_oldest_queue_entry_first(self):
        newer = self.enqueue("yeni", config.PRIORITY_P3, "2026-08-30 14:00:00")
        older = self.enqueue("eski", config.PRIORITY_P3, "2026-08-30 10:00:00")

        self.assertEqual(self.queue_order(), [older, newer])

    def test_4_equal_timestamps_break_the_tie_by_ticket_id(self):
        first = self.enqueue("A", config.PRIORITY_P2, "2026-08-30 10:00:00")
        second = self.enqueue("B", config.PRIORITY_P2, "2026-08-30 10:00:00")
        third = self.enqueue("C", config.PRIORITY_P2, "2026-08-30 10:00:00")

        self.assertEqual(self.queue_order(), sorted([first, second, third]))

    def test_5_fifo_inside_p1(self):
        third = self.enqueue("P1-3", config.PRIORITY_P1, "2026-08-30 12:00:00")
        first = self.enqueue("P1-1", config.PRIORITY_P1, "2026-08-30 08:00:00")
        second = self.enqueue("P1-2", config.PRIORITY_P1, "2026-08-30 10:00:00")

        self.assertEqual(self.queue_order(), [first, second, third])

    def test_6_fifo_inside_p3(self):
        third = self.enqueue("P3-3", config.PRIORITY_P3, "2026-08-30 12:00:00")
        first = self.enqueue("P3-1", config.PRIORITY_P3, "2026-08-30 08:00:00")
        second = self.enqueue("P3-2", config.PRIORITY_P3, "2026-08-30 10:00:00")

        self.assertEqual(self.queue_order(), [first, second, third])

    def test_missing_queue_timestamp_does_not_jump_the_queue(self):
        """queue_entered_at NULL ise created_at'e düşülür; sıra bozulmaz."""
        normal = self.enqueue("normal", config.PRIORITY_P3, "2026-08-01 08:00:00")
        orphan = self.enqueue("damgasız", config.PRIORITY_P3)
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET queue_entered_at = NULL WHERE id = ?",
                         (orphan,))

        order = self.queue_order()
        self.assertEqual(order[0], normal,
                         "Damgası olmayan satır kuyruğun başına geçmemeli.")
        self.assertIn(orphan, order)

    def test_unknown_priority_value_sorts_last_and_does_not_crash(self):
        healthy = self.enqueue("saglam", config.PRIORITY_P4, "2026-08-30 09:00:00")
        broken = self.enqueue("bozuk", config.PRIORITY_P3, "2026-08-01 09:00:00")
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET priority = 'PX' WHERE id = ?", (broken,))

        self.assertEqual(self.queue_order(), [healthy, broken])

    def test_sql_and_python_orderings_agree(self):
        """QUEUE_ORDER_BY_SQL ile db.queue_order_key AYNI sırayı üretmeli."""
        self.enqueue("a", config.PRIORITY_P3, "2026-08-30 10:00:00")
        self.enqueue("b", config.PRIORITY_P1, "2026-08-30 12:00:00")
        self.enqueue("c", config.PRIORITY_P3, "2026-08-30 08:00:00")
        self.enqueue("d", config.PRIORITY_P2, "2026-08-30 09:00:00")

        rows = db.list_waiting_expert_tickets()
        self.assertEqual([r["id"] for r in rows],
                         [r["id"] for r in sorted(rows, key=db.queue_order_key)])

    def test_priority_rank_is_derived_from_config_not_hardcoded(self):
        self.assertEqual(list(db.PRIORITY_RANK), list(config.PRIORITIES))
        self.assertEqual(db.PRIORITY_RANK[config.PRIORITY_P1], 0)


# ==================================================================
# C1 — HAFTA SONU BİRİKMESİ (spec örneği)
# ==================================================================

class TestWeekendBacklogExample(QueueOrderTestCase):
    """Cumartesi 10:00 P3-A · Cumartesi 14:00 P3-B ·
       Pazar 09:00 P1-C   · Pazar 18:00 P2-D

       Pazartesi işlenme sırası: C, D, A, B."""

    def setUp(self):
        super().setUp()
        # 2026-08-29 Cumartesi, 2026-08-30 Pazar (SLA takvimi: hafta sonu kapalı)
        self.ticket_a = self.enqueue("Ticket A", config.PRIORITY_P3,
                                     "2026-08-29 10:00:00")
        self.ticket_b = self.enqueue("Ticket B", config.PRIORITY_P3,
                                     "2026-08-29 14:00:00")
        self.ticket_c = self.enqueue("Ticket C", config.PRIORITY_P1,
                                     "2026-08-30 09:00:00")
        self.ticket_d = self.enqueue("Ticket D", config.PRIORITY_P2,
                                     "2026-08-30 18:00:00")

    def test_monday_processing_order_matches_the_specification(self):
        self.assertEqual(
            self.subjects_in_order(),
            ["Ticket C", "Ticket D", "Ticket A", "Ticket B"])

    def test_worker_assigns_in_that_exact_order(self):
        """Tek müsait teknisyen: sırayla ilk gelen kapar."""
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))

        queue_worker.run_once(sender=self.sender)

        # Dördü de aynı uzmanlıkta, dolayısıyla hepsi atanır. Denetim
        # tablosundaki EKLENME sırası, worker'ın işleme sırasını verir.
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT ticket_id FROM ticket_assignments "
                "WHERE assignment_role = 'PRIMARY' ORDER BY id ASC"
            ).fetchall()
        assigned_order = [r["ticket_id"] for r in rows]

        self.assertEqual(
            assigned_order,
            [self.ticket_c, self.ticket_d, self.ticket_a, self.ticket_b])

    # ---------- C1.1: hafta sonu SLA iş saati tüketmez ----------
    #
    # DETERMİNİZM NOTU
    # ----------------
    # Bu üç kontrol SABİT damgalarla çalışır. Eski tek test,
    # `db.start_sla_cycle_business()` içindeki now() değerini ölçtüğü için
    # yalnızca Cuma 18:00 - Pazartesi 18:00 arasında geçiyordu; haftanın
    # kalanında "İlk çalışma anı Pazartesi olmalı" beklentisi kırılıyordu.
    # Üretim davranışı DEĞİŞMEDİ — yalnız girdi sabitlendi.

    def _tz_datetime(self, gun: str, saat: int, dakika: int = 0):
        """'YYYY-AA-GG' + saat -> SLA saat diliminde aware datetime."""
        return sla_calendar.to_aware("%s %02d:%02d:00" % (gun, saat, dakika))

    def test_11a_weekend_days_are_closed_and_monday_is_open(self):
        """Senaryonun ön koşulu: senaryo günleri beklendiği gibi.

        Takvim tatil tablosundan da beslendiği için önce cache tazelenir;
        bu paketin DB'sinde tatil YOKTUR."""
        sla_calendar.invalidate_cache()
        self.addCleanup(sla_calendar.invalidate_cache)

        cumartesi = datetime.strptime(WEEKEND_SATURDAY, "%Y-%m-%d").date()
        pazar = datetime.strptime(WEEKEND_SUNDAY, "%Y-%m-%d").date()
        pazartesi = datetime.strptime(FOLLOWING_MONDAY, "%Y-%m-%d").date()

        self.assertEqual((cumartesi.weekday(), pazar.weekday(),
                          pazartesi.weekday()), (5, 6, 0))
        self.assertIsNone(sla_calendar.business_window(cumartesi),
                          "Cumartesi kapalı olmalı.")
        self.assertIsNone(sla_calendar.business_window(pazar),
                          "Pazar kapalı olmalı.")
        self.assertIsNotNone(sla_calendar.business_window(pazartesi),
                             "Pazartesi açık olmalı.")

    def test_11_weekend_does_not_consume_business_sla(self):
        """Mesai dışı zaman SLA saatini TÜKETMEZ (mevcut davranış korunur).

        Hafta sonunun HERHANGİ bir anında açılan sayaç, takip eden
        Pazartesi çalışma başlangıcında işlemeye başlar."""
        sla_calendar.invalidate_cache()
        self.addCleanup(sla_calendar.invalidate_cache)

        pazartesi_acilis = self._tz_datetime(FOLLOWING_MONDAY, *MONDAY_OPENS_AT)

        senaryo = [
            (WEEKEND_SATURDAY, 10, "Cumartesi 10:00 — Ticket A"),
            (WEEKEND_SATURDAY, 14, "Cumartesi 14:00 — Ticket B"),
            (WEEKEND_SUNDAY, 9, "Pazar 09:00 — Ticket C"),
            (WEEKEND_SUNDAY, 18, "Pazar 18:00 — Ticket D"),
        ]
        for gun, saat, etiket in senaryo:
            with self.subTest(an=etiket):
                eskalasyon = self._tz_datetime(gun, saat)
                self.assertFalse(sla_calendar.is_business_time(eskalasyon),
                                 "Hafta sonu mesai saati DEĞİLDİR.")

                effective = sla_calendar.next_business_time(eskalasyon)
                self.assertEqual(effective, pazartesi_acilis,
                                 "İlk çalışma anı takip eden Pazartesi 08:00.")
                self.assertEqual(effective.weekday(), 0,
                                 "İlk çalışma anı Pazartesi olmalı.")
                self.assertTrue(sla_calendar.is_business_time(effective))

                # Hafta sonu boyunca HİÇ iş dakikası tüketilmez.
                self.assertEqual(
                    sla_calendar.business_minutes_between(eskalasyon,
                                                          pazartesi_acilis), 0,
                    "Hafta sonu geçen süre SLA saatini tüketmemeli.")

    def test_11b_open_cycle_never_starts_before_a_business_moment(self):
        """Cycle seviyesindeki ZAMANDAN BAĞIMSIZ değişmezler.

        Gerçek `effective_started_at` çalıştırma anına bağlıdır; bu yüzden
        BURADA takvim günü DEĞİL, sözleşme doğrulanır: mod iş takvimidir,
        sayaç bir mesai anında başlar ve gerçek eskalasyon anından geri
        gitmez (mesai dışı zaman tüketilmez)."""
        cycle = db.get_active_cycle(self.ticket_c)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle["calendar_mode"], config.CALENDAR_MODE_BUSINESS)

        started = sla_calendar.to_aware(cycle["started_at"])
        effective = sla_calendar.to_aware(cycle["effective_started_at"])
        self.assertIsNotNone(effective)
        self.assertTrue(sla_calendar.is_business_time(effective),
                        "Sayaç yalnızca mesai içinde işlemeye başlayabilir.")
        self.assertGreaterEqual(effective, started,
                                "Sayaç eskalasyon anından geriye kaydırılamaz.")
        self.assertEqual(
            effective, sla_calendar.next_business_time(started),
            "effective_started_at = next_business_time(started_at).")

    def test_queue_entry_timestamp_is_preserved_while_waiting(self):
        before = db.get_ticket(self.ticket_a)["queue_entered_at"]
        queue_worker.run_once(sender=self.sender)   # aday yok -> kuyrukta kalır
        self.assertEqual(db.get_ticket(self.ticket_a)["queue_entered_at"], before)

    def test_tickets_stay_queued_when_nobody_is_available(self):
        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 0)
        self.assertEqual(stats["checked"], 4)
        for ticket_id in (self.ticket_a, self.ticket_b, self.ticket_c, self.ticket_d):
            self.assertEqual(db.get_ticket(ticket_id)["status"],
                             config.STATUS_ESCALATION_PENDING)


# ==================================================================
# C4 — HER ATAMADAN SONRA WORKLOAD YENİDEN HESAPLANIR
# ==================================================================

class TestWorkloadRecalculation(QueueOrderTestCase):

    def test_7_workload_is_recomputed_between_tickets(self):
        """Başta TEK snapshot alınsaydı ikisi de aynı kişiye giderdi."""
        hakan = self.make_technician("Hakan", skills=((self.cv, 1),))
        kaan = self.make_technician("Kaan", skills=((self.cv, 1),))
        self.assertEqual(db.count_active_tickets_for_technician(hakan), 0)
        self.assertEqual(db.count_active_tickets_for_technician(kaan), 0)

        first = self.enqueue("A", config.PRIORITY_P3, "2026-08-30 08:00:00")
        second = self.enqueue("B", config.PRIORITY_P3, "2026-08-30 09:00:00")

        queue_worker.run_once(sender=self.sender)

        owners = {db.get_ticket(first)["assigned_technician_id"],
                  db.get_ticket(second)["assigned_technician_id"]}
        self.assertEqual(owners, {hakan, kaan},
                         "İkinci talep değerlendirilirken ilk atama sayılmalıydı.")

    def test_8_fair_distribution_across_two_technicians(self):
        hakan = self.make_technician("Hakan", skills=((self.cv, 1),))
        kaan = self.make_technician("Kaan", skills=((self.cv, 1),))

        for i in range(6):
            self.enqueue(f"T{i}", config.PRIORITY_P3, f"2026-08-30 0{i}:00:00")
        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.count_active_tickets_for_technician(hakan), 3)
        self.assertEqual(db.count_active_tickets_for_technician(kaan), 3)

    def test_higher_priority_ticket_is_assigned_first_and_shifts_the_load(self):
        """Sıra P1'den başlar; adalet sıralaması ondan SONRA gelen için kayar."""
        hakan = self.make_technician("Hakan", skills=((self.cv, 1),))
        kaan = self.make_technician("Kaan", skills=((self.cv, 1),))

        p1 = self.enqueue("kritik", config.PRIORITY_P1, "2026-08-31 09:00:00")
        p3 = self.enqueue("normal", config.PRIORITY_P3, "2026-08-29 09:00:00")

        queue_worker.run_once(sender=self.sender)

        # P1 önce işlendi -> ilk (en düşük id'li) müsait kişi onu aldı.
        self.assertEqual(db.get_ticket(p1)["assigned_technician_id"], hakan)
        self.assertEqual(db.get_ticket(p3)["assigned_technician_id"], kaan)

    def test_technician_selection_algorithm_is_untouched(self):
        """§C2: sıralama değişti, SEÇİM algoritması değişmedi."""
        busy = self.make_technician("Yuklu", skills=((self.cv, 1),))
        free = self.make_technician("Bos", skills=((self.cv, 1),))
        self.set_active_workload(busy, 2)

        ticket_id = self.enqueue("tek talep", config.PRIORITY_P1)
        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], free,
                         "En az yüklü kişi seçilmeli (fairness korunur).")

    def test_skill_priority_still_beats_workload(self):
        expert = self.make_technician("Uzman", skills=((self.cv, 1),))
        backup = self.make_technician("Yedek", skills=((self.cv, 2),))
        self.set_active_workload(expert, 5)

        ticket_id = self.enqueue("uzmanlik talebi", config.PRIORITY_P1)
        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"], expert,
                         "1. öncelik uzmanlık varken 2. öncelik değerlendirilmez.")


# ==================================================================
# C5 — KUYRUK DAYANAKLARI AYRIKTIR
# ==================================================================

class TestQueueBasisIsolation(QueueOrderTestCase):

    def test_9_specific_department_and_general_queues_do_not_mix(self):
        specific = self.enqueue("SPESIFIK", config.PRIORITY_P1,
                                "2026-08-30 08:00:00", skill_code="COMPUTER_VISION",
                                basis=config.ROUTING_BASIS_SPECIFIC_SKILL)
        general = self.enqueue("GENEL", config.PRIORITY_P1, "2026-08-30 08:30:00",
                               skill_code=config.GENERAL_SUPPORT_SKILL_CODE,
                               basis=config.ROUTING_BASIS_GENERAL_SUPPORT)
        department = self.enqueue("DEPARTMAN", config.PRIORITY_P1,
                                  "2026-08-30 09:00:00", skill_code=None,
                                  department="Ağ",
                                  basis=config.ROUTING_BASIS_DEPARTMENT)

        self.assertEqual(self.queue_order(skill_code="COMPUTER_VISION"), [specific])
        self.assertEqual(
            self.queue_order(skill_code=config.GENERAL_SUPPORT_SKILL_CODE), [general])
        self.assertEqual(self.queue_order(department="Ağ"), [department])

    def test_priority_order_holds_inside_each_isolated_queue(self):
        cv_p3 = self.enqueue("cv-p3", config.PRIORITY_P3, "2026-08-30 08:00:00")
        cv_p1 = self.enqueue("cv-p1", config.PRIORITY_P1, "2026-08-30 12:00:00")
        net_p3 = self.enqueue("net-p3", config.PRIORITY_P3, "2026-08-30 07:00:00",
                              skill_code="NETWORK")

        self.assertEqual(self.queue_order(skill_code="COMPUTER_VISION"),
                         [cv_p1, cv_p3])
        self.assertEqual(self.queue_order(skill_code="NETWORK"), [net_p3])

    def test_a_specific_ticket_never_falls_into_the_general_queue(self):
        self.enqueue("SPESIFIK", config.PRIORITY_P1, skill_code="COMPUTER_VISION")
        self.assertEqual(
            self.queue_order(skill_code=config.GENERAL_SUPPORT_SKILL_CODE), [])

    def test_10_manual_triage_is_never_auto_assigned(self):
        self.make_technician("Herkese Bakan", skills=((self.cv, 1), (self.net, 2)))
        triage = self.enqueue("BELIRSIZ", config.PRIORITY_P1, skill_code=None,
                              department="Yazılım",
                              basis=config.ROUTING_BASIS_MANUAL_TRIAGE)

        queue_worker.run_once(sender=self.sender)

        ticket = db.get_ticket(triage)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertEqual(ticket["status"], config.STATUS_ESCALATION_PENDING)

    def test_manual_triage_p1_does_not_block_the_rest_of_the_queue(self):
        """En yüksek öncelikli talep MANUAL_TRIAGE olsa bile kuyruk akar."""
        self.make_technician("Hakan", skills=((self.cv, 1),))
        triage = self.enqueue("BELIRSIZ-P1", config.PRIORITY_P1,
                              "2026-08-30 08:00:00", skill_code=None,
                              department="Yazılım",
                              basis=config.ROUTING_BASIS_MANUAL_TRIAGE)
        normal = self.enqueue("CV-P3", config.PRIORITY_P3, "2026-08-30 09:00:00")

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["assigned"], 1)
        self.assertIsNone(db.get_ticket(triage)["assigned_technician_id"])
        self.assertIsNotNone(db.get_ticket(normal)["assigned_technician_id"])

    def test_no_candidate_does_not_move_a_ticket_to_another_basis(self):
        ticket_id = self.enqueue("cv bekliyor", config.PRIORITY_P1)
        self.make_technician("Genelci", skills=((self.general, 1),))

        queue_worker.run_once(sender=self.sender)

        ticket = db.get_ticket(ticket_id)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertEqual(ticket["routing_basis"], config.ROUTING_BASIS_SPECIFIC_SKILL)
        self.assertEqual(ticket["required_skill_code"], "COMPUTER_VISION")


# ==================================================================
# Çoklu tarama birleştirme + concurrency
# ==================================================================

class TestMultiScanOrdering(QueueOrderTestCase):

    def test_availability_rescan_uses_one_global_priority_order(self):
        """Bir kişi müsait olduğunda kendi uzmanlıkları + departmanı taranır.

        Listeler PEŞ PEŞE değerlendirilseydi ikinci uzmanlıktaki P1, birinci
        uzmanlıktaki P4'ün ARKASINA düşerdi."""
        cv_p4 = self.enqueue("cv-p4", config.PRIORITY_P4, "2026-08-30 08:00:00",
                             skill_code="COMPUTER_VISION")
        net_p1 = self.enqueue("net-p1", config.PRIORITY_P1, "2026-08-30 09:00:00",
                              skill_code="NETWORK")

        # Bir kişi aynı ÖNCELİK seviyesinde iki uzmanlık taşıyamaz
        # (set_technician_skill o seviyeyi değiştirir); 1 ve 2 kullanıyoruz.
        technician = self.make_technician(
            "Coklu", department="Yazılım", available=False,
            skills=((self.cv, 1), (self.net, 2)))
        db.set_technician_availability(technician, True)

        decisions = queue_service.reevaluate_for_technician(technician)
        evaluated = [d["ticket_id"] for d in decisions]

        self.assertEqual(evaluated[:2], [net_p1, cv_p4],
                         "P1 önce değerlendirilmeli.")

    def test_collect_waiting_tickets_deduplicates_and_sorts(self):
        first = self.enqueue("a", config.PRIORITY_P3, "2026-08-30 10:00:00")
        second = self.enqueue("b", config.PRIORITY_P1, "2026-08-30 12:00:00")

        scan = db.list_waiting_expert_tickets(skill_code="COMPUTER_VISION")
        merged = queue_service.collect_waiting_tickets([scan, scan])

        self.assertEqual([t["id"] for t in merged], [second, first])

    def test_12_concurrent_workers_cannot_create_two_primaries(self):
        self.make_technician("Hakan", skills=((self.cv, 1),))
        ticket_id = self.enqueue("yaris", config.PRIORITY_P1)

        ticket = db.get_ticket(ticket_id)
        first = queue_service.evaluate_ticket(ticket)
        second = queue_service.evaluate_ticket(ticket)   # BAYAT snapshot

        self.assertTrue(first["assigned"])
        self.assertFalse(second["assigned"])
        self.assertEqual(second["reason"], "zaten_atanmis")

        with db.get_conn() as conn:
            primaries = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_assignments "
                "WHERE ticket_id = ? AND assignment_role = 'PRIMARY' "
                "AND removed_at IS NULL", (ticket_id,)).fetchone()["c"]
        self.assertEqual(primaries, 1)

    def test_worker_stats_reflect_the_ordered_pass(self):
        self.make_technician("Hakan", skills=((self.cv, 1),))
        self.enqueue("p1", config.PRIORITY_P1, "2026-08-30 08:00:00")
        self.enqueue("p3", config.PRIORITY_P3, "2026-08-30 07:00:00")

        stats = queue_worker.run_once(sender=self.sender)

        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["assigned"], 2)
        self.assertEqual(stats["errors"], 0)

    def test_no_real_telegram_call_is_made(self):
        """Bildirimler enjekte edilen sender'a gider; gerçek ağ yok."""
        self.make_technician("Hakan", skills=((self.cv, 1),), telegram_id=555)
        self.enqueue("bildirim", config.PRIORITY_P1)

        queue_worker.run_once(sender=self.sender)

        self.assertTrue(self.sender.calls)
        for call in self.sender.calls:
            self.assertIsNone(call["reply_markup"],
                              "Kullanıcıya teknisyen seçtirilmez.")


# ==================================================================
# İŞ SAATLERİ / SLA korunur
# ==================================================================

class TestBusinessHoursPreserved(QueueOrderTestCase):

    def test_queue_ordering_does_not_touch_the_business_calendar(self):
        """Sıralama değişikliği SLA takvimini DEĞİŞTİRMEZ."""
        self.assertIsNone(config.BUSINESS_HOURS[5], "Cumartesi kapalı kalmalı.")
        self.assertIsNone(config.BUSINESS_HOURS[6], "Pazar kapalı kalmalı.")
        self.assertEqual(config.BUSINESS_HOURS[0], ("08:00", "18:00"))

    def test_sla_policy_windows_are_unchanged(self):
        self.assertEqual(config.SLA_POLICY[config.PRIORITY_P1],
                         {"first_response_minutes": 15, "resolution_minutes": 120})
        self.assertEqual(config.SLA_POLICY[config.PRIORITY_P3],
                         {"first_response_minutes": 120, "resolution_minutes": 1440})

    def test_waiting_in_the_queue_does_not_start_a_second_cycle(self):
        ticket_id = self.enqueue("bekleyen", config.PRIORITY_P2)
        for _ in range(3):
            queue_worker.run_once(sender=self.sender)

        self.assertEqual(len(db.list_sla_cycles(ticket_id)), 1)

    def test_assignment_preserves_the_original_sla_start(self):
        self.make_technician("Hakan", skills=((self.cv, 1),))
        ticket_id = self.enqueue("atanacak", config.PRIORITY_P2)
        started_at = db.get_ticket(ticket_id)["sla_started_at"]

        queue_worker.run_once(sender=self.sender)

        self.assertEqual(db.get_ticket(ticket_id)["sla_started_at"], started_at)
        self.assertEqual(db.get_ticket(ticket_id)["ai_mode"], config.AI_MODE_HUMAN)

    def test_same_ticket_id_is_preserved_through_the_queue(self):
        self.make_technician("Hakan", skills=((self.cv, 1),))
        ticket_id = self.enqueue("ayni talep", config.PRIORITY_P1)

        queue_worker.run_once(sender=self.sender)

        self.assertIsNotNone(db.get_ticket(ticket_id)["assigned_technician_id"])
        with db.get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
        self.assertEqual(total, 1, "Kuyruk yeni talep ÜRETMEZ.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
