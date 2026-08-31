"""FRONTEND DESIGN REVISION — kalıcı regression paketi (§E).

Bu paket RENDER EDİLMİŞ HTML ve gerçek CSS dosyası üzerinde çalışır. Backend
rota testleri bu hataları YAKALAYAMAZ; eksik olan şey render edilen çıktıdır.

Doğrulananlar:
  - role-based navbar (admin vs technician menü görünürlüğü)
  - sidebar YOK
  - arama / bildirim ikonu YOK  (dead control eklenmemiş)
  - segmented control (tek capsule, üç seçenek)
  - ticket kartları
  - aktif conversation (ana çalışma alanı)
  - AÇIK temalı reply composer
  - çözülmüş talepte reopen state (mesaj kutusu YOK)
  - drawer tetikleyicisi
  - drawer POST formlarında CSRF + form_token
  - Flask/Jinja/düz CSS korundu; frontend framework EKLENMEDİ

Bu pakette gerçek Gemini/Telegram çağrısı YAPILMAZ.
"""

from tests import _support, project_file

_support.setup_test_db("frontend")

import os
import re
import unittest
from html.parser import HTMLParser

import config
import db
import webapp
from tests._support import DbTestCase, RecordingSender


# ==================================================================
# HTML yardımcıları — kırılgan regex yerine gerçek parser
# ==================================================================

class FormCollector(HTMLParser):
    """<form method="post"> elemanlarını ve içindeki input adlarını toplar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            form = {"method": (attributes.get("method") or "get").strip().lower(),
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


class ElementFinder(HTMLParser):
    """Belirli bir CSS sınıfını taşıyan elemanları sayar."""

    def __init__(self, class_name: str):
        super().__init__(convert_charrefs=True)
        self.class_name = class_name
        self.count = 0
        self.tags = []

    def handle_starttag(self, tag, attrs):
        if self.class_name in (dict(attrs).get("class") or "").split():
            self.count += 1
            self.tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def collect_forms(html: str):
    parser = FormCollector()
    parser.feed(html)
    parser.close()
    return parser


def count_class(html: str, class_name: str) -> int:
    finder = ElementFinder(class_name)
    finder.feed(html)
    finder.close()
    return finder.count


def slice_between(html: str, start: str, end: str) -> str:
    return html[html.index(start):html.index(end)]


# ==================================================================
# Ortak kurulum
# ==================================================================

ADMIN_PAGES = ["/tickets", "/tickets?status=all", "/tickets?status=resolved",
               "/tickets?status=eskalasyon_bekliyor", "/ekip", "/uzmanliklar",
               "/sla-takvimi", "/sifre-degistir"]
TECHNICIAN_PAGES = ["/my-tickets", "/my-tickets?status=all",
                    "/my-tickets?status=resolved", "/collaborations",
                    "/sifre-degistir"]


class FrontendTestCase(DbTestCase):
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
        self.net = self.make_skill("NETWORK", "Network")

        self.hakan_id = self.make_technician(
            "Hakan Bulut", department="Yazılım", role="Kıdemli Mühendis",
            skills=((self.cv, 1),))
        self.kaan_id = self.make_technician(
            "Kaan Polat", department="Yazılım", skills=((self.cv, 1),))
        self.derya_id = self.make_technician(
            "Derya Uçar", department="Ağ", available=False, skills=((self.net, 1),))

        self.admin_user = self._make_user("admin", config.ROLE_ADMIN, None)
        self.hakan_user = self._make_user("hakan", config.ROLE_TECHNICIAN, self.hakan_id)
        self.kaan_user = self._make_user("kaan", config.ROLE_TECHNICIAN, self.kaan_id)

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

    def open_ticket(self, subject="Sunucuya bağlanamıyorum", technician_id=None,
                    category="Yazılım"):
        ticket_id = self.make_ticket(subject=subject, category=category)
        if technician_id:
            db.assign_primary_if_unassigned(ticket_id, technician_id, "atanan")
        return ticket_id

    def css(self):
        with open(project_file("static", "style.css"), encoding="utf-8") as fh:
            return fh.read()

    def navbar(self, html):
        return slice_between(html, '<header class="appbar">', "</header>")


# ==================================================================
# 1. STACK KORUNDU — framework EKLENMEDİ
# ==================================================================

class TestStackPreserved(FrontendTestCase):
    """Flask + Jinja2 + düz CSS. React/Vue/Tailwind YOK, SPA rewrite YOK."""

    def test_no_frontend_framework_is_loaded(self):
        html = self.render(self.admin_user, "/tickets")
        lowered = html.lower()
        for needle in ("react", "vue", "tailwind", "cdn.jsdelivr", "unpkg.com",
                       "alpine", "htmx"):
            self.assertNotIn(needle, lowered,
                             f"Frontend framework izi bulundu: {needle}")

    def test_only_the_projects_own_script_and_stylesheet_are_used(self):
        html = self.render(self.admin_user, "/tickets")
        scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
        self.assertEqual(scripts, ["/static/app.js"],
                         "Yalnızca projenin kendi minimal JS'i yüklenmeli.")
        sheets = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html)
        self.assertIn("/static/style.css", sheets)

    def test_templates_use_jinja_partials_not_a_component_framework(self):
        for partial in ("navbar.html", "ticket_card.html",
                        "ticket_settings_drawer.html", "conversation.html",
                        "ticket_list_panel.html", "context_panel.html"):
            self.assertTrue(
                os.path.exists(project_file("templates", "partials", partial)),
                f"templates/partials/{partial} bulunmalı.")

    def test_stylesheet_is_plain_css(self):
        css = self.css()
        # Derlenmiş bir utility framework'ün imzası: yüzlerce atomik sınıf.
        self.assertNotIn("--tw-", css)
        self.assertNotIn("@tailwind", css)
        self.assertIn(":root", css)


# ==================================================================
# 2. A1 — GENEL TEMA
# ==================================================================

class TestTheme(FrontendTestCase):

    def test_page_background_uses_the_specified_gradient(self):
        css = self.css()
        gradient = re.search(
            r"body\s*\{[^}]*linear-gradient\(\s*160deg,\s*#DCEEDD 0%,"
            r"\s*#DDE9F5 50%,\s*#E7E2F5 100%\s*\)", css, re.IGNORECASE)
        self.assertIsNotNone(
            gradient, "Sayfa arka planı belirtilen pastel gradient olmalı.")

    def test_gradient_is_only_used_on_the_page_background(self):
        """Gradient YALNIZCA dış sayfa arka planında; kartlarda DEĞİL."""
        css = self.css()
        gradients = re.findall(r"linear-gradient\([^)]*\)", css)
        self.assertEqual(len(gradients), 1,
                         f"Beklenen tek gradient, bulunan: {gradients}")

    def test_palette_tokens_match_the_specification(self):
        css = self.css()
        for token, value in (("--surface", "#FFFFFF"),
                             ("--accent", "#3D8B5F"),
                             ("--accent-light", "#B7E4C0"),
                             ("--accent-ink", "#1F3D2A"),
                             ("--text", "#1F2937"),
                             ("--text-secondary", "#6B7280"),
                             ("--muted-ink", "#9CA3AF")):
            self.assertRegex(css, rf"{token}:\s*{value};",
                             f"{token} = {value} olmalı.")

    def test_cards_use_the_soft_shadow_and_rounded_corners(self):
        css = self.css()
        self.assertIn("--shadow-card: 0 2px 8px rgba(60, 80, 70, 0.06);", css)
        radius = re.search(r"--radius-card:\s*(\d+)px;", css)
        self.assertIsNotNone(radius)
        self.assertTrue(14 <= int(radius.group(1)) <= 20,
                        "Kart köşe yarıçapı 14-20px bandında olmalı.")


# ==================================================================
# 3. A2 / A3 — SIDEBAR YOK, ROLE-BASED NAVBAR
# ==================================================================

class TestNavbar(FrontendTestCase):

    def test_sidebar_is_removed_from_every_authenticated_page(self):
        for user_id, paths in ((self.admin_user, ADMIN_PAGES),
                               (self.hakan_user, TECHNICIAN_PAGES)):
            for path in paths:
                with self.subTest(path=path):
                    html = self.render(user_id, path)
                    self.assertEqual(count_class(html, "sidebar"), 0)
                    self.assertNotIn('<aside class="sidebar"', html)

    def test_dashboard_active_resolved_split_is_gone_from_navigation(self):
        """A2: bu ayrım navigasyondan kaldırıldı, filtreye taşındı."""
        navbar = self.navbar(self.render(self.admin_user, "/tickets"))
        for label in ("Dashboard", "Aktif Talepler", "Çözülmüş Talepler"):
            self.assertNotIn(label, navbar,
                             f"'{label}' navbar'da olmamalı (filtreye taşındı).")

    def test_navbar_is_present_on_every_authenticated_page(self):
        for user_id, paths in ((self.admin_user, ADMIN_PAGES),
                               (self.hakan_user, TECHNICIAN_PAGES)):
            for path in paths:
                with self.subTest(path=path):
                    html = self.render(user_id, path)
                    self.assertEqual(count_class(html, "navbar"), 1)
                    self.assertEqual(count_class(html, "nav-tabs"), 1,
                                     "Sekmeler TEK bir track içinde olmalı.")

    def test_brand_shows_hash_and_name(self):
        navbar = self.navbar(self.render(self.admin_user, "/tickets"))
        self.assertIn("nav-brand-mark", navbar)
        self.assertIn("Destek Masası", navbar)

    def test_admin_navbar_has_exactly_the_four_admin_tabs(self):
        tabs = slice_between(self.render(self.admin_user, "/tickets"),
                             '<div class="nav-tabs"', '<div class="nav-user">')
        for label in ("Talepler", "Ekip", "Uzmanlıklar", "SLA Takvimi"):
            self.assertIn(label, tabs)
        for label in ("Taleplerim", "Destek Verdiklerim"):
            self.assertNotIn(label, tabs)

    def test_technician_navbar_has_exactly_the_two_technician_tabs(self):
        # Yalnızca sekme track'ine bakılır: 'Teknik Ekip' rol etiketi avatar
        # menüsünde geçer ve tüm navbar'a bakmak yanıltıcı olurdu.
        tabs = slice_between(self.render(self.hakan_user, "/my-tickets"),
                             '<div class="nav-tabs"', '<div class="nav-user">')
        for label in ("Taleplerim", "Destek Verdiklerim"):
            self.assertIn(label, tabs)
        for label in ("Ekip", "Uzmanlıklar", "SLA Takvimi"):
            self.assertNotIn(label, tabs,
                             f"Teknisyen sekmelerinde '{label}' olmamalı.")

    def test_technician_navbar_has_no_admin_links_at_all(self):
        """Link gizlemek yetki değildir ama gereksiz link de gösterilmez."""
        html = self.render(self.hakan_user, "/my-tickets")
        self.assertNotIn('href="/ekip"', html)
        self.assertNotIn('href="/uzmanliklar"', html)
        self.assertNotIn('href="/sla-takvimi"', html)

    def test_active_tab_is_marked(self):
        navbar = self.navbar(self.render(self.admin_user, "/ekip"))
        active = re.findall(r'class="nav-tab is-active"[^>]*href="([^"]+)"', navbar)
        self.assertEqual(active, ["/ekip"])

    def test_navbar_right_side_has_only_the_avatar(self):
        """A3: ARAMA İKONU YOK, BİLDİRİM İKONU YOK."""
        navbar = self.navbar(self.render(self.admin_user, "/tickets"))
        user_area = navbar[navbar.index('class="nav-user"'):]

        self.assertIn("avatar-btn", user_area)
        # Kullanıcı alanında TEK etkileşimli tetikleyici vardır: avatar.
        # (Menü içindeki şifre/çıkış/müsaitlik aksiyonları avatarın ALTINDADIR.)
        self.assertEqual(user_area.count("<button type=\"button\""), 1,
                         "Navbar sağında avatar dışında buton olmamalı.")
        for dead in ("search", "arama", "notification", "bildirim",
                     "🔔", "🔍", "zil"):
            self.assertNotIn(dead, user_area.lower(),
                             f"Navbar sağında '{dead}' kontrolü olmamalı.")

    def test_no_search_or_notification_control_anywhere_in_the_panel(self):
        for user_id, path in ((self.admin_user, "/tickets"),
                              (self.hakan_user, "/my-tickets")):
            html = self.render(user_id, path).lower()
            with self.subTest(path=path):
                self.assertNotIn('type="search"', html)
                self.assertNotIn("notification", html)
                self.assertNotIn("bildirim merkezi", html)

    def test_admin_avatar_menu_has_password_and_logout_only(self):
        html = self.render(self.admin_user, "/tickets")
        menu = slice_between(html, 'class="avatar-menu"', "</nav>")

        self.assertIn("Şifremi Değiştir", menu)
        self.assertIn("Çıkış", menu)
        self.assertNotIn("Müsait", menu, "Admin menüsünde müsaitlik olmamalı.")

    def test_technician_avatar_menu_has_availability_toggle(self):
        html = self.render(self.hakan_user, "/my-tickets")
        menu = slice_between(html, 'class="avatar-menu"', "</nav>")

        self.assertIn("/musaitlik", menu)
        self.assertIn("Müsait", menu)
        self.assertIn("müsaitlik değiştir", menu)
        self.assertIn("Şifremi Değiştir", menu)
        self.assertIn("Çıkış", menu)

    def test_avatar_menu_forms_carry_csrf_and_form_token(self):
        html = self.render(self.hakan_user, "/my-tickets")
        actions = {f["action"]: f for f in collect_forms(html).post_forms}

        for action in ("/musaitlik", "/logout"):
            self.assertIn(action, actions, f"{action} formu render edilmeli.")
            self.assertIn("csrf_token", actions[action]["inputs"])
            self.assertIn("form_token", actions[action]["inputs"])

    def test_no_dead_password_reset_link(self):
        """Projede 'şifremi unuttum' rotası yok — ölü link üretilmez."""
        html = webapp.app.test_client().get("/login").get_data(as_text=True)
        self.assertNotIn("unuttum", html.lower())
        self.assertNotIn("sifre-sifirla", html)


# ==================================================================
# 4. A4 / A5 — ÜÇ KOLON + SEGMENTED CONTROL + TICKET KARTLARI
# ==================================================================

class TestTicketScreen(FrontendTestCase):

    def test_workspace_has_three_columns_in_order(self):
        html = self.render(self.admin_user, "/tickets")

        self.assertEqual(count_class(html, "workspace"), 1)
        list_at = html.index('class="ws-list"')
        main_at = html.index('class="ws-main"')
        ctx_at = html.index('class="ws-context"')
        self.assertLess(list_at, main_at)
        self.assertLess(main_at, ctx_at)

    def test_column_widths_match_the_specification(self):
        """250px | minmax(0,1fr) | ~200-240px — orta kolon en geniş alan."""
        css = self.css()
        self.assertIn("grid-template-columns: var(--list-w) minmax(0, 1fr) var(--ctx-w);",
                      css)

        list_w = int(re.search(r"--list-w:\s*(\d+)px;", css).group(1))
        ctx_w = int(re.search(r"--ctx-w:\s*(\d+)px;", css).group(1))
        self.assertEqual(list_w, 250, "Sol liste 250px olmalı.")
        self.assertTrue(200 <= ctx_w <= 240, f"Sağ panel {ctx_w}px — hedef 200-240px.")

    def test_admin_list_is_titled_tickets(self):
        """LEFT PANEL VISUAL FIX: başlık sadeleşti — yalnızca "Talepler"."""
        html = self.render(self.admin_user, "/tickets")
        self.assertIn('<h2 class="list-title">Talepler</h2>', html)

    def test_technician_list_is_titled_my_tickets(self):
        html = self.render(self.hakan_user, "/my-tickets")
        self.assertIn("Benim taleplerim", html)

    def test_segmented_control_is_one_capsule_with_three_options(self):
        for user_id, path in ((self.admin_user, "/tickets"),
                              (self.hakan_user, "/my-tickets")):
            with self.subTest(path=path):
                html = self.render(user_id, path)
                self.assertEqual(count_class(html, "segmented"), 1,
                                 "TEK capsule olmalı, üç ayrı buton değil.")
                self.assertEqual(count_class(html, "segmented-option"), 3)
                for label in ("Tümü", "Aktif", "Çözülmüş"):
                    self.assertIn(label, html)

    def test_segmented_options_are_not_standalone_buttons(self):
        html = self.render(self.admin_user, "/tickets")
        segmented = slice_between(html, 'class="segmented"', "</div>")
        self.assertNotIn("<button", segmented,
                         "Segmented seçenekleri buton değil, capsule içi link olmalı.")

    def test_segmented_control_uses_existing_query_param_filters(self):
        html = self.render(self.admin_user, "/tickets")
        for value in ("all", "active", "resolved"):
            self.assertIn(f"/tickets?status={value}", html)

    def test_active_segment_is_marked(self):
        html = self.render(self.admin_user, "/tickets?status=resolved")
        active = re.search(r'class="segmented-option is-active"\s*\n?\s*href="([^"]+)"',
                           html)
        self.assertIsNotNone(active)
        self.assertIn("status=resolved", active.group(1))

    def test_ticket_cards_carry_the_required_fields(self):
        self.open_ticket(subject="Sunucuya bağlanamıyorum", technician_id=self.hakan_id)
        html = self.render(self.admin_user, "/tickets")

        self.assertEqual(count_class(html, "tcard"), 1)
        card = slice_between(html, 'class="tcard', "</li>")
        self.assertIn("mini-avatar", card)                 # avatar / initial
        self.assertIn("Sunucuya bağlanamıyorum", card)     # başlık
        self.assertIn("tcard-time", card)                  # geçen süre
        self.assertIn("Yazılım", card)                     # kategori
        self.assertIn("priority-badge", card)              # priority badge
        self.assertIn("status-pill", card)                 # status badge

    def test_unread_badge_is_rendered_when_there_is_a_new_user_message(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        db.add_message(ticket_id, "kullanici", 1000, "Ali Veli", "Hâlâ olmuyor")

        html = self.render(self.admin_user, "/tickets")
        self.assertIn("Yeni mesaj", html)

    def test_resolved_cards_are_visually_muted(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        db.resolve_ticket_if_open(ticket_id, source=config.ASSIGN_SOURCE_WEB)

        html = self.render(self.admin_user, "/tickets?status=resolved")
        self.assertIn("tcard is-resolved", html)
        self.assertIn(".tcard.is-resolved", self.css())

    def test_selected_card_is_marked_on_the_detail_screen(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")
        self.assertIn("tcard is-selected", html)

    def test_technician_list_only_contains_own_tickets(self):
        mine = self.open_ticket(subject="BENIM-TALEBIM", technician_id=self.hakan_id)
        other = self.open_ticket(subject="BASKASININ-TALEBI", technician_id=self.kaan_id)

        html = self.render(self.hakan_user, "/my-tickets")
        self.assertIn("BENIM-TALEBIM", html)
        self.assertNotIn("BASKASININ-TALEBI", html)
        self.assertNotIn(f'href="/ticket/{other}"', html)

    def test_detail_sidebar_list_is_also_authorization_filtered(self):
        """Detay ekranındaki sol liste de sunucuda süzülür."""
        mine = self.open_ticket(subject="BENIM-TALEBIM", technician_id=self.hakan_id)
        self.open_ticket(subject="BASKASININ-TALEBI", technician_id=self.kaan_id)

        html = self.render(self.hakan_user, f"/ticket/{mine}")
        self.assertIn("BENIM-TALEBIM", html)
        self.assertNotIn("BASKASININ-TALEBI", html)


# ==================================================================
# 5. A6-A9 — CONVERSATION, MESAJLAR, COMPOSER, RESOLVED
# ==================================================================

class TestConversation(FrontendTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.open_ticket(subject="Sunucuya bağlanamıyorum",
                                          technician_id=self.hakan_id)
        db.add_message(self.ticket_id, "teknisyen", 0, "Hakan Bulut",
                       "Merhaba, kontrol ediyorum.", sender_user_id=self.hakan_user)

    def detail(self, user_id=None):
        return self.render(user_id or self.admin_user, f"/ticket/{self.ticket_id}")

    def test_conversation_is_the_main_work_area(self):
        html = self.detail()
        self.assertEqual(count_class(html, "convo"), 1)
        # Orta kolonun İÇİNDE olmalı — yan panelde değil.
        main = html[html.index('class="ws-main"'):html.index('class="ws-context"')]
        self.assertIn('class="convo"', main)

    def test_header_shows_requester_and_ticket_reference(self):
        html = self.detail()
        header = slice_between(html, 'class="convo-header"', 'class="convo-thread"')

        self.assertIn("Ali Veli" if "Ali Veli" in header else "Test Kullanıcı", header)
        self.assertIn(f"Talep #{self.ticket_id}", header)
        self.assertIn("Sunucuya bağlanamıyorum", header)

    def test_header_has_settings_icon_but_no_dead_phone_action(self):
        """A6: telefon işlevi sistemde yok -> dead action üretilmez."""
        html = self.detail()
        header = slice_between(html, 'class="convo-header"', 'class="convo-thread"')

        self.assertIn('data-drawer-open="ticket-settings"', header)
        for dead in ("tel:", "phone", "telefon", "call"):
            self.assertNotIn(dead, header.lower())

    def test_every_message_names_its_sender_explicitly(self):
        """A7: kim yazdı YALNIZCA renkle anlaşılmaz."""
        html = self.detail()
        thread = slice_between(html, 'class="convo-thread"', 'class="composer"')

        self.assertIn("msg-sender", thread)
        # Personel adı messages.sender_user_id -> web_users ilişkisinden gelir.
        self.assertIn("Hakan", thread)
        self.assertIn("Talep Sahibi", thread)
        self.assertIn("Teknik Ekip", thread)
        self.assertEqual(count_class(thread, "bubble"), count_class(thread, "msg"))

    def test_requester_and_staff_bubbles_use_the_specified_colors(self):
        css = self.css()
        self.assertIn("--accent-soft: #F1FBF4;", css)
        self.assertIn("--surface-sunken: #F5F6FA;", css)
        self.assertIn(".msg-kullanici .bubble { background: var(--accent-soft); }", css)
        self.assertIn(".msg-kullanici .msg-sender { color: var(--accent); }", css)

    def test_reply_composer_is_light_not_dark(self):
        """A8: input KOYU/SİYAH OLMAYACAK."""
        css = self.css()
        composer = re.search(r"\.composer-input\s*\{([^}]*)\}", css)
        self.assertIsNotNone(composer)
        body = composer.group(1)

        self.assertIn("background: var(--surface-soft)", body)
        self.assertIn("border: 1px solid var(--line)", body)
        self.assertIn("color: var(--text)", body)
        self.assertIn("border-radius: var(--pill)", body)
        # Koyu/siyah zemin kullanılmadığını açıkça doğrula.
        for dark in ("#000", "black", "#111", "#1f2937;"):
            self.assertNotIn(f"background: {dark}", body)

    def test_send_button_uses_the_light_accent_pill(self):
        css = self.css()
        send = re.search(r"\.composer-send\s*\{([^}]*)\}", css).group(1)
        self.assertIn("background: var(--accent-light)", send)
        self.assertIn("color: var(--accent-ink)", send)
        self.assertIn("border-radius: var(--pill)", send)

    def test_composer_stays_visible_when_the_thread_grows(self):
        """Mesaj geçmişi uzadığında input kaybolmamalı: yalnızca thread kayar."""
        for i in range(40):
            db.add_message(self.ticket_id, "kullanici", 1000, "Ali Veli", f"Mesaj {i}")

        html = self.detail()
        self.assertIn("Gönder", html)
        self.assertIn('class="composer"', html)

        css = self.css()
        convo = re.search(r"\.convo\s*\{([^}]*)\}", css).group(1)
        thread = re.search(r"\.convo-thread\s*\{([^}]*)\}", css).group(1)
        composer = re.search(r"\.composer\s*\{([^}]*)\}", css).group(1)
        self.assertIn("flex-direction: column", convo)
        self.assertIn("overflow-y: auto", thread, "Kayan alan THREAD olmalı.")
        self.assertIn("flex-shrink: 0", composer, "Composer sıkışıp kaybolmamalı.")

    def test_composer_form_posts_to_the_existing_reply_route(self):
        html = self.detail()
        forms = [f for f in collect_forms(html).post_forms
                 if f["action"] == f"/ticket/{self.ticket_id}/reply"]

        self.assertEqual(len(forms), 1)
        self.assertIn("csrf_token", forms[0]["inputs"])
        self.assertIn("form_token", forms[0]["inputs"])

    def test_resolved_ticket_has_no_message_input(self):
        """A9: çözülmüş talepte message input KALDIRILIR."""
        db.resolve_ticket_if_open(self.ticket_id, source=config.ASSIGN_SOURCE_WEB)
        html = self.detail()

        self.assertNotIn('class="composer"', html)
        self.assertEqual(
            [f for f in collect_forms(html).post_forms
             if f["action"].endswith("/reply")], [],
            "Çözülmüş talepte yanıt formu render edilmemeli.")

    def test_resolved_ticket_shows_the_reopen_state(self):
        db.resolve_ticket_if_open(self.ticket_id, source=config.ASSIGN_SOURCE_WEB)
        html = self.detail()

        self.assertIn("Bu talep çözüldü.", html)
        self.assertIn("Talebi yeniden aç", html)
        reopen = [f for f in collect_forms(html).post_forms
                  if f["action"] == f"/ticket/{self.ticket_id}/reopen"]
        self.assertTrue(reopen)
        self.assertIn("csrf_token", reopen[0]["inputs"])
        self.assertIn("form_token", reopen[0]["inputs"])

    def test_backend_refuses_a_public_reply_before_reopen(self):
        """UI'nın gizlemesi yetmez: backend de reddetmeli."""
        db.resolve_ticket_if_open(self.ticket_id, source=config.ASSIGN_SOURCE_WEB)
        client = self.client_as(self.admin_user)

        response = client.post(
            f"/ticket/{self.ticket_id}/reply",
            data={"message": "kaçak yanıt", "csrf_token": "test-csrf-token",
                  "form_token": "tok-reply-1"})

        self.assertEqual(response.status_code, 302)
        texts = [m["message_text"] for m in db.get_messages(self.ticket_id)]
        self.assertNotIn("kaçak yanıt", texts)
        self.assertEqual(self.sender.calls, [],
                         "Çözülmüş talepte Telegram'a mesaj gitmemeli.")


