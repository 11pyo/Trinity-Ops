# -*- coding: utf-8 -*-
"""
sync_erd_ontology.py — SAP SD ERD · 프로그램 · 함수 · T-Code · 아카이브 · DEV 온톨로지 통합 동기화.

입력 소스:
1. %LOCALAPPDATA%\\erd-extract-data\\sap_dev120\\raw\\ddic_tables.json (DEV 120 DD02/DD03/DD04)
2. %LOCALAPPDATA%\\erd-extract-data\\sap_dev120\\raw\\tstc_tfdir_sd.json (DEV 120 TSTC/TFDIR/ENLFDIR)
3. %LOCALAPPDATA%\\erd-extract-data\\sap_dev120\\raw\\{objects.csv, uses.csv} (ZXX_PKG 등 의존관계)
4. SAP SD AI Indexable Archive/articles/**/*.md (아카이브 지식 및 테이블 설명 보강)
5. SAP SD AI Indexable Archive/task-dashboard/tasks.md (DEV-xxx 변경 이력)
6. erd-graph/manual_overrides.json (신규/변경 테이블·프로그램·관계 증분 upsert 누적)

산출물:
- erd-graph/schema_ontology.json (기계 판독 정본 + sync_hash)
- erd-graph/ERD_CATALOG.md (AI · 사람용 도메인별 Mermaid ERD 및 매트릭스)
- erd-graph/erd-viewer.html (단독 실행 및 /erd 서빙용 인터랙티브 뷰어)
"""

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "erd-extract-data", "sap_dev120", "raw")
OVERRIDE_PATH = os.path.join(HERE, "manual_overrides.json")
OUT_JSON = os.path.join(HERE, "schema_ontology.json")
OUT_MD = os.path.join(HERE, "ERD_CATALOG.md")
OUT_HTML = os.path.join(HERE, "erd-viewer.html")

sys.path.insert(0, HERE)
from viewer_builder import render_viewer_html  # noqa: E402

DOMAINS = {
    "O2C_QUOTE_SO": {"label": "영업기회·견적·매출품의·SO", "color": "#38bdf8", "order": 1},
    "DELIVERY_MM": {"label": "납품·출하·인수증·재고·구매", "color": "#34d399", "order": 2},
    "BILLING_TAX_FI": {"label": "빌링·세금계산서·채권·FI", "color": "#fbbf24", "order": 3},
    "CREDIT_FSCM": {"label": "여신·담보·특수관계인·승인", "color": "#f472b6", "order": 4},
    "DEMO_BMT": {"label": "데모·BMT·장비입출고", "color": "#a78bfa", "order": 5},
    "RETURN_CANCEL": {"label": "반품·취소·승계", "color": "#fb7185", "order": 6},
    "PLAN_CO_CLOSE": {"label": "판매계획·월마감·CO·수익성", "color": "#2dd4bf", "order": 7},
    "ORG_AUTH_IF": {"label": "조직·권한·결재·I/F·마스터", "color": "#94a3b8", "order": 8},
    "OTHER_ZSD": {"label": "기타 ZSD / 참조 오브젝트", "color": "#64748b", "order": 9},
}

CORE_STD_SET = {
    "VBAK", "VBAP", "VBEP", "VBFA", "VBPA", "VBKD",
    "LIKP", "LIPS", "VBRK", "VBRP", "VKDFS",
    "MKPF", "MSEG", "MARA", "MARC", "MARD", "MAKT", "MBEW",
    "KNA1", "KNVV", "LFA1", "ADRC", "J_1BBRANCH",
    "EKKO", "EKPO", "EBAN", "EKBE",
    "BKPF", "BSEG", "BSID", "BSAD", "BSIK", "BSAK", "ACDOCA", "FAGLFLEXT",
    "AUFK", "CE17000", "CE27000", "PRCD_ELEMENTS",
    "UKMBP_CMS", "UKMBP_CMS_SGM",
    "TCURX", "TCURF", "TCURR",
}

CORE_PREFIXES = ("ZXX_T", "ZXX_CO", "ZXX_EI", "ZXX_SB_", "ZXX_SB", "ZXX_SE16N", "ZXX_HR0026", "ZXX_MM")

