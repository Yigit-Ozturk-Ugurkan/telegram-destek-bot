"""YETKİLENDİRME PAKETİ — ACCEPTANCE REVISION §31.

Teknisyen aktif ekibi SALT OKUNUR görebilir. Ama:
  - başka technician'ın available durumunu DEĞİŞTİREMEZ
  - başka technician'ın uzmanlıklarını DEĞİŞTİREMEZ
  - başka technician'ın hesabını/şifresini YÖNETEMEZ
  - başka technician'ın ticket'ına ERİŞEMEZ

UI'da buton gizlemek yeterli DEĞİLDİR: bu testler doğrudan POST atar ve
403 bekler.

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ.
"""

from tests import _support, project_file

_support.setup_test_db("authz")

import unittest

import config
import db
import webapp
from tests._support import DbTestCase, RecordingSender, fail_on_network


class AuthzTestCase(DbTestCase):
    db = db
    config = config

    def setUp(self):
        super().setUp()
        webapp.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        # GERÇEK Telegram çağrısı yapılmaz.
        self.sender = RecordingSender()
        self._real_sender = webapp.send_telegram_message
        webapp.send_telegram_message = self.sender
        self.addCleanup(setattr, webapp, "send_telegram_message", self._real_sender)

        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        self.net = self.make_skill("NETWORK", "Ağ")

        self.hakan_id = self.make_technician(
            "Hakan Bulut", department="Yazılım", skills=((self.cv, 1),))
        self.kaan_id = self.make_technician(
            "Kaan Polat", department="Yazılım", skills=((self.cv, 1),))
        self.derya_id = self.make_technician(
            "Derya Uçar", department="Ağ", skills=((self.net, 1),))

        self.admin_user = self._make_user("admin", config.ROLE_ADMIN, None)
        self.hakan_user = self._make_user("hakan", config.ROLE_TECHNICIAN, self.hakan_id)
        self.kaan_user = self._make_user("kaan", config.ROLE_TECHNICIAN, self.kaan_id)

    def _make_user(self, username, role, technician_id):
        from werkzeug.security import generate_password_hash
        return db.create_web_user(
            username=username,
            password_hash=generate_password_hash("Parola12345!"),
            role=role,
            full_name=username.title(),
            technician_id=technician_id,
            must_change_password=False,
        )

    def client_as(self, user_id):
        """Oturumu doğrudan kurar; CSRF token'ı da oturuma yazar."""
        client = webapp.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = "test-csrf-token"
        return client

    def post(self, client, path, **form):
        form.setdefault("csrf_token", "test-csrf-token")
        return client.post(path, data=form, follow_redirects=False)


# ==================================================================
# 1-2: aktif ekip görünürlüğü
# ==================================================================

class TestTeamVisibility(AuthzTestCase):

    def test_1_admin_sees_full_active_team(self):
        response = self.client_as(self.admin_user).get("/tickets")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Aktif Ekip", body)
        for name in ("Hakan Bulut", "Kaan Polat", "Derya Uçar"):
            self.assertIn(name, body)

    def test_2_technician_sees_active_team_read_only(self):
        response = self.client_as(self.hakan_user).get("/my-tickets")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Aktif Ekip", body)
        # Diğer ekip üyeleri görünür
        self.assertIn("Kaan Polat", body)
        self.assertIn("Derya Uçar", body)
        # FRONTEND UX REVISION: rail kompaktlaştı — departman, iş yükü ve
        # uzmanlık dökümü buradan KALDIRILDI (hepsi /ekip sayfasında).
        # Görünürlük kuralı değişmedi: teknisyen ekibi ve kimin müsait
        # olduğunu görmeye devam eder.
        self.assertIn('<span class="sr-only">Müsait</span>', body)
        self.assertIn("Kaan Polat — Müsait", body)

    def test_2b_technician_panel_has_no_management_actions(self):
        """Teknisyen panelinde BAŞKASINI yöneten form/link bulunmaz."""
        body = self.client_as(self.hakan_user).get("/my-tickets").get_data(as_text=True)

        for other in (self.kaan_id, self.derya_id):
            self.assertNotIn(f"/ekip/{other}/toggle", body)
            self.assertNotIn(f"/ekip/{other}/uzmanlik", body)
            self.assertNotIn(f"/ekip/{other}/hesap", body)
            self.assertNotIn(f"/ekip/{other}/gecici-sifre", body)
        self.assertNotIn('href="/ekip"', body, "Teknisyene ekip yönetim linki verilmez.")
        self.assertNotIn("Geçici şifre ver", body)
        self.assertNotIn("Web hesabı oluştur", body)

    def test_2c_technician_can_still_toggle_own_availability(self):
        """Kendi müsaitliği teknisyenin kendi yetkisindedir (mevcut davranış)."""
        client = self.client_as(self.hakan_user)
        before = db.get_technician(self.hakan_id)["available"]

        response = self.post(client, "/musaitlik", form_token="tok-own-1")

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(db.get_technician(self.hakan_id)["available"], before)


