from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, abort
import firebase_admin
from firebase_admin import credentials, auth, firestore
from functools import lru_cache

import os
import json
import firebase_admin
from firebase_admin import credentials

service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")

if not service_account_json:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT env variable not set")

cred = credentials.Certificate(json.loads(service_account_json))
firebase_admin.initialize_app(cred)


# ------------------------
# Firebase Admin Init
# ------------------------

firebase_admin.initialize_app(cred)

db = firestore.client()

# ------------------------
# Flask Init
# ------------------------
app = Flask(__name__)
app.secret_key = "hackathon_secret_key"

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False
)

# ------------------------
# Cached Dashboard Data
# ------------------------
@lru_cache(maxsize=1)
def get_dashboard_data():
    users = list(db.collection("users").stream())
    events = list(db.collection("events").stream())
    return users, events

# ------------------------
# Helpers
# ------------------------
def get_user(uid):
    doc = db.collection("users").document(uid).get()
    return doc.to_dict() if doc.exists else None

# ------------------------
# Routes
# ------------------------
@app.route("/")
def index():
    if "uid" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/register")
def register():
    return render_template("register.html")

@app.route("/verify-email")
def verify_email():
    return render_template("verify_email.html")

@app.route("/set-session", methods=["POST"])
def set_session():
    data = request.get_json()
    id_token = data.get("idToken")

    try:
        decoded = auth.verify_id_token(id_token)
        uid = decoded["uid"]

        user_doc = db.collection("users").document(uid).get()
        if not user_doc.exists:
            abort(403)

        user = user_doc.to_dict()

        if not decoded.get("email_verified") and not user.get("approved"):
            abort(403)

        session["uid"] = uid
        return {"status": "success"}

    except Exception:
        abort(401)

# ------------------------
# Dashboard
# ------------------------
@app.route("/dashboard")
def dashboard():
    if "uid" not in session:
        return redirect(url_for("login"))

    user = get_user(session["uid"])
    if not user:
        abort(403)

    username = user.get("username")
    role = user.get("role")

    users, events = get_dashboard_data()

    # ---------------- ADMIN ----------------
    if role == "admin":
        total_users = len(users)
        total_admins = len([u for u in users if u.to_dict().get("role") == "admin"])
        total_students = total_users - total_admins
        approved_users = len([u for u in users if u.to_dict().get("approved")])
        pending_access = total_users - approved_users
        total_events = len(events)

        recent_events = []
        for doc in sorted(
            events,
            key=lambda x: x.to_dict().get("created_at", datetime.min),
            reverse=True
        )[:5]:
            e = doc.to_dict()
            e["id"] = doc.id
            recent_events.append(e)

        return render_template(
            "admin.html",
            username=username,
            total_users=total_users,
            total_admins=total_admins,
            total_students=total_students,
            approved_users=approved_users,
            pending_access=pending_access,
            total_events=total_events,
            recent_events=recent_events
        )

    # ---------------- STUDENT ----------------
    elif role == "student":
        today = datetime.now().date()

        ongoing_events = []
        upcoming_events_list = []
        past_events = []

        for doc in events:
            e = doc.to_dict()
            e["id"] = doc.id

            try:
                event_date = datetime.strptime(e.get("date"), "%Y-%m-%d").date()
            except Exception:
                continue

            if event_date == today:
                ongoing_events.append(e)
            elif event_date > today:
                upcoming_events_list.append(e)
            else:
                past_events.append(e)

        # ---------------- STUDENT REGISTRATIONS ----------------
        my_events = []

        registrations = db.collection("registrations") \
            .where("user_id", "==", session["uid"]) \
            .stream()

        for reg in registrations:
            r = reg.to_dict()
            event_doc = db.collection("events").document(r["event_id"]).get()
            if event_doc.exists:
                e = event_doc.to_dict()
                e["id"] = event_doc.id
                e["registered_at"] = r.get("registered_at")
                my_events.append(e)


        return render_template(
            "student.html",
            username=username,
            ongoing_events=ongoing_events,
            upcoming_events_list=upcoming_events_list,
            past_events=past_events,
            upcoming_events=len(upcoming_events_list),
            my_events=my_events
        )


    abort(403)

# ------------------------
# Admin: Users & Events
# ------------------------
@app.route("/admin/users")
def admin_users():
    if "uid" not in session:
        abort(403)

    admin = get_user(session["uid"])
    if not admin or admin.get("role") != "admin":
        abort(403)

    users = []
    for doc in db.collection("users").order_by("email").stream():
        u = doc.to_dict()
        u["uid"] = doc.id
        users.append(u)

    return render_template("manage_users.html", users=users)

@app.route("/admin/approve/<uid>", methods=["POST"])
def approve_user(uid):
    db.collection("users").document(uid).update({"approved": True})
    return redirect("/admin/users")


