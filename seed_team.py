"""
Bilgi İşlem departmanı kadrosunu tek seferde technicians tablosuna ekler.
Kullanım: python seed_team.py

Zaten eklenmiş kayıtları tekrar eklemez (isme göre kontrol eder).
"""

import db
import config

TEAM = [
    ("Ahmet Demir",   "Bilgi İşlem Müdürü",          "Diğer"),
    ("Elif Kaya",      "Ekip Lideri",                  "Diğer"),
    ("Mehmet Şahin",   "Bilgisayar Programcısı",       "Yazılım"),
    ("Zeynep Arslan",  "Bilgisayar Programcısı",       "Yazılım"),
    ("Burak Yıldız",   "Backend / Database Uzmanı",    "Yazılım"),
    ("Selin Aydın",    "Yazılım Mühendisi",            "Yazılım"),
    ("Emre Koç",       "Bilgisayar Mühendisi (RPA)",   "Yazılım"),
    ("Aylin Er",       "Sekreter",                     "Hesap/Erişim"),
    ("Kaan Polat",     "Ağ Uzmanı",                    "Ağ"),
    ("Derya Uçar",     "Ağ Teknisyeni",                "Ağ"),
    ("Onur Çetin",     "Donanım Teknisyeni",           "Donanım"),
    ("Gizem Şen",      "Donanım Teknisyeni",           "Donanım"),
    ("Hakan Bulut",    "Sistem Yöneticisi",            "Ağ"),
    ("Merve Tan",      "Yardım Masası Uzmanı",         "Diğer"),
    ("Cem Aksoy",      "Yardım Masası Uzmanı",         "Diğer"),
]


def main():
    db.init_db()
    existing_names = {t["name"] for t in db.list_technicians()}

    added = 0
    for name, role, department in TEAM:
        if name in existing_names:
            print(f"Atlandı (zaten var): {name}")
            continue
        if department not in config.DEPARTMENTS:
            print(f"UYARI: '{department}' geçerli bir departman değil, atlandı: {name}")
            continue
        db.create_technician(name, None, department, role)
        print(f"Eklendi: {name} ({role} — {department})")
        added += 1

    print(f"\nToplam {added} kişi eklendi. Ekip sayfasını (/ekip) yenileyip kontrol edebilirsin.")


if __name__ == "__main__":
    main()
