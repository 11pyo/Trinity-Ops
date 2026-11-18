# -*- coding: utf-8 -*-
"""
query_ontology.py — AI · 운영담당자용 SAP SD ERD/온톨로지 고속 조회 CLI.

사용 예시:
  python erd-graph/query_ontology.py ZXX_T0100
  python erd-graph/query_ontology.py VA01 --hops 2
  python erd-graph/query_ontology.py 매출품의 --json
"""

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ontology_engine as OE  # noqa: E402


def format_text(res: dict) -> str:
    p = res.get("primary")
    if not p:
        return f"[검색 결과 없음] '{res.get('query', '')}' 에 일치하는 SAP 오브젝트/키워드를 찾지 못했습니다."
    lines = [
        f"=== [{p['type']}] {p['name']} — {p.get('desc') or '설명 없음'} (도메인: {p.get('domain', '-')}) ===",
    ]
    if p["type"] == "PROC" and p.get("flow"):
        lines.append("  단계별 업무 흐름:")
        for st in p["flow"]:
            tcs = ", ".join(st.get("tcodes") or []) or "-"
            tbs = ", ".join(st.get("tables") or []) or "-"
            lines.append(f"    {st.get('step')}. [{st.get('role','')}] {st.get('label','')} (T-Code: {tcs} | 테이블: {tbs})")
    if p["type"] == "TABL" and p.get("fields"):
        pks = [f["field"] for f in p["fields"] if f.get("pk")]
        lines.append("  PK 필드: " + (", ".join(pks) if pks else "-"))
        sample_cols = [f"{f['field']}({f.get('label') or f.get('datatype','')})" for f in p["fields"][:10]]
        lines.append("  주요 컬럼: " + ", ".join(sample_cols))

    bt = res.get("by_type") or {}
    for label, key in (
        ("연관 프로세스", "PROC"),
        ("연관 테이블 (ERD)", "TABL"),
        ("실행 T-Code", "TCODE"),
        ("연관 프로그램", "PROG"),
        ("호출 함수 / 함수그룹", "FUNC"),
        ("소속 FUGR", "FUGR"),
        ("아카이브 문서", "ART"),
        ("관련 DEV 카드", "DEV"),
    ):
        items = [x for x in bt.get(key, []) if x["id"] != p["id"]]
        if not items:
            continue
        rendered = ", ".join(
            f"{x['name']}({x['desc'][:22]})" if x.get("desc") else x["name"]
            for x in items[:15]
        )
        more = f" 외 {len(items) - 15}건" if len(items) > 15 else ""
        lines.append(f"  {label} [{len(items)}]: {rendered}{more}")

    rel_lines = []
    for e in res.get("edges", []):
        if e["rel"] == "TABLE_REL" and (e["src"] == p["id"] or e["dst"] == p["id"]):
            j = f" [키: {e['join']}]" if e.get("join") else ""
            lbl = f" — {e['label']}" if e.get("label") else ""
            rel_lines.append(f"    {e['src'].split(':',1)[-1]} -> {e['dst'].split(':',1)[-1]}{j}{lbl}")
    if rel_lines:
        lines.append("  핵심 조인 경로:")
        lines.extend(rel_lines[:15])
    return "\n".join(lines)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Query SAP SD ERD/Ontology graph")
    ap.add_argument("query", nargs="+", help="Table, Program, Function, T-Code, or Korean keyword")
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--json", action="store_true", help="Output JSON")
    ap.add_argument("--ontology", default=OE.DEFAULT_ONTOLOGY_PATH)
    args = ap.parse_args(argv[1:])

    ont = OE.load_ontology(args.ontology)
    if not ont:
        print(f"[X] 온톨로지 파일이 없습니다: {args.ontology}. 먼저 sync_erd_ontology.py 를 실행하십시오.", file=sys.stderr)
        return 1

    q = " ".join(args.query)
    res = OE.query_entity(ont, q, hops=max(1, min(3, args.hops)))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(format_text(res))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
