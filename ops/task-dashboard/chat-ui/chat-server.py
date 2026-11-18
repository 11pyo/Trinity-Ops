# -*- coding: utf-8 -*-
"""
chat-server.py — 「트리니티봇과 대화하기」 접수 서버 (사번 로그인 · 대화창)

    python -B task-dashboard/chat-ui/chat-server.py --test          → 시험: 포트 5183 · 시험 로그(_test/)
    python -B task-dashboard/chat-ui/chat-server.py                 → 운영 기록 · 포트 5179 (운영 반영 때만 — 담당자 결정)
    --port N 으로 포트를 바꿀 수 있다.

구조 — **운영 접수 서버의 업무 규칙을 다시 짜지 않는다.**
  `intake-server.py` 를 모듈로 불러(검증 장부 ledger.py 와 같은 방식) 그 Handler 를 이어받는다.
  접수 · 추가 질문 · 케이스 닫기 · 그림 검증 · 요청 제한 · 반영 확인은 운영 코드가 그대로 한다.
  이 서버가 더하는 것은 「누구인가」 하나다.
    · 사번 로그인 — 처음 한 번 사번 · 성함 · 소속 · 담당 업무를 등록(accounts.py), 다음부터 사번만으로.
    · 로그인하면 서버가 세션 토큰을 준다(메모리에만 — 디스크에 쓰지 않는다). 화면은 사번을 이 브라우저에 기억해 두고,
      서버가 다시 켜져 토큰이 사라지면 사번으로 조용히 다시 로그인한다.
    · 요청에 실린 토큰을 프로파일로 바꿔 **운영 Handler 가 기대하는 성함 · 소속을 서버가 채운다** — 화면이 보낸 성함 · 소속은 믿지 않는다.

대화가 남는 곳 — 서버를 껐다 켜도 그대로다.
  · 문의 · 답 · 추가 질문 · 종결 = `inquiry-log.js`(기존 그대로 — 대시보드 · 트리니티봇 · 장부가 읽는 정본)
  · 사람 = `…\\SAP SD\\_intake-profiles\\profiles.json`(accounts.py — 공유 아카이브 밖)
  · 기록의 요청자 칸(req)은 지금처럼 「성함·소속」이다 → 대시보드 · 트리니티봇 · 장부는 바뀌는 것이 없다.
    한 사람의 문의 = 그 사번이 쓴 적 있는 (성함, 소속) 으로 남긴 웹 접수 ∪ 이 서버로 남긴 문의 번호.

⛔ 지키는 것 (운영 접수 서버의 설계 원칙을 그대로 따른다)
  1. 쓰기는 신규 접수 · 본인 문의에 추가 질문 · 본인 문의 종결 + 내 프로파일 등록/수정뿐. 남의 문의 · 답 · 상태는 못 건드린다.
  2. 담당자 대시보드 · inquiry-log.js 원본 · 내부 메모는 내보내지 않는다(공개 응답은 운영의 _public_view 화이트리스트).
  3. 접수번호만으로 남의 문의를 보는 `/api/my` 는 열지 않는다.
"""
from http.cookies import SimpleCookie
import copy
import importlib.util
import io
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import accounts as ACC          # noqa: E402

DEMO = "--demo" in sys.argv     # 저장소의 sample-data/ 사본(.runtime/demo)으로 돈다 — 커밋된 표본은 건드리지 않는다
TEST = "--test" in sys.argv or DEMO


def _prepare_demo():
    """--demo: 코드(intake-server.py · log-inquiry.py)는 매번 새로, 문의 로그 · 프로파일은 처음 한 번만 표본에서 복사한다."""
    import shutil
    root = os.path.dirname(os.path.dirname(DASH))
    d = os.path.join(root, ".runtime", "demo")
    pf = os.path.join(root, ".runtime", "_intake-profiles-demo")
    os.makedirs(d, exist_ok=True)
    os.makedirs(pf, exist_ok=True)
    for f in ("intake-server.py", "log-inquiry.py"):
        shutil.copyfile(os.path.join(DASH, f), os.path.join(d, f))
    if not os.path.exists(os.path.join(d, "inquiry-log.js")):
        shutil.copyfile(os.path.join(root, "sample-data", "inquiry-log.js"), os.path.join(d, "inquiry-log.js"))
    if not os.path.exists(os.path.join(pf, "profiles.json")):
        shutil.copyfile(os.path.join(root, "sample-data", "profiles.json"), os.path.join(pf, "profiles.json"))
    return d, pf


