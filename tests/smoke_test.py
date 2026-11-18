#!/usr/bin/env python3
"""smoke_test.py — 데모 구성이 실제로 도는지 확인한다 (커밋된 표본은 절대 바꾸지 않는다).

    python -B tests/smoke_test.py

1. 저장소를 임시 폴더로 통째로 복사한다(.git · .runtime 제외) — 모든 서버 · 쓰기는 그 사본에서만 일어난다.
2. 모든 .py 를 compile() 로 문법 점검(.pyc 를 만들지 않는다).
3. 사본에서 아카이브 · ERD 뷰어를 빌드한다(PyYAML 이 없으면 아카이브 빌드는 건너뛴다).
4. 접수 창구(chat-server.py --demo)와 문의 데스크(desk-server.py --demo)를 빈 포트로 띄운다.
5. 화면 200 · 사번 로그인 · /api/mine · 추가 질문 POST(사본 로그에 기록) · 남의 문의 차단 · Host 차단 · 데스크 허브 경로 · 판정 기록.
6. 회귀 시험 chat-ui/tests/reproduce_defects.py(임시 폴더 · 임시 포트) 실행.
7. 원본 저장소의 sample-data/ 가 처음과 바이트 단위로 같은지 확인.
트리니티봇(Claude Code CLI 호출)은 여기서 절대 돌리지 않는다.
"""
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {".git", ".runtime", "__pycache__"}
FAILS = []


