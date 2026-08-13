#!/usr/bin/env python3
"""
op - a friendly CLI for OpenProject.

Principles:
  * Say names, not numbers.        --project scrum --type bug --assign me
  * Output fits the audience.      table in a terminal, JSON in a pipe
  * Do not make the user repeat.   default project, cached lookups
  * Errors name the valid answers.
  * Work in batches.               op close 1 2 3

Configuration resolves as: flags -> OP_URL/OP_TOKEN -> ~/.openproject/config.json
Requires only the Python 3.8+ standard library.
"""

import argparse
import base64
import datetime as dt
import difflib
import json
import mimetypes
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import Counter
from pathlib import Path

CONFIG_DIR = Path.home() / ".openproject"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_PATH = CONFIG_DIR / "cache.json"
CACHE_TTL = 600  # seconds; lookup tables change rarely
CTX = ssl.create_default_context()

IS_TTY = sys.stdout.isatty()
USE_COLOR = IS_TTY and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    BLUE = "\033[34m"; CYAN = "\033[36m"


def paint(text, *codes):
    if not USE_COLOR or not text:
        return text
    return "".join(codes) + str(text) + C.RESET


def visible_len(s):
    return len(re.sub(r"\033\[[0-9;]*m", "", str(s)))


def die(msg, code=1, hint=None):
    print(paint("op: ", C.RED) + str(msg), file=sys.stderr)
    if hint:
        print("    " + paint(hint, C.DIM), file=sys.stderr)
    sys.exit(code)


def note(msg):
    """Human-facing confirmation. Always stderr so stdout stays pipeable.

    stdout is block-buffered when redirected while stderr is not, so flush
    first to keep the two streams in the order they were written.
    """
    sys.stdout.flush()
    print(msg, file=sys.stderr)
    sys.stderr.flush()


def today():
    return dt.date.today()


def parse_date(s):
    if not s:
        return None
    s = s.strip().lower()
    if s == "today":
        return today().isoformat()
    if s == "tomorrow":
        return (today() + dt.timedelta(days=1)).isoformat()
    if s == "yesterday":
        return (today() - dt.timedelta(days=1)).isoformat()
    m = re.fullmatch(r"([+-]?\d+)\s*([dwm])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"d": 1, "w": 7, "m": 30}[unit]
        return (today() + dt.timedelta(days=days)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    die(f"cannot understand the date {s!r}",
        hint="Use YYYY-MM-DD, or today / tomorrow / +7d / +2w")


def parse_hours(s):
    """Accept 2, 2h, 90m, 1.5h, 1h30m -> ISO-8601 duration."""
    s = str(s).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return f"PT{float(s):g}H"
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+)m)?", s)
    if m and (m.group(1) or m.group(2)):
        hours = float(m.group(1) or 0) + int(m.group(2) or 0) / 60.0
        return f"PT{hours:g}H"
    die(f"cannot understand the duration {s!r}", hint="Try: 2, 2h, 90m, or 1h30m")


def humanize_duration(iso):
    if not iso:
        return None
    m = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?", str(iso))
    if not m:
        return iso
    hours = float(m.group(1) or 0) + float(m.group(2) or 0) / 60.0
    return f"{hours:g}h"


# --------------------------------------------------------------------------
# config and cache
# --------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"config file {CONFIG_PATH} is not valid JSON: {e}")


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# Everything the config file understands. Keeping it declarative means
# `op config` can document itself and `op config set` can validate.
SETTINGS = {
    "url":            dict(type=str, default=None, env="OP_URL",
                           desc="instance base URL"),
    "token":          dict(type=str, default=None, env="OP_TOKEN", secret=True,
                           desc="API token"),
    "defaultProject": dict(type=str, default=None, env="OP_PROJECT",
                           desc="project used when --project is omitted"),
    "defaultType":    dict(type=str, default="Task", env=None,
                           desc="type used by `op new` when --type is omitted"),
    "defaultLimit":   dict(type=int, default=None, env=None,
                           desc="max records when --limit is omitted"),
    "cacheTtl":       dict(type=int, default=600, env=None,
                           desc="seconds to cache lookup tables (0 disables)"),
    "timeout":        dict(type=int, default=90, env=None,
                           desc="HTTP timeout in seconds"),
    "color":          dict(type=str, default="auto", env="OP_COLOR",
                           desc="auto, always or never"),
}

_CONFIG_CACHE = None


def config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = load_config()
        unknown = [k for k in _CONFIG_CACHE if k not in SETTINGS]
        if unknown:
            note(paint(f"op: ignoring unknown setting(s) in {CONFIG_PATH}: "
                       f"{', '.join(unknown)}", C.YELLOW))
    return _CONFIG_CACHE


def setting(name):
    spec = SETTINGS[name]
    env = spec.get("env")
    if env and os.environ.get(env):
        raw = os.environ[env]
    elif config().get(name) is not None:
        raw = config()[name]
    else:
        return spec["default"]
    try:
        return spec["type"](raw)
    except (TypeError, ValueError):
        die(f"setting {name!r} should be a {spec['type'].__name__}, got {raw!r}",
            hint=f"Fix it in {CONFIG_PATH}")


def setting_source(name):
    spec = SETTINGS[name]
    if spec.get("env") and os.environ.get(spec["env"]):
        return f"env {spec['env']}"
    if config().get(name) is not None:
        return "config file"
    return "default"


def resolve_credentials(args):
    url = getattr(args, "url", None) or setting("url")
    token = getattr(args, "token", None) or setting("token")
    if not url or not token:
        die("not configured yet", hint="Run:  op setup")
    return url.rstrip("/"), token


class Cache:
    """On-disk cache for lookup tables.

    The server tops out near 4 requests/second, so resolving names should not
    cost a round trip every time. Lookup tables (projects, types, statuses,
    priorities, users) change rarely, so a short TTL is safe and makes most
    commands a single request.
    """

    def __init__(self, base_url, enabled=True):
        self.base = base_url
        self.ttl = setting("cacheTtl")
        self.enabled = enabled and self.ttl > 0
        self.data = {}
        if self.enabled and CACHE_PATH.exists():
            try:
                self.data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def _key(self, name):
        return f"{self.base}::{name}"

    def get(self, name):
        if not self.enabled:
            return None
        entry = self.data.get(self._key(name))
        if not entry:
            return None
        if time.time() - entry.get("at", 0) > self.ttl:
            return None
        return entry.get("value")

    def put(self, name, value):
        if not self.enabled:
            return
        self.data[self._key(name)] = {"at": time.time(), "value": value}
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(self.data), encoding="utf-8")
            os.chmod(CACHE_PATH, 0o600)
        except OSError:
            pass

    @staticmethod
    def clear():
        if CACHE_PATH.exists():
            CACHE_PATH.unlink()
            return True
        return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

RETRY_STATUS = {429, 500, 502, 503, 504}


