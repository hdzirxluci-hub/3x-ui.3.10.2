<div align="center">

# PABLO — 3x-ui روی Railway

**پنل `3x-ui v3.10.2` آماده‌ی Railway، با صفحه‌ی ساب فارسی و لینک‌های HTTPS**

بدون VPS، بدون Cloudflare، بدون دیتابیس جدا و بدون متغیر دستی.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new)
[![Star on GitHub](https://img.shields.io/github/stars/hdzirxluci-hub/3x-ui.3.9.0?style=social)](https://github.com/hdzirxluci-hub/3x-ui.3.9.0)

</div>

## ✅ چه چیزهایی لازم دارد؟

| مورد | لازم است؟ | توضیح |
|---|:---:|---|
| اکانت GitHub | ✅ | برای انتخاب همین ریپو در Railway |
| اکانت Railway | ✅ | ساخت و اجرای سرویس |
| یک Railway Service | ✅ | همین ریپو را از روی Dockerfile اجرا می‌کند |
| یک Volume | ✅ | فقط برای نگه‌داشتن دیتابیس پنل روی `/etc/x-ui` |
| Railway Domain | ✅ | از بخش Networking با **Generate Domain** ساخته می‌شود |
| PostgreSQL / MySQL / Redis | ❌ | پنل دیتابیس SQLite خودش را دارد |
| Cloudflare یا دامنه شخصی | ❌ | دامنه‌ی رایگان `up.railway.app` کافی است |
| متغیر دستی در Variables | ❌ | چیزی اضافه نکن؛ تنظیمات لازم داخل پروژه ثابت‌اند |

### Volume خاصی می‌خواهد؟

نه؛ **یک Volume معمولی Railway** کافی است. اسم و اندازه‌ی خاصی لازم ندارد، اما
**Mount Path باید دقیقاً این باشد:**

```text
/etc/x-ui
```

این Volume فایل دیتابیس پنل را نگه می‌دارد؛ یعنی کاربران، رمز ادمین، اینباندها،
حجم و تاریخ انقضا بعد از Restart، Redeploy و آپدیت نسخه باقی می‌مانند.

> Volume را قبل از ساختن کاربران اضافه کن. بدون Volume پنل بالا می‌آید، ولی با
> دیپلوی بعدی اطلاعاتی که داخل کانتینر بوده از بین می‌رود.

---

# 📱 آموزش نصب کامل روی Railway

این مراحل برای نصب تازه است و با گوشی هم انجام می‌شود.

## مرحله ۱ — ساخت پروژه از GitHub

1. وارد [Railway](https://railway.com/new) شو.
2. **New Project** را بزن.
3. **Deploy from GitHub Repo** را انتخاب کن.
4. ریپوی زیر را انتخاب کن:

```text
hdzirxluci-hub/3x-ui.3.10.2
```

5. Branch باید `main` باشد.
6. **Deploy Now** را بزن و صبر کن اولین Build تمام شود.

Railway فایل `Dockerfile` را خودش تشخیص می‌دهد؛ Build Command یا Start Command
دستی وارد نکن.

> اگر ریپو را در لیست نمی‌بینی، دسترسی GitHub برنامه‌ی Railway را باز کن و همین
> ریپو را به Repository Access اضافه کن.

## مرحله ۲ — اضافه‌کردن Volume

1. سرویس **3x-ui** را باز کن.
2. برو به **Settings → Volumes**.
3. **Add Volume** را بزن. اگر این گزینه در Settings نبود، روی صفحه‌ی پروژه
   **Attach Volume** را بزن و سرویس 3x-ui را انتخاب کن.
4. در **Mount Path** دقیقاً بنویس:

```text
/etc/x-ui
```

5. ذخیره کن.

فقط همین یک Volume لازم است. این مسیرها اشتباه‌اند:

```text
/etc
/etc/x-ui/x-ui.db
/usr/local/x-ui
/data
```

## مرحله ۳ — ساخت دامنه عمومی

1. داخل همان سرویس برو به **Settings → Networking → Public Networking**.
2. **Generate Domain** را بزن.
3. اگر Railway پورت مقصد خواست، **`8080`** را انتخاب کن.
4. دامنه‌ای شبیه این می‌گیری:

```text
example-production.up.railway.app
```

5. حالا یک **Redeploy معمولی** بزن تا همان کد با Volume و دامنه‌ی جدید اجرا شود.
6. صبر کن وضعیت Deployment به **Active / Success** برسد.

> پورت‌های `2053`، `2096` و `10085` داخلی‌اند. آن‌ها را در Networking انتخاب
> نکن و آخر لینک عمومی ننویس. لینک Railway باید بدون پورت باشد.

## مرحله ۴ — بازکردن پنل

آدرس پنل:

```text
https://YOUR-DOMAIN.up.railway.app/panel/
```

ورود اولیه:

| فیلد | مقدار |
|---|---|
| Username | `admin` |
| Password | `admin` |

بعد از اولین ورود فوراً رمز را از داخل **Panel Settings → Authentication** عوض
کن. رمز جدید داخل Volume می‌ماند و Redeploy آن را به `admin` برنمی‌گرداند.

برای تست سلامت سرویس هم این آدرس باید `ok` نشان بدهد:

```text
https://YOUR-DOMAIN.up.railway.app/healthz
```

## مرحله ۵ — ساخت کاربر و گرفتن لینک ساب

1. پنل را باز کن و برو به **Clients**.
2. **Add Client** را بزن.
3. یک Email بدون فاصله بنویس؛ مثلاً `ali` یا `ali_reza`.
4. یک **Sub ID** بساز یا مقدار ساخته‌شده توسط پنل را نگه دار.
5. اینباند `PABLO` را تیک بزن و ذخیره کن.
6. لینک Subscription همان کاربر را از پنل کپی کن.

شکل صحیح لینک:

```text
https://YOUR-DOMAIN.up.railway.app/sub/SUB-ID
```

- در مرورگر: رابط کامل ساب **PasarGuard با برند PABLO** باز می‌شود.
- در v2rayNG، Streisand یا v2box: کانفیگ خام خود 3x-ui دریافت می‌شود.
- کانفیگ عمومی باید `:443` و `security=tls` داشته باشد.

شکل‌های زیر غلط‌اند و از اینترنت باز نمی‌شوند:

```text
http://YOUR-DOMAIN:2096/...
https://YOUR-DOMAIN:2096/...
vless://...@YOUR-DOMAIN:10085?...security=none
```

نسخه‌ی فعلی هنگام بازشدن پنل یا ساب، دامنه‌ی واقعی را تشخیص می‌دهد و فقط Host
خودکار PABLO را به HTTPS، پورت `443` و TLS منتقل می‌کند. به محض اینکه همان Host
را در بخش **Hosts / میزبان‌ها** ویرایش و Save کنی، دستی محسوب می‌شود و درخواست
بعدی، Restart یا Redeploy مقدارهای Address، Port، SNI، Host Header و Path را
برنمی‌گرداند. Hostهای اضافه و CDNهای دستی هم هیچ‌وقت بازنویسی نمی‌شوند.

---

## 🔄 فرق Redeploy با آپدیت نسخه

این دو گزینه یک کار نمی‌کنند:

| کار | گزینه درست |
|---|---|
| بعد از ساخت Domain یا وصل‌کردن Volume | **Redeploy** |
| Restart همان نسخه و همان کد | **Redeploy** |
| آوردن آخرین تغییرات GitHub | **Deploy Latest Commit** |
| نصب نسخه‌ی تازه‌ای که در ریپو قرار گرفته | **Deploy Latest Commit** |

**Redeploy همان کدی را که قبلاً Build شده دوباره اجرا می‌کند.** پس برای نصب
تازه، انتخاب ریپو در مرحله‌ی اول جدیدترین کد را می‌آورد؛ اما اگر بعداً GitHub
آپدیت شد و Auto Deploy اجرا نشد، داخل سرویس **Command Palette / Search** را باز
کن و **Deploy Latest Commit** را بزن.

### قبل از آپدیت نسخه

1. مطمئن شو Volume به `/etc/x-ui` وصل است.
2. اگر پلن Railway اجازه می‌دهد، از Volume یک Backup بگیر.
3. آخرین Commit را دیپلوی کن.
4. بعد از Active شدن، پنل، یک لینک ساب و یک کانفیگ آزمایشی را بررسی کن.

Volume باعث می‌شود کاربران و تنظیمات در آپدیت بمانند؛ ولی هیچ آپدیتی را بدون
Backup اطلاعات مهم انجام نده.

---

## 🔐 متغیرها

در بخش **Variables** هیچ متغیر برنامه‌ای اضافه نکن. برند، نام کانفیگ، مسیرها،
پورت‌های داخلی و متن صفحه‌ی ساب داخل کد ثابت‌اند.

Railway چند مقدار سیستمی Read-only را خودش نشان می‌دهد. این‌ها مال پلتفرم‌اند
و لازم نیست حذف یا ویرایش شوند. پروژه فقط از پورت و دامنه‌ی عمومی‌ای که Railway
خودکار به سرویس می‌دهد استفاده می‌کند؛ کاربر هیچ Variable دستی نمی‌سازد.

---

## ⚠️ عیب‌یابی سریع

| مشکل | راه‌حل |
|---|---|
| بعد از دیپلوی کاربران و رمز پاک شدند | Volume به مسیر دقیق `/etc/x-ui` وصل نیست |
| دامنه 404 یا Application Failed می‌دهد | Deployment را چک کن و Target Port دامنه را روی `8080` بگذار |
| پنل لینک `:2096` نشان می‌دهد | مطمئن شو آخرین Commit ریپو دیپلوی شده، نه یک Redeploy از کد قدیمی |
| کانفیگ `:10085` یا `security=none` دارد | آخرین Commit را دیپلوی و لینک ساب را یک بار باز کن |
| لینک ساب 404 است | Sub ID کاربر را دوباره از Clients کپی کن |
| Client ذخیره نمی‌شود | یک اینباند را تیک بزن؛ پنل بدون inbound ذخیره نمی‌کند |
| اسم کاربر قبول نمی‌شود | فاصله نگذار؛ از `ali_reza` استفاده کن |
| v2box خطا می‌دهد | آخرین Commit را دیپلوی کن و Subscription را داخل برنامه Update کن |
| صفحه‌ی ساب سفید است | Deployment باید Active باشد؛ سپس لینک `/sub/SUB-ID` را بدون پورت باز کن |

---

## 🎨 مشخصات PABLO

| مورد | مقدار |
|---|---|
| نسخه‌ی 3x-ui | `v3.10.2` |
| نام برند پنل | `PABLO` |
| نام کانفیگ‌ها | `𝗣𝗔𝗕𝗟𝗢 \| <نام کاربر>` |
| عنوان صفحه‌ی ساب | `𝗣𝗔𝗕𝗟𝗢` |
| اعلان | این پنل توسط Pablo ساخته شده |
| کانال | [t.me/pablo_artor](https://t.me/pablo_artor) |
| پشتیبانی | [t.me/poll_o0](https://t.me/poll_o0) |

---

## 🧩 معماری پروژه

Railway یک ورودی عمومی HTTP/HTTPS به سرویس می‌دهد. nginx درخواست‌ها را بر اساس
مسیر به سرویس داخلی درست می‌فرستد:

```text
Phone ──HTTPS──▶ Railway Edge ──HTTP──▶ nginx :$PORT
   ├─ /healthz            → سلامت سرویس
   ├─ /panel/…            → پنل 3x-ui روی :2053
   ├─ /sub و /sub/…       → صفحه و فید ساب :2097 → :2096
   ├─ /ws                 → VLESS + WebSocket روی :10085
   └─ /                   → 404
```

| فایل | وظیفه |
|---|---|
| `Dockerfile` | نصب باینری رسمی `3x-ui v3.10.2` و nginx |
| `entrypoint.sh` | آماده‌سازی دیتابیس، پنل، اینباند و آدرس عمومی |
| `nginx.conf.template` | تقسیم مسیرهای عمومی و اتصال WebSocket |
| `subserver.py` | رندر صفحه‌ی PasarGuard، فید خام اپ‌ها و همگام‌سازی امن دامنه |
| `public_endpoint.py` | حفظ مالکیت Host خودکار/دستی در دیتابیس پنل |
| `templates/subscription/` | رابط، لوگو، QR و تاریخ جلالی PABLO |
| `railway.json` | Healthcheck و سیاست Restart |

---

## 🔧 برای نگهدارنده: ساخت دکمه‌ی تک‌کلیک واقعی

دکمه‌ی بالای README فقط Railway را باز می‌کند. برای اینکه ریپو، Volume و تنظیمات
از قبل در یک Template باشند:

1. پروژه را یک بار کامل و سالم بالا بیاور.
2. Volume را روی `/etc/x-ui` و Domain را تنظیم کن.
3. در Railway از منوی سرویس **Generate Template** را بزن.
4. Template را Publish کن.
5. لینک Template را جایگزین `https://railway.com/new` در دکمه‌ی بالای README کن.

---

## 📚 راهنمای رسمی Railway

- [Deploy از GitHub](https://docs.railway.com/platform/migrate-from-replit#2-create-a-railway-project)
- [Volumes](https://docs.railway.com/overview/the-basics#volumes)
- [Public Domain](https://docs.railway.com/networking/domains/working-with-domains)
- [Deployment Actions](https://docs.railway.com/deployments/deployment-actions)

---

<div align="center">

### ⭐ اگر پروژه براتون مفید بود، بهش Star بدین

[![Star](https://img.shields.io/github/stars/hdzirxluci-hub/3x-ui.3.9.0?style=social&label=Star)](https://github.com/hdzirxluci-hub/3x-ui.3.9.0)

ساخته شده توسط **Pablo** — [t.me/pablo_artor](https://t.me/pablo_artor) · [پشتیبانی](https://t.me/poll_o0)

</div>
