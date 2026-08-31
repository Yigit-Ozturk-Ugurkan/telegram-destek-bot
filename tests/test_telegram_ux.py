"""TELEGRAM UX PAKETİ — ACCEPTANCE UX + ROUTING REVISION §14-§23, §30.

Test edilen davranış:

  §14  /start KALICI ReplyKeyboard gönderir (🆕 Yeni Talep / 📋 Taleplerim / ❓ Yardım)
  §15  slash komutlar KALDIRILMADI; /istek, /taleplerim, /yardim çalışır
  §16  '🆕 Yeni Talep' ile /istek AYNI helper'ı kullanır (kod iki kez yazılmaz)
  §17  '📋 Taleplerim' ile /taleplerim AYNI helper'ı kullanır
  §18  kullanıcıya gösterilen metinlerde `#42` YOKTUR
  §19  etiket TEK merkezden gelir: telegram_ui.format_ticket_label()
  §22  ticket'a özel InlineKeyboard butonları KORUNUR
  §23  same-ticket politikası korunur

GERÇEK TELEGRAM ÇAĞRISI YAPILMAZ: bot'un kullandığı bütün gönderim yolları
(update.message.reply_text, context.bot.send_message) sahte nesnelerle
değiştirilir. Gerçek Gemini de çağrılmaz — ai_assistant fonksiyonları
enjekte edilir.
"""

from tests import _support, project_file

_support.setup_test_db("telegram_ux")

import ast
import asyncio
import unittest

import config
import db
import queue_service
import queue_worker
import routing
import telegram_ui
from tests._support import DbTestCase, RecordingSender, fail_on_network

import bot


# ==================================================================
# Sahte Telegram nesneleri — hiçbir ağ çağrısı YOK
# ==================================================================

class FakeUser:
    def __init__(self, user_id, full_name="Test Kullanıcı", username="testuser"):
        self.id = user_id
        self.full_name = full_name
        self.username = username


class FakeMessage:
    """update.message yerine geçer; reply_text çağrılarını kaydeder."""

    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append({"text": text, "reply_markup": reply_markup})
        return self


class FakeBot:
    """context.bot yerine geçer."""

    def __init__(self):
        self.sent = []
        self.actions = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "reply_markup": reply_markup})

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append((chat_id, action))


class FakeUpdate:
    def __init__(self, user, text=""):
        self.effective_user = user
        self.message = FakeMessage(text)


class FakeContext:
    def __init__(self, args=None):
        self.bot = FakeBot()
        self.args = args or []


class FakeQuery:
    """callback_query yerine geçer."""

    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.answers = []
        self.edits = []
        self.markup_edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append({"text": text, "reply_markup": reply_markup})

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.markup_edits.append(reply_markup)


class FakeCallbackUpdate:
    def __init__(self, data, user):
        self.callback_query = FakeQuery(data, user)
        self.effective_user = user


def run(coro):
    """Async handler'ları senkron testte çalıştırır."""
    return asyncio.run(coro)


class TelegramUxTestCase(DbTestCase):
    db = db
    config = config

    USER_ID = 5551

    def setUp(self):
        super().setUp()
        self.user = FakeUser(self.USER_ID)

        # GERÇEK Gemini ÇAĞRILMAZ. Varsayılan olarak her çağrı hata verir;
        # test bilerek isterse stub_ai() ile davranış tanımlar.
        self._real_conversation = bot.ai_assistant.get_conversation_response
        self._real_classify = bot.ai_assistant.classify_for_escalation
        bot.ai_assistant.get_conversation_response = fail_on_network
        bot.ai_assistant.classify_for_escalation = fail_on_network
        self.addCleanup(setattr, bot.ai_assistant, "get_conversation_response",
                        self._real_conversation)
        self.addCleanup(setattr, bot.ai_assistant, "classify_for_escalation",
                        self._real_classify)

        self.cv = self.make_skill("COMPUTER_VISION", "Görüntü İşleme")
        self.general = self.make_skill(config.GENERAL_SUPPORT_SKILL_CODE,
                                       config.GENERAL_SUPPORT_SKILL_NAME)

    # ---------- yardımcılar ----------

    def stub_conversation(self, response):
        bot.ai_assistant.get_conversation_response = (
            lambda *a, **kw: dict(response) if response else None)

    def stub_classification(self, payload):
        bot.ai_assistant.classify_for_escalation = lambda *a, **kw: dict(payload)

    def press(self, button_text):
        """Ana menü butonuna basar (Telegram bunu düz metin olarak gönderir)."""
        update = FakeUpdate(self.user, button_text)
        context = FakeContext()
        run(bot.handle_plain_text(update, context))
        return update, context

    def command(self, handler, args=None, text=""):
        update = FakeUpdate(self.user, text)
        context = FakeContext(args)
        run(handler(update, context))
        return update, context

    def all_user_texts(self, update, context):
        """Kullanıcıya giden BÜTÜN metinler (reply + bot.send_message)."""
        texts = [r["text"] for r in update.message.replies]
        texts += [s["text"] for s in context.bot.sent
                  if s["chat_id"] == self.USER_ID]
        return texts

    def open_ticket(self, subject="Kamera aracımı algılamıyor"):
        ticket_id = db.create_ticket(self.USER_ID, "testuser", "Test Kullanıcı",
                                     subject)
        db.set_ai_mode(ticket_id, config.AI_MODE_HANDLING)
        return ticket_id


