from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, UserMixin, current_user
from flask_bcrypt import Bcrypt
import mysql.connector
import pandas as pd
from langdetect import detect, DetectorFactory
from dateutil import parser, tz



DetectorFactory.seed = 0

app = Flask(__name__)
app.secret_key = "supersecret"

#  MySQL Config
db_config = {
    "host": "localhost",
    "user": "root",
    "password": "",
    "database": "insightbot_db"
}

bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# Handle common timezone abbreviations
TZINFOS = {
    "EST": tz.gettz("America/New_York"),
    "EDT": tz.gettz("America/New_York"),
    "CST": tz.gettz("America/Chicago"),
    "CDT": tz.gettz("America/Chicago"),
    "MST": tz.gettz("America/Denver"),
    "MDT": tz.gettz("America/Denver"),
    "PST": tz.gettz("America/Los_Angeles"),
    "PDT": tz.gettz("America/Los_Angeles"),
    "PT":  tz.gettz("America/Los_Angeles"),
    "ET":  tz.gettz("America/New_York")
}

# ---- Normalize date helper ----
def normalize_date(val):
    if pd.isna(val) or not str(val).strip():
        return ""
    try:
        dt = parser.parse(str(val), fuzzy=True, tzinfos=TZINFOS)
        return dt.strftime("%b %d, %Y %I:%M %p")  # English, standardized
    except Exception:
        return str(val)  # fallback: raw text


#  Load articles dataset (CSV for now, later we move to DB)
try:
    df = pd.read_csv("news_hybrid_training.csv")
except FileNotFoundError:
    df = pd.DataFrame(columns=["title", "body", "site", "date", "url", "published"])

# Add language column if missing
if "language" not in df.columns:
    print("Language column missing — auto detecting...")
    df["language"] = df["body"].fillna("").apply(
        lambda text: detect(text) if text.strip() else "unknown"
    )

# Normalize date column
if "published" in df.columns:
    df["published"] = df["published"].apply(normalize_date)
elif "date" in df.columns:
    df["published"] = df["date"].apply(normalize_date)
else:
    df["published"] = ""

# ---- User Class ----
class User(UserMixin):
    def __init__(self, id, username, is_admin, is_approved):
        self.id = id
        self.username = username
        self.is_admin = is_admin
        self.is_approved = is_approved

@login_manager.user_loader
def load_user(user_id):
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    conn.close()
    if user:
        return User(user["id"], user["username"], user["is_admin"], user["is_approved"])
    return None

# ---- Routes ----
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        email = request.form.get("email")

        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            conn.close()
            flash("Username already exists")
            return redirect(url_for("register"))

        pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        cursor.execute("INSERT INTO users (username, password_hash, email, is_admin, is_approved) VALUES (%s, %s, %s, %s, %s)",
                       (username, pw_hash, email, False, False))
        conn.commit()
        conn.close()
        flash("Registered successfully. Wait for admin approval.")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and bcrypt.check_password_hash(user["password_hash"], password):
            if not user["is_approved"]:
                flash("Your account is pending admin approval.")
                return redirect(url_for("login"))
            login_user(User(user["id"], user["username"], user["is_admin"], user["is_approved"]))
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/admin/approve")
@login_required
def admin_approve():
    if not current_user.is_admin:
        return "Access denied", 403
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, email FROM users WHERE is_approved = FALSE")
    pending_users = cursor.fetchall()
    conn.close()
    return render_template("approve.html", users=pending_users)

@app.route("/admin/approve/<int:user_id>")
@login_required
def approve_user(user_id):
    if not current_user.is_admin:
        return "Access denied", 403
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_approved = TRUE WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    flash("User approved successfully!")
    return redirect(url_for("admin_approve"))

@app.route("/", methods=["GET"])
@login_required
def index():
    if df.empty:
        return "No articles available yet."

    # Get selected language from query string
    selected_lang = request.args.get("lang")

    if selected_lang:
        filtered = df[df["language"] == selected_lang]
    else:
        filtered = df

    articles = filtered.to_dict(orient="records")
    languages = df["language"].dropna().unique().tolist()

    return render_template("index.html",
                           articles=articles,
                           languages=languages,
                           selected_lang=selected_lang)

@app.route("/article/<int:article_id>")
@login_required
def article(article_id):
    selected_lang = request.args.get("lang")  # keep filter info

    if article_id >= len(df):
        return "Article not found", 404

    row = df.iloc[article_id].to_dict()
    return render_template("article.html", article=row, selected_lang=selected_lang)


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

def clean_date_for_tableau(raw_date):
    if not raw_date or str(raw_date).strip() in ["", "0000-00-00 00:00:00"]:
        return None
    try:
        dt = parser.parse(str(raw_date), fuzzy=True, tzinfos=TZINFOS)
        # Return ISO string
        return dt.isoformat()  # e.g. "2025-09-14T14:30:00"
    except Exception:
        return None



# connecting tableau
@app.route("/api/articles")
def api_articles():
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT url, title, body, published, length, source FROM articles")
    rows = cursor.fetchall()
    conn.close()
    for row in rows:
        row["published"] = clean_date_for_tableau(row.get("puflblished"))
    return jsonify(rows)

@app.route("/tableau_wdc")
def tableau_wdc():
    return render_template("tableau_wdc.html")



if __name__ == "__main__":
    app.run(debug=True)
