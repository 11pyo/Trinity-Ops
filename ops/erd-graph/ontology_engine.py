# -*- coding: utf-8 -*-
"""
ontology_engine.py — SAP SD 테이블·프로그램·함수·T-Code·문서 온톨로지 적재 및 탐색 엔진.

용도:
1. erd-graph/query_ontology.py (CLI 조회)
2. task-dashboard/vacation/prefetch.py (트리니티봇 아카이브 검색 RRF 부스트 + 구조 맥락 주입)
3. task-dashboard/desk/desk-server.py (/api/ontology 엔드포인트)
"""

import io
import json
import os
import re
from collections import deque

DEFAULT_ONTOLOGY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_ontology.json")

_CACHE = {"path": None, "mtime_ns": -1, "size": -1, "data": None}
_SAP_TOKEN_RE = re.compile(r"\b(?:[A-Z][A-Z0-9_/]{2,29})\b")
_LEAK_RE = re.compile(r"\b(?:DEV|OPS)-\d{1,4}\b|\bSR\d{4,6}\b|\bCH\d{4,6}\b")

# 질문 내 한글 업무 키워드 → 온톨로지 핵심 노드 시드 매핑 (질문에 테이블/T-Code가 없을 때도 1~2홉 탐색 가능)
_KW_SEEDS = (
    (("매출품의", "품의", "수주품의", "매출 품의"), ["TABL:ZXX_T0100", "TABL:ZXX_T0110", "TABL:ZXX_T0102", "TABL:VBAK"]),
    (("영업기회", "견적"), ["TABL:ZXX_T0090", "TABL:ZXX_T0100"]),
    (("판매오더", "오더생성", "수주", "확정수량"), ["TCODE:VA01", "TABL:VBAK", "TABL:VBAP", "TABL:VBEP"]),
    (("납품", "출하", "출고", "바코드", "인수증"), ["TCODE:VL01N", "TABL:LIKP", "TABL:LIPS", "TABL:ZXX_T0070", "TABL:ZXX_T0071"]),
    (("세금계산서", "전자세금", "전자세금계산서 중계", "발행"), ["TABL:ZXX_T0011", "TABL:ZXX_T0012", "TABL:ZXX_T0015", "TABL:ZXX_EI_IF", "TABL:VBRK"]),
    (("빌링", "송장", "대금청구", "매출마감", "vf04"), ["TCODE:VF01", "TCODE:VF04", "TABL:VBRK", "TABL:VBRP", "TABL:VKDFS"]),
    (("여신", "신용한도", "특별여신", "여신보류", "여신블록", "한도초과"), ["TABL:ZXX_T0026", "TABL:ZXX_T0020", "TABL:UKMBP_CMS_SGM", "TABL:KNA1"]),
    (("담보", "보증보험", "근저당"), ["TABL:ZXX_T0024", "TABL:ZXX_T0028", "TABL:ZXX_T0026"]),
    (("채권", "미수", "수금", "입금", "전표"), ["TABL:BSID", "TABL:BSAD", "TABL:BKPF", "TABL:BSEG", "TABL:ACDOCA"]),
    (("데모", "bmt", "장비대여", "시연"), ["TABL:ZXX_T0060", "TABL:ZXX_T0063", "TABL:ZXX_T0065"]),
    (("반품", "교환", "승계"), ["TABL:ZXX_T1250", "TABL:ZXX_T1260", "TABL:VBAK"]),
    (("판매계획", "예산", "목표", "수익성"), ["TABL:ZXX_T0240", "TABL:ZXX_T0241", "TABL:ZXX_T0270", "TABL:ZXX_CO0410", "TABL:CE17000"]),
    (("환율", "외화", "단가", "가격"), ["TABL:TCURR", "TABL:TCURX", "TABL:PRCD_ELEMENTS"]),
    (("권한", "메뉴", "담당자", "조직", "영업조직"), ["TABL:ZXX_T1000", "TABL:ZXX_T1110", "TABL:KNA1", "TABL:KNVV"]),
)


