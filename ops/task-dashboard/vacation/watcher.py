# -*- coding: utf-8 -*-
"""
watcher.py — 부재중 모드의 심장. 문의 파일을 지켜보다 처리할 게 있을 때만 Claude 를 부른다.

설계 요지
  · **폴링이 아니라 이벤트에 가깝게**: 5초마다 파일의 mtime/size 만 본다(비용 0).
    바뀌었을 때만 전체를 병합해 처리 대상을 계산한다.
  · **level-triggered**: 이벤트 큐를 믿지 않고 매번 파일 전체에서 처음부터 계산한다.
    죽어 있던 3시간 동안 들어온 건도 기동 즉시 잡힌다. 오프셋 방식은 쓰지 않는다
    (쓰기 도중 절반만 읽고 오프셋을 밀어버리면 그 건은 영영 안 잡힌다).
  · **빈 대기는 공짜**: 신규가 없으면 Claude 를 부르지 않는다. 비용은 「문의 건수 × 회당」 뿐.
  · **접수 확인은 LLM 없이 즉시**: 어떤 장애 상황에서도 현업 화면에 안내는 남는다.
  · **에이전트는 쓰지 않는다**: 읽기 도구만 주고 판정 JSON 만 받는다.
    파일에 닿는 것은 apply_answer.py(쓰기 게이트) 하나뿐.
    (2026-09-10 검증: 쓰기 권한을 주면 명령 패턴이 안 맞아 조용히 거부되는 사고가 실제로 났다)

종료: 상태 파일의 mode 가 off 로 바뀌면 스스로 멈춘다 (B_휴가모드_종료 가 그렇게 한다).
"""

import hashlib
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
LOG_JS = os.path.join(DASH, "inquiry-log.js")
INTAKE = os.path.join(DASH, "intake-server.py")
# 트리니티봇이 답하는 접수 출처 — 웹 접수 + 웍스 메신저(2026-11-25, C:\trinitybot-resume\works-messenger 가 등록).
# 담당자가 직접 적은 카드(by=운영담당 등)는 여전히 절대 건드리지 않는다.
INTAKE_SOURCES = ["웹접수", "웍스메신저"]
CLAUDE = os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")   # 아래 engines.engine_cmd("claude") 와 같은 값

sys.path.insert(0, HERE)
import apply_answer as GATE                                   # noqa: E402
import servers as SRV                                         # noqa: E402
import ledger as LEDGER                                       # noqa: E402
import engines as ENG                                         # noqa: E402  엔진 어댑터(2026-09-18 DEV-047)
import answer_schema as SCHEMA                                # noqa: E402  답변 규격·정규화(2026-09-18 DEV-047)
import auth_canary as CANARY                               # noqa: E402  인증 점검(2026-10-07 — 베타 첫 문의 OAuth 실패)
import prefetch as PF                                      # noqa: E402  선행 검색(2026-10-07 — 같은 질문이 9점/1점인 편차)
import runtime_sync as RSYNC                               # noqa: E402  로컬 C: 상태 ↔ G: 무손실 동기화(2026-11-26)

# 트리니티봇 호출 전용 설정(런타임 trinitybot-settings.json 으로 매 호출 다시 쓴다 — 2026-09-11)
#   · 담당자 PC 의 개인 훅(OMC 플러그인 등)을 끈다 — 도구마다 훅 프로세스가 떠 느린 PC 에서 15분 제한까지 늘어졌다
#   · 막는 규칙(deny)은 프로젝트 권한 목록의 허용보다 우선한다 — 트리니티봇이 Bash 로 grep·sed·wc 를 실제로 실행했다(행동 로그로 발견).
#     설계대로 읽기 도구 + 개발계 120 조회(+ 웹 조사, 2026-09-11 허용)만 쓰게, 셸·파일 쓰기·하위 에이전트·개발계 100(수정 가능한 연결)을 막는다.
#   · 트리니티봇이 쓸 일이 없는 기본 도구(화면 게시 · 워크플로 · 스킬 · 작업 예약 · 다른 세션 메시지 · 알림 등)도 막는다 — 막은 도구는
#     목록에서 빠져 설명문이 매 호출 실리지 않는다(2026-09-11 측정: 매 호출 고정 입력 61k → 38k 토큰).
#     ToolSearch 는 남긴다 — 웹 조사 · 개발계 120 도구 설명을 불러오는 통로다.
BOT_SETTINGS = {
    "disableAllHooks": True,
    "permissions": {"deny": ["Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit",
                             "Agent", "Task", "mcp__mcp-abap-abap-adt-api",
                             "Artifact", "Workflow", "Skill", "ScheduleWakeup", "ReportFindings", "ListAgents",
                             "SendMessage", "RemoteTrigger", "PushNotification", "Monitor", "TaskOutput", "TaskStop",
                             "CronCreate", "CronDelete", "CronList", "EnterWorktree", "ExitWorktree", "DesignSync"]},
}

# 트리니티봇에게 허용하는 도구 — 읽기 3종 + 개발계 120 조회 6종 + 웹 조사 2종.
#   목록의 정본은 `engines.py` 의 LOGICAL_TOOLS 다(엔진마다 실제 이름이 다르므로 논리 이름으로 적고 어댑터가 바꾼다).
#   여기서는 claude 이름으로 펼친 것 — 값은 2026-09-11 까지와 **동일**하다(골든 시험이 대조).
#   웹 조사는 2026-09-11 사용자 허용(「토큰 과소비는 안 하면 좋겠다」): 호출 한 번에 검색 3회 · 페이지 2개까지(프롬프트 규칙),
#   검색어는 행동 로그에 남고 사내 식별자가 섞이면 경고한다.
READ_TOOLS = ENG.tool_names("claude")


_LOGF = None


def _logfile():
    global _LOGF
    if _LOGF is None:
        try:
            cfg = GATE._cfg()
            sdir = GATE.state_dir(cfg, cfg["runtime_dir"])
            os.makedirs(sdir, exist_ok=True)
            _LOGF = os.path.join(sdir, "watcher.log")
        except Exception:
            _LOGF = ""
    return _LOGF


def log(msg):
    """⚠️ 콘솔이 없어도 절대 죽지 않는다. 그리고 **항상 파일에도 남긴다.**

    pythonw 로 뜨면 sys.stdout 이 None 이라 write 한 줄에 감시자가 통째로 죽었다
    (2026-09-10 실측). 콘솔이 없을 때 남는 증거는 이 파일뿐이다 — 런타임/watcher.log
    """
    nl = chr(10)
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    out = getattr(sys, "stdout", None)
    if out is not None:
        try:
            out.write(line + nl)
            out.flush()
        except Exception:
            pass
    f = _logfile()
    if f:
        try:
            with io.open(f, "a", encoding="utf-8", newline="") as fh:
                fh.write(line + nl)
        except Exception:
            pass


