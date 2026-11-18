# -*- coding: utf-8 -*-
"""
board-saver.py — 대시보드 「💾 저장」 전용 로컬 저장 서버 (127.0.0.1 전용)

목적: task-board.html 을 브라우저에서 수정하고 💾 저장을 누를 때
      「다른 이름으로 저장」 창 없이 **원본 파일을 그 자리에서 덮어쓰기**.
      (브라우저 File System Access API 는 보안 모델상 첫 저장·브라우저 재시작마다 사용자 승인이
       필요해 "묻지 않는 덮어쓰기"가 원천적으로 불가능하다. 그래서 로컬 서버가 대신 파일에 쓴다.)

실행:  python board-saver.py         (또는 프로젝트 루트의 6_대시보드_저장서버.bat)
종료:  Ctrl+C                        (또는 7_대시보드_저장서버_종료.bat)

⛔ 보안 — 이 서버는 파일을 덮어쓰는 쓰기 API 다.
   · 반드시 127.0.0.1 에만 바인딩한다. archive-server.py 의 --share(LAN 공개) 같은 옵션을 절대 만들지 말 것.
   · 요청 IP 가 127.0.0.1 이 아니면 거부. 커스텀 헤더(X-Board-Saver) 를 요구해 단순 POST 유입을 차단.
   · (2026-11-17 보강) 출처 제한 — 브라우저 Origin 은 "null"(파일로 연 대시보드)만 허용, 응답도 * 대신 그 Origin.
     Origin 이 없는 요청(파이썬 상태 점검 등 브라우저 밖 로컬 프로세스)은 통과. 그 밖의 Origin 은 403.
   · (2026-11-17 보강) 공유 토큰 — Origin null 은 아무 웹페이지나 sandbox iframe 으로 만들 수 있어 출처 제한만으론
     우회된다. 그래서 쓰기(POST)는 ?t=<토큰> 이 _saver_token.js 의 값과 같아야 한다. 대시보드는 같은 폴더의
     _saver_token.js 를 <script> 로 읽어 붙인다. 외부 웹페이지는 로컬 파일을 읽을 수 없으므로 토큰을 모른다.
     토큰 파일은 없을 때만 한 번 만들고(구글 드라이브로 회사·집 PC 가 같은 값 공유), 요청마다 다시 읽는다.
     ⛔ _saver_token.js 를 어떤 HTTP 서버(desk-server 경로표·archive-server 등)로도 서빙하지 말 것 — 토큰이 샌다.
   · 쓰기 전 형식 검증 + 백업(.prev / _backups 일자별) + 원자적 교체(os.replace).

엔드포인트
  GET  /ping           → {"ok":true, ...}            (대시보드가 서버 가동 여부 탐지)
  POST /save/board     본문 = task-board.html 전문     → 검증·백업 후 그 자리 덮어쓰기
  POST /save/inquiry   본문 = window.INQUIRY_LOG.push(...) 줄들 → append-only 추가(log-inquiry.py 와 동일 규약)
"""
import io, json, os, re, shutil, sys, datetime, threading, secrets, hmac
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HOST, PORT = "127.0.0.1", 5178          # ⛔ HOST 는 절대 0.0.0.0 으로 바꾸지 말 것
BASE       = os.path.dirname(os.path.abspath(__file__))
BOARD      = os.path.join(BASE, "task-board.html")
INQ        = os.path.join(BASE, "inquiry-log.js")
BACKUP_DIR = os.path.join(BASE, "_backups")
KEEP_DAILY = 7                          # 보드 일자별 스냅샷 보관 개수(1.7MB×N)
_wlock     = threading.Lock()           # 서버 내부 동시 쓰기 방지
TOKEN_FILE = os.path.join(BASE, "_saver_token.js")   # ⛔ HTTP 로 서빙 금지(머리말 보안 참조)
ALLOWED_ORIGINS = {"null"}              # 파일(file://)로 연 task-board.html 이 보내는 Origin
_TOKEN_RE  = re.compile(r'BOARD_SAVER_TOKEN\s*=\s*"([A-Za-z0-9_\-]{32,})"')


def _log(msg):
    print(datetime.datetime.now().strftime("[%H:%M:%S] ") + msg, flush=True)


