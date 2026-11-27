# -*- coding: utf-8 -*-
"""
desk-server.py — 담당자 통합 화면 (복귀 보고 · 검토 · 현황) — **이 PC 전용 127.0.0.1:5177**

    python task-dashboard/desk/desk-server.py              → http://127.0.0.1:5177/  (운영 기록)
    python task-dashboard/desk/desk-server.py --dash DIR   → DIR 의 시험 로그로(예: chat-ui/_test) · 포트 5187

한 화면에 모으는 것 — 전부 **기존 기록을 읽기만** 해서 만든다(새 저장소를 두지 않는다).
  · 문의 로그 `inquiry-log.js`                      → 받은 문의 · 검토할 트리니티봇 답 · 답을 기다리는 건 · 문의자 평가
  · 검증 장부 `vacation/ledger.py`(journal 을 읽는다) → 판정 종류 · 모델 · 재판정 필요 · 권한 단계 · 95% 하한 · 처리 시간
  · 감시 하트비트                                    → 창구가 끊긴 시간(「문의가 없었다」와 「멈춰 있었다」의 구분)
  · 복귀 보고서 `vacation/mode.py report`            → 단추로 새로 만들어 이 화면에서 연다(비용 0)

⛔ 지키는 것
  1. **쓰기는 둘뿐이고, 둘 다 `log-inquiry.py` 로만 한다** — ① 트리니티봇 답 판정(맞음 · 보완 · 틀림 + 이유 코드 · 메모)
     ② 담당자 답변 추가. `inquiry-log.js` 를 직접 쓰지 않는다. 쓴 뒤에는 **다시 읽어 반영을 확인**한다.
  2. **127.0.0.1 에만 묶는다.** 쓰기 API 이고 내부 메모 · 전원 문의가 다 보인다 — 0.0.0.0 으로 열지 말 것
     (board-saver.py 와 같은 규칙). 접수 서버 · 저장 서버와 합치지 않는다.
  3. 휴가모드를 켜고 끄지 않는다. 감시자 · 서버를 띄우거나 죽이지 않는다. 트리니티봇을 부르지 않는다.
  4. 답 판정 기준 · 이유 코드 · 권한 단계 계산은 **장부의 것을 그대로 쓴다** — 여기서 다시 계산하지 않는다.
"""
import datetime
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PROD = os.path.dirname(HERE)                      # task-dashboard
VAC = os.path.join(PROD, "vacation")
ARCH = os.path.dirname(PROD)                      # 아카이브 루트
ERD_DIR = os.path.join(ARCH, "erd-graph")
HOST = "127.0.0.1"                                # ⛔ 바꾸지 말 것 — 설계 원칙 2

# Ops Console 허브 — 다른 담당자 화면을 **읽기 전용**으로 내준다(고정 경로만 · 요청 경로는 파일 시스템에 닿지 않는다)
DIAG_DIR = os.path.join(PROD, "diagnostics")
HUB_PAGES = {
    "/archive": os.path.join(ARCH, "archive.html"),
    "/board": os.path.join(PROD, "task-board.html"),
    "/metrics": os.path.join(PROD, "metrics.html"),
    "/bot-metrics": os.path.join(PROD, "bot-metrics.html"),
    "/kpi": os.path.join(DIAG_DIR, "latest.html"),
    "/overview": os.path.join(ARCH, "trinity_overview.html"),
    "/protocol": os.path.join(PROD, "protocol.html"),
}
# 대시보드는 저장 서버(5178)로 원본을 덮어쓴다 — 허브에서 연 사본은 저장하지 않게 막는다(아래 _board_readonly)
BOARD_BANNER = (
    '<div data-noexport role="note" style="position:sticky;top:0;z-index:9999;background:#fff4dc;color:#7a5200;'
    'border-bottom:1px solid #e6c97a;padding:8px 14px;font:13px/1.5 \'Malgun Gothic\',system-ui,sans-serif">'
    '보기 전용 — 수정·저장은 제어판에서 연 대시보드에서 하십시오. 여기서 💾저장을 눌러도 원본에 쓰지 않습니다.</div>'
    '<script data-noexport>try{window.showSaveFilePicker=undefined;}catch(e){}</script>'
)

try:
    if ERD_DIR not in sys.path:
        sys.path.insert(0, ERD_DIR)
    import ontology_engine as _OE
except Exception:
    _OE = None

try:
    if VAC not in sys.path:
        sys.path.insert(0, VAC)
    import prefetch as _PF
except Exception:
    _PF = None


def ontology_api(qs=""):
    """ERD/온톨로지 요약 또는 질의 결과를 반환한다."""
    if _OE is None:
        return {"ok": False, "error": "온톨로지 엔진을 불러오지 못했습니다."}
    ont = _OE.load_ontology()
    if not ont:
        return {"ok": False, "error": "schema_ontology.json 이 없습니다."}
    q = (parse_qs(qs).get("q") or [""])[0].strip()
    if q:
        res = _OE.query_entity(ont, q, hops=2)
        return {"ok": True, "query": q, "result": res}
    return {"ok": True, "meta": ont.get("meta", {}), "domains": ont.get("domains", {})}

