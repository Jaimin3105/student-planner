import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET_KEY",
    "smart-study-planner-secret-key",
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DATABASE = "planner.db"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_PRIORITIES = {"High", "Medium", "Low"}
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
PRIORITY_HOUR_BONUS = {"High": 2, "Medium": 1, "Low": 0}
PRIORITY_DAY_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}
DEFAULT_STUDY_HOURS_PER_DAY = 4
MIN_STUDY_HOURS_PER_DAY = 1
MAX_STUDY_HOURS_PER_DAY = 8


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_table_columns(conn, table_name):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def normalize_email(email):
    return email.strip().lower()


def is_valid_email(email):
    return EMAIL_PATTERN.fullmatch(email) is not None


def is_password_hashed(password):
    return password.startswith(("pbkdf2:", "scrypt:"))


def is_valid_date(date_text):
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def create_tasks_table(conn, table_name="tasks"):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending',
            completed_at TEXT,
            repeat_daily INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )


def create_notifications_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
        )
        """
    )


def migrate_users_table(conn):
    conn.execute(
        """
        UPDATE users
        SET email = LOWER(TRIM(email))
        WHERE email IS NOT NULL
        """
    )

    users = conn.execute("SELECT id, password FROM users").fetchall()
    for user in users:
        if user["password"] and not is_password_hashed(user["password"]):
            conn.execute(
                "UPDATE users SET password = ? WHERE id = ?",
                (generate_password_hash(user["password"]), user["id"]),
            )

    duplicate_email = conn.execute(
        """
        SELECT LOWER(email) AS email_key
        FROM users
        GROUP BY LOWER(email)
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()

    if duplicate_email is None:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
            ON users(email COLLATE NOCASE)
            """
        )


def tasks_table_needs_rebuild(conn):
    task_columns = {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    user_id_column = task_columns.get("user_id")
    foreign_keys = conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
    has_user_foreign_key = any(
        row["from"] == "user_id" and row["table"] == "users"
        for row in foreign_keys
    )

    return user_id_column is None or user_id_column["notnull"] == 0 or not has_user_foreign_key


def rebuild_tasks_table(conn):
    invalid_tasks = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM tasks
        WHERE user_id IS NULL
        OR user_id NOT IN (SELECT id FROM users)
        """
    ).fetchone()["count"]

    if invalid_tasks > 0:
        return

    conn.execute("DROP TABLE IF EXISTS tasks_new")
    create_tasks_table(conn, "tasks_new")
    conn.execute(
        """
        INSERT INTO tasks_new (id, user_id, task_name, subject, deadline, status, completed_at)
        SELECT
            id,
            user_id,
            task_name,
            subject,
            deadline,
            COALESCE(NULLIF(status, ''), 'Pending'),
            completed_at
        FROM tasks
        """
    )
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")


def migrate_tasks_table(conn):
    task_columns = get_table_columns(conn, "tasks")

    if "user_id" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER")

    if "completed_at" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")

    if "repeat_daily" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN repeat_daily INTEGER DEFAULT 0")

    conn.execute(
        """
        UPDATE tasks
        SET user_id = (
            SELECT subjects.user_id
            FROM subjects
            WHERE subjects.name = tasks.subject
            LIMIT 1
        )
        WHERE user_id IS NULL
        AND (
            SELECT COUNT(*)
            FROM subjects
            WHERE subjects.name = tasks.subject
        ) = 1
        """
    )

    user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if user_count == 1:
        only_user_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE tasks SET user_id = ? WHERE user_id IS NULL",
            (only_user_id,),
        )

    if tasks_table_needs_rebuild(conn):
        rebuild_tasks_table(conn)


