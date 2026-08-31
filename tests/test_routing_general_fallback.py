"""GENEL DESTEK CATCH-ALL DEĞİLDİR — ACCEPTANCE UX + ROUTING REVISION §24-§27.

Bu paketin TEK BİR AMACI VAR ve gelecekte yanlışlıkla bozulmasın diye
kalıcıdır:

    Hiçbir talep, "uygun kişi bulunamadı" ya da "sınıflandırma belirsiz"
    diye GENERAL_TECH_SUPPORT'a DÜŞMEZ.

Kanıtlanan dört kural:

  1. §4  SPECIFIC_SKILL en yüksek önceliktir; aday yoksa KENDİ kuyruğunda
         bekler (GENEL'e düşmez).
  2. §5  DEPARTMENT aday yoksa KENDİ kuyruğunda bekler (GENEL'e düşmez).
  3. §6  GENERAL_SUPPORT YALNIZCA model açıkça `general_support` dediğinde
         kullanılır; aday yoksa genel kuyrukta bekler (§9).
  4. §7  null skill / geçersiz kod / pasif skill / parse hatası / timeout /
         "emin değilim" => MANUAL_TRIAGE. Bunların hiçbiri GENEL DEĞİLDİR.

§12 FALLTHROUGH YASAĞI, §25 REGRESSION (20+ sentetik sınıflandırma),
§26 ADALET ve §27 KUYRUK DAYANAĞI da burada doğrulanır.

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ.
"""

from tests import _support, project_file

_support.setup_test_db("general_fallback")

import ast
import unittest

import config
import db
import queue_service
import routing
from tests._support import DbTestCase


GENERAL = config.GENERAL_SUPPORT_SKILL_CODE


class RoutingBasisTestCase(DbTestCase):
    db = db
    config = config

    def setUp(self):
        super().setUp()
        # Katalog: gerçek üretim kataloğunun küçük bir kesiti + genel destek.
        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        self.sysadmin = self.make_skill("SYSTEM_ADMIN", "Sistem Yönetimi")
        self.network = self.make_skill("NETWORK", "Ağ / Network")
        self.general = self.make_skill(GENERAL, config.GENERAL_SUPPORT_SKILL_NAME)

    # ---------- fixture yardımcıları ----------

    def queued(self, basis, skill_code=None, department=None,
               subject="Kuyruk talebi", user_id=1000):
        """Verilen DAYANAKLA kuyruğa girmiş bir talep üretir.

        Üretim yolunun aynısını kullanır: önce mark_escalation_pending
        (status + queue_entered_at + SLA), sonra doğrulanmış yönlendirme."""
        ticket_id = self.make_ticket(user_id=user_id, subject=subject)
        db.mark_escalation_pending(ticket_id, department=department)
        db.set_ticket_routing(ticket_id, basis, skill_code=skill_code,
                              department=department)
        return ticket_id

    def basis_of(self, ticket_id):
        return db.get_ticket(ticket_id)["routing_basis"]


# ==================================================================
# §24.1-§24.3: SPECIFIC SKILL — aday varken ve YOKKEN
# ==================================================================

class TestSpecificSkillNeverFallsToGeneral(RoutingBasisTestCase):

    def test_24_1_computer_vision_with_available_specialist_is_assigned(self):
        """§24.1: geçerli uzmanlık + müsait kişi -> o uzman atanır."""
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        # Genel destekçi de var ama TALEBİ ALMAMALI.
        self.make_technician("Genelci", skills=((self.general, 1),))

        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], hakan)
        self.assertEqual(decision["result"]["routing_basis"],
                         routing.BASIS_SPECIFIC_SKILL)

    def test_24_2_computer_vision_with_zero_candidates_stays_in_its_queue(self):
        """§24.2: CV uzmanı YOK -> CV kuyruğunda bekler, GENEL'e DÜŞMEZ."""
        # Genel destekçi MÜSAİT ve BOŞTA — cazip bir "fallback" olurdu.
        genelci = self.make_technician("Genelci", skills=((self.general, 1),))

        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"], "GENEL destekçiye atanmamalı.")
        self.assertEqual(decision["reason"], "aday_yok")

        ticket = db.get_ticket(ticket_id)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertEqual(ticket["status"], config.STATUS_ESCALATION_PENDING)
        self.assertEqual(ticket["required_skill_code"], "COMPUTER_VISION",
                         "Talep hâlâ CV bekliyor olmalı.")
        self.assertEqual(ticket["routing_basis"], routing.BASIS_SPECIFIC_SKILL)
        self.assertNotEqual(ticket["required_skill_code"], GENERAL)

        # Genel destekçinin iş yükü ARTMAMALI.
        self.assertEqual(db.count_active_tickets_for_technician(genelci), 0)

    def test_24_3_system_admin_with_zero_candidates_stays_in_its_queue(self):
        """§24.3: SYSTEM_ADMIN uzmanı yok -> SYSTEM_ADMIN kuyruğu."""
        self.make_technician("Genelci", skills=((self.general, 1),))
        self.make_technician("CV'ci", skills=((self.cv, 1),))

        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="SYSTEM_ADMIN")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"])
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["required_skill_code"], "SYSTEM_ADMIN")
        self.assertEqual(ticket["routing_basis"], routing.BASIS_SPECIFIC_SKILL)
        self.assertIsNone(ticket["assigned_technician_id"])

    def test_specific_skill_queue_is_released_when_specialist_appears(self):
        """Talep kaybolmaz: uzman müsait olunca AYNI kuyruktan çıkar."""
        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION")
        self.assertFalse(
            queue_service.evaluate_ticket(db.get_ticket(ticket_id))["assigned"])

        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        decisions = queue_service.reevaluate_for_technician(hakan)

        assigned = [d for d in decisions if d["assigned"]]
        self.assertEqual(len(assigned), 1)
        self.assertEqual(assigned[0]["ticket_id"], ticket_id)

    def test_unavailable_specialist_does_not_leak_to_general(self):
        """Uzman VAR ama meşgul -> yine de GENEL'e geçilmez."""
        self.make_technician("Hakan Bulut", available=False,
                             skills=((self.cv, 1),))
        self.make_technician("Genelci", skills=((self.general, 1),))

        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"])
        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         routing.BASIS_SPECIFIC_SKILL)