# ==================================================================
# §14: ANA MENÜ
# ==================================================================

class TestMainMenuKeyboard(TelegramUxTestCase):

    def test_start_sends_reply_keyboard(self):
        update, _context = self.command(bot.start)

        self.assertEqual(len(update.message.replies), 1)
        markup = update.message.replies[0]["reply_markup"]
        self.assertIsNotNone(markup, "/start kalıcı menüyü göndermeli.")
        self.assertTrue(hasattr(markup, "keyboard"),
                        "Ana menü ReplyKeyboardMarkup olmalı (inline değil).")

    def test_keyboard_layout_matches_spec(self):
        """§14: [Yeni Talep][Taleplerim] / [Yardım]"""
        markup = bot.main_menu_keyboard()
        rows = [[button.text for button in row] for row in markup.keyboard]

        self.assertEqual(rows, [
            [telegram_ui.BTN_NEW_TICKET, telegram_ui.BTN_MY_TICKETS],
            [telegram_ui.BTN_HELP],
        ])

    def test_keyboard_is_resizable_and_persistent(self):
        """§21: resize_keyboard açık, one_time_keyboard kapalı.

        one_time_keyboard açık olsaydı kullanıcı bir kez yazdıktan sonra menü
        kaybolurdu; en stabil UX menünün sürekli erişilebilir kalmasıdır."""
        markup = bot.main_menu_keyboard()

        self.assertTrue(markup.resize_keyboard)
        self.assertFalse(bool(getattr(markup, "one_time_keyboard", False)))

    def test_welcome_text_advertises_buttons_not_commands(self):
        update, _ = self.command(bot.start)
        text = update.message.replies[0]["text"]

        for label in (telegram_ui.BTN_NEW_TICKET, telegram_ui.BTN_MY_TICKETS,
                      telegram_ui.BTN_HELP):
            self.assertIn(label, text)

    def test_menu_button_text_is_never_stored_as_a_ticket(self):
        """En sık görülen hata: buton yazısı destek talebi olarak kaydedilir."""
        for label in (telegram_ui.BTN_NEW_TICKET, telegram_ui.BTN_MY_TICKETS,
                      telegram_ui.BTN_HELP):
            with self.subTest(button=label):
                self.press(label)

        self.assertEqual(db.list_tickets_for_user(self.USER_ID), [])


# ==================================================================
# §16, §17: BUTON VE KOMUT AYNI İŞ MANTIĞINI PAYLAŞIR
# ==================================================================

