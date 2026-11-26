# -*- coding: utf-8 -*-
"""
apply_answer.py — 부재중 에이전트의 **유일한 쓰기 통로** (쓰기 게이트)

왜 게이트가 따로 있나
  2026-09-10 검증에서 에이전트에게 `Bash(python ... log-inquiry.py:*)` 권한을 주고 돌렸더니,
  에이전트가 `cd "..." && python "C:/절대경로/log-inquiry.py"` 형태로 불러 **패턴이 안 맞아 거부**됐다.
  조사는 다 해놓고 답은 안 달린 채 조용히 끝났다 — 휴가 중이었다면 침묵이다.
  → 그래서 **에이전트에게는 쓰기 권한을 아예 주지 않는다.** 에이전트는 판단 결과를 JSON 으로
    돌려줄 뿐이고, 파일에 닿는 것은 감시자(watcher)가 부르는 이 파일 하나뿐이다.

이 게이트가 강제하는 것 (에이전트가 빠뜨릴 수 없다)
  ① 「[트리니티봇 · AI 1차 안내 · 담당자 부재중]」 표기와 정정 안내 문구를 **자동 부착**
  ② 내부 메모는 답변란에 넣지 않는다 — journal 로만 간다 (공개본 유출 경로 자체를 없앤다)
  ③ AI 는 문의를 **'완료'로 닫지 않는다** → 전부 '대기'.
     현업 화면의 '완료' 배지는 담당자 확정 답변으로 읽힌다. 사람이 보고 닫아야 한다.
     (config.json 의 ai_answer_status 로 바꿀 수 있으나 기본값은 '대기')
  ④ 기록 후 **재조회로 반영을 확인**한다 — 성공 메시지 ≠ 반영
  ⑤ **현업이 실제로 볼 문장까지 렌더해서 검증**한다. intake-server 의 _public_view/_strip_internal 을
     그대로 import 해, 내부 표시 제거 후 본문이 비거나 크게 줄면 **롤백하고 표준 문구로 대체**한다.
  ⑥ 에스컬레이션(§3-③) 문구를 유형에 맞게 붙인다.
  ⑦ **사내 식별자(DEV/OPS 카드·문의·SR/변경 번호)가 public 에 섞이면 표준 문구로 되돌린다**(2026-09-18 —
     트리니티봇이 tasks.md 카드를 참고하게 되면서 추가. `_LEAK_ID_RX`).
  ⑧ (그림자 기록 · 막지 않음) 근거로 낸 ref 앵커가 아카이브에 실재하고 그 호출에서 실제로 열람됐는지 대조해 journal 에 남긴다
     (2026-10-16 문서 그라운딩 1단계 — `ref_check`).

단독 실행도 된다 (수동 개입·복구용):
  python apply_answer.py --id INQ-... --verdict answer --public "본문" --note "내부메모"
"""

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import runtime_sync as RSYNC

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)                      # task-dashboard
ROOT = os.path.dirname(DASH)
HELPER = os.path.join(DASH, "log-inquiry.py")
INTAKE = os.path.join(DASH, "intake-server.py")