# ==================================================================
# 3-5: admin-only rotalara doğrudan POST -> 403
# ==================================================================

class TestTechnicianCannotManageOthers(AuthzTestCase):

    def test_3_technician_cannot_post_team_toggle(self):
        client = self.client_as(self.hakan_user)
        before = db.get_technician(self.kaan_id)["available"]

        response = self.post(client, f"/ekip/{self.kaan_id}/toggle", form_token="t1")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_technician(self.kaan_id)["available"], before)

    def test_3b_technician_cannot_post_team_add(self):
        client = self.client_as(self.hakan_user)
        before = len(db.list_technicians())

        response = self.post(client, "/ekip/ekle", name="Sahte Kisi",
                             department="Yazılım", form_token="t2")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(db.list_technicians()), before)

    def test_3c_technician_cannot_read_team_page(self):
        response = self.client_as(self.hakan_user).get("/ekip")
        self.assertEqual(response.status_code, 403)

    def test_4_technician_cannot_post_skill_route(self):
        client = self.client_as(self.hakan_user)
        before = db.list_technician_skills(self.kaan_id)

        response = self.post(client, f"/ekip/{self.kaan_id}/uzmanlik",
                             priority="1", skill_id=str(self.net), form_token="t3")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.list_technician_skills(self.kaan_id), before)

    def test_4b_technician_cannot_change_own_skills_either(self):
        """Uzmanlık yalnızca ADMİN tarafından belirlenir — kendi bile değiştiremez."""
        client = self.client_as(self.hakan_user)
        before = db.list_technician_skills(self.hakan_id)

        response = self.post(client, f"/ekip/{self.hakan_id}/uzmanlik",
                             priority="1", skill_id=str(self.net), form_token="t4")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.list_technician_skills(self.hakan_id), before)

    def test_5_technician_cannot_create_account_for_other(self):
        client = self.client_as(self.hakan_user)

        response = self.post(client, f"/ekip/{self.kaan_id}/hesap",
                             username="sahte.hesap", temp_password="Parola12345!",
                             form_token="t5")

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(db.get_web_user_by_username("sahte.hesap"))

    def test_5b_technician_cannot_reset_other_password(self):
        client = self.client_as(self.hakan_user)
        before = db.get_web_user(self.kaan_user)["password_hash"]

        response = self.post(client, f"/ekip/{self.kaan_id}/gecici-sifre",
                             temp_password="YeniParola123!", form_token="t6")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_web_user(self.kaan_user)["password_hash"], before)

    def test_5c_technician_cannot_reach_admin_only_pages(self):
        client = self.client_as(self.hakan_user)
        for path in ("/tickets", "/ekip", "/uzmanliklar", "/sla-takvimi"):
            self.assertEqual(client.get(path).status_code, 403, path)


# ==================================================================
# 6: talep erişimi
# ==================================================================

class TestTicketAccess(AuthzTestCase):

    def test_6_technician_cannot_access_other_technicians_ticket(self):
        ticket_id = self.make_ticket(subject="Kaan'in talebi")
        db.assign_primary_if_unassigned(ticket_id, self.kaan_id, "Kaan Polat")

        response = self.client_as(self.hakan_user).get(f"/ticket/{ticket_id}")

        self.assertEqual(response.status_code, 403)

    def test_6b_technician_cannot_reply_to_other_technicians_ticket(self):
        ticket_id = self.make_ticket(subject="Kaan'in talebi")
        db.assign_primary_if_unassigned(ticket_id, self.kaan_id, "Kaan Polat")
        before = len(db.get_messages(ticket_id))

        response = self.post(self.client_as(self.hakan_user),
                             f"/ticket/{ticket_id}/reply",
                             message="izinsiz yanit", form_token="t7")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(db.get_messages(ticket_id)), before)

    def test_6c_admin_can_access_any_ticket(self):
        ticket_id = self.make_ticket()
        db.assign_primary_if_unassigned(ticket_id, self.kaan_id, "Kaan Polat")

        response = self.client_as(self.admin_user).get(f"/ticket/{ticket_id}")

        self.assertEqual(response.status_code, 200)


