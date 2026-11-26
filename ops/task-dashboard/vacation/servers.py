# -*- coding: utf-8 -*-
"""
servers.py — 휴가모드가 켜고 지키는 두 서버(접수 5179 · 대시보드 저장 5178)를 한 곳에서 띄운다.

2026-09-11 사용자 지시: 「두 서버를 별개로 관리할 필요 없이 휴가모드(트리니티봇 활성화)만 켜면 다 알아서.
부팅 자동시작은 필요 없고 휴가모드일 때만 유지되면 된다. 휴가 중 부득이 재부팅되더라도 휴가모드는 유지되고
서버는 자동 복구되게.」
  · 켜기(mode.py on) · 재개(mode.py resume — 작업 스케줄러 「SD 트리니티봇 감시」가 C: 재개 런처로 부른다) · 감시자 1분 점검
    (watcher.health_check) 이 모두 여기를 부른다. 서버를 띄우는 방법이 한 곳에만 있어야 셋이 어긋나지 않는다.
  · ⚠️ **창 없이** 띄운다(CREATE_NO_WINDOW) — 무인 구성요소를 닫을 수 있는 창으로 띄우지 않는다(실수 137).
    대신 출력은 런타임 폴더 `<key>-server.log` 에 남긴다(죽은 이유를 볼 수 있게).
  · 끄는 길은 그대로다 — 제어판 서버 카드 「종료」(7/9 배치파일이 포트로 찾아 끈다).
  · 서버 코드는 손대지 않는다. 배치파일(6/8)이 하던 것과 같은 명령(`python <script> [args]`)을 창 없이 부를 뿐이다.
"""

import io
import os
import socket
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)

SERVERS = (
    {"key": "intake", "name": "접수 서버", "port": 5179,
     "script": os.path.join("chat-ui", "chat-server.py"), "args": ["--noopen"]},       # --noopen: 켤 때마다 브라우저 탭이 뜨지 않게
    {"key": "saver", "name": "대시보드 저장 서버", "port": 5178,
     "script": "board-saver.py", "args": []},
    {"key": "desk", "name": "통합 데스크 허브", "port": 5177,
     "script": os.path.join("desk", "desk-server.py"), "args": []},
)

LOG_MAX = 5 * 1024 * 1024          # 서버 로그가 이보다 크면 켤 때 .1 로 밀어 둔다