class Client:
    def __init__(self, base_url, token, verbose=False, timeout=90, retries=3):
        self.base = base_url.rstrip("/")
        self.api = self.base + "/api/v3"
        self.auth = base64.b64encode(f"apikey:{token}".encode()).decode()
        self.verbose = verbose
        self.timeout = timeout
        self.retries = retries
        self.calls = 0

    def request(self, method, path, body=None, raw_body=None,
                ctype="application/json", params=None):
        if path.startswith("http"):
            url = path
        elif path.startswith("/api/v3"):
            url = self.base + path
        else:
            url = self.api + ("" if path.startswith("/") else "/") + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)

        data = raw_body if raw_body is not None else (
            json.dumps(body).encode() if body is not None else None)

        attempt = 0
        while True:
            attempt += 1
            self.calls += 1
            if self.verbose:
                note(paint(f"-> {method} {url}", C.DIM))
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", "Basic " + self.auth)
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", ctype)
            try:
                with urllib.request.urlopen(req, context=CTX, timeout=self.timeout) as r:
                    payload = r.read()
                    if not payload:
                        return r.status, None
                    if r.headers.get_content_type().startswith(
                            ("application/json", "application/hal+json")):
                        return r.status, json.loads(payload.decode("utf-8", "replace"))
                    return r.status, payload
            except urllib.error.HTTPError as e:
                if e.code in RETRY_STATUS and attempt <= self.retries:
                    delay = min(2 ** (attempt - 1), 8)
                    if self.verbose:
                        note(paint(f"   HTTP {e.code}, retrying in {delay}s", C.DIM))
                    time.sleep(delay)
                    continue
                payload = e.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    die(f"HTTP {e.code} {e.reason}: {payload[:300]}", 2)
                self._explain(e.code, parsed, url)
            except urllib.error.URLError as e:
                if attempt <= self.retries and isinstance(e.reason, OSError):
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                die(f"cannot reach {self.base}: {e.reason}", 3,
                    hint="Check the URL with: op config")

    def _explain(self, code, parsed, url=""):
        msg = error_text(parsed)
        hint = None
        if code == 401:
            hint = "Token rejected. Re-run: op setup"
        elif code == 403 and "time_entries" in url:
            # Time tracking lives in the "costs" module, which is enabled per
            # project. Admin rights do not bypass a disabled module.
            hint = ("Time tracking is off for that project. Enable the Time and costs "
                    "module in Project settings -> Modules.")
        elif code == 403:
            hint = "Your account lacks permission. Project membership or admin may be required."
        elif code == 404:
            hint = "Check the id with: op ls --status all"
        elif "not set to one of the allowed values" in msg.lower():
            hint = "Types are enabled per project. See: op types --project <name>"
        die(f"HTTP {code}: {msg}", 2, hint)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)[1]

    def collect(self, path, params=None, limit=None):
        """Gather elements across pages. OpenProject's `offset` is a 1-based page."""
        params = dict(params or {})
        params.setdefault("pageSize", 100)
        out, page = [], 1
        while True:
            params["offset"] = page
            body = self.get(path, params)
            els = (body or {}).get("_embedded", {}).get("elements", [])
            if not els:
                return out
            out.extend(els)
            if limit and len(out) >= limit:
                return out[:limit]
            if len(out) >= (body or {}).get("total", 0):
                return out
            page += 1


def error_text(parsed):
    if not isinstance(parsed, dict):
        return str(parsed)[:300]
    msg = parsed.get("message", "")
    embedded = parsed.get("_embedded", {}).get("errors")
    if embedded:
        detail = "; ".join(e.get("message", "") for e in embedded if isinstance(e, dict))
        if detail:
            return f"{msg} ({detail})" if msg else detail
    return msg or json.dumps(parsed)[:300]


# --------------------------------------------------------------------------
# name resolution
# --------------------------------------------------------------------------

class Resolver:
    def __init__(self, client, cache):
        self.c = client
        self.cache = cache

    def table(self, name, path=None):
        cached = self.cache.get(name)
        if cached is not None:
            return cached
        items = self.c.collect(path or ("/" + name))
        slim = [{"id": i.get("id"), "name": i.get("name"),
                 "identifier": i.get("identifier"), "login": i.get("login"),
                 "email": i.get("email"), "isClosed": i.get("isClosed"),
                 "isDefault": i.get("isDefault")} for i in items]
        self.cache.put(name, slim)
        return slim

    def _names(self, item, extra=()):
        vals = [item.get("name"), item.get("identifier")] + [item.get(k) for k in extra]
        return [str(v) for v in vals if v]

    def _match(self, needle, items, kind, extra=()):
        if needle is None:
            return None
        s = str(needle).strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        low = s.lower()

        for predicate in (lambda n: n.lower() == low,
                          lambda n: n.lower().startswith(low),
                          lambda n: low in n.lower()):
            hits = [i for i in items if any(predicate(n) for n in self._names(i, extra))]
            if len(hits) == 1:
                return hits[0]["id"]
            if len(hits) > 1:
                opts = ", ".join(f"{h.get('name') or h.get('identifier')} (#{h['id']})"
                                 for h in hits[:8])
                die(f"{s!r} matches more than one {kind}", 1, f"Be more specific: {opts}")

        available = sorted({n for i in items for n in self._names(i, extra)})
        folded = {n.lower(): n for n in available}
        close = [folded[m] for m in difflib.get_close_matches(low, list(folded), n=3, cutoff=0.5)]
        hint = (f"Did you mean: {', '.join(close)}?" if close else
                "Available: " + ", ".join(available[:12]) + (" ..." if len(available) > 12 else ""))
        die(f"no {kind} matches {s!r}", 1, hint)

    def project(self, v):
        return None if v is None else self._match(v, self.table("projects"), "project")

    def type(self, v, project_id=None):
        if v is None:
            return None
        if project_id:
            items = self.table(f"types:{project_id}", f"/projects/{project_id}/types")
        else:
            items = self.table("types")
        return self._match(v, items, "type")

    def status(self, v):
        return None if v is None else self._match(v, self.table("statuses"), "status")

    def priority(self, v):
        return None if v is None else self._match(v, self.table("priorities"), "priority")

    def user(self, v):
        if v is None:
            return None
        if str(v).lower() == "me":
            me = self.cache.get("me")
            if me is None:
                me = self.c.get("/users/me")["id"]
                self.cache.put("me", me)
            return me
        try:
            users = self.table("users")
        except SystemExit:
            die("listing users needs admin; pass a numeric user id instead", 1)
        return self._match(v, users, "user", extra=("login", "email"))

    def closed_status_id(self):
        statuses = self.table("statuses")
        named = [s for s in statuses if str(s.get("name", "")).lower() == "closed"]
        if named:
            return named[0]["id"]
        closed = [s for s in statuses if s.get("isClosed")]
        if not closed:
            die("this instance has no status marked closed")
        return closed[0]["id"]

    def open_status_id(self):
        statuses = self.table("statuses")
        for want in ("new", "in progress"):
            for s in statuses:
                if str(s.get("name", "")).lower() == want and not s.get("isClosed"):
                    return s["id"]
        for s in statuses:
            if not s.get("isClosed"):
                return s["id"]
        die("this instance has no open status")


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def want_json(args):
    if getattr(args, "json", False):
        return True
    if getattr(args, "table", False):
        return False
    return not IS_TTY


def link(obj, key, part="title"):
    l = ((obj.get("_links") or {}).get(key) or {})
    if part == "title":
        return l.get("title")
    href = l.get("href") or ""
    return href.rstrip("/").split("/")[-1] if href else None


def slim_wp(wp, base=None):
    row = {
        "id": wp.get("id"),
        "type": link(wp, "type"),
        "status": link(wp, "status"),
        "subject": wp.get("subject"),
        "assignee": link(wp, "assignee"),
        "project": link(wp, "project"),
        "priority": link(wp, "priority"),
        "percentDone": wp.get("percentageDone"),
        "startDate": wp.get("startDate"),
        "dueDate": wp.get("dueDate"),
        "updatedAt": wp.get("updatedAt"),
    }
    if base:
        row["url"] = f"{base}/work_packages/{wp.get('id')}"
    return row


def is_overdue(row):
    due = row.get("dueDate")
    if not due:
        return False
    try:
        return dt.date.fromisoformat(due) < today()
    except ValueError:
        return False


