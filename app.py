import os
import json
import csv
from io import StringIO, BytesIO
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
import smtplib
from email.message import EmailMessage

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file
)
from flask_sqlalchemy import SQLAlchemy

# -----------------------------------------------------------------------------
# Crear app y config base
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-me")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --- DB: forzar SQLite en /tmp (escribible en Render) ---
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////tmp/big5.db"

db = SQLAlchemy(app)

# Crea tablas al arrancar
with app.app_context():
    db.create_all()

# -----------------------------------------------------------------------------
# Email: configuración por variables de entorno
# -----------------------------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "no-reply@example.com")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")  # <— tu correo destino

def send_admin_email(subject: str, body_text: str, csv_bytes: bytes | None = None, csv_name: str = "respuestas.csv"):
    """
    Envía email al ADMIN_EMAIL con texto y CSV adjunto (opcional).
    No rompe el flujo si falla: registra y continúa.
    """
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and ADMIN_EMAIL):
        # Config incompleta: no enviar, pero no romper
        app.logger.warning("Email no enviado: faltan variables SMTP/ADMIN_EMAIL")
        return

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = ADMIN_EMAIL
        msg.set_content(body_text)

        if csv_bytes:
            msg.add_attachment(
                csv_bytes,
                maintype="text",
                subtype="csv",
                filename=csv_name
            )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        app.logger.exception(f"Fallo enviando email: {e}")

# -----------------------------------------------------------------------------
# Utilidades: cargar test y calcular puntuaciones
# -----------------------------------------------------------------------------
def load_assessment():
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

    # Escala
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

    # Items normalizados
    for i in spec.get("items", []):
        i["id"] = int(i["id"])
        i["reverse"] = bool(i.get("reverse", False))
        i["trait"] = i.get("trait", "").upper()

    return spec


def score_answers(form_dict, spec):
    s_min = spec["scale"]["min"]
    s_max = spec["scale"]["max"]
    traits = ["O", "C", "E", "A", "N"]
    sums = {t: 0 for t in traits}

    items = {int(it["id"]): it for it in spec["items"]}

    for qid, item in items.items():
        key = f"q{qid}"
        val_str = form_dict.get(key)
        if val_str is None:
            raise ValueError(f"Falta responder el ítem {qid}")
        try:
            val = int(val_str)
        except ValueError:
            raise ValueError(f"Respuesta inválida en ítem {qid}")
        if item.get("reverse"):
            val = s_min + s_max - val
        sums[item["trait"]] += val

    # 0–100
    min_total = 12 * s_min
    max_total = 12 * s_max
    rng = max_total - min_total
    percent = {t: (int(round((sums[t] - min_total) * 100 / rng)) if rng else 0) for t in traits}

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
    db.create_all()
    print("Base de datos inicializada.")

# -----------------------------------------------------------------------------
# Auth simple
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
# Rutas
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("test"))

@app.route("/test", methods=["GET", "POST"])
def test():
    spec = load_assessment()

    if request.method == "POST":
        # Validación
        expected = {f"q{i['id']}" for i in spec["items"]}
        got = {k for k in request.form.keys() if k.startswith("q")}
        if expected - got:
            flash("Faltan respuestas, por favor completa todas las preguntas.", "warning")
            return render_template("test.html", spec=spec, prev=request.form)

        # Calcular + guardar
        sums, percent_list = score_answers(request.form, spec)
        r = Response(
            nickname=request.form.get("nickname", "").strip() or None,
            email=request.form.get("email", "").strip() or None,
            O=sums["O"], C=sums["C"], E=sums["E"], A=sums["A"], N=sums["N"],
        )
        db.session.add(r)
        db.session.commit()

        # Preparar email al admin (texto + CSV adjunto)
        nick = r.nickname or "(sin nombre)"
        mail_user = r.email or "(sin email)"
        lines = [f"Big5-60p - Nuevo resultado",
                 f"Fecha (UTC): {r.ts.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"Nombre: {nick}",
                 f"Email: {mail_user}",
                 ""]
        for name, score, pct in percent_list:
            lines.append(f"{name}: suma={score}, %={pct}")
        body = "\n".join(lines)

        # CSV (1 fila)
        csv_io = StringIO()
        w = csv.writer(csv_io)
        w.writerow(["id","ts_utc","nickname","email","O","C","E","A","N"])
        w.writerow([r.id, r.ts.strftime("%Y-%m-%d %H:%M:%S"), nick, mail_user, r.O, r.C, r.E, r.A, r.N])
        csv_bytes = csv_io.getvalue().encode("utf-8")

        # Enviar (no rompe si falla)
        send_admin_email(
            subject="Big5-60p: nuevo resultado",
            body_text=body,
            csv_bytes=csv_bytes,
            csv_name=f"big5_respuesta_{r.id}.csv"
        )

        # NO mostramos análisis al candidato
        return redirect(url_for("thanks"))

    return render_template("test.html", spec=spec, prev=None)

@app.route("/thanks")
def thanks():
    # Mensaje neutro (no mostramos resultados)
    return render_template("thanks.html")

# -----------------------------------------------------------------------------
# Admin
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
            r.id, r.ts.strftime("%Y-%m-%d %H:%M:%S"),
            r.nickname or "", r.email or "",
            r.O, r.C, r.E, r.A, r.N,
        ])
    mem = BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    filename = f"big5_responses_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(
        mem,
        mimetype="text/csv; charset=utf-8",
        download_name=filename,
        as_attachment=True,
    )

# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
