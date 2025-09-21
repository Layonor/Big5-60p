import os
import json
import csv
import smtplib
from io import StringIO, BytesIO
from email.message import EmailMessage
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file
)
from flask_sqlalchemy import SQLAlchemy

# -----------------------------------------------------------------------------
# App & Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-me")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Base de datos:
# - Por defecto, usar SQLite en /tmp (ideal para Render Free)
# - Si deseas otra URI, define SQLALCHEMY_DATABASE_URI en env.
db_uri = os.environ.get("SQLALCHEMY_DATABASE_URI")
if not db_uri:
    # Usa una base de datos en la misma carpeta del proyecto
    db_path = os.path.join(os.path.dirname(__file__), 'big5.db')
    db_uri = f"sqlite:///{db_path}"

app.config["SQLALCHEMY_DATABASE_URI"] = db_uri

db = SQLAlchemy(app)

# Crear tablas al arrancar (útil en Render/primer deploy)
with app.app_context():
    db.create_all()

# Correo del administrador por defecto
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "vale.romero@gmail.com")


# -----------------------------------------------------------------------------
# Utilidades: cargar test y calcular puntuaciones
# -----------------------------------------------------------------------------
def load_assessment():
    """
    Carga assessments/big5_60.json. Busca al lado de app.py y, si no, en cwd.
    Normaliza estructura (scale, items).
    """
    base = Path(__file__).resolve().parent
    primary = base / "assessments" / "big5_60.json"
    fallback = Path.cwd() / "assessments" / "big5_60.json"
    path = primary if primary.exists() else fallback
    if not path.exists():
        raise FileNotFoundError(
            f"No se encontró el archivo del test en: {primary} ni en: {fallback}"
        )
    with path.open("r", encoding="utf-8") as f:
        spec = json.load(f)

    # Asegurar escala
    scale = spec.get("scale", {})
    s_min = int(scale.get("min", 1))
    s_max = int(scale.get("max", 5))
    options = scale.get("options")
    if not options:
        options = [{"value": v} for v in range(s_min, s_max + 1)]
        scale["options"] = options
    scale["min"] = s_min
    scale["max"] = s_max
    scale.setdefault("label", f"{s_min} = Muy en desacuerdo | {s_max} = Muy de acuerdo")
    spec["scale"] = scale

    # Normalizar items
    norm_items = []
    for it in spec.get("items", []):
        norm_items.append({
            "id": int(it["id"]),
            "text": it["text"],
            "trait": str(it.get("trait", "")).upper(),
            "reverse": bool(it.get("reverse", False)),
        })
    spec["items"] = norm_items

    return spec


def score_answers(form_dict, spec):
    """
    Devuelve:
      - sums: dict con sumas O,C,E,A,N
      - percent: dict con % 0-100 por rasgo
      - percent_list: lista [(nombre, suma, porcentaje_int), ...] para mostrar/enviar
    """
    s_min = spec["scale"]["min"]
    s_max = spec["scale"]["max"]
    traits = ["O", "C", "E", "A", "N"]
    sums = {t: 0 for t in traits}

    items_by_id = {int(it["id"]): it for it in spec["items"]}

    for qid, item in items_by_id.items():
        key = f"q{qid}"
        val_str = form_dict.get(key)
        if val_str is None:
            raise ValueError(f"Falta responder el ítem {qid}")
        try:
            val = int(val_str)
        except ValueError:
            raise ValueError(f"Respuesta inválida en ítem {qid}")

        if item["reverse"]:
            val = s_min + s_max - val  # inversión de escala

        trait = item["trait"]
        if trait not in sums:
            raise ValueError(f"Ítem {qid} con rasgo desconocido: {trait}")
        sums[trait] += val

    # Normalización 0-100 (12 ítems por rasgo)
    min_total = 12 * s_min
    max_total = 12 * s_max
    rng = max_total - min_total if (max_total - min_total) != 0 else 1

    percent = {t: int(round((sums[t] - min_total) * 100.0 / rng)) for t in traits}

    percent_list = [
        ("Apertura (O)",        sums["O"], percent["O"]),
        ("Responsabilidad (C)", sums["C"], percent["C"]),
        ("Extraversión (E)",    sums["E"], percent["E"]),
        ("Amabilidad (A)",      sums["A"], percent["A"]),
        ("Neuroticismo (N)",    sums["N"], percent["N"]),
    ]
    return sums, percent, percent_list