_DEMO_DIR, _DEMO_PROFILES = _prepare_demo() if DEMO else (None, None)
SRC = _DEMO_DIR if DEMO else (os.path.join(HERE, "_test") if TEST else DASH)
PORT = 5183 if TEST else 5179          # 시험 포트 — 회귀 시험들이 5185~5199 를 쓴다(unit_role 은 5199). 겹치면 시험이 이 화면을 받아 실패한다
if "--port" in sys.argv:
    PORT = int(sys.argv[sys.argv.index("--port") + 1])
HOST = "127.0.0.1" if DEMO else "0.0.0.0"                # 현업 사내망 접수 — 운영 접수 서버와 같다

# 운영 접수 서버를 모듈로 — main() 은 부르지 않는다
_spec = importlib.util.spec_from_file_location("intake_core_chat", os.path.join(SRC, "intake-server.py"))
IX = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(IX)
if TEST:                        # 시험 첨부는 운영 첨부와 섞지 않는다(공유본 밖 · 별도 폴더)
    IX.SHOT_DIR = os.path.join(ACC.WORK, ".runtime", "_intake-attachments-chat-test")

STORE = ACC.Store(_DEMO_PROFILES if DEMO else ACC.folder(TEST))
COOKIE_NAME = "sd_chat_tok"
SESSION_SEC = 12 * 3600
LOGIN_MAX, SIGNUP_MAX = 20, 5           # 분당 · IP 당
PIN_FAIL_MAX, PIN_FAIL_WIN = 5, 300     # 5분당 5회 실패 시 잠시 차단
DEDUP_TTL, DEDUP_MAX = 300, 1000
SMALL_MAX = 8 * 1024
REQ_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_sess, _sess_lock = {}, threading.Lock()
_dedup, _dedup_lock = {}, threading.Lock()
_pin_fails, _pin_lock = {}, threading.Lock()


def _build_allowed_hosts():
    hosts = {"127.0.0.1", "localhost", "::1"}
    try:
        hosts.add(socket.gethostname().lower())
        hosts.add(socket.getfqdn().lower())
    except OSError:
        pass
    try:
        lan = IX._lan_ip()
        if lan:
            hosts.add(str(lan).lower())
    except Exception:                               # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            if info and info[4]:
                hosts.add(str(info[4][0]).lower())
    except OSError:
        pass
    return frozenset(h for h in hosts if h)


ALLOWED_HOSTS = _build_allowed_hosts()

LIB_START = 'var LS="sd-intake-me-v1";'
LIB_END = 'var qEd = document.getElementById("q");'
SLOT = "/*__INTAKE_LIB__*/"


def build_page():
    """대화창 화면 = chat-page.html + 운영 화면의 입력칸 스크립트(그대로) + 서버 상수. 자리가 1곳이 아니면 멈춘다."""
    src = io.open(os.path.join(SRC, "intake-server.py"), encoding="utf-8").read()
    page = io.open(os.path.join(HERE, "chat-page.html"), encoding="utf-8").read()
    for text, needle in ((src, LIB_START), (src, LIB_END), (page, SLOT)):
        if text.count(needle) != 1:
            raise SystemExit("[X] 화면 조립 자리가 1곳이 아닙니다: %r" % needle[:50])
    lib = src[src.index(LIB_START):src.index(LIB_END)].rstrip() + "\n"
    page = page.replace(SLOT, lib)
    return (page.replace("__SHOT_MAX__", str(IX.SHOT_MAX_COUNT))
                .replace("__SHOT_BYTES__", str(IX.SHOT_MAX_BYTES))
                .replace("__MAX_Q__", str(IX.MAX_Q)))


PAGE = build_page()