# 핵심 업무 흐름(테이블 간 큐레이션 ERD 엣지)
CURATED_TABLE_RELS = [
    ("ZXX_T0090", "ZXX_T0100", "ZXX_QUONO / ZXX_OPPNO", "1:N", "영업기회→매출품의"),
    ("ZXX_T0100", "ZXX_T0110", "ZXX_DOCNO / ZXX_QUONO,ZXX_REVNO", "1:N", "매출품의 헤더→품목"),
    ("ZXX_T0100", "ZXX_T0102", "ZXX_DOCNO / ZXX_QUONO,ZXX_REVNO", "1:N", "매출품의 헤더→결재/이력"),
    ("ZXX_T0100", "ZXX_T0150", "ZXX_DOCNO", "1:N", "매출품의→매출계획/인정"),
    ("ZXX_T0100", "ZXX_T0180", "ZXX_DOCNO", "1:N", "매출품의→원가/견적품목"),
    ("ZXX_T0100", "ZXX_T0115", "ZXX_DOCNO", "1:N", "매출품의→추가/첨부"),
    ("ZXX_T0100", "VBAK", "VBELN / ZXX_DOCNO", "1:N", "매출품의→판매오더"),
    ("VBAK", "VBAP", "VBELN", "1:N", "판매오더 헤더→아이템"),
    ("VBAP", "VBEP", "VBELN,POSNR", "1:N", "오더아이템→납품일정선"),
    ("VBAK", "VBPA", "VBELN", "1:N", "판매오더→파트너"),
    ("VBAK", "VBKD", "VBELN", "1:N", "판매오더→영업데이터"),
    ("VBAK", "VBFA", "VBELV,VBELN", "1:N", "문서흐름 선후행"),
    ("VBAK", "PRCD_ELEMENTS", "KNUMV", "1:N", "가격조건 레코드"),
    ("VBAP", "LIPS", "VGBEL=VBELN,VGPOS=POSNR", "1:N", "판매오더→납품품목"),
    ("LIKP", "LIPS", "VBELN", "1:N", "납품 헤더→아이템"),
    ("LIKP", "ZXX_T0070", "VBELN", "1:N", "납품→바코드/출하"),
    ("LIKP", "ZXX_T0071", "VBELN", "1:N", "납품→인수증/출하증"),
    ("LIKP", "MKPF", "XBLNR / VBELN", "1:N", "출하PGI→자재문서"),
    ("MKPF", "MSEG", "MBLNR,MJAHR", "1:N", "자재문서 헤더→아이템"),
    ("LIPS", "VBRP", "VGBEL=VBELN,VGPOS=POSNR", "1:N", "납품→빌링품목"),
    ("VBRK", "VBRP", "VBELN", "1:N", "빌링 헤더→아이템"),
    ("VBAK", "VKDFS", "VBELN", "1:N", "오더 빌링인덱스(VF04)"),
    ("LIKP", "VKDFS", "VBELN", "1:N", "납품 빌링인덱스(VF04)"),
    ("VBRK", "BKPF", "AWKEY=VBELN", "1:1", "빌링→회계전표"),
    ("BKPF", "BSEG", "BUKRS,BELNR,GJAHR", "1:N", "회계전표 헤더→개별항목"),
    ("BKPF", "ACDOCA", "RBUKRS,BELNR,GJAHR", "1:N", "S/4 통합분개장"),
    ("KNA1", "BSID", "KUNNR", "1:N", "고객 미결채권"),
    ("KNA1", "BSAD", "KUNNR", "1:N", "고객 반제채권"),
    ("VBRK", "ZXX_T0011", "VBELN", "1:N", "빌링→세금계산서 헤더"),
    ("ZXX_T0011", "ZXX_T0012", "ZXX_TAXNO", "1:N", "세금계산서 헤더→품목"),
    ("ZXX_T0011", "ZXX_T0013", "ZXX_TAXNO", "1:N", "세금계산서 출력PO품목"),
    ("ZXX_T0011", "ZXX_T0014", "ZXX_TAXNO", "1:N", "세금계산서 재발행사유"),
    ("ZXX_T0011", "ZXX_EI_IF", "CONVERSATION_ID / VBELN", "1:1", "세금계산서→전자세금계산서 중계IF"),
    ("KNA1", "ZXX_T0015", "KUNNR", "1:N", "고객 전자세금계산서 수신/제외"),
    ("KNA1", "ZXX_T0017", "KUNNR", "1:N", "세금계산서 통합발행고객"),
    ("KNA1", "KNVV", "KUNNR", "1:N", "고객마스터→영업영역"),
    ("KNA1", "ADRC", "ADRNR=ADDRNUMBER", "N:1", "고객마스터→주소"),
    ("KNA1", "UKMBP_CMS", "KUNNR=PARTNER", "1:1", "고객→FSCM 여신프로파일"),
    ("UKMBP_CMS", "UKMBP_CMS_SGM", "PARTNER", "1:N", "여신마스터→여신세그먼트한도"),
    ("KNA1", "ZXX_T0026", "KUNNR", "1:N", "고객→여신증액/특별여신 결재"),
    ("ZXX_T0026", "ZXX_HR0026", "PERNR / APPR", "N:1", "여신결재→인사조직"),
    ("KNA1", "ZXX_T0020", "KUNNR", "1:N", "고객→여신관리"),
    ("KNA1", "ZXX_T0021", "KUNNR", "1:N", "고객→담보/여신보조"),
    ("KNA1", "ZXX_T0024", "KUNNR", "1:N", "고객→담보/보증정보"),
    ("KNA1", "ZXX_T0032", "KUNNR", "1:N", "고객→미수채권/여신제외"),
    ("KNA1", "ZXX_T0033", "KUNNR", "1:N", "고객→특수관계인/여신"),
    ("ZXX_T0060", "ZXX_T0063", "ZXX_DEMONO", "1:N", "데모/BMT→진행상세"),
    ("ZXX_T0060", "ZXX_T0065", "ZXX_DEMONO", "1:N", "데모/BMT→장비내역"),
    ("ZXX_T0060", "ZXX_T0068", "ZXX_DEMONO", "1:N", "데모/BMT→반납/이력"),
    ("ZXX_T1250", "ZXX_T1260", "ZXX_REQNO / VBELN", "1:N", "반품/취소 요청→상세"),
    ("ZXX_T1250", "ZXX_T1270", "ZXX_REQNO", "1:N", "반품/승계 이력"),
    ("ZXX_T0240", "ZXX_T0241", "GJAHR,MONAT", "1:N", "판매계획 헤더→세부"),
    ("ZXX_T0240", "ZXX_T0270", "GJAHR,MONAT", "1:N", "판매계획→매출실적집계"),
    ("ZXX_T0240", "ZXX_CO0410", "PSPNR / PRCTR", "N:M", "판매계획→수익성(CO)"),
    ("ZXX_CO0410", "ZXX_CO0422", "GJAHR,PERIO", "1:N", "CO 집계 연계"),
    ("MARA", "MARC", "MATNR", "1:N", "자재기본→플랜트"),
    ("MARA", "MARD", "MATNR", "1:N", "자재기본→저장위치재고"),
    ("MARA", "MAKT", "MATNR", "1:N", "자재기본→내역"),
    ("MARA", "MBEW", "MATNR", "1:N", "자재기본→평가/단가"),
    ("MARA", "VBAP", "MATNR", "1:N", "자재→판매오더품목"),
    ("EKKO", "EKPO", "EBELN", "1:N", "구매오더 헤더→품목"),
    ("EKPO", "EKBE", "EBELN,EBELP", "1:N", "구매오더→입고/송장이력"),
    ("TCURR", "VBAK", "WAERS=FCURR", "N:M", "환율→판매오더통화"),
]

# 문서·선례에 잘 나오는 핵심 T-Code 보충(시스템 TSTCT 외 표준/연계)
CURATED_TCODE_LINKS = {
    "VA01": ["VBAK", "VBAP", "VBEP", "ZXX_T0100"],
    "VA02": ["VBAK", "VBAP", "VBEP"],
    "VA03": ["VBAK", "VBAP", "VBFA"],
    "VA05": ["VBAK", "VBAP"],
    "VL01N": ["LIKP", "LIPS", "VBAP"],
    "VL02N": ["LIKP", "LIPS", "MKPF", "MSEG"],
    "VL03N": ["LIKP", "LIPS"],
    "VL06O": ["LIKP", "LIPS"],
    "VL09": ["LIKP", "MKPF", "MSEG"],
    "VF01": ["VBRK", "VBRP", "BKPF", "BSEG"],
    "VF02": ["VBRK", "VBRP", "BKPF"],
    "VF03": ["VBRK", "VBRP", "BKPF"],
    "VF04": ["VKDFS", "VBAK", "LIKP", "VBRK"],
    "VF11": ["VBRK", "VBRP", "BKPF"],
    "UKM_BP": ["UKMBP_CMS", "UKMBP_CMS_SGM", "KNA1"],
    "BP": ["KNA1", "KNVV", "ADRC", "UKMBP_CMS"],
    "FB03": ["BKPF", "BSEG", "ACDOCA"],
    "FBL5N": ["BSID", "BSAD", "KNA1"],
    "MIGO": ["MKPF", "MSEG", "LIPS", "EKPO"],
    "MB51": ["MKPF", "MSEG"],
    "MB52": ["MARD", "MARA"],
    "MMBE": ["MARD", "MARC", "MARA"],
    "KE24": ["CE17000", "CE27000"],
}