def colorize_row(row, closed_names):
    out = dict(row)
    status = row.get("status")
    if status:
        if str(status).lower() in closed_names:
            out["status"] = paint(status, C.DIM)
        elif str(status).lower() in ("in progress", "in development", "in testing"):
            out["status"] = paint(status, C.CYAN)
        else:
            out["status"] = paint(status, C.YELLOW)
    if row.get("dueDate") and is_overdue(row):
        out["dueDate"] = paint(row["dueDate"], C.RED)
    if row.get("id") is not None:
        out["id"] = paint(row["id"], C.BOLD)
    return out


def print_table(rows, columns):
    if not rows:
        print(paint("(nothing found)", C.DIM))
        return
    columns = [c for c in columns if any(r.get(c) not in (None, "") for r in rows)] or \
              list(rows[0].keys())
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in columns] for r in rows]
    widths = [min(max([len(c)] + [visible_len(row[i]) for row in cells]), 60)
              for i, c in enumerate(columns)]

    def pad(v, w):
        vis = visible_len(v)
        return v + " " * max(0, w - vis) if vis <= w else v[:w]

    print(paint("  ".join(pad(c, widths[i]) for i, c in enumerate(columns)).rstrip(), C.BOLD))
    for row in cells:
        print("  ".join(pad(row[i], widths[i]) for i in range(len(columns))).rstrip())


def print_record(rec, title=None):
    if title:
        print(paint(title, C.BOLD))
    keys = [k for k, v in rec.items() if v not in (None, "")]
    if not keys:
        return
    width = max(len(k) for k in keys)
    for k in keys:
        print(f"  {paint(k.ljust(width), C.DIM)}  {rec[k]}")


def output(args, data, columns=None, title=None, closed_names=frozenset()):
    if want_json(args):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif isinstance(data, list):
        cols = getattr(args, "columns", None)
        if cols:
            wanted = [c.strip() for c in cols.split(",")]
            data = [{k: r.get(k) for k in wanted} for r in data]
            columns = wanted
        painted = [colorize_row(r, closed_names) for r in data] if USE_COLOR else data
        print_table(painted, columns or (list(data[0].keys()) if data else []))
    else:
        print_record(data, title)


def confirm(prompt):
    if not IS_TTY:
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def ask(prompt, default=None, secret=False):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        die("setup needs an interactive terminal",
            hint="Non-interactive? Use: op setup --url URL --token TOKEN --project NAME --yes")
    return answer or (default or "")


def ask_yes(prompt, default=True, assume=False):
    # --yes means "take the default", not "answer yes". Otherwise a prompt
    # that defaults to no - such as "use this unreachable URL anyway?" -
    # would be silently accepted in unattended mode.
    if assume:
        return default
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def probe_instance(url):
    """Confirm something OpenProject-shaped answers at this URL.

    Unauthenticated /api/v3 replies 200 or 401 on a real instance; anything
    else means the URL is wrong before we bother asking for a token.
    """
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/v3", method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
            return True, body.get("coreVersion")
    except urllib.error.HTTPError as e:
        return (e.code in (401, 403)), None
    except Exception as e:
        return False, str(e)


def install_completion(shell, assume=False):
    """Append a completion script to the user's shell profile, once."""
    marker = "# >>> op completion >>>"
    if shell == "powershell":
        profile = Path(os.environ.get("USERPROFILE", Path.home())) / \
            "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        script = PS_COMPLETION
    else:
        profile = Path.home() / ".bashrc"
        script = BASH_COMPLETION

    if profile.exists() and marker in profile.read_text(encoding="utf-8", errors="replace"):
        print(f"  completion already present in {profile}")
        return True
    if not ask_yes(f"  Add tab completion to {profile}?", True, assume):
        return False
    try:
        profile.parent.mkdir(parents=True, exist_ok=True)
        with profile.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{marker}\n{script}# <<< op completion <<<\n")
        print(f"  added to {profile} (restart your shell to use it)")
        return True
    except OSError as e:
        print(f"  could not write {profile}: {e}")
        return False


def add_to_path(directory, assume=False):
    """Add a directory to the *user* PATH on Windows.

    Uses the registry rather than setx, because setx silently truncates any
    PATH longer than 1024 characters.
    """
    if os.name != "nt":
        print(f"  add this to your PATH:  export PATH=\"{directory}:$PATH\"")
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            entries = [p for p in current.split(";") if p]
            if any(os.path.normcase(p.rstrip("\\")) ==
                   os.path.normcase(str(directory).rstrip("\\")) for p in entries):
                print("  already on your PATH")
                return True
            if not ask_yes(f"  Add {directory} to your user PATH?", True, assume):
                return False
            winreg.SetValueEx(key, "Path", 0, kind, ";".join(entries + [str(directory)]))
        print("  added to your user PATH (open a new terminal to pick it up)")
        return True
    except OSError as e:
        print(f"  could not update PATH: {e}")
        return False


def cmd_setup(args):
    assume = getattr(args, "yes", False)
    cfg = load_config()
    interactive = sys.stdin.isatty()

    print(paint("op setup", C.BOLD))
    print(paint("Press Enter to accept the value in brackets.\n", C.DIM))

    # ---- 1. instance -----------------------------------------------------
    print(paint("1. Which OpenProject instance?", C.BOLD))
    current = cfg.get("url") or os.environ.get("OP_URL") or ""
    url = args.url or (current if (assume or not interactive) else ask("   URL", current))
    if not url:
        die("a URL is required", hint="op setup --url https://openproject.example.com")
    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/")

    ok, detail = probe_instance(url)
    if not ok:
        print(paint(f"   Could not reach an OpenProject API at {url}", C.RED))
        if detail:
            print(paint(f"   {detail}", C.DIM))
        if not ask_yes("   Use it anyway?", False, assume):
            die("setup cancelled", 0)
    else:
        print(paint(f"   OK  {url}" + (f"  (OpenProject {detail})" if detail else ""), C.GREEN))

    # ---- 2. token --------------------------------------------------------
    print("\n" + paint("2. API token", C.BOLD))
    token = args.token or cfg.get("token") or os.environ.get("OP_TOKEN")
    reuse = bool(token) and not args.token
    if reuse and interactive and not assume:
        reuse = ask_yes("   Keep the existing token?", True)
    if not token or not reuse:
        token_url = f"{url}/my/access_token"
        print(f"   Create one at: {paint(token_url, C.CYAN)}")
        if interactive and ask_yes("   Open that page now?", True, assume):
            try:
                webbrowser.open(token_url)
            except Exception:
                pass
        token = args.token or ask("   Paste the token")
    if not token:
        die("a token is required")

    me = Client(url, token).get("/users/me")
    print(paint(f"   OK  authenticated as {me.get('name')}"
                + (" (admin)" if me.get("admin") else ""), C.GREEN))

    cfg.update({"url": url, "token": token})
    save_config(cfg)
    Cache.clear()

    # ---- 3. default project ---------------------------------------------
    print("\n" + paint("3. Default project", C.BOLD))
    print(paint("   Sets the project used by ls, new, stats and types.", C.DIM))
    chosen = args.project
    if chosen is None and (interactive and not assume):
        client = Client(url, token)
        projects = client.collect("/projects", limit=50)
        if projects:
            for i, p in enumerate(projects, 1):
                print(f"   {str(i).rjust(2)}. {p.get('name')}  "
                      + paint(f"({p.get('identifier')})", C.DIM))
            print(f"   {' 0'}. none - always pass --project")
            pick = ask("   Choose a number", "0")
            if pick.isdigit() and 1 <= int(pick) <= len(projects):
                chosen = projects[int(pick) - 1].get("identifier")
    if chosen:
        cfg["defaultProject"] = chosen
        save_config(cfg)
        print(paint(f"   OK  default project: {chosen}", C.GREEN))
    else:
        print(paint("   skipped", C.DIM))

    # ---- 4. shell integration -------------------------------------------
    if not getattr(args, "skip_shell", False):
        print("\n" + paint("4. Shell integration", C.BOLD))
        here = Path(__file__).resolve().parent
        add_to_path(here, assume)
        install_completion("powershell" if os.name == "nt" else "bash", assume)

    # ---- done ------------------------------------------------------------
    print("\n" + paint("Done.", C.GREEN) + f"  Settings saved to {CONFIG_PATH}")
    print("\nTry:")
    print("  op ls                 open work packages")
    print("  op mine               assigned to you")
    print("  op new \"Something\"    create one")
    print("  op help               everything else")


