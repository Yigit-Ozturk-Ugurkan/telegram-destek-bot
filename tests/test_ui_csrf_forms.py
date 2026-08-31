"""UI / CSRF PAKETİ — ACCEPTANCE REVISION §34.

Acceptance testinde bulunan hata: templates/team.html içindeki
'Geçici şifre ver' ve 'Web hesabı oluştur' formlarında csrf_token EKSİKTİ.
Backend rota testi bunu YAKALAYAMAZ, çünkü rota doğru çalışıyordu — eksik olan
şey render edilen HTML'di.

Bu paket bu yüzden RENDER EDİLMİŞ HTML üzerinde çalışır:
  - Flask test client ile gerçek sayfalar render edilir
  - html.parser ile <form method="post"> elemanları ayrıştırılır
  - her state-changing formda name="csrf_token" aranır

Kırılgan regex KULLANILMAZ; gerçek bir HTML parser kullanılır. Böylece yorum
satırındaki bir 'csrf_token' kelimesi false-positive üretmez ve formatlama
değişiklikleri testi kırmaz.

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ.
"""

from tests import _support, project_file

_support.setup_test_db("ui")

import re
import unittest
from html.parser import HTMLParser

import config
import db
import webapp
from tests._support import DbTestCase, RecordingSender


# ==================================================================
# HTML parser: POST formları ve alanları
# ==================================================================

