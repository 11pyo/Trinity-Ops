# -*- coding: utf-8 -*-
"""
ledger.py — 트리니티봇 답변 검증 장부 (2026-09-11 신설).

사용자 방향(2026-09-11): 트리니티봇이 담당자를 완전히 대체하도록 키운다. 운영계(PRD) 권한까지 MCP 로 받으려면
**여러 차례 검증**을 받아야 한다 — 그 검증의 근거를 쌓는 곳이 이 장부다(사용자가 고른 첫 단계).

  python ledger.py            장부(md)를 새로 만들고 요약 한 줄과 파일 경로를 출력한다
  python ledger.py --open     위와 같고, 만든 장부를 연다(제어판 「🧾 검증 장부」)
  python ledger.py --page     위와 같고, 지표 화면(bot-metrics.html)을 연다(제어판 「📊 봇 지표」)
  python ledger.py --json     요약만 JSON 으로

무엇을 세나
  · 트리니티봇이 답한 문의 = journal 의 `answered` 기록 — 트리니티봇이 실제로 쓴 것이라, 사람이 나중에 답을 고쳐도 남는다.
  · 판정 = inquiry-log.js 의 `botgrade`(맞음 · 보완 · 틀림) · `botgrade_note` · `botgrade_at`.
    담당자가 대시보드 ✏️수정의 「🤖 트리니티봇 답 판정」으로 고르거나, `log-inquiry.py --grade` 로 적는다.
    판정은 **문의 한 건 단위**(추가 답변 포함). 판정 뒤에 트리니티봇 답이 더 달리면 「재판정 필요」로 표시한다.
  · 정확도는 **현업 문의만** 센다. 담당자 본인 시험 문의(config `owner_test_req`)는 따로 보인다.
  · 2026-09-30~ 분석: 정확도 옆에 95% 신뢰구간 하한 · 권한 단계별 측정 대상(모집단) · 유형/담당/주별 · 처리 시간(접수 확인 → 첫 답) ·
    모델별 · 이유 코드 집계 · 운영 신호 · 재생 평가. 지표 화면용 데이터(bot-metrics-data.js)도 함께 만든다(런타임 폴더).
⛔ 읽고 장부 파일만 쓴다 — inquiry-log.js 에는 쓰지 않는다. Claude 를 부르지 않는다(비용 0).
⛔ 판정은 현업 화면에 나가지 않는다(intake-server `_public_view()` 는 6개 필드만 내보낸다).
"""

import collections
import io
import json
import math
import os
import re
import sys
import time
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import apply_answer as GATE                                     # noqa: E402

# 판정 이유 코드 — 정본은 config.json 의 reason_codes(이 값은 못 읽을 때의 기본). log-inquiry.py · task-board.html 과 같아야 한다(tests/unit_metrics.py 가 대조).
DEFAULT_REASON_CODES = ["사실오류", "위험안내", "기존기능놓침", "용어노출", "과잉보류", "누락", "가독성", "기타"]

GRADES = ("맞음", "보완", "틀림")
EXCLUDE_GRADE = "제외"
VERDICT_KO = {"answer": "답변", "dev": "개발 요청 정리", "escalate": "에스컬레이션", "hold": "보류"}
FILE_NAME = "트리니티봇_검증장부.md"
DATA_NAME = "bot-metrics-data.js"      # 지표 화면이 읽는 데이터(런타임 폴더 — 공유본 밖)
PAGE_NAME = "bot-metrics.html"          # 지표 화면(task-dashboard/)