# ────────────────────────────────────────────── 상태
class State(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.gdrive_dir = cfg["runtime_dir"]
        self.dir = GATE.state_dir(cfg, self.gdrive_dir)
        os.makedirs(self.dir, exist_ok=True)
        self.p_state = os.path.join(self.dir, "state.json")
        self.p_seen = os.path.join(self.dir, "seen.json")
        self.state = self._load(self.p_state, {})
        self.seen = self._load(self.p_seen, {})

    @staticmethod
    def _load(p, default):
        if os.path.exists(p):
            try:
                return json.load(io.open(p, encoding="utf-8"))
            except ValueError:
                pass
        return dict(default)

    def _save(self, p, obj):
        tmp = p + ".tmp"
        io.open(tmp, "w", encoding="utf-8", newline="").write(
            json.dumps(obj, ensure_ascii=False, indent=1))
        os.replace(tmp, p)

    # 감시자가 소유한 필드만. mode/started_at/baseline_id/ended_at 은 **mode.py 소유**다.
    OWN = ("last_cycle", "answered_total", "runs_count", "runs_date",
           "sp_count", "sp_date",
           "intake_down_since", "intake_down_total", "_hc_at", "_hc_restart_at",
           "saver_down_since", "saver_down_total",
           "desk_down_since", "desk_down_total",
           "_hc_restart_at_intake", "_hc_restart_at_saver", "_hc_restart_at_desk")

    def flush(self):
        """⚠️ 상태 파일을 통째로 덮어쓰면 안 된다.

        감시자는 기동 시점의 state 를 메모리에 들고 있는데, 그 사이 사람이 「복귀·종료」를
        눌러 mode 를 off 로 바꿔도 다음 flush 가 옛 mode=on 을 되살려 버린다.
        실제로 2026-09-10 에 끄기가 되돌려져 모드가 계속 on 으로 남았다.
        → **매번 파일을 다시 읽어, 내가 소유한 필드만 얹는다.**
        """
        cur = self._load(self.p_state, {})
        for k in self.OWN:
            if k in self.state:
                cur[k] = self.state[k]
        self._save(self.p_state, cur)
        self.state.update({k: v for k, v in cur.items() if k not in self.OWN})
        self._save(self.p_seen, self.seen)

    def mode_on(self):
        return self.state.get("mode") == "on"

    def runs_today(self):
        d = time.strftime("%Y-%m-%d")
        if self.state.get("runs_date") != d:
            self.state["runs_date"] = d
            self.state["runs_count"] = 0
        return self.state.get("runs_count", 0)

    def bump_run(self):
        self.runs_today()
        self.state["runs_count"] = self.state.get("runs_count", 0) + 1

    def second_passes_today(self):
        d = time.strftime("%Y-%m-%d")
        if self.state.get("sp_date") != d:
            self.state["sp_date"] = d
            self.state["sp_count"] = 0
        return self.state.get("sp_count", 0)

    def bump_second_pass(self):
        self.second_passes_today()
        self.state["sp_count"] = self.state.get("sp_count", 0) + 1

    def sync_gdrive(self, full=False):
        try:
            if full:
                return RSYNC.reconcile_all(self.dir, self.gdrive_dir)
            return RSYNC.sync_state_now(self.dir, self.gdrive_dir)
        except Exception:
            return False


def intake_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("intake_mod", INTAKE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ────────────────────────────────────────────── 처리 대상 판정 (전부 무료)
def pending(st, ix):
    """처리해야 할 문의 id 목록. 긴급문의 먼저, 그 다음 접수 순."""
    merged = ix._read_log()
    base = st.state.get("baseline_id", "")
    now = time.time()
    out = []
    for iid, rec in merged.items():
        if rec.get("by") not in INTAKE_SOURCES:
            continue                                   # 담당자가 적은 카드는 절대 건드리지 않는다
        if "(웍스 메신저 접수 중)" in (rec.get("q") or ""):
            continue                                   # 웍스 브리지 사진 첨부·본문 갱신 전 임시 표지(경합 방지)
        s = st.seen.setdefault(iid, {})
        snap = s.get("answered_hash")
        a = rec.get("a") or ""
        status = rec.get("status") or "접수"

        # 켜기 전 건이라도 **추가 질문이 붙으면 지문이 달라지므로 다시 잡힌다.**
        # (mode.py seed_seen 이 켜는 순간 답이 있던 건에만 지문을 심는다.
        #  답이 없던 건은 지문 없이 backlog 로 남아 **처리 대상**이다 — 2026-09-10 수정.)
        # 기준선은 seen 이 유실됐을 때 **이미 AI 가 답한 건을 두 번 답하지 않게**만 막는다.
        # 미답변 건까지 막으면 켜기 전 문의가 영영 방치된다(실측 INQ-YYMMDD-HHMMSS).
        if base and iid <= base and snap is None and GATE.answer_turns(a) >= GATE.q_turns(rec):
            continue                                   # 물은 만큼 이미 답했다(리뷰 #1 — 턴 수로 판정)

        if s.get("human_locked"):
            continue
        if a and not a.startswith(GATE.BOT_PREFIXES):
            s["human_locked"] = True                   # 사람이 답을 썼다 → 영구 제외
            continue
        if status == "완료":
            continue
        if s.get("inflight_until", 0) > now:
            continue
        if s.get("attempts", 0) >= st.cfg["max_attempts"] and s.get("attempt_hash") in (None, _h(rec)):
            continue                                   # 같은 내용으로 여러 번 실패 — 새 추가 질문이 붙으면 다시 센다
        if snap == _h(rec):
            continue                                   # 이 내용으로는 이미 답했다
        out.append(iid)

    out.sort(key=lambda i: (merged[i].get("type") != "긴급문의", i))
    return out, merged


def _h(rec):
    """apply_answer.q_hash 를 그대로 쓴다 — 감시자와 mode.py 가 다른 지문을 쓰면
    「이미 답했다」 판정이 어긋나 같은 건에 두 번 답하거나 영영 안 답하게 된다."""
    return GATE.q_hash(rec)


# ────────────────────────────────────────────── Claude 호출
GRADE_MAX_NOTES = 20         # 교정 노트(보완·틀림)는 최근 판정순으로 이만큼만 — 프롬프트가 한없이 길어지지 않게
GRADE_MAX_OK = 40            # 맞음 목록도 최근순 이만큼만
GRADE_NOTE_CHARS = 300

GRADE_RULES = """## 선례 판정 · 교정 노트 — 선례를 쓰기 전에 반드시 먼저 보십시오
트리니티봇이 예전에 쓴 답(답변 첫 줄이 「[트리니티봇」 또는 「[담당자 부재중」)은 담당자가 **맞음 · 보완 · 틀림**으로 판정합니다.
판정은 `inquiry-log.js` 의 `botgrade`(판정) · `botgrade_note`(메모) 필드에 있고(같은 id 의 줄을 병합한 최종값), 아래에 모아 두었습니다.
- **틀림** 으로 판정된 트리니티봇 답은 선례(근거)로 쓰지 마십시오. 같은 문의에 담당자가 나중에 고친 답(트리니티봇 표기가 없는 최종 답)이 있으면 그것을 보십시오.
- **보완** 으로 판정된 답은 메모에 적힌 빠진 점까지 반영해야 선례로 쓸 수 있습니다.
- **맞음** 으로 판정된 답은 선례로 써도 됩니다.
- **판정 전** 트리니티봇 답(아래 목록에 없는 트리니티봇 답)은 **검증되지 않은 AI 답**입니다. 그것만으로 결론을 내지 말고 아카이브·소스로 다시 확인하십시오.
- 교정 노트는 트리니티봇이 과거에 틀리거나 빠뜨린 방식입니다. 지금 문의에 같은 함정이 있는지 먼저 확인하십시오.
- 판정 · 메모 · 선례 번호는 **내부 기록**입니다. `public` 에 옮기지 마십시오(필요하면 `note` 에).
- 처리할 문의에 `이전_트리니티봇_답_판정` 이 있으면, 그 문의에 트리니티봇이 앞서 쓴 답이 그렇게 판정됐다는 뜻입니다 — 그 판정·메모를 반영해 답하십시오.
"""


def _one_line(t, n):
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[:n] + "…"


def grade_section(merged, cfg=None, ix=None, exclude=()):
    """담당자 판정을 트리니티봇에게 되돌려 주는 프롬프트 절(2026-09-11 사용자 지시 「둘 다 붙이기」).

    검증 장부(ledger.py)와 **같은 기준**으로 트리니티봇이 답한 문의·판정을 모은다. 판정 목록을 만들다 실패해도
    규칙은 넣고 호출은 계속한다 — 판정 기능 때문에 트리니티봇이 멈추면 안 된다.
    """
    try:
        norm = ix._norm if ix is not None and hasattr(ix, "_norm") else (lambda t: "".join((t or "").split()))
        rows = LEDGER.build(cfg or GATE._cfg(), merged, norm)
    except Exception as e:                                      # noqa: BLE001
        log("⚠ 판정 목록을 만들지 못했습니다 — 프롬프트에 규칙만 넣습니다: %s" % e)
        return GRADE_RULES + "\n(판정 목록을 불러오지 못했습니다 — 트리니티봇 답 선례는 모두 **판정 전**으로 취급하십시오.)\n\n"
    skip = set(exclude or ())
    graded = [x for x in rows if x["grade"] and x["id"] not in skip]
    miss = sorted((x for x in graded if x["grade"] in ("틀림", "보완")),
                  key=lambda x: (x["graded_at"], x["id"]), reverse=True)
    ok = sorted((x for x in graded if x["grade"] == "맞음"),
                key=lambda x: (x["graded_at"], x["id"]), reverse=True)
    L = [GRADE_RULES]
    if not graded:
        L.append("(아직 판정된 트리니티봇 답이 없습니다 — 트리니티봇 답 선례는 모두 **판정 전**입니다.)")
    if miss:
        L.append("### 교정 노트 — 보완 · 틀림 (최근 판정순 %d건%s)"
                 % (min(len(miss), GRADE_MAX_NOTES),
                    " · 외 %d건은 생략" % (len(miss) - GRADE_MAX_NOTES) if len(miss) > GRADE_MAX_NOTES else ""))
        for x in miss[:GRADE_MAX_NOTES]:
            L.append("- `%s`%s · **%s** · 문의: %s · 메모: %s%s"
                     % (x["id"], " (담당자 시험 문의)" if x["test"] else "", x["grade"], _one_line(x["q"], 80),
                        _one_line(x["note"], GRADE_NOTE_CHARS) or "(메모 없음)",
                        " · (판정 뒤에 트리니티봇 답이 더 달림 — 판정은 그 앞 답까지)" if x["stale"] else ""))
    if ok:
        L.append("### 맞음으로 판정된 트리니티봇 답 — 선례로 써도 됩니다 (최근순 %d건%s)"
                 % (min(len(ok), GRADE_MAX_OK),
                    " · 외 %d건" % (len(ok) - GRADE_MAX_OK) if len(ok) > GRADE_MAX_OK else ""))
        L.append(", ".join("`%s`" % x["id"] for x in ok[:GRADE_MAX_OK]))
    return "\n".join(L) + "\n\n"


def user_feedback_section(merged, exclude=None):
    """현업 사용자가 케이스를 닫으며 남긴 만족도 점수와 피드백을 트리니티봇에게 전달하여 답변 품질을 자가 개선(Self-Improvement)하게 한다."""
    skip = set(exclude or ())
    feedbacks = []
    for iid, r in merged.items():
        if iid in skip:
            continue
        sc = r.get("req_score")
        fb = (r.get("req_feedback") or "").strip()
        if sc is not None or fb:
            feedbacks.append({
                "id": iid,
                "score": sc,
                "feedback": fb,
                "q": r.get("q") or "",
                "closed": r.get("req_closed") or r.get("date") or ""
            })
    if not feedbacks:
        return ""
    feedbacks.sort(key=lambda x: (x["closed"], x["id"]), reverse=True)
    lines = ["## 현업 만족도 피드백 (문의자가 케이스를 닫으며 남긴 평가 — 자가 개선 참고용)",
             "현업이 남긴 불만(낮은 점수)과 의견을 반영하여, 동일한 오안내(권한 없는 화면·테이블 안내, 불필요한 개발 안내 등)가 반복되지 않도록 스스로 교정하십시오."]
    low = [f for f in feedbacks if f["score"] is not None and f["score"] <= 3]
    high = [f for f in feedbacks if f["score"] is not None and f["score"] >= 4]
    if low:
        lines.append("### ⚠️ 현업 불만 및 개선 요청 (낮은 점수 피드백)")
        for f in low[:10]:
            lines.append("- `%s` (★%s) · 문의: %s · 현업 의견: \"%s\""
                         % (f["id"], f["score"] or "-", _one_line(f["q"], 60), f["feedback"] or "별도 의견 없음"))
    if high:
        lines.append("### ✅ 현업 우수 평가 (만족 피드백)")
        for f in high[:5]:
            lines.append("- `%s` (★%s) · 문의: %s%s"
                         % (f["id"], f["score"], _one_line(f["q"], 60), (" · \"%s\"" % f["feedback"]) if f["feedback"] else ""))
    return "\n".join(lines) + "\n\n"


# ② 운영규칙의 트리니티봇 관련 절 — 매 호출 **파일에서 잘라** 붙인다(트리니티봇이 파일을 나눠 읽던 턴을 없앤다).
#    ⚠️ 운영규칙의 절 제목을 바꾸면 여기도 함께 — 못 찾으면 트리니티봇에게 파일을 직접 읽으라고 안내한다.
RULES_DOC = os.path.join(DASH, "bot-operating-rules.md")
_DIGEST_PARTS = (("## 0.", "## 1."), ("## 2.", "### 상태 표기"), ("## 3.", "## 7."))


def rules_digest():
    try:
        t = io.open(RULES_DOC, encoding="utf-8").read()
    except OSError:
        t = ""
    parts = []
    for a, b in _DIGEST_PARTS:
        i = t.find("\n" + a)
        j = t.find("\n" + b, i + 1) if i >= 0 else -1
        if i < 0 or j < 0:
            log("⚠ 운영규칙에서 「%s」~「%s」 절을 찾지 못했습니다 — 트리니티봇에게 파일을 직접 읽으라고 안내합니다" % (a, b))
            return ("## 운영규칙\n운영규칙 요약을 만들지 못했습니다 — `task-dashboard/bot-operating-rules.md` 의 "
                    "§0 · §2 · §3~§6 을 직접 읽고 그대로 판정하십시오.\n\n")
        parts.append(t[i + 1:j].strip())
    return ("## 운영규칙 — 트리니티봇 관련 절 (파일에서 그대로 옮김 · §1 켜고 끄기 · §7 보고 · §8 한계는 담당자용이라 뺐습니다)\n\n"
            + "\n\n".join(parts) + "\n\n")


def test_ids(merged, cfg):
    """③ 검증용 요청자(config test_reqs)가 넣은 시험 문의 — 선례로 쓰지 말라고 알린다(담당자 본인 시험 문의는 제외 — 실제 질문이다)."""
    reqs = set("".join((x or "").split()) for x in ((cfg or {}).get("test_reqs") or []))
    if not reqs:
        return []
    return sorted(i for i, r in merged.items() if "".join((r.get("req") or "").split()) in reqs)


def prefetch_section(batch, merged, cfg=None):
    """문의마다 코드가 먼저 찾은 아카이브 후보·선례 절(prefetch.py). 설정 `prefetch_enabled`(기본 꺼짐 — 효과 확인 전) ·
    환경변수 TRINITYBOT_PREFETCH(1/0)가 있으면 그것이 우선(재생 평가가 켠다). 실패해도 프롬프트 조립을 막지 않는다."""
    try:
        env = os.environ.get("TRINITYBOT_PREFETCH")
        on = (env == "1") if env in ("0", "1") else bool((cfg or {}).get("prefetch_enabled", False))
        if not on:
            return ""
        return PF.section(batch, merged, os.path.join(os.path.dirname(DASH), "archive.html"), cfg)
    except Exception:                                           # noqa: BLE001
        return ""


SECOND_NOTE = """## 2차 시도 안내 — 이번 호출은 1차에서 보류·에스컬레이션한 문의만 다시 봅니다
아래 각 문의는 1차 호출에서 `hold` 또는 `escalate` 로 판정됐습니다(`1차_판정` — 메모는 참고만, 그대로 믿지 마십시오).
이번에는 바로 아래 「자동 선행 검색 결과」(후보 아카이브 발췌 · 선례)가 함께 주어집니다. 그 근거로 **결론을 확정할 수 있으면** `answer`(개발 요청이면 `dev`)로 바꿔 답하고,
**여전히 근거가 부족하면 `hold`/`escalate` 를 유지**하십시오 — 근거 없이 단정하지 마십시오. 권한 부여 요청·개인정보는 규칙대로 보류입니다(권한·비밀번호 문의의 요청 경로 안내는 1차 호출에서 `answer` 로 처리됩니다).
검색 결과가 문의와 어긋나면 무시하십시오. 맞아 보이는 후보는 해당 절을 끝까지 읽고 확인한 뒤 쓰십시오.

"""


def build_prompt(batch, merged, ix=None, cfg=None, second=None):
    """second = {문의 id: 1차 답 dict} 이면 2차 시도 프롬프트 — 1차 판정을 붙이고 선행 검색 결과를 **반드시** 넣는다(설정과 무관)."""
    items = []
    for iid in batch:
        r = merged[iid]
        # 스크린샷 — 글 속 [[사진:N]] 자리의 그림 파일. 에이전트가 Read 로 직접 본다(리뷰 #9)
        shots = []
        if ix is not None:
            try:
                shots = [os.path.join(ix.SHOT_DIR, iid, "%d.%s" % (c["n"], c["ext"]))
                         for c in ix._shot_list(iid)]
            except Exception:
                shots = []
        item = {"id": iid, "type": r.get("type"), "date": r.get("date"),
                "요청자": r.get("req"),
                # 담당 업무 — 같은 질문도 담당에 따라 답이 갈린다(운영규칙 §3 · 2026-09-22~). 빈 칸은 명시해 되묻게 한다
                "담당": (r.get("role") or "").strip() or "(미기재)",
                "문의": r.get("q"), "사진": shots}
        if r.get("by") == "웍스메신저":
            item["채널"] = "웍스 메신저 대화"          # 대화체로 답한다(PROMPT_TAIL 「웍스 메신저 건」 — 2026-11-26 소유자 지시)
        # 이 문의에 트리니티봇이 앞서 쓴 답을 담당자가 판정해 두었으면 함께 넘긴다(추가 질문으로 다시 잡힌 경우)
        g = (r.get("botgrade") or "").strip()
        if g in LEDGER.GRADES:
            item["이전_트리니티봇_답_판정"] = {"판정": g,
                                          "메모": _one_line(r.get("botgrade_note"), GRADE_NOTE_CHARS)}
        if second and iid in second:
            item["1차_판정"] = {"판정": second[iid].get("verdict"), "메모": _one_line(second[iid].get("note"), 500)}
        items.append(item)
    head = PROMPT_HEAD
    tail = PROMPT_TAIL % (json.dumps(items, ensure_ascii=False, indent=1),)
    tests = [i for i in test_ids(merged, cfg or GATE._cfg()) if i not in batch]
    tblock = ("## 선례로 쓰지 말 시험 문의 (시스템 검증용 — 실제 업무 질문이 아닙니다)\n%s\n\n" % ", ".join(tests)) if tests else ""
    if second:
        pf = PF.section(batch, merged, os.path.join(os.path.dirname(DASH), "archive.html"), cfg or GATE._cfg())
        return head + rules_digest() + grade_section(merged, cfg, ix, exclude=batch) + user_feedback_section(merged, exclude=batch) + tblock + SECOND_NOTE + pf + tail
    return head + rules_digest() + grade_section(merged, cfg, ix, exclude=batch) + user_feedback_section(merged, exclude=batch) + tblock + prefetch_section(batch, merged, cfg or GATE._cfg()) + tail


# 프롬프트의 고정 문장 — 2026-10-16 build_prompt 안에서 모듈 상수로 꺼냈다(글자는 그대로 · 골든 시험이 바이트를 대조).
# prompt_fingerprints() 가 이 둘(+ GRADE_RULES · SECOND_NOTE)로 「어느 판의 문장으로 답했나」 지문을 만든다.
PROMPT_HEAD = """당신은 담당자 부재중에 SD 문의를 1차 대응하는 에이전트입니다.

아래 「운영규칙 — 트리니티봇 관련 절」대로 판정하십시오. 특히 §3 의 휴가 프로토콜 3갈래(일반 SAP 문의 / 일반 개발요청 / 긴급 PRD 데이터 처리)를 따르십시오.
운영규칙 파일은 따로 열지 마십시오 — 트리니티봇에 필요한 절은 이미 아래에 붙어 있고, 나머지 절은 담당자용입니다.

조사 순서: `archive-structure.md` → 아카이브 아티클 해당 절 → 선례(`task-dashboard/inquiry-log.js`)
→ 관련 DEV/OPS 카드(`task-dashboard/tasks.md`, 있으면 — 키워드 Grep 후 `tasks-index.md` 라인범위로 그 카드만 Read. 방향 참고용)
→ 필요할 때만 DEV 120 **조회**. 운영(PRD)은 볼 수 없으니 **현업이 직접 볼 화면·필드·키값을 안내**하십시오.
⛔ DEV/OPS 카드에서 얻은 것은 **참고만** 합니다 — 카드 번호(`DEV-###`/`OPS-###`)·문의 번호·SR/변경 ID·요청자 이름을 public 에 절대 쓰지 마십시오(근거로 쓰면 note 에 카드 ID만).
선례를 근거로 쓰기 전에 아래 「선례 판정 · 교정 노트」와 「현업 만족도 피드백」을 먼저 보십시오 — 틀림으로 판정된 트리니티봇 답은 근거가 아닙니다. 현업의 불만 피드백이 있었던 오안내도 피하십시오.
⛔ DEV 120 소스는 개발계 소스이므로 현재 개발 중인 CTS 전송요청에 묶여 있어 운영계(PRD) 현행 로직과 다를 수 있습니다! 최근 CTS 변경 여부를 확인하고, 현행 로직이 확실하지 않으면 현업에게 운영 화면 실측을 요구하십시오.
⛔ 현업 대상 안내 시 `SE16N`·`SE16`·`SE11` 등 테이블 뷰어 안내 절대 금지 — 현업은 테이블 권한이 없습니다. 오직 실제 비즈니스 트랜잭션 화면(T-Code)만 안내하십시오.

웹 조사(WebSearch · WebFetch)는 아카이브 · 선례 · 개발계 조회로 결론이 나지 않을 때만 씁니다(예: SAP 표준 동작 확인).
**호출 한 번에 검색 3회 · 페이지 읽기 2개까지** — 토큰을 아낍니다. 필요한 부분만 묻고, 긴 페이지를 통째로 읽지 마십시오.
⛔ 검색어 · 주소에 회사명 · 고객명 · 사람 이름 · 실제 문서번호 · 사번 · 금액 · 사내 Z T-Code 를 넣지 마십시오(검색은 외부로 나가는 행위입니다).
웹에서 얻은 것은 근거 등급 C 입니다 — 회사 고유 동작에 그대로 적용하지 말고, note 에 출처 주소를 남기십시오.

문의 글 속 `[[사진:N]]` 은 현업이 그 자리에 붙인 스크린샷입니다. 각 문의의 "사진" 목록에 있는 그림 파일을
**Read 로 열어 직접 확인하십시오**(파일 이름의 번호 = N). 그림 속 문구·화면이 결론을 가르는데 그림을
열 수 없으면 추측하지 말고 hold 로 두거나, 문구를 글로 적어 달라고 요청하십시오.

⛔ 당신에게는 파일을 쓸 권한이 없습니다. 쓰려고 시도하지 마십시오.
   기록은 별도의 쓰기 게이트가 합니다. 당신은 **판정 결과만 JSON 으로** 돌려주면 됩니다.
   표기(「[트리니티봇 · AI 1차 안내 · 담당자 부재중]」)와 정정 안내 문구도 게이트가 자동으로 붙입니다 — 쓰지 마십시오.

"""

# ⚠️ 판정 절은 % 서식 밖에서 붙인다 — 담당자 메모에 %·{ 가 들어 있어도 프롬프트가 깨지지 않게
PROMPT_TAIL = """## public(현업이 읽을 답) 쓰는 법 — 읽기 쉽게 (2026-09-11 담당자 판정: 「가독성이 떨어짐. 적절한 줄 바꿈이 필요」)
- 첫 줄은 결론 한 문장. 그 뒤에 빈 줄 하나.
- 절차는 ① ② ③ 번호를 붙여 한 줄에 하나씩. 한 줄에는 한 동작만.
- 주의·참고는 「주의:」로 시작하는 따로 된 문단.
- 한 문단은 3줄 이하, 문단 사이는 빈 줄 하나. 전체 15줄 안팎.
- 현업 화면은 글자를 그대로 보여 줍니다 — 굵게·제목·표 같은 마크다운 기호로 꾸미지 마십시오. 글머리가 필요하면 ① ② ③ 이나 · 를 씁니다.

## "채널": "웍스 메신저 대화" 인 건 — 위 형식 대신 이렇게 (2026-11-26 소유자 지시: 「템플릿 형식 말고 평범하게 말하듯」)
- 담당자가 메신저로 동료에게 직접 말하듯 자연스러운 존댓말 대화체로 씁니다. ①②③ 번호 목록·「주의:」 문단·정형 머리말을 쓰지 마십시오.
- 짧게: 보통 2~6문장. 필요한 확인 요청은 문장 속에 한두 개만 자연스럽게 묻습니다.
- 사진·앞선 대화에 이미 나온 정보(오류 메시지, 화면 값, 상대가 이미 한 설명)를 다시 묻지 마십시오. 사진을 먼저 꼼꼼히 읽고 그 내용을 근거로 말합니다.
- 같은 대화에서 앞서 한 말(부재 안내, 긴급 연락처, 반복 시도 금지 같은 상투 문구)을 되풀이하지 마십시오. 트리니티봇 표기는 시스템이 붙이니 본문에 쓰지 않습니다.
- 위험한 조작(삭제·취소·전기·저장 반복 등)을 피하라는 말이 꼭 필요할 때만 한 문장으로 덧붙입니다.

## 처리할 문의
%s

## 반드시 이 형식으로만 답하십시오 (JSON 하나, 다른 말 없이)
```json
{"answers":[
  {"id":"INQ-...",
   "verdict":"answer | hold | escalate | dev",
   "public":"현업이 읽을 답변 본문. 결론 1줄 → 절차 → 주의. T-Code 는 「코드 — 화면명」. 경어체. DEV/OPS 카드·문의·SR/변경 번호는 쓰지 않습니다.",
   "note":"담당자용 내부 메모(근거·소스·불확실한 점). 현업에게는 절대 안 갑니다.",
   "ref":"#아카이브앵커 (없으면 빈 문자열)"}
]}
```

판정 기준
- `answer`  : 아카이브·선례·DEV 120 조회로 **결론이 확정**되는 일반 SAP 문의
- `dev`     : 개발 요청 — 가능여부·개발 방향·영향 범위를 public 에, 상세는 note 에
- `escalate`: 긴급 PRD 데이터 처리 등 직접 처리 불가 — 왜 안 되는지 설명 (팀장 안내는 게이트가 붙임). 금액·결산·세금계산서라도 **확인·진단 문의는 escalate 가 아닙니다**(아래 hold 참고)
- `hold`    : 판단이 갈리거나 근거가 추정에 그침 / 권한·롤을 대신 부여해 달라는 요청 · 개인정보 열람 — public 은 비워도 됩니다
              (「내 권한이 있나 · 화면이 안 열린다 · 권한 오류」 문의와 비밀번호 초기화·계정 잠금 해제 요청은 `hold` 가 아니라 `answer` — 운영 SAP 비밀번호 초기화는 회사의 셀프서비스 경로(운영규칙 「권한 문의」 절에 회사별로 적는다)를 안내하고, 봇이 초기화하거나 비밀번호를 알려 주지는 않습니다.)
              (금액·결산·세금계산서의 **확인·진단 문의**는 담당자에게 넘기기 전에 요청자가 읽기 전용으로 확인해 올 경로(「코드 — 화면명」·볼 필드·읽는 법)를 먼저 안내합니다 — 되묻기 틀의 `hold` 또는 조건 분기 `answer`, 값이 오면 진단. 담당자 몫은 요청자 권한으로 못 보는 것과 데이터를 바꾸는 조작뿐입니다. 운영규칙 「운영 값 확인·진단」)

확신이 없으면 `hold` 입니다. 부재중에는 틀린 답을 잡아줄 사람이 없습니다."""

# 답이 어느 판에서 나왔나 — 1단계 추적성(2026-10-16). 앞 8자리 SHA-1 셋을 answered 기록(meta)에 남긴다.
ARCHIVE_STRUCT = os.path.join(ROOT, "archive-structure.md")


def _sha8(b):
    return hashlib.sha1(b).hexdigest()[:8]


def prompt_fingerprints():
    """{"prompt_fp", "rules_fp", "archive_fp"} — 고정 문장(머리 · 꼬리 · 판정 규칙 · 2차 안내) · 매 호출 붙이는 운영규칙 요약(rules_digest) ·
    아카이브 색인(archive-structure.md 파일 바이트). 셋 중 하나가 바뀌면 그 앞뒤 답의 정확도를 장부에서 갈라 본다.
    지문 계산이 실패해도 호출을 막지 않는다 — 실패한 칸은 빈 문자열."""
    out = {"prompt_fp": "", "rules_fp": "", "archive_fp": ""}
    try:
        out["prompt_fp"] = _sha8((PROMPT_HEAD + PROMPT_TAIL + GRADE_RULES + SECOND_NOTE).encode("utf-8"))
        out["rules_fp"] = _sha8(rules_digest().encode("utf-8"))
    except Exception:                                           # noqa: BLE001
        pass
    try:
        with io.open(ARCHIVE_STRUCT, "rb") as fh:
            out["archive_fp"] = _sha8(fh.read())
    except OSError:
        pass
    return out


_SUPERSEDED = "_SUPERSEDED_"


def _make_abort_checker(ix, batch, batch_hashes):
    """호출 도중 처리 대상 문의에 추가 질문·종결·담당자 답변이 붙었는지 5초마다 확인한다(파일 크기·수정시각 변동 시에만 파싱)."""
    log_path = getattr(ix, "LOG_JS", None) or LOG_JS
    last_sig = [None, None]

    def _check():
        try:
            st1 = os.stat(log_path)
            cur_sig = [getattr(st1, "st_mtime_ns", st1.st_mtime), st1.st_size]
        except OSError:
            return False
        if cur_sig == last_sig:
            return False
        last_sig[0], last_sig[1] = cur_sig[0], cur_sig[1]
        try:
            latest = ix._read_log()
        except Exception:                                       # noqa: BLE001
            return False
        for iid in batch:
            rec = latest.get(iid) or {}
            if _h(rec) != batch_hashes.get(iid):
                log("  🔄 처리 중 추가 메시지 감지 (%s) — 최신 내용을 반영해 즉시 다시 시작합니다" % iid)
                return True
            a = rec.get("a") or ""
            if (a and not a.startswith(GATE.BOT_PREFIXES)) or (rec.get("status") or "") == "완료" or (hasattr(ix, "_req_closed") and ix._req_closed(rec)):
                log("  🔄 처리 중 상태 변경 감지 (%s) — 현재 호출을 중단합니다" % iid)
                return True
        return False

    return _check


def _invoke_engine(eng, st, prompt, tag, extra_dirs=(), model=None, should_abort=None):
    """지정된 엔진의 CLI 프로세스를 띄우고 stream-json 출력을 수신하여 정규화된 결과 dict 를 반환한다."""
    rt = st.dir
    outp = os.path.join(rt, "runs", "%s.stream.jsonl" % tag)
    errp = os.path.join(rt, "runs", "%s.err.txt" % tag)
    inv = ENG.build_invocation(eng, {"rt": rt, "root": ROOT,
                                     "cmd": CLAUDE if eng == "claude" else None,
                                     "mcp_config": os.path.join(ROOT, ".mcp.json"),
                                     "settings_obj": BOT_SETTINGS, "extra_dirs": extra_dirs,
                                     # 2026-11-18 담당자 결정: Claude 모델 고정(config.json claude_model) — CLI 「권장」이 11-17 소넷→오퍼스로 바뀌어
                                     # 주간 한도 소진 위험이 커졌다. 비우면 예전처럼 「권장」을 따른다.
                                     "model": (model or st.cfg.get("gemini_model")) if eng == "gemini"
                                              else (model or st.cfg.get("agy_model") or None) if eng == "agy"
                                              else (model or st.cfg.get("claude_model") or None),
                                     "effort": st.cfg.get("agy_effort", "medium")})
    cmd, env = inv["argv"], inv["env"]
    for note in inv.get("notes") or ():
        log("  ⚠ %s" % note)
    t0 = time.time()
    with io.open(outp, "wb") as fh, io.open(errp, "wb") as efh:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fh,
                                 stderr=efh, cwd=ROOT, env=env)
            try:
                p.stdin.write(ENG.format_stdin(eng, prompt).encode("utf-8"))
                p.stdin.close()
            except OSError:
                pass
            deadline = time.time() + st.cfg["claude_timeout_sec"]
            hb = GATE.heartbeat_path(st.cfg)
            abort_fn = should_abort or getattr(st, "_should_abort", None)
            while True:
                try:
                    p.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    if abort_fn and abort_fn():
                        try:
                            with io.open(hb, "a", encoding="utf-8", newline="") as f:
                                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    "busy": tag + ":superseded-restart"},
                                                   ensure_ascii=False) + "\n")
                        except OSError:
                            pass
                        try:
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=60)
                        except Exception:                       # noqa: BLE001
                            pass
                        try:
                            p.kill()
                        except OSError:
                            pass
                        return None, _SUPERSEDED
                    if time.time() > deadline:
                        raise
                    try:
                        with io.open(hb, "a", encoding="utf-8", newline="") as f:
                            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                "busy": tag}, ensure_ascii=False) + "\n")
                    except OSError:
                        pass
            rc = p.returncode
        except subprocess.TimeoutExpired:
            try:
                with io.open(GATE.heartbeat_path(st.cfg), "a", encoding="utf-8", newline="") as f:
                    f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "busy": tag + ":timeout-kill"},
                                       ensure_ascii=False) + "\n")
            except OSError:
                pass
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=60)
            except Exception:                                   # noqa: BLE001
                pass
            try:
                p.kill()
            except OSError:
                pass
            return None, "시간초과(%ds)" % st.cfg["claude_timeout_sec"]
        except OSError as e:
            return None, "실행 실패: %s" % e
    raw = io.open(outp, encoding="utf-8", errors="replace").read()
    err = io.open(errp, encoding="utf-8", errors="replace").read() if os.path.exists(errp) else ""
    model_s = ENG.model_of(eng, raw.splitlines())
    try:
        beh = behavior_of(raw.splitlines() if eng == "claude" else ENG.claude_shaped_lines(eng, raw.splitlines()))
        GATE.journal(st.cfg, dict({"event": "behavior", "tag": tag, "rc": rc, "engine": eng, "model": model_s}, **beh))
        log("  행동: " + behavior_line(beh))
    except Exception as e:                                      # noqa: BLE001
        beh = {}
        log("  (행동 기록 실패: %s)" % e)
    try:                                                        # ref 대조(apply_answer.ref_check)용 — 이번 호출에서 Read 로 연 파일 · 줄 범위
        reads = read_windows(raw.splitlines() if eng == "claude" else ENG.claude_shaped_lines(eng, raw.splitlines()),
                             2000 if eng == "claude" else None)
    except Exception:                                           # noqa: BLE001
        reads = []
    d = (stream_result(raw) if eng == "claude" else ENG.parse_result(eng, raw)) or first_json(raw)
    if isinstance(d, dict):
        d["_behavior"] = beh
        d["_reads"] = reads
        d["_engine"], d["_model"] = eng, model_s
        d["_sec"], d["_rc"] = round(time.time() - t0, 1), rc
    if d is None:
        if rc != 0:
            return None, "종료코드 %s: %s" % (rc, (err or raw)[:200])
        return None, "출력이 JSON 이 아닙니다: %s" % (raw or err)[:200]
    return d, None