def create_indexes(conn):
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subjects_user_id
        ON subjects(user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_user_id
        ON tasks(user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notifications_user_id
        ON notifications(user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notifications_is_read
        ON notifications(is_read)
        """
    )


def create_notification(conn, user_id, task_id, message, notification_type="task_created"):
    """Create a notification for the user"""
    conn.execute(
        """
        INSERT INTO notifications (user_id, task_id, message, notification_type)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, task_id, message, notification_type),
    )
    conn.commit()


def get_task_statistics(conn, user_id):
    summary = conn.execute(
        """
        SELECT
            COUNT(*) AS total_tasks,
            SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS completed_tasks,
            SUM(CASE WHEN status != 'Completed' THEN 1 ELSE 0 END) AS pending_tasks
        FROM tasks
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    return {
        "total": summary["total_tasks"] or 0,
        "completed": summary["completed_tasks"] or 0,
        "pending": summary["pending_tasks"] or 0,
    }


def get_weekly_progress(conn, user_id):
    """Get task completion data for the past 7 days"""
    today = date.today()
    week_ago = today - timedelta(days=6)
    
    weekly_data = conn.execute(
        """
        SELECT
            DATE(completed_at) AS completion_date,
            COUNT(*) AS tasks_completed
        FROM tasks
        WHERE user_id = ?
        AND status = 'Completed'
        AND completed_at IS NOT NULL
        AND DATE(completed_at) >= ?
        GROUP BY DATE(completed_at)
        ORDER BY completion_date ASC
        """,
        (user_id, week_ago.isoformat()),
    ).fetchall()
    
    # Build a dictionary with all 7 days
    week_dict = {}
    for i in range(7):
        day = week_ago + timedelta(days=i)
        week_dict[day.isoformat()] = 0
    
    # Fill in completed tasks
    for row in weekly_data:
        week_dict[row["completion_date"]] = row["tasks_completed"]
    
    days = []
    values = []
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    for i in range(7):
        day = week_ago + timedelta(days=i)
        days.append(day_names[i])
        values.append(week_dict[day.isoformat()])
    
    total_weekly = sum(values)
    
    return {
        "labels": days,
        "values": values,
        "total": total_weekly,
    }


def get_monthly_progress(conn, user_id):
    """Get task completion data for the past 30 days"""
    today = date.today()
    month_ago = today - timedelta(days=29)
    
    monthly_data = conn.execute(
        """
        SELECT
            DATE(completed_at) AS completion_date,
            COUNT(*) AS tasks_completed
        FROM tasks
        WHERE user_id = ?
        AND status = 'Completed'
        AND completed_at IS NOT NULL
        AND DATE(completed_at) >= ?
        GROUP BY DATE(completed_at)
        ORDER BY completion_date ASC
        """,
        (user_id, month_ago.isoformat()),
    ).fetchall()
    
    # Build a dictionary with all 30 days
    month_dict = {}
    for i in range(30):
        day = month_ago + timedelta(days=i)
        month_dict[day.isoformat()] = 0
    
    # Fill in completed tasks
    for row in monthly_data:
        month_dict[row["completion_date"]] = row["tasks_completed"]
    
    days = []
    values = []
    
    for i in range(30):
        day = month_ago + timedelta(days=i)
        days.append(day.strftime("%m-%d"))
        values.append(month_dict[day.isoformat()])
    
    total_monthly = sum(values)
    avg_daily = round(total_monthly / 30, 1) if total_monthly > 0 else 0
    
    return {
        "labels": days,
        "values": values,
        "total": total_monthly,
        "avg_daily": avg_daily,
    }


def get_deadline_note(days_left):
    if days_left < 0:
        return "Overdue"
    if days_left == 0:
        return "Due today"
    if days_left == 1:
        return "Due tomorrow"
    return f"Due in {days_left} days"


def get_pending_tasks_for_schedule(conn, user_id):
    return conn.execute(
        """
        SELECT
            tasks.id,
            tasks.task_name,
            tasks.subject,
            tasks.deadline,
            tasks.status,
            COALESCE(subjects.priority, 'Low') AS priority
        FROM tasks
        LEFT JOIN subjects
            ON subjects.user_id = tasks.user_id
            AND subjects.name = tasks.subject
        WHERE tasks.user_id = ?
        AND tasks.status = 'Pending'
        ORDER BY
            tasks.deadline ASC,
            CASE COALESCE(subjects.priority, 'Low')
                WHEN 'High' THEN 0
                WHEN 'Medium' THEN 1
                ELSE 2
            END,
            tasks.id ASC
        """,
        (user_id,),
    ).fetchall()


def calculate_recommended_hours(deadline_date, priority):
    days_left = (deadline_date - date.today()).days

    if days_left <= 1:
        hours = 3
    elif days_left <= 3:
        hours = 2
    else:
        hours = 1

    hours += PRIORITY_HOUR_BONUS.get(priority, 0)

    return hours, days_left


def calculate_day_schedule_hours(selected_date, deadline_date, priority):
    days_left = (deadline_date - selected_date).days

    if days_left <= 0:
        hours = 3
    elif days_left <= 2:
        hours = 2
    else:
        hours = 1

    hours += PRIORITY_HOUR_BONUS.get(priority, 0)

    return hours, days_left


def generate_study_schedule(task_rows, daily_hours, selected_date):
    prepared_tasks = []

    for row in task_rows:
        priority = row["priority"] if row["priority"] in ALLOWED_PRIORITIES else "Low"

        if is_valid_date(row["deadline"]):
            deadline_date = datetime.strptime(row["deadline"], "%Y-%m-%d").date()
        else:
            deadline_date = date.today()

        recommended_hours, days_left = calculate_day_schedule_hours(selected_date, deadline_date, priority)
        prepared_tasks.append(
            {
                "id": row["id"],
                "task_name": row["task_name"],
                "subject": row["subject"],
                "deadline": row["deadline"],
                "deadline_date": deadline_date,
                "priority": priority,
                "priority_class": priority.lower(),
                "days_left": days_left,
                "deadline_note": get_deadline_note(days_left),
                "recommended_hours": recommended_hours,
                "is_urgent": days_left <= 1,
            }
        )

    if not prepared_tasks:
        return {
            "days": [],
            "task_count": 0,
            "total_hours": 0,
            "urgent_count": 0,
            "high_priority_count": 0,
            "planned_hours": 0,
        }

    prepared_tasks.sort(
        key=lambda task: (
            task["deadline_date"],
            PRIORITY_ORDER.get(task["priority"], 2),
            task["task_name"].lower(),
        )
    )

    day_plan = {
        "day_name": selected_date.strftime("%A"),
        "date_label": selected_date.strftime("%d %b %Y"),
        "entries": [],
        "total_hours": 0,
    }

    # Convert daily hours to minutes for more granular allocation
    total_minutes = daily_hours * 60
    
    # Calculate equal time allocation for all pending tasks
    minutes_per_task = total_minutes // len(prepared_tasks) if prepared_tasks else 0
    remaining_minutes = total_minutes % len(prepared_tasks)
    
    # Allocate time to each task
    for idx, task in enumerate(prepared_tasks):
        # Distribute extra minutes to first few tasks
        task_minutes = minutes_per_task + (1 if idx < remaining_minutes else 0)
        
        if task_minutes > 0:
            # Convert minutes back to hours and minutes for display
            hours = task_minutes // 60
            minutes = task_minutes % 60
            
            day_plan["entries"].append(
                {
                    "task_id": task["id"],
                    "task_name": task["task_name"],
                    "subject": task["subject"],
                    "deadline": task["deadline"],
                    "deadline_note": task["deadline_note"],
                    "priority": task["priority"],
                    "priority_class": task["priority_class"],
                    "is_urgent": task["is_urgent"],
                    "hours": hours,
                    "minutes": minutes,
                    "total_minutes": task_minutes,
                    "sort_deadline": task["deadline_date"],
                }
            )
            day_plan["total_hours"] += hours
    
    day_plan["entries"].sort(
        key=lambda entry: (
            entry["sort_deadline"],
            PRIORITY_ORDER.get(entry["priority"], 2),
            entry["task_name"].lower(),
        )
    )

    visible_days = [day_plan] if day_plan["entries"] else []

    return {
        "days": visible_days,
        "task_count": len(prepared_tasks),
        "total_hours": day_plan["total_hours"],
        "urgent_count": sum(1 for task in prepared_tasks if task["is_urgent"]),
        "high_priority_count": sum(1 for task in prepared_tasks if task["priority"] == "High"),
        "planned_hours": day_plan["total_hours"],
    }


def get_selected_daily_hours():
    requested_hours = request.args.get("hours", type=int)

    if requested_hours is not None:
        if MIN_STUDY_HOURS_PER_DAY <= requested_hours <= MAX_STUDY_HOURS_PER_DAY:
            session["daily_study_hours"] = requested_hours
            return requested_hours

    saved_hours = session.get("daily_study_hours", DEFAULT_STUDY_HOURS_PER_DAY)
    if isinstance(saved_hours, int) and MIN_STUDY_HOURS_PER_DAY <= saved_hours <= MAX_STUDY_HOURS_PER_DAY:
        return saved_hours

    return DEFAULT_STUDY_HOURS_PER_DAY


def get_selected_plan_date():
    requested_date = request.args.get("plan_date")

    if requested_date and is_valid_date(requested_date):
        session["selected_plan_date"] = requested_date
        return datetime.strptime(requested_date, "%Y-%m-%d").date()

    saved_date = session.get("selected_plan_date")
    if saved_date and is_valid_date(saved_date):
        return datetime.strptime(saved_date, "%Y-%m-%d").date()

    today = date.today()
    session["selected_plan_date"] = today.isoformat()
    return today


def reset_repeating_tasks(conn, user_id):
    """Reset daily repeating tasks if they were completed more than 24 hours ago"""
    now = datetime.now()
    
    # Find completed recurring tasks
    completed_recurring_tasks = conn.execute(
        """
        SELECT id, completed_at
        FROM tasks
        WHERE user_id = ? 
        AND status = 'Completed' 
        AND repeat_daily = 1
        AND completed_at IS NOT NULL
        """,
        (user_id,),
    ).fetchall()
    
    for task in completed_recurring_tasks:
        if task["completed_at"]:
            try:
                completed_time = datetime.strptime(task["completed_at"], "%Y-%m-%d %H:%M:%S")
                time_diff = now - completed_time
                
                # If more than 24 hours have passed, reset the task
                if time_diff.total_seconds() > 86400:  # 86400 seconds = 24 hours
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'Pending', completed_at = NULL
                        WHERE id = ?
                        """,
                        (task["id"],),
                    )
            except ValueError:
                pass
    
    conn.commit()


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            password TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            priority TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
        """
    )

    create_tasks_table(conn)
    create_notifications_table(conn)

    migrate_users_table(conn)
    migrate_tasks_table(conn)
    create_indexes(conn)
    conn.commit()
    conn.close()


def login_required(route_function):
    @wraps(route_function)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return route_function(*args, **kwargs)

    return wrapper


init_db()


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db_connection()
    user_id = session["user_id"]

    # Reset repeating tasks if they've been completed for more than 24 hours
    reset_repeating_tasks(conn, user_id)

    total_subjects = conn.execute(
        "SELECT COUNT(*) AS count FROM subjects WHERE user_id = ?",
        (user_id,),
    ).fetchone()["count"]

    recent_subjects = conn.execute(
        """
        SELECT id, name, priority
        FROM subjects
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (user_id,),
    ).fetchall()

    recent_tasks = conn.execute(
        """
        SELECT id, task_name, subject, deadline, status
        FROM tasks
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (user_id,),
    ).fetchall()

    task_stats = get_task_statistics(conn, user_id)
    weekly_progress = get_weekly_progress(conn, user_id)
    monthly_progress = get_monthly_progress(conn, user_id)
    conn.close()

    return render_template(
        "dashboard.html",
        total_subjects=total_subjects,
        task_stats=task_stats,
        recent_subjects=recent_subjects,
        recent_tasks=recent_tasks,
        weekly_progress=weekly_progress,
        monthly_progress=monthly_progress,
        task_chart_data={
            "labels": ["Completed", "Pending"],
            "values": [
                task_stats["completed"],
                task_stats["pending"],
            ],
        },
        user_name=session.get("user_name", "Student"),
    )


@app.route("/schedule")
@login_required
def schedule():
    daily_hours = get_selected_daily_hours()
    selected_plan_date = get_selected_plan_date()
    conn = get_db_connection()
    user_id = session["user_id"]
    
    # Reset repeating tasks if they've been completed for more than 24 hours
    reset_repeating_tasks(conn, user_id)
    
    pending_tasks = get_pending_tasks_for_schedule(conn, user_id)
    schedule_plan = generate_study_schedule(pending_tasks, daily_hours, selected_plan_date)
    conn.close()

    return render_template(
        "schedule.html",
        schedule_days=schedule_plan["days"],
        daily_hours=daily_hours,
        selected_plan_date=selected_plan_date.isoformat(),
        schedule_summary=schedule_plan,
        user_name=session.get("user_name", "Student"),
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        name = request.form["name"].strip()
        email = normalize_email(request.form["email"])
        password = request.form["password"]

        if len(name) < 2:
            error = "Name must contain at least 2 characters."
        elif len(name) > 50:
            error = "Name must be 50 characters or less."
        elif not is_valid_email(email):
            error = "Please enter a valid email address."
        elif len(password) < 6:
            error = "Password must contain at least 6 characters."
        else:
            conn = get_db_connection()
            existing_user = conn.execute(
                "SELECT id FROM users WHERE email = ?",
                (email,),
            ).fetchone()

            if existing_user:
                error = "This email is already registered. Please log in."
            else:
                conn.execute(
                    "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                    (name, email, generate_password_hash(password)),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("login"))

            conn.close()

    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        email = normalize_email(request.form["email"])
        password = request.form["password"]

        if not is_valid_email(email):
            error = "Please enter a valid email address."
        elif not password:
            error = "Password is required."
        else:
            conn = get_db_connection()
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            conn.close()

            if user and check_password_hash(user["password"], password):
                session.clear()
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                return redirect(url_for("dashboard"))

            error = "Invalid email or password."

    return render_template("login.html", error=error)


@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    error = None

    if request.method == "POST":
        name = request.form["name"].strip()
        priority = request.form["priority"].strip()

        if len(name) < 2:
            error = "Subject name must contain at least 2 characters."
        elif len(name) > 50:
            error = "Subject name must be 50 characters or less."
        elif priority not in ALLOWED_PRIORITIES:
            error = "Please choose a valid priority."
        else:
            conn = get_db_connection()
            existing_subject = conn.execute(
                """
                SELECT id
                FROM subjects
                WHERE user_id = ? AND LOWER(name) = LOWER(?)
                """,
                (session["user_id"], name),
            ).fetchone()

            if existing_subject:
                error = "You already added this subject."
            else:
                conn.execute(
                    "INSERT INTO subjects (user_id, name, priority) VALUES (?, ?, ?)",
                    (session["user_id"], name, priority),
                )
                conn.commit()
                conn.close()
                return redirect(url_for("subjects"))

            conn.close()

    conn = get_db_connection()
    subject_list = conn.execute(
        """
        SELECT id, name, priority
        FROM subjects
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    conn.close()

    return render_template(
        "subjects.html",
        subjects=subject_list,
        error=error,
        user_name=session.get("user_name", "Student"),
    )


@app.route("/subjects/delete/<int:subject_id>", methods=["POST"])
@login_required
def delete_subject(subject_id):
    conn = get_db_connection()
    conn.execute(
        "DELETE FROM subjects WHERE id = ? AND user_id = ?",
        (subject_id, session["user_id"]),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("subjects"))


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))