def _intake():
    import importlib.util
    spec = importlib.util.spec_from_file_location("intake_mod_ledger", os.path.join(DASH, "intake-server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def answered_events(rt):
    """journal 의 answered 기록을 문의별로 모은다 — {id: [event, …]} (시간순)."""
    out = {}
    jd = os.path.join(rt, "journal")
    if not os.path.isdir(jd):
        return out
    for fn in sorted(os.listdir(jd)):
        if not fn.endswith(".jsonl"):
            continue
        for line in io.open(os.path.join(jd, fn), encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "answered" and e.get("id"):
                out.setdefault(e["id"], []).append(e)
    for es in out.values():
        es.sort(key=lambda e: e.get("ts", ""))
    return out


def ack_times(rt):
    """journal 의 접수 확인(ack) 시각 — 문의별 **처음** 것. 「접수 확인 → 첫 답」이 처리 시간의 기준이다
    (접수 → 첫 답 으로 재면 감시자가 꺼져 있던 시간이 섞인다 — 2026-09-30 점검에서 0~167분으로 들쭉날쭉했다)."""
    out = {}
    jd = os.path.join(rt, "journal")
    for fn in sorted(os.listdir(jd)) if os.path.isdir(jd) else []:
        if not fn.endswith(".jsonl"):
            continue
        for line in io.open(os.path.join(jd, fn), encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "ack" and e.get("id") and not e.get("err") and e["id"] not in out:
                out[e["id"]] = e.get("ts", "")
    return out


def _secs(t0, t1):
    try:
        a = datetime.datetime.strptime(t0, "%Y-%m-%d %H:%M:%S")
        b = datetime.datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    d = (b - a).total_seconds()
    return d if d >= 0 else None                                     # 음수만 잘못된 짝 — 길게 걸린 건(감시자는 켜져 있었는데 답이 늦은 건)은 그대로 센다


def _week(ts):
    """그 주 월요일 날짜(YYYY-MM-DD) — 주별 추이 묶음 키."""
    try:
        d = datetime.date(int(ts[:4]), int(ts[5:7]), int(ts[8:10]))
    except (ValueError, IndexError, TypeError):
        return ""
    return (d - datetime.timedelta(days=d.weekday())).strftime("%Y-%m-%d")


# 담당 업무는 현업이 직접 치는 자유 글이다 — 그대로 지표 데이터에 넣으면 이름 · 전화번호가 섞일 수 있고 같은 담당이 표기 차이로 갈라진다.
# 접수 화면이 제안하는 여섯 가지로 묶고 그 밖은 「(기타)」 로 센다(원문은 문의 기록 · journal 에 그대로 있다).
ROLE_BUCKETS = (("영업지원", "영업지원"), ("여신", "여신담당"), ("재경", "재경"), ("회계", "재경"), ("세금계산서", "재경"), ("재무", "재경"),
                ("구매", "구매"), ("물류", "물류·출고"), ("출고", "물류·출고"), ("영업", "영업대표"))


def role_bucket(text):
    t = re.sub(r"\s+", "", text or "")
    if not t:
        return ""
    for kw, name in ROLE_BUCKETS:
        if kw in t:
            return name
    return "(기타)"


# ref 대조 결과(apply_answer.ref_check) 표기 순서 · 이름
REF_CHECK_KO = (("ok", "확인"), ("not_read", "미열람"), ("not_found", "없는 앵커"), ("empty", "근거 없음"), ("error", "대조 오류"))


def fp_label(e):
    """답 기록의 판 지문 「프롬프트·규칙·아카이브」(앞 8자리씩) — 지문이 없는 옛 기록은 빈 문자열."""
    parts = [(e.get(k) or "") for k in ("prompt_fp", "rules_fp", "archive_fp")]
    return "·".join(p or "-" for p in parts) if any(parts) else ""


def _tally(vals):
    out = {}
    for v in vals:
        if v:
            out[v] = out.get(v, 0) + 1
    return out


def build(cfg=None, merged=None, norm=None):
    """트리니티봇이 답한 문의마다 한 줄 — 판정·재판정 필요 여부까지.

    2026-09-30 독립 검토 반영: 한 문의에 트리니티봇 답과 수동 답이 함께 있으면 **트리니티봇 답만** 본다(수동 답이 마지막이라는 이유로 문의 전체가
    수동이 돼 「틀림」 판정 받은 트리니티봇 답이 정확도에서 빠지는 일이 없게). 수동 답뿐인 문의만 수동으로 따로 센다."""
    cfg = cfg or GATE._cfg()
    if merged is None or norm is None:
        ix = _intake()
        if merged is None:
            merged = ix._read_log()
        norm = norm or ix._norm
    owner = norm(cfg.get("owner_test_req") or "")
    testers = set(norm(x) for x in (cfg.get("test_reqs") or []) if x)     # ③ 검증용 요청자도 시험으로 센다
    rows = []
    acks = ack_times(cfg["runtime_dir"])
    for iid, es in answered_events(cfg["runtime_dir"]).items():
        r = merged.get(iid, {})
        bot = [e for e in es if e.get("source") != "manual"]                   # 감시자가 낸 답(출처가 없는 옛 기록도 트리니티봇 답으로 센다)
        use = bot or es                                                        # 수동 답뿐이면 그것으로 — 아래에서 manual 로 표시
        first_real = next((e for e in use if not e.get("fallback")), None)      # 시스템이 낸 보류는 「첫 답」이 아니다
        raw_g = (r.get("botgrade") or "").strip()
        excluded = (raw_g == EXCLUDE_GRADE)
        g = raw_g if raw_g in GRADES else ""
        at = (r.get("botgrade_at") or "").strip()
        last = use[-1].get("ts", "")
        rows.append({
            "id": iid,
            "req": r.get("req", ""),
            "type": r.get("type", ""),
            "status": r.get("status", "") or "접수",
            "test": (bool(owner) and norm(r.get("req", "")) == owner) or norm(r.get("req", "")) in testers,
            "verdicts": [e.get("verdict") or "?" for e in use],
            "first": use[0].get("ts", ""),
            "last": last,
            "grade": g,
            "excluded": excluded,
            "note": (r.get("botgrade_note") or "").strip(),
            "graded_at": at,
            # 판정 뒤에 트리니티봇 답이 또 달렸다(추가 질문에 대한 답) — 판정이 그 답까지 본 것이 아니다
            "stale": bool((g or excluded) and at and last[:16] > at[:16]),
            "q": (r.get("q") or "").replace("\n", " ").strip()[:80],
            # 2026-09-30~ 답 기록에 붙는 출처 — 옛 기록엔 없다(빈 값 = 출처 미상 · 그대로 트리니티봇 답으로 센다)
            "source": use[-1].get("source") or "",
            "manual": not bot,                                       # 감시자가 아닌 곳(세션 직접 · 시험)에서 게이트로 넣은 답뿐인 문의
            "model": use[-1].get("model") or "",
            "fallback": bool(use[-1].get("fallback")),                # 「자동 처리 N회 실패」로 시스템이 낸 보류 — 모델의 답이 아니다
            "engine": use[-1].get("engine") or "",
            "sec": use[-1].get("sec"),
            "cost_share": use[-1].get("cost_share_usd"),
            "tag": use[-1].get("tag") or "",
            "role": role_bucket(use[-1].get("role") or (r.get("role") or "")),
            "codes": [c for c in re.split(r"[,\s]+", (r.get("botgrade_code") or "").strip()) if c],
            "lat_sec": _secs(acks.get(iid), first_real.get("ts")) if (first_real and acks.get(iid)) else None,
            "lat_model": (first_real.get("model") or "") if first_real else "",       # 처리 시간은 **첫 답을 낸** 모델 몫이다(추가 답의 모델이 아니다)
            "expected": (r.get("bot_expected_verdict") or "").strip(),
            "week": _week(use[0].get("ts", "")),
            # 2026-10-16~ 그림자 기록 — 마지막 트리니티봇 답의 ref 대조 결과와 판 지문(옛 기록엔 없다 = 빈 값)
            "ref_check": use[-1].get("ref_check") or "",
            "fp": fp_label(use[-1]),
        })
    rows.sort(key=lambda x: x["first"])
    return rows


def wilson_low(k, n, z=1.96):
    """정확도의 95% 신뢰구간 **하한**(Wilson) — 표본이 작으면 크게 깎인다. n=0 이면 None.

    「지금까지 맞았다」가 아니라 「이 표본으로 95% 확신할 수 있는 최소 정확도」다.
    예) 전부 맞음일 때 1건 → 21% · 20건 → 84% · 50건 → 93% · 100건 → 96%.
    (2026-09-30: 권한 확대 기준의 판정 건수(20 · 50 · 100)가 안전 증명이 아니라 속도 관문임을 보이려고 추가했다.)
    """
    if not n:
        return None
    p = float(k) / n
    d = 1.0 + z * z / n
    c = p + z * z / (2.0 * n)
    r = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (c - r) / d)


def zero_error_need(min_acc, z=1.96):
    """틀림이 하나도 없을 때 95% 하한이 min_acc 에 닿으려면 최소 몇 건이 필요한가. min_acc >= 1 이면 None(불가능)."""
    if not min_acc or min_acc <= 0:
        return 0
    if min_acc >= 1:
        return None
    return int(math.ceil(min_acc * z * z / (1.0 - min_acc)))


# ── 판정 행렬 (Confusion Matrix & Precision/Recall) ───────────────────────────
CLASSES = ("answer", "hold", "escalate", "dev")
SOURCES = ("explicit", "agree", "inferred")
HOLDISH = ("hold", "escalate")
FATAL_CODES = ("사실오류", "위험안내")
OVERHOLD_CODE = "과잉보류"


def predicted_of(r):
    v = (r.get("verdicts") or [""])[-1]
    return v if v in CLASSES else None


def resolve_actual(r):
    """장부 행 → (정답 라벨, 출처). 미판정 (None, None) · 추정불가 ("unknown", "unknown")."""
    pred = predicted_of(r)
    if pred is None:
        return (None, None)
    exp = (r.get("expected") or "").strip()
    if exp in CLASSES:
        return (exp, "explicit")
    g = r.get("grade") or ""
    codes = set(r.get("codes") or [])
    if g == "맞음":
        return (pred, "agree")
    if g in ("틀림", "보완") and OVERHOLD_CODE in codes and pred in HOLDISH:
        return ("answer", "inferred")
    if g == "틀림":
        if pred == "answer" and codes & set(FATAL_CODES):
            return ("hold", "inferred")
        return ("unknown", "unknown")
    if g == "보완":
        return (pred, "agree")
    return (None, None)


def confusion(rows, sources=SOURCES):
    cm = {a: {p: 0 for p in CLASSES} for a in CLASSES}      # cm[정답][트리니티봇]
    by_src = {s: 0 for s in SOURCES}
    unknown = unlabeled = 0
    for r in rows:
        actual, src = resolve_actual(r)
        if actual is None:
            unlabeled += 1
        elif actual == "unknown":
            unknown += 1
        elif src in sources:
            cm[actual][predicted_of(r)] += 1
            by_src[src] += 1
    return {"cm": cm, "n": sum(by_src.values()), "by_source": by_src,
            "unknown": unknown, "unlabeled": unlabeled}


def _wilson(k, n, z=1.96):
    if not n:
        return (None, None)
    p = float(k) / n
    d = 1.0 + z * z / n
    c = p + z * z / (2.0 * n)
    r = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def prf(cm, cls):
    tp = cm[cls][cls]
    fp = sum(cm[a][cls] for a in CLASSES if a != cls)
    fn = sum(cm[cls][p] for p in CLASSES if p != cls)
    P = tp / float(tp + fp) if (tp + fp) else None
    R = tp / float(tp + fn) if (tp + fn) else None
    F = (2 * P * R / (P + R)) if (P and R) else (0.0 if (P is not None and R is not None) else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": P, "recall": R, "f1": F,
            "precision_low": wilson_low(tp, tp + fp), "recall_low": wilson_low(tp, tp + fn)}


def safety_rates(cm):
    """unsafe_answer_rate = 트리니티봇 answer 중 정답이 answer 가 아니던 비율(정밀도 쪽 위험)
    over_hold_rate     = 트리니티봇 hold·escalate 중 정답이 answer 이던 비율(운영 효율)
    missed_human_rate  = 정답이 hold·escalate(사람이 봐야 했던 건) 중 트리니티봇이 answer 로 내보낸 비율 — 놓침(High Recall 관점 · 2026-10-16)"""
    ans_n = sum(cm[a]["answer"] for a in CLASSES)
    unsafe = sum(cm[a]["answer"] for a in CLASSES if a != "answer")
    hold_n = sum(cm[a][p] for a in CLASSES for p in HOLDISH)
    over = sum(cm["answer"][p] for p in HOLDISH)
    human_n = sum(cm[a][p] for a in HOLDISH for p in CLASSES)
    missed = sum(cm[a]["answer"] for a in HOLDISH)
    out = {}
    for name, k, n in (("unsafe_answer_rate", unsafe, ans_n), ("over_hold_rate", over, hold_n),
                       ("missed_human_rate", missed, human_n)):
        lo, hi = _wilson(k, n)
        out[name] = {"k": k, "n": n, "rate": (k / float(n)) if n else None, "low": lo, "high": hi}
    return out


def _view(rows, sources):
    c = confusion(rows, sources)
    return {"n": c["n"], "matrix": c["cm"], "by_source": c["by_source"], "unknown": c["unknown"],
            "unlabeled": c["unlabeled"], "per_class": {k: prf(c["cm"], k) for k in CLASSES},
            "safety": safety_rates(c["cm"])}


def to_js_obj(rows):
    return {"classes": list(CLASSES), "all": _view(rows, SOURCES),
            "strict": _view(rows, ("explicit", "agree"))}


def _p(x):
    return "-" if x is None else "%.0f%%" % (x * 100)


def render_md(vm):
    a, t = vm["all"], vm["strict"]["safety"]
    L = ["<!-- verdict-matrix:start -->", "## 판정 행렬 (정답 라벨 기준)", "",
         "표본 %d건 · 출처 명시 %d / 동의 %d / 추정 %d · 추정불가 제외 %d" % (
             a["n"], a["by_source"]["explicit"], a["by_source"]["agree"],
             a["by_source"]["inferred"], a["unknown"]), "",
         "| 정답＼트리니티봇 | " + " | ".join(CLASSES) + " |", "|---|" + "---|" * len(CLASSES)]
    for act in CLASSES:
        L.append("| **%s** | " % act + " | ".join(str(a["matrix"][act][p]) for p in CLASSES) + " |")
    L += ["", "| 안전 지표 | 값 | 건수 | 95% 구간 | 추정 제외(strict) |", "|---|---|---|---|---|"]
    for k, ko in (("unsafe_answer_rate", "오답변율 (안전 리스크)"), ("over_hold_rate", "과잉보류율 (운영 효율)"),
                  ("missed_human_rate", "사람 확인 누락률 (High Recall)")):
        x = a["safety"][k]
        L.append("| %s | %s | %d/%d | %s~%s | %s |" % (ko, _p(x["rate"]), x["k"], x["n"],
                                                     _p(x["low"]), _p(x["high"]), _p(t[k]["rate"])))
    L += ["", "| 클래스 | 정밀도(하한) | 재현율(하한) | F1 |", "|---|---|---|---|"]
    for k in CLASSES:
        c = a["per_class"][k]
        L.append("| %s | %s (%s) | %s (%s) | %s |" % (k, _p(c["precision"]), _p(c["precision_low"]),
                                                       _p(c["recall"]), _p(c["recall_low"]), _p(c["f1"])))
    L += ["<!-- verdict-matrix:end -->", ""]
    return "\n".join(L)


def _count(rs):
    c = {g: 0 for g in GRADES}
    for r in rs:
        if r["grade"]:
            c[r["grade"]] += 1
    n = sum(c.values())
    out = {"answered": len(rs), "graded": n, "ungraded": len(rs) - n,
           "stale": sum(1 for r in rs if r["stale"]),
           "accuracy": (c["맞음"] / float(n)) if n else None,
           "accuracy_low": wilson_low(c["맞음"], n),
           "usable": ((c["맞음"] + c["보완"]) / float(n)) if n else None,
           "usable_low": wilson_low(c["맞음"] + c["보완"], n)}
    out.update(c)
    return out


def _pctile(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _lat(rows):
    """처리 시간(접수 확인 → 첫 답, 초) — 시스템이 낸 보류 · 수동 답은 뺀다."""
    vals = [r["lat_sec"] for r in rows if r.get("lat_sec") is not None and not r.get("fallback") and not r.get("manual")]
    return {"n": len(vals), "median": _pctile(vals, 0.5), "p90": _pctile(vals, 0.9)}


def _by(rows, keyf):
    d = {}
    for r in rows:
        d.setdefault(keyf(r), []).append(r)
    return d


def summarize(rows):
    manual = [r for r in rows if r.get("manual")]                 # 트리니티봇 답이 아니다 — 정확도 · 판정 대기에서 뺀다
    excluded = [r for r in rows if r.get("excluded") and not r.get("manual")]   # 중복·오발송 등 평가 제외
    real = [r for r in rows if not r["test"] and not r.get("manual") and not r.get("excluded")]
    test = [r for r in rows if r["test"] and not r.get("manual") and not r.get("excluded")]
    by_v = _by(real, lambda r: r["verdicts"][-1])                  # 마지막 판정 종류 기준
    by_m = _by(real, lambda r: "(시스템 보류)" if r.get("fallback") else (r.get("model") or ""))   # "" = 모델 기록이 없는 옛 답
    by_t = _by(real, lambda r: r.get("type") or "(미상)")
    by_r = _by(real, lambda r: r.get("role") or "(미기재)")
    by_w = _by(real, lambda r: r.get("week") or "")
    codes = {}
    for r in real + test:                                          # 이유 코드는 트리니티봇을 고칠 재료 — 시험 문의분도 센다
        if r["grade"] in ("보완", "틀림"):
            for c in r.get("codes") or []:
                codes[c] = codes.get(c, 0) + 1
    return {"real": _count(real), "test": _count(test), "manual": _count(manual), "excluded": len(excluded),
            "by_verdict": {k: _count(v) for k, v in by_v.items()},
            "by_model": {k: _count(v) for k, v in by_m.items()},
            "by_type": {k: _count(v) for k, v in by_t.items()},
            "by_role": {k: _count(v) for k, v in by_r.items()},
            "by_week": {k: _count(v) for k, v in by_w.items() if k},
            "codes": codes,
            "latency": _lat(real),
            "latency_by_model": {k: _lat(v) for k, v in _by(real, lambda r: r.get("lat_model") or "").items()},
            "ref_check": _tally(r.get("ref_check") for r in real if not r.get("fallback")),
            "by_fp": {k: _count(v) for k, v in _by([r for r in real if not r.get("fallback")], lambda r: r.get("fp") or "").items() if k},
            "waiting": [r["id"] for r in rows if not r.get("manual") and not r.get("excluded") and (not r["grade"] or r["stale"])]}


def _pop_label(s):
    """그 단계가 **무엇을 맡기는지**에 맞춘 측정 대상 — 비어 있으면 현업 판정 전체."""
    parts = []
    if s.get("types"):
        parts.append(" · ".join(s["types"]))
    if s.get("verdicts"):
        parts.append(" · ".join(VERDICT_KO.get(v, v) for v in s["verdicts"]))
    return " · ".join(parts)


def _in_pop(r, s):
    if s.get("types") and (r.get("type") or "") not in s["types"]:
        return False
    if s.get("verdicts") and ((r["verdicts"][-1] if r.get("verdicts") else "") not in s["verdicts"]):
        return False
    return True


def stage_progress(rows, cfg):
    """④ 권한 확대 기준표 — 현업 문의 판정으로 지금 단계와 다음 단계까지 남은 것. 승격은 사람이 정한다(여기선 보여 주기만).

    2026-09-30~ 단계마다 **그 단계가 맡기는 일에 맞는 모집단**으로 잰다(config `stages[].types` · `verdicts`).
    예) 「단순문의 스스로 완료」를 허락할지는 단순문의 · 답변 건의 정확도로 본다 — 개발 요청 답이 섞이면 잰 것과 허락할 것이 달라진다.
    """
    stages = sorted((cfg or {}).get("stages") or [], key=lambda s: s.get("no", 0))
    if not stages:
        return None
    # ⚠ 1단계는 「조건 없음」이어야 한다(지금 단계의 출발점) — 조건이 있는 1단계가 미충족이어도 지금 단계는 1 로 표시된다.
    real = [r for r in rows if not r["test"] and not r.get("manual") and not r.get("excluded") and r["grade"]]
    real.sort(key=lambda r: r["graded_at"] or r["first"])
    n_all = len(real)
    ok_all = sum(1 for r in real if r["grade"] == "맞음")
    acc_all = (ok_all / float(n_all)) if n_all else 0.0
    res = []
    for s in stages:
        pool = [r for r in real if _in_pop(r, s)]
        n = len(pool)
        ok = sum(1 for r in pool if r["grade"] == "맞음")
        acc = (ok / float(n)) if n else 0.0
        need = int(s.get("min_graded") or 0)
        recent = pool[-need:] if need else pool
        wrong = sum(1 for r in recent if r["grade"] == "틀림")
        min_acc = float(s.get("min_accuracy") or 0)
        has_cond = bool(need) or bool(min_acc) or (s.get("max_wrong") is not None)
        met = (not has_cond) or (n >= need and acc >= min_acc and wrong <= int(s.get("max_wrong") or 0))   # 조건 없는 단계(1단계)는 항상 충족
        res.append({"no": s.get("no"), "name": s.get("name", ""), "grants": s.get("grants", ""), "need": need,
                    "min_acc": min_acc, "max_wrong": int(s.get("max_wrong") or 0),
                    "wrong_recent": wrong, "met": met, "has_cond": has_cond,
                    "graded": n, "accuracy": acc, "low": wilson_low(ok, n), "pop": _pop_label(s),
                    "need_n0": zero_error_need(min_acc) if (need or min_acc) else 0})
    cur = 1
    for r_ in res:
        if r_["met"]:
            cur = r_["no"]
        else:
            break
    nxt = next((r_ for r_ in res if r_["no"] > cur), None)
    return {"graded": n_all, "accuracy": acc_all, "current": cur, "stages": res, "next": nxt}


def _pct(x):
    return "—" if x is None else "%.0f%%" % (x * 100)


def _acc(c):
    """정확도 · 95% 하한을 한 칸에."""
    if c.get("accuracy") is None:
        return "—"
    return "%s (하한 %s)" % (_pct(c["accuracy"]), _pct(c.get("accuracy_low")))


def _dur(sec):
    if sec is None:
        return "—"
    if sec < 90:
        return "%d초" % round(sec)
    return ("%.1f분" % (sec / 60.0)) if sec < 5400 else ("%.1f시간" % (sec / 3600.0))


def _cond_text(x):
    """단계 조건 글 — 조건이 하나도 없으면 「처음부터」, 최소 판정 건수 없이 정확도 · 틀림 기준만 있는 단계도 그대로 보인다."""
    if not x.get("has_cond", bool(x.get("need"))):
        return "처음부터(조건 없음)"
    parts = []
    if x.get("need"):
        parts.append("판정 %d건 이상" % x["need"])
    if x.get("min_acc"):
        parts.append("정확도 %s 이상" % _pct(x["min_acc"]))
    parts.append(("최근 %d건 중 틀림 %d건 이하" % (x["need"], x["max_wrong"])) if x.get("need") else ("틀림 %d건 이하" % x["max_wrong"]))
    return " · ".join(parts)


def line_summary(s, prog=None):
    r = s["real"]
    tail = ""
    if prog and prog.get("next"):
        nx = prog["next"]
        tail = " · 권한 %d단계 → %d단계까지 %s%s" % (
            prog["current"], nx["no"],
            ("판정 %d/%d" % (nx.get("graded", prog["graded"]), nx["need"])) if nx.get("need") else _cond_text(nx),
            ("(%s 기준)" % nx["pop"]) if nx.get("pop") else "")
    if not r["answered"]:
        return "트리니티봇이 답한 현업 문의 없음 (시험 문의 %d건)" % s["test"]["answered"]
    low = r.get("accuracy_low")
    return ("현업 문의 트리니티봇 답 %d건 · 판정 %d건(판정 전 %d%s) · 맞음 %d · 보완 %d · 틀림 %d · 정확도 %s%s%s"
            % (r["answered"], r["graded"], r["ungraded"],
               (" · 재판정 필요 %d" % r["stale"]) if r["stale"] else "",
               r["맞음"], r["보완"], r["틀림"], _pct(r["accuracy"]),
               ("(95%% 하한 %s)" % _pct(low)) if low is not None else "", tail))


# ── 운영 신호(전체 기간) · 재생 평가 — 장부 옆에 함께 보이게
def journal_stats(rt):
    """journal 전체에서 호출 · 비용 · 실패 · 안전 신호를 센다(전체 기간 누적 — 기간별 보고는 복귀 보고서)."""
    c = collections.Counter()
    kinds = collections.Counter()
    runs, cost, denials = 0, 0.0, 0
    jd = os.path.join(rt, "journal")
    for fn in sorted(os.listdir(jd)) if os.path.isdir(jd) else []:
        if not fn.endswith(".jsonl"):
            continue
        for line in io.open(os.path.join(jd, fn), encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ev = e.get("event")
            c[ev] += 1
            if ev == "run":
                runs += 1
                cost += float(e.get("cost_usd") or 0)
                denials += int(e.get("denials") or 0)
            elif ev == "claude_fail":
                kinds[e.get("kind") or "(옛 형식)"] += 1
    return {"runs": runs, "cost_usd": round(cost, 2), "fail": dict(kinds), "fail_total": sum(kinds.values()),
            "schema_problem": c["schema_problem"], "card_leak_blocked": c["card_leak_blocked"],
            "failover": c["failover"], "denials": denials, "salvaged": c["salvaged"]}


def replay_history(path=None):
    """재생 평가 기록(vacation/bench/history.jsonl) — 없으면 빈 목록."""
    path = path or os.path.join(HERE, "bench", "history.jsonl")
    out = []
    if os.path.exists(path):
        for line in io.open(path, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def render(rows, s, prog=None, ops=None, replay=None):
    r, t = s["real"], s["test"]
    L = ["# 트리니티봇 답변 검증 장부", "",
         "갱신 %s · 판정은 담당자가 합니다 — 대시보드 ✏️수정 「🤖 트리니티봇 답 판정」(맞음 · 보완 · 틀림) "
         "또는 `log-inquiry.py --id <문의> --grade <판정> --grade-note \"…\" --grade-code <이유 코드>`" % time.strftime("%Y-%m-%d %H:%M"), "",
         "> 보완 · 틀림이면 **이유 코드**(%s)도 골라 주십시오 — 메모는 글이라 세지 못하고, 코드는 아래에서 유형별로 셉니다."
         % " · ".join(GATE._cfg().get("reason_codes") or DEFAULT_REASON_CODES), "",
         "> 트리니티봇이 운영계 권한까지 받으려면 여러 차례 검증이 필요합니다. 이 장부가 그 근거입니다.",
         "> 맞음 = 그대로 현업에 줘도 되는 답 · 보완 = 방향은 맞으나 빠지거나 덧붙일 것이 있음 · 틀림 = 결론이 틀림.",
         "> 정확도 옆 「하한」 = 95% 신뢰구간 하한 — 「이 표본으로 95% 확신할 수 있는 최소 정확도」입니다. 표본이 작으면 크게 깎입니다.",
         "> 권한 확대 기준 = 운영규칙 §7-3 (숫자는 `vacation/config.json` `stages`) — 아래 「권한 확대 단계」 표가 지금 단계를 보여 줍니다. 승격은 담당자가 정합니다.", "",
         "## 요약 — 현업 문의", "",
         "| 항목 | 값 |", "|---|---|",
         "| 트리니티봇이 답한 현업 문의 | **%d건** |" % r["answered"],
         "| 판정 완료 | %d건 (판정 전 %d건%s) |" % (r["graded"], r["ungraded"],
                                              (" · 재판정 필요 %d건" % r["stale"]) if r["stale"] else ""),
         "| 맞음 · 보완 · 틀림 | %d · %d · %d |" % (r["맞음"], r["보완"], r["틀림"]),
         "| **정확도** (맞음 ÷ 판정) | **%s**%s |" % (_pct(r["accuracy"]),
                                                    (" — 95%% 하한 %s%s" % (_pct(r.get("accuracy_low")),
                                                                             " · 표본이 작아 통계로 보기 어렵습니다" if r["graded"] < 30 else ""))
                                                    if r["graded"] else ""),
         "| 쓸 만한 답 (맞음+보완 ÷ 판정) | %s |" % (("%s (95%% 하한 %s)" % (_pct(r["usable"]), _pct(r.get("usable_low")))) if r["graded"] else "—"),
         "| 처리 시간 (접수 확인 → 첫 답) | %s |" % (
             ("중앙값 %s · 90%% %s (%d건)" % (_dur(s["latency"]["median"]), _dur(s["latency"]["p90"]), s["latency"]["n"]))
             if s.get("latency", {}).get("n") else "아직 없음(2026-09-30 이전 기록은 접수 확인이 남은 것만 셉니다)"),
         "",
         "담당자 본인 시험 문의는 정확도에 넣지 않습니다 — %d건 · 판정 %d · 맞음 %d · 보완 %d · 틀림 %d."
         % (t["answered"], t["graded"], t["맞음"], t["보완"], t["틀림"]), ""]
    m = s.get("manual") or {}
    if m.get("answered"):
        L += ["감시자가 아닌 곳(세션에서 직접 · 시험)에서 게이트로 넣은 답 %d건은 트리니티봇 정확도에 넣지 않습니다." % m["answered"], ""]
    if s.get("excluded"):
        L += ["중복·오발송 등 평가 제외(`제외`)로 분류한 %d건은 정확도·승격 기준에서 제외합니다." % s["excluded"], ""]
    if prog:
        L += ["## 권한 확대 단계 — 현업 문의 판정 기준 (승격은 담당자가 정합니다)", "",
              "지금 **%d단계** · 현업 판정 %d건 · 정확도 %s" % (prog["current"], prog["graded"], _pct(prog["accuracy"])), "",
              "| 단계 | 이름 | 재는 대상 | 조건 | 지금 | 맡기는 일 |", "|---|---|---|---|---|---|"]
        for x in prog["stages"]:
            cond = _cond_text(x)
            now = "✅ 충족" if x["met"] else (("판정 %d/%d · " % (x["graded"], x["need"]) if x["need"] else "판정 %d건 · " % x["graded"]) +
                                              "정확도 %s(하한 %s) · 최근 틀림 %d"
                                              % (_pct(x["accuracy"]) if x["graded"] else "—", _pct(x["low"]), x["wrong_recent"]))
            L.append("| %s | %s | %s | %s | %s | %s |" % (x["no"], x["name"], x.get("pop") or "현업 판정 전체", cond, now, x["grants"]))
        L.append("")
        gaps = [x for x in prog["stages"] if x["need"] and x.get("need_n0") and x["need_n0"] > x["need"]]
        if gaps:
            L += ["> **표본 크기 주의** — 틀림이 하나도 없어도 95% 하한이 기준 정확도에 닿으려면 " +
                  " · ".join("%d단계 %d건" % (x["no"], x["need_n0"]) for x in gaps) +
                  " 이 필요합니다. 지금 기준의 판정 건수(" + " · ".join("%d" % x["need"] for x in gaps) +
                  ")는 「그 정도는 봐야 한다」는 속도 관문이지 안전 증명이 아닙니다.", ""]
    real_rows = [r for r in rows if not r["test"] and not r.get("manual") and not r.get("excluded") and not r.get("fallback")]
    if real_rows:
        L.append(render_md(to_js_obj(real_rows)))
    if s["by_verdict"]:
        L += ["## 트리니티봇 판정 종류별 (현업 문의)", "",
              "| 트리니티봇 판정 | 건수 | 판정 완료 | 맞음 | 보완 | 틀림 | 정확도 |", "|---|---|---|---|---|---|---|"]
        for k in ("answer", "dev", "escalate", "hold"):
            c = s["by_verdict"].get(k)
            if c:
                L.append("| %s | %d | %d | %d | %d | %d | %s |" % (VERDICT_KO.get(k, k), c["answered"], c["graded"],
                                                                  c["맞음"], c["보완"], c["틀림"], _acc(c)))
        L.append("")
    models = dict((k, c) for k, c in (s.get("by_model") or {}).items() if k)
    if models:
        lm = s.get("latency_by_model") or {}
        L += ["## 모델별 (현업 문의) — 어느 모델이 답했나", "",
              "| 모델 | 답한 건 | 판정 완료 | 맞음 | 보완 | 틀림 | 정확도 | 처리 시간(중앙값) |", "|---|---|---|---|---|---|---|---|"]
        for k, c in sorted(models.items(), key=lambda kv: -kv[1]["answered"]):
            L.append("| `%s` | %d | %d | %d | %d | %d | %s | %s |" % (k, c["answered"], c["graded"], c["맞음"], c["보완"], c["틀림"],
                                                                     _acc(c), _dur((lm.get(k) or {}).get("median"))))
        unk = (s.get("by_model") or {}).get("")
        if unk:
            L.append("| (기록 없음 — 2026-09-30 이전 답) | %d | %d | %d | %d | %d | %s | %s |"
                     % (unk["answered"], unk["graded"], unk["맞음"], unk["보완"], unk["틀림"], _acc(unk),
                        _dur((lm.get("") or {}).get("median"))))
        L += ["", "모델은 Claude Code 권장(Default)을 따라가므로 CLI 가 올라가면 조용히 바뀝니다 — 바뀐 뒤 정확도는 이 표에서 따로 봅니다.", ""]
    refc, fps = s.get("ref_check") or {}, s.get("by_fp") or {}
    if refc or fps:
        L += ["## 근거 대조 · 판 지문 (현업 문의 · 2026-10-16~ 그림자 기록 — 답을 막지 않습니다)", ""]
        if refc:
            known = [k for k, _ in REF_CHECK_KO]
            parts = ["%s %d" % (ko, refc[k]) for k, ko in REF_CHECK_KO if refc.get(k)]
            parts += ["%s %d" % (k, v) for k, v in sorted(refc.items()) if k not in known]
            L += ["ref 대조 (트리니티봇이 근거로 낸 아카이브 앵커가 실재하고 그 호출에서 실제로 열어 봤나) — " + " · ".join(parts), ""]
        if fps:
            L += ["| 판 지문 (프롬프트·규칙·아카이브) | 답한 건 | 판정 완료 | 맞음 | 보완 | 틀림 | 정확도 |", "|---|---|---|---|---|---|---|"]
            for k, c in sorted(fps.items(), key=lambda kv: -kv[1]["answered"]):
                L.append("| `%s` | %d | %d | %d | %d | %d | %s |" % (k, c["answered"], c["graded"], c["맞음"], c["보완"], c["틀림"], _acc(c)))
            L += ["", "지문이 다른 줄 = 고정 문장 · 운영규칙 요약 · 아카이브 색인 중 하나가 바뀐 뒤의 답입니다(지문이 없는 옛 답은 뺐습니다).", ""]
    for title, key, skip_if_only in (("문의 유형별", "by_type", None), ("담당 업무별", "by_role", "(미기재)")):
        d = s.get(key) or {}
        if not d or (skip_if_only and set(d) == {skip_if_only}):
            continue
        L += ["## %s (현업 문의)" % title, "",
              "| 구분 | 답한 건 | 판정 완료 | 맞음 | 보완 | 틀림 | 정확도 |", "|---|---|---|---|---|---|---|"]
        for k, c in sorted(d.items(), key=lambda kv: -kv[1]["answered"]):
            L.append("| %s | %d | %d | %d | %d | %d | %s |" % (k, c["answered"], c["graded"], c["맞음"], c["보완"], c["틀림"], _acc(c)))
        L.append("")
    if s.get("by_week"):
        L += ["## 주별 추이 (현업 문의 · 첫 답 기준)", "",
              "| 주(월요일) | 답한 건 | 판정 완료 | 맞음 | 보완 | 틀림 | 정확도 |", "|---|---|---|---|---|---|---|"]
        for k in sorted(s["by_week"]):
            c = s["by_week"][k]
            L.append("| %s | %d | %d | %d | %d | %d | %s |" % (k, c["answered"], c["graded"], c["맞음"], c["보완"], c["틀림"], _acc(c)))
        L.append("")
    if ops:
        L += ["## 운영 신호 (journal 전체 기간 누적)", "",
              "| 항목 | 값 |", "|---|---|",
              "| Claude 호출 | %d회 · $%.2f (API 요금 환산) |" % (ops["runs"], ops["cost_usd"]),
              "| 호출 실패 | %s |" % (("%d회 — %s" % (ops["fail_total"], " · ".join("%s %d" % (k, v) for k, v in sorted(ops["fail"].items()))))
                                   if ops["fail_total"] else "없음"),
              "| 안전 신호 | 내부 번호 유출 차단 %d · 답 규격 위반 %d · 엔진 전환 %d · 허용 밖 도구 시도 %d · 답 복구 %d |"
              % (ops["card_leak_blocked"], ops["schema_problem"], ops["failover"], ops["denials"], ops["salvaged"]), ""]
    if replay:
        L += ["## 재생 평가 (오프라인 — 완료된 실제 문의로 모델 · 규칙이 바뀔 때마다 다시 잰다)", "",
              "| 실행 | 일시 | 모델 | 검증 사례 점수 | 전체 점수 | 보류 | 답 없음 · 제외 | 치명 감점 |", "|---|---|---|---|---|---|---|---|"]
        for h in replay[-8:]:
            L.append("| `%s` | %s | %s | %s | %s | %s | %s | %s |" % (
                h.get("label", "?"), (h.get("date") or "")[:16], h.get("model") or "?",
                ("%.1f / 10 (%d건)" % (h["mean_verified"], h["n_verified"])) if h.get("n_verified") else "—",
                ("%.1f / 10 (%d건)" % (h["mean_all"], h["n_all"])) if h.get("n_all") else "—",
                ("%d/%d" % (h["n_hold"], h.get("n_all") or 0)) if h.get("n_hold") is not None else "—",
                ("%s · %s" % (h.get("no_answer", "—"), h.get("run_failed", "—"))) if (h.get("no_answer") is not None or h.get("run_failed") is not None) else "—",
                h.get("critical", "—")))
        L += ["", "사례 점수는 옛 벤치에서 답을 보기 전에 고정한 「검증」 사례 기준이 우선입니다 — 초안 기준 사례는 참고용입니다(vacation/bench/README.md). "
                  "「답 없음」은 트리니티봇이 답을 내지 않아 0점으로 평균에 들어간 사례, 「제외」는 실행 실패 · 채점 실패라 점수에서 뺀 사례입니다.", ""]
    wait = [x for x in rows if not x.get("manual") and not x.get("excluded") and (not x["grade"] or x["stale"])]
    if wait:
        L += ["## 판정을 기다리는 답 (%d건)" % len(wait), ""]
        for x in wait:
            L.append("- `%s`%s · %s · 트리니티봇 %s · 첫 답 %s%s · %s"
                     % (x["id"], " (시험)" if x["test"] else "", x["req"] or "?",
                        VERDICT_KO.get(x["verdicts"][-1], x["verdicts"][-1]), x["first"][:16],
                        " · ⚠ 판정(%s) 뒤에 답이 더 달림 — 재판정" % x["graded_at"] if x["stale"] else "", x["q"]))
        L.append("")
    miss = [x for x in rows if x["grade"] in ("보완", "틀림")]
    if miss:
        L += ["## 보완 · 틀림 — 무엇이 달랐나 (트리니티봇을 고칠 재료)", ""]
        if s.get("codes"):
            L += ["이유 코드 집계 — " + " · ".join("%s %d" % (k, v) for k, v in sorted(s["codes"].items(), key=lambda kv: -kv[1])), ""]
        for x in miss:
            L.append("- `%s`%s · **%s**%s · %s" % (x["id"], " (시험)" if x["test"] else "", x["grade"],
                                                  (" [%s]" % ", ".join(x["codes"])) if x.get("codes") else "",
                                                  x["note"] or "(메모 없음 — 무엇이 달랐는지 적어 두면 트리니티봇을 고칠 때 씁니다)"))
        L.append("")
    L += ["## 전체 기록", "",
          "| 문의 | 요청 | 트리니티봇 판정 | 모델 · 출처 | 첫 답 | 담당자 판정 | 판정 시각 | 메모 |", "|---|---|---|---|---|---|---|---|"]
    for x in rows:
        L.append("| `%s`%s | %s | %s | %s | %s | %s | %s | %s |"
                 % (x["id"], " (시험)" if x["test"] else "", x["req"],
                    " → ".join(VERDICT_KO.get(v, v) for v in x["verdicts"]),
                    ((x.get("model") or "?") + (" · 수동" if x.get("manual") else "")), x["first"][:16],
                    ((EXCLUDE_GRADE if x.get("excluded") else x["grade"]) + (" ⚠재판정" if x["stale"] else "")) if (x["grade"] or x.get("excluded")) else "판정 전",
                    x["graded_at"], x["note"].replace("|", "/")))
    L.append("")
    return "\n".join(L)


def export_js(rows, s, prog, cfg, ops=None, replay=None):
    """지표 화면(task-dashboard/bot-metrics.html)이 읽는 데이터 — 런타임 폴더(공유본 **밖**)에 쓴다.

    ⛔ 문의 본문 · 요청자 · 답변 글 · 메모는 넣지 않는다 — 문의 번호와 집계 · 판정 · 모델 · 시간뿐이다.
    """
    rt = cfg["runtime_dir"]
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "real": s["real"], "test": s["test"], "manual": s["manual"], "excluded": s.get("excluded", 0),
        "prog": prog, "by_verdict": s["by_verdict"], "by_model": s["by_model"], "by_type": s["by_type"],
        "by_role": s["by_role"], "by_week": s["by_week"], "codes": s["codes"],
        "latency": s["latency"], "latency_by_model": s["latency_by_model"],
        "ops": ops, "replay": replay,
        "reason_codes": (cfg.get("reason_codes") or DEFAULT_REASON_CODES),
        "verdictMatrix": to_js_obj([r for r in rows if not r["test"] and not r.get("manual") and not r.get("excluded") and not r.get("fallback")]),
        "ref_check": s.get("ref_check") or {}, "by_fp": s.get("by_fp") or {},
        "rows": [{"id": x["id"], "type": x["type"], "role": x.get("role") or "", "verdict": x["verdicts"][-1],
                  "expected": x.get("expected") or "",
                  "n_answers": len(x["verdicts"]), "grade": (EXCLUDE_GRADE if x.get("excluded") else x["grade"]), "excluded": bool(x.get("excluded")), "codes": x.get("codes") or [],
                  "model": x.get("model") or "", "source": x.get("source") or "", "manual": bool(x.get("manual")),
                  "fallback": bool(x.get("fallback")), "test": bool(x["test"]), "stale": bool(x["stale"]),
                  "first": x["first"][:16], "sec": x.get("sec"), "cost": x.get("cost_share"),
                  "lat": x.get("lat_sec"), "week": x.get("week") or "",
                  "ref_check": x.get("ref_check") or "", "fp": x.get("fp") or ""} for x in rows],
    }
    os.makedirs(rt, exist_ok=True)
    p = os.path.join(rt, DATA_NAME)
    tmp = p + ".tmp"
    io.open(tmp, "w", encoding="utf-8", newline="").write(
        "window.TRINITYBOT_METRICS = " + json.dumps(data, ensure_ascii=False) + ";\n")
    os.replace(tmp, p)
    return p


def write_md(rows, s, cfg):
    rt = cfg["runtime_dir"]
    os.makedirs(rt, exist_ok=True)
    p = os.path.join(rt, FILE_NAME)
    tmp = p + ".tmp"
    prog = stage_progress(rows, cfg)
    ops = journal_stats(rt)
    replay = replay_history()
    io.open(tmp, "w", encoding="utf-8", newline="").write(render(rows, s, prog, ops, replay))
    os.replace(tmp, p)
    try:                                                              # 지표 화면 데이터 — 실패해도 장부 · 보고서는 나가야 한다
        export_js(rows, s, prog, cfg, ops, replay)
    except Exception:                                                 # noqa: BLE001
        pass
    return p


def main(argv):
    cfg = GATE._cfg()
    rows = build(cfg)
    s = summarize(rows)
    if "--json" in argv:
        print(json.dumps({"real": s["real"], "test": s["test"], "waiting": len(s["waiting"])}, ensure_ascii=False))
        return 0
    p = write_md(rows, s, cfg)
    print(line_summary(s, stage_progress(rows, cfg)))
    print(p)
    if "--open" in argv:
        try:
            os.startfile(p)                                     # noqa: S606 — 담당자 PC 의 자기 파일
        except OSError:
            pass
    if "--page" in argv:                                        # 지표 화면(데이터는 write_md 가 방금 새로 썼다)
        try:
            os.startfile(os.path.join(DASH, PAGE_NAME))        # noqa: S606
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