def _mask(name, value):
    if value and SETTINGS.get(name, {}).get("secret"):
        return str(value)[:14] + "..."
    return value


def cmd_config_show(args):
    rows = [{"setting": name,
             "value": _mask(name, setting(name)),
             "source": setting_source(name),
             "what": SETTINGS[name]["desc"]}
            for name in SETTINGS]
    if want_json(args):
        print(json.dumps({"file": str(CONFIG_PATH),
                          "exists": CONFIG_PATH.exists(),
                          "settings": {r["setting"]: r["value"] for r in rows},
                          "sources": {r["setting"]: r["source"] for r in rows}},
                         indent=2, default=str))
        return
    print(paint(f"Config file: {CONFIG_PATH}"
                + ("" if CONFIG_PATH.exists() else "  (not created yet)"), C.BOLD))
    print_table(rows, ["setting", "value", "source", "what"])
    print(paint("\nEdit it directly with `op config edit`, or set one value with"
                "\n  op config set <setting> <value>", C.DIM))


def cmd_config_path(args):
    print(CONFIG_PATH)


def cmd_config_edit(args):
    if not CONFIG_PATH.exists():
        save_config({k: v for k, v in ((n, setting(n)) for n in SETTINGS) if v is not None})
        note(f"created {CONFIG_PATH}")
    editor = args.editor or os.environ.get("OP_EDITOR") or os.environ.get("EDITOR") \
        or os.environ.get("VISUAL")
    if editor:
        os.system(f'{editor} "{CONFIG_PATH}"')
        return
    if os.name == "nt":
        os.startfile(str(CONFIG_PATH))  # noqa: S606 - opens the user's default editor
        note(f"opened {CONFIG_PATH}")
    else:
        die("no editor found", hint="Set $EDITOR, or edit " + str(CONFIG_PATH))


def cmd_config_set(args):
    cfg = load_config()
    changed = []

    # Named convenience flags.
    for flag, key in (("url", "url"), ("token", "token"), ("project", "defaultProject")):
        val = getattr(args, flag, None)
        if val:
            cfg[key] = val.rstrip("/") if key == "url" else val
            changed.append(key)
    if getattr(args, "clear_project", False):
        cfg.pop("defaultProject", None)
        changed.append("defaultProject (cleared)")

    # Generic  op config set <setting> <value>
    if args.setting:
        name = args.setting
        if name not in SETTINGS:
            close = difflib.get_close_matches(name, list(SETTINGS), n=3, cutoff=0.4)
            die(f"unknown setting {name!r}", 1,
                (f"Did you mean: {', '.join(close)}?" if close
                 else "Known settings: " + ", ".join(SETTINGS)))
        if args.value is None or args.value == "":
            cfg.pop(name, None)
            changed.append(f"{name} (cleared)")
        else:
            spec = SETTINGS[name]
            try:
                cfg[name] = spec["type"](args.value)
            except (TypeError, ValueError):
                die(f"{name} must be a {spec['type'].__name__}, got {args.value!r}")
            if name == "color" and cfg[name] not in ("auto", "always", "never"):
                die("color must be auto, always or never")
            changed.append(name)

    if not changed:
        die("nothing to set", hint="Try:  op config set defaultProject scrum"
                                   "\n      op config set --project scrum")
    save_config(cfg)
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    note(paint("Updated: " + ", ".join(changed), C.GREEN))
    cmd_config_show(args)


def cmd_cache(args):
    if args.action == "clear":
        note("cache cleared" if Cache.clear() else "cache was already empty")
    else:
        exists = CACHE_PATH.exists()
        size = CACHE_PATH.stat().st_size if exists else 0
        output(args, {"file": str(CACHE_PATH), "exists": exists, "bytes": size,
                      "ttlSeconds": CACHE_TTL}, title="Cache")


def cmd_ping(args, c, r):
    me = c.get("/users/me")
    root = c.get("/")
    output(args, {"url": c.base,
                  "version": root.get("coreVersion") if isinstance(root, dict) else None,
                  "user": me.get("name"), "userId": me.get("id"),
                  "admin": me.get("admin")}, title="Connected")


def cmd_whoami(args, c, r):
    u = c.get("/users/me")
    output(args, {"id": u.get("id"), "name": u.get("name"), "login": u.get("login"),
                  "email": u.get("email"), "admin": u.get("admin")}, title="You")


NO_PROJECT = {"", "all", "-", "any"}


def default_project(args):
    """Resolve the project to use: --project, else the configured default.

    `--project all` (or "", "-", "any") deliberately means "every project",
    overriding a configured default so it never becomes a trap.
    """
    if getattr(args, "all_projects", False):
        return None
    raw = getattr(args, "project", None)
    if raw is not None:
        return None if str(raw).strip().lower() in NO_PROJECT else raw
    return setting("defaultProject")


def _filters(args, r, project_required=False):
    filters = []
    status = (getattr(args, "status", None) or "open").lower()
    if status == "open":
        filters.append({"status": {"operator": "o", "values": []}})
    elif status == "closed":
        filters.append({"status": {"operator": "c", "values": []}})
    elif status != "all":
        filters.append({"status": {"operator": "=", "values": [str(r.status(status))]}})

    proj = default_project(args)
    if proj:
        filters.append({"project": {"operator": "=", "values": [str(r.project(proj))]}})
    elif project_required:
        die("which project?", hint="Add --project <name>, or set a default: op config set --project <name>")
    if getattr(args, "type", None):
        pid = r.project(proj) if proj else None
        filters.append({"type": {"operator": "=", "values": [str(r.type(args.type, pid))]}})
    if getattr(args, "assignee", None):
        filters.append({"assignee": {"operator": "=", "values": [str(r.user(args.assignee))]}})
    if getattr(args, "query", None):
        filters.append({"subjectOrId": {"operator": "**", "values": [args.query]}})
    # Always send filters, even empty: omitting the parameter makes the server
    # apply its own "open only" default and silently hide closed records.
    return json.dumps(filters)


def cmd_ls(args, c, r):
    params = {"filters": _filters(args, r),
              "sortBy": json.dumps([[args.sort, args.order]]),
              "pageSize": min(args.limit or 100, 200)}
    items = c.collect("/work_packages", params=params, limit=args.limit)
    rows = items if args.full else [slim_wp(w, c.base) for w in items]

    if not args.full:
        if getattr(args, "overdue", False):
            rows = [w for w in rows if is_overdue(w)]
        if getattr(args, "due_before", None):
            cutoff = parse_date(args.due_before)
            rows = [w for w in rows if w.get("dueDate") and w["dueDate"] <= cutoff]
        if getattr(args, "unassigned", False):
            rows = [w for w in rows if not w.get("assignee")]

    closed = {s["name"].lower() for s in r.table("statuses")
              if s.get("isClosed") and s.get("name")} if (USE_COLOR and not args.full) else set()

    if not want_json(args) and not args.full:
        for row in rows:
            for k in ("url", "updatedAt", "percentDone", "startDate", "priority"):
                row.pop(k, None)
    output(args, rows, columns=["id", "type", "status", "subject", "assignee", "project", "dueDate"],
           closed_names=closed)
    if not want_json(args) and not args.full:
        note(paint(f"({len(rows)} shown)", C.DIM))