@app.route("/admin/event/<event_id>/participants")
def view_participants(event_id):
    if "uid" not in session:
        abort(403)

    admin = get_user(session["uid"])
    if not admin or admin.get("role") != "admin":
        abort(403)

    event_doc = db.collection("events").document(event_id).get()
    if not event_doc.exists:
        abort(404)

    participants = []
    regs = db.collection("registrations") \
        .where("event_id", "==", event_id) \
        .stream()

    for r in regs:
        p = r.to_dict()
        user_doc = db.collection("users").document(p["user_id"]).get()
        if user_doc.exists:
            p["email"] = user_doc.to_dict().get("email")
        participants.append(p)

    return render_template(
        "event_participants.html",
        event=event_doc.to_dict(),
        participants=participants
    )

@app.route("/admin/recent-events")
def recent_events_page():
    if "uid" not in session:
        abort(403)

    admin = get_user(session["uid"])
    if not admin or admin.get("role") != "admin":
        abort(403)

    events = []
    docs = db.collection("events") \
        .order_by("created_at", direction=firestore.Query.DESCENDING) \
        .limit(20) \
        .stream()

    for doc in docs:
        e = doc.to_dict()
        e["id"] = doc.id
        events.append(e)

    return render_template("recent_events.html", events=events)


@app.route("/admin/events/edit/<event_id>", methods=["GET", "POST"])
def edit_event(event_id):
    if "uid" not in session:
        abort(403)

    admin = get_user(session["uid"])
    if not admin or admin.get("role") != "admin":
        abort(403)

    event_ref = db.collection("events").document(event_id)
    event_doc = event_ref.get()

    if not event_doc.exists:
        abort(404)

    event = event_doc.to_dict()

    if request.method == "POST":
        event_ref.update({
            "title": request.form["title"],
            "description": request.form.get("description"),
            "date": request.form["date"],
            "time": request.form["time"],
            "venue": request.form["venue"],
            "updated_at": datetime.utcnow()
        })

        get_dashboard_data.cache_clear()
        return redirect("/admin/events")

    return render_template(
        "edit_event.html",
        event=event,
        event_id=event_id
    )


@app.route("/admin/promote/<uid>", methods=["POST"])
def promote_user(uid):
    if uid == session.get("uid"):
        abort(400)
    db.collection("users").document(uid).update({"role": "admin"})
    return redirect("/admin/users")

@app.route("/admin/create-event", methods=["GET", "POST"])
def create_event():
    if request.method == "POST":
        db.collection("events").add({
            "title": request.form["title"],
            "description": request.form.get("description"),
            "date": request.form["date"],
            "time": request.form["time"],
            "venue": request.form["venue"],
            "created_at": datetime.utcnow()
        })
        get_dashboard_data.cache_clear()
        return redirect("/dashboard")

    return render_template("create_event.html")

@app.route("/admin/events")
def manage_events():
    events = []
    for doc in db.collection("events").order_by("created_at", direction=firestore.Query.DESCENDING).stream():
        e = doc.to_dict()
        e["id"] = doc.id
        events.append(e)
    return render_template("manage_events.html", events=events)

@app.route("/admin/events/delete/<event_id>", methods=["POST"])
def delete_event(event_id):
    db.collection("events").document(event_id).delete()
    get_dashboard_data.cache_clear()
    return redirect("/admin/events")

# ------------------------
# Event Registration
# ------------------------
@app.route("/register/<event_id>", methods=["GET", "POST"])
def register_event(event_id):
    if "uid" not in session:
        abort(403)

    uid = session["uid"]

    existing = db.collection("registrations") \
        .where("event_id", "==", event_id) \
        .where("user_id", "==", uid) \
        .limit(1).stream()

    if any(existing):
        return """
        <script>
          alert("You are already registered for this event.");
          window.location.href = "/dashboard";
        </script>
        """

    if request.method == "POST":
        db.collection("registrations").add({
            "event_id": event_id,
            "user_id": uid,
            "registered_at": datetime.utcnow(),
            **request.form
        })
        return """
        <script>
          alert("Registration successful!");
          window.location.href = "/dashboard";
        </script>
        """

    event = db.collection("events").document(event_id).get().to_dict()
    return render_template("event_register.html", event=event)

@app.route("/event/<event_id>")
def event_details(event_id):
    if "uid" not in session:
        return redirect("/login")

    event_doc = db.collection("events").document(event_id).get()
    if not event_doc.exists:
        abort(404)

    event = event_doc.to_dict()
    event["id"] = event_id

    # Determine event status
    today = datetime.now().date()
    try:
        event_date = datetime.strptime(event.get("date"), "%Y-%m-%d").date()
    except Exception:
        event_date = None

    if event_date == today:
        status = "ongoing"
    elif event_date and event_date > today:
        status = "upcoming"
    else:
        status = "past"

    return render_template(
        "event_details.html",
        event=event,
        status=status
    )


# ------------------------
# Logout
# ------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ------------------------
# Run App
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