# ==================================================================
# §24.4-§24.5: DEPARTMAN
# ==================================================================

class TestDepartmentNeverFallsToGeneral(RoutingBasisTestCase):

    def test_24_4_no_skill_with_valid_department_routes_by_department(self):
        """§24.4: uzmanlık yok + geçerli departman -> departman routing."""
        ayse = self.make_technician("Ayşe Demir", department="Ağ")
        self.make_technician("Genelci", department="Yazılım",
                             skills=((self.general, 1),))

        ticket_id = self.queued(routing.BASIS_DEPARTMENT, department="Ağ")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], ayse)
        self.assertEqual(decision["result"]["routing_basis"],
                         routing.BASIS_DEPARTMENT)

    def test_24_5_department_with_zero_candidates_stays_in_department_queue(self):
        """§24.5: departmanda müsait kimse yok -> DEPARTMAN kuyruğu."""
        genelci = self.make_technician("Genelci", department="Yazılım",
                                       skills=((self.general, 1),))

        ticket_id = self.queued(routing.BASIS_DEPARTMENT, department="Ağ")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"], "Genel destekçiye kaçmamalı.")
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["routing_basis"], routing.BASIS_DEPARTMENT)
        self.assertIsNone(ticket["required_skill_code"],
                          "Departman talebine uzmanlık kodu iliştirilmemeli.")
        self.assertEqual(db.count_active_tickets_for_technician(genelci), 0)

    def test_department_queue_is_released_when_member_becomes_available(self):
        self.make_technician("Ayşe Demir", department="Ağ", available=False)
        ticket_id = self.queued(routing.BASIS_DEPARTMENT, department="Ağ")
        self.assertFalse(
            queue_service.evaluate_ticket(db.get_ticket(ticket_id))["assigned"])

        ayse = db.list_technicians()[0]
        db.set_technician_availability(ayse["id"], True)
        decisions = queue_service.reevaluate_for_technician(ayse["id"])

        self.assertEqual([d["ticket_id"] for d in decisions if d["assigned"]],
                         [ticket_id])


# ==================================================================
# §24.6-§24.7: GENEL DESTEK — YALNIZCA AÇIK BEYANLA
# ==================================================================

class TestGeneralSupportIsExplicitOnly(RoutingBasisTestCase):

    def test_24_6_explicit_general_support_routes_to_general(self):
        """§24.6: model AÇIKÇA general_support dedi -> GENEL routing."""
        genelci = self.make_technician("Genelci", skills=((self.general, 1),))

        decision = routing.resolve_classification({
            "ok": True,
            "classification_type": config.CLASSIFICATION_GENERAL_SUPPORT,
            "category": "Donanım",
            "required_skill": GENERAL,
        })
        self.assertEqual(decision["routing_basis"], routing.BASIS_GENERAL_SUPPORT)
        self.assertEqual(decision["skill_code"], GENERAL)

        ticket_id = self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL,
                                department="Donanım")
        assignment = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(assignment["assigned"])
        self.assertEqual(assignment["technician"]["id"], genelci)
        self.assertEqual(assignment["result"]["routing_basis"],
                         routing.BASIS_GENERAL_SUPPORT)

    def test_24_7_general_support_with_zero_candidates_stays_in_general_queue(self):
        """§24.7 + §9: genel destekçi yoksa GENEL kuyruğunda bekler.

        Rastgele bir departmana ya da başka bir uzmanlığa GÖNDERİLMEZ."""
        cv_ci = self.make_technician("CV'ci", department="Yazılım",
                                     skills=((self.cv, 1),))

        ticket_id = self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL,
                                department="Yazılım")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"])
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["routing_basis"], routing.BASIS_GENERAL_SUPPORT)
        self.assertEqual(ticket["required_skill_code"], GENERAL)
        self.assertEqual(db.count_active_tickets_for_technician(cv_ci), 0,
                         "Aynı departmandaki uzmana kaçmamalı.")

    def test_general_support_ignores_department_soft_filter(self):
        """§26: genel destek adaleti departmandan bağımsızdır.

        Başka departmandaki BOŞTA genelci, aynı departmandaki YÜKLÜ genelciye
        tercih edilir — departman yumuşak filtresi burada UYGULANMAZ."""
        yuklu = self.make_technician("Yüklü Genelci", department="Yazılım",
                                     skills=((self.general, 1),))
        bos = self.make_technician("Boş Genelci", department="Ağ",
                                   skills=((self.general, 1),))
        self.set_active_workload(yuklu, 3)

        ticket_id = self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL,
                                department="Yazılım")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], bos)

    def test_24_10_inactive_general_skill_becomes_manual_triage_not_general(self):
        """§10 + §24.10: GENERAL katalogda pasifse GENEL'e ZORLANMAZ."""
        db.set_skill_active(self.general, False)

        decision = routing.resolve_classification({
            "ok": True,
            "classification_type": config.CLASSIFICATION_GENERAL_SUPPORT,
            "category": "Donanım",
            "required_skill": GENERAL,
        })

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertIsNone(decision["skill_code"])
        self.assertEqual(decision["reason"], "general_support_skill_unavailable")

    def test_owning_general_skill_does_not_make_someone_a_catch_all(self):
        """§10: GENERAL_TECH_SUPPORT sahibi olmak fallback OLMAK DEĞİLDİR.

        Hakan: 1 COMPUTER_VISION, 2 GENERAL_TECH_SUPPORT.
        SYSTEM_ADMIN bekleyen talep ona GİTMEZ."""
        hakan = self.make_technician(
            "Hakan Bulut", skills=((self.cv, 1), (self.general, 2)))

        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="SYSTEM_ADMIN")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"])
        self.assertEqual(db.count_active_tickets_for_technician(hakan), 0)

    def test_general_skill_holder_still_gets_general_tickets(self):
        """Aynı kişi GERÇEK genel destek talebini alır — kural simetriktir."""
        hakan = self.make_technician(
            "Hakan Bulut", skills=((self.cv, 1), (self.general, 2)))

        ticket_id = self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL)
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], hakan)