# ─────────────────────────────────────────────── 세션 (메모리에만)
def new_token(emp):
    # [SECURE] Cryptographically secure session token, memory only - never written to disk (Category 2)
    tok = secrets.token_urlsafe(24)
    now = time.time()
    with _sess_lock:
        for k in [k for k, v in _sess.items() if v[1] < now]:
            del _sess[k]
        _sess[tok] = (emp, now + SESSION_SEC)
    return tok


def session(tok):
    if not isinstance(tok, str) or not (16 <= len(tok) <= 64):
        return None
    with _sess_lock:
        v = _sess.get(tok)
    if not v or v[1] < time.time():
        return None
    return STORE.get(v[0])


def revoke_token(tok):
    if not isinstance(tok, str):
        return
    with _sess_lock:
        _sess.pop(tok, None)


def _pin_rate_ok(ip, emp):
    now = time.time()
    key = (ip, emp)
    with _pin_lock:
        arr = [t for t in _pin_fails.get(key, []) if now - t < PIN_FAIL_WIN]
        if arr:
            _pin_fails[key] = arr
        else:
            _pin_fails.pop(key, None)
        return len(arr) < PIN_FAIL_MAX


def _pin_fail_add(ip, emp):
    now = time.time()
    key = (ip, emp)
    with _pin_lock:
        arr = [t for t in _pin_fails.get(key, []) if now - t < PIN_FAIL_WIN]
        arr.append(now)
        _pin_fails[key] = arr


def _pin_fail_clear(ip, emp):
    with _pin_lock:
        _pin_fails.pop((ip, emp), None)


# ─────────────────────────────────────────────── 이 사람의 문의
def owned(p):
    """이 사번의 웹 접수 — 쓴 적 있는 (성함, 소속) 으로 남긴 것 ∪ 이 서버로 남긴 번호."""
    keys = set(IX._norm(IX._who(n, o)) for n, o in ACC.aliases(p))
    ids = set(p.get("ids") or [])
    return dict((k, v) for k, v in IX._records_cached().items()
                if k in ids or IX._norm(v.get("req", "")) in keys)


def split_req(req):
    """기록의 「성함·소속」 → (성함, 소속). 운영 Handler 가 다시 이어 붙여 같은 글자가 된다."""
    i = (req or "").find("·")
    return (req, "") if i < 0 else (req[:i], req[i + 1:])


def _resolve_owner_req(p, rec):
    """이 사번의 aliases 에서 정확한 (성함, 소속) 을 먼저 찾고, 없을 때만 split_req 로 fallback 한다."""
    req = rec.get("req", "") if isinstance(rec, dict) else ""
    target = IX._norm(req)
    for n, o in ACC.aliases(p):
        if IX._norm(IX._who(n, o)) == target:
            return n, o
    return split_req(req)


def mine(p):
    rows = sorted(owned(p).values(), key=lambda r: r.get("id", ""), reverse=True)
    return [IX._public_view(r) for r in rows]


def pulse(p):
    out = []
    for v in owned(p).values():
        fresh = IX._answer_fresh(v)
        out.append({"id": v.get("id", ""), "rev": IX._answer_rev(v), "fresh": fresh,
                    "line": IX._answer_line(v) if fresh else ""})
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


