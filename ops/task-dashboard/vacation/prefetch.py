# -*- coding: utf-8 -*-
"""
prefetch.py — 트리니티봇 선행 검색 (2026-10-07 신설 · 사용자 승인 「선행 검색 주입 + 3번 반복 측정」)

왜: 재생 평가 반복 측정(26건 × 6번)에서 **같은 질문이 9점이었다가 1점**이 되는 편차가 가장 큰 약점으로 드러났다. 한 원인은 봇의 **검색 운**이다 —
    같은 질문에서 한 번은 여신 아카이브 절을 찾아 답하고, 다른 번에는 「아카이브에 동일 오류 없음」이라고 했다. 베타 첫 문의도 봇이 아카이브 한 절의
    30줄만 읽고 선례는 `grep -o` 조각만 봤다. 검색을 모델에게 맡기지 않고 **코드가 먼저 결정적으로** 하고, 결과(아카이브 후보 절 발췌 · 사람이 답해 끝낸 선례)를
    프롬프트에 넣어 준다. 모델은 그것을 출발점으로 확인·보완한다.
무엇을: ① 아카이브 아티클을 ~700자 창으로 나눠 BM25(한글 2·3글자 조각 + 영숫자 코드 가중)로 검색 → 후보 아티클과 가장 맞는 창의 발췌
        ② 문의 이력에서 **사람이 답해 완료한 건**(트리니티봇 답·시험 문의·자기 자신 제외)을 같은 방식으로 검색 → 질문·답 요약
        ③ 선례에 달린 아카이브 앵커(`ref`)는 아카이브 후보의 가산점으로 쓴다.
원칙: 참고용이다 — 틀릴 수 있다고 프롬프트에 적는다. 실패해도 봇 호출을 막지 않는다(빈 문자열). 파일을 쓰지 않는다(캐시 없음 — 매 호출 메모리에서 만든다).
"""
import collections
import html
import io
import json
import math
import os
import re
import sys

_ARCHIVE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ERD_DIR = os.path.join(_ARCHIVE_ROOT, "erd-graph")
try:
    if _ERD_DIR not in sys.path:
        sys.path.insert(0, _ERD_DIR)
    import ontology_engine as _OE
except Exception:
    _OE = None


def expand_ontology_safe(query):
    """온톨로지(schema_ontology.json)가 있으면 1~2홉 확장을 수행하고, 없으면 빈 딕셔너리를 반환한다."""
    if _OE is None or not query:
        return {"matched_nodes": [], "article_boosts": {}, "expanded_terms": [], "context_lines": []}
    try:
        ont = _OE.load_ontology()
        if not ont:
            return {"matched_nodes": [], "article_boosts": {}, "expanded_terms": [], "context_lines": []}
        return _OE.expand_for_query(ont, query, max_articles=6, max_lines=3)
    except Exception:
        return {"matched_nodes": [], "article_boosts": {}, "expanded_terms": [], "context_lines": []}

WIN_CHARS = 700
WIN_STEP_LINES = 4
K1, B = 1.2, 0.75
ART_TOP, PREC_TOP = 4, 2
# 선례 후보는 점수 하한을 넘을 때만(BM25 는 긴 질문+답 문서라 점수 변별이 약하다 — 오프라인 측정: 상위 3개 중 정답 앵커를 공유하는 선례 23%,
# 하한 30 이면 남는 후보의 36%). 잡음 선례가 모델을 엉뚱한 곳으로 이끄는 것이 더 나쁘므로 확실할 때만 곁들인다.
PREC_MIN = 30.0
# 선례가 가리킨 아카이브 앵커를 후보 순위에 가산하는 가중. 0 = 끔 — 오프라인 측정(정답 앵커 재현율, 문의 104건)에서 가산이 오히려
# @1 74%→55% 로 순위를 흩뜨렸다(선례의 앵커는 그 선례의 주제일 뿐 이 질문의 주제가 아니다). 선례는 후보로 따로 보여 준다.
PREC_ANCHOR_W = 0.0
EXCERPT_CHARS = 650
CODE_W = 3.0                    # 영숫자 코드(ZXX_R0700·VF04 등)는 희귀하고 강한 신호
HEAD_W = 1.6                    # 제목·태그 창 가중
REF_BONUS = 0.35                # 선례가 가리킨 아티클 가산(상위 선례 점수에 비례)
BOT_HEADS = ("[트리니티봇", "[담당자 부재중")
LEAK_RX = re.compile(r"\b(?:DEV|OPS)-\d{1,4}\b|\bSR\d{4,6}\b|\bCH\d{4,6}\b")

