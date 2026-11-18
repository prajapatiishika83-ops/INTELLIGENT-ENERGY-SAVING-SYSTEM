"""Intelligent Energy Saving System (IESS) - Flask backend.
Detection = what the AI sees. Energy = ESTIMATE from editable wattage tables (a camera cannot measure watts)."""
import os, json, sqlite3, random, time
from datetime import datetime
from functools import wraps
import cv2, numpy as np
from flask import Flask, render_template, request, redirect, session, jsonify, url_for, flash
import auth

app = Flask(__name__); app.secret_key = os.environ.get("SECRET_KEY", "iess-demo-secret"); app.register_blueprint(auth.bp)
DB = "iess.db"; os.makedirs("static/results", exist_ok=True)
# name: (watts, default hours/day)
DEFAULTS = {"LED Light": (20, 8), "Fan": (70, 8), "Air Conditioner": (1500, 6), "Computer": (150, 8),
            "Monitor": (40, 8), "TV": (100, 4), "Projector": (300, 3), "Printer": (50, 2),
            "Refrigerator": (150, 24), "Cooler": (200, 8), "Other Appliance": (100, 4)}
# Pretrained COCO YOLO knows only some of these. For lights/fans/ACs supply custom weights (IESS_MODEL=best.pt).
ALIASES = {"tv": "TV", "laptop": "Computer", "refrigerator": "Refrigerator", "microwave": "Other Appliance",
           "oven": "Other Appliance", "toaster": "Other Appliance", "hair drier": "Other Appliance",
           "person": "Person", "ac": "Air Conditioner", "led": "LED Light", "light": "LED Light", "lamp": "LED Light"}