# ─────────────────────────────────────────────── 서버
class Handler(IX.Handler):
    server_version = "SDChat/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        # [SECURE] Session tokens are masked in the access log (Category 2)
        line = re.sub(r"tok=[^&\s\"]+", "tok=***", fmt % args)
        sys.stdout.write("  %s  %s\n" % (self.client_address[0], line))

    def end_headers(self):
        # [SECURE] Framing, MIME sniffing, referrer and CSP hardening on every response (Category 1)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        for ck in getattr(self, "_pending_cookies", ()):
            self.send_header("Set-Cookie", ck)
        self._pending_cookies = []
        super().end_headers()

    def _queue_cookie(self, val, max_age=None):
        parts = ["%s=%s" % (COOKIE_NAME, val), "Path=/", "HttpOnly", "SameSite=Strict"]
        if max_age is not None:
            parts.append("Max-Age=%d" % int(max_age))
        if isinstance(self.connection, ssl.SSLSocket):
            parts.append("Secure")
        self._pending_cookies = list(getattr(self, "_pending_cookies", ())) + ["; ".join(parts)]

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        ck = SimpleCookie()
        try:
            ck.load(raw)
        except Exception:                           # noqa: BLE001
            return ""
        m = ck.get(COOKIE_NAME)
        return m.value if m else ""

    def _req_token(self, qs=None, body=None):
        tok = (self.headers.get("X-Chat-Token") or "").strip()
        if not tok:
            tok = self._cookie_token()
        if not tok and isinstance(body, dict):
            tok = body.get("tok") or ""
        if not tok and qs:
            tok = (qs.get("tok") or [""])[0]
        return tok if isinstance(tok, str) else ""

    def _host_ok(self):
        # [SECURE] Host allowlist blocks DNS rebinding attacks (Category 1)
        hdr = (self.headers.get("Host") or "").strip().lower()
        if not hdr:
            return False
        if hdr.startswith("["):
            end = hdr.find("]")
            if end < 0:
                return False
            host = hdr[1:end]
            rest = hdr[end + 1:]
            if rest and (not rest.startswith(":") or not rest[1:].isdigit()):
                return False
        elif ":" in hdr:
            host, port_s = hdr.rsplit(":", 1)
            if not port_s.isdigit():
                return False
        else:
            host = hdr
        return host in ALLOWED_HOSTS

    def _json(self, code, obj):
        self._out = obj                                   # 운영 Handler 가 낸 결과를 받아 둔다(새 접수 번호를 프로파일에 잇는다)
        return super()._json(code, obj)

    def _auth(self):
        return self._json(200, {"ok": False, "auth": True, "error": "다시 로그인해 주십시오."})

    # ── GET ──
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "text/plain; charset=utf-8", "허용되지 않은 Host 요청입니다.")
        u = urlparse(self.path)
        path, ip, qs = u.path, self.client_address[0], parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE)
        tok = self._req_token(qs=qs)
        if path == "/api/me":
            p = session(tok)
            if not p:
                return self._auth()
            return self._json(200, {"ok": True, "profile": ACC.public(p)})
        if path in ("/api/mine", "/api/pulse"):
            bucket, lim = ("lookup", IX.LOOKUP_MAX) if path == "/api/mine" else ("pulse", IX.PULSE_MAX)
            if not IX._rate_ok(ip, bucket, lim):
                return self._json(200, {"ok": False, "error": "잠시 후 다시 시도해 주세요."})
            p = session(tok)
            if not p:
                return self._auth()
            return self._json(200, {"ok": True, "items": mine(p) if path == "/api/mine" else pulse(p)})
        if path == "/api/shot":
            p = session(tok)
            iid = (qs.get("id") or [""])[0].strip().upper()
            rec = owned(p).get(iid) if p else None
            if not rec:
                return self._send(403, "text/plain; charset=utf-8", "본인 문의가 아닙니다.")
            name, org = _resolve_owner_req(p, rec)
            # [SECURE] Identity for the production handler comes from the server-side profile, never from the client (Category 2)
            self.path = "/api/shot?" + urlencode({"id": iid, "n": (qs.get("n") or ["0"])[0], "name": name, "org": org})
            return super().do_GET()
        # ⛔ /api/my(접수번호만으로 조회) · 대시보드 · 로그 원본은 내보내지 않는다
        return self._send(404, "text/plain; charset=utf-8", "없는 경로입니다.")

    # ── POST ──
    def _same_origin(self):
        # [SECURE] Strict CSRF defense: JSON content type + same-origin or X-Requested-With + Sec-Fetch-Site check (Category 1)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs and sfs not in ("same-origin", "none"):
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        origin = self.headers.get("Origin")
        if origin:
            return urlparse(origin).netloc.lower() == host
        return (self.headers.get("X-Requested-With") or "").strip() == "SDChat"

    def _read(self, limit):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > limit:
            self._drain()
            return None
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        return d if isinstance(d, dict) else None

    def do_POST(self):
        if not self._host_ok():
            self._drain()
            return self._send(403, "text/plain; charset=utf-8", "허용되지 않은 Host 요청입니다.")
        path, ip = urlparse(self.path).path, self.client_address[0]
        if not self._same_origin():
            self._drain()
            return self._json(400, {"ok": False, "error": "요청을 받지 않습니다."})
        try:
            if path in ("/api/login", "/api/signup", "/api/profile", "/api/pin", "/api/logout"):
                d = self._read(SMALL_MAX)
                if d is None:
                    return self._json(200, {"ok": False, "error": "요청을 읽지 못했습니다."})
                return self._account(path, ip, d)
            if path in ("/api/new", "/api/followup", "/api/close"):
                return self._delegate(path, ip)
        except Exception as e:                                       # noqa: BLE001
            # [SECURE] Generic message to the browser, detail to the server console only (Category 4)
            sys.stdout.write("  [!] %s 실패: %s\n" % (path, e))
            return self._json(200, {"ok": False, "error": "처리하지 못했습니다. 잠시 후 다시 시도해 주세요."})
        return self._json(404, {"ok": False, "error": "없는 경로입니다."})

    def _account(self, path, ip, d):
        if path == "/api/logout":
            tok = self._req_token(body=d)
            revoke_token(tok)
            self._queue_cookie("", max_age=0)
            return self._json(200, {"ok": True})
        if path in ("/api/profile", "/api/pin"):
            p = session(self._req_token(body=d))
            if not p:
                return self._auth()
            emp = p["emp"]
        else:
            # [SECURE] Rate limit on sign-in and sign-up - slows id enumeration (Category 2)
            if not IX._rate_ok(ip, path[5:], LOGIN_MAX if path == "/api/login" else SIGNUP_MAX):
                return self._json(200, {"ok": False, "error": "잠시 후 다시 시도해 주세요."})
            emp = ACC.norm_emp(d.get("emp"))
            if not emp:
                return self._json(200, {"ok": False, "error": "사번을 확인해 주세요 (영문 · 숫자 3~20자)."})
        if path == "/api/login":
            p = STORE.get(emp)
            if not p:
                return self._json(200, {"ok": False, "signup": True})
            if ACC.has_pin(p):
                if not _pin_rate_ok(ip, emp):
                    return self._json(200, {"ok": False, "need_pin": True,
                                            "error": "PIN 시도가 너무 많습니다. 5분 후 다시 시도해 주세요."})
                pin = d.get("pin") or ""
                if not pin:
                    return self._json(200, {"ok": False, "need_pin": True})
                if not ACC.verify_pin(p, pin):
                    _pin_fail_add(ip, emp)
                    return self._json(200, {"ok": False, "need_pin": True, "error": "PIN 번호가 맞지 않습니다."})
                _pin_fail_clear(ip, emp)
            STORE.touch(emp)
            tok = new_token(emp)
            self._queue_cookie(tok)
            return self._json(200, {"ok": True, "tok": tok, "profile": ACC.public(p)})
        if path == "/api/pin":
            if ACC.has_pin(p):
                if not _pin_rate_ok(ip, emp):
                    return self._json(200, {"ok": False, "error": "PIN 시도가 너무 많습니다. 5분 후 다시 시도해 주세요."})
                if not ACC.verify_pin(p, d.get("cur_pin") or ""):
                    _pin_fail_add(ip, emp)
                    return self._json(200, {"ok": False, "error": "현재 PIN 번호가 맞지 않습니다."})
                _pin_fail_clear(ip, emp)
            new_pin = (d.get("new_pin") or "").strip()
            if not new_pin:
                updated = STORE.clear_pin(emp)
            else:
                if not ACC.norm_pin(new_pin):
                    return self._json(200, {"ok": False, "error": "새 PIN 은 숫자 4~8자리여야 합니다."})
                updated = STORE.set_pin(emp, new_pin)
            if not updated:
                return self._auth()
            return self._json(200, {"ok": True, "profile": ACC.public(updated)})
        name, org, role = (ACC.clean(d.get("name"), ACC.MAX_NAME), ACC.clean(d.get("org"), ACC.MAX_ORG),
                           ACC.clean(d.get("role"), ACC.MAX_ROLE))
        if not name:
            return self._json(200, {"ok": False, "error": "성함을 입력해 주세요."})
        if not org:
            return self._json(200, {"ok": False, "error": "소속을 입력해 주세요."})
        if not role:
            return self._json(200, {"ok": False, "error": "담당 업무를 입력해 주세요. 담당에 따라 안내가 달라집니다 (예: 영업대표, 여신담당)."})
        if path == "/api/signup":
            pin_raw = (d.get("pin") or "").strip()
            if pin_raw and not ACC.norm_pin(pin_raw):
                return self._json(200, {"ok": False, "error": "선택 PIN 은 숫자 4~8자리여야 합니다."})
            try:
                p = STORE.create(emp, name, org, role, pin=pin_raw or None)
            except ACC.Exists:
                return self._json(200, {"ok": False, "error": "이미 등록된 사번입니다. 사번만 넣고 들어와 주십시오. "
                                                              "본인이 등록한 적이 없다면 담당자에게 알려 주십시오."})
            tok = new_token(emp)
            self._queue_cookie(tok)
            sys.stdout.write("  [+] 등록 %s  %s·%s [%s]\n" % (emp, name, org, role))
            return self._json(200, {"ok": True, "tok": tok, "profile": ACC.public(p),
                                    "earlier": len(owned(p))})
        p = STORE.update(emp, name, org, role)
        if not p:
            return self._auth()
        sys.stdout.write("  [+] 프로파일 수정 %s  %s·%s [%s]\n" % (emp, name, org, role))
        return self._json(200, {"ok": True, "profile": ACC.public(p)})

    def _delegate(self, path, ip):
        """토큰 → 프로파일 → 운영 Handler 가 기대하는 성함 · 소속(· 담당 업무)을 **서버가** 채워 넘긴다."""
        d = self._read(IX.BODY_MAX)
        if d is None:
            return self._json(200, {"ok": False, "error": "요청을 읽지 못했습니다 (사진이 너무 크거나 많습니다)."})
        tok = self._req_token(body=d)
        d.pop("tok", None)
        p = session(tok)
        if not p:
            return self._auth()
        req_id = d.pop("req_id", None)
        if not isinstance(req_id, str) or not REQ_ID_RE.match(req_id):
            req_id = None
        dedup_key = (p["emp"], path, req_id) if (req_id and path in ("/api/new", "/api/followup")) else None
        if dedup_key:
            now = time.time()
            with _dedup_lock:
                for k in [k for k, v in _dedup.items() if now - v[0] > DEDUP_TTL]:
                    del _dedup[k]
                hit = _dedup.get(dedup_key)
                if hit:
                    if hit[1] == "pending":
                        return self._json(200, {"ok": False, "error": "같은 요청을 처리 중입니다. 잠시만 기다려 주세요."})
                    return self._json(200, copy.deepcopy(hit[2]))
                if len(_dedup) >= DEDUP_MAX:
                    oldest = min(_dedup, key=lambda k: _dedup[k][0])
                    del _dedup[oldest]
                _dedup[dedup_key] = (now, "pending", None)
        try:
            if path == "/api/new":
                d["name"], d["org"], d["role"] = p["name"], p["org"], p.get("role", "")
            else:
                rec = owned(p).get(str(d.get("id") or "").strip().upper())
                if not rec:
                    return self._json(200, {"ok": False, "error": "본인이 등록하신 문의에만 이어서 쓰거나 닫으실 수 있습니다."})
                d["name"], d["org"] = _resolve_owner_req(p, rec)
            body = json.dumps(d, ensure_ascii=False).encode("utf-8")
            self.rfile = io.BytesIO(body)
            del self.headers["Content-Length"]
            self.headers["Content-Length"] = str(len(body))
            self._out = None
            IX.Handler.do_POST(self)                                 # 운영 규칙 그대로(요청 제한 · 그림 검증 · 기록 · 반영 확인)
            out = self._out if isinstance(self._out, dict) else {}
            if dedup_key and out.get("ok"):
                with _dedup_lock:
                    _dedup[dedup_key] = (time.time(), "done", copy.deepcopy(out))
            elif dedup_key:
                with _dedup_lock:
                    _dedup.pop(dedup_key, None)
            if path == "/api/new" and out.get("ok") and out.get("id"):
                try:
                    STORE.link(p["emp"], [out["id"]])
                except Exception as e:                               # noqa: BLE001 — 문의는 들어갔다. 잇기만 실패(성함 · 소속으로도 찾는다)
                    sys.stdout.write("  [!] 문의 번호를 프로파일에 잇지 못했습니다 %s %s: %s\n" % (p["emp"], out["id"], e))
        except Exception:
            if dedup_key:
                with _dedup_lock:
                    _dedup.pop(dedup_key, None)
            raise