_TABLE_NAME_RE = re.compile(r"\b(?:ZXX_T\d{3,4}[A-Z0-9_]*|ZXX_CO\d{3,4}[A-Z0-9_]*|ZXX_EI_[A-Z0-9_]+|ZXX_SB_[A-Z0-9_]+|ZXX_HR0026)\b")
_PROG_NAME_RE = re.compile(r"\b(?:Z[REIFS]SD\d{3,4}[A-Z0-9_]*|SAPMZXX\d+[A-Z0-9_]*)\b")
_TCODE_RE = re.compile(r"\b(?:VA0[1235]|VL0[12369][NO]?|VF0[1234]|VF11|UKM_BP|FBL5N|FB03|MIGO|MB5[12]|MMBE|ME2[123]N|KE24|SE16N|ZXX_R[0-9A-Z_]{3,}|ZXX_S[0-9A-Z_]{3,})\b")
_DEV_CARD_RE = re.compile(r"^#\s+(DEV-\d+)\s+(.+)$", re.MULTILINE)


def classify_domain(name: str, desc: str = "") -> str:
    n = (name or "").upper()
    d = (desc or "")
    if n in ("ZXX_T0090", "ZXX_T0100", "ZXX_T0102", "ZXX_T0110", "ZXX_T0115", "ZXX_T0150", "ZXX_T0180",
             "VBAK", "VBAP", "VBEP", "VBFA", "VBPA", "VBKD", "PRCD_ELEMENTS") or any(k in d for k in ("매출품의", "영업기회", "견적", "판매오더")):
        return "O2C_QUOTE_SO"
    if n in ("LIKP", "LIPS", "MKPF", "MSEG", "MARA", "MARC", "MARD", "MAKT", "MBEW",
             "EKKO", "EKPO", "EBAN", "EKBE", "ZXX_T0070", "ZXX_T0071") or any(k in d for k in ("납품", "출하", "바코드", "인수증", "자재", "구매", "재고")):
        return "DELIVERY_MM"
    if n.startswith(("ZXX_T001", "ZXX_EI", "ZXX_SB")) or n in ("VBRK", "VBRP", "VKDFS", "BKPF", "BSEG", "BSID", "BSAD", "BSIK", "BSAK", "ACDOCA") or any(k in d for k in ("세금계산서", "빌링", "전자세금계산서 중계", "채권", "회계")):
        return "BILLING_TAX_FI"
    if re.match(r"^ZXX_T00(?:2|3)", n) or n in ("UKMBP_CMS", "UKMBP_CMS_SGM", "ZXX_HR0026") or any(k in d for k in ("여신", "담보", "보증", "특수관계")):
        return "CREDIT_FSCM"
    if re.match(r"^ZXX_T006", n) or any(k in d for k in ("데모", "BMT", "시연")):
        return "DEMO_BMT"
    if re.match(r"^ZXX_T12[5-8]", n) or any(k in d for k in ("반품", "취소", "승계")):
        return "RETURN_CANCEL"
    if n in ("ZXX_T0240", "ZXX_T0241", "ZXX_T0270", "ZXX_T1430", "ZXX_CO0410", "ZXX_CO0422", "CE17000", "CE27000", "FAGLFLEXT", "AUFK") or any(k in d for k in ("판매계획", "예산", "월마감", "수익성")):
        return "PLAN_CO_CLOSE"
    if re.match(r"^ZXX_T1[012]", n) or n in ("KNA1", "KNVV", "LFA1", "ADRC", "J_1BBRANCH", "TCURR", "TCURX", "TCURF") or any(k in d for k in ("권한", "메뉴", "조직", "고객", "거래처", "결재", "인터페이스", "ASPEN", "환율")):
        return "ORG_AUTH_IF"
    return "OTHER_ZSD"


def _is_core_table(tabname: str, tabclass: str = "") -> bool:
    u = tabname.upper()
    if u in CORE_STD_SET:
        return True
    if u.startswith(CORE_PREFIXES) and tabclass in ("TRANSP", "VIEW", ""):
        return True
    return False


def _load_json(path: str, default):
    if not os.path.isfile(path):
        return default
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_articles():
    """articles/**/*.md 에서 문서 노드 및 ZXX_T 설명·참조 테이블을 추출한다."""
    arts = {}
    tab_descs = {}
    tr_re = re.compile(r'<span class="tc-z">\s*([A-Z0-9_]+)\s*</span>\s*</td>\s*<td>(.*?)</td>', re.S | re.I)
    tag_strip = re.compile(r"<[^>]+>")
    for path in glob.glob(os.path.join(ARCHIVE_ROOT, "articles", "**", "*.md"), recursive=True):
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        aid = os.path.splitext(os.path.basename(path))[0]
        title = aid
        cat = ""
        m_fm = re.match(r"^---\s*\n(.*?)\n---", txt, re.S)
        if m_fm:
            fm = m_fm.group(1)
            m_id = re.search(r'^id:\s*"([^"]+)"', fm, re.M)
            if m_id:
                aid = m_id.group(1).strip()
            m_st = re.search(r'^struct_title:\s*"([^"]+)"', fm, re.M)
            m_ti = re.search(r'^title:\s*"([^"]+)"', fm, re.M)
            raw_t = (m_st.group(1) if m_st else (m_ti.group(1) if m_ti else aid))
            title = tag_strip.sub("", raw_t).strip()
            m_cat = re.search(r'^category:\s*"([^"]+)"', fm, re.M)
            if m_cat:
                cat = m_cat.group(1).strip()
        if aid == "zxx-tables":
            for tn, raw_desc in tr_re.findall(txt):
                clean_d = re.sub(r"\s+", " ", tag_strip.sub("", raw_desc)).strip()
                tab_descs[tn.upper()] = clean_d[:120]
        # 본문에서 언급된 SAP 오브젝트 추출 (zxx-tables 전체 사전은 지나치게 넓으므로 개별 가중치 차별화 가능)
        refs = set()
        for t in _TABLE_NAME_RE.findall(txt.upper()):
            refs.add(("TABL", t))
        for st in CORE_STD_SET:
            if re.search(r"\b" + re.escape(st) + r"\b", txt):
                refs.add(("TABL", st))
        for p in _PROG_NAME_RE.findall(txt.upper()):
            refs.add(("PROG", p))
        for tc in _TCODE_RE.findall(txt.upper()):
            refs.add(("TCODE", tc))
        rel_path = os.path.relpath(path, ARCHIVE_ROOT).replace("\\", "/")
        arts[aid] = {
            "id": aid,
            "title": title,
            "cat": cat,
            "path": rel_path,
            "refs": sorted(refs),
        }
    return arts, tab_descs