BOT_PREFIXES = ("[트리니티봇", "[담당자 부재중")      # vacation/apply_answer.py 와 같은 글자
A_MARK = "──── 추가 답변"
Q_MARK = "──── 추가 질문"
NEED_INFO_MARK = "정확한 안내를 위해 아래 정보가 필요합니다"
A_SPLIT = re.compile(r"\n\n──── 추가 답변 \(([^)]*)\) ────\n")
ID_RE = re.compile(r"^INQ-\d{6}-\d{6}$")
GRADES = ("맞음", "보완", "틀림", "제외")
VERDICT_KO = {"answer": "확정 답변", "hold": "보류", "escalate": "에스컬레이션", "dev": "개발 요청"}
DEFAULT_CODES = ["사실오류", "위험안내", "기존기능놓침", "용어노출", "과잉보류", "누락", "가독성", "기타"]
MAX_TEXT = 4000
MAX_BODY = 64 * 1024
GAP_SEC = 180                                     # 하트비트가 이보다 오래 비면 「끊김」
GAP_SIM = 0.45

_write_lock = threading.Lock()
_log_lock = threading.Lock()
_hb_lock = threading.Lock()
_log_cache = {"sig": None, "merged": {}, "size": 0, "at": 0.0}
SHRINK_IGNORE_SEC = 30   # inquiry-log.js 는 덧붙이기만 한다 — 30초 안에 작아져 보이면 G: 드라이브의 옛 모습으로 보고 무시(2026-11-18)
_PUSH_START_RE = re.compile(r"^[ \t]*window\.INQUIRY_LOG\.push\(", re.M)
_CODE_RE = re.compile(r"\b(?:[A-Z]{1,4}\d{1,3}[A-Z]?|Z[A-Z0-9_]{3,})\b")
_FILLER = ("알려주세요", "알려 주세요", "궁금합니다", "어떻게", "어디인가요", "어디서", "어디",
           "어느", "인가요", "하나요", "합니까", "됩니까", "봅니까", "있나요", "주세요",
           "싶어요", "싶습니다", "입니다", "에서", "방법", "봐요", "보나요", "되나요",
           "할까요", "안됩니다", "안 됩니다", "안돼요", "해주세요", "해 주세요")


def args():
    dash, test = PROD, False
    if "--demo" in sys.argv:            # chat-server.py --demo 와 같은 표본 사본(.runtime/demo)
        import shutil
        root = os.path.dirname(ARCH)
        dash = os.path.join(root, ".runtime", "demo")
        os.makedirs(dash, exist_ok=True)
        for f in ("intake-server.py", "log-inquiry.py"):
            shutil.copyfile(os.path.join(PROD, f), os.path.join(dash, f))
        if not os.path.exists(os.path.join(dash, "inquiry-log.js")):
            shutil.copyfile(os.path.join(root, "sample-data", "inquiry-log.js"), os.path.join(dash, "inquiry-log.js"))
        return dash, True
    if "--dash" in sys.argv:
        i = sys.argv.index("--dash")
        if i + 1 >= len(sys.argv):
            raise SystemExit("--dash 뒤에 폴더를 적어 주십시오.")
        dash, test = os.path.abspath(sys.argv[i + 1]), True
    return dash, test


DASH, TEST = args()
PORT = 5187 if TEST else 5177
if "--port" in sys.argv:                          # 시험이 빈 포트를 쓸 수 있게
    PORT = int(sys.argv[sys.argv.index("--port") + 1])
LOG_JS = os.path.join(DASH, "inquiry-log.js")
HELPER = os.path.join(DASH, "log-inquiry.py")
# 허브 화면이 상대 경로로 부르는 데이터 파일(읽기 전용). 키는 브라우저가 보내는 경로를 풀어 쓴 것
HUB_DATA = {
    "/inquiry-log.js": LOG_JS,                                       # task-board.html · metrics.html
    "/.runtime/_vacation-runtime/bot-metrics-data.js":           # bot-metrics.html 의 ../../SAP%20SD/…
        os.path.join(os.path.dirname(ARCH), ".runtime", "_vacation-runtime", "bot-metrics-data.js"),
}


def _board_readonly(html):
    """허브에서 내주는 대시보드에 「보기 전용」 띠를 넣고 브라우저 파일 저장을 끈다(원본 파일은 건드리지 않는다)."""
    m = re.search(r"<body[^>]*>", html, re.I)
    return html[:m.end()] + BOARD_BANNER + html[m.end():] if m else BOARD_BANNER + html