_CODE_RX = re.compile(r"(?a)\b[A-Za-z][A-Za-z0-9_]*[0-9][A-Za-z0-9_]*\b")
_WORD_RX = re.compile(r"(?a)\b[A-Z][A-Z_]{3,}\b")
_HANGUL_RX = re.compile(r"[가-힣]+")


# ── 큐레이션 키워드 순위 — `archive-structure.md` 표(아티클마다 사람·AI 가 고른 키워드)를 질문과 맞춰 본다.
#    `.claude/hooks/prompt_context.py`(사람 세션용 선행 검색 훅)의 방식·불용어를 그대로 옮겼다(훅 파일에 의존하지 않게 복사 — 봇은 이 폴더만 본다).
#    오프라인 측정(정답 앵커 재현율 @3): 이 방식 93% · BM25 만 87% · 둘을 결합(RRF)한 쪽이 @5 에서 가장 높다.
KO_STOP = {"담당자", "프로그램", "매니저", "사용자", "확인서", "요청서", "개발계", "운영계",
           "테스트", "데이터", "화면명", "처리내용", "진행현황", "조회화면", "대시보드", "시스템",
           "변경", "조회", "처리", "관리", "요청", "문의", "확인", "운영", "개발", "서버", "화면",
           "오류", "방법", "기준", "추가", "수정", "삭제", "등록", "생성", "작업", "업무", "정리",
           "이력", "내용", "결과", "대상", "상태", "담당", "전체", "기본", "일반", "현재", "신규",
           "기존", "자동", "수동", "필요", "가능", "불가", "여부", "구조", "설정", "코드", "표시",
           "항목", "입력", "출력", "파일", "문서", "보고", "월례", "정기", "주간", "사용", "이상",
           "관련", "이번", "그대로", "아니면", "그리고", "그런데", "하는데", "진행", "완료", "대기", "접수", "금액", "일자", "번호", "고객", "회사", "기능"}
ASCII_STOP = {"HTTP", "HTTPS", "HTML", "JSON", "TODO", "NULL", "TRUE", "FALSE", "CLAUDE"}
KO_FUNC = {"이건", "그건", "저건", "이거", "그거", "언제", "어디", "무엇", "누가", "어떻게", "있는", "없는", "있다", "없다",
           "있음", "없음", "에서", "에게", "부터", "까지", "자체", "자체가", "달라", "해줘", "하는", "했다", "된다", "되는",
           "이번", "다시", "이후", "이전", "없이", "통해", "위해", "대한", "대해", "같은", "같이", "모든", "아님", "아닌", "때문"}
FUNC_SUFFIX = ("에서", "에게", "으로", "부터", "까지", "하는", "되는", "있는", "없는", "해서", "하면", "하고", "했다", "한다", "된다", "이라", "라고")
PARTICLE = ("가", "는", "를", "을", "은", "이", "에", "의", "도", "로")
MAX_TOKENS = 25
RRF_K = 60


def _nospace(s):
    return re.sub(r"\s+", "", s or "")


def struct_rows(text):
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"^\|\s*\d+\s*\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$", line)
        if m:
            rows.append({"id": m.group(1), "title": m.group(2), "kw": m.group(3)})
    return rows


def _grammar_ok(w, vocab):
    if w in KO_FUNC:
        return False
    if len(w) >= 3 and w.endswith(FUNC_SUFFIX):
        return False
    if len(w) >= 3 and w.endswith(PARTICLE) and w[:-1] in vocab:
        return False
    return True


def curated_vocab(srows):
    vocab = set()
    for r in srows:
        for w in re.split(r"[,，·/()\s]+", r["kw"] + " " + r["title"]):
            w = w.strip("`*#:.'\"")
            if len(w) >= 2 and re.search(r"[가-힣]", w) and w not in KO_STOP:
                vocab.add(w)
    return {w for w in vocab if _grammar_ok(w, vocab)}