def main():
    if not os.path.exists(IX.HELPER):
        print("  [X] log-inquiry.py 를 찾지 못했습니다: %s" % IX.HELPER)
        return 1
    STORE.all()                                                      # 프로파일 파일을 미리 한 번 읽는다(깨졌으면 여기서 정리)
    try:
        srv = IX.SingleServer((HOST, PORT), Handler)
    except OSError as e:
        print("  [X] 포트 %d 를 열 수 없습니다: %s — 이미 떠 있는 서버가 없는지 확인하십시오." % (PORT, e))
        return 1
    threading.Thread(target=IX._purge_loop, daemon=True).start()    # 종결된 문의의 첨부 정리 — 운영 규칙 그대로
    tls = ""
    if not TEST:
        try:
            ctx = IX._tls_context()
            if ctx:
                tsrv = IX.TLSServer((HOST, IX.TLS_PORT), Handler)
                tsrv.ctx = ctx
                threading.Thread(target=tsrv.serve_forever, daemon=True).start()
                tls = " · HTTPS %d" % IX.TLS_PORT
        except Exception as e:                                       # noqa: BLE001
            print("  [!] HTTPS 를 열지 못했습니다(http 는 정상): %s" % e)
    print("")
    print("  트리니티봇과 대화하기 — 접수 서버%s" % (" ★ 시험 (시험 로그 · 시험 프로파일) ★" if TEST else ""))
    print("  ------------------------------------------------------")
    print("  현업 화면 : http://%s:%d/%s" % (IX._lan_ip(), PORT, tls))
    print("  문의 기록 : %s" % IX.LOG_JS)
    print("  프로파일  : %s" % STORE.path)
    print("  · 사번으로 로그인합니다(처음 한 번 성함 · 소속 · 담당 업무 등록).")
    print("  · 종료하려면 이 창에서 Ctrl+C 를 누르세요.")
    print("")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료했습니다.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# --------------------------------------------------