def load_ontology(path=None):
    """schema_ontology.json을 stat 캐시 기반으로 고속 적재한다. 파일이 없거나 손상되면 None을 반환."""
    p = path or DEFAULT_ONTOLOGY_PATH
    try:
        st = os.stat(p)
    except OSError:
        return None
    if (_CACHE["path"] == p and _CACHE["mtime_ns"] == st.st_mtime_ns
            and _CACHE["size"] == st.st_size and _CACHE["data"] is not None):
        return _CACHE["data"]
    try:
        with io.open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "nodes" not in data or "edges" not in data:
            return None
        _ensure_index(data)
        _CACHE.update({"path": p, "mtime_ns": st.st_mtime_ns, "size": st.st_size, "data": data})
        return data
    except Exception:
        return None


def _ensure_index(ont):
    if "_ix" in ont:
        return ont["_ix"]
    nodes = ont.get("nodes") or {}
    edges = ont.get("edges") or []
    adj_out = {}
    adj_in = {}
    by_name = {}
    for nid, node in nodes.items():
        name = (node.get("name") or nid.split(":", 1)[-1]).upper()
        by_name.setdefault(name, []).append(nid)
        # 접두사 별칭 (예: VBAK -> TABL:VBAK)
        adj_out.setdefault(nid, [])
        adj_in.setdefault(nid, [])
    for e in edges:
        src = e.get("src")
        dst = e.get("dst")
        if src in nodes and dst in nodes:
            adj_out.setdefault(src, []).append(e)
            adj_in.setdefault(dst, []).append(e)
    # 이름 정렬 우선순위: TABL > TCODE > PROG > FUNC > FUGR > CLAS > ART > DEV
    pri = {"TABL": 0, "TCODE": 1, "PROG": 2, "FUNC": 3, "FUGR": 4, "CLAS": 5, "ART": 6, "DEV": 7}
    for name, nids in by_name.items():
        nids.sort(key=lambda x: (pri.get(nodes[x].get("type", ""), 9), x))
    ix = {"adj_out": adj_out, "adj_in": adj_in, "by_name": by_name}
    ont["_ix"] = ix
    return ix


def resolve_nodes(ont, query, limit=12):
    """질문·코드·키워드 문자열에서 일치하는 온톨로지 노드 ID 목록을 찾는다."""
    if not ont:
        return []
    ix = _ensure_index(ont)
    nodes = ont["nodes"]
    found = []
    seen = set()

    def add_nid(nid, exact=False):
        if nid in nodes and nid not in seen:
            seen.add(nid)
            found.append((0 if exact else 1, nid))

    q_raw = (query or "").strip()
    q_up = q_raw.upper()
    q_low = q_raw.lower()

    # 1) 직접 노드 ID 입력 (예: TABL:ZXX_T0100)
    if q_up in nodes:
        add_nid(q_up, exact=True)

    # 2) SAP 토큰 추출 (ZXX_T0100, VA01, ZXX_R0040, VBAK 등)
    for tok in _SAP_TOKEN_RE.findall(q_up):
        for nid in ix["by_name"].get(tok, ()):
            add_nid(nid, exact=True)
        # ZXX_R0040 vs ZXX_T0040 유사 번호가 아니라 정확히 같은 이름 우선, 숫자 4자리 부분일치 보조
        if tok.isdigit() and len(tok) == 4:
            for name, nids in ix["by_name"].items():
                if tok in name and name.startswith(("ZXX_T", "ZXX_R", "ZXX_CO")):
                    for nid in nids[:2]:
                        add_nid(nid, exact=False)

    # 3) 한글 업무 키워드 시드
    for kws, seed_ids in _KW_SEEDS:
        if any(kw in q_low for kw in kws):
            for sid in seed_ids:
                add_nid(sid, exact=False)

    # 4) 토큰이 없거나 적으면 노드 desc/name 부분 문자열 탐색
    if len(found) < 3 and len(q_raw) >= 2:
        terms = [t for t in re.split(r"[\s,/()]+", q_low) if len(t) >= 2]
        scored = []
        for nid, node in nodes.items():
            if nid in seen or node.get("type") == "DEV":
                continue
            hay = (node.get("name", "") + " " + node.get("desc", "")).lower()
            hits = sum(1 for t in terms if t in hay)
            if hits:
                core_b = 1 if node.get("core") else 0
                scored.append((hits, core_b, nid))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        for _, _, nid in scored[:limit]:
            add_nid(nid, exact=False)

    found.sort(key=lambda x: (x[0], 0 if nodes[x[1]].get("core") else 1, x[1]))
    return [nid for _, nid in found[:limit]]