def make_csv_bytes(rows, fieldnames):
    """Crea un CSV en memoria (BytesIO) a partir de filas y encabezados."""
    sio = StringIO()
    writer = csv.DictWriter(sio, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    data = sio.getvalue().encode("utf-8")
    bio = BytesIO(data)
    bio.seek(0)
    return bio


# -----------------------------------------------------------------------------
# Modelo
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
        user_ok = (u == os.environ.get("ADMIN_USER", "admin"))
        pass_ok = (p == os.environ.get("ADMIN_PASSWORD", "admin123"))
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
# Email
# -----------------------------------------------------------------------------
def send_admin_email(subject: str, body: str, csv_bytes: BytesIO, csv_name: str):
    """
    Envía un correo simple al ADMIN_EMAIL con un CSV adjunto (opcional).
    Requiere SMTP_* configurado en env si no usas un relay abierto.
    """
    to_addr = os.environ.get("ADMIN_EMAIL", ADMIN_EMAIL)
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    from_addr = os.environ.get("SMTP_FROM", user if user else to_addr)

    if not host:
        # Si no hay SMTP configurado, solo loguea en consola y retorna.
        print("WARN: SMTP_HOST no configurado. No se envió email.")
        print("Asunto:", subject)
        print("Cuerpo:\n", body)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    if csv_bytes is not None:
        msg.add_attachment(
            csv_bytes.read(),
            maintype="text",
            subtype="csv",
            filename=csv_name,
        )

    # Envío con STARTTLS
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        try:
            smtp.starttls()
        except Exception:
            pass
        if user and pwd:
            smtp.login(user, pwd)
        smtp.send_message(msg)


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
            flash("Faltan respuestas, por favor completa todas las preguntas.", "warning")
            return render_template("test.html", spec=spec, items=spec["items"], prev=request.form)

        # Calcular puntuaciones
        sums, percent, percent_list = score_answers(request.form, spec)

        # Guardar resumen (sumas por rasgo)
        r = Response(
            nickname=request.form.get("nickname", "").strip() or None,
            email=request.form.get("email", "").strip() or None,
            O=sums["O"], C=sums["C"], E=sums["E"], A=sums["A"], N=sums["N"],
        )
        db.session.add(r)
        db.session.commit()

        # Construir CSV con respuestas crudas Q1..Q60 + porcentajes
        answers_row = {
            "id": r.id,
            "ts_utc": r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            "nickname": r.nickname or "",
            "email": r.email or "",
            "O_sum": r.O, "C_sum": r.C, "E_sum": r.E, "A_sum": r.A, "N_sum": r.N,
            "O_pct": percent["O"], "C_pct": percent["C"], "E_pct": percent["E"],
            "A_pct": percent["A"], "N_pct": percent["N"],
        }
        # Agregar respuestas crudas:
        for it in spec["items"]:
            answers_row[f"Q{it['id']}"] = request.form.get(f"q{it['id']}")

        fieldnames = list(answers_row.keys())
        csv_mem = make_csv_bytes([answers_row], fieldnames)
        csv_name = f"big5_respuestas_{r.id}_{r.ts.strftime('%Y%m%d_%H%M%S')}.csv"

        # Email a admin
        subject = f"[Big5-60p] Nuevo test #{r.id}"
        body_lines = [
            "Nuevo cuestionario Big Five (60 ítems)",
            f"ID: {r.id}",
            f"Fecha (UTC): {r.ts.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Nombre/apodo: {r.nickname or ''}",
            f"Email informado: {r.email or ''}",
            "",
            "Sumas por rasgo:",
            f"  O: {r.O}",
            f"  C: {r.C}",
            f"  E: {r.E}",
            f"  A: {r.A}",
            f"  N: {r.N}",
            "",
            "Porcentajes (0-100):",
            f"  O: {percent['O']}%",
            f"  C: {percent['C']}%",
            f"  E: {percent['E']}%",
            f"  A: {percent['A']}%",
            f"  N: {percent['N']}%",
            "",
            "Se adjunta CSV con todas las respuestas crudas y los porcentajes.",
        ]
        body = "\n".join(body_lines)

        # enviar
        csv_mem.seek(0)
        try:
            send_admin_email(subject, body, csv_mem, csv_name)
        except Exception as e:
            # No interrumpir el flujo del usuario por fallo de email
            print("ERROR enviando email:", e)

        # No mostramos resultados al candidato
        return redirect(url_for("thanks"))

    # GET
    return render_template("test.html", spec=spec, items=spec["items"], prev=None)


@app.route("/thanks")
def thanks():
    # Gracias simple, sin mostrar puntajes.
    return render_template("thanks.html")


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
    sio = StringIO()
    writer = csv.writer(sio)
    writer.writerow(["id", "ts_utc", "nickname", "email", "O", "C", "E", "A", "N"])
    for r in rows:
        writer.writerow([
            r.id,
            r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            r.nickname or "",
            r.email or "",
            r.O, r.C, r.E, r.A, r.N,
        ])
    mem = BytesIO(sio.getvalue().encode("utf-8"))
    mem.seek(0)
    filename = f"big5_responses_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(
        mem,
        mimetype="text/csv; charset=utf-8",
        download_name=filename,
        as_attachment=True,
    )


# -----------------------------------------------------------------------------
# Arranque local
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # En local: python app.py
    app.run(host="127.0.0.1", port=5000, debug=False)