def _backup(path, daily_name):
    """쓰기 직전 스냅샷: 최근본(.prev) + 일자별 1회(_backups/). 실패해도 저장은 계속."""
    made = ""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return made
        shutil.copyfile(path, path + ".prev")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        daily = os.path.join(BACKUP_DIR, daily_name)
        if not os.path.exists(daily):
            shutil.copyfile(path, daily)
            made = os.path.basename(daily)
    except Exception as e:
        _log("[!] 백업 실패(저장은 진행): %s" % e)
    return made


def _prune_daily(prefix, suffix, keep):
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith(prefix) and f.endswith(suffix))
        if len(files) > keep:
            for f in files[:-keep]:
                os.remove(os.path.join(BACKUP_DIR, f))
    except Exception:
        pass


def _read_token():
    """_saver_token.js 의 토큰. 요청마다 읽는다(드라이브 동기화로 파일이 바뀌어도 대시보드와 같은 값을 본다)."""
    try:
        m = _TOKEN_RE.search(io.open(TOKEN_FILE, encoding="utf-8").read())
        return m.group(1) if m else ""
    except Exception:
        return ""


def ensure_token():
    """토큰 파일이 없거나 깨졌을 때만 새로 만든다(있으면 그대로 — 회사·집 PC 가 같은 값을 쓴다)."""
    if _read_token():
        return False
    tok = secrets.token_urlsafe(32)
    _atomic_write(TOKEN_FILE,
                  "// board-saver.py 쓰기 토큰 — 자동 생성. 외부 공유·HTTP 서빙 금지. 지우면 서버 재시작 때 새로 만들고,\n"
                  "// 그 뒤엔 열려 있던 대시보드를 새로고침(F5)해야 저장된다.\n"
                  "window.BOARD_SAVER_TOKEN = \"%s\";\n" % tok)
    return True


def _atomic_write(path, text):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)                       # 같은 볼륨 → 원자적 교체


def save_board(text):
    """task-board.html 전체 덮어쓰기. 형식·크기 검증을 통과해야만 쓴다."""
    if "const TASKS = [" not in text or 'id="editbar"' not in text:
        return False, "보드 형식이 아닙니다(TASKS 배열/편집바 없음) — 저장 중단"
    if "</html>" not in text[-2000:]:
        return False, "문서 끝이 잘렸습니다(</html> 없음) — 저장 중단"
    n = len(text.encode("utf-8"))
    if n < 300000 or n > 30000000:
        return False, "크기가 비정상입니다(%d bytes) — 저장 중단" % n
    if os.path.exists(BOARD):
        old = os.path.getsize(BOARD)
        if old and n < old * 0.5:
            return False, "새 내용이 원본의 절반 이하(%d→%d) — 유실 의심으로 저장 중단" % (old, n)
    with _wlock:
        made = _backup(BOARD, "task-board.%s.html" % datetime.date.today().strftime("%Y%m%d"))
        _atomic_write(BOARD, text)
        _prune_daily("task-board.", ".html", KEEP_DAILY)
    return True, {"bytes": n, "backup": made}