@app.route("/tasks", methods=["GET", "POST"])
@login_required
def tasks():
    error = None
    conn = get_db_connection()
    user_id = session["user_id"]

    # Reset repeating tasks if they've been completed for more than 24 hours
    reset_repeating_tasks(conn, user_id)

    subjects = conn.execute(
        """
        SELECT id, name, priority
        FROM subjects
        WHERE user_id = ?
        ORDER BY name
        """,
        (user_id,),
    ).fetchall()

    if request.method == "POST":
        task_name = request.form["task_name"].strip()
        subject_id = request.form["subject_id"].strip()
        deadline = request.form["deadline"].strip()
        repeat_daily = 1 if request.form.get("repeat_daily") == "on" else 0

        if len(task_name) < 2:
            error = "Task name must contain at least 2 characters."
        elif len(task_name) > 100:
            error = "Task name must be 100 characters or less."
        elif not subject_id or not deadline:
            error = "Please fill in all task details."
        elif not is_valid_date(deadline):
            error = "Please choose a valid deadline date."
        else:
            selected_subject = conn.execute(
                """
                SELECT name
                FROM subjects
                WHERE id = ? AND user_id = ?
                """,
                (subject_id, user_id),
            ).fetchone()

            if selected_subject is None:
                error = "Please choose a valid subject from your account."
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO tasks (user_id, task_name, subject, deadline, status, repeat_daily)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, task_name, selected_subject["name"], deadline, "Pending", repeat_daily),
                )
                task_id = cursor.lastrowid
                
                # Create notification for task creation
                notification_message = f"Task '{task_name}' created for {selected_subject['name']}"
                create_notification(conn, user_id, task_id, notification_message, "task_created")
                
                conn.close()
                return redirect(url_for("tasks"))

    all_tasks = conn.execute(
        """
        SELECT id, task_name, subject, deadline, status, repeat_daily
        FROM tasks
        WHERE user_id = ?
        ORDER BY
            CASE WHEN status = 'Pending' THEN 0 ELSE 1 END,
            deadline ASC,
            id DESC
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    return render_template(
        "tasks.html",
        tasks=all_tasks,
        subjects=subjects,
        error=error,
        user_name=session.get("user_name", "Student"),
    )

@app.route("/complete_task/<int:id>", methods=["POST"])
@login_required
def complete_task(id):
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'Completed',
            completed_at = datetime('now', 'localtime')
        WHERE id = ? AND user_id = ?
        """,
        (id, session["user_id"]),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("tasks"))

