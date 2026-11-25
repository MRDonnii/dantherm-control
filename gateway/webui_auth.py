"""Small local authentication store with salted hashes, roles and server-side sessions.

The store holds several users. Each user has one role:

* ``admin``      – the owner. Everything, including user and mail management.
* ``technician`` – installer/service. Everything technical on the unit
  (advanced settings, diagnostics, sniffer, updates, Pi actions, mail setup),
  but not user management.
* ``user``       – daily operation: mode, level, boost, bypass, afterheat.

Older installations stored a single owner (format version 1). That file is
read transparently as one ``admin`` user and is only rewritten in the new
format the next time something is saved.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

SHORT_SESSION_SECONDS = 12 * 60 * 60
REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60
RESET_TOKEN_SECONDS = 30 * 60

ROLES = ("admin", "technician", "user")
ROLE_LABELS = {"admin": "Administrator", "technician": "Tekniker", "user": "Bruger"}
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")


def _normal(username):
    return str(username or "").strip().lower()


class AuthManager:
    def __init__(self, path="/var/lib/dantherm-hch5-ha/webui-auth.json"):
        self.path = Path(path)
        self.session_path = self.path.with_name("webui-sessions.json")
        self.lock = threading.RLock()
        self.sessions = {}
        self.failures = {}
        self.reset_tokens = {}
        # Set by the web server: decides whether a Kontrolrum session may be
        # used by this request (see kontrolrum_sso.KontrolrumSso.session_problem).
        self.session_guard = None
        # With login switched off the browser still needs a CSRF token; one per
        # process start is handed out through the same-origin /api/auth/status.
        self.open_csrf = secrets.token_urlsafe(24)
        # Only one or two password hashes at a time: each costs ~1 s of CPU on
        # a Pi 3B, and parallel logins must not starve the controller.
        self.hash_slots = threading.BoundedSemaphore(2)
        self._load_sessions()

    # ------------------------------------------------------------------ store
    @property
    def backup_path(self):
        return self.path.with_name(self.path.name + ".bak")

    def unreadable(self):
        """True when the store exists but neither it nor its backup can be read.

        Then setup must stay closed and login fails closed: an unreadable file
        must never look like a fresh installation.
        """
        return self.path.exists() and self.load() is None

    def load(self):
        data = self._parse(self.path)
        if data is None and self.path.exists():
            # A damaged store (e.g. a crash during a write): use the last good copy.
            data = self._parse(self.backup_path)
        return data

    def _parse(self, path):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if "users" not in data and data.get("username"):
            # Format version 1: a single owner.
            data = {
                "version": 2,
                "enabled": bool(data.get("enabled", True)),
                "users": [{
                    "username": str(data["username"]),
                    "role": "admin",
                    "salt": data.get("salt", ""),
                    "password_hash": data.get("password_hash", ""),
                    "email": "",
                    "disabled": False,
                    "expires_at": None,
                }],
            }
        users = data.get("users")
        if not isinstance(users, list) or not users:
            return None
        data["users"] = [user for user in users if isinstance(user, dict) and user.get("username")]
        return data if data["users"] else None

    def _write(self, data):
        data = {"version": 2, "enabled": bool(data.get("enabled", True)), "users": data["users"]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        if self._parse(self.path) is not None:
            # Keep the last good store; load() falls back to it if the new one is damaged.
            try:
                backup_tmp = self.backup_path.with_suffix(".baktmp")
                backup_tmp.write_bytes(self.path.read_bytes())
                os.chmod(backup_tmp, 0o600)
                backup_tmp.replace(self.backup_path)
            except OSError:
                pass
        tmp.replace(self.path)
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass

    def configured(self):
        # An existing but unreadable store counts as configured (fail closed).
        return bool(self.load()) or self.path.exists()

    def enabled(self):
        data = self.load()
        if data is None:
            # Missing: nothing to protect yet (setup). Unreadable: keep login on.
            return self.path.exists()
        return bool(data.get("enabled", True))

    @staticmethod
    def _hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()

    @staticmethod
    def _find(data, username):
        wanted = _normal(username)
        return next((user for user in (data or {}).get("users", []) if _normal(user.get("username")) == wanted), None)

    @staticmethod
    def _active(user, now=None):
        if not user or user.get("disabled"):
            return False
        expires = user.get("expires_at")
        if expires:
            try:
                return float(expires) > (now or time.time())
            except (TypeError, ValueError):
                return False
        return True

    @classmethod
    def _active_admins(cls, data, now=None):
        return [user for user in data["users"] if user.get("role") == "admin" and cls._active(user, now)]

    @staticmethod
    def _check_password(password):
        if len(password or "") < 10:
            raise ValueError("Adgangskoden skal være mindst 10 tegn")

    @staticmethod
    def _check_username(username):
        if not USERNAME_RE.match(username or ""):
            raise ValueError("Brugernavn skal være 3–64 tegn: bogstaver, tal, punktum, bindestreg, _ eller @")

    @staticmethod
    def _clean_email(email):
        email = str(email or "").strip()
        if email and (len(email) > 254 or not EMAIL_RE.match(email)):
            raise ValueError("Ugyldig e-mailadresse")
        return email

    @staticmethod
    def _clean_expiry(value):
        if value in (None, "", 0):
            return None
        try:
            expires = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Ugyldig udløbsdato") from error
        if expires <= time.time():
            raise ValueError("Udløbsdatoen skal ligge i fremtiden")
        return expires

    def _new_secret(self, user, password):
        self._check_password(password)
        salt = secrets.token_hex(16)
        user["salt"] = salt
        user["password_hash"] = self._hash(password, salt)
        user["password_changed_at"] = time.time()

    @staticmethod
    def public_user(user, now=None):
        now = now or time.time()
        expires = user.get("expires_at")
        return {
            "username": user.get("username"),
            "role": user.get("role", "user"),
            "role_label": ROLE_LABELS.get(user.get("role"), "Bruger"),
            "email": user.get("email") or "",
            "disabled": bool(user.get("disabled")),
            "expires_at": expires,
            "expired": bool(expires) and float(expires) <= now,
            "created_at": user.get("created_at"),
            "last_login": user.get("last_login"),
        }

    # ------------------------------------------------------------- accounts
    def verify(self, username, password):
        data = self.load()
        user = self._find(data, username)
        if not user or not user.get("salt"):
            # Spend the same time as a real check so usernames cannot be probed.
            with self.hash_slots:
                self._hash(password or "", "00" * 16)
            return False
        with self.hash_slots:
            digest_ = self._hash(password or "", user["salt"])
        ok = hmac.compare_digest(str(user.get("password_hash", "")), digest_)
        return ok and self._active(user)

    def user(self, username):
        user = self._find(self.load(), username)
        return self.public_user(user) if user else None

    def role(self, username):
        user = self._find(self.load(), username)
        return user.get("role", "user") if user else None

    def active_username(self, username):
        """The account's real name when it exists and may log in, else None."""
        user = self._find(self.load(), username)
        return user["username"] if self._active(user) else None

    def list_users(self):
        data = self.load() or {"users": []}
        now = time.time()
        return [self.public_user(user, now) for user in data["users"]]

    def save(self, username, password, enabled=True):
        """Initial setup: create the first administrator (replaces the store)."""
        username = str(username or "").strip()
        self._check_username(username)
        user = {"username": username, "role": "admin", "email": "", "disabled": False,
                "expires_at": None, "created_at": time.time()}
        self._new_secret(user, password)
        with self.lock:
            self._write({"enabled": bool(enabled), "users": [user]})

    def create_user(self, username, password, role="user", email="", expires_at=None):
        username = str(username or "").strip()
        self._check_username(username)
        if role not in ROLES:
            raise ValueError("Ukendt rolle")
        user = {"username": username, "role": role, "email": self._clean_email(email), "disabled": False,
                "expires_at": self._clean_expiry(expires_at), "created_at": time.time()}
        self._new_secret(user, password)
        with self.lock:
            data = self.load()
            if not data:
                raise ValueError("Opret først administratoren")
            if self._find(data, username):
                raise ValueError("Brugernavnet findes allerede")
            if len(data["users"]) >= 50:
                raise ValueError("Der kan højst være 50 brugere")
            data["users"].append(user)
            self._write(data)
        return self.public_user(user)

    def update_user(self, username, *, role=None, email=None, disabled=None, expires_at=..., password=None):
        with self.lock:
            data = self.load()
            user = self._find(data, username)
            if not user:
                raise KeyError("Brugeren findes ikke")
            if role is not None:
                if role not in ROLES:
                    raise ValueError("Ukendt rolle")
                user["role"] = role
            if email is not None:
                user["email"] = self._clean_email(email)
            if disabled is not None:
                user["disabled"] = bool(disabled)
            if expires_at is not ...:
                user["expires_at"] = self._clean_expiry(expires_at)
            if password:
                self._new_secret(user, password)
            if not self._active_admins(data):
                raise ValueError("Der skal altid være mindst én aktiv administrator")
            self._write(data)
            if password or not self._active(user):
                self.drop_sessions(user["username"])
        return self.public_user(user)

    def delete_user(self, username):
        with self.lock:
            data = self.load()
            user = self._find(data, username)
            if not user:
                raise KeyError("Brugeren findes ikke")
            data["users"] = [item for item in data["users"] if item is not user]
            if not data["users"] or not self._active_admins(data):
                raise ValueError("Den sidste aktive administrator kan ikke slettes")
            self._write(data)
            self.drop_sessions(user["username"])

    def record_login(self, username):
        with self.lock:
            data = self.load()
            user = self._find(data, username)
            if user:
                user["last_login"] = time.time()
                try:
                    self._write(data)
                except OSError:
                    pass

    def update(self, current_password, username, password, enabled, acting=None, email=None):
        """Change the signed-in user's own account (and, for admins, the login switch)."""
        with self.lock:
            data = self.load()
            if not data:
                raise PermissionError("Forkert nuværende adgangskode")
            if acting is None:
                acting = next((u["username"] for u in data["users"] if u.get("role") == "admin"), data["users"][0]["username"])
            user = self._find(data, acting)
            if not user or not self.verify(user["username"], current_password):
                raise PermissionError("Forkert nuværende adgangskode")
            new_name = str(username or "").strip() or user["username"]
            if _normal(new_name) != _normal(user["username"]):
                self._check_username(new_name)
                if self._find(data, new_name):
                    raise ValueError("Brugernavnet findes allerede")
            if password:
                self._new_secret(user, password)
            if email is not None:
                user["email"] = self._clean_email(email)
            old_name = user["username"]
            user["username"] = new_name
            if user.get("role") == "admin" and enabled is not None:
                data["enabled"] = bool(enabled)
            self._write(data)
            if password or new_name != old_name:
                self.drop_sessions(old_name)

    def set_enabled(self, enabled):
        with self.lock:
            data = self.load()
            if data:
                data["enabled"] = bool(enabled)
                self._write(data)

    # ------------------------------------------------------ password reset
    def create_reset_token(self, identifier):
        """Return (public_user, token) for a user with an e-mail, else None."""
        data = self.load()
        wanted = _normal(identifier)
        if not data or not wanted:
            return None
        user = self._find(data, wanted) or next(
            (item for item in data["users"] if _normal(item.get("email")) == wanted), None)
        if not user or not user.get("email") or not self._active(user):
            return None
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self.lock:
            self.reset_tokens = {key: value for key, value in self.reset_tokens.items()
                                 if value["expires"] > now and _normal(value["username"]) != _normal(user["username"])}
            self.reset_tokens[digest] = {"username": user["username"], "expires": now + RESET_TOKEN_SECONDS}
        return self.public_user(user), token

    def reset_token_valid(self, token):
        entry = self.reset_tokens.get(hashlib.sha256(str(token or "").encode()).hexdigest())
        return bool(entry and entry["expires"] > time.time())

    def reset_password(self, token, password):
        digest = hashlib.sha256(str(token or "").encode()).hexdigest()
        with self.lock:
            entry = self.reset_tokens.get(digest)
            if not entry or entry["expires"] <= time.time():
                self.reset_tokens.pop(digest, None)
                raise PermissionError("Linket er udløbet eller allerede brugt")
            self._check_password(password)
            self.update_user(entry["username"], password=password)
            self.reset_tokens.pop(digest, None)
            return entry["username"]

    # -------------------------------------------------------- rate limiting
    def allow_attempt(self, ip):
        now = time.time()
        with self.lock:
            values = [t for t in self.failures.get(ip, []) if now - t < 300]
            self.failures[ip] = values
            if len(self.failures) > 1024:
                self.failures = {key: times for key, times in self.failures.items()
                                 if times and now - times[-1] < 300}
            return len(values) < 5

    def take_attempt(self, ip):
        """Reserve a login attempt before the (slow) password check.

        Checking the limit first and recording the failure only after the
        hash let parallel requests bypass the five-per-five-minutes limit.
        The reservation is released again by succeeded().
        """
        now = time.time()
        with self.lock:
            values = [t for t in self.failures.get(ip, []) if now - t < 300]
            if len(values) >= 5:
                self.failures[ip] = values
                return None
            values.append(now)
            self.failures[ip] = values
            return now

    def succeeded(self, ip, token):
        with self.lock:
            values = self.failures.get(ip, [])
            if token in values:
                values.remove(token)

    def failed(self, ip):
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())

    # -------------------------------------------------------------- sessions
    def _load_sessions(self):
        try:
            payload = json.loads(self.session_path.read_text())
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        now = time.time()
        for sid, session in payload.items():
            if not isinstance(session, dict) or not session.get("remember"):
                continue
            try:
                expires = float(session["expires"])
            except (KeyError, TypeError, ValueError):
                continue
            if expires <= now:
                continue
            self.sessions[str(sid)] = {
                "username": str(session.get("username", "")),
                "csrf": str(session.get("csrf", "")),
                "expires": expires,
                "remember": True,
            }

    def _save_sessions(self):
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with self.lock:
            remembered = {
                sid: dict(session)
                for sid, session in self.sessions.items()
                if session.get("remember") and float(session.get("expires", 0)) > now
            }
        if not remembered:
            try:
                self.session_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        tmp = self.session_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(remembered, separators=(",", ":")))
        os.chmod(tmp, 0o600)
        tmp.replace(self.session_path)

    def drop_sessions(self, username):
        wanted = _normal(username)
        with self.lock:
            before = len(self.sessions)
            self.sessions = {sid: s for sid, s in self.sessions.items() if _normal(s.get("username")) != wanted}
            if len(self.sessions) != before:
                try:
                    self._save_sessions()
                except OSError:
                    pass

    def _forget(self, sid):
        """Drop one session; the disk copy is rewritten only for a remembered one."""
        with self.lock:
            removed = self.sessions.pop(sid, None)
            if removed and removed.get("remember"):
                self._save_sessions()

    def session(self, username, remember=False):
        user = self._find(self.load(), username)
        if user:
            username = user["username"]
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        lifetime = REMEMBER_SESSION_SECONDS if remember else SHORT_SESSION_SECONDS
        with self.lock:
            self.sessions[sid] = {
                "username": username,
                "csrf": csrf,
                "expires": time.time() + lifetime,
                "remember": bool(remember),
            }
            if remember:
                self._save_sessions()
        return sid, csrf

    def session_kontrolrum(self, username, binding):
        """A session from Kontrolrum single sign-on. It is bound to Kontrolrum's
        address and key (binding), is never written to disk and ends with a
        restart; Kontrolrum simply signs in again."""
        user = self._find(self.load(), username)
        if not self._active(user):
            raise KeyError("Brugeren findes ikke")
        now = time.time()
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self.lock:
            # Kontrolrum signs in again whenever its session is gone, so old
            # ones are cleared here to keep the memory use flat.
            live = {key: value for key, value in self.sessions.items() if float(value.get("expires", 0)) > now}
            bound = sorted((key for key, value in live.items() if value.get("kr") is not None),
                           key=lambda key: live[key]["expires"])
            for key in bound[:-7]:
                live.pop(key, None)
            live[sid] = {
                "username": user["username"],
                "csrf": csrf,
                "expires": now + SHORT_SESSION_SECONDS,
                "remember": False,
                "kr": dict(binding),
            }
            self.sessions = live
        return sid, csrf

    def _kontrolrum_session_problem(self, session, client_ip):
        """'' when this request may use the Kontrolrum session, else why not."""
        guard = self.session_guard
        if guard is None or client_ip is None:
            return "peer_changed"
        try:
            return guard(session.get("kr"), client_ip, session.get("username"))
        except Exception:  # A fault here must refuse the session, nothing more.
            return "peer_changed"

    def session_max_age(self, sid):
        session = self.sessions.get(sid) or {}
        return REMEMBER_SESSION_SECONDS if session.get("remember") else SHORT_SESSION_SECONDS

    def authenticate_cookie(self, header, client_ip=None):
        data = self.load()
        if not data:
            return None
        if not data.get("enabled", True):
            admin = next((u for u in data["users"] if u.get("role") == "admin"), data["users"][0])
            return {"username": admin.get("username"), "csrf": self.open_csrf, "role": "admin"}
        try:
            cookie = SimpleCookie(header)
            sid = cookie["dantherm_session"].value
        except Exception:  # noqa: BLE001 - a bad or missing cookie is just no session
            return None
        with self.lock:
            session = self.sessions.get(sid)
        if not session or session["expires"] < time.time():
            # An unknown cookie costs no disk write; only a remembered session
            # that really expired is removed from the file.
            if session:
                self._forget(sid)
            return None
        if session.get("kr") is not None:
            problem = self._kontrolrum_session_problem(session, client_ip)
            if problem:
                # A copied cookie from elsewhere does not end Kontrolrum's own
                # session; a new key, SSO off or a removed tick does.
                if problem != "peer_changed":
                    self._forget(sid)
                return None
        # Role, deletion, disabling and expiry take effect immediately.
        user = self._find(data, session.get("username"))
        if not self._active(user):
            self._forget(sid)
            return None
        session["role"] = user.get("role", "user")
        # Keep normal sessions sliding for 12 hours. Remembered sessions are
        # fixed at 30 days from login, matching the UI promise exactly.
        if not session.get("remember"):
            session["expires"] = time.time() + SHORT_SESSION_SECONDS
        return session

    def logout(self, header):
        try:
            cookie = SimpleCookie(header)
            sid = cookie["dantherm_session"].value
        except Exception:  # noqa: BLE001 - a bad or missing cookie
            return
        self._forget(sid)