def save_inquiry(text):
    """inquiry-log.js 에 push() 줄 추가(append-only). log-inquiry.py 와 같은 규약."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False, "추가할 내용이 없습니다"
    if len(text) > 1000000:
        return False, "본문이 너무 큽니다 — 저장 중단"
    for l in lines:                                   # 한 줄이라도 규약 위반이면 전부 거부
        if not (l.startswith("window.INQUIRY_LOG.push(") and l.endswith(");")):
            return False, "허용되지 않는 줄이 있습니다(push 규약 위반) — 저장 중단"
        try:
            json.loads(l[len("window.INQUIRY_LOG.push("):-2])
        except Exception:
            return False, "push 인자가 올바른 JSON 이 아닙니다 — 저장 중단"
    with _wlock:
        if not os.path.exists(INQ):
            _atomic_write(INQ, "window.INQUIRY_LOG = window.INQUIRY_LOG || [];\n")
        cur = io.open(INQ, encoding="utf-8").read()
        if "window.INQUIRY_LOG" not in cur:
            return False, "inquiry-log.js 형식이 아닙니다 — 저장 중단"
        made = _backup(INQ, "inquiry-log.%s.js" % datetime.date.today().strftime("%Y%m%d"))
        with open(INQ, "a", encoding="utf-8", newline="") as f:   # 헬퍼와 동일한 append 방식
            f.write("\n".join(lines) + "\n")
    return True, {"lines": len(lines), "backup": made}


class Handler(BaseHTTPRequestHandler):
    server_version = "BoardSaver/1.0"

    def log_message(self, fmt, *args):
        pass                                     # 접근 로그는 _log 로 직접 찍는다

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Vary", "Origin")
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:                                      # * 대신 허용된 Origin 만 되돌려 준다
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Board-Saver")
            self.send_header("Access-Control-Allow-Private-Network", "true")   # 크롬 PNA 프리플라이트 대비
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, {"ok": False, "err": "로컬 전용"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            _log("[거부] 허용되지 않은 출처 %r → %s %s" % (origin[:80], self.command, self.path.split("?")[0]))
            self._send(403, {"ok": False, "err": "허용되지 않은 출처 — 파일로 연 대시보드에서만 저장할 수 있습니다"})
            return False
        return True

    def do_OPTIONS(self):
        if self._guard():
            self._send(200, {"ok": True})

    def do_GET(self):
        if not self._guard():
            return
        if self.path.split("?")[0] == "/ping":
            self._send(200, {"ok": True, "board": BOARD, "inquiry": INQ, "version": 1})
        else:
            self._send(404, {"ok": False, "err": "no such path"})

    def do_POST(self):
        if not self._guard():
            return
        path = self.path.split("?")[0]
        if self.headers.get("X-Board-Saver") != "1":
            self._send(400, {"ok": False, "err": "헤더 누락 — 대시보드에서만 저장할 수 있습니다"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
        except Exception as e:
            self._send(400, {"ok": False, "err": "본문 수신 실패: %s" % e})
            return
        # 토큰 검사는 본문을 다 받은 뒤에 — 안 읽고 닫으면 브라우저가 연결 끊김(=서버 꺼짐)으로 오판해 파일 선택창으로 폴백한다
        given = (parse_qs(urlsplit(self.path).query).get("t") or [""])[0]
        want = _read_token()
        if not want or not hmac.compare_digest(given.encode("utf-8"), want.encode("utf-8")):
            _log("[거부] 토큰 불일치/누락 → %s" % path)               # 토큰 값은 로그에 남기지 않는다
            self._send(403, {"ok": False, "err": "저장 토큰이 맞지 않습니다 — 대시보드를 새로고침(F5)한 뒤 다시 저장하세요"})
            return
        try:
            if path == "/save/board":
                ok, res = save_board(body)
                label = "task-board.html"
            elif path == "/save/inquiry":
                ok, res = save_inquiry(body)
                label = "inquiry-log.js"
            else:
                self._send(404, {"ok": False, "err": "no such path"})
                return
        except Exception as e:
            _log("[X] 저장 실패(%s): %s" % (path, e))
            self._send(500, {"ok": False, "err": str(e)})
            return
        if ok:
            _log("[OK] %s 저장됨 — %s" % (label, res))
            self._send(200, dict({"ok": True}, **res))
        else:
            _log("[거부] %s — %s" % (label, res))
            self._send(422, {"ok": False, "err": res})


def main():
    print("=" * 62)
    print("  대시보드 저장 서버 (task-board.html / inquiry-log.js)")
    print("  주소: http://%s:%d   (이 PC 전용 · LAN 공개 안 함)" % (HOST, PORT))
    print("  대상: %s" % BASE)
    print("  이 창이 켜져 있는 동안 대시보드의 💾 저장이 원본을 그 자리에서 덮어씁니다.")
    print("  종료: Ctrl+C  (또는 7_대시보드_저장서버_종료.bat)")
    print("=" * 62)
    try:
        if ensure_token():
            print("  쓰기 토큰 파일을 새로 만들었습니다: %s" % TOKEN_FILE)
    except Exception as e:
        print("\n  [X] 쓰기 토큰 파일을 만들 수 없습니다: %s — 저장은 모두 거부됩니다" % e)
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("\n  [X] 포트 %d 를 열 수 없습니다: %s" % (PORT, e))
        print("      이미 저장 서버가 켜져 있을 수 있습니다(그러면 그대로 쓰면 됩니다).")
        return 1
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  종료합니다.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