def _ascii_toks(prompt):
    found = []
    for p in (r"(?a)\b[A-Za-z][A-Za-z0-9_]*[0-9][A-Za-z0-9_]*\b", r"(?a)\b[A-Z][A-Z_]{3,}\b"):
        for m in re.findall(p, prompt or ""):
            t = m.strip()
            if len(t) < 3 or t.upper() in ASCII_STOP or t.lower() in (x.lower() for x in found):
                continue
            if re.fullmatch(r"[0-9-]+", t) or re.fullmatch(r"[0-9a-f]{12,}", t):
                continue
            found.append(t)
    return found[:MAX_TOKENS]


def curated_rank(query, srows, vocab):
    """큐레이션 키워드 순위 — 영숫자 코드 일치 ×2 + 사전 한글 용어 일치 ×1, 점수 2 이상만. [아티클 id …] (점수 높은 순)."""
    atoks = _ascii_toks(query)
    pns = _nospace(query)
    hits = sorted((w for w in vocab if _nospace(w) in pns), key=len, reverse=True)
    kt = []
    for w in hits:
        if not any(_nospace(w) in _nospace(k) for k in kt):
            kt.append(w)
    kt = kt[:MAX_TOKENS]
    scored = []
    for r in srows:
        text = r["id"] + " " + r["title"] + " " + r["kw"]
        low, ns = text.lower(), _nospace(text)
        s = 2 * sum(1 for t in atoks if t.lower() in low) + sum(1 for t in kt if _nospace(t) in ns)
        if s >= 2:
            scored.append((s, r["id"]))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, i in scored:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def tokens(text):
    """(토큰 → 가중) — 영숫자 코드 + 한글 2·3글자 조각."""
    t = text or ""
    out = collections.Counter()
    for m in _CODE_RX.findall(t):
        if len(m) >= 4 and not re.fullmatch(r"[0-9a-fA-F]{12,}", m):
            out["c:" + m.upper()] += 1
    for m in _WORD_RX.findall(t):
        out["c:" + m.upper()] += 1
    for run in _HANGUL_RX.findall(t):
        n = len(run)
        for i in range(n - 1):
            out[run[i:i + 2]] += 1
        for i in range(n - 2):
            out[run[i:i + 3]] += 1
    return out


