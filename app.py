import os
import json
import csv
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file, abort
)
from flask_sqlalchemy import SQLAlchemy

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# SQLite por defecto; usa DATABASE_URL (Render/Heroku) si existe
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Render/Heroku pueden entregar postgres:// -> convertir a postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///big5.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
# Crea tablas en el primer arranque si no existen (útil en Render)
with app.app_context():
    db.create_all()


# -----------------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------------
class Submission(db.Model):
    __tablename__ = "submissions"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Metadata opcional
    nickname = db.Column(db.String(120))
    email = db.Column(db.String(255))
    # Respuestas en JSON string (id->score)
    answers_json = db.Column(db.Text, nullable=False)
    # Puntajes finales
    score_O = db.Column(db.Integer, nullable=False)
    score_C = db.Column(db.Integer, nullable=False)
    score_E = db.Column(db.Integer, nullable=False)
    score_A = db.Column(db.Integer, nullable=False)
    score_N = db.Column(db.Integer, nullable=False)
    # Porcentajes 0-100 (aprox)
    pct_O = db.Column(db.Float, nullable=False)
    pct_C = db.Column(db.Float, nullable=False)
    pct_E = db.Column(db.Float, nullable=False)
    pct_A = db.Column(db.Float, nullable=False)
    pct_N = db.Column(db.Float, nullable=False)

# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------
def load_assessment():
    path = os.path.join(os.path.dirname(__file__), "assessments", "big5_60.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def compute_scores(answers_dict, spec):
    """
    answers_dict: { "1": 1..5, "2": 1..5, ... }
    spec: JSON del test
    Retorna dict con sumas y porcentajes.
    """
    trait_map = {"O": 0, "C": 0, "E": 0, "A": 0, "N": 0}
    counts = {"O": 0, "C": 0, "E": 0, "A": 0, "N": 0}

    reverse_items = {str(it["id"]) for it in spec["items"] if it.get("reverse", False)}
    trait_of = {str(it["id"]): it["trait"] for it in spec["items"]}

    for qid, raw in answers_dict.items():
        val = int(raw)
        if qid in reverse_items:
            val = 6 - val  # invertir
        trait = trait_of[qid]
        trait_map[trait] += val
        counts[trait] += 1

    # Normalizar 0-100 en base a rango [count, 5*count]
    pct = {}
    for t in trait_map:
        c = max(1, counts[t])
        min_sum, max_sum = c * 1, c * 5
        pct[t] = (trait_map[t] - min_sum) / (max_sum - min_sum) * 100.0

    return {
        "sum": trait_map,
        "pct": pct,
    }

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper

# -----------------------------------------------------------------------------
# Rutas
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("test"))

@app.route("/test", methods=["GET", "POST"])
def test():
    spec = load_assessment()
    if request.method == "POST":
        # Validar que todas las preguntas tienen respuesta
        answers = {}
        missing = []
        for item in spec["items"]:
            qid = str(item["id"])
            val = request.form.get(f"q{qid}")
            if not val:
                missing.append(qid)
            else:
                answers[qid] = int(val)

        if missing:
            flash("Por favor, responde todas las preguntas antes de enviar.", "warning")
            return render_template("test.html", spec=spec, prev=request.form)

        # Datos opcionales del participante
        nickname = request.form.get("nickname") or None
        email = request.form.get("email") or None

        # Calcular puntajes
        res = compute_scores(answers, spec)
        sums = res["sum"]
        pcts = res["pct"]

        sub = Submission(
            nickname=nickname,
            email=email,
            answers_json=json.dumps(answers, ensure_ascii=False),
            score_O=sums["O"], score_C=sums["C"], score_E=sums["E"],
            score_A=sums["A"], score_N=sums["N"],
            pct_O=pcts["O"], pct_C=pcts["C"], pct_E=pcts["E"],
            pct_A=pcts["A"], pct_N=pcts["N"],
        )
        db.session.add(sub)
        db.session.commit()

        # Mostrar página de gracias con resultados
        results = [
            ("Apertura a la experiencia (O)", sums["O"], round(pcts["O"], 1)),
            ("Responsabilidad / Escrupulosidad (C)", sums["C"], round(pcts["C"], 1)),
            ("Extraversión (E)", sums["E"], round(pcts["E"], 1)),
            ("Amabilidad (A)", sums["A"], round(pcts["A"], 1)),
            ("Neuroticismo (N)", sums["N"], round(pcts["N"], 1)),
        ]
        return render_template("thanks.html", results=results, spec=spec, sub_id=sub.id)

    return render_template("test.html", spec=spec)

@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Admin súper simple con usuario/password en variables de entorno:
    ADMIN_USER / ADMIN_PASSWORD
    """
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        env_u = os.environ.get("ADMIN_USER", "admin")
        env_p = os.environ.get("ADMIN_PASSWORD", "admin123")
        if u == env_u and p == env_p:
            session["admin"] = True
            flash("Sesión iniciada.", "success")
            next_url = request.args.get("next") or url_for("admin")
            return redirect(next_url)
        flash("Credenciales inválidas.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))

@app.route("/admin")
@admin_required
def admin():
    subs = Submission.query.order_by(Submission.ts.desc()).all()
    # Prepara un resumen compacto para la tabla
    rows = []
    for s in subs:
        rows.append({
            "id": s.id,
            "ts": s.ts.strftime("%Y-%m-%d %H:%M"),
            "nickname": s.nickname or "",
            "email": s.email or "",
            "O": s.score_O, "C": s.score_C, "E": s.score_E, "A": s.score_A, "N": s.score_N
        })
    return render_template("admin.html", rows=rows)

@app.route("/admin/export.csv")
@admin_required
def admin_export_csv():
    subs = Submission.query.order_by(Submission.ts.asc()).all()
    if not subs:
        abort(404, "No hay datos para exportar.")
    fname = f"big5_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    path = os.path.join("/tmp", fname)

    fieldnames = [
        "id","timestamp","nickname","email",
        "score_O","score_C","score_E","score_A","score_N",
        "pct_O","pct_C","pct_E","pct_A","pct_N",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in subs:
            writer.writerow({
                "id": s.id,
                "timestamp": s.ts.isoformat(),
                "nickname": s.nickname or "",
                "email": s.email or "",
                "score_O": s.score_O, "score_C": s.score_C, "score_E": s.score_E, "score_A": s.score_A, "score_N": s.score_N,
                "pct_O": round(s.pct_O, 2), "pct_C": round(s.pct_C, 2), "pct_E": round(s.pct_E, 2), "pct_A": round(s.pct_A, 2), "pct_N": round(s.pct_N, 2),
            })

    return send_file(path, as_attachment=True, download_name=fname)

# -----------------------------------------------------------------------------
# CLI helper (crear DB local rápidamente)
# -----------------------------------------------------------------------------
@app.cli.command("init-db")
def init_db():
    """Crea tablas de la base de datos."""
    db.create_all()
    print("Base de datos inicializada.")

# -----------------------------------------------------------------------------
# App factory fallback
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