class TestButtonsShareCommandLogic(TelegramUxTestCase):

    def test_new_ticket_button_matches_bare_istek_command(self):
        """§16: aynı kod iki kere yazılmaz — çıktı BİREBİR aynı olmalı."""
        button_update, _ = self.press(telegram_ui.BTN_NEW_TICKET)
        command_update, _ = self.command(bot.create_ticket_command, args=[])

        self.assertEqual(button_update.message.replies[0]["text"],
                         command_update.message.replies[0]["text"])
        self.assertIn("Yeni talebinizi yazabilirsiniz",
                      button_update.message.replies[0]["text"])

    def test_my_tickets_button_matches_taleplerim_command(self):
        """§17: buton ve komut aynı helper'ı kullanır."""
        self.open_ticket("VPN bağlantısı kurulamıyor")

        button_update, _ = self.press(telegram_ui.BTN_MY_TICKETS)
        command_update, _ = self.command(bot.my_tickets)

        self.assertEqual(button_update.message.replies[0]["text"],
                         command_update.message.replies[0]["text"])

    def test_help_button_matches_yardim_command(self):
        button_update, _ = self.press(telegram_ui.BTN_HELP)
        command_update, _ = self.command(bot.help_command)

        self.assertEqual(button_update.message.replies[0]["text"],
                         command_update.message.replies[0]["text"])
        self.assertIn("Nasıl çalışır", button_update.message.replies[0]["text"])

    def test_shared_helpers_exist_once_in_source(self):
        """Helper'ların GERÇEKTEN paylaşıldığını AST ile doğrular.

        Metin araması yerine fonksiyon tanımları sayılır: aynı akışın ikinci
        bir kopyası eklenirse bu test kırılır."""
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="bot.py")
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        for helper in ("open_new_ticket_flow", "show_my_tickets", "show_help"):
            self.assertEqual(names.count(helper), 1,
                             f"{helper} tek bir tanım olmalı.")


# ==================================================================
# §15: SLASH KOMUTLAR GERİYE DÖNÜK UYUMLU
# ==================================================================

class TestSlashCommandsPreserved(TelegramUxTestCase):

    def test_istek_with_arguments_still_creates_a_ticket(self):
        self.stub_conversation({"response_type": config.AI_RESPONSE_CLARIFICATION,
                                "message": "Hangi cihazda?",
                                "category": None, "required_skill": None})

        self.command(bot.create_ticket_command,
                     args=["İnternet", "bağlantısı", "kopuyor"])

        tickets = db.list_tickets_for_user(self.USER_ID)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["subject"], "İnternet bağlantısı kopuyor")

    def test_taleplerim_command_still_lists_tickets(self):
        self.open_ticket("VPN bağlantısı kurulamıyor")
        update, _ = self.command(bot.my_tickets)

        self.assertIn("VPN bağlantısı kurulamıyor",
                      update.message.replies[0]["text"])

    def test_yardim_command_still_answers(self):
        update, _ = self.command(bot.help_command)
        self.assertIn("Nasıl çalışır", update.message.replies[0]["text"])

    def test_all_user_commands_are_registered(self):
        """§15: /istek, /taleplerim, /yardim handler'ları main()'de DURUYOR."""
        with open(project_file("bot.py"), encoding="utf-8") as fh:
            source = fh.read()
        for command in ("istek", "taleplerim", "yardim", "start"):
            self.assertIn(f'CommandHandler("{command}"', source,
                          f"/{command} handler'ı kaldırılmamalı.")

    def test_help_text_still_documents_slash_commands(self):
        """Power-user'lar komutları görebilmeli."""
        for command in ("/istek", "/taleplerim", "/yardim"):
            self.assertIn(command, telegram_ui.HELP_TEXT)


# ==================================================================
# §18, §19: TALEP ETİKETİ
# ==================================================================