def cmd_mine(args, c, r):
    args.assignee = "me"
    cmd_ls(args, c, r)


def cmd_search(args, c, r):
    args.query = args.text
    if not getattr(args, "status", None):
        args.status = "all"
    cmd_ls(args, c, r)


def cmd_show(args, c, r):
    wp = c.get(f"/work_packages/{args.id}")
    if args.web:
        url = f"{c.base}/work_packages/{wp['id']}"
        webbrowser.open(url)
        note(f"opened {url}")
        return
    if args.full or want_json(args):
        output(args, wp if args.full else slim_wp(wp, c.base))
        return
    print_record(slim_wp(wp, c.base), title=f"#{wp['id']}  {wp.get('subject')}")
    desc = (wp.get("description") or {}).get("raw")
    if desc:
        print("\n" + paint("  description", C.DIM))
        for line in desc.splitlines():
            print(f"    {line}")
    if not args.no_comments:
        acts = c.collect(f"/work_packages/{args.id}/activities")
        comments = [a for a in acts if (a.get("comment") or {}).get("raw")]
        if comments:
            print("\n" + paint(f"  comments ({len(comments)})", C.DIM))
            for a in comments[-args.comments:]:
                who = link(a, "user") or f"user #{link(a, 'user', 'id')}"
                when = (a.get("createdAt") or "")[:16].replace("T", " ")
                print(f"    {paint(who, C.BOLD)}  {paint(when, C.DIM)}")
                for line in a["comment"]["raw"].splitlines():
                    print(f"      {line}")


def cmd_new(args, c, r):
    proj = default_project(args)
    if not proj:
        die("which project?",
            hint="Add --project <name>, or set a default: op config set --project <name>")
    project_id = r.project(proj)
    type_id = r.type(args.type or setting("defaultType"), project_id)
    body = {"subject": args.subject,
            "_links": {"project": {"href": f"/api/v3/projects/{project_id}"},
                       "type": {"href": f"/api/v3/types/{type_id}"}}}
    desc = args.description
    if desc == "-":
        desc = sys.stdin.read()
    if desc:
        body["description"] = {"raw": desc}
    if args.assign:
        body["_links"]["assignee"] = {"href": f"/api/v3/users/{r.user(args.assign)}"}
    if args.priority:
        body["_links"]["priority"] = {"href": f"/api/v3/priorities/{r.priority(args.priority)}"}
    if args.due:
        body["dueDate"] = parse_date(args.due)
    if args.start:
        body["startDate"] = parse_date(args.start)
    if args.parent:
        body["_links"]["parent"] = {"href": f"/api/v3/work_packages/{args.parent}"}
    _, wp = c.request("POST", "/work_packages", body)
    if want_json(args):
        print(json.dumps(wp if args.full else slim_wp(wp, c.base), indent=2, default=str))
    else:
        note(paint(f"Created #{wp['id']}", C.GREEN))
        print_record(slim_wp(wp, c.base), title=f"#{wp['id']}  {wp.get('subject')}")
    if args.web:
        webbrowser.open(f"{c.base}/work_packages/{wp['id']}")


def _patch(c, wp_id, changes=None, links=None):
    current = c.get(f"/work_packages/{wp_id}")
    body = {"lockVersion": current.get("lockVersion")}
    body.update(changes or {})
    if links:
        body["_links"] = links
    return c.request("PATCH", f"/work_packages/{wp_id}", body)[1]


def _report_many(args, c, results, verb):
    if want_json(args):
        print(json.dumps(results, indent=2, default=str))
    elif len(results) == 1:
        wp = results[0]
        note(paint(f"{verb} #{wp['id']}", C.GREEN))
        print_record(slim_wp(wp, c.base), title=f"#{wp['id']}  {wp.get('subject')}")
    else:
        note(paint(f"{verb} {len(results)} work packages", C.GREEN))
        print_table([slim_wp(w) for w in results],
                    ["id", "type", "status", "subject", "assignee"])


def cmd_edit(args, c, r):
    changes, links = {}, {}
    if args.subject:
        changes["subject"] = args.subject
    if args.description is not None:
        changes["description"] = {"raw": sys.stdin.read() if args.description == "-"
                                  else args.description}
    if args.percent is not None:
        changes["percentageDone"] = args.percent
    if args.due:
        changes["dueDate"] = parse_date(args.due)
    if args.start:
        changes["startDate"] = parse_date(args.start)
    if args.status:
        links["status"] = {"href": f"/api/v3/statuses/{r.status(args.status)}"}
    if args.priority:
        links["priority"] = {"href": f"/api/v3/priorities/{r.priority(args.priority)}"}
    if args.assign:
        links["assignee"] = {"href": f"/api/v3/users/{r.user(args.assign)}"}
    if args.type:
        links["type"] = {"href": f"/api/v3/types/{r.type(args.type)}"}
    if not changes and not links:
        die("nothing to change", hint="Try:  op edit 40 --status 'in progress' --assign me")
    _report_many(args, c, [_patch(c, i, changes, links or None) for i in args.ids], "Updated")


def cmd_close(args, c, r):
    sid = r.closed_status_id()
    _report_many(args, c,
                 [_patch(c, i, None, {"status": {"href": f"/api/v3/statuses/{sid}"}})
                  for i in args.ids], "Closed")


def cmd_reopen(args, c, r):
    sid = r.open_status_id()
    _report_many(args, c,
                 [_patch(c, i, None, {"status": {"href": f"/api/v3/statuses/{sid}"}})
                  for i in args.ids], "Reopened")


def cmd_assign(args, c, r):
    ids, who = list(args.args), args.to
    if who is None:
        if len(ids) < 2:
            die("who should this go to?", hint="Try:  op assign 15 sean   or   op assign 1 2 3 --to sean")
        who = ids.pop()          # trailing name form: op assign 15 sean
    uid = r.user(who)
    _report_many(args, c,
                 [_patch(c, i, None, {"assignee": {"href": f"/api/v3/users/{uid}"}})
                  for i in ids], "Assigned")


def cmd_comment(args, c, r):
    ids, text = list(args.args), args.text
    if text is None:
        if len(ids) < 2:
            die("what should the comment say?",
                hint="Try:  op comment 15 'Deployed'   or   op comment 1 2 --text 'Deployed'")
        text = ids.pop()
    results = []
    for i in ids:
        _, res = c.request("POST", f"/work_packages/{i}/activities",
                           {"comment": {"raw": text}})
        results.append({"workPackage": i, "activity": res.get("id"),
                        "createdAt": res.get("createdAt")})
    note(paint(f"Commented on {len(results)} work package"
               f"{'s' if len(results) != 1 else ''}", C.GREEN))
    if want_json(args):
        print(json.dumps(results, indent=2, default=str))