ALIASES.update({k.lower(): k for k in DEFAULTS})
EMITTERS = {"LED Light", "TV", "Monitor", "Projector", "Computer"}  # judged ON by brightness

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, watts REAL, hours REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS analyses(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, source TEXT,
        room TEXT, people INT, appliances INT, power REAL, daily_kwh REAL, cost REAL, alerts INT, result TEXT)""")
    for n, (w, h) in DEFAULTS.items(): c.execute("INSERT OR IGNORE INTO settings VALUES(?,?,?)", (n, w, h))
    c.execute("INSERT OR IGNORE INTO kv VALUES('tariff','8')"); c.commit(); c.close(); auth.init_auth_tables()

def tariff():
    c = db(); v = float(c.execute("SELECT v FROM kv WHERE k='tariff'").fetchone()[0]); c.close(); return v

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if session.get("user") else redirect(url_for("login"))
    return w

# ---------------- Computer vision ----------------
_model = None
def model():
    global _model
    if _model is None:
        try:
            from ultralytics import YOLO
            _model = YOLO(os.environ.get("IESS_MODEL", "yolov8n.pt"))  # nano = laptop friendly
        except Exception as e:
            print("YOLO unavailable -> DEMO mode:", e); _model = False
    return _model

def preprocess(img):
    h, w = img.shape[:2]; s = 960 / max(h, w)
    if s < 1: img = cv2.resize(img, None, fx=s, fy=s)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB); l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(2.0, (8, 8)).apply(l)           # contrast boost for dim rooms
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

def detect(img):
    m = model(); out = []
    if m:
        for box in m(img, verbose=False, conf=0.35)[0].boxes:
            name = m.names[int(box.cls)].lower(); label = ALIASES.get(name)
            if label: out.append({"label": label, "conf": round(float(box.conf), 2),
                                  "box": [int(v) for v in box.xyxy[0].tolist()]})
        mode = "yolo"
    else:  # DEMO: random boxes so the whole pipeline can be shown without a model
        H, W = img.shape[:2]; rnd = random.Random(int(img.mean() * 1000))
        for lab, n in [("Person", 2), ("LED Light", 4), ("Fan", 2), ("Computer", 5), ("Monitor", 3), ("Projector", 1)]:
            for _ in range(rnd.randint(max(n - 1, 0), n)):
                x, y = rnd.randint(0, int(W * .8)), rnd.randint(0, int(H * .8))
                out.append({"label": lab, "conf": 0.0, "box": [x, y, x + W // 10, y + H // 10]})
        mode = "demo"
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    for o in out:  # ON/OFF heuristic
        x1, y1, x2, y2 = o["box"]; crop = g[max(y1, 0):y2, max(x1, 0):x2]
        o["on"] = True if o["label"] not in EMITTERS or crop.size == 0 else bool(np.percentile(crop, 90) > 140)
    return out, mode

def summarize(objs, room, source, mode):
    c = db(); cfg = {r["name"]: (r["watts"], r["hours"]) for r in c.execute("SELECT * FROM settings")}; c.close()
    people = sum(o["label"] == "Person" for o in objs); items = {}
    for o in objs:
        if o["label"] == "Person" or not o["on"]: continue
        w, h = cfg.get(o["label"], (100, 4)); it = items.setdefault(o["label"], {"label": o["label"], "qty": 0, "watts": w, "hours": h})
        it["qty"] += 1
    rows = list(items.values())
    for r in rows: r["power"] = r["qty"] * r["watts"]
    power = sum(r["power"] for r in rows); kwh = sum(r["power"] * r["hours"] for r in rows) / 1000
    alerts, recs = [], []; q = lambda n: items.get(n, {}).get("qty", 0)
    if people == 0 and power:
        alerts.append(f"Room looks empty but {power:.0f} W of appliances appear ON.")
        recs.append("Switch off lights, fans and computers when nobody is present; consider occupancy sensors.")
    if people and q("Computer") > people * 2:
        alerts.append(f"{q('Computer')} computers ON for {people} people."); recs.append("Shut down or sleep unused computers.")
    if people and q("Fan") + q("LED Light") > people * 3:
        alerts.append("Many fans/lights ON for the number of people."); recs.append("Turn off fans/lights in unoccupied zones.")
    if q("Air Conditioner") and people <= 1:
        alerts.append("Air conditioner running with almost no occupants."); recs.append("Raise AC set-point to 24-26 C or switch it off.")
    if q("LED Light") and not recs: recs.append("Use daylight where possible and keep LED lighting on timers.")
    if not recs: recs.append("No obvious wastage found. Keep monitoring.")
    return {"room": room, "source": source, "mode": mode, "people": people, "items": rows, "objects": objs,
            "power": power, "daily_kwh": round(kwh, 2), "cost_day": round(kwh * tariff(), 2), "alerts": alerts, "recs": recs}

def draw(img, objs):
    for o in objs:
        x1, y1, x2, y2 = o["box"]; col = (200, 120, 30) if o["label"] == "Person" else (30, 160, 230)
        if not o["on"]: col = (150, 150, 150)
        cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
        cv2.putText(img, o["label"] + ("" if o["on"] else " (off)"), (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, .5, col, 2)
    return img

def save(res, img):
    name = f"results/{int(time.time()*1000)}.jpg"; cv2.imwrite("static/" + name, img); res["image"] = name
    c = db(); cur = c.execute("INSERT INTO analyses(created,source,room,people,appliances,power,daily_kwh,cost,alerts,result) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), res["source"], res["room"], res["people"], sum(r["qty"] for r in res["items"]),
         res["power"], res["daily_kwh"], res["cost_day"], len(res["alerts"]), json.dumps(res)))
    res["id"] = cur.lastrowid; c.commit(); c.close(); return res

def run_image(img, room, source):
    img = preprocess(img); objs, mode = detect(img); res = summarize(objs, room, source, mode)
    return save(res, draw(img.copy(), objs))

def run_video(path, room):
    cap = cv2.VideoCapture(path); n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0); frames = []
    for i in np.linspace(0, max(n - 1, 0), 8).astype(int):  # sample 8 frames
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i)); ok, f = cap.read()
        if ok:
            f = preprocess(f); objs, mode = detect(f); frames.append((summarize(objs, room, "video", mode), f, objs))
    cap.release()
    if not frames: return None
    frames.sort(key=lambda t: t[0]["power"]); res, f, objs = frames[len(frames) // 2]  # median-power frame
    res["timeline"] = [t[0]["power"] for t in sorted(frames, key=lambda t: 0)]
    return save(res, draw(f.copy(), objs))

# ---------------- Routes ----------------
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ident, pw = request.form.get("username", ""), request.form.get("password", "")
        if ident == "admin" and pw == "1234":  # legacy demo account still works
            session["user"] = "admin"; return redirect(url_for("dashboard"))
        u = auth.authenticate(ident, pw)
        if u: session["user"] = u["email"]; return redirect(url_for("dashboard"))
        flash("Incorrect username/email or password.")
    return render_template("login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    c = db(); rows = c.execute("SELECT * FROM analyses WHERE id IN (SELECT MAX(id) FROM analyses GROUP BY room)").fetchall(); c.close()
    d = {"rooms": len(rows), "people": sum(r["people"] for r in rows), "appliances": sum(r["appliances"] for r in rows),
         "power": sum(r["power"] for r in rows), "daily": round(sum(r["daily_kwh"] for r in rows), 2),
         "alerts": sum(r["alerts"] for r in rows)}
    d["monthly"] = round(d["daily"] * 30, 1); d["cost"] = round(d["monthly"] * tariff(), 2)
    return render_template("dashboard.html", d=d)

@app.route("/analyze")
@login_required
def analyze_page(): return render_template("analyze.html", mode=request.args.get("mode", "photo"))

@app.route("/scanner")
@login_required
def scanner(): return render_template("scanner.html")

@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    f = request.files.get("file"); room = request.form.get("room") or "Room 1"; kind = request.form.get("kind", "photo")
    if not f: return jsonify(error="No file received."), 400
    if kind == "video":
        p = f"static/results/up_{int(time.time())}.mp4"; f.save(p); res = run_video(p, room); os.remove(p)
        if not res: return jsonify(error="Could not read that video."), 400
        return jsonify(res)
    img = cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)
    if img is None: return jsonify(error="Could not read that image."), 400
    return jsonify(run_image(img, room, kind))

@app.route("/api/frame", methods=["POST"])  # live AR scanner: detect only, nothing saved
@login_required
def api_frame():
    img = cv2.imdecode(np.frombuffer(request.files["file"].read(), np.uint8), cv2.IMREAD_COLOR)
    objs, mode = detect(img); res = summarize(objs, "live", "ar", mode)
    return jsonify(objects=objs, power=res["power"], people=res["people"], mode=mode, size=img.shape[:2])

@app.route("/view3d")
@login_required
def view3d():
    c = db(); aid = request.args.get("id"); r = c.execute("SELECT result FROM analyses WHERE id=?" if aid else
        "SELECT result FROM analyses ORDER BY id DESC LIMIT 1", (aid,) if aid else ()).fetchone(); c.close()
    return render_template("view3d.html", data=json.loads(r[0]) if r else None)

@app.route("/analytics")
@login_required
def analytics():
    c = db(); rows = [dict(r) for r in c.execute("SELECT id,created,room,power,daily_kwh FROM analyses ORDER BY id DESC LIMIT 20")][::-1]
    last = c.execute("SELECT result FROM analyses ORDER BY id DESC LIMIT 1").fetchone(); c.close()
    return render_template("analytics.html", rows=rows, last=json.loads(last[0]) if last else None)

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    c = db()
    if request.method == "POST":
        for n in DEFAULTS:
            c.execute("UPDATE settings SET watts=?, hours=? WHERE name=?", (request.form[f"w_{n}"], request.form[f"h_{n}"], n))
        c.execute("UPDATE kv SET v=? WHERE k='tariff'", (request.form["tariff"],)); c.commit(); flash("Settings saved.")
    rows = c.execute("SELECT * FROM settings").fetchall(); c.close()
    return render_template("settings.html", rows=rows, tariff=tariff())

@app.route("/history")
@login_required
def history():
    c = db(); rows = c.execute("SELECT * FROM analyses ORDER BY id DESC").fetchall(); c.close()
    return render_template("history.html", rows=rows)

if __name__ == "__main__":
    init_db(); app.run(debug=True)