class TestTicketLabelUx(TelegramUxTestCase):

    def test_format_ticket_label_shape(self):
        self.assertEqual(telegram_ui.format_ticket_label(42), "🎫 Talep 42")
        self.assertNotIn("#", telegram_ui.format_ticket_label(42))

    def test_label_preserves_the_database_id(self):
        """§18: yalnızca SUNUM değişir; DB kimliği aynı kalır."""
        ticket_id = self.open_ticket()
        self.assertIn(str(ticket_id), telegram_ui.format_ticket_label(ticket_id))

    def test_my_tickets_list_uses_the_label_and_no_hash(self):
        ticket_id = self.open_ticket("Kamera aracımı algılamıyor")
        update, _ = self.press(telegram_ui.BTN_MY_TICKETS)
        text = update.message.replies[0]["text"]

        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertNotIn(f"#{ticket_id}", text)
        self.assertIn("📋 Talepleriniz", text)
        self.assertIn("Kamera aracımı algılamıyor", text)
        self.assertIn("Durum:", text)

    def test_my_tickets_list_shows_assigned_specialist(self):
        """§20: atanan uzman adı listede görünür."""
        ticket_id = self.open_ticket()
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        db.assign_primary_if_unassigned(ticket_id, hakan, "Hakan Bulut")

        update, _ = self.press(telegram_ui.BTN_MY_TICKETS)
        self.assertIn("Uzman: Hakan Bulut", update.message.replies[0]["text"])

    def test_my_tickets_empty_state_guides_to_the_button(self):
        update, _ = self.press(telegram_ui.BTN_MY_TICKETS)
        self.assertIn(telegram_ui.BTN_NEW_TICKET,
                      update.message.replies[0]["text"])

    def test_ticket_creation_notice_uses_the_label(self):
        # AI kapalı -> güvenli fallback yolu. Talebin oluşturulduğunu bildiren
        # ilk mesaj yeni etiketi taşımalı.
        self.stub_conversation(None)
        update = FakeUpdate(self.user, "Yazıcı çalışmıyor")
        context = FakeContext()
        run(bot.handle_plain_text(update, context))

        ticket_id = db.list_tickets_for_user(self.USER_ID)[0]["id"]
        texts = self.all_user_texts(update, context)
        self.assertTrue(any(f"🎫 Talep {ticket_id}" in t for t in texts), texts)
        self.assertFalse(any(f"#{ticket_id}" in t for t in texts), texts)

    def test_assignment_notification_uses_the_label(self):
        """§30: otomatik atama bildirimi yeni etiketi kullanır."""
        ticket_id = self.open_ticket()
        ticket = db.get_ticket(ticket_id)
        result = {"mode": routing.MODE_SKILL,
                  "routing_basis": routing.BASIS_SPECIFIC_SKILL,
                  "skill": {"id": self.cv, "code": "COMPUTER_VISION",
                            "name": "Görüntü İşleme"},
                  "department": None}
        text = queue_service.assignment_text(ticket, result,
                                             {"name": "Hakan Bulut"})

        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertNotIn(f"#{ticket_id}", text)
        self.assertIn("Görüntü İşleme", text)
        self.assertIn("Hakan Bulut", text)

    def test_queue_notification_uses_the_label(self):
        ticket_id = self.open_ticket()
        text = telegram_ui.queue_notice(ticket_id, "Görüntü İşleme")

        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertNotIn("#", text)
        self.assertIn("Görüntü İşleme", text)

    def test_resolved_and_reopen_notices_use_the_label(self):
        for builder in (telegram_ui.resolved_notice, telegram_ui.reopened_notice,
                        telegram_ui.already_resolved_notice,
                        telegram_ui.manual_triage_notice):
            with self.subTest(builder=builder.__name__):
                text = builder(42)
                self.assertIn("🎫 Talep 42", text)
                self.assertNotIn("#42", text)

    def test_worker_notification_uses_the_label(self):
        """queue_worker gerçek Telegram'a çıkmadan doğru etiketi gönderir."""
        hakan = self.make_technician("Hakan Bulut", skills=((self.cv, 1),))
        ticket_id = self.open_ticket()
        db.mark_escalation_pending(ticket_id, department="Yazılım")
        db.set_ticket_routing(ticket_id, config.ROUTING_BASIS_SPECIFIC_SKILL,
                              skill_code="COMPUTER_VISION", department="Yazılım")

        sender = RecordingSender()
        stats = queue_worker.run_once(sender=sender)

        self.assertEqual(stats["assigned"], 1)
        user_messages = [c["text"] for c in sender.calls
                         if c["chat_id"] == self.USER_ID]
        self.assertTrue(user_messages)
        self.assertIn(f"🎫 Talep {ticket_id}", user_messages[0])
        self.assertNotIn(f"#{ticket_id}", user_messages[0])
        del hakan

    def test_status_change_notice_uses_user_facing_status(self):
        """Kullanıcıya panel dili ('Atandı') değil anlam gösterilir."""
        text = telegram_ui.status_change_notice(42, config.STATUS_ASSIGNED)

        self.assertIn("🎫 Talep 42", text)
        self.assertIn("Teknik ekipte", text)
        self.assertNotIn("#42", text)

    def test_label_helper_is_the_single_definition(self):
        """§19: etiket biçimi TEK yerde tanımlıdır."""
        with open(project_file("telegram_ui.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="telegram_ui.py")
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)]
        self.assertEqual(names.count("format_ticket_label"), 1)

        # bot ve queue_service kendi biçimlerini uydurmamalı.
        for module in ("bot.py", "queue_service.py"):
            with self.subTest(module=module):
                with open(project_file(module), encoding="utf-8") as fh:
                    source = fh.read()
                self.assertNotIn("🎫 Talep {", source,
                                 f"{module} etiketi elle kurmamalı.")


