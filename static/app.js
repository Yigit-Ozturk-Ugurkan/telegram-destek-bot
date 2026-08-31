/* Destek Masası — MİNİMAL vanilla JS.

   Framework YOKTUR. Bu dosyanın tek sorumluluğu küçük UI davranışlarıdır:

     1) form güvenliği   : çift gönderim engeli, bekleme durumu, onay
     2) avatar dropdown  : navbar sağındaki hesap menüsü
     3) ticket drawer    : sağdan açılan talep ayarları slide-over'ı
     4) avatar fallback  : illüstratif avatar yüklenemezse initials'a düş

   İŞ MANTIĞI BURADA YOKTUR. Sunucu tarafındaki tek kullanımlık form_token,
   CSRF doğrulaması ve yetkilendirme bu dosya hiç çalışmasa da geçerlidir;
   JS kapalıyken drawer içindeki formlar da normal HTML formu olarak
   çalışmaya devam eder (drawer görünür hâle getirilir).
*/
(function () {
  "use strict";

  /* ---------------- 1) Form güvenliği ---------------- */

  var forms = document.querySelectorAll("form[data-guard]");

  function setPending(form, pending) {
    var button = form.querySelector("button[type=submit]");
    if (!button) { return; }
    if (pending) {
      button.dataset.idleLabel = button.textContent;
      if (button.dataset.pendingLabel) {
        button.textContent = button.dataset.pendingLabel;
      }
      button.setAttribute("aria-busy", "true");
      // Butonu senkron devre dışı bırakmak gönderimi iptal edebildiği için
      // bir sonraki tick'e bırakıyoruz.
      window.setTimeout(function () { button.disabled = true; }, 0);
    } else {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      if (button.dataset.idleLabel) {
        button.textContent = button.dataset.idleLabel;
      }
    }
  }

  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.submitting === "1") {
        event.preventDefault();
        return;
      }

      // Geri alınamaz seçim (talebi kapatma) için açık onay iste.
      var trigger = form.querySelector("[data-confirm-value]");
      if (trigger && trigger.value === trigger.dataset.confirmValue) {
        if (!window.confirm(trigger.dataset.confirmMessage)) {
          event.preventDefault();
          return;
        }
      }

      form.dataset.submitting = "1";
      setPending(form, true);
    });
  });

  // Geri tuşuyla bfcache'ten dönüldüğünde form yeniden kullanılabilir olmalı.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) { return; }
    Array.prototype.forEach.call(forms, function (form) {
      form.dataset.submitting = "0";
      setPending(form, false);
    });
  });

  // Yönlendirme sonrası hata mesajı klavye ve ekran okuyucu kullanıcısının
  // dikkatine gelsin (WCAG 2.2 — hata özeti odaklama).
  var alertBox = document.querySelector("[data-autofocus]");
  if (alertBox) { alertBox.focus(); }


  /* ---------------- 2) Avatar dropdown ---------------- */

  var menuTrigger = document.querySelector("[data-menu-trigger]");
  var menu = menuTrigger
    ? document.getElementById(menuTrigger.dataset.menuTrigger)
    : null;

  function closeMenu() {
    if (!menu) { return; }
    menu.hidden = true;
    menuTrigger.setAttribute("aria-expanded", "false");
  }

  if (menuTrigger && menu) {
    menuTrigger.addEventListener("click", function (event) {
      event.stopPropagation();
      var open = menu.hidden;
      menu.hidden = !open;
      menuTrigger.setAttribute("aria-expanded", open ? "true" : "false");
    });
    menu.addEventListener("click", function (event) { event.stopPropagation(); });
    document.addEventListener("click", closeMenu);
  }


  /* ---------------- 3) Ticket settings drawer ---------------- */

  var drawer = document.getElementById("ticket-settings");
  var overlay = document.querySelector("[data-drawer-overlay='ticket-settings']");
  var openers = document.querySelectorAll("[data-drawer-open='ticket-settings']");
  var closers = document.querySelectorAll("[data-drawer-close='ticket-settings']");
  var lastFocused = null;

  function openDrawer() {
    if (!drawer) { return; }
    lastFocused = document.activeElement;
    drawer.hidden = false;
    if (overlay) { overlay.hidden = false; }
    var focusable = drawer.querySelector("button, [href], select, textarea, input");
    if (focusable) { focusable.focus(); }
  }

  function closeDrawer() {
    if (!drawer) { return; }
    drawer.hidden = true;
    if (overlay) { overlay.hidden = true; }
    if (lastFocused && lastFocused.focus) { lastFocused.focus(); }
  }

  Array.prototype.forEach.call(openers, function (button) {
    button.addEventListener("click", openDrawer);
  });
  Array.prototype.forEach.call(closers, function (button) {
    button.addEventListener("click", closeDrawer);
  });
  if (overlay) { overlay.addEventListener("click", closeDrawer); }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    closeMenu();
    if (drawer && !drawer.hidden) { closeDrawer(); }
  });

  // Doğrulama hatası olan bir drawer formu gönderilirse tarayıcı görünmeyen
  // alana odaklanamaz; bu yüzden geçersiz alan bulunduğunda drawer açılır.
  if (drawer) {
    drawer.addEventListener("invalid", function () {
      if (drawer.hidden) { openDrawer(); }
    }, true);
  }


  /* ---------------- 4) İllüstratif avatar fallback ----------------
     static/avatars/*.svg yüklenemezse (dosya eksik, ağ hatası, engellenmiş
     istek) <img> DOM'dan çıkarılır ve ALTINDAKİ initials avatarı görünür
     kalır. 'error' olayı balonlanmadığı için capture fazında dinliyoruz. */

  document.addEventListener("error", function (event) {
    var element = event.target;
    if (element && element.tagName === "IMG" &&
        element.classList.contains("rail-avatar-img")) {
      element.remove();
    }
  }, true);


  /* ---------------- 5) Konuşma en alttan başlar ---------------- */
  // En yeni mesaj görünür olsun; composer zaten kartın altında sabittir.
  var thread = document.getElementById("thread");
  if (thread) { thread.scrollTop = thread.scrollHeight; }
})();