def clean_html(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<(br|/tr|/p|/li|/div|/ol|/ul|/h\d|/table|/td|/th)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    return "\n".join(x.strip() for x in s.split("\n") if x.strip())


def load_articles(archive_html):
    try:
        raw = io.open(archive_html, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    arts = []
    for m in re.finditer(r'(?is)<article\b[^>]*\bid="([^"]+)"([^>]*)>(.*?)</article>', raw):
        aid, attrs, body = m.group(1), m.group(2), m.group(3)
        tags = re.search(r'data-tags="([^"]*)"', attrs)
        tm = re.search(r'(?is)<div class="at">(.*?)</div>', body)
        arts.append({"id": aid, "title": clean_html(tm.group(1)) if tm else aid,
                     "tags": html.unescape(tags.group(1)) if tags else "", "text": clean_html(body)})
    return arts


class Index(object):
    """창(문서 조각) 단위 BM25. kind='art' 는 (아티클 id, 창 텍스트), kind='prec' 은 (문의 id, 요약)."""

    def __init__(self):
        self.docs = []              # (owner, text, weight)
        self.tf = []                # 문서별 Counter
        self.inv = collections.defaultdict(list)
        self.dl = []
        self.avg = 1.0

    def add(self, owner, text, weight=1.0):
        c = tokens(text)
        k = len(self.docs)
        self.docs.append((owner, text, weight))
        self.tf.append(c)
        self.dl.append(sum(c.values()) or 1)
        for t, f in c.items():
            self.inv[t].append((k, f))

    def finish(self):
        self.avg = (sum(self.dl) / len(self.dl)) if self.dl else 1.0
        self.n = len(self.docs)

    def search(self, query, limit=None):
        q = tokens(query)
        scores = collections.defaultdict(float)
        for t in q:
            plist = self.inv.get(t)
            if not plist:
                continue
            df = len(plist)
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            w = CODE_W if t.startswith("c:") else 1.0
            for k, f in plist:
                denom = f + K1 * (1 - B + B * self.dl[k] / self.avg)
                scores[k] += w * idf * (f * (K1 + 1)) / denom
        ranked = sorted(((s * self.docs[k][2], k) for k, s in scores.items()), reverse=True)
        return ranked[:limit] if limit else ranked


def build_article_index(arts):
    ix = Index()
    for a in arts:
        ix.add(a["id"], (a["title"] + " " + a["tags"] + " " + a["id"]), HEAD_W)
        lines = a["text"].split("\n")
        i = 0
        while i < len(lines):
            buf, n = [], i
            while n < len(lines) and sum(len(x) + 1 for x in buf) < WIN_CHARS:
                buf.append(lines[n])
                n += 1
            ix.add(a["id"], "\n".join(buf), 1.0)
            if n >= len(lines):
                break
            i += WIN_STEP_LINES
    ix.finish()
    return ix


def human_precedents(merged, exclude=(), test_reqs=()):
    """사람이 답해 완료한 문의만(트리니티봇 답 · 시험 문의 · 제외 id · 답 없음 제외)."""
    ts = set("".join((x or "").split()) for x in test_reqs)
    out = {}
    for iid, r in merged.items():
        if iid in exclude or (r.get("status") or "") != "완료":
            continue
        a = (r.get("a") or "").strip()
        if not a or a.startswith(BOT_HEADS) or "시험 문의" in a[:80]:
            continue
        if "".join((r.get("req") or "").split()) in ts:
            continue
        out[iid] = r
    return out


def build_prec_index(prec):
    ix = Index()
    for iid, r in prec.items():
        ix.add(iid, (r.get("q") or "") + " " + (r.get("a") or ""), 1.0)
    ix.finish()
    return ix


def _anchors(ref):
    return [x.lstrip("#") for x in re.findall(r"#[A-Za-z0-9_\-]+", ref or "")]


def search_all(query, art_ix, prec_ix, prec, arts_by_id, art_top=ART_TOP, prec_top=PREC_TOP, curated=None, ont_boosts=None):
    """(아티클 후보 [(id, 점수, 최적 창 텍스트)], 선례 후보 [(id, 점수)]).

    순위 = BM25(창 최고점 + 0.3×차점) + 큐레이션 키워드 순위(curated) + 온톨로지 DOCUMENTED_IN 부스트 RRF 결합."""
    ar = art_ix.search(query)
    best = {}
    body = {}                                   # 발췌는 본문 창에서만 고른다(제목·태그 창은 순위에는 쓰되 읽을 내용이 없다)
    for s, k in ar:
        owner, text, w = art_ix.docs[k]
        cur = best.get(owner)
        if cur is None:
            best[owner] = [s, 0.0, text]
        elif s > cur[0]:
            best[owner] = [s, cur[0], text]
        elif s > cur[1]:
            cur[1] = s
        if w != HEAD_W and (owner not in body or s > body[owner][0]):
            body[owner] = (s, text)
    for o, v in best.items():
        v[2] = body[o][1] if o in body else arts_by_id[o]["text"][:EXCERPT_CHARS]
    bm = {o: v[0] + 0.3 * v[1] for o, v in best.items()}
    bm_order = [o for o, _ in sorted(bm.items(), key=lambda x: -x[1])]
    score = {}
    for i, o in enumerate(bm_order, 1):
        score[o] = score.get(o, 0.0) + 1.0 / (RRF_K + i)
    if curated:
        for i, o in enumerate(curated_rank(query, curated[0], curated[1]), 1):
            if o in arts_by_id:
                score[o] = score.get(o, 0.0) + 1.0 / (RRF_K + i)
                best.setdefault(o, [0.0, 0.0, arts_by_id[o]["text"][:EXCERPT_CHARS]])
    boosts = ont_boosts if ont_boosts is not None else expand_ontology_safe(query).get("article_boosts")
    if boosts:
        for i, (o, bw) in enumerate(sorted(boosts.items(), key=lambda x: (-x[1], x[0])), 1):
            if o in arts_by_id and bw > 0:
                score[o] = score.get(o, 0.0) + (1.2 / (RRF_K + i)) * bw
                best.setdefault(o, [0.0, 0.0, arts_by_id[o]["text"][:EXCERPT_CHARS]])
    pr = [x for x in prec_ix.search(query)[:prec_top] if x[0] >= PREC_MIN] if prec_ix and prec_ix.n else []
    for n, (s, k) in enumerate(pr):
        for aid in _anchors(prec[prec_ix.docs[k][0]].get("ref")):
            if aid in arts_by_id:
                if PREC_ANCHOR_W:
                    score[aid] = score.get(aid, 0.0) + (1.0 / (RRF_K + 2)) * PREC_ANCHOR_W * (1.0 if n == 0 else 0.5)
                best.setdefault(aid, [0.0, 0.0, arts_by_id[aid]["text"][:EXCERPT_CHARS]])
    arts = sorted(score.items(), key=lambda x: -x[1])[:art_top]
    return ([(o, s, best[o][2]) for o, s in arts], [(prec_ix.docs[k][0], s) for s, k in pr])


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _a_text(r):
    a = (r.get("a") or "")
    a = re.sub(r"──── 추가 답변 \([^)]*\) ────", " ", a)
    a = re.sub(r"⏱\d{4}-\d{2}-\d{2} \d{2}:\d{2}", " ", a)
    return a


def _q_text(r):
    return re.sub(r"\[\[사진:\d+\]\]", " ", r.get("q") or "")


def section(batch, merged, archive_html, cfg=None):
    """프롬프트에 넣을 「자동 선행 검색 결과」 절. 문의마다 아카이브 후보 3 · 선례 후보 3. 실패하면 빈 문자열."""
    try:
        arts = load_articles(archive_html)
        if not arts:
            return ""
        by_id = {a["id"]: a for a in arts}
        art_ix = build_article_index(arts)
        try:
            stext = io.open(os.path.join(os.path.dirname(archive_html), "archive-structure.md"), encoding="utf-8", errors="replace").read()
        except OSError:
            stext = ""
        srows = struct_rows(stext)
        curated = (srows, curated_vocab(srows)) if srows else None
        prec = human_precedents(merged, exclude=set(batch), test_reqs=((cfg or {}).get("test_reqs") or []))
        prec_ix = build_prec_index(prec)
        lines = ["## 자동 선행 검색 결과 (문의마다 · 코드가 먼저 찾은 것)",
                 "아래는 문의 글로 **기계적으로** 찾은 후보입니다. 틀릴 수 있으니 참고용으로만 쓰십시오 — 맞아 보이면 그 아티클의 해당 절 **전체**를 읽어 확인하고 답하고, "
                 "후보가 문의와 어긋나면 무시하고 직접 검색하십시오. 후보가 맞는데 「아카이브에 없다」고 답하면 안 됩니다.",
                 "선례는 **사람이 답해 끝낸 건**만 보여 줍니다. 선례 번호·요청자는 public 에 쓰지 마십시오.", ""]
        for iid in batch:
            r = merged.get(iid) or {}
            q = _q_text(r)
            if len(q.strip()) < 6:
                continue
            ont_exp = expand_ontology_safe(q)
            ar, pr = search_all(q, art_ix, prec_ix, prec, by_id, curated=curated, ont_boosts=ont_exp.get("article_boosts"))
            lines.append("### %s" % iid)
            if ar:
                lines.append("아카이브 후보:")
                for n, (aid, s, text) in enumerate(ar, 1):
                    lines.append("%d. #%s — %s" % (n, aid, _clip(by_id[aid]["title"], 80)))
                    if n <= 2:
                        lines.append("   발췌: " + _clip(text, EXCERPT_CHARS))
            else:
                lines.append("아카이브 후보: (없음)")
            if ont_exp.get("context_lines"):
                lines.append("온톨로지 구조 맥락:")
                for ol in ont_exp["context_lines"]:
                    lines.append("   " + _clip(ol, 220))
            if pr:
                lines.append("선례 후보(사람이 답해 완료):")
                for n, (pid, s) in enumerate(pr, 1):
                    pr_r = prec[pid]
                    lines.append("%d. %s (%s) 질문: %s → 답: %s" % (n, pid, pr_r.get("date") or "", _clip(_q_text(pr_r), 130), _clip(_a_text(pr_r), 260)))
            lines.append("")
        text = "\n".join(lines).rstrip() + "\n\n"
        return LEAK_RX.sub("[카드]", text)
    except Exception:                                           # noqa: BLE001
        return ""