class FormCollector(HTMLParser):
    """<form method="post"> elemanlarını ve içindeki input adlarını toplar.

    Yalnızca gerçek DOM elemanlarını görür — yorumlar ve metin İÇERİĞİ
    dikkate alınmaz, bu yüzden false-positive üretmez."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            method = (attributes.get("method") or "get").strip().lower()
            form = {"method": method,
                    "action": attributes.get("action") or "",
                    "inputs": []}
            self.forms.append(form)
            self._stack.append(form)
        elif tag == "input" and self._stack:
            name = attributes.get("name")
            if name:
                self._stack[-1]["inputs"].append(name)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "form" and self._stack:
            self._stack.pop()

    @property
    def post_forms(self):
        return [f for f in self.forms if f["method"] == "post"]


def collect_forms(html: str):
    parser = FormCollector()
    parser.feed(html)
    parser.close()
    return parser


class ElementFinder(HTMLParser):
    """Belirli bir CSS sınıfını taşıyan elemanları sayar."""

    def __init__(self, class_name: str):
        super().__init__(convert_charrefs=True)
        self.class_name = class_name
        self.count = 0
        self.tags = []

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if self.class_name in classes:
            self.count += 1
            self.tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def count_class(html: str, class_name: str) -> int:
    finder = ElementFinder(class_name)
    finder.feed(html)
    finder.close()
    return finder.count


# ==================================================================
# Ortak kurulum
# ==================================================================

class UiTestCase(DbTestCase):
    db = db
    config = config

    def setUp(self):
        super().setUp()
        webapp.app.config.update(TESTING=True)
        self.sender = RecordingSender()
        real = webapp.send_telegram_message
        webapp.send_telegram_message = self.sender
        self.addCleanup(setattr, webapp, "send_telegram_message", real)

        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        self.ml = self.make_skill("MACHINE_LEARNING", "Machine Learning")
        self.net = self.make_skill("NETWORK", "Network")

        self.hakan_id = self.make_technician(
            "Hakan Bulut", department="Yazılım", role="Kıdemli Mühendis",
            skills=((self.cv, 1), (self.ml, 2)))
        self.kaan_id = self.make_technician(
            "Kaan Polat", department="Yazılım", skills=((self.cv, 1),))
        self.derya_id = self.make_technician(
            "Derya Uçar", department="Ağ", available=False, skills=((self.net, 1),))

        self.admin_user = self._make_user("admin", config.ROLE_ADMIN, None)
        self.hakan_user = self._make_user("hakan", config.ROLE_TECHNICIAN, self.hakan_id)

        # Bir teknisyene web hesabı ver ki team.html'de 'Geçici şifre ver'
        # formu da render edilsin (eksik CSRF bu formda bulunmuştu).
        self._make_user("kaan", config.ROLE_TECHNICIAN, self.kaan_id)

    def _make_user(self, username, role, technician_id):
        from werkzeug.security import generate_password_hash
        return db.create_web_user(
            username=username,
            password_hash=generate_password_hash("Parola12345!"),
            role=role, full_name=username.title(),
            technician_id=technician_id, must_change_password=False)

    def client_as(self, user_id):
        client = webapp.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = "test-csrf-token"
        return client

    def render(self, user_id, path):
        response = self.client_as(user_id).get(path)
        self.assertEqual(response.status_code, 200, f"{path} -> {response.status_code}")
        return response.get_data(as_text=True)


# ==================================================================
# 38-40: CSRF form coverage (asıl acceptance bug'ı)
# ==================================================================

#: Render edilip taranacak sayfalar: (yol, kullanıcı anahtarı)
ADMIN_PAGES = ["/tickets", "/tickets?status=active", "/tickets?status=resolved",
               "/tickets?status=eskalasyon_bekliyor", "/ekip", "/uzmanliklar",
               "/sla-takvimi", "/sifre-degistir"]
TECHNICIAN_PAGES = ["/my-tickets", "/my-tickets?status=waiting",
                    "/my-tickets?status=resolved", "/collaborations",
                    "/sifre-degistir"]


class TestRenderedFormCsrfCoverage(UiTestCase):
    """Render edilen HER state-changing formda csrf_token bulunmalıdır."""

    def _assert_all_post_forms_protected(self, html, label):
        forms = collect_forms(html).post_forms
        self.assertTrue(forms, f"{label}: hiç POST formu render edilmedi "
                               "(test anlamsız kalmasın diye kontrol ediliyor).")
        for form in forms:
            self.assertIn("csrf_token", form["inputs"],
                          f"{label}: '{form['action']}' formunda csrf_token EKSİK.")

    def test_38_all_admin_pages_post_forms_have_csrf(self):
        for path in ADMIN_PAGES:
            with self.subTest(path=path):
                self._assert_all_post_forms_protected(
                    self.render(self.admin_user, path), path)

    def test_38b_all_technician_pages_post_forms_have_csrf(self):
        for path in TECHNICIAN_PAGES:
            with self.subTest(path=path):
                self._assert_all_post_forms_protected(
                    self.render(self.hakan_user, path), path)

    def test_38c_ticket_detail_post_forms_have_csrf(self):
        """Detay sayfası en çok form içeren sayfadır; her rol için taranır."""
        ticket_id = self.make_ticket(subject="Detay formlari")
        db.assign_primary_if_unassigned(ticket_id, self.hakan_id, "Hakan Bulut")

        for user_id, label in ((self.admin_user, "admin"), (self.hakan_user, "primary")):
            with self.subTest(role=label):
                self._assert_all_post_forms_protected(
                    self.render(user_id, f"/ticket/{ticket_id}"),
                    f"/ticket/<id> ({label})")

    def test_38d_closed_ticket_detail_post_forms_have_csrf(self):
        """Kapalı talepte farklı formlar render edilir (yeniden aç)."""
        ticket_id = self.make_ticket()
        db.assign_primary_if_unassigned(ticket_id, self.hakan_id, "Hakan Bulut")
        db.resolve_ticket_if_open(ticket_id, source=config.ASSIGN_SOURCE_WEB)

        self._assert_all_post_forms_protected(
            self.render(self.admin_user, f"/ticket/{ticket_id}"), "kapali talep")

    def test_38e_login_page_post_form_has_csrf(self):
        response = webapp.app.test_client().get("/login")
        self._assert_all_post_forms_protected(response.get_data(as_text=True), "/login")

    def test_39_temporary_password_form_has_csrf(self):
        """ACCEPTANCE BUG: 'Geçici şifre ver' formunda csrf_token eksikti."""
        html = self.render(self.admin_user, "/ekip")
        target = f"/ekip/{self.kaan_id}/gecici-sifre"

        forms = [f for f in collect_forms(html).post_forms if f["action"] == target]
        self.assertEqual(len(forms), 1, f"'{target}' formu render edilmedi.")
        self.assertIn("csrf_token", forms[0]["inputs"])
        self.assertIn("form_token", forms[0]["inputs"],
                      "Çift gönderim koruması da korunmalı.")
        self.assertIn("temp_password", forms[0]["inputs"])

    def test_40_create_account_form_has_csrf(self):
        """ACCEPTANCE BUG: 'Web hesabı oluştur' formunda csrf_token eksikti."""
        html = self.render(self.admin_user, "/ekip")
        target = f"/ekip/{self.derya_id}/hesap"      # Derya'nın web hesabı YOK

        forms = [f for f in collect_forms(html).post_forms if f["action"] == target]
        self.assertEqual(len(forms), 1, f"'{target}' formu render edilmedi.")
        self.assertIn("csrf_token", forms[0]["inputs"])
        self.assertIn("form_token", forms[0]["inputs"])
        self.assertIn("username", forms[0]["inputs"])

    def test_40b_parser_would_actually_catch_a_missing_token(self):
        """Testin gerçekten koruduğunu doğrular (meta-test).

        csrf_token'ı olmayan bir form verildiğinde tarayıcı bunu YAKALAMALI —
        aksi hâlde bu paket sessizce hiçbir şey doğrulamıyor olurdu."""
        broken = ('<form method="post" action="/x">'
                  '<input type="hidden" name="form_token" value="a">'
                  '<button type="submit">Gönder</button></form>')
        forms = collect_forms(broken).post_forms
        self.assertEqual(len(forms), 1)
        self.assertNotIn("csrf_token", forms[0]["inputs"])

    def test_40c_comment_mentioning_csrf_is_not_counted(self):
        """Yorum satırındaki 'csrf_token' kelimesi false-positive ÜRETMEZ."""
        tricky = ('<form method="post" action="/y">'
                  '<!-- csrf_token buraya eklenmeli -->'
                  '<input type="hidden" name="form_token" value="a"></form>')
        forms = collect_forms(tricky).post_forms
        self.assertNotIn("csrf_token", forms[0]["inputs"],
                         "Parser yorumları form alanı sanmamalı.")

    def test_get_forms_are_not_required_to_carry_csrf(self):
        """GET formları state değiştirmez; gereksiz kısıt getirmiyoruz."""
        html = self.render(self.admin_user, "/tickets")
        get_forms = [f for f in collect_forms(html).forms if f["method"] != "post"]
        for form in get_forms:
            self.assertNotIn("csrf_token", form["inputs"])


# ==================================================================
# LAYOUT REVİZYONU
#
# FRONTEND DESIGN REVISION ile sidebar KALDIRILDI ve üç kolonlu Talepler
# ekranına geçildi. Eski sidebar/rail testleri (33-37f) artık geçersiz
# ACCEPTANCE kriterlerini kodluyordu; yenileri kalıcı olarak
# tests/test_frontend_design.py paketindedir.
#
# Bu pakette YALNIZCA form güvenliği (CSRF/form_token), kuyruk dayanağı UX'i
# ve "kullanıcıya teknisyen seçtirilmez" izleri kalır.
# ==================================================================

# ==================================================================
# §28: ADMIN KUYRUK UX — bekleme nedeni ayırt edilebilir
# ==================================================================

class TestAdminQueueRoutingBasisUx(UiTestCase):
    """Admin, bir talebin NEDEN atanmadığını satıra bakarak anlayabilmeli."""

    def _queue(self, basis, skill_code=None, department=None, subject="Kuyruk"):
        ticket_id = self.make_ticket(subject=subject)
        db.mark_escalation_pending(ticket_id, department=department)
        db.set_ticket_routing(ticket_id, basis, skill_code=skill_code,
                              department=department)
        return ticket_id

    def _queue_page(self):
        return self.render(self.admin_user,
                           "/tickets?status=eskalasyon_bekliyor")

    def test_specific_skill_row_names_the_awaited_skill(self):
        self._queue(config.ROUTING_BASIS_SPECIFIC_SKILL,
                    skill_code="COMPUTER_VISION", department="Yazılım",
                    subject="Kamera algılamıyor")
        html = self._queue_page()

        self.assertIn("Uzman bekliyor", html)
        self.assertIn("Görüntü İşleme", html)

    def test_department_row_names_the_awaited_department(self):
        self._queue(config.ROUTING_BASIS_DEPARTMENT, department="Ağ",
                    subject="VPN kurulamıyor")
        html = self._queue_page()

        self.assertIn("Departman bekliyor", html)

    def test_general_support_row_is_labelled_as_general(self):
        db.create_skill(config.GENERAL_SUPPORT_SKILL_CODE,
                        config.GENERAL_SUPPORT_SKILL_NAME)
        self._queue(config.ROUTING_BASIS_GENERAL_SUPPORT,
                    skill_code=config.GENERAL_SUPPORT_SKILL_CODE,
                    subject="Bilgisayar yavaş")
        html = self._queue_page()

        self.assertIn("Genel destek bekliyor", html)

    def test_manual_triage_row_asks_for_manual_routing(self):
        self._queue(config.ROUTING_BASIS_MANUAL_TRIAGE, subject="Belirsiz talep")
        html = self._queue_page()

        self.assertIn("Manuel sınıflandırma gerekiyor", html)
        self.assertIn("Belirsiz talep", html)

    def test_queue_page_explains_that_general_is_not_a_catch_all(self):
        self._queue(config.ROUTING_BASIS_MANUAL_TRIAGE)
        html = self._queue_page()

        self.assertIn("genel desteğe düşmez", html)

    def test_queue_can_be_filtered_by_routing_basis(self):
        # Konular BİLEREK ayırt edici: sayfadaki sabit metinlere (başlık,
        # tablo caption'ı, filtre etiketleri) benzeyen bir konu, yanlışlıkla
        # geçen bir assertNotIn üretirdi.
        self._queue(config.ROUTING_BASIS_SPECIFIC_SKILL,
                    skill_code="COMPUTER_VISION", subject="ALFA-CV-KONUSU")
        self._queue(config.ROUTING_BASIS_MANUAL_TRIAGE,
                    subject="BETA-TRIYAJ-KONUSU")

        html = self.render(
            self.admin_user,
            "/tickets?status=eskalasyon_bekliyor&basis=MANUAL_TRIAGE")

        self.assertIn("BETA-TRIYAJ-KONUSU", html)
        self.assertNotIn("ALFA-CV-KONUSU", html)

    def test_invalid_basis_filter_falls_back_to_the_whole_queue(self):
        self._queue(config.ROUTING_BASIS_SPECIFIC_SKILL,
                    skill_code="COMPUTER_VISION", subject="ALFA-CV-KONUSU")

        html = self.render(self.admin_user,
                           "/tickets?status=eskalasyon_bekliyor&basis=UYDURMA")

        self.assertIn("ALFA-CV-KONUSU", html)

    def test_ticket_detail_shows_the_routing_basis(self):
        """§13: talep detayında 'Yönlendirme' satırı bulunur."""
        ticket_id = self._queue(config.ROUTING_BASIS_SPECIFIC_SKILL,
                                skill_code="COMPUTER_VISION",
                                department="Yazılım")
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")

        self.assertIn("Yönlendirme", html)
        self.assertIn("Uzman bekliyor", html)
        self.assertIn("Görüntü İşleme", html)

    def test_manual_triage_detail_offers_a_routing_form(self):
        """§8: admin uzmanlık/departman atayabilmeli."""
        ticket_id = self._queue(config.ROUTING_BASIS_MANUAL_TRIAGE)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")

        self.assertIn(f"/ticket/{ticket_id}/yonlendirme", html)
        self.assertIn("Manuel sınıflandırma gerekiyor", html)
        self.assertIn("Otomatik olarak genel desteğe", html)

    def test_routing_form_carries_csrf_and_form_token(self):
        ticket_id = self._queue(config.ROUTING_BASIS_MANUAL_TRIAGE)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")

        forms = [f for f in collect_forms(html).post_forms
                 if f["action"].endswith("/yonlendirme")]
        self.assertEqual(len(forms), 1)
        self.assertIn("csrf_token", forms[0]["inputs"])
        self.assertIn("form_token", forms[0]["inputs"])

    def test_technician_detail_has_no_routing_form(self):
        """Yönlendirme yalnızca admindedir (buton gizleme + rota koruması)."""
        ticket_id = self._queue(config.ROUTING_BASIS_MANUAL_TRIAGE)
        db.assign_primary_if_unassigned(ticket_id, self.hakan_id, "Hakan Bulut")

        html = self.render(self.hakan_user, f"/ticket/{ticket_id}")
        self.assertNotIn("/yonlendirme", html)


# ==================================================================
# Kullanıcıya seçim sunulmadığının UI tarafındaki izi
# ==================================================================

class TestNoUserTechnicianChoice(UiTestCase):

    def test_queue_view_explains_automatic_assignment(self):
        # Açıklama satırı yalnızca kuyrukta talep varken render edilir.
        self.queue_ticket("COMPUTER_VISION", subject="Kuyrukta bekleyen")
        html = self.render(self.admin_user, "/tickets?status=eskalasyon_bekliyor")

        self.assertIn("Kuyrukta bekleyen", html)
        self.assertIn("otomatik olarak", html)
        self.assertNotIn("kullanıcıya seçim sunulur", html)
        self.assertNotIn("sessiz atama yapmaz", html)

    def test_bot_module_defines_no_candidate_callback(self):
        """'tech:<ticket>:<technician>' callback'i ARTIK ÜRETİLMEZ."""
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('callback_data=f"tech:', source)
        self.assertIn("handle_stale_candidate_callback", source,
                      "Eski butonlar için güvenli no-op korunmalı.")

    def test_queue_service_defines_no_candidate_keyboard(self):
        with open(project_file("queue_service.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("inline_keyboard", source)
        self.assertNotIn("candidate_keyboard_payload", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