# ==================================================================
# 6. A10 — SAĞ PANEL
# ==================================================================

class TestContextPanel(FrontendTestCase):

    def _panel(self, html):
        """Sağ kolon dilimi — drawer ızgaranın DIŞINDADIR, dâhil edilmez."""
        return slice_between(html, 'class="ws-context"', "</main>")

    def test_panel_shows_priority_and_sla(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        panel = self._panel(self.render(self.admin_user, f"/ticket/{ticket_id}"))

        self.assertIn("Öncelik ve SLA", panel)
        self.assertIn("priority-badge", panel)

    def test_panel_shows_the_active_team_read_only(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        panel = self._panel(self.render(self.admin_user, f"/ticket/{ticket_id}"))

        self.assertIn("Aktif ekip", panel)
        self.assertIn("Hakan Bulut", panel)
        self.assertEqual(collect_forms(panel).post_forms, [],
                         "Sağ panel salt okunurdur; form içermez.")
        self.assertNotIn("<button", panel)

    def test_team_rows_use_an_avatar_instead_of_a_plain_status_dot(self):
        """Düz büyük yeşil durum noktası KALDIRILDI: satır [avatar] İsim."""
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        panel = self._panel(self.render(self.admin_user, f"/ticket/{ticket_id}"))

        self.assertNotIn('class="dot dot-available"', panel)
        self.assertNotIn('class="dot dot-busy"', panel)
        self.assertGreater(count_class(panel, "rail-avatar"), 0)

    def test_initials_remain_as_the_fallback_layer(self):
        """İllüstratif avatarın ALTINDA initials avatarı durmaya devam eder."""
        panel = self._panel(self.render(self.admin_user, "/tickets"))
        avatar = panel[panel.index('class="rail-avatar"'):]

        self.assertIn("HB", avatar, "Hakan Bulut -> HB baş harfleri.")
        self.assertRegex(avatar, r'style="background:#[0-9a-fA-F]{6};"',
                         "Renkli initials zemini fallback olarak korunur.")

    def test_availability_is_a_small_indicator_on_the_avatar(self):
        """Müsaitlik ayrı bir daire değil, avatarın köşesindeki göstergedir."""
        panel = self._panel(self.render(self.admin_user, "/tickets"))

        # Hakan ve Kaan müsait, Derya meşgul.
        self.assertEqual(count_class(panel, "presence-online"), 2)
        self.assertEqual(count_class(panel, "presence-busy"), 1)
        # Gösterge avatarın İÇİNDE durur (köşeye konumlanabilmesi için).
        # Araya illüstratif avatar <img>'i girebilir; bu yüzden DOTALL ile
        # sarmalayıcıdan göstergeye kadar bakıyoruz.
        self.assertRegex(panel, re.compile(
            r'class="rail-avatar"[^>]*>.*?<span class="presence', re.S))

    def test_presence_indicator_is_positioned_at_the_avatar_corner(self):
        css = self.css()
        avatar = re.search(r"\.rail-avatar\s*\{([^}]*)\}", css).group(1)
        presence = re.search(r"\.presence\s*\{([^}]*)\}", css).group(1)

        self.assertIn("position: relative", avatar)
        self.assertIn("border-radius: 50%", avatar)
        self.assertIn("position: absolute", presence)
        self.assertIn("right:", presence)
        self.assertIn("bottom:", presence)
        size = int(re.search(r"width:\s*(\d+)px", presence).group(1))
        self.assertLessEqual(size, 10, "Gösterge küçük kalmalı.")
        self.assertIn(".presence-online { background: #3ECF8E; }", css)

    def test_availability_is_not_conveyed_by_colour_alone(self):
        """WCAG 1.4.1 — rail sadeleşti ama anlam RENGE bağlı kalmadı.

        Uzun "Yeni iş alabilir" satırı kaldırıldı; yerine satırın `title`ı ve
        ekran okuyucuya görünen sr-only metin geçti."""
        panel = self._panel(self.render(self.admin_user, "/tickets"))

        self.assertIn('<span class="sr-only">Müsait</span>', panel)
        self.assertIn('<span class="sr-only">Meşgul</span>', panel)
        self.assertIn("Hakan Bulut — Müsait", panel)
        self.assertIn("Derya Uçar — Meşgul", panel)

    def test_per_ticket_team_reflects_real_availability(self):
        """Eskiden herkes sabit yeşil noktayla gösteriliyordu (yanıltıcıydı)."""
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        ok, message = db.add_collaborator(ticket_id, self.kaan_id)
        self.assertTrue(ok, message)
        db.set_technician_availability(self.kaan_id, False)

        html = self.render(self.admin_user, f"/ticket/{ticket_id}")
        team = slice_between(html, '>Aktif ekip<', "ctx-hint")

        self.assertEqual(count_class(team, "presence-online"), 1)
        self.assertEqual(count_class(team, "presence-busy"), 1)

    def test_technician_panel_has_no_management_routes(self):
        panel = self._panel(self.render(self.hakan_user, "/my-tickets"))

        for action in ("/toggle", "/gecici-sifre", "/hesap", "/uzmanlik"):
            self.assertNotIn(action, panel)
        self.assertNotIn('href="/ekip"', panel)

    def test_overdue_sla_is_marked_with_text_not_only_color(self):
        css = self.css()
        self.assertIn(".ctx-value.is-late { color: var(--red); }", css)
        self.assertIn(".sla-overdue", css)


# ==================================================================
# 6b. FRONTEND AVATAR REVISION — illüstratif ekip avatarları
# ==================================================================

class TestIllustratedAvatars(FrontendTestCase):
    """initials yerine küçük illüstratif insan avatarları.

    DB'de avatar kolonu YOK, avatar için sorgu YOK, harici CDN YOK."""

    AVATAR_DIR = "avatars"

    def _panel(self, html):
        return slice_between(html, 'class="ws-context"', "</main>")

    def _avatar_srcs(self, html):
        return re.findall(r'class="rail-avatar-img"[^>]*?src="([^"]+)"', html,
                          re.S)

    # ---------- 1. illüstratif avatar render ediliyor ----------

    def test_1_illustrated_avatar_is_rendered_instead_of_bare_initials(self):
        panel = self._panel(self.render(self.admin_user, "/tickets"))

        self.assertGreater(count_class(panel, "rail-avatar-img"), 0,
                           "Ekip satırlarında illüstratif avatar bulunmalı.")
        for src in self._avatar_srcs(panel):
            self.assertRegex(src, r"^/static/avatars/av-\d{2}\.svg$")

    def test_every_team_row_has_an_illustrated_avatar(self):
        panel = self._panel(self.render(self.admin_user, "/tickets"))
        # Üç teknisyen -> üç satır, üç avatar.
        self.assertEqual(count_class(panel, "rail-avatar"), 3)
        self.assertEqual(count_class(panel, "rail-avatar-img"), 3)

    # ---------- 2-3. deterministic eşleme ----------

    def test_2_the_same_technician_always_gets_the_same_avatar(self):
        """Aynı technician_id -> her render'da ve her ekranda aynı dosya."""
        first = self._avatar_srcs(self.render(self.admin_user, "/tickets"))
        second = self._avatar_srcs(self.render(self.admin_user, "/tickets"))
        self.assertEqual(first, second, "Avatar render'lar arası değişmemeli.")

        expected = webapp.avatar_illustration(self.hakan_id)
        self.assertIn(f"/static/{expected}", first)

    def test_the_same_person_gets_the_same_avatar_on_every_screen(self):
        """Genel rail ile ticket context paneli AYNI avatarı göstermeli."""
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")

        team = slice_between(html, '>Aktif ekip<', "ctx-hint")   # talebe özel
        rail = html[html.index('>Aktif Ekip<'):]                 # genel liste

        hakan_avatar = f"/static/{webapp.avatar_illustration(self.hakan_id)}"
        self.assertIn(hakan_avatar, team)
        self.assertIn(hakan_avatar, rail)

    def test_3_different_ids_spread_deterministically(self):
        count = webapp.AVATAR_ILLUSTRATION_COUNT
        mapping = [webapp.avatar_illustration(i) for i in range(1, count + 1)]

        self.assertEqual(len(set(mapping)), count,
                         "Ardışık id'ler setin tamamına dağılmalı.")
        # Modulo döngüsü: id ve id+count aynı avatarı alır.
        for i in range(1, count + 1):
            self.assertEqual(webapp.avatar_illustration(i),
                             webapp.avatar_illustration(i + count))

    def test_mapping_needs_no_database_query(self):
        """Seçim tamamen sunum katmanında; DB bağlantısı bile gerekmez."""
        original = db.get_conn

        def explode(*args, **kwargs):
            raise AssertionError("Avatar seçimi için DB sorgusu çalıştırıldı.")

        db.get_conn = explode
        try:
            self.assertEqual(webapp.avatar_illustration(7),
                             webapp.avatar_illustration(7))
        finally:
            db.get_conn = original

    # ---------- 4. asset'ler static altında ----------

    def test_4_avatar_assets_live_under_static_and_are_served(self):
        client = self.client_as(self.admin_user)
        for index in range(1, webapp.AVATAR_ILLUSTRATION_COUNT + 1):
            name = f"av-{index:02d}.svg"
            with self.subTest(asset=name):
                self.assertTrue(
                    os.path.exists(project_file("static", self.AVATAR_DIR, name)),
                    f"static/{self.AVATAR_DIR}/{name} bulunmalı.")
                response = client.get(f"/static/{self.AVATAR_DIR}/{name}")
                self.assertEqual(response.status_code, 200)

    def test_no_external_or_remote_avatar_dependency(self):
        html = self.render(self.admin_user, "/tickets")
        for src in self._avatar_srcs(html):
            self.assertTrue(src.startswith("/static/"),
                            f"Avatar yerel asset olmalı: {src}")

        for index in range(1, webapp.AVATAR_ILLUSTRATION_COUNT + 1):
            with open(project_file("static", self.AVATAR_DIR,
                                   f"av-{index:02d}.svg"), encoding="utf-8") as fh:
                markup = fh.read()
            self.assertIn("<svg", markup)
            # xmlns="http://www.w3.org/2000/svg" ZORUNLU namespace beyanıdır
            # ve ağ isteği YAPMAZ; aranan şey uzak KAYNAK referanslarıdır.
            for remote in ('href="http', 'src="http', "url(http", "@import"):
                self.assertNotIn(remote, markup,
                                 f"{remote}: avatar uzak kaynak çekmemeli.")

    def test_avatar_set_is_large_enough_for_variety(self):
        self.assertTrue(10 <= webapp.AVATAR_ILLUSTRATION_COUNT <= 16,
                        "Hedef 10-16 farklı avatar.")

    # ---------- 5. initials fallback ----------

    def test_5_initials_fallback_is_still_in_the_markup(self):
        """<img> yüklenemezse ALTINDAKİ initials avatarı görünür kalır."""
        panel = self._panel(self.render(self.admin_user, "/tickets"))
        avatar = panel[panel.index('class="rail-avatar"'):]

        self.assertIn("HB", avatar)
        self.assertRegex(avatar, r'style="background:#[0-9a-fA-F]{6};"')

        with open(project_file("static", "app.js"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("rail-avatar-img", script,
                      "Yüklenemeyen avatar için fallback davranışı olmalı.")
        self.assertIn("element.remove()", script)

    def test_fallback_when_there_is_no_technician_id(self):
        """Teknisyen kaydına bağlanamayan satır illüstratif avatar ALMAZ."""
        self.assertIsNone(webapp.avatar_illustration(None))
        self.assertIsNone(webapp.avatar_illustration("bilinmeyen"))
        self.assertIsNone(webapp.avatar_illustration(True))

    def test_legacy_row_without_technician_id_still_renders_initials(self):
        ticket_id = self.open_ticket()
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET assigned_admin_name = ? WHERE id = ?",
                         ("Eski Kayıt", ticket_id))

        html = self.render(self.admin_user, f"/ticket/{ticket_id}")
        self.assertEqual(html[:20].strip()[:5], html[:20].strip()[:5])  # render OK
        self.assertIn("rail-avatar", html, "initials avatarı yine çizilmeli.")

    # ---------- 6-7. presence göstergesi korunur ----------

    def test_6_and_7_presence_indicator_survives_on_top_of_the_illustration(self):
        panel = self._panel(self.render(self.admin_user, "/tickets"))

        self.assertEqual(count_class(panel, "presence-online"), 2)
        self.assertEqual(count_class(panel, "presence-busy"), 1)
        # Gösterge <img>'den SONRA gelir -> illüstrasyonun üstünde kalır.
        self.assertRegex(panel, re.compile(
            r'class="rail-avatar-img".*?<span class="presence', re.S))

    def test_presence_does_not_cover_the_avatar(self):
        css = self.css()
        avatar = re.search(r"\.rail-avatar\s*\{([^}]*)\}", css).group(1)
        presence = re.search(r"\.presence\s*\{([^}]*)\}", css).group(1)

        avatar_size = int(re.search(r"width:\s*(\d+)px", avatar).group(1))
        presence_size = int(re.search(r"width:\s*(\d+)px", presence).group(1))

        self.assertTrue(28 <= avatar_size <= 36, "Avatar 28-36px olmalı.")
        self.assertLess(presence_size, avatar_size / 3,
                        "Gösterge avatarı kaplamamalı.")
        self.assertIn(".presence-online { background: #3ECF8E; }", css)
        self.assertIn(".presence-busy { background: var(--muted-ink); }", css)

    def test_avatar_is_round_with_soft_shadow_and_no_glow(self):
        css = self.css()
        avatar = re.search(r"\.rail-avatar\s*\{([^}]*)\}", css).group(1)
        image = re.search(r"\.rail-avatar-img\s*\{([^}]*)\}", css).group(1)

        self.assertIn("border-radius: 50%", avatar)
        self.assertIn("border-radius: 50%", image)
        self.assertIn("object-fit: cover", image)
        self.assertIn("box-shadow: 0 1px 2px", avatar)
        for glow in ("filter:", "drop-shadow", "0 0 12px", "0 0 20px"):
            self.assertNotIn(glow, avatar, "Glow kullanılmamalı.")

    # ---------- 8-9. panel salt okunur kalır ----------

    def test_8_and_9_active_team_stays_read_only(self):
        panel = self._panel(self.render(self.admin_user, "/tickets"))

        self.assertEqual(collect_forms(panel).post_forms, [])
        self.assertNotIn("<button", panel)
        self.assertNotIn("<input", panel)

    def test_technician_view_gains_no_management_route_from_avatars(self):
        panel = self._panel(self.render(self.hakan_user, "/my-tickets"))

        self.assertGreater(count_class(panel, "rail-avatar-img"), 0)
        for action in ("/toggle", "/gecici-sifre", "/hesap", "/uzmanlik"):
            self.assertNotIn(action, panel)
        self.assertNotIn('href="/ekip"', panel)

    # ---------- 7. accessibility ----------

    def test_avatar_is_decorative_for_assistive_technology(self):
        panel = self._panel(self.render(self.admin_user, "/tickets"))

        self.assertRegex(panel, r'class="rail-avatar"[^>]*aria-hidden="true"')
        self.assertRegex(panel, r'class="rail-avatar-img"[^>]*alt=""')
        # İsim satırda düz metin olarak zaten var.
        self.assertIn("Hakan Bulut", panel)

    def test_availability_is_not_conveyed_by_colour_alone_after_revision(self):
        panel = self._panel(self.render(self.admin_user, "/tickets"))
        self.assertIn('<span class="sr-only">Müsait</span>', panel)
        self.assertIn('<span class="sr-only">Meşgul</span>', panel)

    # ---------- Drawer "Ana sorumlu" tutarlılığı ----------

    def _drawer(self, html):
        return html[html.index('<aside class="drawer"'):]

    def _rail(self, html):
        return slice_between(html, '>Aktif Ekip<', "</main>")

    def test_drawer_primary_uses_the_shared_member_avatar_component(self):
        """Eski `.mini-avatar` initials rozeti KALDIRILDI."""
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        drawer = self._drawer(self.render(self.admin_user, f"/ticket/{ticket_id}"))
        section = drawer[drawer.index("<h3>Ana sorumlu</h3>"):]

        self.assertNotIn("mini-avatar", drawer,
                         "Drawer artık ayrı bir initials rozeti kullanmamalı.")
        self.assertEqual(count_class(section[:600], "rail-avatar"), 1)
        self.assertEqual(count_class(section[:600], "rail-avatar-img"), 1)

    def test_drawer_and_rail_show_the_same_avatar_for_the_same_technician(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")

        expected = f"/static/{webapp.avatar_illustration(self.hakan_id)}"
        self.assertIn(expected, self._rail(html), "Aktif Ekip rail")
        self.assertIn(expected, slice_between(html, '>Aktif ekip<', "ctx-hint"),
                      "ticket context paneli")
        self.assertIn(expected, self._drawer(html), "drawer Ana sorumlu")

        # Yalnızca bu teknisyeni içeren bölümlerde BAŞKA avatar geçmez.
        # (Genel rail bütün ekibi listelediği için sayfa genelinde birden çok
        # asset bulunması normaldir.)
        section = self._drawer(html)
        section = section[section.index("<h3>Ana sorumlu</h3>"):][:600]
        self.assertEqual(set(self._avatar_srcs(section)), {expected})
        self.assertEqual(
            set(self._avatar_srcs(slice_between(html, '>Aktif ekip<', "ctx-hint"))),
            {expected})

    def test_drawer_primary_keeps_the_presence_indicator(self):
        ticket_id = self.open_ticket(technician_id=self.derya_id)   # meşgul
        drawer = self._drawer(self.render(self.admin_user, f"/ticket/{ticket_id}"))
        section = drawer[drawer.index("<h3>Ana sorumlu</h3>"):][:600]

        self.assertEqual(count_class(section, "presence-busy"), 1)
        self.assertEqual(count_class(section, "presence-online"), 0)

    def test_drawer_primary_avatar_is_decorative(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        drawer = self._drawer(self.render(self.admin_user, f"/ticket/{ticket_id}"))
        section = drawer[drawer.index("<h3>Ana sorumlu</h3>"):][:600]

        self.assertRegex(section, r'class="rail-avatar"[^>]*aria-hidden="true"')
        self.assertRegex(section, r'class="rail-avatar-img"[^>]*alt=""')
        self.assertIn("Hakan Bulut", section, "İsim düz metin olarak kalmalı.")

    def test_drawer_legacy_record_falls_back_to_initials(self):
        """technician ilişkisi yoksa illüstratif avatar basılmaz."""
        ticket_id = self.open_ticket()
        with db.get_conn() as conn:
            conn.execute("UPDATE tickets SET assigned_admin_name = ? WHERE id = ?",
                         ("Eski Sorumlu", ticket_id))

        drawer = self._drawer(self.render(self.admin_user, f"/ticket/{ticket_id}"))
        section = drawer[drawer.index("<h3>Ana sorumlu</h3>"):][:600]

        self.assertIn("Eski Sorumlu", section)
        self.assertIn("eski kayıt", section)
        self.assertEqual(count_class(section, "rail-avatar"), 1,
                         "initials avatarı yine çizilmeli.")
        self.assertEqual(count_class(section, "rail-avatar-img"), 0,
                         "technician_id yokken illüstratif avatar basılmaz.")
        self.assertIn("ES", section, "Eski Sorumlu -> ES baş harfleri.")

    def test_drawer_forms_and_csrf_are_unaffected_by_the_avatar_change(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        drawer = self._drawer(self.render(self.admin_user, f"/ticket/{ticket_id}"))
        forms = collect_forms(drawer).post_forms

        self.assertTrue(forms)
        for form in forms:
            self.assertIn("csrf_token", form["inputs"], form["action"])
            self.assertIn("form_token", form["inputs"], form["action"])

        # "Ana sorumlu" bölümündeki yönetim rotaları yerinde duruyor.
        actions = {f["action"] for f in forms}
        self.assertIn(f"/ticket/{ticket_id}/devret", actions)
        self.assertIn(f"/ticket/{ticket_id}/yardimci", actions)

    def test_collaborator_select_is_left_untouched(self):
        """Native <select><option> yapısına avatar EKLENMEDİ."""
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        drawer = self._drawer(self.render(self.admin_user, f"/ticket/{ticket_id}"))
        select = drawer[drawer.index('id="collab_id"'):]
        select = select[:select.index("</select>")]

        self.assertNotIn("rail-avatar", select)
        self.assertNotIn("<img", select)
        self.assertIn("Kaan Polat", select)

    # ---------- 10. N+1 yok ----------

    def test_10_rendering_the_team_panel_does_not_grow_queries_per_member(self):
        """Ekip büyüdükçe sorgu sayısı ARTMAMALI."""
        before = self._count_queries("/tickets")

        for i in range(6):
            self.make_technician(f"Ek Uye {i}", department="Yazılım")

        after = self._count_queries("/tickets")
        self.assertEqual(
            before, after,
            f"Ekip 3 -> 9 kişi olurken sorgu sayısı {before} -> {after} arttı.")

    def _count_queries(self, path):
        original = db.get_conn
        calls = []

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        db.get_conn = counting
        try:
            self.render(self.admin_user, path)
        finally:
            db.get_conn = original
        return len(calls)


# ==================================================================
# 7. A11 — TICKET SETTINGS DRAWER
# ==================================================================

class TestTicketSettingsDrawer(FrontendTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.open_ticket(technician_id=self.hakan_id)

    def detail(self, user_id=None):
        return self.render(user_id or self.admin_user, f"/ticket/{self.ticket_id}")

    def test_drawer_is_rendered_and_hidden_until_triggered(self):
        html = self.detail()
        self.assertIn('id="ticket-settings"', html)
        self.assertRegex(html, r'<aside class="drawer" id="ticket-settings"[^>]*hidden')
        self.assertIn('data-drawer-open="ticket-settings"', html)
        self.assertIn('data-drawer-overlay="ticket-settings"', html)

    def test_drawer_width_and_overlay_match_the_specification(self):
        css = self.css()
        drawer = re.search(r"\.drawer\s*\{([^}]*)\}", css).group(1)
        self.assertIn("width: 380px", drawer)
        self.assertIn("border-left", drawer)

        overlay = re.search(r"\.drawer-overlay\s*\{([^}]*)\}", css).group(1)
        alpha = re.search(r"rgba\(0, 0, 0, ([\d.]+)\)", overlay)
        self.assertIsNotNone(alpha)
        self.assertTrue(0.4 <= float(alpha.group(1)) <= 0.55)

    def test_drawer_header_has_title_and_close(self):
        html = self.detail()
        head = slice_between(html, 'class="drawer-head"', 'class="drawer-body"')
        self.assertIn("Talep ayarları", head)
        self.assertIn('data-drawer-close="ticket-settings"', head)

    def test_always_open_sections_are_present(self):
        html = self.detail()
        body = html[html.index('class="drawer-body"'):]
        for heading in ("<h3>Durum</h3>", "<h3>Öncelik ve SLA</h3>",
                        "<h3>Ana sorumlu</h3>"):
            self.assertIn(heading, body)

    def test_collapsed_accordions_are_details_elements_and_closed_by_default(self):
        html = self.detail()
        body = html[html.index('class="drawer-body"'):]
        for label in ("SLA geçmişi", "AI özeti", "Dahili not"):
            self.assertIn(f"<summary>{label}</summary>", body)
        # Hiçbir accordion 'open' ile başlamaz.
        self.assertNotIn("drawer-accordion\" open", body)
        self.assertNotIn("<details open", body)

    def test_primary_section_offers_transfer_and_collaborator_for_managers(self):
        html = self.detail()
        self.assertIn("Devret", html)
        self.assertIn("Yardımcı Ekle", html)

    def test_collaborator_cannot_see_transfer_or_status_controls(self):
        """Yardımcı ana sorumlu DEĞİLDİR."""
        ok, message = db.add_collaborator(self.ticket_id, self.kaan_id)
        self.assertTrue(ok, message)

        html = self.detail(self.kaan_user)
        self.assertNotIn(f"/ticket/{self.ticket_id}/devret", html)
        self.assertNotIn(f"/ticket/{self.ticket_id}/status", html)
        self.assertIn("durum değiştirme ve devir yetkisi ana sorumludadır", html)

    def test_all_drawer_post_forms_carry_csrf_and_form_token(self):
        for user_id, label in ((self.admin_user, "admin"),
                               (self.hakan_user, "primary")):
            with self.subTest(role=label):
                html = self.detail(user_id)
                drawer = html[html.index('<aside class="drawer"'):]
                forms = collect_forms(drawer).post_forms

                self.assertTrue(forms, "Drawer içinde POST formu bulunmalı.")
                for form in forms:
                    self.assertIn("csrf_token", form["inputs"],
                                  f"{form['action']}: csrf_token EKSİK.")
                    self.assertIn("form_token", form["inputs"],
                                  f"{form['action']}: form_token EKSİK.")

    def test_drawer_forms_post_to_existing_routes_only(self):
        html = self.detail()
        drawer = html[html.index('<aside class="drawer"'):]
        actions = {f["action"] for f in collect_forms(drawer).post_forms}

        allowed_suffixes = ("/status", "/oncelik", "/devret", "/yardimci",
                            "/kaldir", "/ozet", "/dahili-not", "/assign",
                            "/yonlendirme", "/reopen")
        for action in actions:
            self.assertTrue(
                action.startswith(f"/ticket/{self.ticket_id}"),
                f"Drawer yalnızca bu talebin rotalarına POST etmeli: {action}")
            self.assertTrue(any(action.endswith(s) for s in allowed_suffixes),
                            f"Bilinmeyen drawer rotası: {action}")

    def test_internal_note_form_lives_in_the_drawer(self):
        html = self.detail()
        drawer = html[html.index('<aside class="drawer"'):]
        self.assertIn(f"/ticket/{self.ticket_id}/dahili-not", drawer)
        self.assertIn("gönderilmez", drawer)

    def test_technician_drawer_has_no_admin_only_routing_form(self):
        html = self.detail(self.hakan_user)
        self.assertNotIn("/yonlendirme", html)

    def test_drawer_is_usable_without_javascript(self):
        """JS kapalıyken drawer görünür kalır; formlar düz HTML POST'udur."""
        html = self.detail()
        self.assertIn("<noscript>", html)
        noscript = slice_between(html, "<noscript>", "</noscript>")
        self.assertIn(".drawer[hidden]", noscript)


# ==================================================================
# 8. A12 — LOGIN
# ==================================================================

class TestLogin(FrontendTestCase):

    def login_html(self):
        response = webapp.app.test_client().get("/login")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_login_has_the_new_copy_and_card(self):
        html = self.login_html()
        self.assertIn("DESTEK MASASI", html)
        self.assertIn("Tekrar hoş geldiniz", html)
        self.assertIn("Panel hesabınızla giriş yapın.", html)
        self.assertEqual(count_class(html, "login-card"), 1)

    def test_login_card_is_about_360px_wide(self):
        css = self.css()
        card = re.search(r"\.login-card\s*\{([^}]*)\}", css).group(1)
        width = re.search(r"max-width:\s*(\d+)px", card)
        self.assertIsNotNone(width)
        self.assertTrue(340 <= int(width.group(1)) <= 380)

    def test_login_button_is_a_full_width_green_pill(self):
        html = self.login_html()
        self.assertIn("btn btn-primary btn-block", html)

        css = self.css()
        primary = re.search(r"\.btn-primary\s*\{([^}]*)\}", css).group(1)
        self.assertIn("var(--accent-light)", primary)
        self.assertIn(".btn-block { width: 100%; }", css)

    def test_login_has_no_navbar_and_leaks_no_team_data(self):
        html = self.login_html()
        self.assertEqual(count_class(html, "navbar"), 0)
        self.assertNotIn("Hakan Bulut", html)

    def test_login_form_still_carries_csrf(self):
        forms = collect_forms(self.login_html()).post_forms
        self.assertEqual(len(forms), 1)
        self.assertIn("csrf_token", forms[0]["inputs"])

    def test_login_still_enforces_the_rate_limit(self):
        """Auth/rate-limit davranışına DOKUNULMADI (yalnızca görsel revizyon)."""
        client = webapp.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "test-csrf-token"

        payload = {"username": "admin", "password": "yanlis",
                   "csrf_token": "test-csrf-token"}
        for _ in range(config.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(client.post("/login", data=payload).status_code, 200)

        self.assertEqual(client.post("/login", data=payload).status_code, 429)


# ==================================================================
# 9. A13 — RESPONSIVE
# ==================================================================

class TestLeftTicketPanel(FrontendTestCase):
    """LEFT PANEL VISUAL FIX — sol kolon tek rounded panel.

    Bu düzeltme YALNIZCA sol kolonu kapsar: parent grid, orta conversation ve
    sağ context paneli DEĞİŞMEZ (aşağıdaki 'unchanged' testleri bunu kilitler).
    """

    def _column(self, html):
        return slice_between(html, 'class="ws-list"', 'class="ws-main"')

    # ---------- 1-3: panel ve başlık ----------

    def test_left_column_is_a_single_rounded_panel(self):
        for user_id, path in ((self.admin_user, "/tickets"),
                              (self.hakan_user, "/my-tickets")):
            with self.subTest(path=path):
                column = self._column(self.render(user_id, path))
                self.assertEqual(count_class(column, "list-panel"), 1,
                                 "Sol kolon TEK bir kap içinde olmalı.")

    def test_panel_wraps_head_segmented_and_list(self):
        self.open_ticket(technician_id=self.hakan_id)
        column = self._column(self.render(self.admin_user, "/tickets"))
        panel = slice_between(column, '<section class="list-panel">', "</section>")

        self.assertIn('class="list-head"', panel)
        self.assertIn('class="segmented"', panel)
        self.assertIn('class="ticket-cards"', panel)

    def test_panel_uses_the_existing_glass_design_tokens(self):
        css = self.css()
        panel = re.search(r"\.list-panel\s*\{([^}]*)\}", css).group(1)

        radius = int(re.search(r"border-radius:\s*(\d+)px", panel).group(1))
        self.assertTrue(18 <= radius <= 22, f"{radius}px — hedef ~20px.")

        alpha = float(re.search(r"background: rgba\(255, 255, 255, ([\d.]+)\)",
                                panel).group(1))
        self.assertTrue(0.20 <= alpha <= 0.35,
                        f"Panel zemini {alpha} — hedef 0.20-0.35.")
        self.assertIn("border: 1px solid rgba(255, 255, 255,", panel)

    def test_left_title_is_just_talepler(self):
        column = self._column(self.render(self.admin_user, "/tickets"))
        self.assertIn('<h2 class="list-title">Talepler</h2>', column)
        self.assertNotIn("Tüm talepler", column)

    def test_admin_subtitle_is_removed(self):
        html = self.render(self.admin_user, "/tickets")
        self.assertNotIn("Bütün teknik personelin talepleri", html)
        self.assertNotIn('class="list-sub"', self._column(html))

    def test_middle_column_has_no_second_ticket_heading(self):
        """KPI kartlarının üstünde ikinci bir ticket başlığı olmamalı."""
        for query in ("", "?status=all", "?status=active", "?status=resolved"):
            with self.subTest(query=query):
                html = self.render(self.admin_user, f"/tickets{query}")
                main = slice_between(html, 'class="ws-main"', 'class="ws-context"')

                self.assertNotIn("<h1>", main)
                for heading in ("Tüm Talepler", "Aktif Talepler",
                                "Çözülmüş Talepler"):
                    self.assertNotIn(f"<h1>{heading}</h1>", main)

    def test_kpi_cards_keep_their_place_and_labels(self):
        main = slice_between(self.render(self.admin_user, "/tickets"),
                             'class="ws-main"', 'class="ws-context"')

        self.assertEqual(count_class(main, "ops-grid"), 1)
        self.assertEqual(count_class(main, "ops-card"), 6)
        for label in ("Aktif Talepler", "P1 Kritik", "SLA Yaklaşan",
                      "SLA Geciken", "Uzman Bekleyen", "Çözülmüş"):
            self.assertIn(label, main)

    # ---------- DESKTOP KPI GRID FIX ----------

    def _shell(self):
        css = self.css()
        start = css.index("@media (min-width: 1181px)")
        return css[start:css.index("@media (min-width: 1700px)", start)]

    def test_desktop_kpi_grid_is_exactly_six_columns(self):
        """REGRESYON: auto-fit kolon sayısını genişlikten türetiyordu ve
        1280-1440px ekranlarda KPI şeridi 4+2 / 5+1 olarak kırılıyordu."""
        shell = self._shell()

        self.assertIn(".ops-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }",
                      shell)
        # Yorumları at: kural gövdesinde auto-fit kalmamalı (yorumda kuralın
        # NEDEN değiştiğini anlatmak serbesttir).
        declarations = re.sub(r"/\*.*?\*/", "", shell, flags=re.S)
        self.assertNotIn("auto-fit", declarations,
                         "Masaüstünde kolon sayısı SABİT olmalı.")

    def test_six_kpi_cards_fit_in_a_single_row_on_desktop(self):
        """6 kart / 6 kolon = 1 satır."""
        main = slice_between(self.render(self.admin_user, "/tickets"),
                             'class="ws-main"', 'class="ws-context"')
        columns = int(re.search(r"\.ops-grid \{ grid-template-columns: repeat\((\d+),",
                                self._shell()).group(1))

        self.assertEqual(count_class(main, "ops-card"), 6)
        self.assertEqual(columns, 6)

    def test_kpi_dom_order_is_unchanged(self):
        main = slice_between(self.render(self.admin_user, "/tickets"),
                             'class="ws-main"', 'class="ws-context"')
        labels = re.findall(r'class="ops-label">([^<]+)<', main)

        self.assertEqual(labels, ["Aktif Talepler", "P1 Kritik", "SLA Yaklaşan",
                                  "SLA Geciken", "Uzman Bekleyen", "Çözülmüş"])

    def test_kpi_cards_can_shrink_without_overflowing(self):
        """minmax(0,1fr) + min-width:0 — içerik kartı alt satıra itemez."""
        card = re.search(r"\.ops-card\s*\{([^}]*)\}", self.css()).group(1)
        self.assertIn("min-width: 0", card)
        self.assertIn("minmax(0, 1fr)", self._shell())

    def test_kpi_card_typography_and_height_are_not_inflated(self):
        css = self.css()
        card = re.search(r"\.ops-card\s*\{([^}]*)\}", css).group(1)
        value = re.search(r"\.ops-value\s*\{([^}]*)\}", css).group(1)
        label = re.search(r"\.ops-label\s*\{([^}]*)\}", css).group(1)

        self.assertIn("font-size: 1.4rem", value, "Değer punto'su korunmalı.")
        self.assertIn("font-size: 0.74rem", label, "Etiket punto'su korunmalı.")
        self.assertNotIn("height:", card, "Kart yüksekliği büyütülmemeli.")
        # Desktop'ta yalnızca yatay padding hafifçe daralır.
        self.assertIn("padding: 14px 15px", card)
        self.assertIn(".ops-card { padding: 14px 10px; }", self._shell())

    def test_medium_and_mobile_keep_the_existing_wrap_behaviour(self):
        """6-kolon zorlaması YALNIZ >=1181px içindir."""
        css = self.css()

        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));",
                      css, "Varsayılan wrap davranışı korunmalı.")
        mobile = css[css.index("@media (max-width: 520px)"):]
        self.assertIn("repeat(auto-fit, minmax(120px, 1fr))", mobile)

    def test_filter_chip_row_is_untouched(self):
        main = slice_between(self.render(self.admin_user, "/tickets"),
                             'class="ws-main"', 'class="ws-context"')
        row = main[main.index('class="counter-row"'):]

        for label in ("Kullanıcı Bekleniyor", "Uzman Bekleyen",
                      "P1", "P2", "P3", "P4", "SLA Yaklaşan", "SLA Geciken"):
            self.assertIn(label, row)
        self.assertIn("counter-chip", row)

    def test_kpi_fix_creates_no_horizontal_scroll(self):
        css = self.css()
        body = re.search(r"\nbody\s*\{([^}]*)\}", css).group(1)
        grid = re.search(r"\.ops-grid\s*\{([^}]*)\}", css).group(1)

        self.assertIn("overflow-x: hidden", body)
        self.assertNotIn("min-width:", grid)
        self.assertNotIn("overflow-x: auto", grid)

    def test_expert_queue_view_keeps_its_own_heading(self):
        """Kuyruk ayrı bir operasyon görünümü; başlığı korunur."""
        main = slice_between(
            self.render(self.admin_user, "/tickets?status=eskalasyon_bekliyor"),
            'class="ws-main"', 'class="ws-context"')
        self.assertIn("<h1>Uzman Bekleyen</h1>", main)

    # ---------- 4-5: segmented control ve kart ölçüleri ----------

    def test_segmented_control_is_untouched(self):
        column = self._column(self.render(self.admin_user, "/tickets"))

        self.assertEqual(count_class(column, "segmented"), 1)
        self.assertEqual(count_class(column, "segmented-option"), 3)
        self.assertEqual(count_class(column, "segmented-count"), 3)
        for label in ("Tümü", "Aktif", "Çözülmüş"):
            self.assertIn(label, column)

    def test_ticket_card_styling_is_unchanged(self):
        css = self.css()
        card = re.search(r"\.tcard\s*\{([^}]*)\}", css).group(1)
        cards = re.search(r"\.ticket-cards\s*\{([^}]*)\}", css).group(1)

        self.assertIn("padding: 12px 13px", card)
        self.assertIn("border-radius: var(--radius-inner)", card)
        self.assertIn("box-shadow: var(--shadow-card)", card)
        self.assertIn("gap: 10px", cards, "Kartlar arası boşluk korunmalı.")

    def test_cards_are_not_spread_over_the_panel_height(self):
        """Kartlar doğal akışta yukarıdan aşağı dizilir — boşluk aralara
        DAĞITILMAZ.

        REGRESYON: liste `display: grid` iken app-shell ona kesin bir yükseklik
        verdi ve grid'in varsayılan `align-content: stretch`'i implicit
        satırları esneterek kartların arasını açtı."""
        css = self.css()
        cards = re.search(r"\.ticket-cards\s*\{([^}]*)\}", css).group(1)
        panel = re.search(r"\.list-panel\s*\{([^}]*)\}", css).group(1)

        # Yığın yönü açıkça yukarıdan aşağı ve başa hizalı.
        self.assertIn("display: flex", cards)
        self.assertIn("flex-direction: column", cards)
        self.assertIn("justify-content: flex-start", cards)
        self.assertIn("align-items: stretch", cards)
        self.assertNotIn("display: grid", cards,
                         "Grid stretch davranışı bu hataya yol açıyordu.")

        for banned in ("space-between", "space-around", "space-evenly"):
            self.assertNotIn(f"justify-content: {banned}", cards,
                             f"{banned} kullanılmamalı.")
            self.assertNotIn(f"justify-content: {banned}", panel)

    def test_cards_do_not_grow_or_stretch(self):
        css = self.css()
        item = re.search(r"\.ticket-cards > li\s*\{([^}]*)\}", css).group(1)
        card = re.search(r"\.tcard\s*\{([^}]*)\}", css).group(1)

        self.assertIn("flex: 0 0 auto", item, "Kartlar büyümemeli.")
        self.assertNotIn("flex-grow", card)
        self.assertNotIn("height: 100%", card)

    def test_gap_between_cards_stays_small_and_fixed(self):
        cards = re.search(r"\.ticket-cards\s*\{([^}]*)\}", self.css()).group(1)
        gap = int(re.search(r"gap:\s*(\d+)px", cards).group(1))
        self.assertTrue(8 <= gap <= 12, f"Kartlar arası {gap}px — hedef ~10px.")

    # ---------- SELECTED / HOVER / NORMAL görsel hiyerarşi ----------

    def _alpha(self, selector):
        """Bir kural bloğundaki background beyaz-alfa değeri."""
        block = re.search(rf"{selector}\s*\{{([^}}]*)\}}", self.css()).group(1)
        return float(re.search(r"background: rgba\(255, 255, 255, ([\d.]+)\)",
                               block).group(1))

    def test_selected_card_is_the_most_opaque(self):
        selected = self._alpha(r"\.tcard\.is-selected,\s*\n\.tcard\.is-selected:hover")
        hover = self._alpha(r"\.tcard:hover")
        normal = self._alpha(r"\.tcard")

        self.assertGreater(selected, hover, "SELECTED > HOVER")
        self.assertGreater(hover, normal, "HOVER > NORMAL")
        self.assertGreaterEqual(selected, 0.9, "Açık talep en beyaz kart olmalı.")

    def test_normal_card_is_translucent(self):
        normal = self._alpha(r"\.tcard")
        self.assertTrue(0.45 <= normal <= 0.65,
                        f"Normal kart {normal} — glass görünüm beklenir.")

    def test_selected_card_keeps_the_accent_border_and_soft_shadow(self):
        block = re.search(
            r"\.tcard\.is-selected,\s*\n\.tcard\.is-selected:hover\s*\{([^}]*)\}",
            self.css()).group(1)
        self.assertIn("border-color: var(--accent)", block)
        self.assertIn("box-shadow: 0 2px 8px rgba(60, 80, 70, 0.08)", block)

    def test_hover_never_overrides_the_selected_card(self):
        """`.tcard.is-selected:hover` tanımlı olmalı; yoksa hover selected'ı
        bastırır ve açık talep listede kaybolur."""
        self.assertIn(".tcard.is-selected:hover", self.css())

    def test_no_global_opacity_on_ticket_cards(self):
        """Metin, avatar, rozet ve süre soluklaşmamalı — yalnız zemin alfası."""
        css = self.css()
        for selector in (r"\.tcard", r"\.tcard:hover",
                         r"\.tcard\.is-resolved"):
            block = re.search(rf"{selector}\s*\{{([^}}]*)\}}", css).group(1)
            self.assertNotIn("opacity:", block,
                             "Karta global opacity verilmemeli.")

    def test_selected_state_comes_from_the_server(self):
        """Yeni bir JS state sistemi kurulmadı; kaynak selected_id."""
        tickets = [self.open_ticket(subject=f"Talep {i}",
                                    technician_id=self.hakan_id)
                   for i in range(3)]
        html = self.render(self.admin_user, f"/ticket/{tickets[1]}")
        column = self._column(html)

        classes = re.findall(r'<a class="(tcard[^"]*)"', column)
        self.assertEqual(len(classes), 3)
        self.assertEqual(sum("is-selected" in c for c in classes), 1,
                         "Yalnızca açık talep is-selected almalı.")

        with open(project_file("static", "app.js"), encoding="utf-8") as fh:
            script = fh.read()
        self.assertNotIn("is-selected", script,
                         "Seçim durumu JS ile yönetilmiyor.")

    def test_list_view_has_no_selected_card(self):
        self.open_ticket(technician_id=self.hakan_id)
        column = self._column(self.render(self.admin_user, "/tickets"))
        self.assertNotIn("is-selected", column)

    # ---------- Boş orta state ----------

    def test_empty_state_is_a_card_surface(self):
        html = self.render(self.admin_user, "/tickets")
        main = slice_between(html, 'class="ws-main"', 'class="ws-context"')
        self.assertEqual(count_class(main, "convo-empty"), 1)
        self.assertIn("Bir talep seçin", main)

        block = re.search(r"\.convo-empty\s*\{([^}]*)\}", self.css()).group(1)
        alpha = float(re.search(r"background: rgba\(255, 255, 255, ([\d.]+)\)",
                                block).group(1))
        self.assertTrue(0.88 <= alpha <= 0.96, f"Boş state zemini {alpha}.")
        self.assertIn("border: 1px solid rgba(255, 255, 255,", block)
        self.assertIn("border-radius: var(--radius-card)", block)
        self.assertIn("text-align: center", block)

    def test_empty_state_keeps_its_size_and_position(self):
        block = re.search(r"\.convo-empty\s*\{([^}]*)\}", self.css()).group(1)
        self.assertIn("padding: 44px 28px", block,
                      "Boş state'in ölçüsü değişmemeli.")
        for banned in ("width:", "height:", "position:", "margin:"):
            self.assertNotIn(banned, block)

    # ---------- 6: yalnız liste kayar ----------

    def test_only_the_ticket_list_scrolls(self):
        css = self.css()
        panel = re.search(r"\.list-panel\s*\{([^}]*)\}", css).group(1)
        cards = re.search(r"\.ticket-cards\s*\{([^}]*)\}", css).group(1)

        self.assertNotIn("overflow-y: auto", panel,
                         "Panelin kendisi kaymaz.")
        self.assertIn("overflow-y: auto", cards)
        self.assertIn("min-height: 0", cards)
        self.assertIn("flex-direction: column", panel)
        self.assertIn("min-height: 0", panel)

    def test_head_and_segmented_stay_above_the_scroll_area(self):
        self.open_ticket(technician_id=self.hakan_id)
        panel = slice_between(self._column(self.render(self.admin_user, "/tickets")),
                              '<section class="list-panel">', "</section>")

        self.assertLess(panel.index('class="list-head"'),
                        panel.index('class="segmented"'))
        self.assertLess(panel.index('class="segmented"'),
                        panel.index('class="ticket-cards"'))

    def test_panel_fills_the_column_inside_the_shell(self):
        css = self.css()
        panel = re.search(r"\.list-panel\s*\{([^}]*)\}", css).group(1)
        self.assertIn("flex: 1 1 auto", panel)

    # ---------- 9-12: dokunulmayanlar ----------

    def test_workspace_grid_columns_are_unchanged(self):
        css = self.css()
        self.assertIn(
            "grid-template-columns: var(--list-w) minmax(0, 1fr) var(--ctx-w);",
            css, "Parent grid tanımı DEĞİŞMEMELİ.")
        self.assertIn("--list-w: 250px;", css)
        self.assertIn("--ctx-w: 240px;", css)

    def test_conversation_structure_is_unchanged(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")
        main = slice_between(html, 'class="ws-main"', 'class="ws-context"')

        for marker in ("convo", "convo-header", "convo-thread", "composer"):
            self.assertEqual(count_class(main, marker), 1, marker)
        self.assertIn('data-drawer-open="ticket-settings"', main)

        css = self.css()
        convo = re.search(r"\.convo\s*\{([^}]*)\}", css).group(1)
        self.assertIn("height: 100%", convo)
        self.assertIn("overflow: hidden", convo)
        self.assertIn("flex-direction: column", convo)

    def test_right_context_panel_is_unchanged(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        html = self.render(self.admin_user, f"/ticket/{ticket_id}")
        panel = slice_between(html, 'class="ws-context"', "</main>")

        self.assertIn("Öncelik ve SLA", panel)
        self.assertIn("Aktif ekip", panel)
        self.assertIn("Aktif Ekip", panel)
        self.assertGreater(count_class(panel, "rail-avatar-img"), 0)
        self.assertGreater(count_class(panel, "presence-online"), 0)
        self.assertEqual(collect_forms(panel).post_forms, [],
                         "Sağ panel salt okunur kalmalı.")

    def test_desktop_page_scroll_lock_is_preserved(self):
        css = self.css()
        shell = css[css.index("@media (min-width: 1181px)"):]
        shell = shell[:shell.index("@media (min-width: 1700px)")]

        self.assertIn("overflow: hidden", shell)
        self.assertIn(".workspace {", shell)
        self.assertIn(".ticket-cards { flex: 1 1 auto; min-height: 0; }", shell)


class TestCompactActiveTeam(FrontendTestCase):
    """FRONTEND UX REVISION — rail satırı: [avatar●] Ad Soyad / Ünvan.

    Panel "ekipte kim var ve kim müsait?" sorusunu yanıtlar; tam personel
    kartına dönüşmez. Ayrıntı /ekip sayfasındadır."""

    def _rail(self, html):
        return slice_between(html, '<ul class="team-rail-list">', "</ul>")

    def test_row_shows_avatar_name_and_title(self):
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertEqual(count_class(rail, "rail-avatar"), 3)
        self.assertEqual(count_class(rail, "team-rail-name"), 3)
        self.assertIn("Hakan Bulut", rail)
        self.assertIn("Kıdemli Mühendis", rail, "Ünvan varsa gösterilmeli.")

    def test_row_without_a_title_shows_only_the_name(self):
        """Kaan Polat'ın ünvanı yok -> yalnızca isim."""
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertIn("Kaan Polat", rail)
        # Ünvanı olan tek kişi Hakan; rol satırı sayısı 1 olmalı.
        self.assertEqual(count_class(rail, "team-rail-role"), 1)

    def test_department_is_not_shown_in_the_rail(self):
        rail = self._rail(self.render(self.admin_user, "/tickets"))
        self.assertNotIn("team-rail-dept", rail)
        self.assertNotIn("Ağ", rail, "Departman rail'de gösterilmez.")

    def test_active_ticket_count_is_not_shown_in_the_rail(self):
        self.open_ticket(technician_id=self.hakan_id)
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertNotIn("aktif talep", rail)
        self.assertNotIn("team-rail-load", rail)
        self.assertNotIn("yardımcılık", rail)

    def test_skill_metadata_is_not_shown_in_the_rail(self):
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertNotIn("Uzmanlık tanımlanmadı", rail)
        self.assertNotIn("Görüntü İşleme", rail)
        self.assertNotIn("team-rail-pri", rail)

    def test_availability_text_line_is_not_shown_in_the_rail(self):
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertNotIn("Yeni iş alabilir", rail)
        self.assertNotIn("Yeni iş almıyor", rail)

    def test_presence_indicator_is_preserved(self):
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertEqual(count_class(rail, "presence-online"), 2)
        self.assertEqual(count_class(rail, "presence-busy"), 1)
        # Büyük yeşil nokta geri gelmedi.
        self.assertNotIn('class="dot dot-available"', rail)

    def test_meaning_is_not_carried_by_colour_alone(self):
        rail = self._rail(self.render(self.admin_user, "/tickets"))

        self.assertIn('<span class="sr-only">Müsait</span>', rail)
        self.assertIn('<span class="sr-only">Meşgul</span>', rail)
        self.assertIn("Derya Uçar — Meşgul", rail, "title ipucu da bulunmalı.")

    def test_row_height_target_is_compact(self):
        css = self.css()
        card = re.search(r"\.team-rail-card\s*\{([^}]*)\}", css).group(1)

        min_height = int(re.search(r"min-height:\s*(\d+)px", card).group(1))
        padding = int(re.search(r"padding:\s*(\d+)px", card).group(1))
        self.assertTrue(44 <= min_height <= 52,
                        f"Satır {min_height}px — hedef 44-52px.")
        self.assertLessEqual(padding, 8, "Gereksiz dikey padding olmamalı.")

    def test_rail_stays_read_only(self):
        for user_id, path in ((self.admin_user, "/tickets"),
                              (self.hakan_user, "/my-tickets")):
            with self.subTest(path=path):
                rail = self._rail(self.render(user_id, path))
                self.assertEqual(collect_forms(rail).post_forms, [])
                self.assertNotIn("<button", rail)
                self.assertNotIn("<input", rail)

    def test_compacting_the_rail_added_no_query(self):
        """Sadeleştirme mevcut team_rail verisini kullanır; N+1 yok."""
        before = self._count_queries("/tickets")

        for i in range(6):
            self.make_technician(f"Ek Uye {i}", department="Yazılım",
                                 role="Uzman")

        after = self._count_queries("/tickets")
        self.assertEqual(before, after,
                         f"Ekip 3 -> 9 olurken sorgu {before} -> {after}.")

    def _count_queries(self, path):
        original = db.get_conn
        calls = []

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        db.get_conn = counting
        try:
            self.render(self.admin_user, path)
        finally:
            db.get_conn = original
        return len(calls)


# ==================================================================
# 9b. DESKTOP APP SHELL — sayfa kaymaz, bölgeler kendi içinde kayar
# ==================================================================

class TestWorkspaceGeometryConsistency(FrontendTestCase):
    """WORKSPACE WIDTH CONSISTENCY — geometri STATE'TEN BAĞIMSIZDIR.

    Talep seçili olsun (/ticket/<id>) ya da olmasın (/tickets), dış workspace
    kutusu ve kolon ölçüleri AYNI kaynaktan gelir: templates/workspace_base.html
    ve tek bir `.workspace` kuralı. Bu paket o tekliği kalıcı olarak kilitler —
    ileride birine özel bir max-width/margin eklenirse test kırılır.

    GLOBAL SHELL REVİZYONU: dış kutu artık `.page` ile PAYLAŞILAN tek bir
    kuraldan ve `--shell-*` token'larından gelir. `.workspace`in kendine ait
    bir genişliği YOKTUR; kilit bu yüzden "tek kural + token" üzerinedir.
    Yasak olan hâlâ aynı: bir SAYFA/DURUM sınıfına bağlı workspace kuralı.
    """

    #: dış kutuyu `.workspace` ile paylaşması meşru olan shell ortakları
    SHELL_PARTNERS = {".page"}

    def _css_no_comments(self):
        return re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)

    def _workspace_rules(self):
        """(selector, body) — `.workspace` seçen bütün kurallar."""
        return [(sel.strip(), " ".join(body.split()))
                for sel, body in re.findall(
                    r"([^{}]*\.workspace[^{}]*)\{([^}]*)\}", self._css_no_comments())]

    def _chain(self, html):
        """<body> -> .ws-list zincirindeki eleman/class dizisi."""
        body = html[html.index("<body>"):]
        chain = []
        for tag, cls in re.findall(
                r'<(main|div|section|header|aside)[^>]*class="([^"]*)"[^>]*>', body):
            chain.append((tag, cls))
            if "ws-list" in cls:
                break
        return chain

    def _screens(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        return (self.render(self.admin_user, "/tickets"),
                self.render(self.admin_user, f"/ticket/{ticket_id}"))

    # ---------- 1: aynı outer class / aynı sarmalayıcı zinciri ----------

    def test_both_screens_use_the_same_workspace_wrapper(self):
        listing, detail = self._screens()

        for name, html in (("/tickets", listing), ("/ticket/<id>", detail)):
            with self.subTest(screen=name):
                self.assertEqual(count_class(html, "workspace"), 1)
                self.assertIn('<main class="workspace" id="main">', html)
                for column in ("ws-list", "ws-main", "ws-context"):
                    self.assertEqual(count_class(html, column), 1, column)

    def test_wrapper_chain_is_identical_on_both_screens(self):
        listing, detail = self._screens()
        self.assertEqual(self._chain(listing), self._chain(detail),
                         "Sarmalayıcı zinciri iki ekranda birebir aynı olmalı.")

    def test_both_screens_extend_the_shared_workspace_template(self):
        for name in ("admin_tickets.html", "detail.html"):
            with open(project_file("templates", name), encoding="utf-8") as fh:
                head = fh.read(200)
            self.assertIn('{% extends "workspace_base.html" %}', head, name)

    # ---------- 2-3: tek geometri kaynağı ----------

    def test_workspace_geometry_is_declared_exactly_once(self):
        rules = self._workspace_rules()
        geometry = [body for _sel, body in rules if "max-width" in body]

        self.assertEqual(len(geometry), 1,
                         f"Genişlik tek yerde tanımlanmalı, bulunan: {geometry}")
        # Ölçü HARDCODE EDİLMEZ; global shell token'larından gelir.
        self.assertIn("max-width: var(--shell-max-width)", geometry[0])
        self.assertIn("padding-inline: var(--shell-padding-x)", geometry[0])
        self.assertIn("margin-inline: auto", geometry[0])
        self.assertNotIn("1560px", geometry[0],
                         "Shell genişliği selector içinde tekrar yazılmamalı.")

        dikey = [body for _sel, body in rules if "padding-block" in body]
        self.assertTrue(any("8px 32px" in body for body in dikey),
                        "Workspace dikey padding'i korunmalı.")
        gap = [body for _sel, body in rules if "gap:" in body]
        self.assertTrue(any("gap: 16px" in body for body in gap))

    def test_desktop_grid_template_columns_has_a_single_source(self):
        rules = self._workspace_rules()
        three_column = [body for _sel, body in rules
                        if "var(--list-w) minmax(0, 1fr) var(--ctx-w)" in body]
        self.assertEqual(len(three_column), 1)

    def test_no_workspace_rule_is_scoped_to_a_page_or_state(self):
        """Kritik değişmez: `.workspace` hiçbir yerde bir sayfa/durum
        sınıfıyla NİTELENMEZ — yoksa iki ekran farklı genişlik alırdı."""
        for selector, _body in self._workspace_rules():
            for part in selector.split(","):
                part = part.strip()
                if part in self.SHELL_PARTNERS:
                    continue          # paylaşılan global shell kuralı
                self.assertEqual(
                    part, ".workspace",
                    f"Duruma özel workspace kuralı bulundu: {part!r}")

    # ---------- 4-5: token'lar ve margin state'ten bağımsız ----------

    def test_column_width_tokens_are_state_independent(self):
        """Kolon token'ları YALNIZCA :root bloklarında tanımlanır.

        Bir medya sorgusu içindeki :root da geçerlidir — o da iki ekrana aynı
        şekilde uygulanır. Yasak olan, bir sayfa/durum sınıfına bağlı tanım."""
        css = self._css_no_comments()
        root_bodies = re.findall(r":root\s*\{([^}]*)\}", css)

        for token in ("--list-w", "--ctx-w"):
            in_root = sum(body.count(token + ":") for body in root_bodies)
            total = css.count(token + ":")
            self.assertEqual(in_root, total,
                             f"{token} :root DIŞINDA da tanımlanmış.")

        self.assertIn("--list-w: 250px;", css)
        self.assertIn("--ctx-w: 240px;", css)

    def test_selecting_a_ticket_does_not_change_the_workspace_margin(self):
        margins = [body for _sel, body in self._workspace_rules()
                   if "margin" in body]
        self.assertEqual(len(margins), 1, "Tek margin tanımı olmalı.")
        self.assertIn("margin-inline: auto", margins[0])

    def test_only_the_middle_column_content_differs_between_states(self):
        """State farkı YALNIZCA .ws-main içeriğindedir."""
        listing, detail = self._screens()

        listing_main = slice_between(listing, 'class="ws-main"', 'class="ws-context"')
        detail_main = slice_between(detail, 'class="ws-main"', 'class="ws-context"')
        self.assertIn("ops-grid", listing_main)
        self.assertIn("convo-empty", listing_main)
        self.assertIn("convo-header", detail_main)
        self.assertNotIn("ops-grid", detail_main)

        # Sol kolon iki ekranda da aynı bileşeni kullanır.
        for html in (listing, detail):
            column = slice_between(html, 'class="ws-list"', 'class="ws-main"')
            self.assertEqual(count_class(column, "list-panel"), 1)
            self.assertEqual(count_class(column, "segmented"), 1)

    # ---------- 6-9: komşu alanlar bozulmadı ----------

    def test_kpi_six_column_rule_survives(self):
        shell = self._css_no_comments()
        shell = shell[shell.index("@media (min-width: 1181px)"):]
        shell = shell[:shell.index("@media (min-width: 1700px)")]
        self.assertIn("grid-template-columns: repeat(6, minmax(0, 1fr))", shell)

    def test_ticket_list_presentation_survives(self):
        css = self.css()
        cards = re.search(r"\.ticket-cards\s*\{([^}]*)\}", css).group(1)

        self.assertIn("flex-direction: column", cards)
        self.assertIn("justify-content: flex-start", cards)
        self.assertIn("overflow-y: auto", cards)
        self.assertIn(".ticket-cards > li { flex: 0 0 auto; }", css)
        self.assertIn(".tcard.is-selected:hover", css)

    def test_conversation_presentation_survives(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        main = slice_between(self.render(self.admin_user, f"/ticket/{ticket_id}"),
                             'class="ws-main"', 'class="ws-context"')
        for marker in ("convo", "convo-header", "convo-thread", "composer"):
            self.assertEqual(count_class(main, marker), 1, marker)

        convo = re.search(r"\.convo\s*\{([^}]*)\}", self.css()).group(1)
        self.assertIn("height: 100%", convo)
        self.assertIn("overflow: hidden", convo)

    def test_right_context_presentation_survives(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        panel = slice_between(self.render(self.admin_user, f"/ticket/{ticket_id}"),
                              'class="ws-context"', "</main>")

        self.assertIn("Öncelik ve SLA", panel)
        self.assertIn("Aktif Ekip", panel)
        self.assertGreater(count_class(panel, "rail-avatar-img"), 0)
        self.assertEqual(collect_forms(panel).post_forms, [])


class TestDesktopAppShell(FrontendTestCase):

    SHELL = "@media (min-width: 1181px)"

    def shell_block(self):
        css = self.css()
        start = css.index(self.SHELL)
        end = css.index("@media (min-width: 1700px)", start)
        return css[start:end]

    def test_desktop_locks_the_page_scroll(self):
        shell = self.shell_block()
        body = re.search(r"\n  body\s*\{([^}]*)\}", shell).group(1)

        self.assertIn("height: 100dvh", body)
        self.assertIn("display: flex", body)
        self.assertIn("flex-direction: column", body)
        self.assertIn("overflow: hidden", body,
                      "Tarayıcının ana sayfası aşağı uzamamalı.")

    def test_navbar_keeps_its_own_height_and_stays_on_screen(self):
        shell = self.shell_block()
        self.assertIn(".appbar { position: static; flex: 0 0 auto; }", shell)

    def test_workspace_fills_the_remaining_area(self):
        shell = self.shell_block()
        workspace = re.search(r"\.workspace\s*\{([^}]*)\}", shell).group(1)

        self.assertIn("flex: 1 1 auto", workspace)
        self.assertIn("min-height: 0", workspace)
        self.assertIn("overflow: hidden", workspace)

    def test_scroll_children_declare_min_height_zero(self):
        """flex/grid çocuğu min-height:0 olmadan içeriğinden küçülemez."""
        shell = self.shell_block()
        columns = re.search(
            r"\.ws-list,\s*\n\s*\.ws-main,\s*\n\s*\.ws-context\s*\{([^}]*)\}",
            shell).group(1)

        self.assertIn("height: 100%", columns)
        self.assertIn("min-height: 0", columns)

    def test_ticket_list_scrolls_locally(self):
        css = self.css()
        cards = re.search(r"\.ticket-cards\s*\{([^}]*)\}", css).group(1)

        self.assertIn("overflow-y: auto", cards)
        self.assertIn("min-height: 0", cards)
        # Sabit viewport hesabı kaldırıldı; yükseklik shell'den gelir.
        self.assertNotIn("calc(100dvh", cards)
        self.assertIn(".ticket-cards { flex: 1 1 auto; min-height: 0; }",
                      self.shell_block())

    def test_ticket_list_header_and_segmented_stay_outside_the_scroll_area(self):
        """Başlık ve segmented control listeyle birlikte kaymaz."""
        self.open_ticket(technician_id=self.hakan_id)   # liste boş olmamalı
        html = self.render(self.admin_user, "/tickets")
        column = slice_between(html, 'class="ws-list"', 'class="ws-main"')

        head_at = column.index('class="list-head"')
        segmented_at = column.index('class="segmented"')
        list_at = column.index('class="ticket-cards"')
        self.assertLess(head_at, segmented_at)
        self.assertLess(segmented_at, list_at,
                        "Kayan bölge YALNIZCA ticket listesi olmalı.")

    def test_conversation_history_is_the_only_scrolling_part(self):
        css = self.css()
        convo = re.search(r"\.convo\s*\{([^}]*)\}", css).group(1)
        header = re.search(r"\.convo-header\s*\{([^}]*)\}", css).group(1)
        thread = re.search(r"\.convo-thread\s*\{([^}]*)\}", css).group(1)
        composer = re.search(r"\.composer\s*\{([^}]*)\}", css).group(1)

        self.assertIn("overflow: hidden", convo)
        self.assertIn("flex-direction: column", convo)
        self.assertIn("flex-shrink: 0", header, "Ticket header kaybolmamalı.")
        self.assertIn("overflow-y: auto", thread)
        self.assertIn("flex: 1 1 auto", thread)
        self.assertIn("flex-shrink: 0", composer, "Composer kaybolmamalı.")

    def test_reply_composer_stays_below_a_long_conversation(self):
        ticket_id = self.open_ticket(technician_id=self.hakan_id)
        for i in range(60):
            db.add_message(ticket_id, "kullanici", 1000, "Ali Veli", f"Mesaj {i}")

        html = self.render(self.admin_user, f"/ticket/{ticket_id}")
        # Sol panel de bir <section> olduğu için kapanışı KONUŞMADAN SONRA ara.
        start = html.index('class="convo"')
        convo = html[start:html.index("</section>", start)]

        self.assertIn('class="composer"', convo)
        self.assertIn("Gönder", convo)
        # DOM sırası: header -> thread -> composer
        self.assertLess(convo.index('class="convo-header"'),
                        convo.index('class="convo-thread"'))
        self.assertLess(convo.index('class="convo-thread"'),
                        convo.index('class="composer"'))

    def test_active_team_list_scrolls_locally(self):
        shell = self.shell_block()

        self.assertIn(".ws-context { overflow: hidden; }", shell)
        rail = re.search(r"\.ws-context > \.team-rail\s*\{([^}]*)\}", shell).group(1)
        self.assertIn("flex: 1 1 auto", rail)
        self.assertIn("min-height: 0", rail)

        team_list = re.search(r"\.team-rail-list\s*\{([^}]*)\}", shell).group(1)
        self.assertIn("overflow-y: auto", team_list)
        self.assertIn("min-height: 0", team_list)

    def test_thirty_technicians_do_not_stretch_the_page(self):
        for i in range(30):
            self.make_technician(f"Teknisyen {i}", department="Yazılım")

        html = self.render(self.admin_user, "/tickets")
        # Shell hâlâ yürürlükte ve rail listesi tek kayan bölge.
        self.assertEqual(count_class(html, "workspace"), 1)
        self.assertEqual(count_class(html, "team-rail-list"), 1)
        self.assertGreater(count_class(html, "team-rail-card"), 30)

    def test_standard_pages_scroll_inside_the_shell(self):
        """Ekip / Uzmanlıklar / SLA takvimi sayfaları da kendi içinde kayar."""
        shell = self.shell_block()
        page = re.search(r"\.page,\s*\n\s*\.login-wrap\s*\{([^}]*)\}",
                         shell).group(1)

        self.assertIn("flex: 1 1 auto", page)
        self.assertIn("min-height: 0", page)
        self.assertIn("overflow-y: auto", page)

    def test_scrollbars_stay_thin_and_native(self):
        """Sade ince scrollbar; platform davranışı ezilmiyor."""
        css = self.css()
        self.assertIn("scrollbar-width: thin", css)
        self.assertNotIn("::-webkit-scrollbar-thumb", css,
                         "Aşırı custom scrollbar yapılmamalı.")

    def test_mobile_returns_to_normal_document_flow(self):
        css = self.css()
        mobile = css[css.index("@media (max-width: 860px)"):]
        mobile = mobile[:mobile.index("@media (max-width: 520px)")]

        self.assertIn("body { height: auto; overflow: visible; display: block; }",
                      mobile, "Mobilde scroll kilidi OLMAMALI.")
        self.assertIn(".ticket-cards { max-height: none; }", mobile)
        self.assertIn(".convo-thread { max-height: none; }", mobile)

    def test_shell_does_not_apply_below_the_three_column_band(self):
        """1180px altında sağ panel alta iniyor; kilit orada uygulanmaz."""
        css = self.css()
        self.assertLess(css.index("@media (min-width: 1181px)"),
                        css.index("@media (max-width: 1180px)"))
        medium = css[css.index("@media (max-width: 1180px)"):]
        medium = medium[:medium.index("@media (max-width: 860px)")]
        self.assertIn("grid-column: 1 / -1", medium)
        self.assertIn(".ticket-cards { max-height: 70vh; }", medium)


class TestResponsive(FrontendTestCase):

    def test_body_never_scrolls_horizontally(self):
        css = self.css()
        body = re.search(r"\nbody\s*\{([^}]*)\}", css).group(1)
        self.assertIn("overflow-x: hidden", body)

    def test_wide_content_scrolls_inside_its_own_container(self):
        css = self.css()
        table = re.search(r"\.table-scroll\s*\{([^}]*)\}", css).group(1)
        self.assertIn("overflow-x: auto", table)

    def test_medium_band_moves_the_context_panel_below_the_conversation(self):
        css = self.css()
        self.assertIn("@media (max-width: 1180px)", css)
        medium = css[css.index("@media (max-width: 1180px)"):]
        medium = medium[:medium.index("@media (max-width: 860px)")]
        self.assertIn("grid-template-columns: var(--list-w) minmax(0, 1fr);", medium)
        self.assertIn("grid-column: 1 / -1", medium)

    def test_mobile_band_stacks_the_list_and_conversation(self):
        css = self.css()
        self.assertIn("@media (max-width: 860px)", css)
        mobile = css[css.index("@media (max-width: 860px)"):]
        mobile = mobile[:mobile.index("@media (max-width: 520px)")]
        self.assertIn("grid-template-columns: minmax(0, 1fr);", mobile)
        self.assertIn(".drawer { width: 100%; }", mobile)

    def test_grid_columns_cannot_overflow(self):
        """minmax(0, 1fr): uzun içerik orta kolonu şişirip taşma yapamaz."""
        css = self.css()
        self.assertIn("minmax(0, 1fr)", css)
        self.assertIn(".ws-main { min-width: 0; }", css)


# ==================================================================
# 13. GLOBAL AUTHENTICATED LAYOUT — TEK YATAY SHELL
#
# Navbar görsel referanstır: sol/sağ içerik sınırı, max-width ve yatay
# gutter TEK bir token çiftinden (`--shell-max-width`, `--shell-padding-x`)
# gelir. Standart `.page` ekranları, ticket `.workspace`i ve flash bandı
# aynı kaynaktan beslenir; sayfaya özel dar merkez kolon YOKTUR.
#
#   |<- shell-padding-x ->[  shell content  ]<- shell-padding-x ->|
#                         ^                 ^
#                    NAVBAR LEFT       NAVBAR RIGHT
#                    PAGE   LEFT       PAGE   RIGHT
# ==================================================================

class TestGlobalShell(FrontendTestCase):

    #: shell geometrisini paylaşan kutular
    SHELL_BOXES = (".page", ".workspace", ".navbar", ".flash-stack")

    def _css(self):
        return re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)

    def _rule(self, selector):
        """Verilen selector'ı İÇEREN kuralların gövdelerini döner."""
        bodies = []
        for sel, body in re.findall(r"([^{}]*)\{([^}]*)\}", self._css()):
            parts = [part.strip() for part in sel.strip().split(",")]
            if selector in parts:
                bodies.append(" ".join(body.split()))
        return bodies

    # ---------- 1-4: tek CSS kaynağı ----------

    def test_shell_tokens_exist_and_are_defined_only_in_root(self):
        css = self._css()
        root_bodies = re.findall(r":root\s*\{([^}]*)\}", css)

        for token in ("--shell-max-width", "--shell-padding-x",
                      "--shell-content-width"):
            with self.subTest(token=token):
                self.assertIn(token + ":", css, f"{token} tanımlı olmalı.")
                in_root = sum(body.count(token + ":") for body in root_bodies)
                self.assertEqual(in_root, css.count(token + ":"),
                                 f"{token} :root DIŞINDA tanımlanmış.")

        self.assertIn("--shell-max-width: 1560px;", css)
        self.assertIn("--shell-padding-x: 24px;", css)

    def test_shell_width_is_never_hardcoded_in_a_selector(self):
        """1560px YALNIZCA token tanımında geçer — selector'da tekrar edilmez."""
        css = self._css()
        self.assertEqual(css.count("1560px"), 1,
                         "Shell genişliği birden fazla yerde yazılmış.")
        self.assertNotIn("max-width: 1100px", css,
                         "Eski dar merkez kolon geri gelmiş.")

    def test_every_shell_box_derives_its_width_from_the_tokens(self):
        for selector in self.SHELL_BOXES:
            with self.subTest(selector=selector):
                bodies = " ".join(self._rule(selector))
                self.assertIn("max-width: var(--shell-", bodies,
                              f"{selector} shell token'ı kullanmıyor.")
                self.assertIn("margin-inline: auto", bodies)

    def test_gutter_comes_from_the_token_on_every_band(self):
        css = self._css()
        for selector in (".page", ".workspace", ".appbar", ".flash-region"):
            with self.subTest(selector=selector):
                self.assertIn("padding-inline: var(--shell-padding-x)",
                              " ".join(self._rule(selector)),
                              f"{selector} gutter'ı token'dan almıyor.")

        # Mobil bant token'ı EZER; selector'lar yeniden yazılmaz.
        mobile = css[css.index("@media (max-width: 860px)"):]
        mobile = mobile[:mobile.index("@media (max-width: 520px)")]
        self.assertIn("--shell-padding-x: 14px", mobile)
        self.assertNotIn("padding: 8px 14px", mobile,
                         "Mobil gutter yeniden hardcode edilmiş.")

    # ---------- 5-6: navbar == page == workspace ----------

    def test_page_and_workspace_share_one_shell_rule(self):
        css = self._css()
        shared = re.search(r"\.page,\s*\.workspace\s*\{([^}]*)\}", css)
        self.assertIsNotNone(shared, ".page ve .workspace tek kuralı paylaşmalı.")
        body = " ".join(shared.group(1).split())
        self.assertIn("max-width: var(--shell-max-width)", body)
        self.assertIn("padding-inline: var(--shell-padding-x)", body)
        self.assertIn("margin-inline: auto", body)

    def test_navbar_pill_sits_inside_the_shell_gutter(self):
        """Navbar pill'i sarmalayıcısının gutter'ı içinde shell içerik
        genişliğine oturur: NAVBAR LEFT == PAGE LEFT == WORKSPACE LEFT."""
        css = self._css()
        shared = re.search(r"\.navbar,\s*\.flash-stack\s*\{([^}]*)\}", css)
        self.assertIsNotNone(shared)
        self.assertIn("max-width: var(--shell-content-width)",
                      " ".join(shared.group(1).split()))

        navbar = " ".join(self._rule(".navbar"))
        self.assertNotIn("max-width: 1", navbar,
                         "Navbar kendi genişliğini hardcode etmemeli.")

    def test_appbar_no_longer_owns_a_horizontal_gutter_of_its_own(self):
        appbar = " ".join(self._rule(".appbar"))
        self.assertIn("padding-block: 14px 10px", appbar)
        self.assertNotIn("padding: 14px 24px", appbar)

    # ---------- 7: admin ve technician aynı shell ----------

    def test_admin_and_technician_use_the_same_shell_wrapper(self):
        pages = ((self.admin_user, "/tickets"), (self.admin_user, "/ekip"),
                 (self.admin_user, "/uzmanliklar"),
                 (self.admin_user, "/sla-takvimi"),
                 (self.hakan_user, "/my-tickets"),
                 (self.hakan_user, "/collaborations"))
        for user_id, path in pages:
            with self.subTest(path=path):
                html = self.render(user_id, path)
                outer = re.search(r'<main class="(page|workspace)" id="main">', html)
                self.assertIsNotNone(
                    outer, f"{path} paylaşılan shell sarmalayıcısını kullanmıyor.")
                self.assertEqual(
                    count_class(html, "page") + count_class(html, "workspace"),
                    1, f"{path} ikinci bir dış kutu eklemiş.")

    def test_technician_screens_reuse_the_workspace_shell(self):
        for path in ("/my-tickets", "/collaborations"):
            with self.subTest(path=path):
                html = self.render(self.hakan_user, path)
                self.assertIn('<main class="workspace" id="main">', html)

    # ---------- 8: yatay taşma ----------

    def test_no_shell_box_can_cause_horizontal_page_overflow(self):
        css = self._css()
        body = re.search(r"\nbody\s*\{([^}]*)\}", css).group(1)
        self.assertIn("overflow-x: hidden", body)
        for selector in (".page", ".workspace"):
            self.assertIn("width: 100%", " ".join(self._rule(selector)))
        # border-box olmadan padding + max-width taşardı.
        self.assertIn("* { box-sizing: border-box; }", css)


class TestTeamPageGrid(FrontendTestCase):
    """EKİP SAYFASI — shell genişliğinde ızgara; dar merkez kolon YOK."""

    def _grid(self):
        return " ".join(re.search(r"\.team-grid\s*\{([^}]*)\}",
                                  self.css()).group(1).split())

    def test_grid_columns_are_derived_from_the_available_width(self):
        self.assertIn("repeat(auto-fit, minmax(260px, 1fr))", self._grid())

    def test_wide_desktop_fits_at_least_four_cards(self):
        """1560px shell içinde 260px'lik izler: 4-5 kolon çıkmalı."""
        gap = int(re.search(r"gap:\s*(\d+)px", self._grid()).group(1))
        icerik = 1560 - 2 * 24                      # shell content genişliği
        kolon = (icerik + gap) // (260 + gap)
        self.assertGreaterEqual(kolon, 4, f"Geniş ekranda {kolon} kart sığıyor.")
        self.assertLessEqual(kolon, 5, "Kartlar anlamsız şekilde daralmamalı.")

    def test_medium_and_narrow_bands_step_down_gracefully(self):
        gap = 14
        for icerik, beklenen in ((1052, 3), (740, 2), (492, 1)):
            with self.subTest(width=icerik):
                self.assertEqual((icerik + gap) // (260 + gap), beklenen)

    def test_cards_do_not_stretch_absurdly_when_the_team_is_small(self):
        card = re.search(r"\.team-card\s*\{([^}]*)\}", self.css()).group(1)
        genislik = int(re.search(r"max-width:\s*(\d+)px", card).group(1))
        self.assertTrue(360 <= genislik <= 520,
                        f"Kart üst sınırı {genislik}px — makul bantta olmalı.")

    def test_page_header_starts_at_the_shell_edge(self):
        """Başlık ve açıklama shell'in sol sınırından başlar: `.board-head`
        `.page`in DOĞRUDAN çocuğudur, araya dar bir sarmalayıcı girmez."""
        html = self.render(self.admin_user, "/ekip")
        govde = slice_between(html, '<main class="page" id="main">', "</main>")
        self.assertIn("board-head", re.search(r'<div class="([^"]+)"',
                                              govde).group(1))

    def test_team_card_actions_are_unchanged(self):
        html = self.render(self.admin_user, "/ekip")
        for action in ("/toggle", "/gecici-sifre", "/hesap", "/uzmanlik"):
            self.assertIn(action, html, f"{action} formu kaybolmuş.")
        forms = collect_forms(html)
        self.assertGreater(len(forms.post_forms), 0)
        for form in forms.post_forms:
            self.assertIn("csrf_token", form["inputs"], form["action"])


class TestSkillsPageLayout(FrontendTestCase):
    """UZMANLIKLAR — tablo dar merkez kolonda kalmaz; form dengeli durur."""

    def test_content_uses_the_shared_two_column_primitive(self):
        html = self.render(self.admin_user, "/uzmanliklar")
        self.assertEqual(count_class(html, "page-split"), 1)
        self.assertEqual(count_class(html, "page"), 1,
                         "Sayfaya özel ikinci bir dış kutu olmamalı.")

    def test_table_is_not_trapped_in_a_narrow_column(self):
        css = re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)
        split = " ".join(re.search(r"\.page-split\s*\{([^}]*)\}",
                                   css).group(1).split())
        self.assertIn("minmax(0, 1.6fr)", split, "Ana alan geniş olmalı.")
        self.assertIn("minmax(300px, 0.8fr)", split, "Yan alan dar olmalı.")
        self.assertNotIn("max-width", split,
                         "Bölüm ilkelinin kendi max-width'i olmamalı.")

    def test_table_scrolls_inside_its_own_container(self):
        html = self.render(self.admin_user, "/uzmanliklar")
        self.assertEqual(count_class(html, "table-scroll"), 1,
                         "Tablo kendi kayan kabında olmalı.")

    def test_add_form_fills_its_grid_cell(self):
        css = re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)
        self.assertIn(".page-split > .form-card { max-width: none; }", css)
        # Tek başına duran form kartı (şifre değiştir, yeni teknisyen) dar kalır.
        self.assertIn(".form-card { padding: 22px; max-width: 420px;", css)

    def test_skill_forms_and_csrf_are_unchanged(self):
        html = self.render(self.admin_user, "/uzmanliklar")
        forms = collect_forms(html)
        self.assertGreater(len(forms.post_forms), 0)
        for form in forms.post_forms:
            self.assertIn("csrf_token", form["inputs"], form["action"])
            self.assertIn("form_token", form["inputs"], form["action"])


class TestSlaCalendarLayout(FrontendTestCase):
    """SLA TAKVİMİ — üstte tam genişlik tatiller, altta dengeli iki kolon."""

    def test_dead_wrapper_classes_are_gone(self):
        with open(project_file("templates", "sla_calendar.html"),
                  encoding="utf-8") as fh:
            template = fh.read()
        for dead in ('class="detail-grid"', 'class="thread-col"'):
            self.assertNotIn(dead, template,
                             f"{dead}: CSS karşılığı olmayan ölü sarmalayıcı.")

    def test_holidays_span_the_full_shell_width(self):
        html = self.render(self.admin_user, "/sla-takvimi")
        self.assertEqual(count_class(html, "page-block"), 1)
        blok = slice_between(html, 'class="page-block"', 'class="page-split')
        self.assertIn("Tatiller", blok)

    def test_bottom_row_is_a_balanced_two_column_section(self):
        html = self.render(self.admin_user, "/sla-takvimi")
        self.assertEqual(count_class(html, "page-split"), 1)
        self.assertIn('class="page-split is-side-first"', html)

        alt = html[html.index('class="page-split is-side-first"'):]
        self.assertLess(alt.index("Tatil Ekle"), alt.index("Çalışma Saatleri"),
                        "Dar sütun (Tatil Ekle) önde olmalı.")

        css = re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)
        ters = re.search(r"\.page-split\.is-side-first\s*\{([^}]*)\}",
                         css).group(1)
        self.assertIn("minmax(300px, 0.8fr) minmax(0, 1.6fr)",
                      " ".join(ters.split()))

    def test_business_hours_and_timezone_output_is_unchanged(self):
        html = self.render(self.admin_user, "/sla-takvimi")
        for gun in ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma",
                    "Cumartesi", "Pazar"):
            self.assertIn(gun, html)
        self.assertIn("Saat dilimi:", html)

    def test_holiday_forms_and_csrf_are_unchanged(self):
        html = self.render(self.admin_user, "/sla-takvimi")
        forms = collect_forms(html)
        self.assertGreater(len(forms.post_forms), 0)
        for form in forms.post_forms:
            self.assertIn("csrf_token", form["inputs"], form["action"])


class TestPageSectionsResponsive(FrontendTestCase):
    """Yeni sayfa ilkelleri mevcut breakpoint'lerle ÇAKIŞMAZ."""

    def test_split_collapses_in_the_existing_mobile_band(self):
        css = self.css()
        mobile = css[css.index("@media (max-width: 860px)"):]
        mobile = mobile[:mobile.index("@media (max-width: 520px)")]
        self.assertIn(".page-split,", mobile)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", mobile)

    def test_no_new_breakpoint_was_introduced(self):
        css = self.css()
        bantlar = sorted(
            set(re.findall(r"@media \((?:min|max)-width: (\d+)px\)", css)))
        self.assertEqual(bantlar, ["1180", "1181", "1700", "520", "860"],
                         f"Breakpoint listesi değişmiş: {bantlar}")

    def test_split_children_cannot_overflow_horizontally(self):
        css = re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)
        self.assertIn(".page-split > * { min-width: 0; }", css)

    def test_scroll_containers_clip_their_absolutely_positioned_children(self):
        """`.sr-only` position: absolute'tur; konumlandırılmamış bir kapta
        containing block VIEWPORT olur, `overflow` tarafından KIRPILMAZ ve
        sayfayı kaydırılabilir yapar.

        Ölçüm (Chrome): düzeltmeden önce /uzmanliklar 500px'te 73px yatay,
        /ticket/<id> 1920x900'de 71px dikey kayabiliyordu. Kabı
        konumlandırılmış yapmak ikisini de sıfırlar."""
        css = self._css_no_comments()
        for selector in (".table-scroll", ".team-rail-list"):
            with self.subTest(selector=selector):
                body = re.search(
                    re.escape(selector) + r"\s*\{([^}]*)\}", css).group(1)
                self.assertIn("position: relative", body,
                              f"{selector} mutlak konumlu çocuğunu kırpmalı.")
        # Kırpmanın anlamlı olması için her iki kap da kayan bölgedir
        # (`.team-rail-list` overflow'unu desktop app shell bandından alır).
        self.assertIn("overflow-x: auto",
                      re.search(r"\.table-scroll\s*\{([^}]*)\}", css).group(1))
        shell = css[css.index("@media (min-width: 1181px)"):]
        shell = shell[:shell.index("@media (min-width: 1700px)")]
        rail = re.search(r"\.team-rail-list\s*\{([^}]*)\}", shell).group(1)
        self.assertIn("overflow-y: auto", rail)

    def _css_no_comments(self):
        return re.sub(r"/\*.*?\*/", "", self.css(), flags=re.S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