# ─────────────────────────────────────────────── 읽기
def _parse_log_text(raw):
    merged = {}
    dec = json.JSONDecoder()
    i, n = 0, len(raw)
    while i < n:
        m = _PUSH_START_RE.search(raw, i)
        if not m:
            break
        pos = m.end()
        while pos < n and raw[pos] in " \t\r\n":
            pos += 1
        try:
            obj, end_idx = dec.raw_decode(raw, pos)
        except ValueError:
            nl = raw.find("\n", pos)
            i = n if nl < 0 else nl + 1
            continue
        if isinstance(obj, dict):
            rid = obj.get("id")
            if rid:
                cur = merged.setdefault(rid, {})
                for k, v in obj.items():
                    if v not in ("", None):
                        cur[k] = v
        i = end_idx
    return merged


def read_log():
    """inquiry-log.js 를 id 별 최신 상태로 병합한다(한 줄 · 여러 줄 push 모두 파싱, mtime 캐시)."""
    try:
        st = os.stat(LOG_JS)
    except FileNotFoundError:
        with _log_lock:
            _log_cache["sig"] = None
            _log_cache["merged"] = {}
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    with _log_lock:
        if sig == _log_cache["sig"]:
            return {k: dict(v) for k, v in _log_cache["merged"].items()}
        # 판정 직후 같은 건이 다시 「검토 대기」로 보이던 현상(2026-11-17 운영 1회) 방어 — 방금 본 것보다 작은 파일은 옛 모습
        if _log_cache["sig"] is not None and st.st_size < _log_cache["size"] and time.time() - _log_cache["at"] < SHRINK_IGNORE_SEC:
            return {k: dict(v) for k, v in _log_cache["merged"].items()}
    # [SECURE] Context manager - guaranteed file release (Category 5)
    with io.open(LOG_JS, "r", encoding="utf-8") as f:
        raw = f.read()
    merged = _parse_log_text(raw)
    with _log_lock:
        _log_cache["sig"] = sig
        _log_cache["merged"] = merged
        _log_cache["size"], _log_cache["at"] = st.st_size, time.time()
        return {k: dict(v) for k, v in merged.items()}


_ask_lock = threading.Lock()
_ask_cache = {"fp": None, "bundle": None}
_ASK_MAX_Q = 2000
_ASK_QUICK_PREFIX = "man-quick-procedures"


def _stat_sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _ask_ensure_loaded():
    if _PF is None:
        return None
    arch_html = os.path.join(ARCH, "archive.html")
    arch_struct = os.path.join(ARCH, "archive-structure.md")
    ont_json = os.path.join(ERD_DIR, "schema_ontology.json")
    fp = (_stat_sig(arch_html), _stat_sig(arch_struct), _stat_sig(LOG_JS), _stat_sig(ont_json))
    with _ask_lock:
        if _ask_cache["fp"] == fp and _ask_cache["bundle"] is not None:
            return _ask_cache["bundle"]
    arts = _PF.load_articles(arch_html)
    if not arts:
        return None
    by_id = {a["id"]: a for a in arts}
    art_ix = _PF.build_article_index(arts)
    art_ix.tf = []
    curated = None
    try:
        with io.open(arch_struct, encoding="utf-8", errors="replace") as f:
            srows = _PF.struct_rows(f.read())
        curated = (srows, _PF.curated_vocab(srows)) if srows else None
    except OSError:
        pass
    prec = _PF.human_precedents(read_log())
    prec_ix = _PF.build_prec_index(prec)
    prec_ix.tf = []
    if _OE is not None:
        _OE.load_ontology()
    bundle = {
        "by_id": by_id,
        "art_ix": art_ix,
        "curated": curated,
        "prec": prec,
        "prec_ix": prec_ix,
    }
    with _ask_lock:
        _ask_cache["fp"] = fp
        _ask_cache["bundle"] = bundle
    return bundle