# ==================================================================
# §24.8-§24.10: BELİRSİZLİK -> MANUEL TRİYAJ (GENEL DEĞİL)
# ==================================================================

class TestUncertainNeverBecomesGeneral(RoutingBasisTestCase):

    def test_24_8_json_parse_failure_is_manual_triage(self):
        """§24.8: ai_assistant ok=False döndü -> MANUAL_TRIAGE."""
        decision = routing.resolve_classification(
            {"ok": False, "classification_type": None,
             "category": None, "required_skill": None})

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertIsNone(decision["skill_code"])
        self.assertEqual(decision["reason"], "classification_failed")

    def test_parse_failure_does_not_reuse_existing_category(self):
        """Başarısız sınıflandırma eski kategoriyle 'kurtarılmaz'.

        Aksi hâlde Gemini her düştüğünde talepler sessizce departman
        routing'ine girerdi ve hata görünmez olurdu."""
        decision = routing.resolve_classification(
            {"ok": False, "category": None, "required_skill": None},
            fallback_department="Yazılım")

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)

    def test_24_9_invalid_skill_code_does_not_become_general(self):
        """§24.9: katalogda olmayan kod -> server doğrulaması eler."""
        decision = routing.resolve_classification({
            "ok": True,
            "classification_type": config.CLASSIFICATION_SPECIFIC_SKILL,
            "category": None,
            "required_skill": "KUANTUM_ISINLAMA",
        })

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertIsNone(decision["skill_code"])

    def test_24_10_inactive_skill_code_does_not_become_general(self):
        """§24.10: pasif uzmanlık -> GENEL yapılmaz."""
        db.set_skill_active(self.cv, False)

        decision = routing.resolve_classification({
            "ok": True,
            "classification_type": config.CLASSIFICATION_SPECIFIC_SKILL,
            "category": None,
            "required_skill": "COMPUTER_VISION",
        })

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)

    def test_null_skill_without_category_is_manual_triage(self):
        """§7: required_skill=null + kategori yok -> belirsiz."""
        decision = routing.resolve_classification({
            "ok": True, "classification_type": None,
            "category": None, "required_skill": None})

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)

    def test_explicit_uncertain_is_not_downgraded_to_department(self):
        """Model 'emin değilim' dediyse kategori VARSA BİLE triyaja gider.

        'Emin değilim' ile 'departman şu' aynı şey değildir; belirsizliği
        sessizce bir kuyruğa çevirmek hatayı görünmez kılar."""
        decision = routing.resolve_classification({
            "ok": True,
            "classification_type": config.CLASSIFICATION_UNCERTAIN,
            "category": "Yazılım",
            "required_skill": None})

        self.assertEqual(decision["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertEqual(decision["reason"], "model_uncertain")

    def test_manual_triage_ticket_is_never_auto_assigned(self):
        """§8 + §27: worker MANUAL_TRIAGE talebine atama YAPMAZ."""
        genelci = self.make_technician("Genelci", department="Yazılım",
                                       skills=((self.general, 1),))
        herkes = self.make_technician("Serbest", department="Ağ")

        ticket_id = self.queued(routing.BASIS_MANUAL_TRIAGE, department=None)
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertFalse(decision["assigned"])
        self.assertEqual(decision["reason"], "manuel_triyaj_bekliyor")
        self.assertEqual(decision["result"]["mode"], routing.MODE_MANUAL_TRIAGE)
        for tech in (genelci, herkes):
            self.assertEqual(db.count_active_tickets_for_technician(tech), 0)

    def test_manual_triage_ticket_survives_a_full_worker_sweep(self):
        """Talep KAYBOLMAZ: tam kuyruk taramasından sonra da orada durur."""
        self.make_technician("Genelci", skills=((self.general, 1),))
        ticket_id = self.queued(routing.BASIS_MANUAL_TRIAGE)

        queue_service.reevaluate_waiting_tickets()

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["status"], config.STATUS_ESCALATION_PENDING)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertEqual(ticket["routing_basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertIsNotNone(ticket["candidate_checked_at"],
                             "Değerlendirildiği iz bırakmalı (unutulmasın).")

    def test_admin_can_route_manual_triage_ticket_to_a_skill(self):
        """§8: admin uzmanlık atayınca talep O kuyruğa girer."""
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        ticket_id = self.queued(routing.BASIS_MANUAL_TRIAGE)

        db.set_ticket_routing(ticket_id, config.ROUTING_BASIS_SPECIFIC_SKILL,
                              skill_code="COMPUTER_VISION", department="Yazılım")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], hakan)

    def test_admin_can_route_manual_triage_ticket_to_a_department(self):
        ayse = self.make_technician("Ayşe Demir", department="Ağ")
        ticket_id = self.queued(routing.BASIS_MANUAL_TRIAGE)

        db.set_ticket_routing(ticket_id, config.ROUTING_BASIS_DEPARTMENT,
                              skill_code=None, department="Ağ")
        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))

        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], ayse)

    def test_db_rejects_unknown_routing_basis(self):
        """Yazım hatası bir talebi kuyruk dışına düşüremez."""
        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION")

        self.assertFalse(db.set_ticket_routing_basis(ticket_id, "GENEL"))
        self.assertFalse(db.set_ticket_routing(ticket_id, "her_neyse"))
        self.assertEqual(self.basis_of(ticket_id), routing.BASIS_SPECIFIC_SKILL)


