# Telegram Bot Ultimate Controller v6.0

<p align="center">
  <b>نظام تحكم كامل في حسابات تلجرام متعددة عبر بوت واحد</b><br>
  <i>Complete Multi-Account Telegram Control System via Single Bot</i>
</p>

---

## المميزات | Features

- **بوت تحكم مركزي** — تحكم كامل عبر بوت @BotFather (لا حاجة لجروب أوامر)
- **حماية بكلمة مرور** — أول تشغيل يطلب إنشاء كلمة مرور، كل دخول لاحق يطلبها
- **إضافة حسابات تفاعلية** — خطوة بخطوة: رقم → API ID → API Hash → Group → OTP
- **إدارة الحسابات** — تشغيل، إيقاف، حذف، تغيير وضع، تغيير مجموعة
- **كلمات مفتاحية** — إضافة/حذف كلمات يراقبها البوت
- **ردود تلقائية** — إدارة الردود مع متغيرات {first_name} وغيرها
- **فلاتر ذكية** — تجاهل رسائل الإدمن، البوتات، الروابط، الأرقام، @منشن
- **انضمام تلقائي** — انضمام الحسابات للجروبات المخزنة مع تأخير عشوائي
- **Circuit Breaker** — عزل الحسابات عند تكرار الأخطاء
- **Rate Limiting** — حد أقصى للردود التلقائية لكل مستخدم
- **نسخ احتياطي** — تصدير واستيراد قاعدة البيانات كاملة
- **صحة النظام** — فحص دوري لقاعدة البيانات والحسابات النشطة
- **أداء فائق** — معالجة غير متزامنة، connection pooling، semaphores

---

## البنية المعمارية | Architecture

```
┌─────────────────────────────────────────────────────┐
│              @BotFather (Controller Bot)              │
│  /start → Password → Menu → All Commands            │
│  python-telegram-bot (async polling)                  │
└────────────────────────┬────────────────────────────┘
                         │ HTTP API (Bot Token)
┌────────────────────────▼────────────────────────────┐
│                 Railway Server                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐ │
│  │  Controller  │  │   Account    │  │  Health  │ │
│  │    Bot       │  │  Workers     │  │  Server  │ │
│  │  (main.py)   │  │ (Telethon)   │  │ (8080)   │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────┘ │
│         │                  │                        │
│  ┌──────▼──────────────────▼──────┐                 │
│  │      PostgreSQL Database       │                 │
│  │   (accounts, settings, logs)   │                 │
│  └────────────────────────────────┘                 │
└─────────────────────────────────────────────────────┘
```

---

## النشر على Railway | Railway Deployment