def ask_api(qs=""):
    """상주 인덱스 기반 고속 검색 API(읽기 전용 · 아카이브/온톨로지/선례)."""
    if _PF is None:
        return {"ok": False, "error": "검색 엔진을 불러오지 못했습니다."}
    params = parse_qs(qs)
    q = (params.get("q") or [""])[0].strip()
    if len(q) < 2:
        return {"ok": False, "error": "질문이 너무 짧습니다."}
    if len(q) > _ASK_MAX_Q:
        q = q[:_ASK_MAX_Q]
    try:
        top = max(1, min(10, int((params.get("top") or ["4"])[0])))
    except ValueError:
        top = 4
    t0 = time.perf_counter()
    b = _ask_ensure_loaded()
    if not b:
        return {"ok": False, "error": "아카이브를 읽지 못했습니다."}
    sq, ont = _PF.prepare_query(q)
    meta = {}
    ar, pr = _PF.search_all(
        sq,
        b["art_ix"],
        b["prec_ix"],
        b["prec"],
        b["by_id"],
        art_top=top,
        curated=b["curated"],
        ont_boosts=ont.get("article_boosts"),
        out_meta=meta,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return {
        "ok": True,
        "query": q,
        "quick": any(x[0].startswith(_ASK_QUICK_PREFIX) for x in ar),
        "confidence": meta.get("confidence") or {},
        "articles": [
            {
                "id": aid,
                "score": s,
                "title": _PF._clip(b["by_id"][aid]["title"], 80),
                "excerpt": _PF._clip(text, 400) if n <= 2 else "",
            }
            for n, (aid, s, text) in enumerate(ar, 1)
        ],
        "ontology": {
            "matched_nodes": ont.get("matched_nodes") or [],
            "context_lines": [_PF._clip(ln, 220) for ln in (ont.get("context_lines") or [])],
        },
        "precedents": [
            {
                "id": pid,
                "score": s,
                "date": b["prec"][pid].get("date") or "",
                "q": _PF._clip(_PF._q_text(b["prec"][pid]), 110),
                "a": _PF._clip(_PF._a_text(b["prec"][pid]), 220),
            }
            for pid, s in pr
        ],
        "elapsed_ms": elapsed_ms,
    }


def blocks(a):
    """답 칸을 [(시각, 글, 봇인가)] 로 자른다."""
    parts = A_SPLIT.split(a or "")
    raw = [("", parts[0])] + [(parts[k], parts[k + 1]) for k in range(1, len(parts) - 1, 2)]
    return [(ts, t.strip(), t.strip().startswith(BOT_PREFIXES)) for ts, t in raw if (t or "").strip()]


def bot_real(a):
    """트리니티봇의 **실제 답**이 있는가 — 접수 확인만 있는 것은 답이 아니다(게이트의 answer_turns 와 같은 기준)."""
    bl = blocks(a)
    if not bl or not bl[0][2]:
        return any(b[2] for b in bl)
    return ("⏱" in bl[0][1] or "AI 1차 안내" in bl[0][1]) or len(bl) > 1


def wait_reason(r):
    """담당자의 답이 필요한 건인가. 필요 없으면 ""."""
    if (r.get("status") or "접수") == "완료":
        return ""
    bl = blocks(r.get("a"))
    q_stamps = re.findall(r"──── 추가 질문 \(([^)]*)\) ────", r.get("q") or "")
    last_q_ts = q_stamps[-1].strip() if q_stamps else ""
    last_a_ts = (bl[-1][0] or "").strip() if bl else ""
    if not last_a_ts and bl:
        lines = bl[-1][1].splitlines()
        if lines and lines[-1].startswith("⏱"):
            last_a_ts = lines[-1][1:].strip()
    answered_after = bool(last_q_ts and last_a_ts and last_a_ts >= last_q_ts)
    if any(not b[2] for b in bl) and ((r.get("q") or "").count(Q_MARK) < len(bl) or (not bl[-1][2] and answered_after)):
        return ""                                   # 담당자가 이미 답했고 그 뒤 추가 질문이 없다
    if not bl:
        return "답 없음"
    if not bot_real(r.get("a")):
        return "접수 확인만 — 트리니티봇이 아직 답하지 못함"
    if (r.get("q") or "").count(Q_MARK) >= len(bl) and not answered_after:
        return "추가 질문에 답하기 전"
    last = bl[-1][1]
    if NEED_INFO_MARK in last:
        return "추가 정보를 되물은 상태"
    if last.startswith("[트리니티봇 · 접수 확인"):
        return "보류 — 트리니티봇이 확정하지 못함"
    return ""


def _norm_gap(s):
    s = (s or "").lower()
    for w in _FILLER:
        s = s.replace(w, " ")
    return re.sub(r"[\W_]+", "", s)


def grams(s):
    s = _norm_gap(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def sim(a, b):
    A, B = grams(a), grams(b)
    return (len(A & B) / ((len(A) * len(B)) ** 0.5)) if (A and B) else 0.0


def _gap_codes(s):
    return set(_CODE_RE.findall((s or "").upper()))


def _gap_match(a, b):
    sc = sim(a, b)
    if sc >= GAP_SIM:
        return True
    ca, cb = _gap_codes(a), _gap_codes(b)
    return bool((ca & cb) and sc >= 0.20)


def _safe_int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    m = re.match(r"^\s*(\d+)", str(v))
    return int(m.group(1)) if m else None


def first_q(r):
    return (r.get("q") or "").split("\n\n" + Q_MARK)[0].strip()


_hb = {"pos": 0, "last_sec": None, "last_ts": "", "gaps": [], "n": 0, "path": ""}


def heartbeat(path):
    """하트비트 파일을 바이너리 오프셋으로 이어서 읽는다(CRLF 오차 없음 · 스레드 안전)."""
    if not path or not os.path.exists(path):
        return None, [], 0
    with _hb_lock:
        try:
            fsize = os.path.getsize(path)
        except OSError:
            return None, [], 0
        if _hb["path"] != path or fsize < _hb["pos"]:
            _hb.update(pos=0, last_sec=None, last_ts="", gaps=[], n=0, path=path)
        day_cache_b, day_cache_sec = None, 0
        last_sec, last_ts = _hb["last_sec"], _hb["last_ts"]
        pos = _hb["pos"]
        with io.open(path, "rb") as f:
            f.seek(pos)
            buf = b""
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                data = buf + chunk
                cut = data.rfind(b"\n")
                if cut < 0:
                    buf = data
                    continue
                pos += cut + 1
                buf = data[cut + 1:]
                for line in data[:cut].splitlines():
                    i = line.find(b'"ts": "')
                    if i < 0:
                        continue
                    ts_b = line[i + 7:i + 26]
                    if len(ts_b) < 19:
                        continue
                    try:
                        db = ts_b[:10]
                        if db != day_cache_b:
                            day_cache_sec = int(datetime.datetime(int(ts_b[0:4]), int(ts_b[5:7]), int(ts_b[8:10])).timestamp())
                            day_cache_b = db
                        sec = day_cache_sec + int(ts_b[11:13]) * 3600 + int(ts_b[14:16]) * 60 + int(ts_b[17:19])
                    except ValueError:
                        continue
                    ts_s = ts_b.decode("ascii", "replace")
                    if last_sec is not None and (sec - last_sec) > GAP_SEC:
                        _hb["gaps"].append({"from": last_ts[5:16], "to": ts_s[5:16], "min": int((sec - last_sec) // 60)})
                    last_sec = sec
                    last_ts = ts_s
                    _hb["n"] += 1
        _hb["pos"] = pos
        _hb["last_sec"] = last_sec
        _hb["last_ts"] = last_ts
        age = (time.time() - last_sec) if last_sec is not None else None
        return age, list(_hb["gaps"][-10:]), _hb["n"]


def ledger_view():
    """검증 장부(운영 기록일 때만). 못 읽어도 화면은 떠야 하므로 실패하면 빈 값과 사유를 돌려준다."""
    if TEST:
        return {}, None, None, None, {}, "시험 로그 — 검증 장부는 운영 기록에서만 봅니다."
    try:
        if VAC not in sys.path:
            sys.path.insert(0, VAC)
        import ledger as L                          # 읽기 함수만 쓴다(build · summarize · stage_progress · journal_stats)
        cfg = L.GATE._cfg()
        rows = L.build(cfg)
        return ({r["id"]: r for r in rows}, L.summarize(rows), L.stage_progress(rows, cfg),
                L.journal_stats(cfg["runtime_dir"]), cfg, "")
    except Exception as e:                           # noqa: BLE001
        return {}, None, None, None, {}, "검증 장부를 읽지 못했습니다: %s" % str(e)[:160]


def codes():
    try:
        with io.open(os.path.join(DASH if not TEST else PROD, "vacation", "config.json"), encoding="utf-8") as f:
            c = json.load(f).get("reason_codes")
        if isinstance(c, list) and c and all(isinstance(x, str) and x for x in c):
            return c
    except (OSError, ValueError):
        pass
    return list(DEFAULT_CODES)


def overview():
    merged = read_log()
    by_id, summ, prog, ops, cfg, note = ledger_view()
    queue, waiting, recheck = [], [], []
    count = {"total": len(merged), "bot": 0, "closed": 0, "scores": 0, "score_sum": 0}
    for iid, r in sorted(merged.items()):
        a, row = r.get("a") or "", by_id.get(iid) or {}
        real = bot_real(a)
        count["bot"] += real
        count["closed"] += bool(r.get("req_closed"))
        sc = _safe_int(r.get("req_score"))
        if sc is not None and sc > 0:
            count["scores"] += 1
            count["score_sum"] += sc
        base = {"id": iid, "date": r.get("date", ""), "type": r.get("type", ""), "req": r.get("req", ""),
                "role": r.get("role", ""), "status": r.get("status") or "접수", "q": r.get("q", ""), "a": a,
                "score": sc, "feedback": r.get("req_feedback", ""),
                "closed": r.get("req_closed", ""), "grade": r.get("botgrade", ""),
                "note": r.get("botgrade_note", ""), "codes": r.get("botgrade_code", ""),
                "verdict": VERDICT_KO.get((row.get("verdicts") or [""])[-1], ""),
                "model": row.get("model", ""), "stale": bool(row.get("stale")), "test": bool(row.get("test"))}
        # 검토 대상은 **장부가 세는 답**이면서 현재 로그에도 트리니티봇 답이 남아 있는 건으로 한정한다
        # (do_grade의 bot_real(r.get("a")) 검사와 기준을 일치시켜 [테스트 종결] 덮어쓰기 건을 제외).
        counted = (iid in by_id and not row.get("manual") and real) if not TEST else real
        if counted and (not r.get("botgrade") or row.get("stale")):
            queue.append(base)
        why = wait_reason(r)
        if why:
            waiting.append(dict(base, why=why))
        bad = []
        if sc is not None and sc <= 2:
            bad.append("문의자 평가 %s점" % sc)
        if r.get("botgrade") in ("보완", "틀림"):
            bad.append("판정 %s%s" % (r["botgrade"], (" · " + r["botgrade_code"]) if r.get("botgrade_code") else ""))
        if bad:
            recheck.append({"id": iid, "q": first_q(r)[:120], "why": " / ".join(bad),
                            "note": r.get("botgrade_note", "") or r.get("req_feedback", "")})
    # 현업 문의가 시험 문의보다 먼저. 그 안에서 낮은 평가 · 재판정 필요가 먼저, 그다음 오래된 순
    queue.sort(key=lambda x: (x["test"], not (x["score"] is not None and x["score"] <= 2), not x["stale"], x["id"]))
    waiting.sort(key=lambda x: (x["type"] != "긴급문의", x["id"]))
    clusters = []
    for w in waiting:                                # 답하지 못한 문의를 비슷한 것끼리 — 아카이브에 다음으로 쓸 것
        q = first_q(w)
        for c in clusters:
            if _gap_match(q, c["q"]):
                c["ids"].append(w["id"])
                break
        else:
            clusters.append({"q": q[:200], "ids": [w["id"]]})
    clusters.sort(key=lambda c: -len(c["ids"]))
    age, gaps, beats = heartbeat(cfg.get("heartbeat_path") if cfg else "")
    real = (summ or {}).get("real") or {}
    lat = ((summ or {}).get("latency") or {}).get("median")
    return {"ok": True, "test": TEST, "note": note, "codes": codes(), "count": count,
            "queue": queue, "waiting": waiting, "recheck": recheck[-30:][::-1],
            "gaps": [{"q": c["q"], "n": len(c["ids"]), "ids": c["ids"][:8]} for c in clusters[:10]],
            "ledger": {"answered": real.get("answered"), "graded": real.get("graded"),
                       "ok": real.get("맞음"), "fix": real.get("보완"), "wrong": real.get("틀림"),
                       "low": real.get("accuracy_low"), "latency": lat,
                       "by_type": {k: {"graded": v.get("graded"), "ok": v.get("맞음"), "low": v.get("accuracy_low")}
                                   for k, v in ((summ or {}).get("by_type") or {}).items()}},
            "stages": [{"no": s["no"], "name": s["name"], "pop": s["pop"], "graded": s["graded"], "need": s["need"],
                        "accuracy": s["accuracy"], "low": s["low"], "min_acc": s["min_acc"], "met": s["met"],
                        "need_n0": s["need_n0"], "has_cond": s["has_cond"]} for s in (prog or {}).get("stages", [])],
            "stage_now": (prog or {}).get("current"),
            "ops": ops or {}, "hb_age": age, "outages": gaps, "beats": beats}


# ─────────────────────────────────────────────── 쓰기 (log-inquiry.py 로만)
def helper(argv):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # [SECURE] Argument list, no shell - user text never reaches a command line parser (Category 1)
    out = subprocess.run([sys.executable, HELPER] + argv, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", env=env, cwd=DASH, timeout=60,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "기록 실패").strip()[-300:])


def with_answer(cur, text):
    """담당자 답을 답 칸에 이어 붙인다 — 표기 없는 블록 = 사람의 답(mode.human_blocks 가 그렇게 읽는다)."""
    if not (cur or "").strip():
        return text
    return "%s\n\n%s (%s) ────\n%s" % (cur.rstrip(), A_MARK, time.strftime("%Y-%m-%d %H:%M"), text)


def do_grade(d):
    iid, grade = d.get("id"), d.get("grade")
    note, text = clean(d.get("note"), 500), clean(d.get("answer"), MAX_TEXT)
    picked = [c for c in (d.get("codes") if isinstance(d.get("codes"), list) else []) if isinstance(c, str)]
    # [SECURE] Whitelist validation of every field before it reaches the helper (Category 1)
    if not (isinstance(iid, str) and ID_RE.match(iid)) or grade not in GRADES:
        return "판정을 골라 주십시오."
    allowed = codes()
    if any(c not in allowed for c in picked):
        return "없는 이유 코드입니다."
    if grade == "맞음":
        picked = []
    with _write_lock:
        r = read_log().get(iid)
        if r is None or not bot_real(r.get("a")):
            return "트리니티봇 답이 있는 문의를 찾지 못했습니다."
        argv = ["--id", iid, "--grade", grade]
        if note:
            argv += ["--grade-note", note]
        if picked:
            argv += ["--grade-code", ",".join(picked)]
        new_a = with_answer(r.get("a"), text) if text else ""
        if new_a:
            argv += ["--a", new_a]
        if d.get("close") is True:
            argv += ["--done"]
        try:
            helper(argv)
        except Exception as e:                       # noqa: BLE001
            sys.stdout.write("  [!] 판정 기록 실패 %s: %s\n" % (iid, e))
            return "기록하지 못했습니다. 잠시 뒤 다시 해 주십시오."
        again = read_log().get(iid) or {}            # 반영 확인 — 성공 메시지를 믿지 않는다
        if again.get("botgrade") != grade or (new_a and again.get("a") != new_a):
            return "기록이 반영되지 않았습니다. 화면을 새로 고쳐 확인해 주십시오."
    sys.stdout.write("  [+] 판정 %s %s%s\n" % (iid, grade, " + 담당자 답" if text else ""))
    return ""


def do_answer(d):
    iid, text = d.get("id"), clean(d.get("text"), MAX_TEXT)
    if not (isinstance(iid, str) and ID_RE.match(iid)):
        return "대상을 찾지 못했습니다."
    if not text:
        return "답변 내용을 적어 주십시오."
    with _write_lock:
        r = read_log().get(iid)
        if r is None:
            return "대상을 찾지 못했습니다."
        new_a = with_answer(r.get("a"), text)
        argv = ["--id", iid, "--a", new_a] + (["--done"] if d.get("close") is True else ["--status", "대기"])
        try:
            helper(argv)
        except Exception as e:                       # noqa: BLE001
            sys.stdout.write("  [!] 답변 기록 실패 %s: %s\n" % (iid, e))
            return "기록하지 못했습니다. 잠시 뒤 다시 해 주십시오."
        if (read_log().get(iid) or {}).get("a") != new_a:
            return "기록이 반영되지 않았습니다. 화면을 새로 고쳐 확인해 주십시오."
    sys.stdout.write("  [+] 담당자 답 %s\n" % iid)
    return ""


_report = {"path": ""}


def do_report():
    """복귀 보고서를 새로 만든다 — vacation/mode.py report (journal 을 읽을 뿐 · 비용 0 · 휴가모드는 건드리지 않는다)."""
    if TEST:
        return "시험 로그에서는 복귀 보고서를 만들지 않습니다."
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        out = subprocess.run([sys.executable, os.path.join(VAC, "mode.py"), "report"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", env=env, cwd=VAC, timeout=120,
                             creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception as e:                           # noqa: BLE001
        sys.stdout.write("  [!] 복귀 보고서 실패: %s\n" % e)
        return "복귀 보고서를 만들지 못했습니다."
    lines = [x.strip() for x in (out.stdout or "").splitlines() if x.strip()]
    p = next((x for x in lines if x.lower().endswith(".html") and os.path.isfile(x)), "")
    if out.returncode != 0 or not p:
        sys.stdout.write("  [!] 복귀 보고서 실패: %s\n" % (out.stderr or out.stdout or "")[-300:])
        return "복귀 보고서를 만들지 못했습니다."
    _report["path"] = p
    return ""


# ─────────────────────────────────────────────── 서버
def clean(s, limit):
    # [SECURE] Type check, control-character strip and length cap on all free text (Category 1)
    if not isinstance(s, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s).strip()[:limit]


class SingleServer(ThreadingHTTPServer):
    """중복 기동을 거부한다 — 두 번째 기동이 조용히 성공하면 옛 코드가 계속 서빙된다."""
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "SDDesk/1.0"
    sys_version = ""
    timeout = 15

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # [SECURE] Sniffing / framing / referrer / CSP hardening (Category 1)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.path != "/report":
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
            )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False))

    def _host_ok(self):
        # [SECURE] Host allow-list - blocks DNS rebinding against this local write API (Category 1)
        return (self.headers.get("Host") or "").lower() in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "text/plain; charset=utf-8", "허용되지 않은 주소입니다.")
        path = urlparse(self.path).path
        try:
            # [SECURE] Fixed routes only - no request path reaches the filesystem (Category 1)
            if path in ("/", "/index.html"):
                with io.open(os.path.join(HERE, "desk.html"), encoding="utf-8") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
            if path == "/api/overview":
                return self._json(overview())
            if path == "/erd":
                erd_html = os.path.join(ERD_DIR, "erd-viewer.html")
                if not os.path.isfile(erd_html):
                    return self._send(404, "text/plain; charset=utf-8", "ERD 뷰어가 아직 생성되지 않았습니다.")
                with io.open(erd_html, encoding="utf-8") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
            if path in HUB_PAGES:
                page = HUB_PAGES[path]
                if not os.path.isfile(page):
                    return self._send(404, "text/plain; charset=utf-8", "화면 파일을 찾지 못했습니다.")
                with io.open(page, encoding="utf-8") as f:
                    html = f.read()
                return self._send(200, "text/html; charset=utf-8", _board_readonly(html) if path == "/board" else html)
            diag_m = re.fullmatch(r"/diagnostics/(\d{4}-\d{2}-\d{2}_[^/\\]+\.html|index\.html|latest\.html)", unquote(path))
            if diag_m:
                dfile = os.path.join(DIAG_DIR, diag_m.group(1))
                if os.path.dirname(os.path.abspath(dfile)) == os.path.abspath(DIAG_DIR) and os.path.isfile(dfile):
                    with io.open(dfile, encoding="utf-8") as f:
                        return self._send(200, "text/html; charset=utf-8", f.read())
                return self._send(404, "text/plain; charset=utf-8", "화면 파일을 찾지 못했습니다.")
            data_key = unquote(path)
            if data_key in HUB_DATA:
                if not os.path.isfile(HUB_DATA[data_key]):
                    return self._send(404, "text/plain; charset=utf-8", "데이터 파일을 찾지 못했습니다.")
                with io.open(HUB_DATA[data_key], encoding="utf-8") as f:
                    return self._send(200, "application/javascript; charset=utf-8", f.read())
            if path == "/api/ontology":
                return self._json(ontology_api(urlparse(self.path).query))
            if path == "/api/ask":
                return self._json(ask_api(urlparse(self.path).query))
            if path == "/report":
                if not _report["path"] or not os.path.isfile(_report["path"]):
                    return self._send(404, "text/plain; charset=utf-8", "먼저 복귀 보고서를 만들어 주십시오.")
                with io.open(_report["path"], encoding="utf-8") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
        except Exception as e:                       # noqa: BLE001
            # [SECURE] Generic message to the browser, detail to the console only (Category 4)
            sys.stdout.write("  [!] %s 실패: %s\n" % (path, e))
            return self._send(500, "text/plain; charset=utf-8", "화면을 만들지 못했습니다.")
        return self._send(404, "text/plain; charset=utf-8", "없는 경로입니다.")

    def do_POST(self):
        if not self._host_ok():
            return self._json({"ok": False, "error": "허용되지 않은 주소입니다."}, 403)
        # [SECURE] JSON-only, same-origin / X-Requested-With and Sec-Fetch-Site check (Category 1, CSRF)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        host = (self.headers.get("Host") or "").strip().lower()
        origin = self.headers.get("Origin")
        origin_ok = (urlparse(origin).netloc.lower() == host) if origin else ((self.headers.get("X-Requested-With") or "").strip() == "SDDesk")
        if ctype != "application/json" or (sfs and sfs not in ("same-origin", "none")) or not origin_ok:
            return self._json({"ok": False, "error": "요청을 받지 않습니다."}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:               # [SECURE] Body size cap (Category 1)
                raise ValueError("size")
            d = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(d, dict):
                raise ValueError("shape")
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self._json({"ok": False, "error": "요청을 읽지 못했습니다."}, 400)
        path = urlparse(self.path).path
        try:
            if path == "/api/grade":
                err = do_grade(d)
            elif path == "/api/answer":
                err = do_answer(d)
            elif path == "/api/report":
                err = do_report()
            else:
                return self._json({"ok": False, "error": "없는 경로입니다."}, 404)
        except Exception as e:                       # noqa: BLE001
            sys.stdout.write("  [!] %s 실패: %s\n" % (path, e))
            err = "처리하지 못했습니다. 잠시 뒤 다시 해 주십시오."
        return self._json({"ok": not err, "error": err})


def main():
    if not os.path.exists(HELPER):
        print("  [X] log-inquiry.py 를 찾지 못했습니다: %s" % HELPER)
        return 1
    try:
        srv = SingleServer((HOST, PORT), Handler)
    except OSError as e:
        print("  [X] 포트 %d 를 열 수 없습니다: %s — 이미 떠 있는지 확인하십시오." % (PORT, e))
        return 1
    print("")
    print("  담당자 통합 화면%s  http://%s:%d/" % (" — ★ 시험 로그 ★" if TEST else "", HOST, PORT))
    print("  · 이 PC 에서만 열립니다. 기록: %s" % LOG_JS)
    print("  · 쓰기는 「트리니티봇 답 판정」 · 「담당자 답변」 둘뿐이며 log-inquiry.py 로만 합니다.")
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
#   - Exposure: bound to 127.0.0.1 only; Host header allow-list on GET and POST (DNS rebinding)
#   - CSRF: POST requires application/json + matching Origin (or X-Requested-With when Origin absent) + Sec-Fetch-Site check
#   - OS Command Injection: helper and report run with argument lists, no shell, fixed script paths
#   - Input validation: inquiry id by regex, grade and reason codes by whitelist, free text type-checked, stripped and capped
#   - Path Traversal: fixed routes; hub pages/data come from fixed dicts (HUB_PAGES · HUB_DATA, exact-match lookup); the only other file is the report this process just generated
#   - Hub (Ops Console): GET only, read-only; /board gets a 「보기 전용」 banner and showSaveFilePicker is disabled, and CSP connect-src 'self' blocks board-saver (5178)
#   - Error Message Information Exposure: generic messages to the browser, details to the console only
#   - Improper Resource Release: context managers for every file; server socket closed in finally
#   - Race Condition: thread locks around log cache, heartbeat state, and write operations
#   - Write integrity: writes only through log-inquiry.py (lock, backup, append-only) and re-read verification
#   - XSS & Headers: JSON API, textContent rendering, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, and CSP on UI responses
# Not Applied:
#   - [WARN] No login: any local user or process on this PC can read every inquiry (including internal notes) and write grades or answers. Same trust model as board-saver.py; never bind to 0.0.0.0
#   - [WARN] Plain HTTP on loopback; data at rest is the existing plaintext inquiry log
#   - [WARN] Inline script/style allowed in UI CSP ('unsafe-inline'); /report serves locally generated return report HTML without CSP
# --------------------------------------------------
