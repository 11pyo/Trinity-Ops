# -*- coding: utf-8 -*-
"""
log-inquiry.py — 일반·단순·긴급 문의를 대시보드 로그(inquiry-log.js)에 기록.
append-only(push 한 줄) + id 병합 → 여러 세션 동시 기록해도 충돌 없음.
상태: 접수 → 진행중 → 대기 → 완료 (대시보드 4열 보드로 표시, 완료열은 스크롤).

① 접수(문의 들어옴): '접수'로 박음 → 보드 접수열 맨 위
   python log-inquiry.py --new --type 단순문의 --q "문의 요약" --by 운영담당 [--date 2026-06-04]
   → stdout 'NEW id=INQ...' 의 id 를 기억할 것.

② 상태 전이 / 채우기 (같은 id):
   python log-inquiry.py --id INQ123 --status 진행중
   python log-inquiry.py --id INQ123 --status 대기
   python log-inquiry.py --done --id INQ123 --a "처리·답변 요약" --ref "#ts-account-auth"   # =완료

③ 문의자 종결(2026-10-06~ — 내 문의함 [케이스 닫기] 가 부른다. 사람이 손으로 부를 일은 거의 없다):
   python log-inquiry.py --id INQ123 --req-close      # 상태=완료 + req_closed=닫은 시각 (내용·답변은 그대로)

※ 접수와 동시에 답이 끝났으면 ①에서 --a(또는 --status 완료)까지 주면 바로 완료.
type 예: 단순문의 | 일반요청 | 긴급문의   /   status: 접수 | 진행중 | 대기 | 완료
"""
import argparse, json, sys, io, os, re, time, shutil, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inquiry-log.js")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backups")
VALID = ("접수", "진행중", "대기", "완료")

def ensure():
    if not os.path.exists(LOG):
        with open(LOG, "w", encoding="utf-8") as f:
            f.write("window.INQUIRY_LOG = window.INQUIRY_LOG || [];\n")

def _backup_before_write():
    # 쓰기 직전 스냅샷: 최근본(.prev) + 일자별 1회(_backups/). 브라우저측 직접저장(File System Access API)
    # 오작동으로 파일이 비는 사고(2026-08-24 실증)가 있었기에 헬퍼 경로에서라도 항상 직전 상태를 보존.
    if not os.path.exists(LOG) or os.path.getsize(LOG) == 0:
        return
    try:
        shutil.copyfile(LOG, LOG + ".prev")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        daily = os.path.join(BACKUP_DIR, "inquiry-log." + datetime.date.today().strftime("%Y%m%d") + ".js")
        if not os.path.exists(daily):
            shutil.copyfile(LOG, daily)
    except Exception:
        pass

def _lock(lf):
    # 짧은 OS 락: 동시에 두 세션이 같은 EOF에 써서 한 줄이 사라지는 것 방지.
    # Windows의 평범한 append("a")는 원자적이지 않음(lseek→write 경쟁). best-effort.
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0); msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lf, fcntl.LOCK_EX)
    except Exception:
        pass

def _unlock(lf):
    try:
        if os.name == "nt":
            import msvcrt
            lf.seek(0); msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception:
        pass

def _free_id(cand):
    """⚠️ 신규 id 는 **초 단위 시각**이라 같은 초에 두 건이 들어오면 겹친다. 겹치면 두 문의가 한 카드로
    병합돼 한쪽이 사라진다(2026-09-10 리뷰 #5 — 접수 창구에선 그림 폴더까지 섞인다).
    → 락을 쥔 채로 이미 쓰인 id 인지 보고, 쓰였으면 1초씩 밀어 빈 번호를 쓴다(형식은 그대로)."""
    try:
        with open(LOG, encoding="utf-8") as f:
            used = set(re.findall(r'"id":\s*"(INQ-\d{6}-\d{6})"', f.read()))
    except OSError:
        return cand
    try:
        t = datetime.datetime.strptime(cand, "INQ-%y%m%d-%H%M%S")
    except ValueError:
        return cand
    while cand in used:
        t += datetime.timedelta(seconds=1)
        cand = t.strftime("INQ-%y%m%d-%H%M%S")
    return cand

