# Telegram Destek Talebi Bildirim ve Yönetim Sistemi

Kullanıcıların Telegram üzerinden gönderdiği mesajlarla otomatik destek talebi (ticket) açan,
yöneticilere anlık bildirim gönderen ve talepleri hem bot komutlarıyla hem de basit bir
web panelinden yönetmeyi sağlayan sistem. Gemini AI ile gelen talepleri otomatik sınıflandırıp
ilk çözüm önerisi üretir; öneri yetmezse talebi ilgili departmandaki müsait teknisyene yönlendirir.

## Mimari

```
Kullanıcı (Telegram) --> Bot (polling) --> SQLite veritabanı <-- Web Paneli (Flask)
                                                  |
                                                  v
                                     Yöneticilere bildirim (Telegram)
```

- **bot.py** — Telegram bot. Kullanıcı mesajlarını talebe çevirir, yöneticilere bildirim
  gönderir, admin komutlarını (`/liste`, `/detay`, `/ata`, `/yanitla`, `/kapat`) işler.
- **webapp.py** — Flask web paneli. Talepleri tarayıcıdan listeleme, detay görme,
  üstlenme, yanıtlama ve durum değiştirme.
- **db.py** — Ortak SQLite veritabanı katmanı (`destek.db`). Bot ve web paneli aynı
  veritabanını paylaşır.
- **config.py** — Tüm ayarlar (token, admin listesi, şifreler).

Bağlantı biçimi olarak **polling** kullanıldı: ekstra sunucu/domain/SSL gerektirmez,
`python bot.py` komutuyla doğrudan çalışır. İleride trafik artarsa Application Builder'da
`run_polling()` yerine `run_webhook()` kullanılarak webhook moduna geçilebilir (bunun için
herkese açık bir HTTPS adresi gerekir).

## Kurulum

### 1. Bot oluşturma

1. Telegram'da **@BotFather**'a yazın.
2. `/newbot` komutuyla yeni bot oluşturun, adını ve kullanıcı adını belirleyin.
3. Size verilen **token**'ı not edin (örnek: `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`).

### 2. Yönetici ID'lerini öğrenme

1. Telegram'da **@userinfobot**'a mesaj atın, size Telegram ID'nizi verecektir.
2. Bu ID'yi `config.py` içindeki `ADMIN_IDS` listesine ekleyin.

### 3. Gemini API anahtarı alma (ücretsiz)