### الخطوة 1: إنشاء مشروع
1. سجل دخول في [Railway](https://railway.app)
2. أنشئ مشروع جديد
3. أضف PostgreSQL: `New → Database → Add PostgreSQL`

### الخطوة 2: إنشاء البوت
1. افتح [@BotFather](https://t.me/BotFather) في Telegram
2. أرسل `/newbot` واتبع التعليمات
3. احفظ **التوكن** (مثال: `123456789:ABCdef...`)

### الخطوة 3: رفع الكود
**طريقة A: GitHub**
```bash
git init
git add .
git commit -m "Initial commit"
# أنشئ repo في GitHub وارفع
```
ثم في Railway: `New → GitHub Repo → اختر المستودع`

**طريقة B: CLI**
```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

### الخطوة 4: إعداد المتغيرات
في Railway: `Variables → New Variable`

| Variable | Value | Source |
|----------|-------|--------|
| `DATABASE_URL` | `postgresql://...` | تُنشأ تلقائياً مع PostgreSQL |
| `BOT_TOKEN` | `123456789:ABC...` | من @BotFather |

> ملاحظة: Railway يضيف `DATABASE_URL` تلقائياً. إذا بدأ بـ `postgres://`، البوت يحوله تلقائياً.

### الخطوة 5: التشغيل
1. اضغط `Deploy` في Railway
2. تأكد من الـ Build ينجح
3. افتح البوت في Telegram وارسل `/start`
4. أدخل كلمة مرور (أول استخدام)
5. ابدأ بإضافة الحسابات!

---

## التشغيل المحلي | Local Development

```bash
# 1. Clone
git clone <repo-url>
cd telegram-bot-ultimate

# 2. Virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# أو: venv\Scripts\activate  # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. PostgreSQL (Docker)
docker run -d \
  --name tg-bot-db \
  -e POSTGRES_DB=telegram_bot \
  -e POSTGRES_USER=admin \
  -e POSTGRES_PASSWORD=secret123 \
  -p 5432:5432 \
  postgres:15

# 5. Environment
cp .env.example .env
# عدل .env:
# DATABASE_URL=postgresql://admin:secret123@localhost:5432/telegram_bot
# BOT_TOKEN=your_token_from_botfather

# 6. Run
python main.py
```

---

## أوامر البوت | Bot Commands

### 🔐 المصادقة
| Command | Description |
|---------|-------------|
| `/start` | بدء البوت — إعداد كلمة المرور (أول استخدام) أو الدخول |

### 📱 إدارة الحسابات
| Command | Description |
|---------|-------------|
| `/addaccount` | إضافة حساب جديد (تفاعلي — 5 خطوات) |
| `/accounts` | عرض جميع الحسابات مع حالتها |
| `/startacc <phone>` | تشغيل حساب متوقف |
| `/stopacc <phone>` | إيقاف حساب نشط |
| `/removeacc <phone>` | حذف حساب نهائياً من DB |
| `/setgroup <phone> <group_id>` | تغيير مجموعة الهدف |
| `/setmode <phone> <forward/reply/both>` | تغيير وضع الحساب |

### 🔗 إدارة الجروبات
| Command | Description |
|---------|-------------|
| `/groups` | عرض الجروبات المخزنة |
| `/addgroup <link>` | إضافة جروب |
| `/delgroup <link>` | حذف جروب |
| `/joingroups <phone> [start]` | انضمام حساب لكل الجروبات |
| `/stopjoin [all/phone]` | إيقاف الانضمام |
| `/usergroups <phone>` | حالة انضمام الحساب |

### 🔑 الكلمات المفتاحية
| Command | Description |
|---------|-------------|
| `/keywords` | عرض الكلمات المفتاحية |
| `/addkw <word> [category]` | إضافة كلمة |
| `/delkw <word>` | حذف كلمة |

### 💬 الردود التلقائية
| Command | Description |
|---------|-------------|
| `/replies` | عرض الردود المخزنة |
| `/addreply <text>` | إضافة رد |
| `/delreply <text>` | حذف رد |
| `/defaultreply [text]` | عرض/تعديل الرد الافتراضي |

### 🛡 الفلاتر
| Command | Description |
|---------|-------------|
| `/filters` | عرض حالة الفلاتر |
| `/togglefilter <name> on/off` | تفعيل/تعطيل فلتر |

### 🚫 المستخدمون المحظورون
| Command | Description |
|---------|-------------|
| `/blocked` | عرض المحظورين |
| `/block <user_id>` أو رد | حظر مستخدم |
| `/unblock <user_id>` | فك الحظر |

### ⚙️ النظام
| Command | Description |
|---------|-------------|
| `/stats` | إحصائيات كاملة |
| `/health` | فحص صحة النظام |
| `/config` | عرض الإعدادات |
| `/setconfig <key> <value>` | تعديل إعداد |
| `/backup` | نسخة احتياطية gzip |
| `/restart` | إعادة تشغيل الحسابات |
| `/unblockspam` | فك حظر @SpamBot |
| `/menu` | القائمة الرئيسية |
| `/help` | دليل الأوامر |

---

## الهيكل المعماري للملفات | File Structure

```
telegram-bot-ultimate/
├── main.py              # Entry point — initializes DB, state, workers, bot
├── controller.py        # python-telegram-bot handlers & conversations
├── worker.py            # Telethon AccountWorker — monitors groups, auto-replies
├── database.py          # Async PostgreSQL layer with migrations
├── models.py            # Enums & dataclasses (AccountInfo, Status, etc.)
├── utils.py             # Text utils, cache, circuit breaker, messenger, formatter
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container image
├── railway.toml         # Railway deployment config
├── .env.example         # Environment variables template
└── README.md            # This file
```

---

## الأداء | Performance

- **Connection Pooling**: PostgreSQL pool (5-30 connections)
- **Async Semaphores**: 50 concurrent message dispatch
- **TTL Cache**: O(1) deduplication for forwards/replies
- **Circuit Breaker**: Auto-recovery after failures
- **Non-blocking I/O**: All Telegram API calls are async
- **Health Endpoint**: `/health` for Railway monitoring

---

## المتغيرات البيئية | Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `BOT_TOKEN` | ✅ | Token from @BotFather |
| `OWNER_ID` | ❌ | Auto-set on first `/start` |
| `PORT` | ❌ | Health server port (default: 8080) |

---

## Troubleshooting

### البوت لا يستجيب
- تأكد من `BOT_TOKEN` صحيح
- تحقق من logs في Railway Dashboard
- تأكد من الـ health endpoint يعمل: `curl https://your-app.up.railway.app/health`

### فشل الاتصال بقاعدة البيانات
- تأكد من إضافة PostgreSQL addon في Railway
- تحقق من `DATABASE_URL` يظهر في Variables

### OTP لا يصل
- تأكد من صحة الرقم مع كود الدولة (+966...)
- تأكد من صحة API ID و API Hash
- بعض الدول تتطلب استخدام VPN/Proxy

### حسابات تتوقف
- استخدم `/health` لفحص الحالة
- استخدم `/restart` لإعادة التشغيل
- تحقق من `/accounts` لمعرفة أخطاء الحسابات

---

## License

MIT License — استخدم كما تشاء.

---

<p align="center">
  <b>صنع بـ ❤️ للتحكم الكامل في تلجرام</b>
</p>