def append(obj, fresh_id=False):
    ensure()
    _backup_before_write()
    lockpath = LOG + ".lock"
    wrote = [False]

    def _write():
        if fresh_id:
            obj["id"] = _free_id(obj["id"])
        line = "window.INQUIRY_LOG.push(" + json.dumps(obj, ensure_ascii=False) + ");\n"
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
        wrote[0] = True

    try:
        with open(lockpath, "a+") as lf:        # 락 게이트(내용 무관)
            _lock(lf)
            try:
                _write()                        # 락 보유 중: 빈 id 확인 + 원자적 append
            finally:
                _unlock(lf)
    except Exception:
        if not wrote[0]:
            _write()                            # 락 자체 실패 시에도 기록은 유지

ap = argparse.ArgumentParser()
ap.add_argument("--new",  action="store_true", help="접수: 새 문의 기록")
ap.add_argument("--done", action="store_true", help="완료 처리(=--status 완료)")
ap.add_argument("--id",   default="", help="전이/채우기 대상 id (--new 출력값)")
ap.add_argument("--status", default="", help="접수|진행중|대기|완료")
ap.add_argument("--date", default="", help="YYYY-MM-DD (오늘)")
ap.add_argument("--type", default="", help="단순문의|일반요청|긴급문의")
ap.add_argument("--q",    default="", help="문의 요약")
ap.add_argument("--a",    default="", help="처리/답변 요약")
ap.add_argument("--by",   default="", help="기록 담당자(보통 운영담당)")
ap.add_argument("--ref",  default="", help="archive 앵커(선택, 예 #ts-account-auth)")
ap.add_argument("--req",  default="", help="요청자·조직 (카드 코드 옆 칩으로 표시, 예: 홍길동·영업·고객사A)")
ap.add_argument("--role", default="", help="요청자의 담당 업무 (예: 영업대표·영업지원·여신담당·재경) — 트리니티봇이 담당에 맞춰 답한다(2026-09-22~)")
# 트리니티봇 답 판정 — 검증 장부(vacation/ledger.py)가 모아 정확도를 낸다(2026-09-11). 현업 화면에는 나가지 않는다.
ap.add_argument("--grade", default="", choices=["", "맞음", "보완", "틀림", "제외"],
                help="트리니티봇 답 판정: 맞음(그대로 줘도 됨)|보완(방향은 맞으나 덧붙일 것)|틀림(결론이 틀림)|제외(중복·오발송 등 평가 제외)")
ap.add_argument("--grade-note", default="", help="판정 메모 — 보완·틀림이면 무엇이 달랐는지")
# 판정 이유 코드 — 메모는 글이라 집계가 안 된다. 코드로 남기면 장부가 유형별로 센다(2026-09-30). 정본 = vacation/config.json reason_codes
# (없거나 못 읽으면 아래 기본값). 대시보드 task-board.html 의 BOT_CODES 와 같아야 한다 — tests/unit_metrics.py 가 대조한다.
_DEFAULT_GRADE_CODES = ['사실오류', '위험안내', '기존기능놓침', '용어노출', '과잉보류', '누락', '가독성', '기타']


def _grade_codes():
    try:
        with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vacation", "config.json"), encoding="utf-8") as f:
            c = json.load(f).get("reason_codes")
        if isinstance(c, list) and c and all(isinstance(x, str) and x for x in c):
            return c
    except (OSError, ValueError):
        pass
    return list(_DEFAULT_GRADE_CODES)


ap.add_argument("--grade-code", default="",
                help="판정 이유 코드(쉼표로 여러 개) — 보완·틀림일 때 무엇이 문제였나. 허용: " + ", ".join(_grade_codes()))
# 정답 판정(명시적 라벨) — 혼동행렬 및 Precision/Recall 산출용 (선택 입력)
ap.add_argument("--expected-verdict", default="", choices=["", "answer", "hold", "escalate", "dev"],
                help="기대 정답 판정 (answer|hold|escalate|dev) — 혼동행렬용 명시적 정답 라벨")