1. https://aistudio.google.com/apikey adresine git, Google hesabınla giriş yap.
2. **Create API key** butonuna tıkla. Google otomatik olarak bir proje oluşturup anahtarı verir.
3. Verilen anahtarı kopyala (örnek: `AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`).
4. Ücretsiz kotayla başlarsın; yoğun kullanımda ücretli katmana geçmen gerekebilir
   (güncel limitler için AI Studio'daki "Usage" sayfasına bakabilirsin).

### 4. Ortam değişkenleri

Proje klasöründe bir `.env` dosyası yerine, doğrudan ortam değişkeni olarak ayarlayabilir
ya da `config.py` içine sabit değer olarak yazabilirsiniz:

```bash
export BOT_TOKEN="123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export ADMIN_GROUP_ID=""          # opsiyonel: bildirimlerin gideceği grup ID'si
export WEB_ADMIN_PASSWORD="guclu-bir-sifre"
export WEB_SECRET_KEY="rastgele-uzun-bir-anahtar"
export GEMINI_API_KEY="AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

> **Not:** `GEMINI_API_KEY` boş bırakılırsa sistem çökmez — sadece AI önerisi
> özelliği devre dışı kalır, talepler normal şekilde açılmaya devam eder.

`config.py` içindeki `ADMIN_IDS` listesine kendi Telegram ID'nizi eklemeyi unutmayın:

```python
ADMIN_IDS = [123456789]
```

> **Not:** `ADMIN_GROUP_ID` boş bırakılırsa yeni talep bildirimleri `ADMIN_IDS`
> listesindeki herkese tek tek gönderilir. Bir grup/kanal kullanmak isterseniz botu
> o gruba ekleyip grup ID'sini buraya yazabilirsiniz.

### 5. Bağımlılıkları kurma

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 6. Bilgi İşlem kadrosunu tek seferde yükleme (opsiyonel)

Teşkilat, kadroyu tek tek web panelden eklemek yerine hazır bir liste ile yükleyebilirsin.
`seed_team.py` dosyasında 15 kişilik örnek bir Bilgi İşlem kadrosu tanımlı (isim, unvan,
departman). Kendi kadronu bu dosyada düzenleyip şunu çalıştır:

```bash
python seed_team.py
```

Zaten eklenmiş isimler tekrar eklenmez, script'i güvenle birden fazla kez çalıştırabilirsin.
Kişilerin Telegram ID'si eklenmediyse (script'te `None`), o kişiye otomatik bildirim gitmez;
sadece web panelden "Ekip" sayfasında takip edilir ve talepler ona atanabilir. Bildirim
istiyorsan `/ekip` sayfasından o kişiyi düzenle veya bot üzerinden `/ekibeekle` ile
telegram_id'siyle yeniden ekle.

### 7. Çalıştırma

İki bileşen ayrı süreçlerde çalışır (aynı `destek.db` dosyasını paylaşırlar):

```bash
# Terminal 1 — Telegram bot
python bot.py

# Terminal 2 — Web paneli
python webapp.py
```

Web paneli varsayılan olarak `http://localhost:5000` adresinde açılır.

### Hesaplar ve şifreler (Faz 2.5)

Sırlar artık **kaynak kodda tutulmaz**; `.env` dosyasından okunur
(`.env.example` dosyasını kopyalayarak başlayın).

- **İlk admin hesabı** yalnızca `.env` içindeki `WEB_ADMIN_USERNAME` /
  `WEB_ADMIN_PASSWORD` ile *bootstrap* edilir. Hesap oluştuktan sonra bu
  değişken kaldırılabilir; mevcut hesabın şifresi asla sıfırlanmaz.
- **Teknisyen hesapları için ortak/varsayılan şifre YOKTUR.** Admin, Ekip
  sayfasından her kişi için ayrı bir kullanıcı adı ve **geçici şifre** belirler.
  Kullanıcı ilk girişte şifresini değiştirmek zorundadır.
- Şifreler yalnızca hash olarak saklanır. Admin hiçbir kullanıcının mevcut
  şifresini göremez; yalnızca yeni bir geçici şifre atayabilir.
- Herkes `Şifremi Değiştir` ekranından kendi şifresini güncelleyebilir.

> HTTPS arkasında çalıştırıyorsanız `.env` içinde `SESSION_COOKIE_SECURE=1` yapın.

## AI destekli otomatik triyaj akışı

1. Kullanıcı talep açar (**🆕 Yeni Talep** butonu, `/istek ...` ya da doğrudan mesaj).
2. **Gemini** ilk seviye asistan olarak çalışır; en fazla 3 çözüm denemesi yapar.
   Açıklayıcı soru sormak deneme hakkı harcamaz.
3. Kullanıcı sorunu çözerse **✅ Çözüldü** ile talebi kapatır.
4. Çözülmezse (ya da kullanıcı **👨‍🔧 Teknik destek istiyorum** derse) talep
   sınıflandırılır ve teknik ekibe aktarılır.
5. Uygun kişi bulunursa **otomatik olarak ana sorumlu atanır**; bulunmazsa talep
   kendi kuyruğunda bekler ve biri müsait olduğunda otomatik atanır.
   Son kullanıcıya teknik personel **seçtirilmez**.

### Sınıflandırma sonucu: dört yönlendirme dayanağı

Sınıflandırma **server tarafında doğrulanır**; model source-of-truth değildir.
Sonuç şu dört dayanaktan biridir:

| Dayanak | Ne zaman | Kuyruk |
| --- | --- | --- |
| `SPECIFIC_SKILL` | Geçerli ve **aktif** bir uzmanlık kodu üretildi | O uzmanlığın kuyruğu |
| `DEPARTMENT` | Uzmanlık yok ama geçerli bir departman var | O departmanın kuyruğu |
| `GENERAL_SUPPORT` | Model **açıkça** `general_support` dedi | `GENERAL_TECH_SUPPORT` kuyruğu |
| `MANUAL_TRIAGE` | Sınıflandırma başarısız / belirsiz / geçersiz | Admin incelemesi |

> **`GENERAL_TECH_SUPPORT` bir catch-all DEĞİLDİR.**
> Belirsizlik, katalogda olmayan kod, pasif uzmanlık, bozuk JSON, Gemini
> timeout'u ve **"uygun kişi bulunamadı"** durumlarının hiçbiri genel desteğe
> dönüşmez. Bir talebin hangi uzmanlığa ait olduğu biliniyorsa, o uzman müsait
> olana kadar **kendi kuyruğunda** bekler; başka bir uzmanlık sınıfına
> kaydırılmaz. `GENERAL_TECH_SUPPORT` yalnızca gerçekten genel masaüstü /
> temel kullanıcı desteği olan talepler için kullanılır ve teknisyenlere normal
> 1/2/3 öncelik sistemi içinde atanır — bu skill'e sahip olmak kimseyi bütün
> taleplerin fallback'i yapmaz.

Yönlendirme dayanağı `tickets.routing_basis` alanında saklanır ve admin
panelinde talep detayındaki **Yönlendirme** satırında, kuyruk ekranında ise
**Bekleme nedeni** sütununda görünür.

## Kullanım

### Kullanıcı tarafı (herkes)

Birincil arayüz `/start` sonrası açılan **kalıcı menüdür**; komut ezberlemek
gerekmez:

```
[ 🆕 Yeni Talep ] [ 📋 Taleplerim ]
[ ❓ Yardım ]
```

- **🆕 Yeni Talep** — yeni talep açar. Devam eden bir talebiniz varsa yenisi
  açılmaz, mevcut talebe yazmaya devam edersiniz.
- **📋 Taleplerim** — taleplerinizi durumu ve atanan uzmanla listeler.
- **❓ Yardım** — akışı ve komutları anlatır.
- Doğrudan mesaj yazmak da talep açar; açık bir talebiniz varsa mesaj o talebe eklenir.

Slash komutlar **kaldırılmadı**; eski kullanıcılar ve power-user'lar için
çalışmaya devam eder:

- `/start`, `/istek <talebiniz>`, `/taleplerim`, `/yardim`

Talep gösterimi kullanıcı tarafında `🎫 Talep 42` biçimindedir (`#42` kullanılmaz).
Veritabanı kimliği değişmez; değişen yalnızca sunumdur. Admin/terminal
loglarında `#42` biçimi korunur.

### Teknik ekip üyesi tarafı

- `/musaitim` — kendini müsait olarak işaretler (bu departmanda bekleyen talep varsa otomatik atanır).
- `/mesgulum` — kendini meşgul olarak işaretler.
- Bu komutların çalışması için teknisyenin Telegram ID'sinin `/ekibeekle` ile eklenmiş olması gerekir.

### Yönetici tarafı (Telegram bot komutları)

- `/liste [acik|islemde|kapatildi]` — talepleri listeler.
- `/detay <id>` — talebin tüm mesaj geçmişini gösterir.
- `/ata <id>` — talebi kendinize atar (durumu "İşlemde" yapar).
- `/yanitla <id> <mesaj>` — kullanıcıya yanıt gönderir.
- `/kapat <id>` — talebi kapatır, kullanıcıya bilgi verir.
- Yeni talep bildirimlerindeki **✅ Üstlen** / **🔒 Kapat** butonları ile tek tıkla işlem
  yapılabilir.
- `/ekip` — teknik ekip listesini ve müsaitlik durumlarını gösterir.
- `/ekibeekle <isim> <departman> [telegram_id]` — yeni teknisyen ekler.
  Örnek: `/ekibeekle Ahmet Yılmaz Ağ 123456789`

### Yönetici tarafı (Web paneli)

- Ana sayfada tüm talepler, durum filtreleriyle (Açık / İşlemde / Kapatıldı) listelenir.
- Talep detayında: Gemini'nin ürettiği AI önerisi (varsa), departman etiketi, mesaj geçmişi,
  yanıt yazma, üstlenme ve durum değiştirme formları bulunur. Yapılan her işlem kullanıcıya
  otomatik Telegram mesajı olarak iletilir.
- **Ekip** sayfasında teknik personel eklenir, departmanı seçilir ve müsaitlik durumu
  tek tıkla değiştirilir. Bir teknisyen müsait yapıldığında, o departmanda bekleyen en eski
  talep varsa otomatik olarak ona atanır.

## Veri modeli

- **tickets**: `id, user_id, username, full_name, subject, status, assigned_admin_id, assigned_admin_name, helper_name, assigned_technician_id, category, ai_suggestion, escalated, pending_department, created_at, updated_at`
- **messages**: `id, ticket_id, sender_type (kullanici/yonetici/sistem), sender_id, sender_name, message_text, created_at`
- **technicians**: `id, name, role, telegram_id, department, available, created_at`

`helper_name`: Sorumlu zaten atanmışken farklı biri talebe yanıt yazarsa, o kişinin adı
buraya "Yardımcı Sorumlu" olarak kaydedilir — Sorumlu değişmez, ama hem web panelde hem
gerekirse bildirimlerde görünür kılınabilir.

## Sonraki adımlar için öneriler

- Şifre sıfırlama (e-posta ile) ve iki faktörlü doğrulama eklenebilir.
- Yoğun trafikte SQLite yerine PostgreSQL kullanılabilir.
- Webhook moduna geçmek isterseniz `bot.py` içindeki `application.run_polling()` satırı
  `application.run_webhook(...)` ile değiştirilip bir HTTPS endpoint tanımlanmalıdır.
- SLA takibi, otomatik hatırlatmalar veya kategori/etiket sistemi eklenebilir.