def check(cond, what):
    print("  [%s] %s" % ("OK" if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def digest(d):
    h = hashlib.sha256()
    for dp, dirs, files in sorted(os.walk(d)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, d).encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, proc, sec=40):
    end = time.time() + sec
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def req(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": host or "127.0.0.1:%d" % port}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw.decode("utf-8"))
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")


def start(cmd, cwd, port):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    log = open(os.path.join(cwd, ".runtime-%d.log" % port), "w", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-B"] + cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=env)
    return p, log


def main():
    before = digest(os.path.join(ROOT, "sample-data"))
    tmp = tempfile.mkdtemp(prefix="trinity_smoke_")
    work = os.path.join(tmp, "repo")
    shutil.copytree(ROOT, work, ignore=lambda d, names: [n for n in names if n in SKIP])
    procs = []
    try:
        print("1) 문법 점검")
        bad = 0
        for dp, dirs, files in os.walk(work):
            dirs[:] = [x for x in dirs if x not in SKIP]
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(dp, f)
                    try:
                        compile(open(p, encoding="utf-8").read(), p, "exec")
                    except SyntaxError as e:
                        bad += 1
                        print("     %s: %s" % (os.path.relpath(p, work), e))
        check(bad == 0, "모든 .py 컴파일")

        print("2) 아카이브 · ERD 뷰어 빌드(사본)")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        try:
            import yaml  # noqa: F401
            r = subprocess.run([sys.executable, "-B", "ops/build_archive.py"], cwd=work, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env)
            check(r.returncode == 0 and os.path.isfile(os.path.join(work, "ops", "archive.html")), "build_archive.py → archive.html")
        except ImportError:
            print("  [SKIP] PyYAML 이 없어 아카이브 빌드를 건너뜁니다 (pip install pyyaml)")
        r = subprocess.run([sys.executable, "-B", "ops/erd-graph/viewer_builder.py"], cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        check(r.returncode == 0 and os.path.isfile(os.path.join(work, "ops", "erd-graph", "erd-viewer.html")), "viewer_builder.py → erd-viewer.html")

        print("3) 접수 창구(chat-server --demo)")
        cp = free_port()
        p, lg = start(["ops/task-dashboard/chat-ui/chat-server.py", "--demo", "--port", str(cp), "--noopen"], work, cp)
        procs.append((p, lg))
        check(wait_port(cp, p), "chat-server 기동 (포트 %d)" % cp)
        st, page = req(cp, "GET", "/")
        check(st == 200 and "<html" in str(page).lower(), "GET / → 200")
        st, _ = req(cp, "GET", "/", host="evil.example:%d" % cp)
        check(st in (400, 403), "다른 Host 차단 (%s)" % st)
        xh = {"X-Requested-With": "SDChat"}
        st, j = req(cp, "POST", "/api/login", {"emp": "90000001"}, xh)
        tok = j.get("tok") if isinstance(j, dict) else None
        check(st == 200 and bool(tok), "표본 사번 90000001 로그인")
        th = dict(xh, **{"X-Chat-Token": tok or ""})
        st, j = req(cp, "GET", "/api/mine", headers=th)
        ids = [x.get("id") for x in (j.get("items") or [])] if isinstance(j, dict) else []
        check(st == 200 and "INQ-260110-153000" in ids and "INQ-260106-101500" not in ids, "/api/mine = 본인 문의만 (%d건)" % len(ids))
        st, j = req(cp, "GET", "/api/mine")
        check(not (isinstance(j, dict) and j.get("ok")), "토큰 없는 /api/mine 거부")
        demo_log = os.path.join(work, ".runtime", "demo", "inquiry-log.js")
        size0 = os.path.getsize(demo_log)
        st, j = req(cp, "POST", "/api/followup", {"id": "INQ-260110-153000", "text": "SMOKE 추가 질문입니다 — 취소 후 재청구도 되나요?",
                                                  "req_id": "smoke-followup-0001"}, th)
        check(st == 200 and isinstance(j, dict) and j.get("ok"), "추가 질문 POST → ok (%s)" % (j if not isinstance(j, dict) else j.get("error", "ok")))
        txt = open(demo_log, encoding="utf-8").read()
        check(os.path.getsize(demo_log) > size0 and "SMOKE 추가 질문" in txt, "추가 질문이 사본 로그(.runtime/demo)에 기록됨")
        st, j = req(cp, "POST", "/api/followup", {"id": "INQ-260106-101500", "text": "남의 문의에 쓰기 시도"}, th)
        check(isinstance(j, dict) and not j.get("ok"), "남의 문의에 추가 질문 거부")
        st, j = req(cp, "POST", "/api/followup", {"id": "INQ-260110-153000", "text": "CSRF"},
                    {"Origin": "http://evil.example", "X-Chat-Token": tok or ""})
        check(not (isinstance(j, dict) and j.get("ok")), "다른 출처(Origin) POST 거부")

        print("4) 문의 데스크(desk-server --demo)")
        dp = free_port()
        p2, lg2 = start(["ops/task-dashboard/desk/desk-server.py", "--demo", "--port", str(dp)], work, dp)
        procs.append((p2, lg2))
        check(wait_port(dp, p2), "desk-server 기동 (포트 %d)" % dp)
        for path in ("/", "/board", "/metrics", "/bot-metrics", "/erd", "/inquiry-log.js"):
            st, body = req(dp, "GET", path)
            check(st == 200, "GET %s → %s" % (path, st))
        st, body = req(dp, "GET", "/board")
        check("SAMPLE" in str(body) and "보기 전용" in str(body), "/board = 표본 TASKS + 보기 전용 띠")
        st, j = req(dp, "GET", "/api/overview")
        check(st == 200 and isinstance(j, dict), "/api/overview → JSON")
        st, _ = req(dp, "GET", "/", host="evil.example:%d" % dp)
        check(st == 403, "데스크 다른 Host 차단")
        st, j = req(dp, "POST", "/api/grade", {"id": "INQ-260110-153000", "grade": "맞음", "note": "SMOKE"}, {"X-Requested-With": "SDDesk"})
        txt = open(demo_log, encoding="utf-8").read()
        check(st == 200 and isinstance(j, dict) and j.get("ok") and '"botgrade": "맞음"' in txt, "판정 기록(log-inquiry.py 경유)")

        print("5) 회귀 시험 reproduce_defects.py")
        r = subprocess.run([sys.executable, "-B", "ops/task-dashboard/chat-ui/build_test.py"], cwd=work, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env)
        check(r.returncode == 0, "build_test.py → chat-ui/_test 사본")
        r = subprocess.run([sys.executable, "-B", "ops/task-dashboard/chat-ui/tests/reproduce_defects.py"], cwd=work, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, timeout=400)
        out = r.stdout + r.stderr
        summ = out[out.find("=== SUMMARY ==="):] if "=== SUMMARY ===" in out else out[-800:]
        for ln in summ.strip().splitlines():
            print("     " + ln)
        npass, nfail = summ.count(": PASS"), summ.count(": FAIL")
        check(r.returncode == 0 and npass >= 8 and nfail == 0 and "Traceback" not in out,
              "reproduce_defects.py %d PASS / %d FAIL" % (npass, nfail))
    finally:
        for p, lg in procs:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            lg.close()
        if FAILS:
            for f in os.listdir(work):
                if f.startswith(".runtime-") and f.endswith(".log"):
                    print("---- %s (끝 15줄)" % f)
                    print("\n".join(open(os.path.join(work, f), encoding="utf-8", errors="replace").read().splitlines()[-15:]))
        shutil.rmtree(tmp, ignore_errors=True)
    check(digest(os.path.join(ROOT, "sample-data")) == before, "커밋된 sample-data/ 변경 없음")
    print("SMOKE %s — 실패 %d건" % ("PASS" if not FAILS else "FAIL", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