# 에이전트 이름 = **트리니티봇** (2026-09-10 사용자 결정).
#   · 이름을 붙여도 **AI 라는 사실은 반드시 남긴다** — 현업이 담당자 확정 답변으로
#     오해하면 틀렸을 때 되돌릴 수단이 없다.
#   · 정본 정의 = task-dashboard/bot-operating-rules.md §0
BOT = "트리니티봇"
MARK_AI = "[%s · AI 1차 안내 · 담당자 부재중]" % BOT
MARK_ACK = "[%s · 접수 확인 · 담당자 부재중]" % BOT
# 옛 표기도 봇이 쓴 것으로 인정한다 — 과거 기록이 「사람이 쓴 답」으로 잠기면
# 추가 질문이 와도 영영 처리되지 않는다.
BOT_PREFIXES = ("[트리니티봇", "[담당자 부재중")
# 대화 턴 구분 표시 — intake-server.py 의 Q_MARK / A_MARK 와 **같은 글자**여야 한다(시험이 대조한다).
Q_MARK = "──── 추가 질문"
A_MARK = "──── 추가 답변"
AWAY_NOTICE = "현재 부재중이라 트리니티봇이 대신 안내드립니다."
TAIL = "담당자 복귀 후 확인하여 달라지는 부분이 있으면 정정드리겠습니다."
# 확정 답변(answer)에만 붙이는 종결 질문(2026-10-06 사용자 지시). 앞부분은 stale_cases.CLOSE_ASK_KEY 와 같아야 한다(시험이 대조).
CLOSE_ASK = ("이 답변으로 해결되셨으면 아래 [케이스 닫기]를 눌러 주십시오.\n"
             "더 문의하실 내용이 있으면 추가 질문으로 남겨 주십시오.")
# 정보가 부족해 되묻는 글의 첫 줄(운영규칙 §3). intake-server.py 의 NEED_INFO_MARK 와 같다(시험이 대조).
NEED_INFO_MARK = "정확한 안내를 위해 아래 정보가 필요합니다"
# 답 안에 「더 알려 주시면 안내」 같은 정보 요구가 있으면 완결된 답이 아니다 — 종결 질문을 붙이지 않는다(2026-10-06 페르소나 시험: 확정 답변 10건 중 3건이 둘을 같이 말했다).
ASKS_MORE_RX = re.compile(r"추가 질문으로 (?:알려|보내)|알려 ?주시면|보내 ?주시면|알려 ?주십시오|회신 ?(?:부탁|주시면|해 ?주시면|바랍니다)")
ESCALATE = ("업무가 멈출 정도로 급하신 건이면 운영팀장께 문의해 주십시오.")

STD_HOLD = ("접수되었습니다. 담당자가 부재중이라 확인 후 답변드리겠습니다.\n" + ESCALATE)
STD_FALLBACK = ("접수되었습니다. 확인이 더 필요한 건이라 담당자 복귀 후 답변드리겠습니다.\n" + ESCALATE)
# 트리니티봇이 **판단해서** 보류하고 공개 답을 비웠을 때의 표준 문구(2026-10-08 담당자 지적 — 접수 확인과 문장이 같아 요청자가 처리 중인지
# 사람을 기다리는지 구분하지 못했다). 머리 표기(MARK_ACK)와 ⏱ 줄은 그대로라 화면 라벨(「담당자 확인 대기」)·닫기 규칙은 변하지 않는다.
# 접수 확인(STD_HOLD)은 건드리지 않는다 — 시험과 라벨 판정이 그 문장을 기준으로 한다.
STD_HOLD_DECIDED = ("접수되었습니다. 이 문의는 AI가 확정 답변을 드리기 어려워 담당자 확인이 필요합니다. "
                    "담당자가 부재중이라 복귀 후 답변드리겠습니다.\n" + ESCALATE)

VERDICTS = ("answer", "hold", "escalate", "dev")

# 사내 식별자가 public 에 그대로 나가면 유출이다(2026-09-18 — 트리니티봇 조사 범위에 DEV/OPS 카드(tasks.md)를
# 넣으면서 함께 도입. 사용자 지시: "그 전 케이스의 자세한 의뢰자나 번호 등은 안 알려주고, 참고만 해서 방향을
# 제시해줘야겠지"). 프롬프트로도 막지만(watcher.py head · 운영규칙 §2·§6), 여기서도 한 번 더 막는다 —
# 모델이 지시를 빠뜨릴 수 있는 **마지막 관문**이라 이중화한다. 걸리면 render_empty 와 같은 자리에서
# 표준 문구로 되돌린다. note(담당자용)는 대상이 아니다 — 카드 ID 를 근거로 남기는 것은 의도된 사용이다.
_LEAK_ID_RX = re.compile(r"\b(?:DEV|OPS)-\d{1,4}\b|INQ-\d{6}-\d{6}|\bSR\d{4,6}\b|\bCH\d{4,6}\b")