def alive(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(port, path="/", timeout=4.0):
    """포트가 열려 있는 것만으로는 부족하다 — **실제 HTTP 응답**이 오는지 본다(2026-10-08).

    G: 가 끊겼다 돌아온 뒤 접수 서버가 포트는 열어 둔 채 모든 요청에 빈 응답만 돌려준 일이 있다(서버 로그가 G: 에 열려 있어
    요청마다 로그를 쓰다 실패한 것으로 추정). 포트 확인(alive)은 그 상태를 정상으로 봐서, 현업 창구가 멈춘 채 아무도 몰랐다.
    상태 코드는 가리지 않는다(저장 서버는 / 에 404 를 돌려주는 게 정상) — 응답 줄이 오면 살아 있다."""
    import http.client
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        r.read(64)
        c.close()
        return 100 <= r.status < 600
    except Exception:                                           # noqa: BLE001
        return False


def pid_on_port(port):
    """그 포트를 듣고 있는 프로세스의 PID. 못 찾으면 0. (netstat — WMI 가 아니라 RPC 시간초과에 안 걸린다)"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20,
                             creationflags=0x08000000).stdout or ""
    except Exception:                                           # noqa: BLE001
        return 0
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[3].upper() == "LISTENING" and p[1].endswith(":%d" % port):
            try:
                return int(p[4])
            except ValueError:
                return 0
    return 0


def kill_listener(port):
    """포트를 듣는 프로세스를 끝낸다. 끝낸 PID(없으면 0)."""
    pid = pid_on_port(port)
    if pid and pid != os.getpid():
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=20, creationflags=0x08000000)
        except Exception:                                       # noqa: BLE001
            return 0
        for _ in range(20):
            if not alive(port, 0.2):
                break
            time.sleep(0.25)
        return pid
    return 0


def console_python():
    """pythonw.exe 가 아니라 python.exe 로 띄운다 — pythonw 자식은 표준출력이 없어 첫 print 에서 죽는다(2026-09-10)."""
    exe = sys.executable or "python"
    d, b = os.path.split(exe)
    if b.lower() == "pythonw.exe":
        cand = os.path.join(d, "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


# ── 띄운 쪽과 수명을 끊어서 띄우기 (2026-09-30 · 어느 AI 호스트에서 켜도 살아남게) ─────────────────
#  휴가모드는 제어판뿐 아니라 Claude Code · Gemini(안티그래비티) · Codex 같은 AI 세션이 `mode.py on` 으로 켤 수 있다.
#  그런 호스트는 명령을 윈도 「작업 개체(Job)」 안에서 돌리다가 명령이 끝나면 작업을 닫아 **자식까지 정리**할 수 있다
#  → 감시자·접수 서버가 켜지자마자 같이 꺼진다. 그래서 두 단계로 띄운다.
#   ① 작업에서 떼어 내며 띄운다(CREATE_BREAKAWAY_FROM_JOB). 작업 밖이면 아무 효과가 없고(제어판 경로 = 종전과 같음),
#      떼어 내기가 허용된 작업이면 여기서 끝난다.
#   ② 떼어 내기가 금지된 작업이면(접근 거부) WMI 로 띄운다 — 부모가 WMI 서비스라 부른 쪽 작업과 무관하다.
#      작은 중계 프로세스(`servers.py --spawn …`)가 작업 밖에서 창 없이 먼저 뜨고, ①과 같은 방식으로 진짜 프로세스를 띄운 뒤 끝난다.
#  검증 = tests/unit_detach.py (떼어 내기를 막은 작업 개체 안에서 켠 뒤 작업을 닫아도 살아 있는지 · 창이 안 뜨는지).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACH_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def _popen(argv, cwd, env, out_path, err_path, flags):
    """표준출력은 **반드시 파일이나 NUL 로** 준다 — 비워 두면 부른 쪽 파이프를 물려받아 부른 쪽이 영영 기다린다."""
    out = io.open(out_path, "ab") if out_path else subprocess.DEVNULL
    err = subprocess.STDOUT if out_path else (io.open(err_path, "ab") if err_path else subprocess.DEVNULL)
    try:
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=flags).pid
    finally:
        for fh in (out, err):
            if hasattr(fh, "close"):
                fh.close()


def _popen_breakaway(argv, cwd, env, out_path, err_path):
    """①: 작업에서 떼어 내며 띄운다. 떼어 내기가 금지된 작업이면 OSError(winerror 5)."""
    return _popen(argv, cwd, env, out_path, err_path, DETACH_FLAGS | CREATE_BREAKAWAY_FROM_JOB)


def _wmi_spawn(argv, cwd, out_path, err_path):
    """②: WMI(Win32_Process.Create)로 중계 프로세스를 작업 밖에서 창 없이 띄운다. 중계 프로세스 pid 를 돌려준다."""
    boot = [console_python(), os.path.abspath(__file__), "--spawn", out_path or "-", err_path or "-", cwd, "--"] + list(argv)
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          "@{CommandLine=$env:WPB_CL; CurrentDirectory=$env:WPB_CWD; ProcessStartupInformation=$si}; "
          "Write-Output ('{0} {1}' -f $r.ReturnValue, $r.ProcessId)")
    env = dict(os.environ, WPB_CL=subprocess.list2cmdline(boot), WPB_CWD=cwd)   # 인자는 환경변수로 — 따옴표 문제 차단
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                       capture_output=True, text=True, timeout=90, creationflags=CREATE_NO_WINDOW)
    parts = (r.stdout or "").split()
    if len(parts) < 2 or parts[0] != "0":
        raise OSError("WMI 로 띄우지 못했습니다: %s %s" % ((r.stdout or "").strip()[:200], (r.stderr or "").strip()[:200]))
    return int(parts[1])


def spawn_detached(argv, cwd, env=None, out_path=None, err_path=None):
    """무인 구성요소를 창 없이, **띄운 쪽이 끝나거나 정리돼도 살아남게** 띄운다. (방식, pid) — 방식 = 'direct' | 'wmi'.

    out_path 를 주면 표준출력·오류를 그 파일에 이어 쓴다. 안 주면 표준출력은 버리고 오류만 err_path 에(있으면).
    ⚠️ 'wmi' 방식의 pid 는 중계 프로세스 것이다 — 살아 있는지는 포트·하트비트로 본다(종전과 같다).
    """
    env = dict(os.environ if env is None else env)
    try:
        return "direct", _popen_breakaway(argv, cwd, env, out_path, err_path)
    except OSError as e:
        if getattr(e, "winerror", None) != 5:          # 떼어 내기 금지 = 접근 거부(5). 그 밖의 실패는 그대로 알린다
            raise
    return "wmi", _wmi_spawn(argv, cwd, out_path, err_path)


def _spawn_main(args):
    """중계 모드: servers.py --spawn <out|-> <err|-> <cwd> -- <argv…> — WMI 가 작업 밖에서 띄운 자리에서 진짜 프로세스를 띄우고 끝난다."""
    out_path, err_path, cwd = [None if a == "-" else a for a in args[:3]]
    argv = args[4:] if len(args) > 3 and args[3] == "--" else args[3:]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        _popen_breakaway(argv, cwd, env, out_path, err_path)
    except OSError as e:                               # WMI 쪽이 또 다른 작업 안이면 — 떼어 내기 없이라도 띄운다
        if getattr(e, "winerror", None) != 5:
            raise
        _popen(argv, cwd, env, out_path, err_path, DETACH_FLAGS)
    return 0


def start(srv, log_dir, wait_sec=12, dash=DASH):
    """꺼져 있으면 창 없이 띄우고 포트가 응답할 때까지 기다린다. (성공 여부, 설명)."""
    if alive(srv["port"]):
        if http_ok(srv["port"]) or http_ok(srv["port"]):         # 한 번 더 — 잠깐 느린 것과 반쯤 죽은 것을 가른다
            return True, "이미 켜져 있음"
        gone = kill_listener(srv["port"])                       # 포트는 열렸는데 응답이 없다 — 정리하고 다시 띄운다
        if not gone:
            return False, "포트는 열려 있으나 응답이 없고 그 프로세스를 찾지 못했습니다"
        time.sleep(1.0)
    script = os.path.join(dash, srv["script"])
    if not os.path.exists(script):
        return False, "%s 를 찾지 못했습니다" % srv["script"]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"                               # 죽기 직전 출력까지 로그에 남게
    try:
        os.makedirs(log_dir, exist_ok=True)
        lp = os.path.join(log_dir, "%s-server.log" % srv["key"])
        try:
            if os.path.getsize(lp) > LOG_MAX:
                os.replace(lp, lp + ".1")
        except OSError:
            pass
        with io.open(lp, "ab") as fh:
            fh.write(("\n==== %s 기동 (휴가모드) ====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        # ⚠️ 표준출력을 **반드시 파일로** 준다. 비워 두면 부른 쪽(제어판의 capture_output)의 파이프를 물려받아,
        #    서버가 살아 있는 동안 제어판이 mode.py 가 끝나기를 영영 기다린다.
        # 2026-09-30~ 띄운 쪽(AI 호스트 등)이 정리돼도 살아남게 spawn_detached 로 띄운다.
        spawn_detached([console_python(), script] + list(srv["args"]), cwd=dash, env=env, out_path=lp)
    except Exception as e:                                      # noqa: BLE001
        return False, "띄우지 못했습니다: %s" % e
    for _ in range(int(wait_sec / 0.5)):
        time.sleep(0.5)
        if alive(srv["port"]):
            return True, "기동 확인"
    return False, "띄웠지만 %d초 안에 응답하지 않았습니다 (로그: %s-server.log)" % (wait_sec, srv["key"])


def start_all(log_dir, wait_sec=12):
    """두 서버를 차례로 켠다. [(srv, 성공, 설명), …]"""
    return [(srv,) + start(srv, log_dir, wait_sec) for srv in SERVERS]


if __name__ == "__main__":
    # 중계 모드만 있다(spawn_detached 의 WMI 경로가 부른다). 사람이 직접 쓸 일은 없다.
    if sys.argv[1:2] == ["--spawn"]:
        sys.exit(_spawn_main(sys.argv[2:]))
    print("servers.py 는 모듈입니다 — mode.py 가 부릅니다.")