def call_claude(st, prompt, tag, extra_dirs=(), model=None):
    """stdout 을 파이프가 아니라 파일로 받는다 — 손자 프로세스가 파이프를 물고 있으면 교착한다."""
    rt = st.dir
    os.makedirs(os.path.join(rt, "runs"), exist_ok=True)
    promptp = os.path.join(rt, "runs", "%s.prompt.md" % tag)
    io.open(promptp, "w", encoding="utf-8", newline="").write(prompt)

    eng = ENG.resolve(st.cfg)
    d, err = _invoke_engine(eng, st, prompt, tag, extra_dirs, model=model if eng == "claude" else None)
    if err == _SUPERSEDED:
        return None, _SUPERSEDED

    # ⚡ 자동 페일오버: Claude 가 한도 소진이면 Gemini(공식 CLI · 격리 정책)가 이어받는다 — engines.check_usable 관문(사람이 켠 verified)을 통과할 때만
    # 전환 조건은 **한도 소진만**(2026-09-22 좁힘) — 종전 `or err` 는 시간초과·일시 오류에도 넘어가 한 문의에 두 엔진 시간을 썼다.
    if eng == "claude" and ENG.is_exhausted("claude", d=d, err=err):
        fail_why = (d or {}).get("result") or err or "Claude 한도 소진(Weekly/Rate Limit)"
        for fb_eng in ("agy",):  # 2026-11-26 담당자 지시: gemini-3.8-flash 페일오버 금지, agy(수소 모델)만 사용
            can_fb, _ = ENG.check_usable(fb_eng)
            if not can_fb:
                continue
            log("  ⚡ Claude 한도 소진 감지 (%s) -> %s로 자동 대체 실행" % (str(fail_why)[:100], fb_eng))
            GATE.journal(st.cfg, {"event": "failover", "from": "claude", "to": fb_eng,
                                  "tag": tag, "reason": str(fail_why)[:200]})
            if fb_eng == "agy":
                chain = ENG.select_agy_models(effort=st.cfg.get("agy_effort", "medium"))
                first = (st.cfg.get("agy_model") or "").strip()
            else:
                # 모델 사슬 — 한도 소진이면 다음 모델, 그 밖의 실패는 거기서 멈춘다(무한 재시도 금지)
                # config.json gemini_model 은 **맨 앞** 모델을 정할 뿐, 예비 사슬을 없애지 않는다(2026-09-22)
                chain = list(ENG.ENGINES["gemini"].get("models") or [])
                first = st.cfg.get("gemini_model")
            models = ([first] + [m for m in chain if m != first]) if first else (chain or [None])
            for mi, mdl in enumerate(models):
                tag_fb = "%s-%s%s" % (tag, fb_eng, "" if mi == 0 else "-%d" % mi)
                d_fb, err_fb = _invoke_engine(fb_eng, st, prompt, tag_fb, extra_dirs, model=mdl)
                if err_fb == _SUPERSEDED:
                    return None, _SUPERSEDED
                if d_fb and not err_fb and not d_fb.get("is_error") and not ENG.is_exhausted(fb_eng, d=d_fb, err=err_fb):
                    log("  ✨ %s(%s) 대체 호출 성공" % (fb_eng, mdl or "기본"))
                    return d_fb, None
                if ENG.is_exhausted(fb_eng, d=d_fb, err=err_fb) and mi + 1 < len(models):
                    log("  ⚠ %s(%s) 한도 소진 → %s" % (fb_eng, mdl, models[mi + 1]))
                    continue
                log("  ⚠ %s 대체 실패(%s): %s" % (fb_eng, mdl, str(err_fb or (d_fb or {}).get("result"))[:200]))
                break
    return d, err
