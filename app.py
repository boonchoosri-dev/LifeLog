"""
Daily Life Logger — จดทุกอย่างในชีวิตประจำวัน (Single-file Flask app)

บันทึก: ค่าใช้จ่าย, เมนูอาหาร, การดื่มน้ำ, การออกกำลังกาย
Sync: ทุกกิจกรรมที่ใส่ "ค่าใช้จ่าย" จะถูกสร้าง/แก้ไข/ลบ รายการในหน้า "ค่าใช้จ่าย" อัตโนมัติ
      และถ้าแก้ไข/ลบรายการค่าใช้จ่ายที่ผูกกับกิจกรรม ค่าใช้จ่ายของกิจกรรมนั้นจะอัปเดตตามด้วย (2 ทาง)

วิธีรัน:
    pip install flask
    python app.py
    เปิด http://127.0.0.1:5000
"""
import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from jinja2 import DictLoader

DB_PATH = os.environ.get("DAILY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily.db"))
WATER_GOAL_ML = int(os.environ.get("WATER_GOAL_ML", 2000))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# ---------------------------------------------------------------------------
# Config ของแต่ละกิจกรรม (ใช้สร้างตาราง ฟอร์ม และรายการแบบอัตโนมัติ)
# field: (name, label, type, options/placeholder)
# ---------------------------------------------------------------------------
EXPENSE_CATEGORIES = ["อาหาร", "เครื่องดื่ม", "ออกกำลังกาย", "เดินทาง", "ช้อปปิ้ง", "บิล/ค่าบริการ", "สุขภาพ", "บันเทิง", "อื่นๆ"]