def query_entity(ont, token_or_kw, hops=2, neighbor_cap=60):
    """단일 엔티티 또는 키워드 기준 1~N홉 온톨로지 이웃 서브그래프를 반환한다."""
    if not ont:
        return None
    ix = _ensure_index(ont)
    nodes = ont["nodes"]
    seeds = resolve_nodes(ont, token_or_kw, limit=6)
    if not seeds:
        return {"query": token_or_kw, "seeds": [], "primary": None, "neighbors": [], "edges": []}

    primary = nodes[seeds[0]]
    visited = {seeds[0]: 0}
    queue = deque([(seeds[0], 0)])
    # 다수 seed가 있으면 1번 이하만 보조 시드 포함
    for s in seeds[1:3]:
        if s not in visited:
            visited[s] = 0
            queue.append((s, 0))

    collected_edges = []
    seen_edges = set()

    def _edge_pri(e):
        rel = e.get("rel", "")
        dst_n = nodes.get(e.get("dst", ""), {})
        src_n = nodes.get(e.get("src", ""), {})
        rel_w = {
            "TABLE_REL": 0, "RUNS_PROG": 1, "DOCUMENTED_IN": 2,
            "CALLS_FUNC": 3, "BELONGS_TO_FUGR": 4, "READS_WRITES_TABLE": 5,
            "MODIFIED_BY_DEV": 6, "CALLS_PROG": 7,
        }.get(rel, 9)
        core_w = 0 if (dst_n.get("core") or src_n.get("core")) else 1
        return (rel_w, core_w, e.get("src", ""), e.get("dst", ""))

    while queue and len(visited) < neighbor_cap:
        cur, d = queue.popleft()
        if d >= hops:
            continue
        incident = ix["adj_out"].get(cur, []) + ix["adj_in"].get(cur, [])
        incident = sorted(incident, key=_edge_pri)
        per_node_cap = 28 if d == 0 else 12
        taken = 0
        for e in incident:
            other = e["dst"] if e["src"] == cur else e["src"]
            ek = (e["src"], e["rel"], e["dst"])
            if ek not in seen_edges:
                seen_edges.add(ek)
                collected_edges.append(e)
            if other not in visited and len(visited) < neighbor_cap:
                visited[other] = d + 1
                queue.append((other, d + 1))
                taken += 1
                if taken >= per_node_cap:
                    break

    by_type = {"TABL": [], "PROG": [], "FUNC": [], "FUGR": [], "TCODE": [], "ART": [], "DEV": []}
    for nid, dist in visited.items():
        n = nodes[nid]
        tp = n.get("type", "")
        entry = {
            "id": nid,
            "type": tp,
            "name": n.get("name", ""),
            "desc": n.get("desc", ""),
            "domain": n.get("domain", ""),
            "dist": dist,
            "core": bool(n.get("core")),
        }
        if tp == "TABL":
            entry["pk"] = n.get("pk", [])
        by_type.setdefault(tp, []).append(entry)
    for lst in by_type.values():
        lst.sort(key=lambda x: (x["dist"], 0 if x["core"] else 1, x["name"]))

    return {
        "query": token_or_kw,
        "seeds": [nodes[s] for s in seeds],
        "primary": primary,
        "by_type": by_type,
        "edges": collected_edges[:120],
    }