def stream_result(raw):
    """stream-json 출력에서 마지막 `type:"result"` 줄을 돌려준다(없으면 None — 그땐 first_json 으로)."""
    found = None
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or '"result"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "result":
            found = e
    return found


def _short(p):
    """경로를 읽기 좋게 — 아카이브 폴더는 상대경로, 첨부 사진 폴더는 「사진/」."""
    p = str(p or "").replace("/", os.sep)
    for base, label in ((os.path.join(ROOT, ""), ""), ):
        if p.lower().startswith(base.lower()):
            p = label + p[len(base):]
    i = p.find("_intake-attachments" + os.sep)
    if i >= 0:
        p = "사진/" + p[i + len("_intake-attachments" + os.sep):]
    return p.replace(os.sep, "/")


def _brief(inp):
    try:
        return json.dumps(inp, ensure_ascii=False)[:140]
    except Exception:                                           # noqa: BLE001
        return str(inp)[:140]


def behavior_of(lines):
    """트리니티봇이 한 일 — stream-json 의 tool_use 를 모은다(2026-09-11 사용자 요청 「행동패턴이 로그로」).

    거부된 시도(permission_denials)도 모은다 — 허용되지 않은 도구를 쓰려 한 것은 규칙 점검 대상이다.
    """
    tools, reads, greps, globs, mcp, other, denied, web = {}, [], [], [], [], [], [], []
    turns = cost = sec = None
    for line in lines:
        line = (line or "").strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t == "assistant":
            for c in ((e.get("message") or {}).get("content") or []):
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name = str(c.get("name") or "?")
                inp = c.get("input") or {}
                tools[name] = tools.get(name, 0) + 1
                if name == "Read":
                    reads.append(_short(inp.get("file_path")) + (" @%s" % inp.get("offset") if inp.get("offset") else ""))
                elif name == "Grep":
                    greps.append("%s · %s" % (str(inp.get("pattern"))[:80], _short(inp.get("path") or inp.get("glob") or "")))
                elif name == "Glob":
                    globs.append(str(inp.get("pattern"))[:80])
                elif name.startswith("mcp__"):
                    mcp.append("%s %s" % (name.split("__")[-1], _brief(inp)))
                elif name in ("WebSearch", "WebFetch"):
                    web.append("%s %s" % ("검색" if name == "WebSearch" else "읽기",
                                          str(inp.get("query") or inp.get("url") or "")[:160]))
                else:
                    other.append("%s %s" % (name, _brief(inp)))
        elif t == "result":
            turns = e.get("num_turns")
            cost = e.get("total_cost_usd")
            sec = int(round((e.get("duration_ms") or 0) / 1000.0))
            for x in e.get("permission_denials") or []:
                if isinstance(x, dict):
                    denied.append("%s %s" % (x.get("tool_name"), _brief(x.get("tool_input") or {})))
    return {"tools": tools, "reads": reads[:80], "greps": greps[:60], "globs": globs[:30], "mcp": mcp[:40],
            "other": other[:20], "denied": denied[:20],
            "web": web[:20], "web_flags": web_flags(web),
            "shots_opened": sum(1 for r in reads if r.startswith("사진/")),
            "turns": turns, "cost_usd": cost, "sec": sec}


