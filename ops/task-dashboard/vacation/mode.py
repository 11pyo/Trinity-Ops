# -*- coding: utf-8 -*-
"""
mode.py — 부재중(휴가) 모드 켜기·끄기·상태. 제어판 버튼이 이 파일을 부른다.

  python mode.py on      부재중 모드 진입 (기준선 기록 → 두 서버 · PC 유지 설정 · 감시자)
  python mode.py resume  재개 런처(작업 스케줄러 「SD 트리니티봇 감시」 → C: 의 trinitybot-resume/resume.py)가 죽은 것을 보면 부른다 — 휴가모드가 켜져 있을 때만 두 서버 · 감시자를 되살린다
  python mode.py off     복귀 (감시자 정지 → 설정 원복 → 복귀 보고서 생성 · 두 서버는 끄지 않는다)

  ⚠️ 두 서버(접수 5179 · 대시보드 저장 5178)는 2026-09-11 부터 부팅 자동시작이 없다 — **휴가모드가 켜고 지킨다**
     (servers.py 가 창 없이 띄우고, 감시자가 1분마다 확인해 되살리며, 재부팅 뒤엔 resume 이 다시 켠다).
  python mode.py status  현재 상태를 JSON 으로 출력 (제어판이 읽는다)
  python mode.py report  보고서만 다시 만든다

⛔ 원칙
  · **켜는 순간 이미 답이 있는 문의는 건드리지 않는다**(사람 답 = 영구 제외, AI 답 = 지문 고정).
    ⚠️ 단 **켜기 전에 들어와 아직 아무도 답하지 않은 문의는 처리한다**(2026-09-10 사용자 지적 —
    예전엔 켜기 전 문의를 전부 「이미 답함」으로 심어, 답을 기다리던 건이 영영 방치됐다).
    기준선(baseline_id)은 seen.json 이 유실됐을 때 **이미 AI 가 답한 건을 두 번 답하지 않게** 막는 용도로만 쓴다.
  · 끄기는 **상태 파일을 off 로 바꾸는 것**이 정본이다. 감시자는 그걸 보고 스스로 멈춘다.
    프로세스를 죽이는 건 보조 수단일 뿐(죽여도 상태가 on 이면 워치독이 되살린다).
  · 보고서는 **Claude 를 부르지 않는다**(비용 0). journal 을 읽어 정리할 뿐이다.
"""

import collections
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
ROOT = os.path.dirname(DASH)
sys.path.insert(0, HERE)
sys.path.append(DASH)                                           # stale_cases(문의자 종결 점검) — 뒤에 붙여 같은 이름 모듈을 가리지 않게
import apply_answer as GATE                                     # noqa: E402
import servers as SRV                                           # noqa: E402
import ledger as LEDGER                                         # noqa: E402
import engines as ENG                                           # noqa: E402  strip_host_env — 감시자에 사람 세션 환경이 새지 않게

CFG = GATE._cfg()
RT = CFG["runtime_dir"]
LAST_REPORT_HTML = ""                  # 마지막으로 만든 복귀 보고서의 HTML 경로(없으면 빈 문자열) — report() 가 채운다
P_STATE = os.path.join(RT, "state.json")


def _load(p, d=None):
    if os.path.exists(p):
        try:
            return json.load(io.open(p, encoding="utf-8"))
        except ValueError:
            pass
    return dict(d or {})


def _save(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="").write(
        json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


# 재개 런처(resume_launcher.py — C: 에 설치되어 작업 스케줄러가 5분마다 부른다)가 읽는 로컬 표지.
# G: 가 아직 안 붙은 재부팅 직후에도 「휴가모드가 켜져 있었나」를 알아야 하므로 state.json(G:) 이 아니라 C: 에 둔다.
# 표지가 없으면 런처는 아무것도 하지 않는다 — 휴가모드가 꺼져 있을 때 재부팅이 서버를 멋대로 켜면 안 된다.
RESUME_HOME = os.environ.get("TRINITYBOT_RESUME_HOME") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".runtime", "bot-resume")
RESUME_FLAG = os.path.join(RESUME_HOME, "vacation.on")


