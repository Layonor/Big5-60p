import os
import json
import csv
from io import StringIO, BytesIO
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file, abort
)
from flask_sqlalchemy import SQLAlchemy

# -----------------------------------------------------------------------------
# Crear la app y configuración base
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-me")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --- Config DB: SQLite por defecto; DATABASE_URL (Render) si existe ---
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Algunas PaaS entregan postgres://; SQLAlchemy espera postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    # Si usas psycopg v3, fuerza el driver explícito (no afecta a SQLite)
    if database_url.startswith("postgresql://") and "+psycopg" not in database_url and "+pg8000" not in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///big5.db"

db = SQLAlchemy(app)

# Crea tablas al arrancar (útil en Render/primer deploy)
with app.app_context():
    db.create_all()

# -----------------------------------------------------------------------------
# Utilidades: cargar test y calcular puntuaciones
# -----------------------------------------------------------------------------
def load_assessment():
    """
    Carga assessments/big5_60.json. Si no encuentra la ruta al lado del app.py,
    intenta con el cwd (por si se ejecuta desde otra carpeta).
    """
    base = Path(__file__).resolve().parent
    primary = base / "assessments" / "big5_60.json"
    fallback = Path.cwd() / "assessments" / "big5_60.json"
    path = primary if primary.exists() else fallback
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el archivo del test en: {primary} ni en: {fallback}")
    with path.open("r", encoding="utf-8") as f:
        spec = json.load(f)

    # Asegura opciones de escala si no vienen
    scale = spec.get("scale", {})
    s_min = int(scale.get("min", 1))
    s_max = int(scale.get("max", 5))
    options = scale.get("options")
    if not options:
        options = [{"value": v} for v in range(s_min, s_max + 1)]
        scale["options"] = options
    scale["min"] = s_min
    scale["max"] = s_max
    scale.setdefault("label", "1 = Muy en desacuerdo | 5 = Muy de acuerdo")
    spec["scale"] = scale

    # Normaliza items
    for i in spec.get("items", []):
        i["id"] = int(i["id"])
        i["reverse"] = bool(i.get("reverse", False))
        i["trait"] = i.get("trait", "").upper()

    return spec


def score_answers(form_dict, spec):
    """
    Devuelve:
      - sums: dict con sumas O,C,E,A,N
      - percent_list: lista [(nombre, suma, porcentaje_int), ...]
    """
    s_min = spec["scale"]["min"]
    s_max = spec["scale"]["max"]
    traits = ["O", "C", "E", "A", "N"]
    sums = {t: 0 for t in traits}

    # Mapa rápido id->item
    items = {int(it["id"]): it for it in spec["items"]}

    # Recorremos los 60 ítems
    for qid, item in items.items():
        key = f"q{qid}"
        val_str = form_dict.get(key)
        if val_str is None:
            raise ValueError(f"Falta responder el ítem {qid}")
        try:
            val = int(val_str)
        except ValueError:
            raise ValueError(f"Respuesta inválida en ítem {qid}")
        # Reversa
        if item.get("reverse"):
            val = s_min + s_max - val
        sums[item["trait"]] += val

    # Normalización 0-100 (cada rasgo tiene 12 ítems)
    min_total = 12 * s_min
    max_total = 12 * s_max
    rng = max_total - min_total
    percent = {t: int(round((sums[t] - min_total) * 100 / rng)) for t in traits}

    percent_list = [
        ("Apertura (O)", sums["O"], percent["O"]),
        ("Responsabilidad (C)", sums["C"], percent["C"]),
        ("Extraversión (E)", sums["E"], percent["E"]),
        ("Amabilidad (A)", sums["A"], percent["A"]),
        ("Neuroticismo (N)", sums["N"], percent["N"]),
    ]
    return sums, percent_list


# -----------------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------------
class Response(db.Model):
    __tablename__ = "responses"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    nickname = db.Column(db.String(120))
    email = db.Column(db.String(320))
    # Sumas por rasgo
    O = db.Column(db.Integer, nullable=False)
    C = db.Column(db.Integer, nullable=False)
    E = db.Column(db.Integer, nullable=False)
    A = db.Column(db.Integer, nullable=False)
    N = db.Column(db.Integer, nullable=False)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
@app.cli.command("init-db")
def init_db():
    """Crea tablas si no existen."""
    db.create_all()
    print("Base de datos inicializada.")


# -----------------------------------------------------------------------------
# Auth muy simple para admin
# -----------------------------------------------------------------------------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        user_ok = u == os.environ.get("ADMIN_USER", "admin")
        pass_ok = p == os.environ.get("ADMIN_PASSWORD", "admin123")
        if user_ok and pass_ok:
            session["admin"] = True
            flash("Sesión iniciada.", "success")
            nxt = request.args.get("next") or url_for("admin")
            return redirect(nxt)
        flash("Credenciales inválidas.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("admin", None)
    flash("Sesión cerrada.", "info")
    return redirect(url_for("test"))


# -----------------------------------------------------------------------------
# Rutas principales
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("test"))


@app.route("/test", methods=["GET", "POST"])
def test():
    spec = load_assessment()

    if request.method == "POST":
        # Validar que llegó todo
        expected = {f"q{i['id']}" for i in spec["items"]}
        got = {k for k in request.form.keys() if k.startswith("q")}
        if expected - got:
            missing = ", ".join(sorted(expected - got))
            flash("Faltan respuestas, por favor completa todas las preguntas.", "warning")
            # Vuelve a mostrar formulario con lo respondido
            return render_template("test.html", spec=spec, prev=request.form)

        # Calcular y guardar
        sums, percent_list = score_answers(request.form, spec)
        r = Response(
            nickname=request.form.get("nickname", "").strip() or None,
            email=request.form.get("email", "").strip() or None,
            O=sums["O"], C=sums["C"], E=sums["E"], A=sums["A"], N=sums["N"],
        )
        db.session.add(r)
        db.session.commit()

        session["last_results"] = percent_list  # para mostrar en /thanks
        return redirect(url_for("thanks"))

    # GET
    return render_template("test.html", spec=spec, prev=None)


@app.route("/thanks")
def thanks():
    results = session.pop("last_results", None)
    if not results:
        return redirect(url_for("test"))
    return render_template("thanks.html", results=results)


# -----------------------------------------------------------------------------
# Panel admin
# -----------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin():
    rows = (
        Response.query
        .order_by(Response.ts.desc())
        .limit(500)
        .all()
    )
    data = [
        {
            "id": r.id,
            "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            "nickname": r.nickname or "",
            "email": r.email or "",
            "O": r.O, "C": r.C, "E": r.E, "A": r.A, "N": r.N,
        }
        for r in rows
    ]
    return render_template("admin.html", rows=data)


@app.route("/admin/export.csv")
@admin_required
def admin_export_csv():
    rows = Response.query.order_by(Response.ts.asc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "ts_utc", "nickname", "email", "O", "C", "E", "A", "N"])
    for r in rows:
        writer.writerow([
            r.id,
            r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            r.nickname or "",
            r.email or "",
            r.O, r.C, r.E, r.A, r.