_WEB_ID_PATTERNS = (
    (re.compile(r"(?<![A-Za-z])Z[A-Z]{1,4}[0-9]{3,5}"), "사내 Z T-Code·테이블"),
    (re.compile(r"INQ-[0-9]{6}"), "문의번호"),
    (re.compile(r"[0-9]{7,}"), "긴 번호(문서·사번·금액 의심)"),
    (re.compile(r"회사|COMPANY|DK ?UNC|company", re.I), "회사명"),
)


def web_flags(web):
    """검색어·주소에 사내 식별자가 섞였는지 — 검색은 외부로 나가는 행위다(운영규칙 §0). 걸린 것만 돌려준다."""
    out = []
    for q in web or []:
        for rx, why in _WEB_ID_PATTERNS:
            if rx.search(q):
                out.append("%s ← %s" % (q[:80], why))
                break
    return out


def behavior_line(b):
    """감시자 로그 한 줄 요약 — 예) Read 12 · Grep 5 · 개발계 조회 2 · 사진 열람 1 · 거부 1(Bash) · 14턴 · 268초."""
    t = b.get("tools") or {}
    parts = ["%s %d" % (k, t[k]) for k in ("Read", "Grep", "Glob") if t.get(k)]
    n_mcp = sum(v for k, v in t.items() if k.startswith("mcp__"))
    if n_mcp:
        parts.append("개발계 조회 %d" % n_mcp)
    n_ws, n_wf = t.get("WebSearch", 0), t.get("WebFetch", 0)
    if n_ws or n_wf:
        parts.append("웹 검색 %d · 읽기 %d" % (n_ws, n_wf))
    n_other = sum(v for k, v in t.items()
                  if k not in ("Read", "Grep", "Glob", "WebSearch", "WebFetch") and not k.startswith("mcp__"))
    if n_other:
        parts.append("그 밖 %d" % n_other)
    if b.get("shots_opened"):
        parts.append("사진 열람 %d" % b["shots_opened"])
    if b.get("denied"):
        parts.append("거부 %d(%s)" % (len(b["denied"]), ", ".join(sorted(set(x.split(" ")[0] for x in b["denied"])))))
    if b.get("web_flags"):
        parts.append("⚠ 검색어 점검 %d" % len(b["web_flags"]))
    if b.get("turns") is not None:
        parts.append("%s턴" % b["turns"])
    if b.get("sec"):
        parts.append("%s초" % b["sec"])
    return " · ".join(parts) or "(도구 사용 없음)"


def read_windows(lines, default_limit=2000):
    """이번 호출에서 트리니티봇이 Read 로 연 파일과 줄 범위 — [(경로, 시작 줄, 끝 줄 | None)] (2026-10-16 · ref 대조의 「열람」 근거).

    끝 줄 None = 파일 끝까지. Claude Read 는 limit 이 없으면 2000 줄까지 읽는다(default_limit). 오프셋을 넘기지 않는 엔진
    (Gemini — claude_shaped_lines 가 경로만 옮긴다)은 default_limit=None 으로 불러 파일 전체를 본 것으로 친다."""
    out = []
    for line in lines or []:
        line = (line or "").strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") != "assistant":
            continue
        for c in ((e.get("message") or {}).get("content") or []):
            if not isinstance(c, dict) or c.get("type") != "tool_use" or c.get("name") != "Read":
                continue
            inp = c.get("input") or {}
            p = str(inp.get("file_path") or "")
            if not p:
                continue
            try:
                start = max(1, int(inp.get("offset") or 1))
                lim = int(inp.get("limit") or default_limit or 0)
            except (TypeError, ValueError):
                start, lim = 1, 0
            out.append((p, start, (start + lim - 1) if lim > 0 else None))
    return out


_PF_HEAD = "## 자동 선행 검색 결과"
_PF_ITEM_RX = re.compile(r"^\d+\. #([A-Za-z0-9_\-]+) — ", re.M)


def prefetched_ids(prompt):
    """프롬프트의 「자동 선행 검색 결과」 절에 후보로 실린 아카이브 id — 트리니티봇이 열지 않았어도 본 것으로 친다(ref 대조)."""
    p = prompt or ""
    i = p.find(_PF_HEAD)
    if i < 0:
        return set()
    j = p.find("\n## ", i + 1)
    return set(_PF_ITEM_RX.findall(p[i:] if j < 0 else p[i:j]))


def ref_seen(ev, sp, iid):
    """ref 대조에 넘길 「이번에 본 것」 — 2차 시도를 거친 문의는 1차 · 2차 호출에서 본 것을 합친다."""
    s2 = (sp or {}).get("seen") if iid in ((sp or {}).get("ids") or ()) else None
    if not s2:
        return ev
    return {"reads": list(ev.get("reads") or []) + list(s2.get("reads") or []),
            "prefetched": set(ev.get("prefetched") or ()) | set(s2.get("prefetched") or ())}


def first_json(raw):
    """출력에서 **JSON 이 시작되는 줄부터** 읽는다.

    ⚠️ claude 가 JSON 앞에 경고 줄을 찍을 때가 있다(2026-09-11 실측: 「Ignoring 86 permissions.allow entries …
       this workspace has not been trusted」). 그 한 줄 때문에 전체를 못 읽어 **이미 받은 답을 버리고 재호출**했다.
    """
    lines = (raw or "").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    return None


def parse_answers(result_text, with_problems=False):
    """에이전트 최종 텍스트에서 답 JSON 을 꺼내 **규격대로 정규화**해 돌려준다.

    코드펜스가 있든 없든 견디고, 모델마다 다른 변형(최상위 배열 · 판정 한국어 · 본문 줄 배열 등)은
    `answer_schema.normalize` 가 흡수한다(2026-09-18 DEV-047 4단계 — 엔진이 바뀌어도 파싱이 조용히 실패하지 않게).
    규격을 못 맞춘 것은 **버리지 않고 문제 목록으로** 돌려준다 — journal 에 남아 담당자가 본다.
    """
    t = result_text or ""
    m = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", t, re.S)
    cand = m.group(1) if m else None
    if cand is None:                                   # 코드펜스가 없으면 첫 여는 괄호 ~ 마지막 닫는 괄호
        for a, b in (("{", "}"), ("[", "]")):
            i, j = t.find(a), t.rfind(b)
            if i >= 0 and j > i:
                cand = t[i:j + 1]
                break
    try:
        d = json.loads(cand or "")
    except ValueError:
        return ([], ["답변에서 JSON 을 읽지 못했습니다"]) if with_problems else []
    answers, problems = SCHEMA.normalize(d)
    problems += SCHEMA.validate(answers)
    return (answers, problems) if with_problems else answers