def _set_resume_flag(on):
    """표지를 만들거나 지운다. 실패해도 휴가모드 자체는 계속 — 다만 재부팅 자동 재개가 안 되니 알린다."""
    try:
        if on:
            os.makedirs(RESUME_HOME, exist_ok=True)
            io.open(RESUME_FLAG, "w", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S"))
        elif os.path.exists(RESUME_FLAG):
            os.remove(RESUME_FLAG)
        return True
    except OSError as e:
        print("  ⚠ 재부팅 자동 재개 표지를 %s 하지 못했습니다(%s)" % ("만들지" if on else "지우지", e))
        return False


def _intake():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "intake_mod", os.path.join(DASH, "intake-server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _watcher_running():
    """감시자가 살아 있나 — **잠금 파일을 잡아 볼 수 있는지**로 본다. 잡히면(=아무도 안 쥐고 있으면) 없음.

    ⚠️ 예전엔 WMI(Get-CimInstance)로 프로세스를 찾았는데, 이 PC 에서 WMI 가 자주 시간초과돼
       「없음」으로 보고 감시자를 하나 더 띄울 수 있었다(2026-09-11). 잠금 확인은 즉시 끝나고 틀리지 않는다.
       잠금 파일이 없던 옛 감시자(이 변경 전에 뜬 것)를 위해서만 WMI 를 보조로 쓴다.
    """
    lock = GATE.lock_path(CFG, RT)                  # 운영은 로컬(C:) — G: 끊김에 잠금이 풀리지 않게(2026-10-08)
    if os.path.exists(lock):
        import msvcrt
        try:
            fh = io.open(lock, "a+b")
        except OSError:
            fh = None
        if fh is not None:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                try:
                    return [int(io.open(os.path.join(os.path.dirname(lock), "watcher.pid"), encoding="utf-8").read().strip())]
                except (OSError, ValueError):
                    return [-1]                         # 누군가 쥐고 있다 — PID 는 모름
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.close()
            return []
    return _watcher_running_wmi()


def _watcher_running_wmi():
    """(보조) 명령줄에 watcher.py 가 있는 python 을 WMI 로 찾는다 — 느리고 시간초과될 수 있다."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*watcher.py*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, encoding="cp949", errors="ignore",
            creationflags=0x08000000, timeout=15).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def _port_alive(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _console_python():
    """⚠️ 반드시 python.exe 로 띄운다 — pythonw.exe 가 아니라.

    제어판은 pythonw.exe 로 돈다. 그 sys.executable 을 그대로 물려주면 자식도 pythonw 로
    뜨는데, explorer 가 띄운 pythonw 에는 표준출력이 없다(sys.stdout = None).
    감시자가 첫 로그를 찍는 순간 죽고 하트비트도 안 남아, 화면에는 「켜짐 · 감시 끊김」으로만
    보였다(2026-09-10 실측 — 제어판 버튼으로 켤 때만 재현됐고, 셸에서 켜면 멀쩡했다).
    """
    exe = sys.executable or "python"
    d, b = os.path.split(exe)
    if b.lower() == "pythonw.exe":
        cand = os.path.join(d, "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def _heartbeat_age():
    hb = GATE.heartbeat_path(CFG)
    if not os.path.exists(hb):
        return None
    try:
        return time.time() - os.path.getmtime(hb)
    except OSError:
        return None


def _start_watcher(wait_sec=20):
    """감시자를 띄우고 **정말 살아났는지 하트비트로 확인**한다.

    ⚠️ 배치파일을 os.startfile 로 부르는 방식은 호출 맥락에 따라 조용히 실패한다
       (2026-09-10 실측: 요청은 갔는데 프로세스가 안 떴다). 그래서 파이썬을
       새 콘솔로 직접 띄우고, 하트비트 파일이 갱신되는 것까지 봐야 성공으로 친다.
    """
    if _watcher_running():
        return True, "감시자 이미 실행 중"
    before = _heartbeat_age()
    try:
        env = ENG.strip_host_env(dict(os.environ))      # AI 세션이 심은 인증 위임 변수가 감시자·트리니티봇으로 새지 않게(2026-10-07)
        env["PYTHONIOENCODING"] = "utf-8"
        # ⚠️ **창 없이** 띄운다(CREATE_NO_WINDOW). 예전엔 새 콘솔 창으로 띄웠는데, 그 창을 닫으면 감시자가
        #    그대로 죽는다 — 2026-09-11 07:57 실제로 창이 닫혀 멈췄고 아무도 몰랐다. 끄는 정식 경로는 상태 파일(off)
        #    뿐이고 감시자가 그걸 보고 스스로 멈춘다. 살아 있는지는 제어판 하트비트로 본다.
        #    (pythonw 가 아니라 python.exe 로 띄운다 · 표준출력은 NUL — 감시자 기록은 watcher.log 에 따로 남는다)
        # 2026-09-30~ 띄운 쪽이 정리돼도 살아남게 servers.spawn_detached 로 띄운다 — 제어판뿐 아니라
        #    Claude Code · Gemini(안티그래비티) · Codex 세션이 `mode.py on` 을 불러도 명령이 끝날 때 같이 꺼지지 않게.
        #    잡히지 않은 예외(감시자가 죽은 이유)는 watcher-stderr.log 에 남는다.
        SRV.spawn_detached([_console_python(), os.path.join(HERE, "watcher.py")], cwd=HERE, env=env,
                           err_path=os.path.join(RT, "watcher-stderr.log"))
    except Exception as e:
        return False, "감시자를 띄우지 못했습니다: %s" % e
    for _ in range(int(wait_sec / 0.5)):
        time.sleep(0.5)
        age = _heartbeat_age()
        if age is not None and age < 15 and (before is None or age < before):
            return True, "감시자 기동 확인 (하트비트 %.0f초 전)" % age
        if _watcher_running():
            return True, "감시자 실행 중 (PID %s)" % _watcher_running()
    return False, "감시자를 띄웠지만 %d초 안에 살아난 증거가 없습니다" % wait_sec


def _sysmode(mode):
    """전원·Windows Update·SAP 업데이터를 부재중용으로 바꾸거나 원복한다.

    바꾸기 전 백업을 뜨고, 바꾼 뒤 재조회로 확인하는 것은 전부 sysmode.ps1 안에 있다.
    여기서는 그 출력을 그대로 사람에게 보여준다 — 무엇이 바뀌었는지 눈으로 확인해야 한다.
    """
    ps = os.path.join(HERE, "sysmode.ps1")
    if not os.path.exists(ps):
        return False, "sysmode.ps1 이 없어 시스템 설정은 건너뜁니다"
    try:
        # ⚠️ -File 로 부르면 PowerShell 이 콘솔 코드페이지(한국어=CP949)로 출력해 한글이 깨진다.
        #    -Command 로 감싸 OutputEncoding 을 UTF-8 로 먼저 세우고 부른다(2026-09-10 실측).
        inner = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "& '%s' -Mode %s" % (ps.replace("'", "''"), mode))
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", inner],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, timeout=180)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return False, str(e)


def _start_intake(wait_sec=12):
    """접수 서버가 꺼져 있으면 창 없이 켠다(servers.py). 이게 없으면 접수 자체가 안 되므로 부재중 모드의 전제다."""
    return SRV.start(next(s for s in SRV.SERVERS if s["key"] == "intake"), GATE.server_log_dir(CFG, RT), wait_sec)


def _start_servers(indent="  "):
    """휴가모드가 책임지는 두 서버(접수 5179 · 대시보드 저장 5178)를 켠다 — 2026-09-11~ 부팅 자동시작 대신.

    돌려주는 값 = (접수 서버 성공 여부, 한 줄 요약). 저장 서버는 현업 창구가 아니라 실패해도 휴가모드는 계속한다
    (표시만 하고, 감시자가 1분마다 다시 시도한다).
    """
    res = SRV.start_all(GATE.server_log_dir(CFG, RT))
    for srv, ok, note in res:
        print("%s%s %s(%d) — %s" % (indent, "✔" if ok else "✘", srv["name"], srv["port"], note))
    ok_intake = [ok for srv, ok, note in res if srv["key"] == "intake"][0]
    return ok_intake, " · ".join("%s %s" % (srv["name"], note) for srv, ok, note in res)


# ───────────────────────────────────────────────────── ON
def seed_seen(web):
    """켜는 순간의 웹접수 건을 어떻게 볼지 정한다.

      · 사람이 답한 건            → 영구 제외(human_locked)
      · 완료됐거나 AI 가 이미 답한 건 → 지금 내용의 지문을 심는다. 그대로면 다시 잡지 않고,
                                    현업이 **추가 질문**을 붙이면 지문이 달라져 다시 잡힌다.
      · ⚠️ **아직 아무도 답하지 않은 건(접수 확인만 붙은 건 포함) → 지문을 심지 않는다 = 처리 대상.**
        2026-09-10 실측: 예전엔 여기서 모든 건에 지문을 심어, 켜기 전에 들어온 미답변 문의
        (INQ-YYMMDD-HHMMSS)가 「이미 답했다」로 분류돼 영영 처리되지 않았다.
      · 「답했다」는 **물은 횟수만큼 답했는가**로 판정한다(2026-09-10 리뷰 #1). AI 가 첫 문의에만
        답하고 그 뒤 붙은 추가 질문이 미답변이면 backlog 다.
      · 담당자가 진행중·대기로 옮겨 둔(답은 아직 없는) 건은 봇이 가로채지 않는다(리뷰 #8).
      · 답이 이미 있는 건은 접수 확인도 된 것으로 친다 — 접수 확인이 앞선 답을 덮지 않게(리뷰 #2).
    """
    seen = {}
    for iid, rec in web.items():
        a = rec.get("a") or ""
        status = rec.get("status") or "접수"
        if a and not a.startswith(GATE.BOT_PREFIXES):
            seen[iid] = {"answered_hash": GATE.q_hash(rec), "human_locked": True}
        elif status == "완료" or GATE.answer_turns(a) >= GATE.q_turns(rec):
            # 닫혔거나, 물은 만큼 봇이 다 답했다 → 지금 내용의 지문(추가 질문이 붙으면 다시 잡힌다)
            seen[iid] = {"answered_hash": GATE.q_hash(rec), "ack_posted": True}
        elif not a and status in ("진행중", "대기"):
            # 담당자가 손대 둔 건 — 가로채지 않는다. 추가 질문이 붙으면 지문이 달라져 다시 잡힌다
            seen[iid] = {"answered_hash": GATE.q_hash(rec)}
        else:
            # 답을 기다리는 건 — 미답변 첫 문의 · 접수 확인만 붙은 건 · AI 답 뒤의 미답변 추가 질문
            seen[iid] = {"backlog": True, "ack_posted": bool(a.strip())}
    return seen


def mode_on():
    st = _load(P_STATE)
    if st.get("mode") == "on":
        # 이미 켜져 있으면 **빠진 것만 다시 켠다.** 버튼을 다시 누르는 게 가장 쉬운 복구 수단이어야 한다.
        # (지문·기준선은 건드리지 않는다 — 부재 도중에 다시 심으면 처리 이력이 흐트러진다)
        print("이미 부재중 모드입니다 (시작: %s) — 꺼진 것만 다시 켭니다" % st.get("started_at"))
        _set_resume_flag(True)
        ok1, _ = _start_servers()
        ok3, n3 = _start_watcher()
        print("  %s 감시자 — %s" % ("✔" if ok3 else "✘", n3))
        return 0 if (ok1 and ok3) else 1

    ix = _intake()
    web = ix._web_records()
    baseline = max(web.keys()) if web else ""

    st.update({
        "mode": "on",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline_id": baseline,
        "ended_at": "",
        "answered_total": 0,
        "runs_count": 0,
        "runs_date": time.strftime("%Y-%m-%d"),
        # 서버 끊김 누계는 이번 부재 기간 것만 보고서에 나오게 새로 시작한다
        "intake_down_since": 0, "intake_down_total": 0,
        "saver_down_since": 0, "saver_down_total": 0,
    })
    _save(P_STATE, st)
    _set_resume_flag(True)
    # 지금 있는 웹접수 건의 **문의 지문을 심어 둔다.**
    #   · 그대로면 다시 잡히지 않는다(과거 건을 헤집지 않음)
    #   · 현업이 **추가 질문**을 붙이면 지문이 달라져 자동으로 다시 잡힌다
    #     ← 기준선 id 로만 막으면 이 경우가 영영 무시된다(2026-09-10 발견)
    seen = seed_seen(web)
    _save(os.path.join(RT, "seen.json"), seen)
    backlog = sorted(k for k, v in seen.items() if v.get("backlog"))

    print("부재중 모드 ON")
    print("  기준선 : %s" % (baseline or "(웹접수 없음)"))
    print("  밀린 문의 : %d건%s  ← 켜기 전에 들어와 아직 답이 없는 건. 곧바로 처리합니다"
          % (len(backlog), (" (" + ", ".join(backlog) + ")") if backlog else ""))
    print("  런타임 : %s" % RT)

    print("")
    print("[1/3] 두 서버 — 접수 창구(5179) · 대시보드 저장(5178)")
    print("  (휴가모드가 켜고 지킵니다 — 휴가 중 죽으면 감시자가, 재부팅 뒤엔 로그인 시 재개가 되살립니다)")
    ok, note = _start_servers()
    if not ok:
        print("  ⚠ 접수 서버가 없으면 현업이 문의를 남길 수 없습니다. 제어판에서 확인해 주십시오.")

    print("")
    print("[2/3] PC 가 꺼지거나 재부팅되지 않게")
    ok2, out = _sysmode("On")
    for line in (out or "").splitlines():
        if line.strip():
            print("  " + line.rstrip())
    if not ok2:
        print("  ⚠ 시스템 설정을 바꾸지 못했습니다 — 절전·업데이트로 멈출 수 있습니다.")

    print("")
    print("[3/3] 감시자")
    ok3, note3 = _start_watcher()
    print("  %s %s" % ("✔" if ok3 else "✘", note3))
    if not ok3:
        print("  ⚠ 감시자가 없으면 문의가 들어와도 아무도 답하지 않습니다.")
        print("     수동 실행: python \"%s\"" % os.path.join(HERE, "watcher.py"))

    print("")
    if ok and ok2 and ok3:
        print("휴가모드 준비 완료. 편히 다녀오십시오.")
    else:
        print("⚠ 일부 단계가 끝나지 않았습니다. 위 표시를 확인해 주십시오.")
    return 0 if (ok and ok3) else 1


# ───────────────────────────────────────────────────── OFF
def mode_resume():
    """**재개 런처(작업 스케줄러 「SD 트리니티봇 감시」 — 2026-10-06 시작프로그램 「SD 트리니티봇 재개」를 대체)가 서비스가 죽은 것을 보면 부른다.** 휴가모드가 켜져 있을 때만 두 서버·감시자를 되살린다.

    2026-09-11 실측: PC 를 다시 시작하자 접수·저장 서버는 시작프로그램으로 떴는데 감시자는 아무도 띄우지 않아
    휴가모드가 「켜짐 · 감시 끊김」으로 멈춰 있었다. 휴가 중 재부팅(정전·업데이트·사람)이 나면 복귀할 때까지
    문의가 방치된다.
    2026-09-11 사용자 지시로 두 서버의 부팅 자동시작을 없앴다 — 휴가 중 재부팅이면 **여기서** 두 서버를 켠다.
    ⛔ 휴가모드가 꺼져 있으면 **아무것도 하지 않는다** — 재부팅할 때마다 휴가모드·서버가 멋대로 켜지면 안 된다.
    """
    st = _load(P_STATE)
    if st.get("mode") != "on":
        _set_resume_flag(False)                                 # 꺼진 채 표지만 남은 경우(끄기가 도중에 끊김)를 치운다
        print("휴가모드 꺼짐 — 할 일 없음")
        return 0
    _set_resume_flag(True)                                      # 켜져 있는데 표지가 없던 경우(이 장치 도입 전에 켠 모드)를 채운다
    # 로그인 직후엔 네트워크·프로필이 덜 올라와 있을 수 있다 — 접수 서버가 안 뜨면 두 번 더 해 본다
    # (그래도 안 되면 감시자가 1분마다 되살린다)
    for i in range(3):
        ok1, n1 = _start_servers()
        if ok1:
            break
        time.sleep(10)
    ok3, n3 = _start_watcher()
    GATE.journal(CFG, {"event": "resume", "servers": n1, "watcher": n3})
    print("휴가모드 재개 — %s · 감시자: %s" % (n1, n3))
    return 0 if (ok1 and ok3) else 1


def mode_off():
    st = _load(P_STATE)
    if st.get("mode") != "on":
        print("부재중 모드가 아닙니다.")
    st["mode"] = "off"
    st["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(P_STATE, st)
    _set_resume_flag(False)                                     # 재부팅 자동 재개 해제 — 끄는 즉시
    print("부재중 모드 OFF — 감시자는 다음 확인(5초 이내)에 스스로 멈춥니다.")

    for _ in range(16):
        if not _watcher_running():
            break
        time.sleep(0.5)
    left = _watcher_running()
    print("  감시자 종료 확인" if not left else "  ⚠ 감시자 프로세스 잔존: %s" % left)

    print("")
    print("시스템 설정 원복")
    ok, out = _sysmode("Off")
    for line in (out or "").splitlines():
        if line.strip():
            print("  " + line.rstrip())
    if not ok:
        print("  ⚠ 원복에 실패했습니다. 절전·Windows Update 설정을 직접 확인해 주십시오.")
    print("")
    print("  · 두 서버(접수 5179 · 저장 5178)는 끄지 않고 둡니다 — 복귀 직후에도 현업이 트리니티봇 답을 다시 보고")
    print("    추가 질문을 남길 수 있게. 부팅 자동시작은 없으니 다음 재부팅 때 꺼집니다(바로 끄려면 제어판 서버 카드 「종료」).")

    path = report(st)
    shown = LAST_REPORT_HTML or path                            # 사람이 여는 것은 HTML(2026-10-08), Markdown 은 옆에 그대로 둔다
    print("  복귀 보고서: %s" % shown)
    if shown != path:
        print("  (같은 내용의 Markdown: %s)" % path)
    try:
        os.startfile(shown)                                     # noqa: S606
    except OSError:
        pass
    return 0


# ───────────────────────────────────────────────────── 보고서 (비용 0)
_REAL_TAG = re.compile(r"^\d{8}-\d{6}(-gemini(-\d+)?)?$")        # 감시자가 붙이는 호출 이름 — 그 밖(test-… 등)은 수동 실행


def last_answers(events):
    """판정별로 묶되 **문의마다 마지막 답만** 센다(2026-10-06).

    같은 문의에 답이 여러 번 기록되는 것은 정상이다 — 보류(정보 요청) → 추가 질문 → 확정 답변. 예전엔 답마다 따로 세서
    이미 해결된 문의가 「대기 — 확인 필요」에도 나와 담당자가 할 일이 남은 것처럼 보였다.
    돌려주는 값 = (판정별 마지막 답 목록, {문의 id: [앞선 판정, …]}).
    """
    last, earlier = {}, {}
    for e in events:                                   # 시간순
        if e.get("event") != "answered":
            continue
        i = e.get("id")
        if i in last:
            earlier.setdefault(i, []).append(last[i].get("verdict") or "?")
        last[i] = e
    by_verdict = {}
    for e in last.values():
        by_verdict.setdefault(e.get("verdict") or "?", []).append(e)
    return by_verdict, earlier


VERDICT_KO = {"answer": "확정 답변", "hold": "보류(정보 요청·확인 대기)", "escalate": "에스컬레이션", "dev": "개발 요청"}


_HB_SPLIT = re.compile(r"\n\n──── 추가 답변 \(([^)]*)\) ────\n")


def human_blocks(a):
    """답 칸에서 **사람(담당자)이 쓴** 블록 [(시각, 글)] — 봇 표기로 시작하지 않는 블록(2026-10-08 보고서 보강).

    보고서가 journal 의 봇 답만 보여 줘서, 담당자가 정정·안내를 올린 건도 「대기 — 확인 필요」에 그대로 남아 있었다."""
    parts = _HB_SPLIT.split(a or "")
    blocks = [("", parts[0])] + [(parts[k], parts[k + 1]) for k in range(1, len(parts) - 1, 2)]
    out = []
    for ts, txt in blocks:
        t = (txt or "").strip()
        if t and not t.startswith(GATE.BOT_PREFIXES):
            out.append((ts, t))
    return out


def is_test_req(req, cfg, norm):
    """시험 요청자(config test_reqs)·담당자 본인 시험(owner_test_req)이 넣은 문의인가 — 복귀 보고서에서 따로 모은다.
    norm = 접수 서버의 이름 정규화(ix._norm) — 감시자의 시험 문의 판정과 같은 함수를 쓴다."""
    pool = set(norm(x) for x in ([cfg.get("owner_test_req") or ""] + list(cfg.get("test_reqs") or [])) if x)
    who = norm(req or "")
    return bool(who) and who in pool


def state_line(r):
    """문의 카드의 「현재 기록」 — 보고서를 만든 시점의 실제 상태(봇 답 이후 담당자가 한 일 포함)."""
    bits = ["상태 %s" % (r.get("status") or "접수")]
    hb = human_blocks(r.get("a"))
    if hb:
        bits.append("담당자 답 %d건" % len(hb))
    if r.get("botgrade"):
        bits.append("판정 %s" % r["botgrade"])
    if r.get("req_closed"):
        bits.append("요청자가 닫음 %s" % r["req_closed"])
    if r.get("req_score"):
        bits.append("만족도 ★%s점" % r["req_score"])
    if r.get("req_feedback"):
        bits.append("의견 \"%s\"" % r["req_feedback"])
    return " · ".join(bits)


def report(st=None):
    st = st or _load(P_STATE)
    ix = _intake()
    merged = ix._read_log()
    base = st.get("baseline_id", "")

    events, cost, runs, fails = [], 0.0, 0, []
    # ⚠️ 이번 부재 기간의 기록만 센다 — journal 폴더에는 이전 시험·이전 부재의 기록이 계속 쌓여 있다.
    p_start = st.get("started_at", "") or ""
    p_end = st.get("ended_at", "") or "9999"
    jd = os.path.join(RT, "journal")
    if os.path.isdir(jd):
        for fn in sorted(os.listdir(jd)):
            for line in io.open(os.path.join(jd, fn), encoding="utf-8", errors="replace"):
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not (p_start <= (e.get("ts") or "") <= p_end):
                    continue                                # 이번 부재 기간 밖의 기록
                events.append(e)
                if e.get("event") == "run":
                    runs += 1
                    cost += float(e.get("cost_usd") or 0)
                if e.get("event") in ("claude_fail", "write_fail", "verify_fail",
                                      "loop_error", "ack_fail"):
                    fails.append(e)

    by_verdict_all, earlier = last_answers(events)

    # 켜기 전에 들어와 답을 기다리던 건(backlog)도 이번 부재 기간의 처리 대상이다 — 보고서에 넣는다
    seen = _load(os.path.join(RT, "seen.json"), {}) or {}
    backlog = [i for i, v in seen.items() if (v or {}).get("backlog")]
    new = {i: r for i, r in merged.items()
           if r.get("by") == "웹접수" and (not base or i > base or i in backlog)}
    # 시험 문의(시험 요청자 · 담당자 본인 시험)는 처리 대상 집계에서 빼고 보고서 뒤쪽에 따로 모은다(2026-10-08 — 시험 5건이 섞여 「처리 대상 8건」이 부풀었다)
    def _is_test(i):
        return is_test_req((merged.get(i) or {}).get("req", ""), CFG, ix._norm)
    new_test = {i: r for i, r in new.items() if _is_test(i)}
    new_real = {i: r for i, r in new.items() if i not in new_test}
    by_verdict = {k: [e for e in rows if not _is_test(e.get("id"))] for k, rows in by_verdict_all.items()}
    test_rows = [e for rows in by_verdict_all.values() for e in rows if _is_test(e.get("id"))]
    # 보류 건 중 담당자가 이미 답했거나 닫은 건은 「확인 필요」가 아니다
    def _handled(i):
        r = merged.get(i) or {}
        return (r.get("status") or "") == "완료" or bool(human_blocks(r.get("a")))
    hold_open = [e for e in by_verdict.get("hold", []) if not _handled(e.get("id"))]
    hold_done = [e for e in by_verdict.get("hold", []) if _handled(e.get("id"))]
    untouched = [i for i, r in new_real.items()
                 if not (r.get("a") or "").startswith(GATE.BOT_PREFIXES)]
    open_items = [i for i, r in new_real.items() if (r.get("status") or "접수") != "완료"]
    # 담당자가 답했던 건에 부재 중 추가 질문이 붙은 것 — 트리니티봇은 사람이 답한 건에 손대지 않으므로
    # 여기서라도 알린다(리뷰: 안 알리면 현업은 부재 기간 내내 기다리고 담당자는 모른다)
    human_waiting = sorted(i for i, v in seen.items()
                           if (v or {}).get("human_locked") and v.get("answered_hash")
                           and i in merged and GATE.q_hash(merged[i]) != v["answered_hash"])

    hb = GATE.heartbeat_path(CFG)
    beats = 0
    if os.path.exists(hb):
        for line in io.open(hb, encoding="utf-8", errors="replace"):
            ts = line[8:27] if line.startswith('{"ts": "') else ""
            if '"storage": "down"' in line:
                continue                                        # G: 끊김 중 박동은 일을 한 시간이 아니다
            if p_start <= ts <= p_end:
                beats += 1

    L = []
    L.append("# 부재중 처리 결과")
    L.append("")
    L.append("| 항목 | 값 |")
    L.append("|---|---|")
    L.append("| 기간 | %s ~ %s |" % (st.get("started_at", "?"), st.get("ended_at", "(진행중)")))
    L.append("| 기준선 | `%s` 이후 접수분 + 켜기 전 미답변 %d건 |" % (base or "(없음)", len(backlog)))
    L.append("| 처리 대상 문의 | **%d건** (시험 문의 %d건은 뺌 — 아래에 따로) |" % (len(new_real), len(new_test)))
    L.append("| AI 가 답변 기재 | **%d건** |" % len(by_verdict.get("answer", [])))
    L.append("| 개발 요청 정리 | %d건 |" % len(by_verdict.get("dev", [])))
    L.append("| 에스컬레이션 안내 | %d건 |" % len(by_verdict.get("escalate", [])))
    L.append("| 대기(판단 보류) | 확인 필요 **%d건** · 담당자가 이미 처리 %d건 |" % (len(hold_open), len(hold_done)))
    L.append("| **아직 열려 있는 건** | **%d건** |" % len(open_items))
    L.append("| 담당자가 답했던 건에 붙은 추가 질문 | %s |"
             % ("없음" if not human_waiting else "**%d건** — 아래 목록" % len(human_waiting)))
    L.append("| Claude 호출 | %d회 · $%.2f |" % (runs, cost))
    # 호출 실패 · 안전 신호 · 답변 출처(2026-09-30) — 기록은 journal 에 있었는데 보고서가 세지 않아 한도 소진으로 3회 다 실패한 날(9/21)에도
    # 「오류·실패 0건」이 나왔다. 「기록된다」와 「보인다」는 다르다.
    ev_n = collections.Counter(e.get("event") for e in events)
    kinds = collections.Counter((e.get("kind") or "") for e in events if e.get("event") == "claude_fail")
    n_kind = sum(kinds.values())
    L.append("| 호출 실패 | %s |" % (("**%d회** — 한도 소진 %d · 오류 %d · 그 밖 %d" % (
        n_kind, kinds.get("limit", 0), kinds.get("error", 0), n_kind - kinds.get("limit", 0) - kinds.get("error", 0)))
        if n_kind else "없음"))
    denials = sum(int(e.get("denials") or 0) for e in events if e.get("event") == "run")
    L.append("| 안전 신호 | 내부 번호 유출 차단 %d · 답 규격 위반 %d · 엔진 전환 %d · 허용 밖 도구 시도 %d · 답 복구 %d |" % (
        ev_n.get("card_leak_blocked", 0), ev_n.get("schema_problem", 0), ev_n.get("failover", 0), denials,
        ev_n.get("salvaged", 0)))
    ans_ev = [e for e in events if e.get("event") == "answered"]
    if ans_ev:
        src_n = collections.Counter(e.get("source") or "옛 기록" for e in ans_ev)
        mod_n = collections.Counter(e.get("model") or ("(시스템 보류)" if e.get("fallback") else "(기록 없음)") for e in ans_ev)
        L.append("| 답변 출처 · 모델 | 감시자 %d · 수동 %d · 옛 기록 %d — %s |" % (
            src_n.get("watcher", 0), src_n.get("manual", 0), src_n.get("옛 기록", 0),
            " · ".join("%s %d" % (k, v) for k, v in mod_n.most_common())))
    L.append("| 감시 하트비트 | %d회 (5초 간격 · 살아 있었는지의 근거) |" % beats)
    dn = int(st.get("intake_down_total", 0))
    L.append("| **접수 창구 끊김** | %s |"
             % ("없음" if not dn else "**총 %d분** — 그동안은 접수 자체가 불가능했습니다" % (dn // 60)))
    ds = int(st.get("saver_down_total", 0))
    if ds:
        L.append("| 대시보드 저장 서버 끊김 | 총 %d분 (현업 영향 없음 — 담당자 보드 저장만) |" % (ds // 60))
    tests = [e for e in events if e.get("event") == "auto_close_test"]
    L.append("| 담당자 시험 문의 자동 종결 | %s |" % ("없음" if not tests else "%d건" % len(tests)))
    resumes = [e for e in events if e.get("event") == "resume"]
    drift = [e for e in events if e.get("event") == "settings_drift"]
    L.append("| 감시자 재개(재부팅 뒤 자동 · 수동 포함) | %s |" % ("없음" if not resumes else "%d회" % len(resumes)))
    L.append("| 휴가모드 설정 되돌림 | %s |"
             % ("없음" if not drift else "%d회 — Windows Update 설정이 바뀌어 있어 감시자가 되돌렸습니다" % len(drift)))
    # 트리니티봇 답변 검증 장부(누적) — 판정은 담당자가 한다. 장부가 실패해도 보고서는 나가야 한다.
    try:
        lrows = LEDGER.build(CFG, merged, ix._norm)
        lsum = LEDGER.summarize(lrows)
        lpath = LEDGER.write_md(lrows, lsum, CFG)
        lline = LEDGER.line_summary(lsum, LEDGER.stage_progress(lrows, CFG))
    except Exception as e:                                      # noqa: BLE001
        lpath, lline = "", "장부를 만들지 못했습니다(%s)" % e
    L.append("| 트리니티봇 답 검증(누적) | %s |" % lline)
    L.append("| 오류·실패 | %d건 |" % len(fails))
    try:
        import stale_cases as _SC
        _n_st = len(_SC.stale(merged))
        _n_cl = len(_SC.closed_by_requester(merged, since=(st.get("started_at") or "")[:16]))
        L.append("| 문의자 종결 | 직접 닫은 건(판정 전) **%d건** · %d시간 넘게 안 닫힌 건 **%d건** |" % (_n_cl, _SC.STALE_HOURS, _n_st))
    except Exception:                                       # noqa: BLE001
        pass
    user_scores = [r.get("req_score") for r in new.values() if r.get("req_score") is not None]
    if user_scores:
        avg_score = sum(user_scores) / len(user_scores)
        sc_cnt = collections.Counter(user_scores)
        score_summary = "평균 ★%.1f (%d건) — %s" % (
            avg_score, len(user_scores),
            " · ".join("%d점 %d건" % (k, sc_cnt.get(k, 0)) for k in (5, 4, 3, 2, 1) if sc_cnt.get(k, 0) > 0)
        )
        L.append("| **현업 만족도 평가** | %s |" % score_summary)
    L.append("")

    # 문의자 종결(2026-10-06) — 닫힌 건은 바로 「완료」라 열린 목록에 안 잡힌다. 답변 판정을 놓치지 않게 따로 모은다.
    try:
        import stale_cases as SC
        sc_stale = SC.stale(merged)
        sc_closed = SC.closed_by_requester(merged, since=(st.get("started_at") or "")[:16])
    except Exception as e:                                  # noqa: BLE001
        sc_stale, sc_closed = [], []
        L.append("> ⚠ 문의자 종결 점검을 못 했습니다(%s)" % e)
        L.append("")
    if sc_stale:
        L.append("## 🔔 답변이 나갔는데 %d시간이 지나도 문의자가 닫지 않은 건 (%d건)" % (SC.STALE_HOURS, len(sc_stale)))
        L.append("해결됐을 가능성이 큽니다 — 문의자에게 확인하시거나 직접 완료로 닫아 주십시오.")
        L.append("")
        for x in sc_stale:
            L.append("- `%s` · %s · 답변 %s (%d시간 경과) · %s" % (x["id"], x["req"], x["answered_at"], x["hours"], x["q"]))
        L.append("")
    if sc_closed:
        L.append("## ✅ 문의자가 직접 닫은 건 — 답변 판정이 필요합니다 (%d건)" % len(sc_closed))
        L.append("상태는 이미 「완료」입니다. 트리니티봇 답이 맞았는지 대시보드 ✏️수정에서 판정(맞음·보완·틀림)해 주십시오.")
        L.append("")
        for x in sc_closed:
            r_item = merged.get(x["id"]) or {}
            sc_star = (" · ★%s점" % r_item["req_score"]) if r_item.get("req_score") else ""
            sc_fb = (" · \"%s\"" % r_item["req_feedback"]) if r_item.get("req_feedback") else ""
            L.append("- `%s` · %s · 닫음 %s%s%s%s · %s" % (
                x["id"], x["req"], x["closed_at"], sc_star, sc_fb,
                " · ⚠ 보류 중에 닫음(답이 확정되지 않았음)" if x.get("kind") == "보류" else "", x["q"]))
        L.append("")

    if human_waiting:
        L.append("## ⚠ 담당자가 답했던 건에 추가 질문이 붙었습니다")
        L.append("트리니티봇은 사람이 답한 건에 손대지 않습니다. 직접 답해 주십시오.")
        L.append("")
        for i in human_waiting:
            last_q = (merged[i].get("q") or "").split(GATE.Q_MARK)[-1]
            L.append("- `%s` · %s · %s" % (i, merged[i].get("req", ""),
                                            last_q.split("────", 1)[-1].strip().replace("\n", " ")[:90]))
        L.append("")

    if untouched:
        L.append("## ⚠ AI 가 손대지 못한 건 — 먼저 보십시오")
        L.append("접수는 됐는데 안내조차 남지 않은 건입니다. 감시자가 멈춰 있었을 수 있습니다.")
        L.append("")
        for i in sorted(untouched):
            L.append("- `%s` · %s · %s" % (i, new[i].get("req", ""), (new[i].get("q") or "")[:70]))
        L.append("")

    order = [("answer", "답변 기재"), ("dev", "개발 요청 — 설계 착수"),
             ("escalate", "에스컬레이션 안내"), ("hold", "대기 — 확인 필요")]
    for key, title in order:
        rows = hold_open if key == "hold" else (by_verdict.get(key) or [])
        if not rows:
            continue
        L.append("## %s (%d건)" % (title, len(rows)))
        L.append("")
        for e in rows:
            i = e.get("id")
            r = merged.get(i, {})
            L.append("### `%s` — %s" % (i, r.get("req", "")))
            if earlier.get(i):
                L.append("**앞선 답**: %s → 이후 같은 문의의 답이 **%s**(으)로 갱신됐습니다 — 아래는 마지막 답입니다." % (
                    " → ".join(VERDICT_KO.get(v, v) for v in earlier[i]), VERDICT_KO.get(key, key)))
            L.append("**문의**: %s" % (r.get("q") or "").replace("\n", " ")[:200])
            L.append("**현재 기록**: %s" % state_line(r))
            L.append("")
            L.append("**현업에게 나간 답변**")
            L.append("")
            L.append("```")
            L.append((e.get("public") or "").strip())
            L.append("```")
            for _ts, _txt in human_blocks(r.get("a")):                # 봇 답 뒤에 담당자가 올린 답(정정·안내)
                L.append("")
                L.append("**담당자가 올린 답%s**" % ((" (%s)" % _ts) if _ts else ""))
                L.append("")
                L.append("```")
                L.append(_txt)
                L.append("```")
            if e.get("note"):
                L.append("")
                L.append("**담당자용 메모(현업 미노출)**: %s" % e["note"])
            L.append("")
            if (r.get("status") or "") == "완료" and r.get("botgrade"):
                L.append("**확인하실 것**: 판정(%s)과 완료 처리가 이미 끝났습니다 — 따로 하실 일이 없습니다." % r["botgrade"])
            else:
                L.append("**확인하실 것**: 대시보드 ✏️수정의 「🤖 트리니티봇 답 판정」에서 맞음 · 보완 · 틀림을 고르고"
                         "(보완·틀림이면 판정 메모에 무엇이 달랐는지), 맞으면 `완료`로 함께 닫아 주십시오.")
            L.append("")
        L.append("---")
        L.append("")

    if hold_done:
        L.append("## ✅ 이미 처리된 보류 건 (%d건) — 담당자가 답했거나 닫았습니다" % len(hold_done))
        L.append("")
        for e in hold_done:
            i = e.get("id")
            r = merged.get(i, {})
            L.append("- `%s` · %s · %s · %s" % (i, r.get("req", ""), state_line(r), (r.get("q") or "").replace("\n", " ").replace("|", "/")[:70]))
        L.append("")

    if test_rows:
        L.append("## 시험 문의 (%d건) — 시험 요청자·담당자 시험은 처리 대상에서 뺐습니다" % len(test_rows))
        L.append("")
        L.append("| 문의 | 요청자 | 최종 판정 | 상태 | 질문 앞부분 |")
        L.append("|---|---|---|---|---|")
        for e in sorted(test_rows, key=lambda x: str(x.get("id"))):
            i = e.get("id")
            r = merged.get(i, {})
            L.append("| `%s` | %s | %s | %s | %s |" % (i, str(r.get("req", "")).replace("|", "/"), VERDICT_KO.get(e.get("verdict"), e.get("verdict") or "?"),
                                                 r.get("status") or "접수", (r.get("q") or "").replace("\n", " ").replace("|", "/")[:50]))
        L.append("")

    # 트리니티봇 행동 기록(2026-09-11~) — 호출마다 무엇을 읽고·찾고·열었는지, 거부된 시도, 점검할 점
    behs_all = [e for e in events if e.get("event") == "behavior"]
    # 감시자가 부른 호출만 센다 — 사람이 세션에서 손으로 돌린 시험(tag 모양이 다르다)은 뺀다(9/21 「호출 6회」 중 3회가 수동 시험이었다)
    behs = [b for b in behs_all if _REAL_TAG.match(str(b.get("tag") or ""))]
    other_tags = [str(b.get("tag")) for b in behs_all if not _REAL_TAG.match(str(b.get("tag") or ""))]
    if behs:
        run_by_tag = dict((e.get("tag"), e) for e in events if e.get("event") == "run")
        L.append("## 트리니티봇 행동 기록 (호출 %d회)" % len(behs))
        L.append("전체 흐름 = 런타임 `runs/<시각>.stream.jsonl`. 아래는 호출별 요약입니다.")
        L.append("")
        L.append("| 호출 | 모델 | 문의 | 도구 | 사진(넘김/열람) | 개발계 조회 | 웹(검색/읽기) | 거부된 시도 | 점검 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for b in behs:
            r = run_by_tag.get(b.get("tag")) or {}
            t = b.get("tools") or {}
            tool_s = " · ".join("%s %d" % (k, v) for k, v in sorted(t.items()) if not k.startswith("mcp__")) or "-"
            n_mcp = sum(v for k, v in t.items() if k.startswith("mcp__"))
            given, opened = r.get("shots_given"), b.get("shots_opened", 0)
            warn = []
            if given and not opened:
                warn.append("⚠ 사진을 넘겼는데 열지 않음")
            if b.get("denied"):
                warn.append("⚠ 허용되지 않은 도구 시도")
            if b.get("web_flags"):
                warn.append("⚠ 검색어에 사내 식별자 의심 %d건" % len(b["web_flags"]))
            if b.get("rc") not in (0, None):
                warn.append("호출 종료코드 %s" % b.get("rc"))
            L.append("| `%s` | %s | %s | %s | %s/%s | %s | %s | %s | %s |" % (
                b.get("tag"), b.get("model") or "?", ", ".join(r.get("ids") or []) or "?", tool_s,
                "?" if given is None else given, opened, n_mcp or "-",
                "%d/%d" % (t.get("WebSearch", 0), t.get("WebFetch", 0)) if (t.get("WebSearch") or t.get("WebFetch")) else "-",
                "; ".join(x.replace("|", "/")[:80] for x in (b.get("denied") or [])) or "-",
                " · ".join(warn) or "-"))
        L.append("")
        if other_tags:
            L.append("감시자가 부른 것이 아닌 실행(수동 시험 등) %d회는 뺐습니다: %s" % (
                len(other_tags), ", ".join("`%s`" % t for t in other_tags[:6])))
            L.append("")
        webs = [(b.get("tag"), b.get("web") or [], b.get("web_flags") or []) for b in behs if b.get("web")]
        if webs:
            L.append("**웹 검색어·주소** (검색은 외부로 나가는 행위 — 사내 식별자가 섞이면 ⚠)")
            L.append("")
            for tag, qs, flags in webs:
                L.append("- `%s` · %s%s" % (tag, " · ".join(q.replace("|", "/")[:90] for q in qs),
                                            ("  ⚠ " + "; ".join(flags)) if flags else ""))
            L.append("")

    if fails:
        L.append("## 오류·실패 기록 (%d건)" % len(fails))
        L.append("")
        for e in fails[:30]:
            L.append("- `%s` %s%s — %s" % (e.get("ts", ""), e.get("event"),
                                            ("(%s)" % e["kind"]) if e.get("kind") else "",
                                            str(e.get("err") or e.get("id"))[:120]))
        L.append("")

    L.append("## 읽는 법")
    L.append("")
    L.append("- **AI 답변은 전부 `대기` 상태입니다.** 사람이 보고 닫아야 `완료`가 됩니다 —")
    L.append("  현업 화면의 `완료` 배지가 담당자 확정 답변으로 읽히기 때문입니다.")
    L.append("- **하트비트가 0 이거나 기간에 비해 적으면** 감시자가 멈춰 있었다는 뜻입니다.")
    L.append("  「문의가 없었다」가 아니라 「멈춰 있었다」를 먼저 의심하십시오.")
    L.append("- **시험 문의**(시험 요청자·담당자 본인 시험)는 처리 대상 집계에서 빼고 「시험 문의」 구역에 따로 모았습니다.")
    L.append("- 카드의 **현재 기록**은 보고서를 만든 시점의 실제 상태입니다 — 담당자가 봇 답 뒤에 올린 답은 「담당자가 올린 답」으로 보이고,")
    L.append("  이미 답했거나 닫은 보류 건은 「대기 — 확인 필요」가 아니라 「이미 처리된 보류 건」에 있습니다.")
    L.append("- ITSM(ITSM)와 별개입니다. 여기 접수분은 `OPS-003` 월간 집계 대상이 아닙니다.")
    L.append("- **트리니티봇 답은 판정해 주십시오.** 맞음 · 보완 · 틀림이 검증 장부에 쌓여 트리니티봇 권한 확대의 근거가 됩니다"
             + (" — `%s`" % lpath if lpath else "") + " (제어판 「🧾 검증 장부」로도 열립니다).")

    os.makedirs(RT, exist_ok=True)
    name = "부재중_처리결과_%s.md" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(RT, name)
    md_text = "\n".join(L) + "\n"
    io.open(path, "w", encoding="utf-8", newline="").write(md_text)
    # 2026-10-08 담당자 지시: 보고서는 HTML 로 — 사람은 화면으로, AI 는 같은 파일의 구조화 JSON 으로(report_html.py). Markdown 은 옆에 그대로 둔다.
    # HTML 을 못 만들어도 보고서 자체는 나와야 하므로 실패하면 Markdown 경로를 돌려준다.
    # ⚠️ report() 의 반환값(Markdown 경로)은 바꾸지 않는다 — 시험(unit_metrics)과 기존 호출이 그 글을 읽는다. HTML 경로는 LAST_REPORT_HTML 로 알린다.
    global LAST_REPORT_HTML
    LAST_REPORT_HTML = ""
    try:
        import report_html as RH
        LAST_REPORT_HTML = RH.write_html(md_text, path)
    except Exception as e:                                          # noqa: BLE001
        try:
            GATE.journal(CFG, {"event": "report_html_fail", "err": str(e)[:200]})
        except Exception:                                           # noqa: BLE001
            pass
    return path


# ───────────────────────────────────────────────────── STATUS
def status():
    st = _load(P_STATE)
    ix = _intake()
    merged = ix._read_log()
    base = st.get("baseline_id", "")
    seen = _load(os.path.join(RT, "seen.json"), {}) or {}
    new = [i for i, r in merged.items()
           if r.get("by") == "웹접수"
           and (not base or i > base or (seen.get(i) or {}).get("backlog"))]
    openn = [i for i in new if (merged[i].get("status") or "접수") != "완료"]
    out = {
        "mode": st.get("mode", "off"),
        "started_at": st.get("started_at", ""),
        "baseline_id": base,
        "watcher_pids": _watcher_running(),
        "intake_up": _port_alive(5179) and SRV.http_ok(5179),      # 포트만이 아니라 실제 응답까지(2026-10-08)
        "saver_up": _port_alive(5178) and SRV.http_ok(5178),
        "new_since_start": len(new),
        "open_items": len(openn),
        "answered_total": st.get("answered_total", 0),
        "runs_today": st.get("runs_count", 0),
        "last_cycle": st.get("last_cycle", ""),
        "runtime_dir": RT,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # 콘솔 없이(작업 스케줄러·파이프) 불리면 표준출력이 cp949 라 「✔」 출력에서 UnicodeEncodeError 로 **중간에 죽는다**
    # (2026-10-06 재개 런처 실전 시험에서 발견 — 첫 서버만 켜고 나머지를 건너뛸 뻔했다). 어디서 불러도 UTF-8 로 쓴다.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "on":
        sys.exit(mode_on())
    if cmd == "resume":
        sys.exit(mode_resume())
    if cmd == "off":
        sys.exit(mode_off())
    if cmd == "report":
        p = report()
        print(LAST_REPORT_HTML or p)                            # HTML 경로를 먼저(사람용 + AI 용 JSON), 없으면 Markdown
        if LAST_REPORT_HTML:
            print(p)
        sys.exit(0)
    sys.exit(status())