# ── 문서 그라운딩 1단계 — ref 실재 · 열람 대조 (2026-10-16 · 그림자 기록: 답을 막거나 바꾸지 않고 journal 에만 남긴다)
# 트리니티봇이 근거로 낸 ref(아카이브 앵커)가 ① 아카이브에 실제로 있고 ② 그 호출에서 실제로 열어 봤는지(Read) 또는 선행 검색으로
# 프롬프트에 실렸는지를 본다. 결과: ok | empty(근거 없음) | not_found(없는 앵커) | not_read(있지만 이번에 안 봄).
# 정본 = articles/<카테고리>/<id>.md, 열람 줄 범위는 거울 archive.html 의 <article id=…> ~ </article>.
ARCHIVE_ROOT = ROOT                                # 시험이 가짜 아카이브로 바꿔 끼운다
_REF_HASH_RX = re.compile(r"#([A-Za-z][A-Za-z0-9_\-]*)")
_REF_BARE_RX = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+$")
_ARTICLE_RX = re.compile(r'<article\b[^>]*\bid="([^"]+)"')
_SPAN_CACHE = {}


def ref_ids(ref):
    """ref 글 → 아카이브 id 목록(순서 유지 · 중복 제거). 「#a #b」 · 「#a (설명 INQ-…)」 · 「a」(# 빠짐) ·
    「articles/04_x/a.md」 · 「archive.html#a」 를 모두 읽는다. # 가 하나라도 있으면 # 붙은 것만 앵커로 본다."""
    if isinstance(ref, (list, tuple)):
        ref = " ".join(str(x) for x in ref)
    ref = str(ref or "")
    ids = _REF_HASH_RX.findall(ref)
    if not ids:
        for tok in re.split(r"[\s,;·()\[\]]+", ref):
            tok = re.split(r"[\\/]", tok)[-1]
            if tok.lower().endswith(".md"):
                tok = tok[:-3]
            if _REF_BARE_RX.match(tok):
                ids.append(tok)
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def _md_ids(root):
    """정본 아티클 id 집합 — articles/<카테고리>/<id>.md 파일 이름."""
    out = set()
    base = os.path.join(root, "articles")
    for cat in (os.listdir(base) if os.path.isdir(base) else ()):
        d = os.path.join(base, cat)
        if os.path.isdir(d):
            out.update(f[:-3] for f in os.listdir(d) if f.lower().endswith(".md"))
    return out


