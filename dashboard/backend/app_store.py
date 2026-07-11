import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "app.db"
TRIAL_DAYS = 30
SESSION_DAYS = 1
try:
    SESSION_HOURS = max(1, int(os.getenv("CIPHERFX_SESSION_HOURS", "24") or "24"))
except ValueError:
    SESSION_HOURS = 24

PLAN_DEFS = {
    "basic": {
        "name": "Basic",
        "price_monthly": 299,
        "currency": "ZAR",
        "trial_days": TRIAL_DAYS,
        "features": [
            "30-day paper trading trial",
            "Cipher FX dashboard access",
            "Paper bot monitoring",
            "Email support",
        ],
    },
    "pro": {
        "name": "Pro",
        "price_monthly": 599,
        "currency": "ZAR",
        "trial_days": TRIAL_DAYS,
        "features": [
            "Everything in Basic",
            "Advanced analytics",
            "Priority onboarding",
            "Deeper risk profile setup",
        ],
    },
    "ultimate": {
        "name": "Ultimate",
        "price_monthly": 999,
        "currency": "ZAR",
        "trial_days": TRIAL_DAYS,
        "features": [
            "Everything in Pro",
            "White-glove setup",
            "Strategy review workflow",
            "Highest priority support",
        ],
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def user_count() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return int(row["c"] or 0)
    finally:
        conn.close()


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        150_000,
    ).hex()
    return salt, digest


def verify_password(password: str, salt: str, password_hash: str) -> bool:
    _, digest = hash_password(password, salt=salt)
    return secrets.compare_digest(digest, password_hash)


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            full_name TEXT NOT NULL,
            company TEXT,
            ibkr_account_type TEXT DEFAULT 'paper',
            onboarding_status TEXT DEFAULT 'pending',
            risk_profile TEXT,
            experience_level TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            goals TEXT,
            trading_experience TEXT,
            markets TEXT,
            time_horizon TEXT,
            risk_tolerance TEXT,
            starting_capital TEXT,
            paper_trading_ready INTEGER DEFAULT 1,
            answers_json TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_entitlements (
            user_id INTEGER PRIMARY KEY,
            selected_plan TEXT NOT NULL,
            trial_started_at TEXT,
            trial_ends_at TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'trial',
            subscription_started_at TEXT,
            subscription_ends_at TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS app_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ibkr_credentials (
            user_id INTEGER PRIMARY KEY,
            login_id_enc BLOB NOT NULL,
            password_enc BLOB NOT NULL,
            account_id_enc BLOB,
            gateway_host_enc BLOB,
            gateway_port_enc BLOB,
            trading_mode_enc BLOB,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    conn.close()


def list_plans():
    return [
        {"code": code, **details}
        for code, details in PLAN_DEFS.items()
    ]


def create_user(email: str, password: str, full_name: str, company: str | None):
    now = _utcnow()
    salt, password_hash = hash_password(password)
    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO users (
                email, password_hash, password_salt, full_name, company, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (email.strip().lower(), password_hash, salt, full_name.strip(), company or None, _to_iso(now), _to_iso(now)),
        )
        user_id = cur.lastrowid
        trial_ends_at = now + timedelta(days=TRIAL_DAYS)
        conn.execute(
            """
            INSERT INTO user_entitlements (
                user_id, selected_plan, trial_started_at, trial_ends_at, subscription_status, updated_at
            ) VALUES (?, ?, ?, ?, 'trial', ?)
            """,
            (user_id, "", _to_iso(now), _to_iso(trial_ends_at), _to_iso(now)),
        )
        conn.execute(
            """
            INSERT INTO user_profiles (
                user_id, answers_json, updated_at
            ) VALUES (?, ?, ?)
            """,
            (user_id, "{}", _to_iso(now)),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Email already exists") from exc
    finally:
        conn.close()
    return get_user_by_email(email)


def get_user_by_email(email: str):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT u.*, e.selected_plan, e.trial_started_at, e.trial_ends_at, e.subscription_status,
               e.subscription_started_at, e.subscription_ends_at, e.stripe_customer_id, e.stripe_subscription_id
        FROM users u
        LEFT JOIN user_entitlements e ON e.user_id = u.id
        WHERE u.email = ?
        """,
        (email.strip().lower(),),
    ).fetchone()
    conn.close()
    return row


def get_user_by_id(user_id: int):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT u.*, e.selected_plan, e.trial_started_at, e.trial_ends_at, e.subscription_status,
               e.subscription_started_at, e.subscription_ends_at, e.stripe_customer_id, e.stripe_subscription_id
        FROM users u
        LEFT JOIN user_entitlements e ON e.user_id = u.id
        WHERE u.id = ?
        """,
        (user_id,),
    ).fetchone()
    profile = conn.execute(
        "SELECT * FROM user_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row, profile


def authenticate_user(email: str, password: str):
    row = get_user_by_email(email)
    if row is None:
        return None
    if not verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return row


def create_session(user_id: int) -> str:
    now = _utcnow()
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO app_sessions (token, user_id, expires_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, user_id, _to_iso(now + timedelta(hours=SESSION_HOURS)), _to_iso(now)),
    )
    conn.commit()
    conn.close()
    return token


def ensure_configured_dashboard_user():
    email = os.getenv("CIPHERFX_DASHBOARD_EMAIL", "").strip().lower()
    password_hash = os.getenv("CIPHERFX_DASHBOARD_PASSWORD_HASH", "").strip()
    password_salt = os.getenv("CIPHERFX_DASHBOARD_PASSWORD_SALT", "").strip()
    if not email or not password_hash or not password_salt:
        return None

    full_name = os.getenv("CIPHERFX_DASHBOARD_FULL_NAME", "Calvin Brink").strip() or "Calvin Brink"
    company = os.getenv("CIPHERFX_DASHBOARD_COMPANY", "Cipher FX").strip() or "Cipher FX"
    access_until = os.getenv("CIPHERFX_DASHBOARD_ACCESS_UNTIL", "").strip()
    now = _utcnow()
    access_until_dt = _from_iso(access_until) or (now + timedelta(days=3650))

    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO users (
                    email, password_hash, password_salt, full_name, company,
                    onboarding_status, ibkr_account_type, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'complete', 'paper', ?, ?)
                """,
                (email, password_hash, password_salt, full_name, company, _to_iso(now), _to_iso(now)),
            )
            user_id = cur.lastrowid
        else:
            user_id = row["id"]
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, password_salt = ?, full_name = ?, company = ?,
                    onboarding_status = 'complete', updated_at = ?
                WHERE id = ?
                """,
                (password_hash, password_salt, full_name, company, _to_iso(now), user_id),
            )

        conn.execute(
            """
            INSERT INTO user_entitlements (
                user_id, selected_plan, trial_started_at, trial_ends_at,
                subscription_status, subscription_started_at, subscription_ends_at, updated_at
            ) VALUES (?, 'pro', ?, ?, 'active', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                selected_plan='pro',
                subscription_status='active',
                subscription_started_at=excluded.subscription_started_at,
                subscription_ends_at=excluded.subscription_ends_at,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                _to_iso(now),
                _to_iso(access_until_dt),
                _to_iso(now),
                _to_iso(access_until_dt),
                _to_iso(now),
            ),
        )
        conn.execute(
            """
            INSERT INTO user_profiles (
                user_id, goals, trading_experience, markets, time_horizon,
                risk_tolerance, starting_capital, paper_trading_ready, answers_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                paper_trading_ready=1,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                "Managed Cipher FX dashboard access",
                "advanced",
                "MT5, IBKR, forex, indices, equities",
                "intraday",
                "controlled",
                "$5,000",
                "{}",
                _to_iso(now),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return get_user_by_email(email)


def delete_session(token: str):
    conn = get_conn()
    conn.execute("DELETE FROM app_sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def get_session_user(token: str):
    now = _utcnow()
    conn = get_conn()
    row = conn.execute(
        """
        SELECT s.user_id, s.expires_at
        FROM app_sessions s
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    expires_at = _from_iso(row["expires_at"])
    if expires_at is None or expires_at <= now:
        conn.execute("DELETE FROM app_sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None
    user_row = conn.execute(
        """
        SELECT u.*, e.selected_plan, e.trial_started_at, e.trial_ends_at, e.subscription_status,
               e.subscription_started_at, e.subscription_ends_at, e.stripe_customer_id, e.stripe_subscription_id
        FROM users u
        LEFT JOIN user_entitlements e ON e.user_id = u.id
        WHERE u.id = ?
        """,
        (row["user_id"],),
    ).fetchone()
    profile = conn.execute(
        "SELECT * FROM user_profiles WHERE user_id = ?",
        (row["user_id"],),
    ).fetchone()
    conn.close()
    return user_row, profile


def save_onboarding(user_id: int, payload: dict):
    now = _utcnow()
    answers = payload.get("answers") or {}
    conn = get_conn()
    conn.execute(
        """
        UPDATE user_profiles
        SET goals = ?, trading_experience = ?, markets = ?, time_horizon = ?,
            risk_tolerance = ?, starting_capital = ?, paper_trading_ready = ?,
            answers_json = ?, updated_at = ?
        WHERE user_id = ?
        """,
        (
            payload.get("goals"),
            payload.get("trading_experience"),
            payload.get("markets"),
            payload.get("time_horizon"),
            payload.get("risk_tolerance"),
            payload.get("starting_capital"),
            1 if payload.get("paper_trading_ready", True) else 0,
            json.dumps(answers),
            _to_iso(now),
            user_id,
        ),
    )
    conn.execute(
        """
        UPDATE users
        SET onboarding_status = 'complete',
            risk_profile = ?,
            experience_level = ?,
            ibkr_account_type = 'paper',
            updated_at = ?
        WHERE id = ?
        """,
        (
            payload.get("risk_tolerance"),
            payload.get("trading_experience"),
            _to_iso(now),
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def activate_subscription(user_id: int, plan_code: str):
    now = _utcnow()
    conn = get_conn()
    conn.execute(
        """
        UPDATE user_entitlements
        SET selected_plan = ?, subscription_status = 'active',
            subscription_started_at = ?, subscription_ends_at = ?,
            updated_at = ?
        WHERE user_id = ?
        """,
        (
            plan_code.lower(),
            _to_iso(now),
            _to_iso(now + timedelta(days=30)),
            _to_iso(now),
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def save_ibkr_credentials(
    user_id: int,
    *,
    login_id_enc: bytes,
    password_enc: bytes,
    account_id_enc: bytes | None = None,
    gateway_host_enc: bytes | None = None,
    gateway_port_enc: bytes | None = None,
    trading_mode_enc: bytes | None = None,
):
    now = _utcnow()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO ibkr_credentials (
            user_id, login_id_enc, password_enc, account_id_enc,
            gateway_host_enc, gateway_port_enc, trading_mode_enc, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            login_id_enc=excluded.login_id_enc,
            password_enc=excluded.password_enc,
            account_id_enc=excluded.account_id_enc,
            gateway_host_enc=excluded.gateway_host_enc,
            gateway_port_enc=excluded.gateway_port_enc,
            trading_mode_enc=excluded.trading_mode_enc,
            updated_at=excluded.updated_at
        """,
        (
            user_id,
            login_id_enc,
            password_enc,
            account_id_enc,
            gateway_host_enc,
            gateway_port_enc,
            trading_mode_enc,
            _to_iso(now),
        ),
    )
    conn.commit()
    conn.close()


def get_ibkr_credential_status(user_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT updated_at FROM ibkr_credentials WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return {"saved": False, "updated_at": None}
    return {"saved": True, "updated_at": row["updated_at"]}


def serialize_user(user_row, profile_row=None):
    if user_row is None:
        return None
    now = _utcnow()
    trial_ends_at = _from_iso(user_row["trial_ends_at"])
    subscription_ends_at = _from_iso(user_row["subscription_ends_at"])
    status = user_row["subscription_status"] or "trial"
    trial_active = bool(trial_ends_at and trial_ends_at > now)
    subscription_active = status == "active" and subscription_ends_at and subscription_ends_at > now
    access_granted = trial_active or bool(subscription_active)
    onboarding_answers = {}
    if profile_row and profile_row["answers_json"]:
        try:
            onboarding_answers = json.loads(profile_row["answers_json"])
        except Exception:
            onboarding_answers = {}
    return {
        "id": user_row["id"],
        "email": user_row["email"],
        "full_name": user_row["full_name"],
        "company": user_row["company"],
        "onboarding_status": user_row["onboarding_status"],
        "ibkr_account_type": user_row["ibkr_account_type"],
        "risk_profile": user_row["risk_profile"],
        "experience_level": user_row["experience_level"],
        "selected_plan": user_row["selected_plan"],
        "trial_started_at": user_row["trial_started_at"],
        "trial_ends_at": user_row["trial_ends_at"],
        "subscription_status": status,
        "subscription_started_at": user_row["subscription_started_at"],
        "subscription_ends_at": user_row["subscription_ends_at"],
        "trial_active": trial_active,
        "subscription_active": bool(subscription_active),
        "access_granted": access_granted,
        "plan": PLAN_DEFS.get((user_row["selected_plan"] or "").lower(), {}),
        "profile": {
            "goals": profile_row["goals"] if profile_row else None,
            "trading_experience": profile_row["trading_experience"] if profile_row else None,
            "markets": profile_row["markets"] if profile_row else None,
            "time_horizon": profile_row["time_horizon"] if profile_row else None,
            "risk_tolerance": profile_row["risk_tolerance"] if profile_row else None,
            "starting_capital": profile_row["starting_capital"] if profile_row else None,
            "paper_trading_ready": bool(profile_row["paper_trading_ready"]) if profile_row else True,
            "answers": onboarding_answers,
        },
        "ibkr_credentials": get_ibkr_credential_status(user_row["id"]),
    }


def ensure_local_bootstrap_user():
    email = os.getenv("CIPHERFX_LOCAL_ADMIN_EMAIL", "admin@mytradebot.co.za").strip().lower() or "admin@mytradebot.co.za"
    full_name = os.getenv("CIPHERFX_LOCAL_ADMIN_NAME", "Super User").strip() or "Super User"
    company = os.getenv("CIPHERFX_LOCAL_ADMIN_COMPANY", "MyTradeBot").strip() or "MyTradeBot"
    now = _utcnow()
    row = get_user_by_email(email)
    if row is None:
        create_user(
            email=email,
            password=secrets.token_urlsafe(24),
            full_name=full_name,
            company=company,
        )
        row = get_user_by_email(email)
    if row is None:
        raise RuntimeError("Could not create local bootstrap user")

    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE users
            SET onboarding_status = 'complete',
                full_name = ?,
                company = ?,
                ibkr_account_type = 'paper',
                updated_at = ?
            WHERE id = ?
            """,
            (full_name, company, _to_iso(now), row["id"]),
        )
        trial_ends_at = now + timedelta(days=TRIAL_DAYS)
        conn.execute(
            """
            INSERT INTO user_entitlements (
                user_id, selected_plan, trial_started_at, trial_ends_at, subscription_status, updated_at
            ) VALUES (?, ?, ?, ?, 'trial', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                trial_started_at=excluded.trial_started_at,
                trial_ends_at=excluded.trial_ends_at,
                updated_at=excluded.updated_at
            """,
            (row["id"], "", _to_iso(now), _to_iso(trial_ends_at), _to_iso(now)),
        )
        conn.execute(
            """
            INSERT INTO user_profiles (
                user_id, goals, trading_experience, markets, time_horizon,
                risk_tolerance, starting_capital, paper_trading_ready, answers_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                paper_trading_ready=excluded.paper_trading_ready,
                updated_at=excluded.updated_at
            """,
            (
                row["id"],
                "Local managed dashboard access",
                "intermediate",
                "US equities, UK equities, forex",
                "daily",
                "balanced",
                "$5,000",
                1,
                "{}",
                _to_iso(now),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if not get_ibkr_credential_status(row["id"])["saved"]:
        save_ibkr_credentials(
            row["id"],
            login_id_enc=b"local-managed",
            password_enc=b"local-managed",
            account_id_enc=b"paper",
            gateway_port_enc=b"4002",
            trading_mode_enc=b"paper",
        )

    return get_user_by_id(row["id"])