# 실시간 문의 1차 답변까지 쓴 도구 라운드 수(CLAUDE.md ⚡ 절 상한 3) — 총점검 §D 효율 측정용(2026-09-29). AI 자기 기록.
ap.add_argument("--req-close", dest="req_close", action="store_true",
                help="문의자가 케이스를 닫음 — 상태 완료 + req_closed(닫은 시각) 기록. 추가 질문이 붙으면 다시 열린 것으로 본다(시각 비교)")
ap.add_argument("--req-score", type=int, default=None, choices=[1, 2, 3, 4, 5],
                help="문의자가 케이스를 닫을 때 남긴 만족도 점수 (1~5)")
ap.add_argument("--req-feedback", default="",
                help="문의자가 케이스를 닫을 때 남긴 한줄 피드백")
ap.add_argument("--rounds", type=int, default=None, help="1차 답변 전 도구 라운드 수(병렬 호출 1묶음 = 1)")
x = ap.parse_args()

if x.status and x.status not in VALID:
    sys.exit("[오류] --status 는 " + "|".join(VALID))

if x.new:
    iid = x.id or ("INQ-" + time.strftime("%y%m%d-%H%M%S"))   # 예: INQ-YYMMDD-HHMMSS (읽기 좋은 고유코드)
    status = x.status or ("완료" if x.a else "접수")
    obj = {"id": iid, "date": x.date, "type": (x.type or "단순문의"), "q": x.q,
           "by": x.by, "req": x.req, "a": x.a, "ref": x.ref, "status": status}
    if x.role:                               # 준 경우에만 — 기존 기록 모양은 그대로
        obj["role"] = x.role
    if x.rounds is not None:
        obj["rounds"] = x.rounds
    append(obj, fresh_id=not x.id)
    print("NEW id=" + obj["id"] + " status=" + status)
elif x.id:
    obj = {"id": x.id}
    if x.done:
        obj["status"] = "완료"
    elif x.status:
        obj["status"] = x.status
    if x.req_close:
        obj["status"] = "완료"
        obj["req_closed"] = time.strftime("%Y-%m-%d %H:%M")
        if x.req_score is not None:
            obj["req_score"] = x.req_score
        if x.req_feedback:
            obj["req_feedback"] = x.req_feedback.strip()
    for k in ("a", "ref", "q", "by", "req", "role", "date", "type"):   # 준 값만 갱신(빈값 무시)
        v = getattr(x, k)
        if v:
            obj[k] = v
    if x.grade:
        obj["botgrade"] = x.grade
        obj["botgrade_at"] = time.strftime("%Y-%m-%d %H:%M")    # 판정 뒤에 트리니티봇 답이 더 달리면 장부가 재판정 필요로 표시
    if x.grade_note:
        obj["botgrade_note"] = x.grade_note
    if x.grade_code:
        _codes = [c for c in re.split(r"[,\s]+", x.grade_code.strip()) if c]
        _bad = [c for c in _codes if c not in _grade_codes()]
        if _bad:
            sys.exit("[오류] --grade-code 에 없는 코드: %s (허용: %s)" % (", ".join(_bad), ", ".join(_grade_codes())))
        obj["botgrade_code"] = ",".join(dict.fromkeys(_codes))      # 중복 제거 · 입력 순서 유지
    if x.expected_verdict:
        obj["bot_expected_verdict"] = x.expected_verdict
    if x.rounds is not None:
        obj["rounds"] = x.rounds
    if len(obj) == 1:
        sys.exit("[오류] --id 만으로는 변경할 게 없음 (--status/--done/--a/--ref 등 필요)")
    if x.done and x.rounds is None:
        print("[경고] --rounds 없이 완료 처리했습니다. 즉답 문의였다면 1차 답변 전 도구 라운드 수를 --rounds N 으로 같은 id에 추가하세요(총점검 §D 효율 지표).", file=sys.stderr)
    append(obj)
    print("UPDATE id=" + x.id + (" status=" + obj["status"] if "status" in obj else ""))
else:
    sys.exit("[오류] --new (접수) 또는 --id (전이/완료) 를 지정")