# ==================================================================
# §23: SAME-TICKET POLİTİKASI
# ==================================================================

class TestSameTicketPreserved(TelegramUxTestCase):

    def test_new_ticket_button_does_not_open_a_second_ticket(self):
        """§23: devam eden talep varken yeni kayıt AÇILMAZ."""
        ticket_id = self.open_ticket("Kamera aracımı algılamıyor")

        update, _ = self.press(telegram_ui.BTN_NEW_TICKET)

        self.assertEqual(len(db.list_tickets_for_user(self.USER_ID)), 1)
        text = update.message.replies[0]["text"]
        self.assertIn("Devam eden bir talebiniz bulunuyor", text)
        self.assertIn(f"🎫 Talep {ticket_id}", text)

    def test_plain_text_still_appends_to_the_open_ticket(self):
        ticket_id = self.open_ticket("Kamera aracımı algılamıyor")
        db.mark_escalation_pending(ticket_id, department="Yazılım")

        update = FakeUpdate(self.user, "Hâlâ çözülmedi")
        run(bot.handle_plain_text(update, FakeContext()))

        self.assertEqual(len(db.list_tickets_for_user(self.USER_ID)), 1)
        texts = [m["message_text"] for m in db.get_messages(ticket_id)]
        self.assertIn("Hâlâ çözülmedi", texts)

    def test_message_added_notice_uses_the_label(self):
        ticket_id = self.open_ticket()
        hakan = self.make_technician("Hakan Bulut")
        db.assign_primary_if_unassigned(ticket_id, hakan, "Hakan Bulut")

        update = FakeUpdate(self.user, "Bir güncelleme var mı?")
        run(bot.handle_plain_text(update, FakeContext()))

        text = update.message.replies[0]["text"]
        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertNotIn(f"#{ticket_id}", text)
        self.assertIn("Hakan Bulut", text)


# ==================================================================
# §22: INLINE BUTONLAR AYRI VE KORUNUYOR
# ==================================================================

