"""Bütün regression paketlerini çalıştırır.

    python run_all_tests.py            # hepsi
    python run_all_tests.py -v         # ayrıntılı
    python run_all_tests.py routing    # yalnızca adı eşleşen paket(ler)

Paketler PROJENİN İÇİNDE, tests/ klasöründe durur — geçici dizinde değil.

NEDEN ALT SÜREÇ?
----------------
Her paket kendi geçici SQLite dosyasını kullanır ve bunu config import
EDİLMEDEN ÖNCE (test_support.setup_test_db) ayarlar. config bir kez import
edildikten sonra DB_PATH değiştirilemez; bu yüzden paketler tek bir Python
sürecinde ARDIŞIK çalıştırılamaz. Her paket ayrı bir alt süreçte koşar —
böylece paketler birbirinin verisini de göremez.

GÜVENLİK: hiçbir paket gerçek Gemini veya gerçek Telegram çağrısı yapmaz;
gönderim fonksiyonları enjekte edilir (bkz. test_support.RecordingSender).
Projedeki destek.db dosyasına DOKUNULMAZ.
"""

import os
import re
import subprocess
import sys
import time

#: (paket modülü, insan tarafından okunabilir ad)
PACKAGES = [
    ("tests.test_routing_auto_assignment", "Routing — otomatik teknisyen seçimi"),
    ("tests.test_routing_general_fallback",
     "Routing — genel destek catch-all DEĞİLDİR (§24-§27)"),
    ("tests.test_queue_auto_assignment", "Kuyruk — otomatik atama ve concurrency"),
    ("tests.test_queue_priority_fifo",
     "Kuyruk — öncelik + FIFO işleme sırası (§C)"),
    ("tests.test_ai_priority", "AI destekli P1-P4 önceliği (§B)"),
    ("tests.test_telegram_ux", "Telegram UX — ana menü, etiket, uyumluluk (§14-§23)"),
    ("tests.test_authorization", "Yetkilendirme — ekip görünürlüğü ve 403 kontrolleri"),
    ("tests.test_ui_csrf_forms", "UI / CSRF — form coverage ve kuyruk UX'i"),
    ("tests.test_frontend_design",
     "Frontend tasarım revizyonu — navbar, liste, conversation, drawer (§A)"),
    ("tests.test_preserved_behavior", "Korunan davranış — AI, atama, SLA, güvenlik"),
]

_SUMMARY = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.MULTILINE)


def _print_safe(text: str):
    """Konsol kod sayfası (Windows cp1254) taşıyamayan karakterleri yutmadan yazar.

    Hata çıktısı Türkçe karakter ve unicode kaçış içerebilir; bunun yüzünden
    RAPOR KAYBOLMAMALIDIR."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding)
    print(safe)


def run_package(module: str, verbose: bool):
    """Paketi alt süreçte çalıştırır; (basarili_mi, test_sayisi, ozet) döner."""
    command = [sys.executable, "-m", "unittest", module]
    if verbose:
        command.append("-v")

    started = time.time()
    process = subprocess.run(command, capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             cwd=os.path.dirname(os.path.abspath(__file__)))
    elapsed = time.time() - started

    output = (process.stdout or "") + (process.stderr or "")
    match = _SUMMARY.search(output)
    count = int(match.group(1)) if match else 0
    ok = process.returncode == 0

    if not ok or verbose:
        _print_safe(output.rstrip())
    return ok, count, elapsed


def main():
    args = [a for a in sys.argv[1:] if a not in ("-v", "--verbose")]
    verbose = len(args) != len(sys.argv[1:])

    selected = PACKAGES
    if args:
        needle = args[0].lower()
        selected = [p for p in PACKAGES if needle in p[0].lower()]
        if not selected:
            print(f"'{args[0]}' ile eşleşen paket yok. Mevcut paketler:")
            for module, label in PACKAGES:
                print(f"  {module:<34} {label}")
            return 2

    print("=" * 70)
    print(f"REGRESSION — {len(selected)} paket")
    print("=" * 70)

    results = []
    total_tests = 0
    for module, label in selected:
        print(f"\n>> {label}")
        print(f"   ({module})")
        ok, count, elapsed = run_package(module, verbose)
        total_tests += count
        results.append((module, label, ok, count))
        print(f"   {'GEÇTİ ' if ok else 'BAŞARISIZ'} — {count} kontrol, {elapsed:.1f}s")

    print("\n" + "=" * 70)
    print("ÖZET")
    print("=" * 70)
    for module, label, ok, count in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {count:>4} kontrol  {label}")

    failed = [r for r in results if not r[2]]
    print("-" * 70)
    print(f"  TOPLAM: {total_tests} kontrol / {len(results)} paket")
    if failed:
        print(f"  BAŞARISIZ PAKET: {len(failed)}")
        return 1
    print("  SONUÇ: TEMİZ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