@app.route("/incomplete_task/<int:id>", methods=["POST"])
@login_required
def incomplete_task(id):
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'Pending',
            completed_at = NULL
        WHERE id = ? AND user_id = ?
        """,
        (id, session["user_id"]),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("tasks"))

@app.route("/delete_task/<int:id>", methods=["POST"])
@login_required
def delete_task(id):
    conn = get_db_connection()
    conn.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?",
        (id, session["user_id"]),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("tasks"))


@app.route("/notifications")
@login_required
def get_notifications():
    """Get unread notifications for the current user"""
    conn = get_db_connection()
    user_id = session["user_id"]
    
    notifications = conn.execute(
        """
        SELECT id, message, notification_type, created_at, is_read
        FROM notifications
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (user_id,),
    ).fetchall()
    
    conn.close()
    
    return jsonify(
        notifications=[
            {
                "id": n["id"],
                "message": n["message"],
                "type": n["notification_type"],
                "created_at": n["created_at"],
                "is_read": n["is_read"],
            }
            for n in notifications
        ]
    )


@app.route("/notifications/unread_count")
@login_required
def unread_notification_count():
    """Get count of unread notifications"""
    conn = get_db_connection()
    user_id = session["user_id"]
    
    count = conn.execute(
        "SELECT COUNT(*) as count FROM notifications WHERE user_id = ? AND is_read = 0",
        (user_id,),
    ).fetchone()["count"]
    
    conn.close()
    
    return jsonify(unread_count=count)


@app.route("/notifications/mark_as_read/<int:notification_id>", methods=["POST"])
@login_required
def mark_notification_read(notification_id):
    """Mark a notification as read"""
    conn = get_db_connection()
    user_id = session["user_id"]
    
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    conn.commit()
    conn.close()
    
    return jsonify(status="success")


if __name__ == "__main__":
    app.run(debug=True)