# ────────────────────────────────────────────── 접수 창구 생존 확인
def _alive(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def health_check(st, now):
    """두 서버(접수 5179 · 대시보드 저장 5178)가 죽어 있으면 되살린다 — 휴가모드가 두 서버를 책임진다.

    2026-09-10 실측: 휴가모드가 켜진 채 접수 서버만 조용히 내려가 있었다. 감시자는 멀쩡히 돌고
    하트비트도 찍혔지만 현업은 문의를 남길 수 없는 상태였다.
    2026-09-11 사용자 지시: 두 서버는 부팅 자동시작 없이 **휴가모드일 때만** 유지 → 저장 서버도 여기서 지킨다.
    → 1분마다 확인하고, 죽어 있으면 **창 없이 직접 되살린다**(서버마다 5분에 한 번까지 · servers.py).
    """
    if now - st.state.get("_hc_at", 0) < 60:
        return
    st.state["_hc_at"] = now
    for srv in SRV.SERVERS:
        _keep_alive(st, now, srv)


def _keep_alive(st, now, srv):
    k = srv["key"]              # 접수 서버 필드 이름(intake_down_*)은 예전 그대로 — 복귀 보고서가 읽는다
    up = SRV.alive(srv["port"], 0.6)
    if up and not SRV.http_ok(srv["port"], timeout=6.0):
        # 포트는 열렸는데 응답이 없다 — G: 가 잠깐 느린 것과 반쯤 죽은 것을 가르려 **연속 2번(약 2분)** 실패해야 죽은 것으로 본다
        st.state[k + "_nohttp"] = st.state.get(k + "_nohttp", 0) + 1
        if st.state[k + "_nohttp"] >= 2:
            up = False
    else:
        st.state[k + "_nohttp"] = 0
    down_since = st.state.get(k + "_down_since") or 0

    if up:
        if down_since:
            dur = int(now - down_since)
            st.state[k + "_down_total"] = st.state.get(k + "_down_total", 0) + dur
            st.state[k + "_down_since"] = 0
            log("%s 복구 확인 (%d초 끊김)" % (srv["name"], dur))
            GATE.journal(st.cfg, {"event": k + "_recovered", "down_sec": dur})
        return

    if not down_since:
        st.state[k + "_down_since"] = now
        log("⚠ %s(%d)가 죽었습니다%s" % (srv["name"], srv["port"],
                                        " — 현업이 문의를 남길 수 없습니다" if k == "intake" else ""))
        GATE.journal(st.cfg, {"event": k + "_down"})

    if now - st.state.get("_hc_restart_at_" + k, 0) >= 300:
        st.state["_hc_restart_at_" + k] = now
        ok, note = SRV.start(srv, GATE.server_log_dir(st.cfg, st.dir))
        log("  %s 재기동 %s — %s" % (srv["name"], "성공" if ok else "실패", note))
        GATE.journal(st.cfg, {"event": k + "_restart_try", "ok": ok, "note": note})


# ────────────────────────────────────────────── 한 사이클
def close_owner_tests(st, merged, ix):
    """담당자 본인(config `owner_test_req`)이 웹으로 넣은 **시험 문의**는, 트리니티봇이 물은 만큼 다 답했으면
    스스로 완료로 닫는다(2026-09-11 사용자 지시). LLM 을 부르지 않는다(비용 0).
    사람이 답한 건 · 아직 답이 덜 된 건(새 추가 질문 포함) · 현업 문의는 건드리지 않는다.
    """
    owner = ix._norm(st.cfg.get("owner_test_req") or "")
    # ③ 검증용 요청자(config test_reqs)의 시험 문의도 다 답했으면 닫는다
    testers = set(ix._norm(x) for x in (st.cfg.get("test_reqs") or []) if x)
    if not owner and not testers:
        return 0
    n = 0
    for iid, rec in sorted(merged.items()):
        who = ix._norm(rec.get("req", ""))
        if rec.get("by") != "웹접수" or (who != owner and who not in testers):
            continue
        if (rec.get("status") or "접수") == "완료":
            continue
        a = rec.get("a") or ""
        if not a or not a.startswith(GATE.BOT_PREFIXES):
            continue                                   # 답이 없거나 사람이 답했다
        if GATE.answer_turns(a) < GATE.q_turns(rec):
            continue                                   # 아직 다 답하지 못했다 — 먼저 답한다
        if GATE.close_test(iid, st.cfg):
            n += 1
            log("시험 문의 자동 종결  %s (담당자 본인 등록 · 트리니티봇 답변 완료)" % iid)
    return n


def classify_failure(d, answers, aprob):
    """호출은 돌아왔는데 쓸 답이 하나도 없을 때 **왜인지** — 기록만 한다(재시도 · 보류 흐름은 건드리지 않는다).

    2026-09-21 Claude 주간 한도 소진으로 호출 3회가 전부 실패했는데 「답 규격 문제」로만 남아 복귀 보고서가 「오류·실패 0건」이라고 했다.
    'limit' = 한도 소진 · 'error' = 종료코드가 0 이 아니거나 CLI 가 오류로 표시 · 'no_answer' = 정상 종료했으나 답이 없거나 규격 위반.
    답이 하나라도 있으면 None.
    """
    if answers:
        return None
    d = d or {}
    if ENG.is_exhausted(d.get("_engine") or "claude", d=d):
        return "limit"
    if d.get("is_error") or d.get("_rc") not in (None, 0):
        return "error"
    return "no_answer"


# ── 2차 시도 (2026-10-07) — 1차에서 보류·에스컬레이션한 문의에만 선행 검색 결과를 넣어 한 번 더 부른다.
#    재생 평가(26건 × 3번)에서 선행 검색을 전체에 켜면 이득이 입증되지 않았지만(+0.13, 95% −0.44~+0.67) 어려운 사례 쪽에 몰려 있었다(약한 7건 +1.07 · 강한 10건 −0.40).
#    그래서 쉬운 사례는 건드리지 않고 어려운 사례(보류·에스컬레이션)에만 쓴다. 권한·비밀번호처럼 규칙상 보류인 문의는 다시 부르지 않는다.
_CRED_RX = re.compile(r"비밀번호|패스워드|password|권한|롤 ?부여|계정|사번|로그인", re.I)


def _second_pass_on(cfg):
    env = os.environ.get("TRINITYBOT_SECOND_PASS")
    return (env == "1") if env in ("0", "1") else bool((cfg or {}).get("second_pass_enabled", False))


def second_pass_ids(answers, merged, batch):
    """2차 시도 대상 — 1차 판정이 hold/escalate 이고 문의가 권한·비밀번호·계정 얘기가 아닌 건."""
    out = []
    for a in answers or []:
        iid = a.get("id")
        if iid in batch and a.get("verdict") in ("hold", "escalate") and not _CRED_RX.search((merged.get(iid) or {}).get("q") or ""):
            out.append(iid)
    return out


def merge_second(answers, answers2, ids2):
    """1차 답 목록에서 2차 답이 규격대로 온 문의만 2차 답으로 바꾼다. (새 목록, [(id, 1차 판정, 2차 판정)])"""
    by2 = {a.get("id"): a for a in (answers2 or []) if a.get("id") in ids2 and a.get("verdict") in GATE.VERDICTS}
    out, changed = [], []
    for a in answers or []:
        b = by2.get(a.get("id"))
        if b is None:
            out.append(a)
            continue
        if b.get("verdict") != a.get("verdict"):
            changed.append((a.get("id"), a.get("verdict"), b.get("verdict")))
        out.append(b)
    return out, changed


def second_pass(st, ix, merged, batch, answers, tag):
    """(최종 답 목록, 정보 dict{ids, changed, cost, seen}). 어떤 실패도 1차 답을 그대로 쓰게 하고 밖으로 예외를 내지 않는다.
    seen = 2차 호출에서 트리니티봇이 본 것(ref 대조용 — 2026-10-16)."""
    info = {"ids": [], "changed": [], "cost": 0.0, "seen": None}
    try:
        ids2 = second_pass_ids(answers, merged, batch)
        if not ids2 or st.runs_today() >= st.cfg["max_runs_per_day"]:
            return answers, info
        sp_cap = (st.cfg or {}).get("max_second_pass_per_day")
        if sp_cap is not None and hasattr(st, "second_passes_today") and st.second_passes_today() >= int(sp_cap):
            log("  ⚠ 일일 2차 시도 상한(%d회) 도달 — 1차 답을 그대로 씁니다" % int(sp_cap))
            return answers, info
        first = {a["id"]: a for a in answers if a.get("id") in ids2}
        prompt2 = build_prompt(ids2, merged, ix, st.cfg, second=first)
        if "## 자동 선행 검색 결과" not in prompt2:             # 검색 결과를 못 만들면 2차 시도는 의미가 없다
            return answers, info
        st.bump_run()
        if hasattr(st, "bump_second_pass"):
            st.bump_second_pass()
        st.flush()
        log("2차 시도 — 보류·에스컬레이션 %d건에 선행 검색 결과를 넣어 다시 호출 (%s)" % (len(ids2), ", ".join(ids2)))
        # 계단식(2026-11-18 · 개선기록 5절): 2차만 다른 모델(config second_pass_claude_model, 비우면 1차와 같은 모델)
        m2 = (st.cfg.get("second_pass_claude_model") or "").strip() or None
        if m2:
            log("  2차 모델: %s" % m2)
        d2, err2 = (call_claude(st, prompt2, tag + "b", [getattr(ix, "SHOT_DIR", "")], model=m2) if m2
                     else call_claude(st, prompt2, tag + "b", [getattr(ix, "SHOT_DIR", "")]))
        if err2 == _SUPERSEDED:
            if st.state.get("runs_count", 0) > 0:
                st.state["runs_count"] = max(0, st.state.get("runs_count", 1) - 1)
                st.flush()
            info["superseded"] = True
            return answers, info
        if err2:
            log("  ⚠ 2차 호출 실패(1차 답을 그대로 씁니다): %s" % err2)
            GATE.journal(st.cfg, {"event": "second_pass_fail", "tag": tag, "ids": ids2, "err": str(err2)[:300]})
            return answers, info
        ans2, _ = parse_answers((d2 or {}).get("result", ""), with_problems=True)
        info["cost"] = float((d2 or {}).get("total_cost_usd") or 0)
        final, changed = merge_second(answers, ans2, ids2)
        info.update(ids=ids2, changed=changed,
                    seen={"reads": list((d2 or {}).get("_reads") or []), "prefetched": prefetched_ids(prompt2)})
        GATE.journal(st.cfg, {"event": "second_pass", "tag": tag, "ids": ids2, "changed": changed, "cost_usd": info["cost"]})
        log("  2차 결과: %s · $%.4f" % (", ".join("%s %s→%s" % c for c in changed) or "판정 변화 없음(1차 유지 또는 같은 판정)", info["cost"]))
        return final, info
    except Exception as e:                                       # noqa: BLE001
        log("  ⚠ 2차 시도 오류(1차 답을 그대로 씁니다): %s" % e)
        return answers, info


def cycle(st, ix):
    MAX_SUPERSEDE = 3
    for supersede_round in range(MAX_SUPERSEDE + 1):
        ids, merged = pending(st, ix)
        close_owner_tests(st, merged, ix)              # 이미 다 답한 시험 문의부터 닫는다(비용 0)

        # ① 처음 본 건은 즉시 접수 확인 (비용 0) — 어떤 장애에도 침묵하지 않게
        for iid in ids:
            s = st.seen[iid]
            if not s.get("ack_posted"):
                r = GATE.ack(iid, st.cfg)
                if r:
                    s["ack_posted"] = True
                    log(("접수 확인 기재  %s" if r is True else "접수 확인 생략(이미 답이 있음)  %s") % iid)
                st.flush()

        if not ids:
            return 0
        if st.runs_today() >= st.cfg["max_runs_per_day"]:
            log("⚠ 일일 호출 상한 도달 — 접수 확인만 남기고 대기합니다")
            return 0

        batch = ids[:st.cfg["max_batch"]]
        batch_hashes = {iid: _h(merged[iid]) for iid in batch}
        now = time.time()
        for iid in batch:
            s = st.seen[iid]
            h = batch_hashes[iid]
            # ⚠️ 시도 횟수는 **내용 한 벌당** 센다. 예전엔 성공한 호출까지 누적하고 초기화하지 않아,
            #    한 문의에 3번 쓰고 나면 그 뒤 추가 질문이 조용히 영영 건너뛰어졌다(2026-09-10 문서 검수 중 발견).
            if s.get("attempt_hash") != h:
                s["attempt_hash"] = h
                s["attempts"] = 0
            s["inflight_until"] = now + st.cfg["claude_timeout_sec"] + 60
            s["attempts"] = s.get("attempts", 0) + 1
        st.flush()

        tag = time.strftime("%Y%m%d-%H%M%S") if supersede_round == 0 else "%s-r%d" % (time.strftime("%Y%m%d-%H%M%S"), supersede_round)
        log("Claude 호출 — %d건 (%s)" % (len(batch), ", ".join(batch)))
        st.bump_run()
        st.flush()
        prompt = build_prompt(batch, merged, ix, st.cfg)
        fps = prompt_fingerprints()                    # 이 답이 어느 판의 고정 문장 · 운영규칙 · 아카이브에서 나왔나(2026-10-16)
        st._should_abort = _make_abort_checker(ix, batch, batch_hashes)
        try:
            d, err = call_claude(st, prompt, tag, [getattr(ix, "SHOT_DIR", "")])

            if err == _SUPERSEDED:
                log("🔄 처리 중 추가 메시지 반영 — 최신 내용으로 즉시 재호출합니다 (%s)" % ", ".join(batch))
                GATE.journal(st.cfg, {"event": "superseded_restart", "tag": tag, "ids": batch, "round": supersede_round + 1})
                if st.state.get("runs_count", 0) > 0:
                    st.state["runs_count"] = max(0, st.state.get("runs_count", 1) - 1)
                for iid in batch:
                    s = st.seen[iid]
                    s["inflight_until"] = 0
                    s["attempts"] = max(0, s.get("attempts", 1) - 1)
                st.flush()
                if supersede_round < MAX_SUPERSEDE:
                    continue
                return 0

            if err:
                log("⚠ 호출 실패: %s" % err)
                GATE.journal(st.cfg, {"event": "claude_fail", "ids": batch, "err": err})
                for iid in batch:
                    st.seen[iid]["inflight_until"] = 0
                st.flush()
                return 0

            # 응답이 돌아온 직후에도 추가 질문이 찰나에 들어왔는지 대조해 옛 질문 기준 답을 덮어쓰지 않는다
            try:
                latest_after = ix._read_log()
                changed_late = [iid for iid in batch if _h(latest_after.get(iid) or {}) != batch_hashes[iid]]
            except Exception:                           # noqa: BLE001
                latest_after, changed_late = merged, []
            if changed_late and len(changed_late) == len(batch) and supersede_round < MAX_SUPERSEDE:
                log("🔄 응답 직전 추가 메시지 감지 — 예전 답변 대신 최신 내용으로 즉시 재호출합니다 (%s)" % ", ".join(changed_late))
                GATE.journal(st.cfg, {"event": "superseded_late", "tag": tag, "ids": changed_late, "round": supersede_round + 1})
                for iid in batch:
                    s = st.seen[iid]
                    s["inflight_until"] = 0
                    s["attempts"] = max(0, s.get("attempts", 1) - 1)
                st.flush()
                continue

            cost = (d or {}).get("total_cost_usd", 0)
            # 규격을 못 맞춘 답은 조용히 사라지지 않는다 — 문제 목록을 journal 에 남겨 담당자가 본다(복귀 보고서).
            answers, aprob = parse_answers((d or {}).get("result", ""), with_problems=True)
            kind = classify_failure(d, answers, aprob)
            if kind in ("limit", "error"):
                # 답이 없는 이유가 규격이 아니라 **호출 자체의 실패**다 — 복귀 보고서가 「오류·실패」로 세도록 실패로 남긴다(2026-09-30).
                why = str((d or {}).get("result") or "")[:200]
                log("  ⚠ 호출은 돌아왔으나 실패(%s): %s" % (kind, why))
                GATE.journal(st.cfg, {"event": "claude_fail", "kind": kind, "tag": tag, "ids": batch,
                                      "engine": (d or {}).get("_engine"), "model": (d or {}).get("_model"), "err": why})
            elif aprob:
                log("  ⚠ 답 규격 문제 %d건: %s" % (len(aprob), " / ".join(aprob[:3])))
                GATE.journal(st.cfg, {"event": "schema_problem", "tag": tag, "ids": batch, "problems": aprob[:20]})
            ref_ev = {"reads": list((d or {}).get("_reads") or []), "prefetched": prefetched_ids(prompt)}   # ref 대조용 — 이번 호출에서 본 것
            sp = {}
            if kind not in ("limit", "error") and _second_pass_on(st.cfg):
                answers, sp = second_pass(st, ix, merged, batch, answers, tag)
                cost = float(cost or 0) + sp["cost"]
                if sp.get("superseded"):
                    log("🔄 2차 시도 중 추가 메시지 반영 — 최신 내용으로 즉시 재호출합니다 (%s)" % ", ".join(batch))
                    GATE.journal(st.cfg, {"event": "superseded_restart", "tag": tag + "b", "ids": batch, "round": supersede_round + 1})
                    for iid in batch:
                        s = st.seen[iid]
                        s["inflight_until"] = 0
                        s["attempts"] = max(0, s.get("attempts", 1) - 1)
                    st.flush()
                    if supersede_round < MAX_SUPERSEDE:
                        continue
                    return 0
        finally:
            st._should_abort = None

        shots_given = 0
        for iid in batch:
            try:
                shots_given += len(ix._shot_list(iid))
            except Exception:                                   # noqa: BLE001
                pass
        GATE.journal(st.cfg, {"event": "run", "tag": tag, "ids": batch,
                              "cost_usd": cost, "answers": len(answers),
                              "denials": len((d or {}).get("permission_denials") or []),
                              "shots_given": shots_given,
                              "engine": (d or {}).get("_engine"), "model": (d or {}).get("_model"),
                              "sec": (d or {}).get("_sec"), "turns": (d or {}).get("num_turns"),
                              "fail_kind": kind})
        # 이 호출에서 나온 답마다 붙는 출처 — 어느 모델이 · 어느 호출에서 · 얼마 걸려 나왔나(비용은 묶음을 건수로 나눈 근사치)
        meta = {"source": "watcher", "tag": tag, "engine": (d or {}).get("_engine"), "model": (d or {}).get("_model"),
                "sec": (d or {}).get("_sec"), "turns": (d or {}).get("num_turns"),
                "cost_share_usd": round(float(cost or 0) / max(1, len(batch)), 4), "batch_n": len(batch)}
        meta.update(fps)                               # prompt_fp · rules_fp · archive_fp — 판이 바뀐 앞뒤로 정확도를 갈라 본다(장부 「판 지문」)
        log("응답 %d건 · $%.4f" % (len(answers), cost))

        done = 0
        got = set()
        try:
            latest_before_write = ix._read_log()
        except Exception:                               # noqa: BLE001
            latest_before_write = merged
        for a in answers:
            iid = a.get("id")
            if iid not in batch:
                continue
            rec_now = latest_before_write.get(iid) or {}
            if _h(rec_now) != batch_hashes[iid]:
                log("  🔄 기록 직전 추가 메시지 감지 (%s) — 예전 답변 저장을 건너뛰고 다음 처리로 넘깁니다" % iid)
                s = st.seen[iid]
                s["inflight_until"] = 0
                s["attempts"] = max(0, s.get("attempts", 1) - 1)
                got.add(iid)
                continue
            got.add(iid)
            ok, status, shown = GATE.apply_one(
                iid, a.get("verdict", "hold"), a.get("public", ""),
                a.get("note", ""), a.get("ref", ""), cfg=st.cfg, meta=meta, seen=ref_seen(ref_ev, sp, iid))
            s = st.seen[iid]
            s["inflight_until"] = 0
            if ok:
                s["answered_hash"] = _h(merged[iid])
                s["attempts"] = 0                      # 답했다 — 다음 추가 질문은 처음부터 센다
                s["last_status"] = status
                s["verdict"] = a.get("verdict")
                done += 1
                log("  기록 완료  %s → %s (%s)" % (iid, status, a.get("verdict")))
            else:
                log("  ⚠ 기록 실패  %s — %s" % (iid, shown))
        close_owner_tests(st, ix._read_log(), ix)      # 방금 답한 시험 문의도 닫는다
        for iid in batch:
            st.seen[iid]["inflight_until"] = 0
            if iid not in got:
                log("  ⚠ 응답 누락  %s (attempts=%d)" % (iid, st.seen[iid]["attempts"]))
                if st.seen[iid]["attempts"] >= st.cfg["max_attempts"]:
                    GATE.apply_one(iid, "hold", "", "자동 처리 %d회 실패 — 담당자 확인 필요"
                                   % st.seen[iid]["attempts"], cfg=st.cfg, meta=dict(meta, fallback=True, model="", engine=""))   # 시스템이 낸 보류 — 어느 모델의 답도 아니다
        st.flush()
        return done
    return 0


# ────────────────────────────────────────────── 메인 루프
# 휴가모드가 바꿔 두고 감시자가 지키는 레지스트리 값 — sysmode.ps1 On 이 쓰는 값과 같아야 한다
_HOLD = (
    (r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU",
     (("AUOptions", 2), ("NoAutoRebootWithLoggedOnUsers", 1))),
    (r"SOFTWARE\Policies\Microsoft\WindowsStore",
     (("AutoDownload", 2),)),                       # MS Store 앱 자동 업데이트 끔 (2026-09-11 추가)
)
_au_next = [0.0]


def hold_settings(now):
    """⚠️ 휴가모드가 바꿔 둔 업데이트 설정(Windows Update · MS Store)이 그대로인지 10분마다 본다.

    2026-09-11 실측: 켤 때 「알림만(2)」으로 바꿔 둔 AUOptions 가 다음 날 아침 「자동 설치(4)」로 돌아가 있었다
    (이 PC 는 도메인 PC 가 아니라 그룹 정책은 아니다 — 무엇이 되돌렸는지는 모른다). 되돌려져 있으면 다시 맞추고
    기록한다(복귀 보고서에 나온다). 끄는 중(mode off → 원복)과 겹치지 않게 쓰기 직전에 모드를 다시 확인한다.
    """
    if now < _au_next[0]:
        return
    _au_next[0] = now + 600
    try:
        import winreg
    except Exception:
        return
    fixed = []
    for path, pairs in _HOLD:
        try:
            k = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                   winreg.KEY_READ | winreg.KEY_WRITE)
        except Exception as e:
            log("⚠ 휴가모드 설정을 확인하지 못했습니다(%s): %s" % (path.rsplit("\\", 1)[-1], e))
            continue
        try:
            for name, want in pairs:
                try:
                    cur = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    cur = None
                if cur == want:
                    continue
                if not State(GATE._cfg()).mode_on():
                    return                             # 방금 꺼졌다 — 원복을 방해하지 않는다
                try:
                    winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, want)
                    fixed.append("%s %s→%s" % (name, cur, want))
                except OSError as e:
                    fixed.append("%s 되돌리기 실패(%s)" % (name, e))
        finally:
            winreg.CloseKey(k)
    if fixed:
        log("⚠ 휴가모드 설정이 바뀌어 있어 되돌렸습니다: " + ", ".join(fixed))
        GATE.journal(GATE._cfg(), {"event": "settings_drift", "fixed": fixed})