# ==================================================================
# §25: "HER TALEP GENEL DESTEĞE DÜŞMESİN" REGRESSION
# ==================================================================

#: 20+ SENTETİK sınıflandırma sonucu. Her satır:
#:   (etiket, ham model çıktısı, beklenen routing_basis)
#: Gerçek Gemini ÇAĞRILMAZ; doğrulama katmanının sözleşmesi test edilir.
SYNTHETIC_CLASSIFICATIONS = [
    # --- SPECIFIC_SKILL: gerçek uzmanlık üretildi ---
    ("cv_specific", {"ok": True, "classification_type": "specific_skill",
                     "category": "Yazılım", "required_skill": "COMPUTER_VISION"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    ("sysadmin_specific", {"ok": True, "classification_type": "specific_skill",
                           "category": "Diğer", "required_skill": "SYSTEM_ADMIN"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    ("network_specific", {"ok": True, "classification_type": "specific_skill",
                          "category": "Ağ", "required_skill": "NETWORK"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    ("skill_lowercase", {"ok": True, "classification_type": "specific_skill",
                         "category": "Yazılım", "required_skill": "computer_vision"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    ("skill_without_category", {"ok": True, "classification_type": "specific_skill",
                                "category": None, "required_skill": "NETWORK"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    ("skill_with_invalid_category", {"ok": True, "classification_type": "specific_skill",
                                     "category": "Uzay Mühendisliği",
                                     "required_skill": "COMPUTER_VISION"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    # Tip yanlış beyan edilse bile GEÇERLİ uzmanlık kazanır (§4).
    ("skill_beats_department_type", {"ok": True, "classification_type": "department",
                                     "category": "Ağ", "required_skill": "SYSTEM_ADMIN"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),
    ("skill_beats_general_type", {"ok": True, "classification_type": "general_support",
                                  "category": "Ağ", "required_skill": "COMPUTER_VISION"},
     config.ROUTING_BASIS_SPECIFIC_SKILL),

    # --- DEPARTMENT: uzmanlık yok ama kategori geçerli ---
    ("department_declared", {"ok": True, "classification_type": "department",
                             "category": "Ağ", "required_skill": None},
     config.ROUTING_BASIS_DEPARTMENT),
    ("department_donanim", {"ok": True, "classification_type": "department",
                            "category": "Donanım", "required_skill": None},
     config.ROUTING_BASIS_DEPARTMENT),
    ("department_null_string_skill", {"ok": True, "classification_type": "department",
                                      "category": "Yazılım", "required_skill": "null"},
     config.ROUTING_BASIS_DEPARTMENT),
    ("department_type_missing", {"ok": True, "classification_type": None,
                                 "category": "Hesap/Erişim", "required_skill": None},
     config.ROUTING_BASIS_DEPARTMENT),
    ("department_with_unknown_skill", {"ok": True, "classification_type": "department",
                                       "category": "Ağ",
                                       "required_skill": "UYDURMA_KOD"},
     config.ROUTING_BASIS_DEPARTMENT),

    # --- GENERAL_SUPPORT: YALNIZCA açık beyan ---
    ("general_explicit", {"ok": True, "classification_type": "general_support",
                          "category": "Donanım", "required_skill": GENERAL},
     config.ROUTING_BASIS_GENERAL_SUPPORT),
    ("general_explicit_no_skill", {"ok": True, "classification_type": "general_support",
                                   "category": "Diğer", "required_skill": None},
     config.ROUTING_BASIS_GENERAL_SUPPORT),
    ("general_explicit_no_category", {"ok": True, "classification_type": "general_support",
                                      "category": None, "required_skill": GENERAL},
     config.ROUTING_BASIS_GENERAL_SUPPORT),

    # --- MANUAL_TRIAGE: belirsizlik, hata, geçersizlik ---
    ("parse_failure", {"ok": False, "classification_type": None,
                       "category": None, "required_skill": None},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("parse_failure_with_category", {"ok": False, "classification_type": "department",
                                     "category": "Ağ", "required_skill": "NETWORK"},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("model_uncertain", {"ok": True, "classification_type": "uncertain",
                         "category": "Yazılım", "required_skill": None},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("model_uncertain_bare", {"ok": True, "classification_type": "uncertain",
                              "category": None, "required_skill": None},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("unknown_skill_no_category", {"ok": True, "classification_type": "specific_skill",
                                   "category": None, "required_skill": "KUANTUM"},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("nothing_at_all", {"ok": True, "classification_type": None,
                        "category": None, "required_skill": None},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("invalid_category_only", {"ok": True, "classification_type": "department",
                               "category": "Uzay Mühendisliği", "required_skill": None},
     config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("empty_payload", {}, config.ROUTING_BASIS_MANUAL_TRIAGE),
    ("none_payload", None, config.ROUTING_BASIS_MANUAL_TRIAGE),
]


class TestSyntheticClassificationRegression(RoutingBasisTestCase):
    """§25: 20+ sentetik sınıflandırma sonucuyla catch-all yasağının kanıtı."""

    def test_at_least_twenty_synthetic_cases_exist(self):
        self.assertGreaterEqual(len(SYNTHETIC_CLASSIFICATIONS), 20)

    def test_every_synthetic_case_resolves_to_expected_basis(self):
        for label, payload, expected in SYNTHETIC_CLASSIFICATIONS:
            with self.subTest(case=label):
                decision = routing.resolve_classification(payload)
                self.assertEqual(decision["routing_basis"], expected,
                                 f"{label}: beklenen {expected}, "
                                 f"gelen {decision['routing_basis']}")

    def test_general_is_used_only_by_explicitly_general_cases(self):
        """En kritik kural: GENERAL yalnızca AÇIK beyanla seçilir."""
        general_cases = {label for label, payload, expected
                         in SYNTHETIC_CLASSIFICATIONS
                         if expected == config.ROUTING_BASIS_GENERAL_SUPPORT}

        for label, payload, _expected in SYNTHETIC_CLASSIFICATIONS:
            with self.subTest(case=label):
                decision = routing.resolve_classification(payload)
                if decision["routing_basis"] == config.ROUTING_BASIS_GENERAL_SUPPORT:
                    self.assertIn(label, general_cases)
                    self.assertEqual(
                        (payload or {}).get("classification_type"),
                        config.CLASSIFICATION_GENERAL_SUPPORT,
                        f"{label}: GENERAL yalnızca classification_type="
                        "general_support ile seçilebilir.")

    def test_specific_skill_cases_never_produce_general(self):
        for label, payload, expected in SYNTHETIC_CLASSIFICATIONS:
            if expected != config.ROUTING_BASIS_SPECIFIC_SKILL:
                continue
            with self.subTest(case=label):
                decision = routing.resolve_classification(payload)
                self.assertNotEqual(decision["routing_basis"],
                                    config.ROUTING_BASIS_GENERAL_SUPPORT)
                self.assertNotEqual(decision["skill_code"], GENERAL)

    def test_department_cases_never_produce_general(self):
        for label, payload, expected in SYNTHETIC_CLASSIFICATIONS:
            if expected != config.ROUTING_BASIS_DEPARTMENT:
                continue
            with self.subTest(case=label):
                decision = routing.resolve_classification(payload)
                self.assertNotEqual(decision["routing_basis"],
                                    config.ROUTING_BASIS_GENERAL_SUPPORT)
                self.assertIsNone(decision["skill_code"])

    def test_failure_cases_never_produce_general(self):
        for label, payload, expected in SYNTHETIC_CLASSIFICATIONS:
            if expected != config.ROUTING_BASIS_MANUAL_TRIAGE:
                continue
            with self.subTest(case=label):
                decision = routing.resolve_classification(payload)
                self.assertNotEqual(decision["routing_basis"],
                                    config.ROUTING_BASIS_GENERAL_SUPPORT)

    def test_zero_candidates_never_changes_the_basis(self):
        """§12: HİÇBİR teknisyen yokken bile dayanak DEĞİŞMEZ.

        Aday bulunamaması bir sınıflandırma sinyali değildir."""
        for label, payload, expected in SYNTHETIC_CLASSIFICATIONS:
            with self.subTest(case=label):
                decision = routing.resolve_classification(payload)
                ticket_id = self.queued(decision["routing_basis"],
                                        skill_code=decision["skill_code"],
                                        department=decision["department"],
                                        subject=label)
                queue_service.evaluate_ticket(db.get_ticket(ticket_id))

                ticket = db.get_ticket(ticket_id)
                self.assertEqual(ticket["routing_basis"], expected)
                self.assertIsNone(ticket["assigned_technician_id"])


# ==================================================================
# §12: FALLTHROUGH YASAĞI — KAYNAK DENETİMİ
# ==================================================================

class TestNoFallthroughInSource(RoutingBasisTestCase):
    """'Aday yoksa başka kuyruğa dene' deseni KODDA OLMAMALI.

    Metin araması yapılmaz (yorumlar false-positive üretirdi); kaynak AST'ye
    çevrilip GERÇEK çağrı düğümleri incelenir."""

    PRODUCTION_MODULES = ("bot.py", "webapp.py", "queue_service.py",
                          "queue_worker.py", "routing.py")

    def _calls(self, filename):
        with open(project_file(filename), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=filename)
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    found.append(func.attr)
                elif isinstance(func, ast.Name):
                    found.append(func.id)
        return found

    def test_select_for_ticket_evaluates_exactly_one_basis(self):
        """Tek çağrıda YALNIZCA bir selector kullanılır.

        Bu, 'skill boşsa departmanı dene, o da boşsa geneli dene' zincirinin
        kodda OLMADIĞININ davranışsal kanıtıdır: her dayanak için sonuç tek
        bir mode taşır ve aday listesi başka bir havuzdan beslenmez."""
        self.make_technician("Ayşe Demir", department="Ağ")
        self.make_technician("Genelci", department="Ağ",
                             skills=((self.general, 1),))

        result = routing.select_for_ticket({
            "routing_basis": routing.BASIS_SPECIFIC_SKILL,
            "required_skill_code": "COMPUTER_VISION",
            "category": "Ağ", "pending_department": "Ağ"})

        self.assertEqual(result["mode"], routing.MODE_SKILL)
        self.assertEqual(result["routing_basis"], routing.BASIS_SPECIFIC_SKILL)
        self.assertEqual(result["candidates"], [],
                         "Departman ya da genel havuzdan aday sızmamalı.")
        self.assertIsNone(result["selected"])

    def test_department_basis_never_consults_skill_pool(self):
        self.make_technician("Hakan Bulut", department="Yazılım",
                             skills=((self.cv, 1),))

        result = routing.select_for_ticket({
            "routing_basis": routing.BASIS_DEPARTMENT,
            "required_skill_code": None,
            "category": "Ağ", "pending_department": "Ağ"})

        self.assertEqual(result["mode"], routing.MODE_DEPARTMENT)
        self.assertEqual(result["candidates"], [])

    def test_manual_triage_basis_never_queries_candidates(self):
        self.make_technician("Genelci", department="Yazılım",
                             skills=((self.general, 1),))

        result = routing.select_for_ticket({
            "routing_basis": routing.BASIS_MANUAL_TRIAGE,
            "required_skill_code": None,
            "category": "Yazılım", "pending_department": "Yazılım"})

        self.assertEqual(result["mode"], routing.MODE_MANUAL_TRIAGE)
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["selected"])

    def test_no_module_hardcodes_the_general_skill_as_a_fallback(self):
        """GENERAL_TECH_SUPPORT kodu üretim modüllerinde SABİT yazılmamalı.

        Yalnızca config'te tanımlıdır ve routing üzerinden kullanılır; bir
        modül kendi içine 'GENERAL_TECH_SUPPORT' string'i gömerse bu, sessiz
        bir catch-all yolunun ilk adımıdır."""
        for module in self.PRODUCTION_MODULES:
            with self.subTest(module=module):
                with open(project_file(module), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=module)
                literals = [n.value for n in ast.walk(tree)
                            if isinstance(n, ast.Constant)
                            and isinstance(n.value, str)]
                self.assertNotIn(GENERAL, literals,
                                 f"{module} genel destek kodunu sabit yazmış.")

    def test_config_is_the_only_place_defining_the_general_code(self):
        with open(project_file("config.py"), encoding="utf-8") as fh:
            self.assertIn(f'GENERAL_SUPPORT_SKILL_CODE = "{GENERAL}"', fh.read())


# ==================================================================
# §27: QUEUE WORKER DAYANAĞA SAYGI DUYAR
# ==================================================================

class TestQueueRespectsRoutingBasis(RoutingBasisTestCase):

    def test_skill_scan_does_not_see_department_tickets(self):
        skill_ticket = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                   skill_code="COMPUTER_VISION",
                                   department="Yazılım", subject="skill")
        self.queued(routing.BASIS_DEPARTMENT, department="Yazılım",
                    subject="department")

        rows = db.list_waiting_expert_tickets(skill_code="COMPUTER_VISION")
        self.assertEqual([r["id"] for r in rows], [skill_ticket])

    def test_department_scan_does_not_see_skill_tickets(self):
        self.queued(routing.BASIS_SPECIFIC_SKILL, skill_code="COMPUTER_VISION",
                    department="Yazılım", subject="skill")
        dept_ticket = self.queued(routing.BASIS_DEPARTMENT, department="Yazılım",
                                  subject="department")

        rows = db.list_waiting_expert_tickets(department="Yazılım")
        self.assertEqual([r["id"] for r in rows], [dept_ticket])

    def test_department_scan_does_not_see_general_tickets(self):
        """Genel destek talebi departman taramasına GİRMEZ."""
        self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL,
                    department="Yazılım", subject="general")

        rows = db.list_waiting_expert_tickets(department="Yazılım")
        self.assertEqual(rows, [])

    def test_general_scan_only_sees_general_tickets(self):
        general_ticket = self.queued(routing.BASIS_GENERAL_SUPPORT,
                                     skill_code=GENERAL, subject="general")
        self.queued(routing.BASIS_SPECIFIC_SKILL, skill_code="COMPUTER_VISION",
                    subject="skill")
        self.queued(routing.BASIS_MANUAL_TRIAGE, subject="triage")

        rows = db.list_waiting_expert_tickets(skill_code=GENERAL)
        self.assertEqual([r["id"] for r in rows], [general_ticket])

    def test_manual_triage_is_in_no_automatic_scan(self):
        triage = self.queued(routing.BASIS_MANUAL_TRIAGE, department="Yazılım",
                             subject="triage")

        self.assertEqual(db.list_waiting_expert_tickets(department="Yazılım"), [])
        self.assertEqual(db.list_waiting_expert_tickets(skill_code=GENERAL), [])
        self.assertEqual(
            db.list_waiting_expert_tickets(skill_code="COMPUTER_VISION"), [])
        # Ama DAYANAK filtresiyle admin panelinde GÖRÜNÜR (kaybolmaz).
        rows = db.list_waiting_expert_tickets(basis=routing.BASIS_MANUAL_TRIAGE)
        self.assertEqual([r["id"] for r in rows], [triage])

    def test_technician_availability_scans_only_their_own_queues(self):
        """§27: müsait olan kişi yalnızca KENDİ kuyruklarını tetikler."""
        cv_ticket = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION", subject="cv")
        general_ticket = self.queued(routing.BASIS_GENERAL_SUPPORT,
                                     skill_code=GENERAL, subject="general")
        triage = self.queued(routing.BASIS_MANUAL_TRIAGE, subject="triage")

        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     skills=((self.cv, 1),))
        decisions = queue_service.reevaluate_for_technician(hakan)
        assigned = [d["ticket_id"] for d in decisions if d["assigned"]]

        self.assertEqual(assigned, [cv_ticket])
        self.assertIsNone(db.get_ticket(general_ticket)["assigned_technician_id"])
        self.assertIsNone(db.get_ticket(triage)["assigned_technician_id"])

    def test_legacy_tickets_without_basis_keep_working(self):
        """routing_basis NULL (revizyon öncesi) kayıtlar davranış değiştirmez."""
        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     skills=((self.cv, 1),))
        ticket_id = self.make_ticket(subject="Eski kayıt",
                                     skill_code="COMPUTER_VISION",
                                     category="Yazılım")
        db.mark_escalation_pending(ticket_id, department="Yazılım")
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET routing_basis = NULL WHERE id = ?",
                         (ticket_id,))

        ticket = db.get_ticket(ticket_id)
        self.assertIsNone(ticket["routing_basis"])
        self.assertEqual(routing.effective_routing_basis(ticket),
                         routing.BASIS_SPECIFIC_SKILL)

        decision = queue_service.evaluate_ticket(ticket)
        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], hakan)

    def test_legacy_department_only_ticket_still_routes_by_department(self):
        ayse = self.make_technician("Ayşe Demir", department="Ağ")
        ticket_id = self.make_ticket(subject="Eski departman", category="Ağ")
        db.mark_escalation_pending(ticket_id, department="Ağ")
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET routing_basis = NULL WHERE id = ?",
                         (ticket_id,))

        decision = queue_service.evaluate_ticket(db.get_ticket(ticket_id))
        self.assertTrue(decision["assigned"])
        self.assertEqual(decision["technician"]["id"], ayse)


# ==================================================================
# §26: ADALET KURALLARI HER ÜÇ YOLDA DA AYNI
# ==================================================================

class TestFairnessPreservedAcrossBases(RoutingBasisTestCase):

    def test_general_support_uses_skill_priority_first(self):
        """1. öncelik genelci varken 2. öncelikli DEĞERLENDİRİLMEZ."""
        birinci = self.make_technician("Birinci", skills=((self.general, 1),))
        ikinci = self.make_technician("İkinci", skills=((self.general, 2),))
        self.set_active_workload(birinci, 5)

        result = routing.select_best_technician_for_skill(GENERAL)

        self.assertEqual(result["matched_priority"], 1)
        self.assertEqual(result["selected"]["technician_id"], birinci)
        self.assertNotIn(ikinci, [c["technician_id"] for c in result["candidates"]])

    def test_general_support_uses_workload_then_recency(self):
        bos = self.make_technician("Boş", skills=((self.general, 1),))
        yuklu = self.make_technician("Yüklü", skills=((self.general, 1),))
        self.set_active_workload(yuklu, 2)

        result = routing.select_best_technician_for_skill(GENERAL)
        self.assertEqual(result["selected"]["technician_id"], bos)

    def test_general_support_never_assigned_goes_first(self):
        eski = self.make_technician("Eski", skills=((self.general, 1),))
        yeni = self.make_technician("Yeni", skills=((self.general, 1),))
        self.give_primary_history(eski, "2026-01-01 09:00:00")

        result = routing.select_best_technician_for_skill(GENERAL)
        self.assertEqual(result["selected"]["technician_id"], yeni,
                         "Hiç ana iş almamış kişi önce gelmeli.")

    def test_general_support_result_carries_general_basis(self):
        self.make_technician("Genelci", skills=((self.general, 1),))
        result = routing.select_best_technician_for_skill(GENERAL)

        self.assertEqual(result["routing_basis"], routing.BASIS_GENERAL_SUPPORT)

    def test_specific_skill_result_carries_specific_basis(self):
        self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        result = routing.select_best_technician_for_skill("COMPUTER_VISION")

        self.assertEqual(result["routing_basis"], routing.BASIS_SPECIFIC_SKILL)

    def test_basis_cannot_be_faked_by_the_caller(self):
        """Dayanak KODDAN türetilir; çağıran parametreyle değiştiremez."""
        self.assertEqual(routing._basis_for_skill_code("computer_vision"),
                         routing.BASIS_SPECIFIC_SKILL)
        self.assertEqual(routing._basis_for_skill_code(GENERAL),
                         routing.BASIS_GENERAL_SUPPORT)
        self.assertEqual(routing._basis_for_skill_code("general tech support"),
                         routing.BASIS_GENERAL_SUPPORT)

    def test_department_result_carries_department_basis(self):
        self.make_technician("Ayşe Demir", department="Ağ")
        result = routing.select_best_technician_for_department("Ağ")

        self.assertEqual(result["routing_basis"], routing.BASIS_DEPARTMENT)


# ==================================================================
# §13, §28: DAYANAK SUNUMU
# ==================================================================

class TestRoutingBasisPresentation(RoutingBasisTestCase):

    def test_waiting_reason_for_specific_skill(self):
        ticket_id = self.queued(routing.BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION")
        reason = queue_service.waiting_reason(db.get_ticket(ticket_id))

        self.assertEqual(reason["basis"], routing.BASIS_SPECIFIC_SKILL)
        self.assertEqual(reason["label"], "Uzman bekliyor")
        self.assertEqual(reason["target"], "Görüntü İşleme")
        self.assertEqual(reason["text"], "Uzman bekliyor: Görüntü İşleme")

    def test_waiting_reason_for_department(self):
        ticket_id = self.queued(routing.BASIS_DEPARTMENT, department="Ağ")
        reason = queue_service.waiting_reason(db.get_ticket(ticket_id))

        self.assertEqual(reason["label"], "Departman bekliyor")
        self.assertEqual(reason["target"], "Ağ")

    def test_waiting_reason_for_general_support(self):
        ticket_id = self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL)
        reason = queue_service.waiting_reason(db.get_ticket(ticket_id))

        self.assertEqual(reason["label"], "Genel destek bekliyor")
        self.assertEqual(reason["target"], config.GENERAL_SUPPORT_SKILL_NAME)

    def test_waiting_reason_for_manual_triage(self):
        ticket_id = self.queued(routing.BASIS_MANUAL_TRIAGE)
        reason = queue_service.waiting_reason(db.get_ticket(ticket_id))

        self.assertEqual(reason["basis"], routing.BASIS_MANUAL_TRIAGE)
        self.assertEqual(reason["label"], "Manuel sınıflandırma gerekiyor")
        self.assertEqual(reason["target"], "")

    def test_routing_summary_lines(self):
        cases = [
            (self.queued(routing.BASIS_SPECIFIC_SKILL, skill_code="SYSTEM_ADMIN"),
             "Sistem Yönetimi"),
            (self.queued(routing.BASIS_GENERAL_SUPPORT, skill_code=GENERAL),
             config.GENERAL_SUPPORT_SKILL_NAME),
            (self.queued(routing.BASIS_DEPARTMENT, department="Ağ"),
             "Ağ (departman)"),
            (self.queued(routing.BASIS_MANUAL_TRIAGE),
             "Manuel sınıflandırma gerekiyor"),
        ]
        for ticket_id, expected in cases:
            with self.subTest(ticket=ticket_id):
                self.assertEqual(
                    routing.routing_summary(db.get_ticket(ticket_id)), expected)

    def test_basis_counts_are_grouped(self):
        self.queued(routing.BASIS_SPECIFIC_SKILL, skill_code="COMPUTER_VISION")
        self.queued(routing.BASIS_SPECIFIC_SKILL, skill_code="SYSTEM_ADMIN")
        self.queued(routing.BASIS_DEPARTMENT, department="Ağ")
        self.queued(routing.BASIS_MANUAL_TRIAGE)

        counts = db.count_waiting_by_routing_basis()

        self.assertEqual(counts[routing.BASIS_SPECIFIC_SKILL], 2)
        self.assertEqual(counts[routing.BASIS_DEPARTMENT], 1)
        self.assertEqual(counts[routing.BASIS_MANUAL_TRIAGE], 1)
        self.assertNotIn(routing.BASIS_GENERAL_SUPPORT, counts)


# ==================================================================
# Katalog: GENERAL_TECH_SUPPORT varsayılan kataloğa eklendi
# ==================================================================

class TestGeneralSkillCatalog(RoutingBasisTestCase):

    def test_seed_creates_general_support_skill(self):
        self._wipe()
        db.init_db()
        db.seed_default_skills()

        skill = db.get_skill_by_code(GENERAL)
        self.assertIsNotNone(skill, "Genel destek kataloğa eklenmeli.")
        self.assertTrue(skill["active"])
        self.assertEqual(skill["name"], config.GENERAL_SUPPORT_SKILL_NAME)

    def test_general_skill_is_assignable_at_any_priority(self):
        """§10: normal 1/2/3 öncelik sistemi içinde atanabilir."""
        hakan = self.make_technician("Hakan Bulut")
        for priority in (1, 2, 3):
            ok, message = db.set_technician_skill(hakan, self.general, priority)
            self.assertTrue(ok, message)

        skills = db.list_technician_skills(hakan)
        self.assertEqual([s["skill_code"] for s in skills], [GENERAL])


if __name__ == "__main__":
    unittest.main(verbosity=2)