class TestInlineKeyboardsPreserved(TelegramUxTestCase):

    def test_solution_choice_keyboard_still_has_three_actions(self):
        markup = bot.solution_choice_keyboard(42)
        labels = [b.text for row in markup.inline_keyboard for b in row]

        self.assertEqual(labels, ["✅ Çözüldü", "🔄 Devam ediyor",
                                  "👨‍🔧 Teknik destek istiyorum"])

    def test_escalate_only_keyboard_still_exists(self):
        markup = bot.escalate_only_keyboard(42)
        labels = [b.text for row in markup.inline_keyboard for b in row]

        self.assertEqual(labels, ["✅ Çözüldü", "👨‍🔧 Teknik destek istiyorum"])

    def test_reopen_keyboard_still_exists(self):
        markup = bot.reopen_keyboard(42)
        labels = [b.text for row in markup.inline_keyboard for b in row]

        self.assertEqual(labels, ["🔄 Sorun hâlâ devam ediyor"])

    def test_inline_and_reply_keyboards_are_different_types(self):
        """§22: iki klavye türü BİRBİRİNE KARIŞTIRILMAZ."""
        reply_markup = bot.main_menu_keyboard()
        inline_markup = bot.solution_choice_keyboard(42)

        self.assertTrue(hasattr(reply_markup, "keyboard"))
        self.assertFalse(hasattr(reply_markup, "inline_keyboard"))
        self.assertTrue(hasattr(inline_markup, "inline_keyboard"))
        self.assertFalse(hasattr(inline_markup, "keyboard"))

    def test_resolve_button_uses_the_new_label(self):
        ticket_id = self.open_ticket()
        update = FakeCallbackUpdate(f"solved:{ticket_id}", self.user)

        run(bot.button_callback(update, FakeContext()))

        edit = update.callback_query.edits[0]
        self.assertIn(f"🎫 Talep {ticket_id}", edit["text"])
        self.assertNotIn(f"#{ticket_id}", edit["text"])
        self.assertIsNotNone(edit["reply_markup"],
                             "Yeniden açma butonu korunmalı.")
        self.assertEqual(db.get_ticket(ticket_id)["status"],
                         config.STATUS_CLOSED)

    def test_reopen_button_uses_the_new_label(self):
        ticket_id = self.open_ticket()
        db.resolve_ticket_if_open(ticket_id, source=config.RESOLVED_SOURCE_AI)

        update = FakeCallbackUpdate(f"reopen:{ticket_id}", self.user)
        run(bot.button_callback(update, FakeContext()))

        self.assertIn(f"🎫 Talep {ticket_id}",
                      update.callback_query.edits[0]["text"])
        self.assertEqual(db.get_ticket(ticket_id)["status"],
                         config.STATUS_REOPENED)

    def test_callback_still_rejects_foreign_tickets(self):
        """Yetki kontrolü korunur: başkasının talebine dokunulamaz."""
        ticket_id = self.open_ticket()
        intruder = FakeUser(9999, "Yabancı")
        update = FakeCallbackUpdate(f"solved:{ticket_id}", intruder)

        run(bot.button_callback(update, FakeContext()))

        self.assertTrue(update.callback_query.answers[0]["show_alert"])
        self.assertEqual(update.callback_query.edits, [])
        self.assertNotEqual(db.get_ticket(ticket_id)["status"],
                            config.STATUS_CLOSED)


# ==================================================================
# §4-§9: ESKALASYON AKIŞININ TELEGRAM TARAFI
# ==================================================================