# ==================================================================
# 7-9: PRIMARY / COLLABORATOR yetkileri (Faz 4 korunur)
# ==================================================================

class TestCollaboratorAuthorization(AuthzTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.make_ticket(subject="Hakan'in talebi")
        db.assign_primary_if_unassigned(self.ticket_id, self.hakan_id, "Hakan Bulut")

    def test_7_primary_can_add_collaborator(self):
        response = self.post(self.client_as(self.hakan_user),
                             f"/ticket/{self.ticket_id}/yardimci",
                             technician_id=str(self.kaan_id), form_token="c1")

        self.assertEqual(response.status_code, 302)
        collaborators = db.list_active_collaborators(self.ticket_id)
        self.assertEqual([c["technician_id"] for c in collaborators], [self.kaan_id])

    def test_7b_admin_can_add_collaborator(self):
        response = self.post(self.client_as(self.admin_user),
                             f"/ticket/{self.ticket_id}/yardimci",
                             technician_id=str(self.kaan_id), form_token="c2")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(db.list_active_collaborators(self.ticket_id)), 1)

    def test_7c_primary_sees_team_info_when_choosing_collaborator(self):
        """PRIMARY yardımcı seçerken ekip bilgisini görebilmelidir."""
        body = self.client_as(self.hakan_user).get(
            f"/ticket/{self.ticket_id}").get_data(as_text=True)

        self.assertIn("Yardımcı Ekle", body)
        # Aday satırında departman + iş yükü + uzmanlık
        self.assertIn("Kaan Polat — Yazılım", body)
        self.assertIn("aktif · müsait", body)
        self.assertIn("1.Görüntü İşleme", body)
        # Sağ Aktif Ekip paneli de aynı sayfada
        self.assertIn("Aktif Ekip", body)

    def test_8_collaborator_cannot_add_another_collaborator(self):
        ok, message = db.add_collaborator(self.ticket_id, self.kaan_id)
        self.assertTrue(ok, message)

        response = self.post(self.client_as(self.kaan_user),
                             f"/ticket/{self.ticket_id}/yardimci",
                             technician_id=str(self.derya_id), form_token="c3")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(db.list_active_collaborators(self.ticket_id)), 1)

    def test_8b_collaborator_can_view_and_reply(self):
        """Yardımcı talebi GÖRÜR ve yanıt yazabilir — sadece yönetemez."""
        db.add_collaborator(self.ticket_id, self.kaan_id)

        response = self.client_as(self.kaan_user).get(f"/ticket/{self.ticket_id}")

        self.assertEqual(response.status_code, 200)

    def test_8c_collaborator_cannot_transfer_primary(self):
        db.add_collaborator(self.ticket_id, self.kaan_id)

        response = self.post(self.client_as(self.kaan_user),
                             f"/ticket/{self.ticket_id}/devret",
                             technician_id=str(self.derya_id),
                             reason="Kendime almak istiyorum", form_token="c4")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_ticket(self.ticket_id)["assigned_technician_id"],
                         self.hakan_id)

    def test_8d_unrelated_technician_cannot_add_collaborator(self):
        derya_user = self._make_user("derya", config.ROLE_TECHNICIAN, self.derya_id)

        response = self.post(self.client_as(derya_user),
                             f"/ticket/{self.ticket_id}/yardimci",
                             technician_id=str(self.kaan_id), form_token="c5")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.list_active_collaborators(self.ticket_id), [])

    def test_9_removed_collaborator_loses_access(self):
        db.add_collaborator(self.ticket_id, self.kaan_id)
        client = self.client_as(self.kaan_user)
        self.assertEqual(client.get(f"/ticket/{self.ticket_id}").status_code, 200)

        ok, message = db.remove_collaborator(self.ticket_id, self.kaan_id)
        self.assertTrue(ok, message)

        self.assertEqual(client.get(f"/ticket/{self.ticket_id}").status_code, 403)

    def test_9b_removed_collaborator_row_is_kept_for_audit(self):
        db.add_collaborator(self.ticket_id, self.kaan_id)
        db.remove_collaborator(self.ticket_id, self.kaan_id, reason="Vardiya sonu")

        history = db.list_ticket_assignments(self.ticket_id, active_only=False)
        removed = [a for a in history if a["technician_id"] == self.kaan_id]
        self.assertEqual(len(removed), 1, "Atama geçmişi SİLİNMEZ.")
        self.assertIsNotNone(removed[0]["removed_at"])
        self.assertEqual(removed[0]["removal_reason"], "Vardiya sonu")