def expand_for_query(ont, text, max_articles=6, max_lines=3):
    """트리니티봇 prefetch.py 전용: 질문에서 1~2홉 온톨로지 확장을 수행해
    (1) DOCUMENTED_IN 아카이브 문서 가중치, (2) 연관 SAP 토큰, (3) 요약 맥락 줄을 반환한다.
    내부 DEV/OPS/SR/CH 식별자는 맥락 줄에서 자동 제거한다."""
    if not ont or not text:
        return {"matched_nodes": [], "article_boosts": {}, "expanded_terms": [], "context_lines": []}
    ix = _ensure_index(ont)
    nodes = ont["nodes"]
    seeds = resolve_nodes(ont, text, limit=4)
    if not seeds:
        return {"matched_nodes": [], "article_boosts": {}, "expanded_terms": [], "context_lines": []}

    article_boosts = {}
    table_rels = []
    prog_links = []
    expanded_terms = []
    seen_terms = set()

    def _add_term(t):
        t = (t or "").strip()
        if t and t not in seen_terms:
            seen_terms.add(t)
            expanded_terms.append(t)

    hop1_tables = []
    for s in seeds:
        sn = nodes[s]
        _add_term(sn.get("name"))
        inc = ix["adj_out"].get(s, []) + ix["adj_in"].get(s, [])
        for e in inc:
            rel = e["rel"]
            other_id = e["dst"] if e["src"] == s else e["src"]
            other = nodes.get(other_id)
            if not other:
                continue
            if rel == "DOCUMENTED_IN" and other.get("type") == "ART":
                aid = other.get("article_id") or other.get("name")
                article_boosts[aid] = max(article_boosts.get(aid, 0.0), 1.0)
            elif rel == "TABLE_REL" and other.get("type") == "TABL":
                _add_term(other.get("name"))
                hop1_tables.append(other_id)
                arrow = "→" if e["src"] == s else "←"
                j = e.get("join") or ""
                j_txt = f" (키: {j})" if j else ""
                table_rels.append(f"{sn['name']} {arrow} {other['name']}({other.get('desc','')[:18]}){j_txt}")
            elif rel in ("RUNS_PROG", "READS_WRITES_TABLE", "CALLS_FUNC"):
                _add_term(other.get("name"))
                if len(prog_links) < 6:
                    prog_links.append(f"{sn['name']}─{other['name']}({other.get('desc','')[:16]})")

    # 2홉: 인접 테이블이 연결된 아카이브 문서도 약한 부스트(0.55)
    for t_id in hop1_tables[:6]:
        for e in ix["adj_out"].get(t_id, []):
            if e["rel"] == "DOCUMENTED_IN":
                other = nodes.get(e["dst"])
                if other and other.get("type") == "ART":
                    aid = other.get("article_id") or other.get("name")
                    article_boosts[aid] = max(article_boosts.get(aid, 0.0), 0.55)

    # 문서 부스트 상위 max_articles 개로 정리
    top_arts = sorted(article_boosts.items(), key=lambda x: (-x[1], x[0]))[:max_articles]
    article_boosts = dict(top_arts)

    lines = []
    if table_rels:
        dedup_rels = list(dict.fromkeys(table_rels))[:3]
        lines.append("[테이블 관계] " + " | ".join(dedup_rels))
    if prog_links:
        dedup_progs = list(dict.fromkeys(prog_links))[:4]
        lines.append("[프로그램·T-Code·함수] " + ", ".join(dedup_progs))
    if seeds:
        desc_parts = [f"{nodes[s]['name']}({nodes[s].get('desc','')[:24]})" for s in seeds[:3] if nodes[s].get("desc")]
        if desc_parts:
            lines.append("[핵심 오브젝트] " + " · ".join(desc_parts))

    safe_lines = [_LEAK_RE.sub("[카드]", ln) for ln in lines[:max_lines]]
    return {
        "matched_nodes": [nodes[s]["name"] for s in seeds],
        "article_boosts": article_boosts,
        "expanded_terms": expanded_terms[:16],
        "context_lines": safe_lines,
    }