class TestEscalationTelegramFlow(TelegramUxTestCase):

    def _escalate(self, classification):
        ticket_id = self.open_ticket()
        self.stub_classification(classification)
        context = FakeContext()
        run(bot.start_escalation(context, ticket_id, self.USER_ID))
        return ticket_id, context

    def user_texts(self, context):
        return [s["text"] for s in context.bot.sent
                if s["chat_id"] == self.USER_ID]

    def test_specific_skill_with_candidate_notifies_assignment(self):
        hakan = self.make_technician("Hakan Bulut", department="Yazılım",
                                     skills=((self.cv, 1),))
        ticket_id, context = self._escalate({
            "ok": True, "classification_type": "specific_skill",
            "category": "Yazılım", "required_skill": "COMPUTER_VISION"})

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["assigned_technician_id"], hakan)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_SPECIFIC_SKILL)

        text = self.user_texts(context)[0]
        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertIn("Görüntü İşleme", text)
        self.assertIn("Hakan Bulut", text)

    def test_specific_skill_without_candidate_stays_in_its_queue(self):
        """§4: uzman yok -> GENEL DESTEĞE DÜŞMEZ, kendi kuyruğunda bekler."""
        genelci = self.make_technician("Genelci", department="Yazılım",
                                       skills=((self.general, 1),))
        ticket_id, context = self._escalate({
            "ok": True, "classification_type": "specific_skill",
            "category": "Yazılım", "required_skill": "COMPUTER_VISION"})

        ticket = db.get_ticket(ticket_id)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertEqual(ticket["required_skill_code"], "COMPUTER_VISION")
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_SPECIFIC_SKILL)
        self.assertEqual(db.count_active_tickets_for_technician(genelci), 0)

        text = self.user_texts(context)[0]
        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertIn("Görüntü İşleme", text)

    def test_explicit_general_support_routes_to_general(self):
        genelci = self.make_technician("Genelci", department="Donanım",
                                       skills=((self.general, 1),))
        ticket_id, context = self._escalate({
            "ok": True, "classification_type": "general_support",
            "category": "Donanım",
            "required_skill": config.GENERAL_SUPPORT_SKILL_CODE})

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["assigned_technician_id"], genelci)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_GENERAL_SUPPORT)
        self.assertIn(config.GENERAL_SUPPORT_SKILL_NAME,
                      self.user_texts(context)[0])

    def test_department_classification_routes_to_department(self):
        ayse = self.make_technician("Ayşe Demir", department="Ağ")
        ticket_id, context = self._escalate({
            "ok": True, "classification_type": "department",
            "category": "Ağ", "required_skill": None})

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["assigned_technician_id"], ayse)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_DEPARTMENT)
        self.assertIn("Ayşe Demir", self.user_texts(context)[0])

    def test_classification_failure_goes_to_manual_triage_not_general(self):
        """§7, §8: parse hatası GENEL DESTEĞE GİTMEZ."""
        genelci = self.make_technician("Genelci", department="Yazılım",
                                       skills=((self.general, 1),))
        ticket_id, context = self._escalate({
            "ok": False, "classification_type": None,
            "category": None, "required_skill": None})

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)
        self.assertIsNone(ticket["assigned_technician_id"])
        self.assertIsNone(ticket["required_skill_code"])
        self.assertEqual(ticket["status"], config.STATUS_ESCALATION_PENDING,
                         "Talep KAYBOLMAMALI.")
        self.assertEqual(db.count_active_tickets_for_technician(genelci), 0)

        text = self.user_texts(context)[0]
        self.assertIn(f"🎫 Talep {ticket_id}", text)
        self.assertNotIn("sınıflandır", text.lower(),
                         "Kullanıcıya teknik itiraf yapılmamalı.")

    def test_invalid_skill_code_goes_to_manual_triage(self):
        """§9: uydurma kod + kategori yok -> manuel triyaj."""
        ticket_id, _context = self._escalate({
            "ok": True, "classification_type": "specific_skill",
            "category": None, "required_skill": "KUANTUM_ISINLAMA"})

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["routing_basis"],
                         config.ROUTING_BASIS_MANUAL_TRIAGE)
        self.assertIsNone(ticket["required_skill_code"],
                          "Doğrulanmamış kod DB'ye yazılmamalı.")

    def test_escalation_starts_sla_and_switches_to_human_mode(self):
        """Korunan davranış: HUMAN_ASSISTED + SLA başlangıcı."""
        ticket_id, _ = self._escalate({
            "ok": True, "classification_type": "department",
            "category": "Ağ", "required_skill": None})

        ticket = db.get_ticket(ticket_id)
        self.assertEqual(ticket["ai_mode"], config.AI_MODE_HUMAN)
        self.assertIsNotNone(ticket["sla_started_at"])
        self.assertEqual(ticket["escalated"], 1)

    def test_escalation_writes_a_routing_audit_message(self):
        """§13: yönlendirme dayanağı talep geçmişine düşer."""
        self.make_technician("Hakan Bulut", department="Yazılım",
                             skills=((self.cv, 1),))
        ticket_id, _ = self._escalate({
            "ok": True, "classification_type": "specific_skill",
            "category": "Yazılım", "required_skill": "COMPUTER_VISION"})

        system_texts = [m["message_text"] for m in db.get_messages(ticket_id)
                        if m["message_kind"] == config.MESSAGE_KIND_SYSTEM]
        self.assertTrue(any("Yönlendirme" in t for t in system_texts),
                        system_texts)
        self.assertTrue(any("Görüntü İşleme" in t for t in system_texts),
                        system_texts)


# ==================================================================
# GERÇEK AĞ ÇAĞRISI YOK
# ==================================================================

class TestNoRealNetworkCalls(TelegramUxTestCase):

    def test_ai_stub_is_active_by_default(self):
        """Stub'lanmamış bir Gemini çağrısı testi PATLATIR."""
        with self.assertRaises(AssertionError):
            bot.ai_assistant.classify_for_escalation("Konu", [], [])

    def test_telegram_ui_does_not_import_the_telegram_package(self):
        """telegram_ui saf sunum katmanıdır; ağ/kütüphane bağımlılığı yoktur."""
        with open(project_file("telegram_ui.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename="telegram_ui.py")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertNotIn("telegram", imported)
        self.assertNotIn("requests", imported)

    def test_queue_service_stays_free_of_telegram_objects(self):
        with open(project_file("queue_service.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("inline_keyboard", source)
        self.assertNotIn("ReplyKeyboardMarkup", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