def cmd_history(args, c, r):
    items = c.collect(f"/work_packages/{args.id}/activities", limit=args.limit)
    if args.full:
        output(args, items)
        return
    rows = []
    for a in items:
        uid = link(a, "user", "id")
        rows.append({"id": a.get("id"),
                     "who": link(a, "user") or (f"user #{uid}" if uid else ""),
                     "when": (a.get("createdAt") or "")[:19].replace("T", " "),
                     "comment": (a.get("comment") or {}).get("raw", "").replace("\n", " ")})
    output(args, rows, columns=["id", "who", "when", "comment"])


def cmd_log(args, c, r):
    # The time-entry schema calls the work-package link "entity", requires a
    # user, and gives "activity" a server-side default, so it is left off.
    body = {"hours": parse_hours(args.hours),
            "spentOn": parse_date(args.on or "today"),
            "_links": {"entity": {"href": f"/api/v3/work_packages/{args.id}"},
                       "user": {"href": f"/api/v3/users/{r.user('me')}"}}}
    if args.comment:
        body["comment"] = {"raw": args.comment}
    _, res = c.request("POST", "/time_entries", body)
    note(paint(f"Logged {humanize_duration(res.get('hours'))} on #{args.id}", C.GREEN))
    output(args, {"id": res.get("id"), "workPackage": args.id,
                  "hours": humanize_duration(res.get("hours")),
                  "spentOn": res.get("spentOn"),
                  "activity": link(res, "activity"),
                  "comment": (res.get("comment") or {}).get("raw")})


def cmd_time(args, c, r):
    filters = []
    if args.user:
        filters.append({"user": {"operator": "=", "values": [str(r.user(args.user))]}})
    params = {"filters": json.dumps(filters)} if filters else None
    items = c.collect("/time_entries", params=params, limit=args.limit)
    if args.id:
        # There is no server-side work-package filter on this collection, so
        # narrow client-side against the entity link.
        items = [t for t in items if str(link(t, "workPackage", "id") or
                                         link(t, "entity", "id")) == str(args.id)]
    rows = [{"id": t.get("id"),
             "workPackage": link(t, "workPackage", "id") or link(t, "entity", "id"),
             "subject": link(t, "workPackage") or link(t, "entity"),
             "user": link(t, "user"),
             "hours": humanize_duration(t.get("hours")),
             "spentOn": t.get("spentOn"),
             "comment": (t.get("comment") or {}).get("raw", "")} for t in items]
    output(args, items if args.full else rows,
           columns=["id", "workPackage", "subject", "user", "hours", "spentOn", "comment"])
    if rows and not want_json(args) and not args.full:
        total = sum(float(re.sub(r"[^\d.]", "", h["hours"] or "0") or 0) for h in rows)
        note(paint(f"(total {total:g}h across {len(rows)} entries)", C.DIM))


def cmd_stats(args, c, r):
    params = {"filters": _filters(args, r), "pageSize": 200}
    items = c.collect("/work_packages", params=params)
    rows = [slim_wp(w) for w in items]
    if not rows:
        die("no work packages matched", 0)

    def tally(key, label):
        counts = Counter((row.get(key) or "(none)") for row in rows)
        return [{label: k, "count": v} for k, v in counts.most_common()]

    if want_json(args):
        print(json.dumps({
            "total": len(rows), "overdue": sum(1 for w in rows if is_overdue(w)),
            "unassigned": sum(1 for w in rows if not w.get("assignee")),
            "byStatus": tally("status", "status"), "byType": tally("type", "type"),
            "byAssignee": tally("assignee", "assignee"),
            "byProject": tally("project", "project"),
        }, indent=2))
        return

    print(paint(f"{len(rows)} work packages", C.BOLD)
          + paint(f"   {sum(1 for w in rows if is_overdue(w))} overdue"
                  f"   {sum(1 for w in rows if not w.get('assignee'))} unassigned", C.DIM))
    for key, label in (("status", "By status"), ("type", "By type"),
                       ("assignee", "By assignee"), ("project", "By project")):
        counts = Counter((row.get(key) or "(none)") for row in rows)
        if len(counts) <= 1 and key == "project":
            continue
        print("\n" + paint(label, C.DIM))
        width = max(len(str(k)) for k in counts)
        biggest = max(counts.values())
        for name, n in counts.most_common():
            bar = "#" * max(1, round(20 * n / biggest))
            print(f"  {str(name).ljust(width)}  {str(n).rjust(4)}  {paint(bar, C.CYAN)}")


def cmd_attach(args, c, r):
    path = Path(args.file)
    if not path.exists():
        die(f"file not found: {path}")
    content = path.read_bytes()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    boundary = "----opcli" + uuid.uuid4().hex
    meta = json.dumps({"fileName": path.name, "description": {"raw": args.description or ""}})
    parts = [f"--{boundary}\r\n".encode(),
             b'Content-Disposition: form-data; name="metadata"\r\n',
             b"Content-Type: application/json\r\n\r\n", meta.encode() + b"\r\n",
             f"--{boundary}\r\n".encode(),
             f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
             f"Content-Type: {ctype}\r\n\r\n".encode(), content + b"\r\n",
             f"--{boundary}--\r\n".encode()]
    _, res = c.request("POST", f"/work_packages/{args.id}/attachments",
                       raw_body=b"".join(parts),
                       ctype=f"multipart/form-data; boundary={boundary}")
    note(paint(f"Uploaded {path.name} to #{args.id}", C.GREEN))
    output(args, {"id": res.get("id"), "fileName": res.get("fileName"),
                  "fileSize": res.get("fileSize"), "contentType": res.get("contentType")})


def cmd_files(args, c, r):
    items = c.collect(f"/work_packages/{args.id}/attachments")
    output(args, items if args.full else
           [{"id": a.get("id"), "fileName": a.get("fileName"), "fileSize": a.get("fileSize"),
             "author": link(a, "author"),
             "when": (a.get("createdAt") or "")[:19].replace("T", " ")} for a in items],
           columns=["id", "fileName", "fileSize", "author", "when"])


def cmd_rm(args, c, r):
    if not args.yes:
        if not IS_TTY:
            die(f"refusing to delete {len(args.ids)} work package(s) without --yes")
        subjects = []
        for i in args.ids:
            wp = c.get(f"/work_packages/{i}")
            subjects.append(f"#{i} {wp.get('subject')}")
        for s in subjects:
            print("  " + s)
        if not confirm(f"Delete {len(args.ids)} work package(s)?"):
            die("cancelled", 0)
    for i in args.ids:
        c.request("DELETE", f"/work_packages/{i}")
    note(paint(f"Deleted {len(args.ids)} work package"
               f"{'s' if len(args.ids) != 1 else ''}", C.GREEN))
    if want_json(args):
        print(json.dumps({"deleted": args.ids}))


def cmd_projects(args, c, r):
    items = c.collect("/projects", limit=args.limit)
    rows = items if args.full else [
        {"id": p.get("id"), "identifier": p.get("identifier"), "name": p.get("name"),
         "active": p.get("active"), "public": p.get("public")} for p in items]
    output(args, rows, columns=["id", "identifier", "name", "active", "public"])


def cmd_types(args, c, r):
    proj = default_project(args)
    items = c.collect(f"/projects/{r.project(proj)}/types") if proj else c.collect("/types")
    output(args, items if args.full else
           [{"id": t["id"], "name": t.get("name"), "default": t.get("isDefault")} for t in items],
           columns=["id", "name", "default"])


def cmd_statuses(args, c, r):
    items = c.collect("/statuses")
    output(args, items if args.full else
           [{"id": s["id"], "name": s.get("name"), "closed": s.get("isClosed")} for s in items],
           columns=["id", "name", "closed"])