# ==================================================================
# CSRF hâlâ zorunlu
# ==================================================================

class TestManualRoutingAuthorization(AuthzTestCase):
    """§8: manuel yönlendirme ADMIN yetkisidir ve server'da doğrulanır."""

    def _queued_triage_ticket(self):
        ticket_id = self.make_ticket(subject="Belirsiz talep")
        db.mark_escalation_pending(ticket_id)
        db.set_ticket_routing(ticket_id, config.ROUTING_BASIS_MANUAL_TRIAGE)
        return ticket_id

    def test_technician_cannot_change_routing(self):
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.hakan_user)

        response = self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                             skill_code="COMPUTER_VISION", form_token="r1")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)

    def test_primary_technician_cannot_change_routing_either(self):
        """Ana sorumlu olmak da yetmez — yönlendirme yalnızca admindedir."""
        ticket_id = self._queued_triage_ticket()
        db.assign_primary_if_unassigned(ticket_id, self.hakan_id, "Hakan Bulut")
        client = self.client_as(self.hakan_user)

        response = self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                             skill_code="COMPUTER_VISION", form_token="r2")

        self.assertEqual(response.status_code, 403)

    def test_admin_can_route_to_a_valid_skill(self):
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        response = self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                             skill_code="COMPUTER_VISION", form_token="r3")

        self.assertEqual(response.status_code, 302)
        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_SPECIFIC_SKILL)
        self.assertEqual(ticket["required_skill_code"], "COMPUTER_VISION")

    def test_admin_routing_triggers_automatic_assignment(self):
        """Yönlendirme yapılır yapılmaz merkezi motor uygun kişiyi atar."""
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                  skill_code="NETWORK", form_token="r4")

        self.assertEqual(db.get_ticket(ticket_id)["assigned_technician_id"],
                         self.derya_id)

    def test_admin_cannot_route_to_an_unknown_skill(self):
        """Form değerine güvenilmez: server yeniden doğrular."""
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                  skill_code="UYDURMA_SKILL", form_token="r5")

        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)

    def test_admin_cannot_route_to_an_inactive_skill(self):
        ticket_id = self._queued_triage_ticket()
        db.set_skill_active(self.cv, False)
        client = self.client_as(self.admin_user)

        self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                  skill_code="COMPUTER_VISION", form_token="r6")

        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)

    def test_admin_cannot_route_to_an_unknown_department(self):
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                  department="Uzay Mühendisliği", form_token="r7")

        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)

    def test_empty_routing_submission_changes_nothing(self):
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        self.post(client, f"/ticket/{ticket_id}/yonlendirme", form_token="r8")

        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)

    def test_skill_wins_over_department_when_both_are_sent(self):
        """§4: belirli uzmanlık en yüksek önceliktir."""
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        self.post(client, f"/ticket/{ticket_id}/yonlendirme",
                  skill_code="COMPUTER_VISION", department="Ağ", form_token="r9")

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_SPECIFIC_SKILL)
        self.assertEqual(ticket["required_skill_code"], "COMPUTER_VISION")

    def test_routing_without_csrf_is_rejected(self):
        ticket_id = self._queued_triage_ticket()
        client = self.client_as(self.admin_user)

        response = client.post(f"/ticket/{ticket_id}/yonlendirme",
                               data={"skill_code": "COMPUTER_VISION",
                                     "form_token": "r10"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(db.get_ticket(ticket_id)["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)


class TestCsrfStillEnforced(AuthzTestCase):

    def test_post_without_csrf_token_is_rejected(self):
        client = self.client_as(self.admin_user)
        before = len(db.list_technicians())

        response = client.post("/ekip/ekle", data={"name": "CSRF Yok",
                                                   "department": "Yazılım",
                                                   "form_token": "x1"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(db.list_technicians()), before)

    def test_post_with_wrong_csrf_token_is_rejected(self):
        client = self.client_as(self.admin_user)

        response = client.post("/ekip/ekle", data={"name": "Yanlis Token",
                                                   "department": "Yazılım",
                                                   "form_token": "x2",
                                                   "csrf_token": "baska-oturumun-tokeni"})

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(next((t for t in db.list_technicians()
                                if t["name"] == "Yanlis Token"), None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