_LOCK_FH = None


def single_instance(rt):
    """감시자는 한 번에 하나만. 잠금 파일을 배타적으로 잡고 PID 를 적는다. 이미 잡혀 있으면 None.

    ⚠️ 이전엔 「이미 떠 있나」를 WMI 로 물었는데, 이 PC 에서 WMI 가 자주 시간초과돼 「없음」으로 보고
       감시자를 하나 더 띄울 수 있었다(2026-09-11). 잠금은 프로세스가 죽으면 OS 가 자동으로 푼다.
    """
    global _LOCK_FH
    import msvcrt
    os.makedirs(rt, exist_ok=True)
    # 운영은 잠금·PID 를 로컬(C:)에 둔다 — G: 가 끊겼다 돌아오면 거기 있던 잠금이 조용히 풀렸다(2026-10-08, apply_answer.lock_path)
    lockp = GATE.lock_path(GATE._cfg(), rt)
    fh = io.open(lockp, "a+b")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    _LOCK_FH = fh
    io.open(os.path.join(os.path.dirname(lockp), "watcher.pid"), "w", encoding="utf-8").write(str(os.getpid()))
    return fh


def _beat(hb, extra):
    """하트비트 한 줄. 쓰지 못해도(G: 끊김·폴더 문제) 감시자를 죽이지 않는다. 키 순서는 ts 가 맨 앞(보고서가 앞머리로 읽는다)."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        rec.update(extra)
        with io.open(hb, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _canary_recorder(cfg, hb):
    """인증 점검 결과를 watcher.log · journal · 하트비트 옆 로컬 기록에 남기는 콜백(실패해도 조용히)."""
    def rec(msg):
        _local_note(hb, msg)
        try:
            log(msg)
            GATE.journal(cfg, {"event": "auth_canary", "msg": msg[:300]})
        except Exception:                               # noqa: BLE001
            pass
    return rec


def _local_note(hb, msg):
    """G: 가 없을 때도 남는 기록 — 하트비트 옆(운영은 C:)에 따로 적는다. 못 써도 조용히 넘어간다."""
    try:
        with io.open(os.path.join(os.path.dirname(hb), "watcher_local.log"), "a", encoding="utf-8", newline="") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def main():
    cfg = GATE._cfg()
    st = State(cfg)
    if not st.mode_on():
        log("부재중 모드가 켜져 있지 않습니다 (state.json mode != on). 종료합니다.")
        return 1
    # 엔진 관문 — 실행 파일이 있는지, 그리고 **격리가 실측으로 증명된 엔진인지**(engines.py check_usable).
    # 증명 안 된 엔진으로 무인 실행을 열면 셸·쓰기 차단이 조용히 사라진다. 부재중에는 그게 오답보다 위험하다.
    eng = ENG.resolve(cfg)
    usable, why = ENG.check_usable(eng)
    if not usable:
        log("⚠ 엔진을 쓸 수 없어 감시를 시작하지 않습니다 — %s" % why)
        return 1
    log("엔진: %s" % ENG.describe(eng))

    if single_instance(st.dir) is None:
        log("감시자가 이미 실행 중입니다 — 이 인스턴스는 바로 끝냅니다(중복 방지)")
        return 0

    ix = intake_mod()
    log("부재중 감시 시작 — 기준 id %s · 디바운스 %ds · 배치 %d · 일일상한 %d"
        % (st.state.get("baseline_id", "(없음)"), cfg["debounce_sec"],
           cfg["max_batch"], cfg["max_runs_per_day"]))

    sig = None
    last_change = 0.0
    last_reconcile = 0.0
    hb = GATE.heartbeat_path(cfg)
    p_state = os.path.join(st.dir, "state.json")
    outage_since = None
    health_p = os.path.join(os.path.dirname(hb), "bot_health.json")      # 인증 점검 결과(운영은 C: — 하트비트 옆)
    canary_at = time.time() + 120                                          # 기동 2분 뒤 첫 확인 — 주기 안이면 건너뛴다

    while True:
        try:
            # ⚠ G:(구글 드라이브)가 끊긴 동안 「상태 파일이 없다」를 「휴가모드가 꺼졌다」로 읽고 스스로 끝나거나(State 가
            #    없는 파일을 빈 상태로 읽는다), 오류 처리 구간의 journal 쓰기가 다시 G: 에서 실패해 프로세스가 통째로 죽었다
            #    (2026-10-07 07:10:52 G: 76초 끊김 → 감시자 사망 · 80분 중단). 상태 파일에 닿지 않으면 끝내지 않고 기다린다.
            if not os.path.exists(p_state):
                if outage_since is None:
                    outage_since = time.time()
                    _local_note(hb, "G: 끊김 감지 — 상태 파일에 닿지 않습니다. 끝내지 않고 기다립니다")
                _beat(hb, {"storage": "down"})
                time.sleep(10)
                continue
            if outage_since is not None:
                secs = int(time.time() - outage_since)
                outage_since = None
                sig = None                                      # 끊긴 사이 들어온 문의를 다시 훑게 한다
                _local_note(hb, "G: 복구 — %d초 끊겼다가 이어갑니다" % secs)
                try:
                    log("G: 복구 — %d초 끊겼다가 이어갑니다" % secs)
                    GATE.journal(cfg, {"event": "storage_outage", "secs": secs})
                except Exception:                               # noqa: BLE001
                    pass
            if not State(cfg).mode_on():
                log("모드가 꺼졌습니다. 감시를 종료합니다.")
                return 0
            now = time.time()
            try:
                s = os.stat(LOG_JS)
                cur = (getattr(s, "st_mtime_ns", s.st_mtime), s.st_size)
            except OSError:
                cur = None
            if cur != sig:
                sig = cur
                last_change = now
                log("변경 감지")
                try:
                    p_ids, p_merged = pending(st, ix)
                    urgent_now = False
                    wrote_ack = False
                    for iid in p_ids:
                        s_i = st.seen.setdefault(iid, {})
                        was_acked = bool(s_i.get("ack_posted"))
                        if not was_acked:
                            r = GATE.ack(iid, st.cfg)
                            if r:
                                s_i["ack_posted"] = True
                                log(("접수 확인 기재  %s" if r is True else "접수 확인 생략(이미 답이 있음)  %s") % iid)
                                if r is True:
                                    wrote_ack = True
                            st.flush()
                        else:
                            urgent_now = True
                        if GATE.q_turns(p_merged[iid]) > 1:
                            urgent_now = True
                    if wrote_ack:
                        try:
                            s2 = os.stat(LOG_JS)
                            sig = (getattr(s2, "st_mtime_ns", s2.st_mtime), s2.st_size)
                        except OSError:
                            pass
                    if urgent_now:
                        last_change = now - cfg["debounce_sec"]
                except Exception:                               # noqa: BLE001
                    pass
            due = last_change and (now - last_change >= cfg["debounce_sec"])
            recon = now - last_reconcile >= cfg["reconcile_sec"]
            if due or recon:
                last_change = 0.0
                last_reconcile = now
                st.state["last_cycle"] = time.strftime("%Y-%m-%d %H:%M:%S")
                n = cycle(st, ix)
                if n:
                    st.state["answered_total"] = st.state.get("answered_total", 0) + n
                st.flush()
                if hasattr(st, "sync_gdrive"):
                    st.sync_gdrive(full=bool(recon))
            health_check(st, now)
            hold_settings(now)
            if now >= canary_at and ENG.resolve(cfg) == "claude":
                canary_at = now + 60
                _beat(hb, {"runs_today": st.runs_today(), "canary": "checking"})      # 점검은 최대 90초 걸린다 — 그 사이 생존 표시
                try:
                    CANARY.check(health_p, CLAUDE, hours=cfg.get("auth_canary_hours", 4), record=_canary_recorder(cfg, hb))
                except Exception:                       # noqa: BLE001
                    pass                                # 점검이 봇을 멈추게 하지 않는다
            _beat(hb, {"runs_today": st.runs_today()})
            time.sleep(5)
        except KeyboardInterrupt:
            log("중단했습니다.")
            return 0
        except Exception as e:                                  # 죽지 않는다
            # 오류 처리 안의 기록도 실패할 수 있다(G: 끊김) — 거기서 다시 터지면 이 줄이 「죽지 않는다」를 깨뜨린다
            try:
                log("⚠ 루프 오류(계속 진행): %s" % e)
                GATE.journal(cfg, {"event": "loop_error", "err": str(e)[:400]})
            except Exception:                                   # noqa: BLE001
                _local_note(hb, "루프 오류(기록 실패 — 계속 진행): %s" % str(e)[:200])
            time.sleep(15)


def works_once():
    """웍스 메신저 자동응답 시간대(평일 17~08시·주말·캘린더 외근/교육/휴가)에 휴가모드가 꺼져 있으면
    **웍스메신저 건만** 한 번 처리하고 끝낸다. works-messenger/watcher.mjs 가 건이 생길 때만 부른다(2026-11-25).
    휴가모드가 켜져 있으면 상시 감시자가 같은 건을 처리하므로 아무것도 하지 않는다. 휴가모드 상태는 바꾸지 않는다."""
    global INTAKE_SOURCES
    cfg = GATE._cfg()
    st = State(cfg)
    if st.mode_on():
        log("[웍스 1회] 휴가모드 감시자가 처리합니다 — 종료")
        return 0
    eng = ENG.resolve(cfg)
    usable, why = ENG.check_usable(eng)
    if not usable:
        log("⚠ [웍스 1회] 엔진을 쓸 수 없습니다 — %s" % why)
        return 1
    if single_instance(st.dir) is None:
        log("[웍스 1회] 다른 감시자가 실행 중 — 종료")
        return 0
    INTAKE_SOURCES = ["웍스메신저"]                    # 시간대 자동응답은 메신저 건만 — 웹 문의 자동답변은 휴가모드 때만
    ix = intake_mod()
    n = cycle(st, ix)
    st.flush()
    if hasattr(st, "sync_gdrive"):
        st.sync_gdrive(full=False)
    log("[웍스 1회] 처리 %d건 · 엔진 %s" % (n, ENG.describe(eng)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(works_once() if "--works-once" in sys.argv else main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        log("⚠ 감시자 비정상 종료" + chr(10) + traceback.format_exc())
        raise