# Security Checklist
# Applied:
#   - Improper Authorization: every inquiry read/write is checked against the signed-in profile on the server; client-sent name/org are ignored
#   - Session tokens: secrets-generated, memory only, 12 h expiry, HttpOnly SameSite=Strict cookie (plus optional header), never leaked in /api/shot URL, masked in access log
#   - CSRF: JSON content type + Origin matches Host (or X-Requested-With header when Origin absent) + Sec-Fetch-Site check on every POST
#   - DNS Rebinding: Host header allowlist (127.0.0.1, localhost, local hostname/FQDN/LAN IP) on every GET and POST
#   - Missing Rate Limit on Auth: sign-in 20/min and sign-up 5/min per IP, plus 5/5min PIN brute-force throttle
#   - Idempotency: 5-min (emp, path, req_id) deduplication prevents duplicate ticket creation on retry
#   - Input validation: employee id and PIN whitelist, free text cleaned and capped (accounts.py); production validation reused for inquiries and images
#   - Error Message Information Exposure: generic messages to the browser, details to the console
#   - Data exposure: /api/my (lookup by inquiry number alone) is not served; public fields still come from the production _public_view whitelist
#   - Content-Security-Policy and X-Content-Type-Options: nosniff set on all responses
#   - Business rules: production handler code is reused unchanged (no re-implementation drift)
# Not Applied:
#   - [WARN] Missing Auth for Critical Function when user does not configure an optional PIN (intranet employee id login by design)
#   - [WARN] Account enumeration: /api/login reveals whether an employee id is registered (needed to show sign-up or PIN prompt); rate-limited only
#   - [WARN] Unencrypted transport on plain http (LAN); HTTPS only where the production TLS certificate exists
#   - [WARN] Inline script and style remain allowed in CSP ('unsafe-inline') because production input widget JS is injected inline
# --------------------------------------------------