def _html_spans(path):
    """archive.html 의 아티클별 줄 범위 {id: (시작, 끝)} (1부터). 파일 시각이 같으면 다시 읽지 않는다."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _SPAN_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    spans, cur = {}, None
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            m = _ARTICLE_RX.search(line)
            if m:
                cur = m.group(1)
                spans[cur] = [n, n]
            if cur:
                spans[cur][1] = n
                if "</article>" in line:
                    cur = None
    spans = dict((k, tuple(v)) for k, v in spans.items())
    _SPAN_CACHE[path] = (mt, spans)
    return spans


def _was_read(aid, reads, span):
    """정본 md 를 열었거나, archive.html 의 읽은 줄 범위가 그 아티클 줄 범위와 겹치면 본 것이다.
    archive-structure.md(색인)만 읽은 것은 본 것으로 치지 않는다."""
    for r in reads or ():
        try:
            p, a, b = r[0], int(r[1] or 1), (int(r[2]) if r[2] is not None else None)
        except (TypeError, ValueError, IndexError):
            continue
        base = re.split(r"[\\/]", str(p))[-1].lower()
        if base == (aid + ".md").lower():
            return True
        if base == "archive.html" and span and a <= span[1] and (b is None or b >= span[0]):
            return True
    return False


def ref_check(ref, seen, root=None):
    """(판정, 세부) — 판정 ok | empty | not_found | not_read.

    seen = {"reads": [(경로, 시작 줄, 끝 줄|None)], "prefetched": {id, …}} — 감시자가 그 호출의 stream 과 프롬프트에서 모아 넘긴다.
    앵커가 여럿이면 하나라도 없으면 not_found, 하나라도 안 봤으면 not_read. 선행 검색에만 실렸던 것은 ok 로 치되 세부에 남긴다(prefetch_only)."""
    ids = ref_ids(ref)
    if not ids:
        return "empty", {}
    root = root or ARCHIVE_ROOT
    spans = _html_spans(os.path.join(root, "archive.html"))
    known = _md_ids(root) | set(spans)
    missing = [i for i in ids if i not in known]
    if missing:
        return "not_found", {"not_found": missing}
    seen = seen or {}
    pre = set(seen.get("prefetched") or ())
    unread, via_pf = [], []
    for i in ids:
        if _was_read(i, seen.get("reads"), spans.get(i)):
            continue
        (via_pf if i in pre else unread).append(i)
    det = {}
    if unread:
        det["not_read"] = unread
    if via_pf:
        det["prefetch_only"] = via_pf
    return ("not_read" if unread else "ok"), det


def _cfg():
    p = os.path.join(HERE, "config.json")
    base = {"ai_answer_status": "대기", "runtime_dir": ""}
    if os.path.exists(p):
        try:
            base.update(json.load(io.open(p, encoding="utf-8")))
        except ValueError:
            pass
    if not base["runtime_dir"]:
        base["runtime_dir"] = os.path.join(
            os.path.dirname(ROOT), ".runtime", "_vacation-runtime")
    return base


def heartbeat_path(cfg):
    """감시자 하트비트 파일 위치 — 감시자·mode.py·제어판·재개 런처가 모두 이 규칙으로 같은 파일을 본다.

    운영(런타임 폴더가 기본 위치일 때)은 설정 heartbeat_path(C:)를 쓴다. 이유 둘(2026-10-07):
      ① 5초마다 바뀌는 파일을 구글 드라이브가 동기화하다 충돌 사본(lost_and_found)을 2~4시간마다 남겼다(12개).
      ② G: 가 끊겨도 감시자가 살아 있는지 볼 수 있다(07:10 사고 — G: 가 76초 사라진 사이 감시자가 죽었다).
    시험처럼 런타임 폴더를 바꿔 쓰면 운영 하트비트를 건드리지 않게 그 폴더 안에 둔다. 폴더를 못 만들면 런타임 폴더로 되돌아간다."""
    rt = cfg.get("runtime_dir") or ""
    local = (cfg.get("heartbeat_path") or "").strip()
    prod = os.path.join(os.path.dirname(ROOT), ".runtime", "_vacation-runtime")
    if local and os.path.normcase(os.path.abspath(rt)) == os.path.normcase(os.path.abspath(prod)):
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            return local
        except OSError:
            pass
    return os.path.join(rt, "heartbeat.jsonl")


def _is_prod_rt(cfg, rt):
    prod = os.path.join(os.path.dirname(ROOT), ".runtime", "_vacation-runtime")
    cands = {os.path.normcase(os.path.abspath(prod))}
    lsd = (cfg.get("local_state_dir") or "").strip()
    if lsd:
        cands.add(os.path.normcase(os.path.abspath(lsd)))
    return os.path.normcase(os.path.abspath(rt)) in cands


def server_log_dir(cfg, rt=None):
    """접수·저장 서버의 출력 로그 폴더 — 운영은 설정 server_log_dir(C:), 시험은 런타임 폴더(lock_path 와 같은 규칙).

    서버 로그를 G: 에 열어 두면 G: 끊김 뒤 서버가 요청마다 로그를 쓰다 실패해 빈 응답만 돌려줬다(2026-10-08)."""
    rt = rt or cfg["runtime_dir"]
    local = (cfg.get("server_log_dir") or "").strip()
    if local and _is_prod_rt(cfg, rt):
        try:
            os.makedirs(local, exist_ok=True)
            return local
        except OSError:
            pass
    return rt


def lock_path(cfg, rt=None):
    """감시자 단일 실행 잠금 파일 위치 — 감시자·mode.py 가 모두 이 규칙으로 같은 파일을 본다.

    운영(런타임 폴더가 기본 위치일 때)은 설정 lock_path(C:)를 쓴다(2026-10-08 예행연습에서 발견):
    잠금 파일이 G: 에 있으면 G: 가 잠깐 끊겼다 돌아올 때 감시자가 쥔 잠금이 조용히 풀려(12:24~12:27 끊김 뒤 감시자는 살아 있는데
    잠금은 비어 있었다) mode.py 가 「감시자 없음」으로 보고, 재개 장치가 둘째 감시자를 띄울 길이 열린다. 하트비트(C:)와 같은 이유다.
    시험처럼 런타임 폴더를 바꿔 쓰면 운영 잠금을 건드리지 않게 그 폴더 안에 둔다. 폴더를 못 만들면 런타임 폴더로 되돌린다."""
    rt = rt or cfg["runtime_dir"]
    local = (cfg.get("lock_path") or "").strip()
    if local and _is_prod_rt(cfg, rt):
        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)
            return local
        except OSError:
            pass
    return os.path.join(rt, "watcher.lock")


def state_dir(cfg, rt=None):
    r"""고빈도 상태 파일(state.json · seen.json · runs/)의 작업 폴더 — 운영은 로컬 C:, 시험은 런타임 폴더.

    2026-11-26 도입: state.json · seen.json 을 G: 에서 os.replace 로 쓰면 구글 드라이브가 (1..210) 충돌 파일을 양산했다.
    운영 런타임일 때는 C:\trinitybot-resume\runtime 에서만 원자적 교체를 수행하고, G: 정본(_vacation-runtime)에는
    runtime_sync.py 가 제자리 쓰기 + 주기적 대조(reconcile)로 무손실 동기화한다."""
    rt = rt or cfg["runtime_dir"]
    local = (cfg.get("local_state_dir") or "").strip()
    prod = os.path.join(os.path.dirname(ROOT), ".runtime", "_vacation-runtime")
    if local and os.path.normcase(os.path.abspath(rt)) == os.path.normcase(os.path.abspath(prod)):
        try:
            os.makedirs(local, exist_ok=True)
            RSYNC.seed_local_from_gdrive(local, rt)
            return local
        except OSError:
            pass
    return rt


def _intake():
    """intake-server 의 공개 렌더 함수를 그대로 빌려 쓴다 — 검증 로직 이중화를 막는다."""
    spec = importlib.util.spec_from_file_location("intake_mod", INTAKE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _helper(args):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, HELPER] + args, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=env, cwd=DASH, timeout=60)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def journal(cfg, rec):
    rt = cfg["runtime_dir"]
    sdir = state_dir(cfg, rt)
    if os.path.normcase(os.path.abspath(sdir)) != os.path.normcase(os.path.abspath(rt)):
        RSYNC.append_journal_dual(sdir, rt, rec)
        return
    d = os.path.join(rt, "journal")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, time.strftime("%Y-%m-%d") + ".jsonl")
    rec = dict(rec)
    rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    with io.open(f, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def q_hash(rec):
    """문의 본문의 지문. **추가 질문이 붙으면 반드시 달라져야 한다** — 이게 재처리의 방아쇠다.
    감시자와 mode.py 가 같은 함수를 써야 판정이 어긋나지 않는다."""
    q = rec.get("q") or ""
    return "%d:%s" % (len(q), q[-160:])


def q_turns(rec):
    """현업이 물은 횟수 = 최초 문의 1 + 추가 질문 수."""
    return 1 + (rec.get("q") or "").count(Q_MARK)


def answer_turns(a):
    """봇이 **실제로 답한** 횟수. 접수 확인만 붙어 있으면 0 이다.

    답 블록(compose)은 판정과 상관없이 끝에 ⏱ 시각 줄이 붙고, 접수 확인(ack)에는 없다.
    hold 답은 머리 표기가 「접수 확인」이라 표기로는 못 가린다 — 그래서 ⏱ 로 가린다(리뷰 #7).
    ⏱ 도입 전의 옛 답은 「AI 1차 안내」 표기로 알아본다. 이어 붙인 답은 전부 실제 답이다
    (이어 붙이기는 실제 답 위에서만 일어난다).
    """
    a = a or ""
    first = a.split(A_MARK)[0]
    if "⏱" in first or "AI 1차 안내" in first:
        return 1 + a.count(A_MARK)
    return 0


_CLOSE_LINE_RX = re.compile(r"케이스 닫기|더 문의하실 내용이 있으면 추가 질문으로 남겨")


def _drop_close_ask(body):
    """본문에서 종결 질문 변형(모델이 직접 쓴 줄)을 줄 단위로 지운다."""
    return "\n".join(ln for ln in body.split("\n") if not _CLOSE_LINE_RX.search(ln)).rstrip()


def compose(verdict, public):
    """표기·꼬리말·에스컬레이션을 강제로 붙인다. 에이전트가 빠뜨릴 수 없다."""
    body = (public or "").strip()
    # 에이전트가 표기를 이미 붙였으면 중복시키지 않는다
    for m in (MARK_AI, MARK_ACK):
        if body.startswith(m):
            body = body[len(m):].strip()
    # 옛 표기(「[담당자 부재중 · …]」 등)를 붙여 와도 떼어 낸다 — 첫 줄이 봇 표기뿐이면 버린다
    first, _, rest = body.partition("\n")
    if first.strip().startswith(BOT_PREFIXES) and first.strip().endswith("]"):
        body = rest.strip()
    # 동일 문장을 모델이 넣어도 게이트에서 답변당 한 번만 붙인다.
    body = body.replace(AWAY_NOTICE, "").strip()
    if verdict == "hold":
        body = body or STD_HOLD_DECIDED
        head = MARK_ACK
    else:
        body = body or STD_FALLBACK
        head = MARK_AI
    if verdict in ("escalate", "hold") and "운영팀장" not in body:
        body += "\n" + ESCALATE
    if verdict == "answer":
        # 모델이 종결 질문을 스스로 써 오는 일이 있다(2026-10-07 베타 첫 답 — 첫 줄이 두 번 찍혔다: 두 줄 전체가 있어야만 중복으로 봤다).
        # 모델이 쓴 변형은 줄째 지우고 게이트의 정식 문구를 한 번만 붙인다.
        body = _drop_close_ask(body) or STD_FALLBACK
        if not ASKS_MORE_RX.search(body):
            body += "\n" + CLOSE_ASK
    body += "\n" + AWAY_NOTICE
    if verdict != "hold" and TAIL not in body:
        body += "\n" + TAIL
    # 답변마다 기재 시각을 남긴다. 현업 화면이 이 줄을 뽑아 헤더에 보여준다.
    # (이 PC 시계가 서울 기준이다. 별도 변환을 하지 않는 이유 = 변환하면 오히려 어긋난다)
    body += "\n⏱" + time.strftime("%Y-%m-%d %H:%M")
    return head + "\n" + body


def apply_one(iid, verdict, public, note="", ref="", cfg=None, dry=False, meta=None, seen=None):
    """한 건을 기록하고 검증한다. (ok, 상태, 메시지) 를 돌려준다.

    meta = 이 답이 **어디서 나왔는지**(감시자가 넘긴다): source · tag · engine · model · sec · turns · cost_share_usd · batch_n.
    안 주면 사람이 세션에서 직접 부른 것으로 보고 source="manual" 로 남긴다 — 검증 장부가 트리니티봇의 답으로 세지 않도록(2026-09-30).
    seen = 그 호출에서 트리니티봇이 본 것(감시자가 넘긴다 — watcher.read_windows · prefetched_ids). 주면 ref 대조 결과(ref_check)를
    answered 에 함께 남긴다 — 그림자 기록이라 판정 · 기록 · 공개 답은 바뀌지 않는다(2026-10-16). 안 주면(수동 · 시스템 보류) 남기지 않는다.
    """
    cfg = cfg or _cfg()
    if verdict not in VERDICTS:
        return False, "", "알 수 없는 판정: %s" % verdict

    ix = _intake()
    rec0 = ix._read_log().get(iid) or {}
    prior = rec0.get("a") or ""
    # ⚠️ 「접수 확인 안내」도 봇 표기로 시작한다. 그것까지 이어붙이기로 보면
    #    진짜 답변이 「추가 답변」으로 취급돼 **필수 표기가 떨어져 나간다**(2026-09-10 실측).
    #    → 앞에 **실제 답(판정 무관 — hold 답 포함)이 있을 때만** 이어붙인다. 접수 확인뿐이면 그 자리를 채운다.
    #    (예전엔 「AI 1차 안내」 글자로만 가려, hold 답 위의 추가 답변이 hold 답을 덮어썼다 — 리뷰 #7)
    appending = answer_turns(prior) > 0

    # ⑤-a **이번 턴의 글만** 검증한다.
    #    ⚠️ 누적된 전체를 검증하면 안 된다 — 앞선 답변에 내부 메모가 길게 붙어 있으면
    #       「많이 줄었다」로 오판해 **대화 전체를 표준문구로 갈아치운다**(2026-09-10 실측 사고).
    #       앞 대화는 이미 검증을 통과해 저장된 것이다. 다시 판단하지 않는다.
    block = compose(verdict, public)
    shown_new = ix._strip_internal(block)
    leaked = _LEAK_ID_RX.findall(shown_new)
    if leaked:
        journal(cfg, {"event": "card_leak_blocked", "id": iid, "matched": leaked})
        block = compose(verdict, "")
        shown_new = ix._strip_internal(block)
    if not shown_new.strip():
        journal(cfg, {"event": "render_empty", "id": iid})
        block = compose(verdict, STD_FALLBACK)
        shown_new = ix._strip_internal(block)
    if not shown_new.strip():
        return False, "", "공개 렌더가 비어 사전 차단했습니다"

    if appending:
        # 추가 질문에 대한 답 — **덮어쓰지 않고 이어붙인다**. 대화가 끊기면 안 된다.
        # 표기를 떼지 않는다 — 어떤 답변에도 「AI 1차 안내」가 붙어 있어야 한다(운영규칙 §5)
        text = "%s\n\n──── 추가 답변 (%s) ────\n%s" % (
            prior, time.strftime("%Y-%m-%d %H:%M"), block)
    else:
        text = block
    status = cfg["ai_answer_status"] if verdict in ("answer", "dev") else "대기"

    shown = ix._strip_internal(text)

    if dry:
        return True, status, shown

    rc, out = _helper(["--id", iid, "--status", status, "--a", text] +
                      (["--ref", ref] if ref else []))
    if rc != 0:
        journal(cfg, {"event": "write_fail", "id": iid, "err": out[:400]})
        return False, "", "기록 실패: %s" % out[:200]

    # ④ 재조회 검증 — 성공 메시지를 믿지 않는다
    rec = ix._read_log().get(iid) or {}
    if (rec.get("status") or "") != status or (rec.get("a") or "") != text:
        journal(cfg, {"event": "verify_fail", "id": iid,
                      "want": status, "got": rec.get("status")})
        return False, "", "기록은 됐다는데 재조회에서 확인되지 않았습니다"

    # ⑤-b 실제 공개본으로 한 번 더
    final_shown = ix._public_view(rec)["a"]
    ok_render = bool(final_shown.strip()) and MARK_AI.split("]")[0] in final_shown

    src = dict(meta or {})
    src.setdefault("source", "manual")
    if seen is not None:
        try:
            src["ref_check"], det = ref_check(ref, seen)
        except Exception as e:                                  # noqa: BLE001  대조가 실패해도 기록은 막지 않는다
            src["ref_check"], det = "error", {"err": str(e)[:200]}
        if det:
            src["ref_check_detail"] = det
    journal(cfg, dict({"event": "answered", "id": iid, "verdict": verdict,
                       "status": status, "ref": ref, "note": note,
                       "public_len": len(final_shown), "render_ok": ok_render,
                       "public": final_shown,
                       "type": rec.get("type") or "", "role": rec.get("role") or ""}, **src))
    return True, status, final_shown


def close_test(iid, cfg=None):
    """담당자 **본인이 넣은 시험 문의**만 트리니티봇이 닫는다(2026-09-11 사용자 지시 —
    「시험 문의는 네가 하지 말고 트리니티봇이 알아서 완료 처리하게」).

    현업 문의는 여전히 닫지 않는다 — 「대기」(현업 화면 「1차 답변 완료」)로 두고 담당자가 확인 후 닫는다.
    LLM 을 부르지 않는다(비용 0). 닫은 뒤 재조회로 확인한다.
    """
    cfg = cfg or _cfg()
    rc, out = _helper(["--id", iid, "--status", "완료"])
    ok = rc == 0 and ((_intake()._read_log().get(iid) or {}).get("status") == "완료")
    journal(cfg, {"event": "auto_close_test" if ok else "auto_close_fail", "id": iid,
                  "err": None if ok else out[:200]})
    return ok


def ack(iid, cfg=None):
    """접수 즉시 붙이는 안내 — LLM 을 부르지 않는다(비용 0). 어떤 장애에도 침묵하지 않게."""
    cfg = cfg or _cfg()
    # ⚠️ **답변란이 비어 있을 때만 쓴다.** 헬퍼 --a 는 답변란을 통째로 바꾸므로, 앞선 AI 답·hold 답
    #    위에 쓰면 그 답이 화면에서 사라지고 다음 답이 엉뚱한 질문에 붙는다(리뷰 #2 — 추가 질문이
    #    붙은 옛 카드에서 실제로 재현됨). 이미 답이 있으면 쓰지 않고 「생략」을 돌려준다.
    rec = _intake()._read_log().get(iid) or {}
    if rec.get("by") == "웍스메신저":
        # 웍스 메신저 건은 웹 화면 안내([알림 켜기])가 맞지 않고, 상대에게는 최종 답만 보낸다(2026-11-25).
        journal(cfg, {"event": "ack_skip_messenger", "id": iid})
        return "skip"
    cur = rec.get("a") or ""
    if cur.strip():
        journal(cfg, {"event": "ack_skip", "id": iid})
        return "skip"
    text = MARK_ACK + "\n" + STD_HOLD + "\n" + AWAY_NOTICE + \
        "\n※ 대화 목록 위쪽 [알림 켜기]를 눌러 두시면, 이 화면이 열려 있는 동안 답변이 등록될 때 알려 드립니다." + \
        "\n   (화면을 닫으면 알림이 가지 않으니 이 주소를 즐겨찾기 해두시고, 닫아 두셨다면 다시 열어 확인해 주세요.)"
    rc, out = _helper(["--id", iid, "--status", "대기", "--a", text])
    ok = rc == 0
    journal(cfg, {"event": "ack" if ok else "ack_fail", "id": iid,
                  "err": None if ok else out[:300]})
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--verdict", default="answer", choices=VERDICTS)
    ap.add_argument("--public", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--ref", default="")
    ap.add_argument("--ack", action="store_true", help="접수 확인 안내만 기재")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.ack:
        print("ACK ok" if ack(a.id) else "ACK 실패")
        return 0
    ok, status, msg = apply_one(a.id, a.verdict, a.public, a.note, a.ref, dry=a.dry)
    print(("OK  %s → %s\n---- 현업이 보는 문장 ----\n%s" % (a.id, status, msg))
          if ok else ("실패: %s" % msg))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
