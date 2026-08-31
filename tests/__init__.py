"""Kalıcı regression test paketi.

ACCEPTANCE FIX 2: testler artık geçici dizinde DEĞİL, projenin içinde durur.
Bir sonraki oturumda kaybolmazlar.

Çalıştırma:

    python run_all_tests.py                     # hepsi
    python run_all_tests.py -v                  # ayrıntılı
    python run_all_tests.py routing             # tek paket
    python -m unittest tests.test_authorization # doğrudan

KURAL: bu dosya config / db / webapp IMPORT ETMEZ. Test modülleri kendi
geçici SQLite dosyalarını `_support.setup_test_db()` ile config import
EDİLMEDEN ÖNCE ayarlar; burada erken bir import olursa o mekanizma bozulur ve
testler gerçek destek.db üzerinde çalışmaya başlar.
"""

import os
import sys

#: Proje kökü. Testler dosya okurken (bot.py kaynak taraması, style.css)
#: buna göre mutlak yol kurar; hangi dizinden çalıştırıldıklarından bağımsız
#: olarak doğru dosyayı bulurlar.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `python tests/test_x.py` gibi doğrudan çalıştırmalarda da proje kökü
# import edilebilir olsun.
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def project_file(*parts) -> str:
    """Proje köküne göre mutlak yol: project_file('bot.py')."""
    return os.path.join(PROJECT_ROOT, *parts)