ACTIVITIES = {
    "meals": {
        "label": "เมนูอาหาร",
        "icon": "🍽️",
        "color": "orange",
        "category": "อาหาร",
        "fields": [
            ("meal_type", "มื้อ", "select", ["เช้า", "กลางวัน", "เย็น", "ของว่าง"]),
            ("menu", "เมนู", "text", "เช่น ข้าวผัดกะเพราไก่"),
            ("calories", "แคลอรี่ (kcal)", "number", "0"),
        ],
        "title": lambda r: f"มื้อ{r['meal_type']}: {r['menu']}",
    },
    "water": {
        "label": "ดื่มน้ำ",
        "icon": "💧",
        "color": "sky",
        "category": "เครื่องดื่ม",
        "fields": [
            ("drink", "เครื่องดื่ม", "text", "น้ำเปล่า"),
            ("ml", "ปริมาณ (ml)", "number", "250"),
        ],
        "title": lambda r: f"{r['drink'] or 'น้ำ'} {int(r['ml'] or 0)} ml",
    },
    "exercises": {
        "label": "ออกกำลังกาย",
        "icon": "🏃",
        "color": "emerald",
        "category": "ออกกำลังกาย",
        "fields": [
            ("activity", "ประเภท", "text", "เช่น วิ่ง, ฟิตเนส, ว่ายน้ำ"),
            ("minutes", "ระยะเวลา (นาที)", "number", "30"),
            ("calories", "เผาผลาญ (kcal)", "number", "0"),
        ],
        "title": lambda r: f"{r['activity']} {int(r['minutes'] or 0)} นาที",
    },
}
NUMERIC_FIELDS = {"calories", "ml", "minutes"}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT,
            amount REAL NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'อื่นๆ',
            note TEXT,
            source_type TEXT,
            source_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_type, source_id)
        );
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, time TEXT,
            meal_type TEXT, menu TEXT, calories REAL DEFAULT 0,
            cost REAL DEFAULT 0, note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS water (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, time TEXT,
            drink TEXT, ml REAL DEFAULT 0,
            cost REAL DEFAULT 0, note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, time TEXT,
            activity TEXT, minutes REAL DEFAULT 0, calories REAL DEFAULT 0,
            cost REAL DEFAULT 0, note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_exp_date ON expenses(date);
        CREATE INDEX IF NOT EXISTS idx_meal_date ON meals(date);
        CREATE INDEX IF NOT EXISTS idx_water_date ON water(date);
        CREATE INDEX IF NOT EXISTS idx_ex_date ON exercises(date);
        """
    )
    db.commit()
    db.close()


def to_float(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def valid_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


# ---------------------------------------------------------------------------
# Sync: กิจกรรม <-> ค่าใช้จ่าย
# ---------------------------------------------------------------------------
def sync_activity_expense(db, kind, row_id):
    """สร้าง/อัปเดต/ลบ expense ที่ผูกกับกิจกรรม ตามค่า cost ของกิจกรรม"""
    cfg = ACTIVITIES[kind]
    row = db.execute(f"SELECT * FROM {kind} WHERE id=?", (row_id,)).fetchone()
    if row is None or to_float(row["cost"]) <= 0:
        db.execute("DELETE FROM expenses WHERE source_type=? AND source_id=?", (kind, row_id))
        return
    note = f"{cfg['icon']} {cfg['title'](row)}"
    existing = db.execute(
        "SELECT id FROM expenses WHERE source_type=? AND source_id=?", (kind, row_id)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE expenses SET date=?, time=?, amount=?, note=? WHERE id=?",
            (row["date"], row["time"], row["cost"], note, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO expenses (date, time, amount, category, note, source_type, source_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (row["date"], row["time"], row["cost"], cfg["category"], note, kind, row_id),
        )


def sync_all(db):
    """Sync ทั้งหมด: สร้างรายการที่หาย, ลบรายการกำพร้า"""
    count = 0
    for kind in ACTIVITIES:
        for r in db.execute(f"SELECT id FROM {kind}").fetchall():
            sync_activity_expense(db, kind, r["id"])
            count += 1
        db.execute(
            f"DELETE FROM expenses WHERE source_type=? AND source_id NOT IN (SELECT id FROM {kind})",
            (kind,),
        )
    db.commit()
    return count


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def day_summary(db, d):
    exp = db.execute("SELECT COALESCE(SUM(amount),0) s, COUNT(*) c FROM expenses WHERE date=?", (d,)).fetchone()
    by_cat = db.execute(
        "SELECT category, SUM(amount) s FROM expenses WHERE date=? GROUP BY category ORDER BY s DESC", (d,)
    ).fetchall()
    meals = db.execute("SELECT * FROM meals WHERE date=? ORDER BY time, id", (d,)).fetchall()
    water = db.execute("SELECT * FROM water WHERE date=? ORDER BY time, id", (d,)).fetchall()
    exercises = db.execute("SELECT * FROM exercises WHERE date=? ORDER BY time, id", (d,)).fetchall()
    expenses = db.execute("SELECT * FROM expenses WHERE date=? ORDER BY time, id", (d,)).fetchall()
    cal_in = sum(to_float(m["calories"]) for m in meals)
    cal_out = sum(to_float(e["calories"]) for e in exercises)
    water_ml = sum(to_float(w["ml"]) for w in water)
    return {
        "expense_total": exp["s"],
        "expense_count": exp["c"],
        "by_cat": by_cat,
        "meals": meals,
        "water": water,
        "exercises": exercises,
        "expenses": expenses,
        "cal_in": cal_in,
        "cal_out": cal_out,
        "water_ml": water_ml,
        "water_pct": min(100, int(water_ml / WATER_GOAL_ML * 100)) if WATER_GOAL_ML else 0,
        "ex_minutes": sum(to_float(e["minutes"]) for e in exercises),
    }


def week_stats(db, end_d):
    end = datetime.strptime(end_d, "%Y-%m-%d").date()
    days = []
    for i in range(6, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        days.append({
            "date": d,
            "label": (end - timedelta(days=i)).strftime("%d/%m"),
            "expense": db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=?", (d,)).fetchone()[0],
            "water": db.execute("SELECT COALESCE(SUM(ml),0) FROM water WHERE date=?", (d,)).fetchone()[0],
            "minutes": db.execute("SELECT COALESCE(SUM(minutes),0) FROM exercises WHERE date=?", (d,)).fetchone()[0],
            "cal": db.execute("SELECT COALESCE(SUM(calories),0) FROM meals WHERE date=?", (d,)).fetchone()[0],
        })
    mx = {k: max([x[k] for x in days] + [1]) for k in ("expense", "water", "minutes", "cal")}
    return days, mx


def form_values(kind):
    """อ่านค่าจากฟอร์มตาม config"""
    vals = {
        "date": valid_date(request.form.get("date")),
        "time": (request.form.get("time") or datetime.now().strftime("%H:%M"))[:5],
        "note": request.form.get("note", "").strip(),
    }
    if kind == "expenses":
        vals["amount"] = max(0.0, to_float(request.form.get("amount")))
        cat = request.form.get("category", "อื่นๆ")
        vals["category"] = cat if cat in EXPENSE_CATEGORIES else "อื่นๆ"
    else:
        vals["cost"] = max(0.0, to_float(request.form.get("cost")))
        for name, _label, ftype, opts in ACTIVITIES[kind]["fields"]:
            raw = request.form.get(name, "")
            if name in NUMERIC_FIELDS:
                vals[name] = max(0.0, to_float(raw))
            elif ftype == "select":
                vals[name] = raw if raw in opts else opts[0]
            else:
                vals[name] = raw.strip()
    return vals


def check_kind(kind):
    if kind != "expenses" and kind not in ACTIVITIES:
        abort(404)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    d = valid_date(request.args.get("date"))
    db = get_db()
    s = day_summary(db, d)
    days, mx = week_stats(db, d)
    prev_d = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    next_d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
    return render_template("dashboard.html", d=d, s=s, days=days, mx=mx, prev_d=prev_d, next_d=next_d)


@app.route("/<kind>")
def list_kind(kind):
    check_kind(kind)
    db = get_db()
    d = request.args.get("date") or ""
    q = f"SELECT * FROM {kind}"
    params = ()
    if d:
        d = valid_date(d)
        q += " WHERE date=?"
        params = (d,)
    rows = db.execute(q + " ORDER BY date DESC, time DESC, id DESC LIMIT 300", params).fetchall()
    total = None
    if kind == "expenses":
        total = sum(to_float(r["amount"]) for r in rows)
    return render_template("list.html", kind=kind, rows=rows, d=d, total=total, edit=None)


@app.route("/<kind>/add", methods=["POST"])
def add_kind(kind):
    check_kind(kind)
    db = get_db()
    vals = form_values(kind)
    cols = ", ".join(vals.keys())
    qs = ", ".join("?" for _ in vals)
    cur = db.execute(f"INSERT INTO {kind} ({cols}) VALUES ({qs})", tuple(vals.values()))
    if kind in ACTIVITIES:
        sync_activity_expense(db, kind, cur.lastrowid)
    db.commit()
    label = "ค่าใช้จ่าย" if kind == "expenses" else ACTIVITIES[kind]["label"]
    extra = " (sync ค่าใช้จ่ายแล้ว 💸)" if kind in ACTIVITIES and vals["cost"] > 0 else ""
    flash(f"บันทึก{label}เรียบร้อย{extra}", "success")
    return redirect(request.form.get("next") or url_for("list_kind", kind=kind))


@app.route("/<kind>/<int:row_id>/edit", methods=["GET", "POST"])
def edit_kind(kind, row_id):
    check_kind(kind)
    db = get_db()
    row = db.execute(f"SELECT * FROM {kind} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        abort(404)
    if request.method == "POST":
        vals = form_values(kind)
        if kind == "expenses" and row["source_type"] in ACTIVITIES:
            # แก้ expense ที่ผูกกับกิจกรรม -> อัปเดต cost/date/time กลับไปที่กิจกรรม (sync 2 ทาง)
            vals["category"] = row["category"]
            db.execute(
                f"UPDATE {row['source_type']} SET cost=?, date=?, time=? WHERE id=?",
                (vals["amount"], vals["date"], vals["time"], row["source_id"]),
            )
            sync_activity_expense(db, row["source_type"], row["source_id"])
            if vals["note"]:
                db.execute("UPDATE expenses SET note=? WHERE id=?", (vals["note"], row_id))
        else:
            sets = ", ".join(f"{k}=?" for k in vals)
            db.execute(f"UPDATE {kind} SET {sets} WHERE id=?", (*vals.values(), row_id))
            if kind in ACTIVITIES:
                sync_activity_expense(db, kind, row_id)
        db.commit()
        flash("แก้ไขเรียบร้อย", "success")
        return redirect(request.form.get("next") or url_for("list_kind", kind=kind))
    rows = db.execute(f"SELECT * FROM {kind} ORDER BY date DESC, time DESC, id DESC LIMIT 300").fetchall()
    return render_template("list.html", kind=kind, rows=rows, d="", total=None, edit=row)


@app.route("/<kind>/<int:row_id>/delete", methods=["POST"])
def delete_kind(kind, row_id):
    check_kind(kind)
    db = get_db()
    row = db.execute(f"SELECT * FROM {kind} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        abort(404)
    if kind == "expenses":
        if row["source_type"] in ACTIVITIES:
            # ลบ expense ที่ผูกกับกิจกรรม -> ตั้ง cost ของกิจกรรมเป็น 0
            db.execute(f"UPDATE {row['source_type']} SET cost=0 WHERE id=?", (row["source_id"],))
        db.execute("DELETE FROM expenses WHERE id=?", (row_id,))
    else:
        db.execute(f"DELETE FROM {kind} WHERE id=?", (row_id,))
        sync_activity_expense(db, kind, row_id)  # ลบ expense ที่ผูกอยู่
    db.commit()
    flash("ลบรายการแล้ว", "info")
    return redirect(request.form.get("next") or url_for("list_kind", kind=kind))


@app.route("/sync", methods=["POST"])
def sync_route():
    n = sync_all(get_db())
    flash(f"Sync ค่าใช้จ่ายกับทุกกิจกรรมเรียบร้อย ({n} รายการ)", "success")
    return redirect(request.form.get("next") or url_for("dashboard"))


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------
@app.context_processor
def inject():
    return {
        "ACTIVITIES": ACTIVITIES,
        "EXPENSE_CATEGORIES": EXPENSE_CATEGORIES,
        "WATER_GOAL_ML": WATER_GOAL_ML,
        "today": date.today().isoformat(),
        "now_time": datetime.now().strftime("%H:%M"),
    }


@app.template_filter("money")
def money(v):
    return f"{to_float(v):,.2f}"


@app.template_filter("num")
def num(v):
    f = to_float(v)
    return f"{int(f):,}" if f == int(f) else f"{f:,.1f}"


@app.template_filter("thdate")
def thdate(s):
    months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return f"วัน{days[dt.weekday()]}ที่ {dt.day} {months[dt.month - 1]} {dt.year + 543}"
    except (TypeError, ValueError):
        return s


# ---------------------------------------------------------------------------
# Templates (Tailwind CDN)
# ---------------------------------------------------------------------------
BASE = r"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Daily Log{% endblock %} · จดทุกอย่างในชีวิตประจำวัน</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script>
    tailwind.config = { theme: { extend: { fontFamily: { sans: ['"Noto Sans Thai"', 'ui-sans-serif', 'system-ui'] } } } }
  </script>
</head>
<body class="bg-slate-50 text-slate-800 font-sans min-h-screen">
  <header class="bg-white/80 backdrop-blur border-b border-slate-200 sticky top-0 z-20">
    <div class="max-w-6xl mx-auto px-4 py-3 flex flex-wrap items-center gap-3 justify-between">
      <a href="{{ url_for('dashboard') }}" class="flex items-center gap-2 font-bold text-lg">
        <span class="text-2xl">📒</span> <span>Daily Log</span>
      </a>
      <nav class="flex flex-wrap gap-1 text-sm">
        {% set k = request.view_args.get('kind') if request.view_args else None %}
        <a href="{{ url_for('dashboard') }}" class="px-3 py-1.5 rounded-lg {{ 'bg-indigo-600 text-white' if request.endpoint=='dashboard' else 'hover:bg-slate-100' }}">🏠 ภาพรวม</a>
        <a href="{{ url_for('list_kind', kind='expenses') }}" class="px-3 py-1.5 rounded-lg {{ 'bg-indigo-600 text-white' if k=='expenses' else 'hover:bg-slate-100' }}">💸 ค่าใช้จ่าย</a>
        {% for key, a in ACTIVITIES.items() %}
        <a href="{{ url_for('list_kind', kind=key) }}" class="px-3 py-1.5 rounded-lg {{ 'bg-indigo-600 text-white' if k==key else 'hover:bg-slate-100' }}">{{ a.icon }} {{ a.label }}</a>
        {% endfor %}
      </nav>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-6">
    {% with msgs = get_flashed_messages(with_categories=true) %}
      {% for cat, m in msgs %}
        <div class="mb-4 rounded-xl px-4 py-3 text-sm {{ 'bg-emerald-50 text-emerald-800 border border-emerald-200' if cat=='success' else 'bg-sky-50 text-sky-800 border border-sky-200' }}">{{ m }}</div>
      {% endfor %}
    {% endwith %}
    {% block content %}{% endblock %}
  </main>

  <footer class="text-center text-xs text-slate-400 py-8">Daily Log · Flask + Tailwind · ข้อมูลเก็บใน SQLite บนเครื่องของคุณ</footer>
</body>
</html>"""

MACROS = r"""
{% macro input_cls() %}w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400{% endmacro %}

{% macro activity_form(kind, row=None, next=None, compact=False) %}
  {% set a = ACTIVITIES[kind] %}
  <form method="post" action="{{ url_for('edit_kind', kind=kind, row_id=row.id) if row else url_for('add_kind', kind=kind) }}" class="grid gap-3 {{ 'grid-cols-2' if compact else 'sm:grid-cols-2' }}">
    {% if next %}<input type="hidden" name="next" value="{{ next }}">{% endif %}
    {% for name, label, ftype, opt in a.fields %}
      <label class="text-sm {{ 'col-span-2' if name in ('menu','activity') else '' }}">
        <span class="block mb-1 text-slate-600">{{ label }}</span>
        {% if ftype == 'select' %}
          <select name="{{ name }}" class="{{ input_cls() }}">
            {% for o in opt %}<option {{ 'selected' if row and row[name]==o }}>{{ o }}</option>{% endfor %}
          </select>
        {% else %}
          <input name="{{ name }}" type="{{ 'number' if ftype=='number' else 'text' }}" {% if ftype=='number' %}step="any" min="0"{% endif %}
                 placeholder="{{ opt }}" value="{{ (row[name]|num if ftype=='number' else row[name]) if row and row[name] is not none else '' }}"
                 {{ 'required' if name in ('menu','activity','ml') }} class="{{ input_cls() }}">
        {% endif %}
      </label>
    {% endfor %}
    <label class="text-sm">
      <span class="block mb-1 text-slate-600">💸 ค่าใช้จ่าย (บาท) <span class="text-xs text-indigo-500">sync อัตโนมัติ</span></span>
      <input name="cost" type="number" step="any" min="0" placeholder="0" value="{{ row.cost|num if row and row.cost else '' }}" class="{{ input_cls() }}">
    </label>
    <div class="grid grid-cols-2 gap-2 text-sm">
      <label><span class="block mb-1 text-slate-600">วันที่</span>
        <input name="date" type="date" value="{{ row.date if row else (request.args.get('date') or today) }}" class="{{ input_cls() }}"></label>
      <label><span class="block mb-1 text-slate-600">เวลา</span>
        <input name="time" type="time" value="{{ row.time if row else now_time }}" class="{{ input_cls() }}"></label>
    </div>
    <label class="text-sm col-span-full">
      <span class="block mb-1 text-slate-600">โน้ต</span>
      <input name="note" type="text" value="{{ row.note or '' if row else '' }}" class="{{ input_cls() }}">
    </label>
    <div class="col-span-full flex gap-2">
      <button class="rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 text-sm font-medium">{{ 'บันทึกการแก้ไข' if row else '+ เพิ่ม' + a.label }}</button>
      {% if row %}<a href="{{ url_for('list_kind', kind=kind) }}" class="rounded-lg border px-4 py-2 text-sm">ยกเลิก</a>{% endif %}
    </div>
  </form>
{% endmacro %}

{% macro expense_form(row=None, next=None) %}
  {% set linked = row and row.source_type %}
  <form method="post" action="{{ url_for('edit_kind', kind='expenses', row_id=row.id) if row else url_for('add_kind', kind='expenses') }}" class="grid gap-3 sm:grid-cols-2">
    {% if next %}<input type="hidden" name="next" value="{{ next }}">{% endif %}
    <label class="text-sm"><span class="block mb-1 text-slate-600">จำนวนเงิน (บาท)</span>
      <input name="amount" type="number" step="any" min="0" required value="{{ row.amount|num if row else '' }}" class="{{ input_cls() }}"></label>
    <label class="text-sm"><span class="block mb-1 text-slate-600">หมวดหมู่</span>
      <select name="category" {{ 'disabled' if linked }} class="{{ input_cls() }} disabled:bg-slate-100">
        {% for c in EXPENSE_CATEGORIES %}<option {{ 'selected' if row and row.category==c }}>{{ c }}</option>{% endfor %}
      </select></label>
    <label class="text-sm"><span class="block mb-1 text-slate-600">วันที่</span>
      <input name="date" type="date" value="{{ row.date if row else (request.args.get('date') or today) }}" class="{{ input_cls() }}"></label>
    <label class="text-sm"><span class="block mb-1 text-slate-600">เวลา</span>
      <input name="time" type="time" value="{{ row.time if row else now_time }}" class="{{ input_cls() }}"></label>
    <label class="text-sm col-span-full"><span class="block mb-1 text-slate-600">รายละเอียด</span>
      <input name="note" type="text" placeholder="เช่น ค่ารถไฟฟ้า" value="{{ row.note or '' if row else '' }}" class="{{ input_cls() }}"></label>
    {% if linked %}<p class="col-span-full text-xs text-indigo-600">🔗 รายการนี้ผูกกับ{{ ACTIVITIES[row.source_type].label }} — แก้จำนวนเงิน/วันที่ที่นี่ จะอัปเดตที่กิจกรรมด้วย</p>{% endif %}
    <div class="col-span-full flex gap-2">
      <button class="rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 text-sm font-medium">{{ 'บันทึกการแก้ไข' if row else '+ เพิ่มค่าใช้จ่าย' }}</button>
      {% if row %}<a href="{{ url_for('list_kind', kind='expenses') }}" class="rounded-lg border px-4 py-2 text-sm">ยกเลิก</a>{% endif %}
    </div>
  </form>
{% endmacro %}

{% macro row_actions(kind, r, next=None) %}
  <div class="flex items-center gap-1 shrink-0">
    <a href="{{ url_for('edit_kind', kind=kind, row_id=r.id) }}" class="text-xs px-2 py-1 rounded hover:bg-slate-100 text-slate-500">แก้ไข</a>
    <form method="post" action="{{ url_for('delete_kind', kind=kind, row_id=r.id) }}" onsubmit="return confirm('ลบรายการนี้?')">
      {% if next %}<input type="hidden" name="next" value="{{ next }}">{% endif %}
      <button class="text-xs px-2 py-1 rounded hover:bg-rose-50 text-rose-500">ลบ</button>
    </form>
  </div>
{% endmacro %}

{% macro activity_line(kind, r) %}
  {% if kind == 'meals' %}
    <span class="font-medium">{{ r.menu }}</span> <span class="text-xs text-slate-500">มื้อ{{ r.meal_type }} · {{ r.calories|num }} kcal</span>
  {% elif kind == 'water' %}
    <span class="font-medium">{{ r.drink or 'น้ำ' }}</span> <span class="text-xs text-slate-500">{{ r.ml|num }} ml</span>
  {% elif kind == 'exercises' %}
    <span class="font-medium">{{ r.activity }}</span> <span class="text-xs text-slate-500">{{ r.minutes|num }} นาที · 🔥 {{ r.calories|num }} kcal</span>
  {% endif %}
{% endmacro %}
"""

DASHBOARD = r"""{% extends 'base.html' %}
{% from 'macros.html' import activity_form, expense_form, row_actions, activity_line with context %}
{% block title %}ภาพรวม{% endblock %}
{% block content %}
{% set here = url_for('dashboard', date=d) %}
<div class="flex flex-wrap items-center justify-between gap-3 mb-6">
  <div>
    <h1 class="text-2xl font-bold">{{ d|thdate }}</h1>
    <p class="text-sm text-slate-500">สรุปกิจกรรมและค่าใช้จ่ายของวัน</p>
  </div>
  <div class="flex items-center gap-2">
    <a href="{{ url_for('dashboard', date=prev_d) }}" class="rounded-lg border bg-white px-3 py-2 text-sm hover:bg-slate-50">←</a>
    <form method="get"><input type="date" name="date" value="{{ d }}" onchange="this.form.submit()" class="rounded-lg border px-3 py-2 text-sm bg-white"></form>
    <a href="{{ url_for('dashboard', date=next_d) }}" class="rounded-lg border bg-white px-3 py-2 text-sm hover:bg-slate-50">→</a>
    {% if d != today %}<a href="{{ url_for('dashboard') }}" class="rounded-lg bg-slate-800 text-white px-3 py-2 text-sm">วันนี้</a>{% endif %}
    <form method="post" action="{{ url_for('sync_route') }}"><input type="hidden" name="next" value="{{ here }}">
      <button class="rounded-lg bg-indigo-50 text-indigo-700 border border-indigo-200 px-3 py-2 text-sm hover:bg-indigo-100" title="Sync ค่าใช้จ่ายกับทุกกิจกรรม">🔄 Sync</button></form>
  </div>
</div>

<!-- Stat cards -->
<div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
  <div class="rounded-2xl bg-white border p-4">
    <div class="text-sm text-slate-500">💸 ค่าใช้จ่าย</div>
    <div class="text-2xl font-bold mt-1">฿{{ s.expense_total|money }}</div>
    <div class="text-xs text-slate-400">{{ s.expense_count }} รายการ</div>
  </div>
  <div class="rounded-2xl bg-white border p-4">
    <div class="text-sm text-slate-500">🍽️ แคลอรี่ที่กิน</div>
    <div class="text-2xl font-bold mt-1">{{ s.cal_in|num }} <span class="text-sm font-normal">kcal</span></div>
    <div class="text-xs text-slate-400">สุทธิ {{ (s.cal_in - s.cal_out)|num }} kcal (หักออกกำลังกาย)</div>
  </div>
  <div class="rounded-2xl bg-white border p-4">
    <div class="text-sm text-slate-500">💧 ดื่มน้ำ</div>
    <div class="text-2xl font-bold mt-1">{{ s.water_ml|num }} <span class="text-sm font-normal">/ {{ WATER_GOAL_ML|num }} ml</span></div>
    <div class="h-2 bg-sky-100 rounded-full mt-2 overflow-hidden"><div class="h-full bg-sky-500 rounded-full" style="width: {{ s.water_pct }}%"></div></div>
  </div>
  <div class="rounded-2xl bg-white border p-4">
    <div class="text-sm text-slate-500">🏃 ออกกำลังกาย</div>
    <div class="text-2xl font-bold mt-1">{{ s.ex_minutes|num }} <span class="text-sm font-normal">นาที</span></div>
    <div class="text-xs text-slate-400">เผาผลาญ {{ s.cal_out|num }} kcal</div>
  </div>
</div>

<!-- Quick water -->
<div class="rounded-2xl bg-white border p-4 mb-6 flex flex-wrap items-center gap-2">
  <span class="text-sm font-medium mr-2">💧 ดื่มน้ำด่วน:</span>
  {% for ml in [150, 250, 350, 500, 750] %}
  <form method="post" action="{{ url_for('add_kind', kind='water') }}">
    <input type="hidden" name="next" value="{{ here }}"><input type="hidden" name="date" value="{{ d }}">
    <input type="hidden" name="drink" value="น้ำเปล่า"><input type="hidden" name="ml" value="{{ ml }}">
    <button class="rounded-full bg-sky-50 hover:bg-sky-100 text-sky-700 border border-sky-200 px-3 py-1.5 text-sm">+{{ ml }} ml</button>
  </form>
  {% endfor %}
</div>

<div class="grid lg:grid-cols-3 gap-6">
  <!-- Timeline of the day -->
  <div class="lg:col-span-2 space-y-6">
    {% for kind, a in ACTIVITIES.items() %}
    {% set items = s[kind] %}
    <section class="rounded-2xl bg-white border">
      <div class="flex items-center justify-between px-4 py-3 border-b">
        <h2 class="font-semibold">{{ a.icon }} {{ a.label }} <span class="text-xs text-slate-400 font-normal">({{ items|length }})</span></h2>
        <details class="relative">
          <summary class="list-none cursor-pointer text-sm text-indigo-600 hover:underline">+ เพิ่ม</summary>
          <div class="absolute right-0 mt-2 w-[22rem] max-w-[90vw] bg-white border rounded-xl shadow-lg p-4 z-10">
            {{ activity_form(kind, next=here, compact=True) }}
          </div>
        </details>
      </div>
      <ul class="divide-y">
        {% for r in items %}
        <li class="px-4 py-2.5 flex items-center gap-3">
          <span class="text-xs text-slate-400 w-12">{{ r.time or '' }}</span>
          <div class="flex-1 min-w-0 truncate">{{ activity_line(kind, r) }}</div>
          {% if r.cost %}<span class="text-xs rounded-full bg-indigo-50 text-indigo-700 px-2 py-0.5" title="sync กับค่าใช้จ่ายแล้ว">🔗 ฿{{ r.cost|money }}</span>{% endif %}
          {{ row_actions(kind, r, here) }}
        </li>
        {% else %}
        <li class="px-4 py-4 text-sm text-slate-400">ยังไม่มีรายการ</li>
        {% endfor %}
      </ul>
    </section>
    {% endfor %}
  </div>

  <!-- Expenses side -->
  <div class="space-y-6">
    <section class="rounded-2xl bg-white border">
      <div class="px-4 py-3 border-b flex justify-between items-center">
        <h2 class="font-semibold">💸 ค่าใช้จ่ายวันนี้</h2>
        <span class="font-bold">฿{{ s.expense_total|money }}</span>
      </div>
      <div class="p-4 space-y-2">
        {% for c in s.by_cat %}
          <div class="text-sm">
            <div class="flex justify-between"><span>{{ c.category }}</span><span>฿{{ c.s|money }}</span></div>
            <div class="h-1.5 bg-slate-100 rounded-full overflow-hidden"><div class="h-full bg-indigo-500" style="width: {{ (c.s / s.expense_total * 100) if s.expense_total else 0 }}%"></div></div>
          </div>
        {% else %}<p class="text-sm text-slate-400">ยังไม่มีค่าใช้จ่าย</p>{% endfor %}
      </div>
      <ul class="divide-y border-t">
        {% for e in s.expenses %}
        <li class="px-4 py-2 flex items-center gap-2 text-sm">
          <div class="flex-1 min-w-0">
            <div class="truncate">{{ e.note or e.category }}</div>
            <div class="text-xs text-slate-400">{{ e.time or '' }} · {{ e.category }}{% if e.source_type %} · 🔗 sync{% endif %}</div>
          </div>
          <span class="font-medium">฿{{ e.amount|money }}</span>
          {{ row_actions('expenses', e, here) }}
        </li>
        {% endfor %}
      </ul>
      <details class="border-t">
        <summary class="px-4 py-3 cursor-pointer text-sm text-indigo-600">+ เพิ่มค่าใช้จ่ายอื่นๆ</summary>
        <div class="px-4 pb-4">{{ expense_form(next=here) }}</div>
      </details>
    </section>

    <section class="rounded-2xl bg-white border p-4">
      <h2 class="font-semibold mb-3">📊 7 วันล่าสุด</h2>
      {% for metric, label, color, unit in [('expense','ค่าใช้จ่าย (฿)','bg-indigo-500',''), ('water','ดื่มน้ำ (ml)','bg-sky-500',''), ('minutes','ออกกำลังกาย (นาที)','bg-emerald-500',''), ('cal','แคลอรี่ที่กิน','bg-orange-400','')] %}
      <div class="mb-4">
        <div class="text-xs text-slate-500 mb-1">{{ label }}</div>
        <div class="flex items-end gap-1 h-16">
          {% for x in days %}
          <a href="{{ url_for('dashboard', date=x.date) }}" class="flex-1 flex flex-col justify-end h-full group" title="{{ x.label }}: {{ x[metric]|num }}">
            <div class="{{ color }} rounded-t {{ 'ring-2 ring-slate-800' if x.date==d }} group-hover:opacity-80" style="height: {{ (x[metric] / mx[metric] * 100)|round }}%; min-height: 2px"></div>
          </a>
          {% endfor %}
        </div>
        <div class="flex gap-1 text-[10px] text-slate-400">{% for x in days %}<span class="flex-1 text-center">{{ x.label }}</span>{% endfor %}</div>
      </div>
      {% endfor %}
    </section>
  </div>
</div>
{% endblock %}"""

LIST = r"""{% extends 'base.html' %}
{% from 'macros.html' import activity_form, expense_form, row_actions, activity_line with context %}
{% set is_exp = kind == 'expenses' %}
{% set label = 'ค่าใช้จ่าย' if is_exp else ACTIVITIES[kind].label %}
{% set icon = '💸' if is_exp else ACTIVITIES[kind].icon %}
{% block title %}{{ label }}{% endblock %}
{% block content %}
<div class="flex flex-wrap items-center justify-between gap-3 mb-6">
  <h1 class="text-2xl font-bold">{{ icon }} {{ label }}</h1>
  <form method="get" class="flex items-center gap-2 text-sm">
    <input type="date" name="date" value="{{ d }}" class="rounded-lg border px-3 py-2 bg-white">
    <button class="rounded-lg border bg-white px-3 py-2 hover:bg-slate-50">กรอง</button>
    {% if d %}<a href="{{ url_for('list_kind', kind=kind) }}" class="text-slate-500 hover:underline">ทั้งหมด</a>{% endif %}
  </form>
</div>

<div class="grid lg:grid-cols-3 gap-6">
  <section class="rounded-2xl bg-white border p-4 h-fit lg:sticky lg:top-20">
    <h2 class="font-semibold mb-3">{{ 'แก้ไขรายการ' if edit else 'เพิ่มรายการ' }}</h2>
    {% if is_exp %}{{ expense_form(edit) }}{% else %}{{ activity_form(kind, edit) }}{% endif %}
    {% if not is_exp %}<p class="text-xs text-slate-400 mt-3">ใส่ค่าใช้จ่าย &gt; 0 ระบบจะสร้างรายการในหน้า “ค่าใช้จ่าย” หมวด “{{ ACTIVITIES[kind].category }}” ให้อัตโนมัติ</p>{% endif %}
  </section>

  <section class="lg:col-span-2 rounded-2xl bg-white border">
    <div class="px-4 py-3 border-b flex justify-between items-center">
      <span class="text-sm text-slate-500">{{ rows|length }} รายการ{{ ' · ' + (d|thdate) if d }}</span>
      {% if total is not none %}<span class="font-bold">รวม ฿{{ total|money }}</span>{% endif %}
    </div>
    <ul class="divide-y">
      {% set ns = namespace(last='') %}
      {% for r in rows %}
        {% if r.date != ns.last %}
          {% set ns.last = r.date %}
          <li class="px-4 py-1.5 bg-slate-50 text-xs font-medium text-slate-500"><a href="{{ url_for('dashboard', date=r.date) }}" class="hover:underline">{{ r.date|thdate }}</a></li>
        {% endif %}
        <li class="px-4 py-2.5 flex items-center gap-3 {{ 'bg-amber-50' if edit and edit.id==r.id }}">
          <span class="text-xs text-slate-400 w-12">{{ r.time or '' }}</span>
          <div class="flex-1 min-w-0">
            {% if is_exp %}
              <div class="truncate">{{ r.note or '-' }}</div>
              <div class="text-xs text-slate-400">{{ r.category }}{% if r.source_type %} · 🔗 sync จาก{{ ACTIVITIES[r.source_type].label }}{% endif %}</div>
            {% else %}
              <div class="truncate">{{ activity_line(kind, r) }}</div>
              {% if r.note %}<div class="text-xs text-slate-400 truncate">{{ r.note }}</div>{% endif %}
            {% endif %}
          </div>
          {% if is_exp %}<span class="font-medium">฿{{ r.amount|money }}</span>
          {% elif r.cost %}<span class="text-xs rounded-full bg-indigo-50 text-indigo-700 px-2 py-0.5">🔗 ฿{{ r.cost|money }}</span>{% endif %}
          {{ row_actions(kind, r) }}
        </li>
      {% else %}
        <li class="px-4 py-8 text-center text-sm text-slate-400">ยังไม่มีรายการ</li>
      {% endfor %}
    </ul>
  </section>
</div>
{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE,
    "macros.html": MACROS,
    "dashboard.html": DASHBOARD,
    "list.html": LIST,
})

init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