def cmd_priorities(args, c, r):
    items = c.collect("/priorities")
    output(args, items if args.full else
           [{"id": p["id"], "name": p.get("name")} for p in items], columns=["id", "name"])


def cmd_users(args, c, r):
    items = c.collect("/users", limit=args.limit)
    output(args, items if args.full else
           [{"id": u.get("id"), "login": u.get("login"), "name": u.get("name"),
             "email": u.get("email"), "admin": u.get("admin"), "status": u.get("status")}
            for u in items], columns=["id", "login", "name", "admin", "status"])


def cmd_web(args, c, r):
    url = f"{c.base}/work_packages/{args.id}" if args.id else c.base
    webbrowser.open(url)
    note(f"opened {url}")


def cmd_raw(args, c, r):
    body = None
    if args.data:
        src = args.data
        if src.startswith("@"):
            src = sys.stdin.read() if src == "@-" else Path(src[1:]).read_text(encoding="utf-8")
        try:
            body = json.loads(src)
        except json.JSONDecodeError as e:
            die(f"--data is not valid JSON: {e}")
    params = {}
    for kv in args.query or []:
        if "=" not in kv:
            die(f"--query expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        params[k] = v
    status, res = c.request(args.method.upper(), args.path, body=body, params=params or None)
    if isinstance(res, bytes):
        sys.stdout.buffer.write(res)
    else:
        print(json.dumps(res if res is not None else {"status": status},
                         indent=2, ensure_ascii=False, default=str))


BASH_COMPLETION = """\
_op_completions() {
  local cur prev
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  local cmds="init config cache ping whoami ls mine search show new edit close reopen \
assign comment history log time stats attach files rm projects types statuses \
priorities users web raw completion help"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "$cmds" -- "$cur") ); return
  fi
  case "$prev" in
    --project) COMPREPLY=( $(compgen -W "$(op projects --json 2>/dev/null | \
      python3 -c 'import sys,json;print(" ".join(p["identifier"] for p in json.load(sys.stdin)))' \
      2>/dev/null)" -- "$cur") ); return ;;
    --status) COMPREPLY=( $(compgen -W "open closed all" -- "$cur") ); return ;;
    --assign|--assignee|--to) COMPREPLY=( $(compgen -W "me" -- "$cur") ); return ;;
  esac
  COMPREPLY=( $(compgen -W "--json --table --full --limit --project --type --status \
--assignee --columns --overdue --unassigned --help" -- "$cur") )
}
complete -F _op_completions op
"""

PS_COMPLETION = """\
Register-ArgumentCompleter -Native -CommandName op -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @('init','config','cache','ping','whoami','ls','mine','search','show','new',
                  'edit','close','reopen','assign','comment','history','log','time','stats',
                  'attach','files','rm','projects','types','statuses','priorities','users',
                  'web','raw','completion','help')
    $flags = @('--json','--table','--full','--limit','--project','--type','--status',
               '--assignee','--columns','--overdue','--unassigned','--help')
    $tokens = $commandAst.CommandElements | ForEach-Object { $_.ToString() }
    $pool = if ($tokens.Count -le 1) { $commands } else { $commands + $flags }
    $pool | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
}
"""


def cmd_completion(args):
    shell = args.shell.lower()
    if shell == "bash":
        print(BASH_COMPLETION)
    elif shell in ("powershell", "pwsh", "ps"):
        print(PS_COMPLETION)
    else:
        die(f"no completion script for {args.shell!r}", hint="Supported: bash, powershell")
    note("")
    note("bash:        op completion bash >> ~/.bashrc")
    note("powershell:  op completion powershell >> $PROFILE")


QUICKSTART = """\
op - OpenProject from the command line

Setup
  op setup                               guided setup: URL, token, default
                                         project, PATH and tab completion
  op config set --project scrum          change the default project later

Look
  op ls                                  open work packages
  op ls --overdue                        past their due date
  op ls --unassigned --type bug
  op mine                                assigned to you
  op search "login"                      by subject, any status
  op show 15                             detail, description and comments
  op stats                               counts by status, type, assignee
  op web 15                              open in a browser

Change  (every id argument accepts several: op close 1 2 3)
  op new "Fix login bug" --type bug --assign me --due +1w
  op edit 15 --status "in progress" --percent 50
  op assign 15 sean          op assign 1 2 3 --to sean
  op close 15                op reopen 15
  op comment 15 "Deployed to staging"
  op log 15 2h "pairing on the fix"      log time
  op attach 15 ./shot.png
  op rm 15

Look up
  op projects   op types   op statuses   op priorities   op users   op time

Names work anywhere an id does: "scrum", "bug", "in progress", "me".
Dates accept today, tomorrow, +7d, +2w, or 2026-09-01.
Terminal gets a table, a pipe gets JSON:
  op ls --json | jq '.[].subject'

Escape hatch:  op raw GET /projects/1/types
Completion:    op completion powershell >> $PROFILE
"""


def cmd_help(args):
    print(QUICKSTART)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

COMMON = [
    (("--json",), {"action": "store_true", "help": "force JSON output"}),
    (("--table",), {"action": "store_true", "help": "force table output"}),
    (("--full",), {"action": "store_true", "help": "return the complete API response"}),
    (("--columns",), {"help": "comma-separated columns to show"}),
    (("--limit",), {"type": int, "help": "maximum records"}),
    (("--no-cache",), {"action": "store_true", "dest": "no_cache",
                       "help": "bypass the lookup cache"}),
    (("--color",), {"choices": ["auto", "always", "never"],
                    "help": "colour output (default auto)"}),
    (("--url",), {"help": "override the configured URL"}),
    (("--token",), {"help": "override the configured token"}),
    (("-v", "--verbose"), {"action": "store_true", "help": "log requests to stderr"}),
]


def add_common(p):
    existing = set(p._option_string_actions)
    group = None
    for flags, kw in COMMON:
        if any(f in existing for f in flags):
            continue
        if group is None:
            group = p.add_argument_group("output options")
        group.add_argument(*flags, default=argparse.SUPPRESS, **kw)


def propagate(p):
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sp in set(action.choices.values()):
                add_common(sp)
                propagate(sp)


def add_filters(p, with_query=True):
    p.add_argument("--project", help="project name or id, or 'all' (default from config)")
    p.add_argument("--all-projects", action="store_true", dest="all_projects",
                   help="ignore the configured default project")
    p.add_argument("--type", help="type name or id, e.g. bug")
    p.add_argument("--assignee", help="user name, login, id, or 'me'")
    p.add_argument("--status", help="open (default), closed, all, or a status name")
    p.add_argument("--overdue", action="store_true", help="only past their due date")
    p.add_argument("--unassigned", action="store_true", help="only without an assignee")
    p.add_argument("--due-before", dest="due_before", help="due on or before this date")
    p.add_argument("--sort", default="id", help="sort field (default id)")
    p.add_argument("--order", default="desc", choices=["asc", "desc"])
    if with_query:
        p.add_argument("--query", help="match subject text")


def build_parser():
    p = argparse.ArgumentParser(prog="op", add_help=False,
                                description="OpenProject from the command line.")
    p.add_argument("-h", "--help", action="store_true")
    add_common(p)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", aliases=["init"], help="guided first-time setup")
    s.add_argument("--url", help="instance URL, to skip the prompt")
    s.add_argument("--token", help="API token, to skip the prompt")
    s.add_argument("--project", help="default project, to skip the prompt")
    s.add_argument("--yes", "-y", action="store_true",
                   help="accept every default; needs --url and --token")
    s.add_argument("--skip-shell", action="store_true", dest="skip_shell",
                   help="do not touch PATH or shell profiles")
    s.set_defaults(func=cmd_setup, offline=True)

    s = sub.add_parser("config", help="show or change configuration")
    csub = s.add_subparsers(dest="subcmd")
    cs = csub.add_parser("set", help="change a setting")
    cs.add_argument("setting", nargs="?", help="setting name, e.g. defaultProject")
    cs.add_argument("value", nargs="?", help="new value; omit to clear it")
    cs.add_argument("--url"); cs.add_argument("--token")
    cs.add_argument("--project", help="shorthand for defaultProject")
    cs.add_argument("--clear-project", action="store_true", dest="clear_project")
    cs.set_defaults(func=cmd_config_set, offline=True)
    csub.add_parser("show", help="show every setting and where it came from").set_defaults(
        func=cmd_config_show, offline=True)
    csub.add_parser("path", help="print the config file path").set_defaults(
        func=cmd_config_path, offline=True)
    ce = csub.add_parser("edit", help="open the config file in your editor")
    ce.add_argument("--editor", help="editor command (default $EDITOR)")
    ce.set_defaults(func=cmd_config_edit, offline=True)
    s.set_defaults(func=cmd_config_show, offline=True)

    s = sub.add_parser("cache", help="inspect or clear the lookup cache")
    s.add_argument("action", nargs="?", default="show", choices=["show", "clear"])
    s.set_defaults(func=cmd_cache, offline=True)

    s = sub.add_parser("completion", help="print a shell completion script")
    s.add_argument("shell", choices=["bash", "powershell", "pwsh", "ps"])
    s.set_defaults(func=cmd_completion, offline=True)

    sub.add_parser("help", help="show the quickstart").set_defaults(func=cmd_help, offline=True)
    sub.add_parser("ping", help="check the connection").set_defaults(func=cmd_ping)
    sub.add_parser("whoami", help="show the current user").set_defaults(func=cmd_whoami)

    s = sub.add_parser("ls", aliases=["list"], help="list work packages")
    add_filters(s); s.set_defaults(func=cmd_ls)

    s = sub.add_parser("mine", help="assigned to you")
    add_filters(s); s.set_defaults(func=cmd_mine)

    s = sub.add_parser("search", help="find by subject")
    s.add_argument("text"); add_filters(s, with_query=False)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("stats", help="summarise work packages")
    add_filters(s); s.set_defaults(func=cmd_stats)

    s = sub.add_parser("show", aliases=["get"], help="show one work package")
    s.add_argument("id")
    s.add_argument("--web", action="store_true", help="open in a browser instead")
    s.add_argument("--comments", type=int, default=5, help="how many comments to show")
    s.add_argument("--no-comments", action="store_true", dest="no_comments")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("new", aliases=["create", "add"], help="create a work package")
    s.add_argument("subject")
    s.add_argument("--project", help="project name or id (default from config)")
    s.add_argument("--type", help="type name (default Task)")
    s.add_argument("--description", "-d", help="description, or - to read stdin")
    s.add_argument("--assign"); s.add_argument("--priority")
    s.add_argument("--due"); s.add_argument("--start"); s.add_argument("--parent")
    s.add_argument("--web", action="store_true", help="open it after creating")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("edit", aliases=["update"], help="change work packages")
    s.add_argument("ids", nargs="+")
    s.add_argument("--subject")
    s.add_argument("--description", "-d", help="description, or - to read stdin")
    s.add_argument("--status"); s.add_argument("--priority"); s.add_argument("--type")
    s.add_argument("--assign"); s.add_argument("--percent", type=int)
    s.add_argument("--due"); s.add_argument("--start")
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("close", help="close work packages")
    s.add_argument("ids", nargs="+"); s.set_defaults(func=cmd_close)

    s = sub.add_parser("reopen", help="reopen work packages")
    s.add_argument("ids", nargs="+"); s.set_defaults(func=cmd_reopen)

    s = sub.add_parser("assign", help="assign work packages")
    s.add_argument("args", nargs="+", metavar="ID... [WHO]")
    s.add_argument("--to", help="assignee, if not given as the last argument")
    s.set_defaults(func=cmd_assign)

    s = sub.add_parser("comment", help="comment on work packages")
    s.add_argument("args", nargs="+", metavar="ID... [TEXT]")
    s.add_argument("--text", help="comment body, if not given as the last argument")
    s.set_defaults(func=cmd_comment)

    s = sub.add_parser("history", aliases=["activities"], help="comments and changes")
    s.add_argument("id"); s.set_defaults(func=cmd_history)

    s = sub.add_parser("log", help="log time against a work package")
    s.add_argument("id"); s.add_argument("hours", help="2, 2h, 90m, 1h30m")
    s.add_argument("comment", nargs="?")
    s.add_argument("--on", help="date worked (default today)")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("time", help="list logged time")
    s.add_argument("id", nargs="?", help="limit to one work package")
    s.add_argument("--user", help="limit to one user, or 'me'")
    s.set_defaults(func=cmd_time)

    s = sub.add_parser("attach", help="upload a file")
    s.add_argument("id"); s.add_argument("file"); s.add_argument("--description")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("files", aliases=["attachments"], help="list attachments")
    s.add_argument("id"); s.set_defaults(func=cmd_files)

    s = sub.add_parser("rm", aliases=["delete"], help="delete work packages")
    s.add_argument("ids", nargs="+")
    s.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    s.set_defaults(func=cmd_rm)

    sub.add_parser("projects", help="list projects").set_defaults(func=cmd_projects)

    s = sub.add_parser("types", help="list types")
    s.add_argument("--project", help="only types enabled in this project")
    s.set_defaults(func=cmd_types)

    sub.add_parser("statuses", help="list statuses").set_defaults(func=cmd_statuses)
    sub.add_parser("priorities", help="list priorities").set_defaults(func=cmd_priorities)
    sub.add_parser("users", help="list users (admin only)").set_defaults(func=cmd_users)

    s = sub.add_parser("web", help="open in a browser")
    s.add_argument("id", nargs="?"); s.set_defaults(func=cmd_web)

    s = sub.add_parser("raw", help="call any API endpoint")
    s.add_argument("method"); s.add_argument("path")
    s.add_argument("--data", help="JSON body, @file.json, or @- for stdin")
    s.add_argument("--query", action="append", help="key=value, repeatable")
    s.set_defaults(func=cmd_raw)

    propagate(p)
    return p


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(QUICKSTART)
        return

    args = build_parser().parse_args(argv)
    if getattr(args, "help", False) or not getattr(args, "cmd", None):
        print(QUICKSTART)
        return

    for attr, default in (("full", False), ("table", False), ("json", False),
                          ("limit", None), ("verbose", False), ("no_cache", False),
                          ("columns", None), ("color", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)

    # Colour: flag beats config beats "auto".
    global USE_COLOR
    mode = (args.color or setting("color") or "auto").lower()
    if mode == "always":
        USE_COLOR = True
    elif mode == "never":
        USE_COLOR = False
    else:
        USE_COLOR = IS_TTY and not os.environ.get("NO_COLOR") \
            and os.environ.get("TERM") != "dumb"

    if args.limit is None:
        args.limit = setting("defaultLimit")

    if getattr(args, "offline", False):
        args.func(args)
        return

    url, token = resolve_credentials(args)
    client = Client(url, token, verbose=args.verbose, timeout=setting("timeout"))
    resolver = Resolver(client, Cache(url, enabled=not args.no_cache))
    args.func(args, client, resolver)
    if args.verbose:
        note(paint(f"({client.calls} API calls)", C.DIM))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        sys.exit(130)
    except EOFError:
        die("cancelled", 130)