def _load_dev_cards():
    """task-dashboard/tasks.md 에서 DEV-xxx 카드와 언급된 SAP 오브젝트를 추출한다."""
    p = os.path.join(ARCHIVE_ROOT, "task-dashboard", "tasks.md")
    if not os.path.isfile(p):
        return {}
    with io.open(p, "r", encoding="utf-8", errors="replace") as f:
        txt = f.read()
    devs = {}
    matches = list(_DEV_CARD_RE.finditer(txt))
    for i, m in enumerate(matches):
        did = m.group(1).upper()
        title = m.group(2).strip()[:90]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(txt)
        body = txt[m.start():end]
        refs = set()
        for t in _TABLE_NAME_RE.findall(body.upper()):
            refs.add(("TABL", t))
        for st in CORE_STD_SET:
            if re.search(r"\b" + re.escape(st) + r"\b", body):
                refs.add(("TABL", st))
        for pg in _PROG_NAME_RE.findall(body.upper()):
            refs.add(("PROG", pg))
        for tc in _TCODE_RE.findall(body.upper()):
            refs.add(("TCODE", tc))
        devs[did] = {"id": did, "title": title, "refs": sorted(refs)}
    return devs


def build_ontology() -> dict:
    ddic = _load_json(os.path.join(RAW_DIR, "ddic_tables.json"), {}).get("tables", {})
    tf_raw = _load_json(os.path.join(RAW_DIR, "tstc_tfdir_sd.json"), {})
    overrides = _load_json(OVERRIDE_PATH, {"tables": {}, "programs": {}, "edges": []})
    arts, tab_descs = _load_articles()
    devs = _load_dev_cards()

    nodes = {}
    edges_map = {}  # (src, rel, dst) -> edge dict

    def ensure_node(ntype, name, desc="", domain=None, core=False, extra=None):
        nname = name.strip().upper() if ntype != "ART" else name.strip()
        nid = f"{ntype}:{nname}"
        cur = nodes.get(nid)
        if cur is None:
            cur = {
                "id": nid,
                "type": ntype,
                "name": nname,
                "desc": desc or "",
                "domain": domain or classify_domain(nname, desc),
                "core": bool(core),
            }
            if extra:
                cur.update(extra)
            nodes[nid] = cur
        else:
            if desc and (not cur["desc"] or len(cur["desc"]) < len(desc) and ntype == "TABL" and desc in tab_descs.values()):
                cur["desc"] = desc
            if core:
                cur["core"] = True
            if domain and cur["domain"] == "OTHER_ZSD":
                cur["domain"] = domain
            if extra:
                for k, v in extra.items():
                    if v and not cur.get(k):
                        cur[k] = v
        return nid

    def add_edge(src, rel, dst, join="", card="", label="", source="auto", curated=False):
        if src == dst or src not in nodes or dst not in nodes:
            return
        k = (src, rel, dst)
        if k not in edges_map:
            edges_map[k] = {"src": src, "rel": rel, "dst": dst}
            if join:
                edges_map[k]["join"] = join
            if card:
                edges_map[k]["card"] = card
            if label:
                edges_map[k]["label"] = label
            if curated:
                edges_map[k]["curated"] = True
            edges_map[k]["source"] = source
        else:
            e = edges_map[k]
            if curated:
                e["curated"] = True
            if join and not e.get("join"):
                e["join"] = join
            if label and not e.get("label"):
                e["label"] = label

    # 1) DDIC 테이블 적재 (핵심 테이블은 전체/주요 필드, 보조 테이블은 PK 요약)
    for tabname, info in ddic.items():
        tn = tabname.upper()
        tcls = (info.get("tabclass") or "").upper()
        desc = tab_descs.get(tn) or info.get("desc") or info.get("desc_en") or ""
        is_core = _is_core_table(tn, tcls)
        # 보조 DDIC 중 INTTAB/APPEND면서 커스텀이 아니면 그래프 잡음을 줄이기 위해 스킵 (단 CORE/Z*는 포함)
        if not is_core and tcls not in ("TRANSP", "VIEW", ""):
            continue
        raw_fields = info.get("fields") or []
        pks = [f["field"] for f in raw_fields if f.get("pk") and f.get("field") != "MANDT"]
        if is_core:
            slim_fields = [
                {
                    "field": f["field"],
                    "pk": bool(f.get("pk")),
                    "datatype": f.get("datatype", ""),
                    "leng": f.get("leng", 0),
                    "label": (f.get("label") or f.get("rollname") or "")[:36],
                    "checktable": f.get("checktable", ""),
                }
                for f in raw_fields[:45] if f.get("field") != "MANDT"
            ]
        else:
            slim_fields = [
                {
                    "field": f["field"],
                    "pk": bool(f.get("pk")),
                    "datatype": f.get("datatype", ""),
                    "leng": f.get("leng", 0),
                    "label": (f.get("label") or "")[:30],
                }
                for f in raw_fields[:12] if f.get("pk") and f.get("field") != "MANDT"
            ]
        ensure_node("TABL", tn, desc=desc, core=is_core, extra={"tabclass": tcls, "pk": pks, "fields": slim_fields})

    # 아카이브 zxx-tables 사전에만 있고 ddic에 누락된 ZXX_T 테이블 보충
    for tn, d in tab_descs.items():
        if tn.startswith(("ZXX_T", "ZXX_CO", "ZXX_EI")):
            ensure_node("TABL", tn, desc=d, core=True, extra={"tabclass": "TRANSP", "pk": [], "fields": []})

    # 2) cbo-impact objects.csv에서 ZXX_PKG/ZSD 패키지 오브젝트(PROG/FUNC/FUGR/TRAN/CLAS) 적재
    pkg_objs = set()
    obj_path = os.path.join(RAW_DIR, "objects.csv")
    if os.path.isfile(obj_path):
        with io.open(obj_path, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                dc = (r.get("devclass") or "").upper()
                ot = (r.get("obj_type") or "").upper()
                on = (r.get("obj_name") or "").upper()
                if not on:
                    continue
                is_sd = dc.startswith("ZXX") or on.startswith(("ZXX_R", "ZXX_S", "ZXX_E", "ZXX_F", "SAPMZXX", "ZXX_FG"))
                if not is_sd:
                    continue
                if ot in ("PROG", "FUNC", "FUGR", "TRAN", "CLAS"):
                    nid_t = "TCODE" if ot == "TRAN" else ot
                    pkg_objs.add((ot, on))
                    ensure_node(nid_t, on, domain=classify_domain(on), core=on.startswith(("ZXX_R", "ZXX_S", "SAPMZXX", "ZXX_FG")), extra={"devclass": dc})

    # 3) TSTC/TSTCT & TFDIR/ENLFDIR 적재
    tcode_desc = {r["tcode"].upper(): r.get("ttext", "") for r in tf_raw.get("tstct", []) if r.get("tcode")}
    func_desc = {r["funcname"].upper(): r.get("stext", "") for r in tf_raw.get("tftit", []) if r.get("funcname")}
    func_area = {r["funcname"].upper(): r.get("area", "").upper() for r in tf_raw.get("enlfdir", []) if r.get("funcname")}

    for r in tf_raw.get("tstc", []):
        tc = (r.get("tcode") or "").upper()
        pg = (r.get("pgmna") or "").upper()
        if not tc:
            continue
        desc = tcode_desc.get(tc, "")
        tid = ensure_node("TCODE", tc, desc=desc, domain=classify_domain(tc, desc), core=True)
        if pg:
            pid = ensure_node("PROG", pg, desc=desc, domain=classify_domain(pg, desc), core=pg.startswith(("ZXX_R", "ZXX_S", "SAPMZXX")))
            add_edge(tid, "RUNS_PROG", pid, label="실행 프로그램", source="TSTC", curated=True)

    for tc, tables in CURATED_TCODE_LINKS.items():
        tid = ensure_node("TCODE", tc, desc=tcode_desc.get(tc, ""), core=True)
        for tn in tables:
            if f"TABL:{tn}" in nodes:
                add_edge(tid, "READS_WRITES_TABLE", f"TABL:{tn}", label="주요 사용 테이블", source="curated", curated=True)

    for r in tf_raw.get("tfdir", []):
        fn = (r.get("funcname") or "").upper()
        pn = (r.get("pname") or "").upper()
        if not fn:
            continue
        fdesc = func_desc.get(fn, "")
        fid = ensure_node("FUNC", fn, desc=fdesc, domain=classify_domain(fn, fdesc), core=True)
        area = func_area.get(fn, "")
        if area:
            gid = ensure_node("FUGR", area, desc=fdesc or "함수그룹", domain=classify_domain(area, fdesc), core=True)
            add_edge(fid, "BELONGS_TO_FUGR", gid, label="소속 함수그룹", source="ENLFDIR", curated=True)
        elif pn.startswith("SAPL"):
            gid = ensure_node("FUGR", pn[4:], desc="함수그룹", domain=classify_domain(pn[4:]), core=True)
            add_edge(fid, "BELONGS_TO_FUGR", gid, label="소속 함수그룹", source="TFDIR", curated=True)

    # 4) uses.csv 의존관계 병합 (ZXX_PKG 사용자 ↔ 테이블/함수/프로그램/T-Code)
    uses_path = os.path.join(RAW_DIR, "uses.csv")
    if os.path.isfile(uses_path):
        with io.open(uses_path, "r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                ut = (r.get("user_type") or "").upper()
                un = (r.get("user_name") or "").upper()
                dt = (r.get("used_type") or "").upper()
                dn = (r.get("used_name") or "").upper()
                src_tag = (r.get("source") or "uses").upper()
                if not un or not dn:
                    continue
                is_zsd_user = (ut, un) in pkg_objs or un.startswith(("ZXX_R", "ZXX_S", "ZXX_E", "SAPMZXX", "ZXX_FG"))
                if not is_zsd_user:
                    continue
                u_nid = f"{'TCODE' if ut == 'TRAN' else ut}:{un}"
                if u_nid not in nodes:
                    ensure_node("TCODE" if ut == "TRAN" else ut, un, core=False)
                if dt == "TABL" and f"TABL:{dn}" in nodes:
                    # 표준 공통 잡음(SYST 등) 제외 — ddic에 수록된 테이블만 연결
                    add_edge(u_nid, "READS_WRITES_TABLE", f"TABL:{dn}", source=src_tag)
                elif dt == "FUNC" and dn.startswith(("Z", "BAPI_", "SD_", "RV_", "WB2_")):
                    fn_nid = ensure_node("FUNC", dn, desc=func_desc.get(dn, ""), domain=nodes[u_nid]["domain"])
                    add_edge(u_nid, "CALLS_FUNC", fn_nid, source=src_tag)
                elif dt == "TRAN" and f"TCODE:{dn}" in nodes:
                    add_edge(u_nid, "RUNS_PROG", f"TCODE:{dn}", label="CALL TRANSACTION", source=src_tag)
                elif dt == "PROG" and dn.startswith(("ZXX_R", "ZXX_S", "SAPMZXX")) and un != dn:
                    p2_nid = ensure_node("PROG", dn, domain=nodes[u_nid]["domain"])
                    add_edge(u_nid, "CALLS_PROG", p2_nid, source=src_tag)

    # 5) 큐레이션 TABLE_REL + DDIC checktable 자동 추론
    for s, d, j, card, lbl in CURATED_TABLE_RELS:
        sid = ensure_node("TABL", s, core=True)
        did = ensure_node("TABL", d, core=True)
        add_edge(sid, "TABLE_REL", did, join=j, card=card, label=lbl, source="curated", curated=True)

    # checktable 기반 외래키 자동 연결 (핵심 테이블 중심)
    for nid, node in list(nodes.items()):
        if node["type"] != "TABL" or not node.get("core"):
            continue
        for fld in node.get("fields") or []:
            chk = (fld.get("checktable") or "").upper()
            if chk and chk != "*" and chk != node["name"] and f"TABL:{chk}" in nodes:
                add_edge(nid, "TABLE_REL", f"TABL:{chk}", join=fld["field"], card="N:1", label="DDIC 체크테이블", source="DD03L")

    # 6) 아카이브 문서 ↔ 엔티티 연결 (DOCUMENTED_IN)
    for aid, ainfo in arts.items():
        art_nid = ensure_node("ART", aid, desc=f"[{ainfo['cat']}] {ainfo['title']}" if ainfo["cat"] else ainfo["title"],
                              domain="OTHER_ZSD", core=True, extra={"article_id": aid, "path": ainfo["path"]})
        # zxx-tables 전체 사전은 160개 테이블을 전부 걸면 RRF 부스트가 퍼지므로 개별 아티클 위주(<=40 refs) 또는 1차 참조만 연결
        ref_list = ainfo["refs"]
        if aid == "zxx-tables":
            # 핵심 대표 테이블 몇 개만 대표 링크 (개별 설명은 node["desc"]에 이미 반영됨)
            ref_list = [(t, n) for (t, n) in ref_list if n in ("ZXX_T0011", "ZXX_T0015", "ZXX_T0026", "ZXX_T0071", "ZXX_T0100", "ZXX_T0240")]
        for rtype, rname in ref_list:
            rnid = f"{rtype}:{rname}"
            if rnid in nodes:
                add_edge(rnid, "DOCUMENTED_IN", art_nid, label=ainfo["title"][:30], source="articles")

    # 7) DEV 카드 ↔ 엔티티 연결 (MODIFIED_BY_DEV)
    for did, dinfo in devs.items():
        dev_nid = ensure_node("DEV", did, desc=dinfo["title"], domain="OTHER_ZSD", core=False)
        for rtype, rname in dinfo["refs"]:
            rnid = f"{rtype}:{rname}"
            if rnid in nodes:
                add_edge(rnid, "MODIFIED_BY_DEV", dev_nid, label=dinfo["title"][:28], source="tasks.md")

    # 8) 수동 upsert(manual_overrides.json) 병합 — 수동 추가/수정분이 재동기화로 지워지지 않도록 보장
    for tname, tinfo in overrides.get("tables", {}).items():
        tn = tname.upper()
        pks = tinfo.get("pk") or []
        flds = [{"field": p, "pk": True, "datatype": "", "leng": 0, "label": "PK"} for p in pks]
        nid = ensure_node("TABL", tn, desc=tinfo.get("desc", ""), domain=tinfo.get("domain"), core=True, extra={"tabclass": "TRANSP", "pk": pks, "fields": flds})
        for fk_spec in tinfo.get("fk", []):
            if ":" in fk_spec:
                fk_f, fk_t = fk_spec.split(":", 1)
                tid = ensure_node("TABL", fk_t.upper(), core=True)
                add_edge(nid, "TABLE_REL", tid, join=fk_f.upper(), card="N:1", label="수동 FK 등록", source="manual_override", curated=True)
    for pname, pinfo in overrides.get("programs", {}).items():
        pn = pname.upper()
        pid = ensure_node("PROG", pn, desc=pinfo.get("desc", ""), core=True)
        tc = (pinfo.get("tcode") or "").upper()
        if tc:
            tid = ensure_node("TCODE", tc, desc=pinfo.get("desc", ""), core=True)
            add_edge(tid, "RUNS_PROG", pid, label="실행 프로그램", source="manual_override", curated=True)
        for tn in pinfo.get("tables", []):
            tn_id = ensure_node("TABL", tn.upper(), core=True)
            add_edge(pid, "READS_WRITES_TABLE", tn_id, source="manual_override", curated=True)
        for fn in pinfo.get("funcs", []):
            fn_id = ensure_node("FUNC", fn.upper(), core=True)
            add_edge(pid, "CALLS_FUNC", fn_id, source="manual_override", curated=True)
    for oe in overrides.get("edges", []):
        if oe.get("src") in nodes and oe.get("dst") in nodes:
            add_edge(oe["src"], oe["rel"], oe["dst"], join=oe.get("join", ""), label=oe.get("label", ""), source="manual_override", curated=True)

    # 정렬 및 sync_hash 계산
    sorted_nodes = {k: nodes[k] for k in sorted(nodes)}
    sorted_edges = sorted(edges_map.values(), key=lambda e: (e["src"], e["rel"], e["dst"]))
    counts = {
        "tables": sum(1 for n in sorted_nodes.values() if n["type"] == "TABL"),
        "core_tables": sum(1 for n in sorted_nodes.values() if n["type"] == "TABL" and n.get("core")),
        "programs": sum(1 for n in sorted_nodes.values() if n["type"] == "PROG"),
        "functions": sum(1 for n in sorted_nodes.values() if n["type"] == "FUNC"),
        "fugrs": sum(1 for n in sorted_nodes.values() if n["type"] == "FUGR"),
        "tcodes": sum(1 for n in sorted_nodes.values() if n["type"] == "TCODE"),
        "articles": sum(1 for n in sorted_nodes.values() if n["type"] == "ART"),
        "dev_cards": sum(1 for n in sorted_nodes.values() if n["type"] == "DEV"),
        "edges": len(sorted_edges),
        "table_rels": sum(1 for e in sorted_edges if e["rel"] == "TABLE_REL"),
    }
    hash_payload = json.dumps({"nodes": sorted_nodes, "edges": sorted_edges}, ensure_ascii=False, sort_keys=True)
    sync_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()[:16]

    return {
        "meta": {
            "version": "1.0",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_system": "SAP DEV 120 DDIC + cbo-impact + archive + tasks",
            "sync_hash": sync_hash,
            "counts": counts,
        },
        "domains": DOMAINS,
        "nodes": sorted_nodes,
        "edges": sorted_edges,
    }


def render_catalog_md(ont: dict) -> str:
    meta = ont["meta"]
    c = meta["counts"]
    nodes = ont["nodes"]
    edges = ont["edges"]
    lines = [
        f"<!-- sync_hash: {meta['sync_hash']} -->",
        "# SAP SD ERD · 프로그램 · 함수 · 온톨로지 카탈로그 (`ERD_CATALOG.md`)",
        "",
        f"> 자동 생성: `erd-graph/sync_erd_ontology.py` | sync_hash: `{meta['sync_hash']}` | 기준: `{meta['source_system']}`",
        "",
        "## 1. 전체 규모 요약",
        "",
        "| 구분 | 건수 | 비고 |",
        "|---|---:|---|",
        f"| 전체 테이블 (`TABL`) | **{c['tables']:,}** | 핵심 SD/ZXX_T 테이블 **{c['core_tables']}**개 + 참조 DDIC |",
        f"| 프로그램 (`PROG`) | **{c['programs']:,}** | ZXX_PKG/ZSD 모듈 · 레포트 · Include |",
        f"| T-Code (`TRAN`) | **{c['tcodes']:,}** | TSTC/TSTCT + 핵심 표준 T-Code |",
        f"| 함수 / 함수그룹 (`FUNC`/`FUGR`) | **{c['functions']} / {c['fugrs']}** | TFDIR/ENLFDIR + CROSS 호출 |",
        f"| 아카이브 문서 / DEV 카드 | **{c['articles']} / {c['dev_cards']}** | `DOCUMENTED_IN` / `MODIFIED_BY_DEV` |",
        f"| 온톨로지 관계 (`edges`) | **{c['edges']:,}** | 테이블 ERD 관계 `TABLE_REL` **{c['table_rels']}**건 |",
        "",
        "---",
        "",
        "## 2. 핵심 업무 흐름 ERD (Mermaid)",
        "",
        "### O2C 전체 흐름 (매출품의 → 판매오더 → 납품 → 빌링 → 회계·세금계산서)",
        "```mermaid",
        "erDiagram",
        '    ZXX_T0090 ||--o{ ZXX_T0100 : "ZXX_QUONO"',
        '    ZXX_T0100 ||--o{ ZXX_T0110 : "ZXX_DOCNO"',
        '    ZXX_T0100 ||--o{ ZXX_T0102 : "결재이력"',
        '    ZXX_T0100 ||--o{ VBAK : "VBELN/ZXX_DOCNO"',
        '    VBAK ||--o{ VBAP : "VBELN"',
        '    VBAP ||--o{ VBEP : "VBELN,POSNR"',
        '    VBAP ||--o{ LIPS : "VGBEL,VGPOS"',
        '    LIKP ||--o{ LIPS : "VBELN"',
        '    LIKP ||--o{ ZXX_T0070 : "바코드"',
        '    LIKP ||--o{ ZXX_T0071 : "인수증"',
        '    LIPS ||--o{ VBRP : "VGBEL,VGPOS"',
        '    VBRK ||--o{ VBRP : "VBELN"',
        '    VBRK ||--|| BKPF : "AWKEY"',
        '    BKPF ||--o{ BSEG : "BELNR,GJAHR"',
        '    VBRK ||--o{ ZXX_T0011 : "VBELN"',
        '    ZXX_T0011 ||--o{ ZXX_T0012 : "ZXX_TAXNO"',
        '    ZXX_T0011 ||--|| ZXX_EI_IF : "전자세금계산서 중계"',
        "```",
        "",
        "### 여신 · FSCM · 채권 흐름",
        "```mermaid",
        "erDiagram",
        '    KNA1 ||--o{ KNVV : "KUNNR"',
        '    KNA1 ||--|| UKMBP_CMS : "PARTNER"',
        '    UKMBP_CMS ||--o{ UKMBP_CMS_SGM : "C_SGMNT"',
        '    KNA1 ||--o{ ZXX_T0026 : "여신증액/특별여신"',
        '    KNA1 ||--o{ ZXX_T0024 : "담보/보증"',
        '    KNA1 ||--o{ ZXX_T0032 : "채권면제"',
        '    KNA1 ||--o{ BSID : "미결채권"',
        '    KNA1 ||--o{ BSAD : "반제채권"',
        "```",
        "",
        "---",
        "",
        "## 3. 도메인별 핵심 테이블 카탈로그",
        "",
    ]

    # 도메인별 핵심 테이블 목록 + 연결 관계
    rel_out = {}
    prog_by_tab = {}
    art_by_ent = {}
    for e in edges:
        if e["rel"] == "TABLE_REL":
            rel_out.setdefault(e["src"], []).append(e)
        elif e["rel"] == "READS_WRITES_TABLE":
            prog_by_tab.setdefault(e["dst"], []).append(e["src"].split(":", 1)[-1])
        elif e["rel"] == "DOCUMENTED_IN":
            art_by_ent.setdefault(e["src"], []).append(e["dst"].split(":", 1)[-1])

    order = sorted(DOMAINS.items(), key=lambda kv: kv[1]["order"])
    for dk, dmeta in order:
        tabs = [n for n in nodes.values() if n["type"] == "TABL" and n.get("core") and n.get("domain") == dk]
        if not tabs:
            continue
        lines.append(f"### {dmeta['label']} (`{dk}` · {len(tabs)}개)")
        lines.append("")
        lines.append("| 테이블 | 설명 | PK(주요) | 연결 테이블(`TABLE_REL`) | 주요 프로그램 | 아카이브 |")
        lines.append("|---|---|---|---|---|---|")
        for t in sorted(tabs, key=lambda x: x["name"])[:60]:
            pks = ", ".join(t.get("pk", [])[:4]) or "-"
            rels = ", ".join(
                f"{e['dst'].split(':',1)[-1]}" + (f"({e['join']})" if e.get("join") else "")
                for e in rel_out.get(t["id"], [])[:4]
            ) or "-"
            progs = ", ".join(sorted(set(prog_by_tab.get(t["id"], [])))[:4]) or "-"
            arts = ", ".join(sorted(set(art_by_ent.get(t["id"], [])))[:3]) or "-"
            desc_clean = (t.get("desc") or "").replace("|", "/").replace("\n", " ")[:48]
            lines.append(f"| `{t['name']}` | {desc_clean} | `{pks}` | {rels} | {progs} | {arts} |")
        lines.append("")

    # T-Code -> Program -> Table 매핑 요약 (상위 80개)
    lines.extend([
        "---",
        "",
        "## 4. 주요 T-Code → 프로그램 → 테이블 빠른 색인",
        "",
        "| T-Code | 설명 | 실행 프로그램(`RUNS_PROG`) | 사용 테이블 요약 |",
        "|---|---|---|---|",
    ])
    t_runs = {}
    p_tabs = {}
    t_tabs = {}
    for e in edges:
        if e["rel"] == "RUNS_PROG" and e["src"].startswith("TCODE:"):
            t_runs.setdefault(e["src"], []).append(e["dst"].split(":", 1)[-1])
        elif e["rel"] == "READS_WRITES_TABLE":
            if e["src"].startswith("PROG:"):
                p_tabs.setdefault(e["src"], []).append(e["dst"].split(":", 1)[-1])
            elif e["src"].startswith("TCODE:"):
                t_tabs.setdefault(e["src"], []).append(e["dst"].split(":", 1)[-1])
    tc_nodes = sorted([n for n in nodes.values() if n["type"] == "TCODE" and (n.get("core") or n["id"] in t_runs)],
                      key=lambda x: x["name"])[:100]
    for tn in tc_nodes:
        progs = sorted(set(t_runs.get(tn["id"], [])))
        tabs = list(dict.fromkeys(t_tabs.get(tn["id"], [])))
        for p in progs:
            tabs.extend([x for x in p_tabs.get(f"PROG:{p}", []) if x.startswith(("ZXX_T", "ZXX_CO", "ZXX_EI")) or x in CORE_STD_SET])
        tabs = list(dict.fromkeys(tabs))[:6]
        tdesc = (tn.get("desc") or "").replace("|", "/")[:40]
        lines.append(f"| `{tn['name']}` | {tdesc} | {', '.join(progs[:2]) or '-'} | {', '.join(tabs) or '-'} |")
    lines.append("")
    return "\n".join(lines)


def _compute_hash(ont: dict) -> str:
    payload = json.dumps({"nodes": ont.get("nodes", {}), "edges": ont.get("edges", [])}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def check_sync() -> int:
    """schema_ontology.json, ERD_CATALOG.md, erd-viewer.html 간 정합성을 검사한다."""
    for p in (OUT_JSON, OUT_MD, OUT_HTML):
        if not os.path.isfile(p) or os.path.getsize(p) < 200:
            print(f"[FAIL] 산출물 누락 또는 빈 파일: {p}")
            return 1
    try:
        with io.open(OUT_JSON, "r", encoding="utf-8") as f:
            ont = json.load(f)
    except Exception as e:
        print(f"[FAIL] schema_ontology.json 파싱 실패: {e}")
        return 1
    recorded = (ont.get("meta") or {}).get("sync_hash", "")
    actual = _compute_hash(ont)
    if not recorded or recorded != actual:
        print(f"[FAIL] schema_ontology.json 해시 불일치: recorded={recorded}, actual={actual}")
        return 1
    with io.open(OUT_MD, "r", encoding="utf-8") as f:
        md_head = f.read(300)
    if f"sync_hash: {recorded}" not in md_head:
        print(f"[FAIL] ERD_CATALOG.md sync_hash 불일치 (expected {recorded})")
        return 1
    with io.open(OUT_HTML, "r", encoding="utf-8") as f:
        html_txt = f.read()
    if recorded not in html_txt:
        print(f"[FAIL] erd-viewer.html sync_hash 불일치 (expected {recorded})")
        return 1
    c = (ont.get("meta") or {}).get("counts", {})
    print(f"[OK] ERD/Ontology sync verified (hash={recorded}, tables={c.get('tables')}, progs={c.get('programs')}, edges={c.get('edges')})")
    return 0


def apply_upsert(args) -> bool:
    ov = _load_json(OVERRIDE_PATH, {"tables": {}, "programs": {}, "edges": []})
    changed = False
    if args.upsert_table:
        tn = args.upsert_table.strip().upper()
        cur = ov["tables"].setdefault(tn, {})
        if args.desc:
            cur["desc"] = args.desc
        if args.domain:
            cur["domain"] = args.domain
        if args.pk:
            cur["pk"] = [x.strip().upper() for x in args.pk.split(",") if x.strip()]
        if args.fk:
            fks = set(cur.get("fk", []))
            fks.add(args.fk.strip().upper())
            cur["fk"] = sorted(fks)
        changed = True
    if args.upsert_prog:
        pn = args.upsert_prog.strip().upper()
        cur = ov["programs"].setdefault(pn, {})
        if args.desc:
            cur["desc"] = args.desc
        if args.tcode:
            cur["tcode"] = args.tcode.strip().upper()
        if args.tables:
            ts = set(cur.get("tables", []))
            ts.update(x.strip().upper() for x in args.tables.split(",") if x.strip())
            cur["tables"] = sorted(ts)
        if args.funcs:
            fs = set(cur.get("funcs", []))
            fs.update(x.strip().upper() for x in args.funcs.split(",") if x.strip())
            cur["funcs"] = sorted(fs)
        changed = True
    if args.upsert_edge:
        src, rel, dst = args.upsert_edge
        ov["edges"].append({
            "src": src.strip().upper(),
            "rel": rel.strip().upper(),
            "dst": dst.strip().upper(),
            "join": (args.join or "").strip().upper(),
            "label": (args.desc or "").strip(),
        })
        changed = True
    if changed:
        tmp = OVERRIDE_PATH + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(ov, f, ensure_ascii=False, indent=2)
        os.replace(tmp, OVERRIDE_PATH)
    return changed


def write_all() -> int:
    ont = build_ontology()
    for path, content in (
        (OUT_JSON, json.dumps(ont, ensure_ascii=False, separators=(",", ":"))),
        (OUT_MD, render_catalog_md(ont)),
        (OUT_HTML, render_viewer_html(ont)),
    ):
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    c = ont["meta"]["counts"]
    print(f"[+] Synced ERD & Ontology (hash={ont['meta']['sync_hash']}): "
          f"tables={c['tables']} (core={c['core_tables']}), progs={c['programs']}, "
          f"funcs={c['functions']}, tcodes={c['tcodes']}, arts={c['articles']}, devs={c['dev_cards']}, edges={c['edges']}")
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Sync & verify SAP SD ERD / Ontology catalog and viewer")
    ap.add_argument("--check", action="store_true", help="Verify schema_ontology.json, ERD_CATALOG.md, erd-viewer.html consistency")
    ap.add_argument("--upsert-table", help="Register/update a SAP table name")
    ap.add_argument("--upsert-prog", help="Register/update a SAP program name")
    ap.add_argument("--upsert-edge", nargs=3, metavar=("SRC", "REL", "DST"), help="Add manual edge (e.g. TABL:A TABLE_REL TABL:B)")
    ap.add_argument("--desc", help="Korean description")
    ap.add_argument("--domain", help="Domain key (O2C_QUOTE_SO, DELIVERY_MM, ...)")
    ap.add_argument("--pk", help="Comma-separated PK fields")
    ap.add_argument("--fk", help="FK mapping FIELD:TARGET_TAB")
    ap.add_argument("--tcode", help="Linked T-Code for --upsert-prog")
    ap.add_argument("--tables", help="Comma-separated tables for --upsert-prog")
    ap.add_argument("--funcs", help="Comma-separated functions for --upsert-prog")
    ap.add_argument("--join", help="Join key for --upsert-edge")
    args = ap.parse_args(argv[1:])

    if args.check:
        return check_sync()
    apply_upsert(args)
    return write_all()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
