# -*- coding: utf-8 -*-
"""
test_reply.py — 시험 로그의 문의에 답을 달아 본다 (대화창 화면 확인용)

트리니티봇 감시자는 운영 로그만 보므로 시험 서버의 문의에는 답이 달리지 않는다. 화면이 답·상태·알림·
케이스 닫기를 어떻게 보여주는지 보려면 이 도구로 답을 흉내 낸다. **시험 로그(_test/)에만 쓴다.**

    python test_reply.py                      → 시험 로그의 문의 목록
    python test_reply.py <INQ> ai   "답 본문"  → 트리니티봇 확정 답변(1차 답변 완료 · 케이스 닫기 가능)
    python test_reply.py <INQ> ack             → 접수 확인만(확인 중)
    python test_reply.py <INQ> hold "사유"     → 보류(담당자 확인 대기)
    python test_reply.py <INQ> need "항목"     → 정보 되묻기(추가 정보 필요)
    python test_reply.py <INQ> human "답 본문" → 담당자 답변

표기 문구는 쓰기 게이트(vacation/apply_answer.py)의 것을 옮겨 적었다. 여기는 화면 시험용 흉내일 뿐,
게이트를 대신하지 않는다 — 운영 답변은 반드시 게이트를 거친다.
"""
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST = os.path.join(HERE, "_test")
LOG = os.path.join(TEST, "inquiry-log.js")
HELPER = os.path.join(TEST, "log-inquiry.py")

MARK_AI = "[트리니티봇 · AI 1차 안내 · 담당자 부재중]"
MARK_ACK = "[트리니티봇 · 접수 확인 · 담당자 부재중]"
TAIL = "담당자 복귀 후 확인하여 달라지는 부분이 있으면 정정드리겠습니다."
CLOSE_ASK = "이 답변으로 해결되셨으면 아래 [케이스 닫기]를 눌러 주십시오."
NEED_INFO = "정확한 안내를 위해 아래 정보가 필요합니다"
ESCALATE = "업무가 멈출 정도로 급하신 건이면 백업 담당자께 문의해 주십시오."
A_MARK = "──── 추가 답변"
Q_MARK = "──── 추가 질문"


def records():
    out = {}
    if not os.path.exists(LOG):
        return out
    with io.open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("window.INQUIRY_LOG.push("):
                continue
            try:
                o = json.loads(line[len("window.INQUIRY_LOG.push("):].rstrip(";")[:-1])
            except ValueError:
                continue
            cur = out.setdefault(o.get("id"), {})
            for k, v in o.items():
                if v not in ("", None):
                    cur[k] = v
    return out


def main(argv):
    if not os.path.exists(HELPER):
        print("시험 사본이 없습니다. 먼저 build_test.py 를 실행하십시오.")
        return 1
    recs = records()
    if len(argv) < 2:
        for rid, r in sorted(recs.items()):
            print("%s  [%s]  %s  %s" % (rid, r.get("status", ""), r.get("req", ""), (r.get("q") or "").splitlines()[0][:50]))
        if not recs:
            print("시험 로그에 문의가 없습니다.")
        return 0
    rid, kind, text = argv[0].upper(), argv[1], (argv[2] if len(argv) > 2 else "")
    rec = recs.get(rid)
    if rec is None:
        print("시험 로그에 그 문의가 없습니다: %s" % rid)
        return 1
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if kind == "ai":
        block = "%s\n%s\n%s\n%s\n⏱%s" % (MARK_AI, text or "(시험 답변)", TAIL, CLOSE_ASK, stamp)
    elif kind == "ack":
        block = "%s\n접수되었습니다. 확인 후 답변드리겠습니다.\n%s" % (MARK_ACK, ESCALATE)
    elif kind == "hold":
        block = "%s\n%s\n%s\n⏱%s" % (MARK_ACK, text or "확정할 근거가 부족해 담당자에게 보고하겠습니다.", ESCALATE, stamp)
    elif kind == "need":
        block = "%s\n%s.\n%s\n⏱%s" % (MARK_ACK, NEED_INFO, text or "· 어느 화면(T-Code)에서 · 어떤 오류 문구가 나왔는지", stamp)
    elif kind == "human":
        block = text or "(시험 — 담당자 답변)"
    else:
        print("종류는 ai · ack · hold · need · human 중 하나입니다.")
        return 2
    cur = rec.get("a") or ""
    # 추가 질문에 대한 답은 「추가 답변」 표시로 이어 붙인다. 접수 확인만 있던 자리는 답으로 바꾼다.
    if cur and "⏱" not in cur.split(A_MARK)[0] and "AI 1차 안내" not in cur.split(A_MARK)[0] and cur.startswith("[트리니티봇"):
        cur = ""
    new_a = block if not cur else "%s\n\n%s (%s) ────\n%s" % (cur, A_MARK, stamp, block)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, HELPER, "--id", rid, "--a", new_a, "--status", "대기"],
                         capture_output=True, text=True, encoding="utf-8", env=env, cwd=TEST, timeout=30)
    if out.returncode != 0:
        print("기록하지 못했습니다: %s" % (out.stderr or out.stdout)[:300])
        return 1
    again = records().get(rid) or {}
    ok = again.get("a") == new_a                    # 재조회로 확인 — 성공 메시지는 반영의 증거가 아니다
    print("%s  %s  %s" % (rid, kind, "기록 확인" if ok else "⚠ 기록이 반영되지 않았습니다"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))

# --------------------------------------------------
# Security Checklist
# Applied:
#   - OS Command Injection: the helper is run with an argument list (no shell); the script path is fixed
#   - Data separation: reads and writes only the test copy under _test/, never the production log
#   - Verification: re-reads the log after writing instead of trusting the helper's exit message
# Not Applied:
#   - [WARN] No gate: this tool imitates the bot's answer format for screen testing only. Production answers must go through vacation/apply_answer.py
# --------------------------------------------------
