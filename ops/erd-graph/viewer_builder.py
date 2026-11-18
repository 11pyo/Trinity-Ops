# -*- coding: utf-8 -*-
"""
viewer_builder.py — erd-graph/erd-viewer.html 단독 실행형 인터랙티브 ERD · 온톨로지 뷰어 생성기.

특징:
- 외부 CDN 의존성 0 (오프라인·CSP 'self' 'unsafe-inline' 환경 완전 호환).
- View 1: 도메인별 SVG ERD 캔버스 (PK/FK 필드 표시, 관계선 곡선 연결, 드래그·줌·필터).
- View 2: 1~2홉 온톨로지 탐색기 (테이블 · 프로그램 · 함수 · T-Code · 아카이브 문서 · DEV 카드).
- AI / 사용자용 1클릭 Markdown 요약 복사 버튼.
"""

import json


def render_viewer_html(ontology: dict) -> str:
    """schema_ontology dict를 자립형 HTML 문자열로 렌더링한다."""
    # 뷰어 경량화를 위해 _ix 제거 후 JSON 임베드
    payload = {
        "meta": ontology.get("meta", {}),
        "domains": ontology.get("domains", {}),
        "nodes": ontology.get("nodes", {}),
        "edges": ontology.get("edges", []),
    }
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # HTML <script> 내부 안전 이스케이프
    safe_json = raw_json.replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SAP SD ERD · 온톨로지 관계도</title>
<style>
:root{{
  --bg:#0b1120; --panel:#111a2e; --panel2:#16223b; --line:#243454; --txt:#e2e8f0; --muted:#94a3b8;
  --accent:#38bdf8; --ok:#34d399; --warn:#fbbf24; --pink:#f472b6; --purple:#a78bfa; --rose:#fb7185;
  --hi:#fff; --field:#0f172a; --field-line:#1a2844; --field-on:#152340; --chip-on:#172a4a; --tagbg:#1e293b; --on-accent:#061224;
  --stage1:#111d35; --stage2:#080d1a; --pill:rgba(15,23,42,.88); --box:#111a2e; --box-line:#2d4269; --hdr:#182747;
  --fld:#cbd5e1; --pk:#fbbf24; --edge:#475569; --edge-ont:#334155; --arrow:#64748b; --row-line:#1c2a46;
}}
*{{box-sizing:border-box}}
html,body{{margin:0;height:100%;background:var(--bg);color:var(--txt);font-family:"Malgun Gothic","Segoe UI",system-ui,sans-serif;font-size:13px}}
.app{{display:grid;grid-template-rows:52px 1fr;height:calc(100vh - 44px);overflow:hidden}}
.top{{display:flex;align-items:center;gap:10px;padding:0 14px;background:var(--panel);border-bottom:1px solid var(--line)}}
.top h1{{font-size:15px;margin:0;white-space:nowrap;color:var(--hi)}}
.badge{{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--tagbg);color:var(--accent);border:1px solid var(--line)}}
.search{{flex:1;max-width:340px;padding:7px 11px;border-radius:8px;border:1px solid var(--line);background:var(--field);color:var(--hi);outline:none}}
.search:focus{{border-color:var(--accent)}}
.btn{{padding:6px 11px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--txt);cursor:pointer;font-size:12px}}
.btn:hover{{border-color:var(--accent)}}
.btn.on{{background:var(--accent);color:var(--on-accent);font-weight:700;border-color:var(--accent)}}
.main{{display:grid;grid-template-columns:250px 1fr 340px;height:100%;overflow:hidden}}
.side,.insp{{background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:12px}}
.insp{{border-right:none;border-left:1px solid var(--line)}}
.sec-t{{font-size:11px;text-transform:uppercase;color:var(--muted);margin:12px 0 6px;letter-spacing:.4px}}
.sec-t:first-child{{margin-top:0}}
.dom-item{{display:flex;justify-content:space-between;align-items:center;padding:7px 9px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent}}
.dom-item:hover{{background:var(--panel2)}}
.dom-item.on{{background:var(--panel2);border-color:var(--accent)}}
.dot{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px}}
.chip-row{{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}}
.chip{{font-size:11px;padding:3px 8px;border-radius:999px;background:var(--field);border:1px solid var(--line);color:var(--muted);cursor:pointer}}
.chip.on{{color:var(--hi);border-color:var(--accent);background:var(--chip-on)}}
.node-list{{display:flex;flex-direction:column;gap:4px}}
.n-row{{padding:6px 8px;border-radius:7px;background:var(--field);border:1px solid var(--field-line);cursor:pointer;display:flex;flex-direction:column;gap:2px}}
.n-row:hover,.n-row.on{{border-color:var(--accent);background:var(--field-on)}}
.n-top{{display:flex;justify-content:space-between;align-items:center;font-weight:700}}
.tag{{font-size:10px;padding:1px 5px;border-radius:4px;background:var(--tagbg);color:var(--accent)}}
.tag.PROG{{color:var(--ok)}} .tag.TCODE{{color:var(--warn)}} .tag.PROC{{color:var(--accent)}} .tag.FUNC,.tag.FUGR{{color:var(--purple)}} .tag.ART{{color:var(--pink)}} .tag.DEV{{color:var(--rose)}}
.n-sub{{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.stage{{position:relative;overflow:hidden;background:radial-gradient(circle at 20% 20%,var(--stage1),var(--stage2))}}
.stage-bar{{position:absolute;top:10px;left:10px;right:10px;z-index:5;display:flex;justify-content:space-between;pointer-events:none}}
.stage-bar>*{{pointer-events:auto}}
.hint-pill{{background:var(--pill);border:1px solid var(--line);border-radius:8px;padding:5px 10px;font-size:11px;color:var(--muted)}}
svg#canvas{{width:100%;height:100%;cursor:grab}}
svg#canvas:active{{cursor:grabbing}}
.erd-card{{cursor:pointer}}
.erd-box{{fill:var(--box);stroke:var(--box-line);stroke-width:1.4;rx:8}}
.erd-card.sel .erd-box{{stroke:var(--accent);stroke-width:2.5}}
.erd-hdr{{fill:var(--hdr)}}
.erd-title{{fill:var(--hi);font-size:12px;font-weight:700}}
.erd-desc{{fill:var(--muted);font-size:10px}}
.erd-fld{{fill:var(--fld);font-size:10.5px;font-family:Consolas,monospace}}
.erd-pk{{fill:var(--pk);font-weight:700}}
.edge-path{{fill:none;stroke:var(--edge);stroke-width:1.4;opacity:.75}}
.edge-path.hi{{stroke:var(--accent);stroke-width:2.4;opacity:1}}
.edge-path.curated{{stroke:var(--accent)}}
.edge-lbl{{fill:var(--muted);font-size:9.5px;pointer-events:none}}
.insp h2{{margin:0 0 4px;font-size:16px;color:var(--hi);word-break:break-all}}
.insp .desc{{color:var(--muted);margin-bottom:10px;line-height:1.45}}
.tbl-f{{width:100%;border-collapse:collapse;font-size:11px;margin-top:6px}}
.tbl-f th,.tbl-f td{{padding:4px 6px;border-bottom:1px solid var(--row-line);text-align:left}}
.tbl-f th{{color:var(--muted);font-weight:600}}
.rel-card{{padding:6px 8px;border-radius:6px;background:var(--field);border:1px solid var(--field-line);margin-bottom:5px;cursor:pointer}}
.rel-card:hover{{border-color:var(--accent)}}
.empty{{color:var(--muted);padding:14px 4px;font-size:12px}}
/* SD 화면 공통 기반 (2026-11-17) — 키보드 포커스 표시 · 작은 글씨 하한 11px */
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{{outline:2px solid #5b9fe0;outline-offset:2px}}
.tag{{font-size:11px}}
/* Ops Console 공통 색 이름 · 위쪽 메뉴 (정본 templates/ops-console.md, 2026-11-17) */
:root{{--oc-bg:#eef1f5;--oc-panel:#f4f6f9;--oc-card:#ffffff;--oc-ink:#18212c;--oc-sub:#5d6978;--oc-line:#d9dfe6;
  --oc-pt:#1a6fb5;--oc-pt-ink:#ffffff;--oc-chip:#eef1f5;--oc-warn-bg:#fff4dc;--oc-warn-ink:#7a5200;
  --oc-good:#1f7a33;--oc-good-bg:#e8f6ea;--oc-bad:#b3261e;--oc-bad-bg:#fdecea;--oc-nav:#18212c;--oc-nav-ink:#e7ecf2;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--oc-bg:#0f141a;--oc-panel:#151b23;--oc-card:#1c242e;--oc-ink:#e7ecf2;--oc-sub:#9aa7b6;--oc-line:#2b3542;
  --oc-pt:#5b9fe0;--oc-pt-ink:#0b1220;--oc-chip:#232d39;--oc-warn-bg:#3a2e12;--oc-warn-ink:#f3d58a;
  --oc-good:#6fd19c;--oc-good-bg:#17291f;--oc-bad:#ff8a80;--oc-bad-bg:#3a1d1b;--oc-nav:#0b1016;--oc-nav-ink:#e7ecf2;color-scheme:dark}}}}
.oc-nav{{display:flex;align-items:center;gap:2px;height:44px;padding:0 10px;background:var(--oc-nav);color:var(--oc-nav-ink);
  font:13px/1 "Malgun Gothic","맑은 고딕",system-ui,sans-serif;overflow-x:auto;scrollbar-width:none;position:relative;z-index:500}}
.oc-nav b{{margin:0 10px 0 4px;white-space:nowrap;font-size:13.5px}}
.oc-nav a{{color:inherit;text-decoration:none;padding:8px 10px;border-radius:8px;white-space:nowrap;opacity:.78}}
.oc-nav a:hover{{opacity:1;background:rgba(255,255,255,.08)}}
.oc-nav a[aria-current="page"]{{opacity:1;font-weight:700;background:rgba(255,255,255,.16)}}
.oc-nav a:focus-visible{{outline:2px solid #5b9fe0;outline-offset:-2px;opacity:1}}
/* 밝은 화면 (2026-11-17) — 어두운 화면이 기본, 기기 설정이 밝음이거나 <html data-theme="light"> 이면 Ops Console 색 이름으로 전환 */
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark}}}}
@media (prefers-color-scheme:light){{:root:not([data-theme="dark"]){{--bg:var(--oc-bg); --panel:var(--oc-card); --panel2:var(--oc-panel); --line:var(--oc-line); --txt:var(--oc-ink); --muted:var(--oc-sub); --accent:#0b67b0; --ok:#0f7a4f; --warn:#9a5b00; --pink:#b5176b; --purple:#6d3fd0; --rose:#b4233c; --hi:var(--oc-ink); --field:var(--oc-card); --field-line:var(--oc-line); --field-on:#e3eefa; --chip-on:#e3eefa; --tagbg:var(--oc-chip); --on-accent:#fff; --stage1:#f7f9fc; --stage2:#e6ebf2; --pill:rgba(255,255,255,.92); --box:#fff; --box-line:#b9c4d2; --hdr:#e7eef7; --fld:#2c3a4b; --pk:#9a5b00; --edge:#8a97a8; --edge-ont:#a3afbd; --arrow:#6b7888; --row-line:var(--oc-line); color-scheme:light}}}}
:root[data-theme="light"]{{--bg:var(--oc-bg); --panel:var(--oc-card); --panel2:var(--oc-panel); --line:var(--oc-line); --txt:var(--oc-ink); --muted:var(--oc-sub); --accent:#0b67b0; --ok:#0f7a4f; --warn:#9a5b00; --pink:#b5176b; --purple:#6d3fd0; --rose:#b4233c; --hi:var(--oc-ink); --field:var(--oc-card); --field-line:var(--oc-line); --field-on:#e3eefa; --chip-on:#e3eefa; --tagbg:var(--oc-chip); --on-accent:#fff; --stage1:#f7f9fc; --stage2:#e6ebf2; --pill:rgba(255,255,255,.92); --box:#fff; --box-line:#b9c4d2; --hdr:#e7eef7; --fld:#2c3a4b; --pk:#9a5b00; --edge:#8a97a8; --edge-ont:#a3afbd; --arrow:#6b7888; --row-line:var(--oc-line); color-scheme:light}}
#canvas line{{stroke:var(--edge-ont)}}
#canvas marker path{{fill:var(--arrow)}}
.erd-card>rect:not([class]){{fill:var(--box)}}
@media(max-width:1080px){{.main{{grid-template-columns:210px 1fr 290px}}}}
@media(max-width:820px){{.main{{grid-template-columns:1fr;grid-template-rows:190px 1fr 240px}}.side,.insp{{border:none;border-bottom:1px solid var(--line)}}}}
@media(max-width:820px){{.app{{grid-template-rows:auto 1fr}}.top{{flex-wrap:wrap;padding:8px 12px}}.top h1{{white-space:normal}}.search{{flex:1 1 100%;max-width:none}}}}
</style>
</head>
<body>
<nav class="oc-nav" aria-label="Ops Console" data-noexport>
  <b>Ops Console</b>
  <a href="http://127.0.0.1:5177/" data-hub="/">문의 데스크</a>
  <a href="../task-dashboard/task-board.html" data-hub="/board">대시보드</a>
  <a href="../archive.html" data-hub="/archive">아카이브</a>
  <a href="../erd-graph/erd-viewer.html" data-hub="/erd">ERD·온톨로지</a>
  <a href="../task-dashboard/bot-metrics.html" data-hub="/bot-metrics">봇 지표</a>
  <a href="../task-dashboard/metrics.html" data-hub="/metrics">협업 지표</a>
</nav>
<script>/* Ops Console 메뉴: 문의 데스크 서버(5177)로 열었으면 서버 주소로, 파일로 열었으면 파일 상대 경로로 */
(function(){{var h=location.host;var hub=["127.0.0.1:5177","localhost:5177","127.0.0.1:5187","localhost:5187"].indexOf(h)>=0;
document.querySelectorAll(".oc-nav a[data-hub]").forEach(function(a){{if(hub){{if(!a.getAttribute("data-hub")){{a.hidden=true;return;}}a.setAttribute("href",a.getAttribute("data-hub"));}}
var me=hub?location.pathname:location.pathname.split("/").pop();var t=hub?a.getAttribute("data-hub"):a.getAttribute("href").split("/").pop();
if(me===t||(hub&&me==="/"&&t==="/"))a.setAttribute("aria-current","page");}});}})();</script>
<div class="app">
  <header class="top">
    <h1>SAP SD ERD · 온톨로지 관계도</h1>
    <span class="badge" id="stats">적재 중...</span>
    <input id="q" class="search" type="search" placeholder="테이블·프로그램·함수·T-Code·업무명 검색 (예: ZXX_T0100, VA01, 여신)" />
    <button id="v-erd" class="btn on" type="button">ERD 도메인</button>
    <button id="v-ont" class="btn" type="button">온톨로지 1~2홉</button>
    <button id="copy-md" class="btn" type="button">AI 요약 복사</button>
  </header>
  <div class="main">
    <aside class="side">
      <div class="sec-t">업무 도메인</div>
      <div id="dom-list"></div>
      <div class="sec-t">유형 필터</div>
      <div id="type-chips" class="chip-row"></div>
      <div class="sec-t">노드 목록 <span id="list-cnt"></span></div>
      <div id="node-list" class="node-list"></div>
    </aside>
    <section class="stage">
      <div class="stage-bar">
        <div class="hint-pill" id="canvas-hint">드래그: 이동 · 휠: 확대/축소 · 카드 클릭: 연결 강조 및 상세</div>
        <div style="display:flex;gap:6px">
          <button id="z-in" class="btn" type="button">+</button>
          <button id="z-out" class="btn" type="button">-</button>
          <button id="z-rst" class="btn" type="button">맞춤</button>
        </div>
      </div>
      <svg id="canvas" xmlns="http://www.w3.org/2000/svg"></svg>
    </section>
    <aside class="insp" id="insp">
      <div class="empty">왼쪽 목록이나 캔버스에서 테이블·프로그램·T-Code를 선택해 주십시오.</div>
    </aside>
  </div>
</div>
<script id="ont-data" type="application/json">{safe_json}</script>
<script>
(function(){{
  var ONT = JSON.parse(document.getElementById("ont-data").textContent || "{{}}");
  var NODES = ONT.nodes || {{}};
  var EDGES = ONT.edges || [];
  var DOMAINS = ONT.domains || {{}};
  var ALL_IDS = Object.keys(NODES);

  var ADJ = {{}};
  ALL_IDS.forEach(function(id){{ ADJ[id] = []; }});
  EDGES.forEach(function(e){{
    if(ADJ[e.src]) ADJ[e.src].push(e);
    if(ADJ[e.dst]) ADJ[e.dst].push(e);
  }});

  var state = {{
    view: "erd",
    domain: "O2C_QUOTE_SO",
    typeFilter: "ALL",
    query: "",
    selected: NODES["TABL:ZXX_T0100"] ? "TABL:ZXX_T0100" : (ALL_IDS[0] || null),
    vx: 0, vy: 0, vw: 1200, vh: 760
  }};

  function $(id){{ return document.getElementById(id); }}
  function esc(s){{
    return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }}
  function svgEl(tag, attrs, text){{
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if(attrs) Object.keys(attrs).forEach(function(k){{ el.setAttribute(k, String(attrs[k])); }});
    if(text !== undefined) el.textContent = text;
    return el;
  }}

  function updateStats(){{
    var c = (ONT.meta && ONT.meta.counts) || {{}};
    $("stats").textContent = "테이블 " + (c.tables || 0) + " · 프로세스 " + (c.processes || 0) + " · 프로그램 " + (c.programs || 0) + " · T-Code " + (c.tcodes || 0) + " · 함수 " + (c.functions || 0) + " · 관계 " + (c.edges || 0);
  }}

  function renderDomains(){{
    var box = $("dom-list"); box.textContent = "";
    var counts = {{}};
    ALL_IDS.forEach(function(id){{
      var n = NODES[id];
      if(n.type === "TABL") counts[n.domain] = (counts[n.domain] || 0) + 1;
    }});
    var order = Object.keys(DOMAINS).sort(function(a,b){{ return (DOMAINS[a].order||99) - (DOMAINS[b].order||99); }});
    order.forEach(function(dk){{
      var d = DOMAINS[dk];
      var row = document.createElement("div");
      row.className = "dom-item" + (state.domain === dk ? " on" : "");
      row.innerHTML = '<span><span class="dot" style="background:' + esc(d.color || "#38bdf8") + '"></span>' + esc(d.label || dk) + '</span><span class="n-sub">' + (counts[dk] || 0) + '</span>';
      row.onclick = function(){{ state.domain = dk; state.view = "erd"; syncButtons(); renderDomains(); renderList(); renderCanvas(); }};
      box.appendChild(row);
    }});
  }}

  function renderChips(){{
    var types = [["ALL","전체"],["PROC","프로세스"],["TABL","테이블"],["TCODE","T-Code"],["PROG","프로그램"],["FUNC","함수"],["ART","문서"],["DEV","DEV"]];
    var box = $("type-chips"); box.textContent = "";
    types.forEach(function(t){{
      var sp = document.createElement("span");
      sp.className = "chip" + (state.typeFilter === t[0] ? " on" : "");
      sp.textContent = t[1];
      sp.onclick = function(){{ state.typeFilter = t[0]; renderChips(); renderList(); }};
      box.appendChild(sp);
    }});
  }}

  function filteredNodes(){{
    var q = state.query.trim().toLowerCase();
    var arr = [];
    ALL_IDS.forEach(function(id){{
      var n = NODES[id];
      if(state.typeFilter !== "ALL" && n.type !== state.typeFilter) return;
      if(!q && state.typeFilter === "ALL" && state.view === "erd" && n.type === "TABL" && n.domain !== state.domain) return;
      if(!q && state.typeFilter === "ALL" && state.view === "erd" && n.type !== "TABL") return;
      if(q){{
        var hay = (n.name + " " + (n.desc || "") + " " + (n.domain || "")).toLowerCase();
        if(hay.indexOf(q) === -1) return;
      }}
      arr.push(n);
    }});
    arr.sort(function(a,b){{
      var ca = a.core ? 0 : 1, cb = b.core ? 0 : 1;
      if(ca !== cb) return ca - cb;
      return a.name.localeCompare(b.name);
    }});
    return arr;
  }}

  function renderList(){{
    var list = filteredNodes();
    $("list-cnt").textContent = "(" + list.length + ")";
    var box = $("node-list"); box.textContent = "";
    list.slice(0, 120).forEach(function(n){{
      var row = document.createElement("div");
      row.className = "n-row" + (state.selected === n.id ? " on" : "");
      row.innerHTML = '<div class="n-top"><span>' + esc(n.name) + '</span><span class="tag ' + esc(n.type) + '">' + esc(n.type) + '</span></div>'
                    + '<div class="n-sub">' + esc(n.desc || n.domain || "") + '</div>';
      row.onclick = function(){{ selectNode(n.id, state.view === "ont" || n.type !== "TABL"); }};
      box.appendChild(row);
    }});
    if(!list.length) box.innerHTML = '<div class="empty">일치하는 항목이 없습니다.</div>';
  }}

  function selectNode(id, switchToOnt){{
    state.selected = id;
    var n = NODES[id];
    if(n && n.type === "TABL" && n.domain && state.view === "erd" && !switchToOnt){{
      state.domain = n.domain;
      renderDomains();
    }}
    if(switchToOnt) state.view = "ont";
    syncButtons();
    renderList();
    renderInspector();
    renderCanvas();
  }}

  function syncButtons(){{
    $("v-erd").className = "btn" + (state.view === "erd" ? " on" : "");
    $("v-ont").className = "btn" + (state.view === "ont" ? " on" : "");
  }}

  function applyViewBox(){{
    $("canvas").setAttribute("viewBox", state.vx + " " + state.vy + " " + state.vw + " " + state.vh);
  }}

  function renderCanvas(){{
    var svg = $("canvas");
    while(svg.firstChild) svg.removeChild(svg.firstChild);
    var defs = svgEl("defs");
    var m = svgEl("marker", {{id:"arr", viewBox:"0 0 10 10", refX:9, refY:5, markerWidth:6, markerHeight:6, orient:"auto-start-reverse"}});
    m.appendChild(svgEl("path", {{d:"M0,1 L10,5 L0,9 z", fill:"#64748b"}}));
    defs.appendChild(m);
    svg.appendChild(defs);

    if(state.view === "erd") renderErdSvg(svg);
    else renderOntologySvg(svg);
    applyViewBox();
  }}

  function renderErdSvg(svg){{
    var tabs = [];
    ALL_IDS.forEach(function(id){{
      var n = NODES[id];
      if(n.type === "TABL" && n.domain === state.domain) tabs.push(n);
    }});
    tabs.sort(function(a,b){{ return (b.core?1:0)-(a.core?1:0) || a.name.localeCompare(b.name); }});
    tabs = tabs.slice(0, 32);
    var pos = {{}}, cols = 4, cardW = 240, gapX = 65, gapY = 42;
    var yCursor = [30, 30, 30, 30];
    var cardHeights = {{}};

    tabs.forEach(function(t, idx){{
      var flds = (t.fields || []).slice(0, 7);
      var h = 44 + Math.max(1, flds.length) * 16 + 10;
      cardHeights[t.id] = h;
      var col = idx % cols;
      var x = 30 + col * (cardW + gapX);
      var y = yCursor[col];
      pos[t.id] = {{x:x, y:y, w:cardW, h:h}};
      yCursor[col] += h + gapY;
    }});

    var maxH = Math.max.apply(null, yCursor.concat([520]));
    state.vx = 0; state.vy = 0; state.vw = Math.max(1260, cols * (cardW + gapX) + 60); state.vh = Math.min(920, maxH + 40);

    var eLayer = svgEl("g");
    var nLayer = svgEl("g");
    svg.appendChild(eLayer); svg.appendChild(nLayer);

    EDGES.forEach(function(e){{
      if(e.rel !== "TABLE_REL") return;
      var p1 = pos[e.src], p2 = pos[e.dst];
      if(!p1 || !p2 || e.src === e.dst) return;
      var x1 = p1.x + p1.w, y1 = p1.y + 24;
      var x2 = p2.x, y2 = p2.y + 24;
      if(p2.x < p1.x){{ x1 = p1.x; x2 = p2.x + p2.w; }}
      else if(p2.x === p1.x){{ x1 = p1.x + p1.w/2; y1 = p1.y + p1.h; x2 = p2.x + p2.w/2; y2 = p2.y; }}
      var cx = (x1 + x2) / 2;
      var d = "M" + x1 + "," + y1 + " C" + cx + "," + y1 + " " + cx + "," + y2 + " " + x2 + "," + y2;
      var hi = (state.selected === e.src || state.selected === e.dst);
      eLayer.appendChild(svgEl("path", {{d:d, "marker-end":"url(#arr)", class:"edge-path" + (hi?" hi":"") + (e.curated?" curated":"")}}));
      if(e.join){{
        eLayer.appendChild(svgEl("text", {{x:cx, y:(y1+y2)/2 - 4, class:"edge-lbl", "text-anchor":"middle"}}, e.join.slice(0,24)));
      }}
    }});

    tabs.forEach(function(t){{
      var p = pos[t.id];
      var g = svgEl("g", {{class:"erd-card" + (state.selected === t.id ? " sel" : ""), transform:"translate(" + p.x + "," + p.y + ")"}});
      g.appendChild(svgEl("rect", {{width:p.w, height:p.h, class:"erd-box"}}));
      g.appendChild(svgEl("rect", {{width:p.w, height:38, rx:8, class:"erd-hdr"}}));
      g.appendChild(svgEl("text", {{x:10, y:16, class:"erd-title"}}, t.name + (t.tabclass ? " (" + t.tabclass + ")" : "")));
      g.appendChild(svgEl("text", {{x:10, y:30, class:"erd-desc"}}, (t.desc || "").slice(0, 32)));
      var flds = (t.fields || []).slice(0, 7);
      if(!flds.length){{
        g.appendChild(svgEl("text", {{x:10, y:56, class:"erd-fld"}}, "필드 요약 없음 (클릭하여 상세)"));
      }}else{{
        flds.forEach(function(f, fi){{
          var fy = 55 + fi * 16;
          var txt = (f.pk ? "🔑 " : "· ") + f.field + " : " + (f.datatype || "") + (f.label ? " · " + f.label.slice(0,12) : "");
          g.appendChild(svgEl("text", {{x:10, y:fy, class:"erd-fld" + (f.pk?" erd-pk":"")}}, txt));
        }});
      }}
      g.onclick = function(){{ selectNode(t.id, false); }};
      nLayer.appendChild(g);
    }});
    $("canvas-hint").textContent = (DOMAINS[state.domain] ? DOMAINS[state.domain].label : state.domain) + " · 테이블 " + tabs.length + "개 표시";
  }}

  function renderOntologySvg(svg){{
    var centerId = state.selected || ALL_IDS[0];
    if(!centerId || !NODES[centerId]) return;
    var inc = ADJ[centerId] || [];
    var ring1 = [], seen = {{}}; seen[centerId] = 1;
    inc.forEach(function(e){{
      var o = (e.src === centerId) ? e.dst : e.src;
      if(!seen[o] && NODES[o]){{ seen[o] = 1; ring1.push({{node:NODES[o], edge:e}}); }}
    }});
    ring1 = ring1.slice(0, 26);
    var cx = 600, cy = 380, r = Math.min(280, 140 + ring1.length * 7);
    state.vx = 0; state.vy = 0; state.vw = 1200; state.vh = 760;

    var pos = {{}}; pos[centerId] = {{x:cx, y:cy}};
    ring1.forEach(function(it, i){{
      var ang = (2 * Math.PI * i) / Math.max(1, ring1.length) - Math.PI / 2;
      pos[it.node.id] = {{x: cx + r * Math.cos(ang), y: cy + r * Math.sin(ang)}};
    }});

    var eLayer = svgEl("g"), nLayer = svgEl("g");
    svg.appendChild(eLayer); svg.appendChild(nLayer);
    var colorMap = {{TABL:"#38bdf8", PROG:"#34d399", TCODE:"#fbbf24", FUNC:"#a78bfa", FUGR:"#a78bfa", ART:"#f472b6", DEV:"#fb7185", CLAS:"#60a5fa"}};

    ring1.forEach(function(it){{
      var p2 = pos[it.node.id];
      eLayer.appendChild(svgEl("line", {{x1:cx, y1:cy, x2:p2.x, y2:p2.y, stroke:"#334155", "stroke-width":1.5}}));
      eLayer.appendChild(svgEl("text", {{x:(cx+p2.x)/2, y:(cy+p2.y)/2 - 3, class:"edge-lbl", "text-anchor":"middle"}}, it.edge.rel));
    }});

    function drawNode(n, p, isCenter){{
      var w = isCenter ? 190 : 152, h = isCenter ? 52 : 42;
      var col = colorMap[n.type] || "#94a3b8";
      var g = svgEl("g", {{class:"erd-card" + (isCenter?" sel":""), transform:"translate(" + (p.x - w/2) + "," + (p.y - h/2) + ")"}});
      g.appendChild(svgEl("rect", {{width:w, height:h, rx:10, fill:"#111a2e", stroke:col, "stroke-width":isCenter?2.8:1.5}}));
      g.appendChild(svgEl("text", {{x:10, y:18, class:"erd-title", fill:col}}, "[" + n.type + "] " + n.name));
      g.appendChild(svgEl("text", {{x:10, y:34, class:"erd-desc"}}, (n.desc || "").slice(0, 22)));
      g.onclick = function(){{ selectNode(n.id, true); }};
      nLayer.appendChild(g);
    }}
    ring1.forEach(function(it){{ drawNode(it.node, pos[it.node.id], false); }});
    drawNode(NODES[centerId], pos[centerId], true);
    $("canvas-hint").textContent = "온톨로지 중심: " + NODES[centerId].name + " · 1홉 연결 " + ring1.length + "개 (총 " + inc.length + "건)";
  }}

  function renderInspector(){{
    var n = NODES[state.selected];
    var box = $("insp");
    if(!n){{ box.innerHTML = '<div class="empty">선택된 노드가 없습니다.</div>'; return; }}
    var html = '<span class="tag ' + esc(n.type) + '">' + esc(n.type) + '</span>'
             + (n.domain ? ' <span class="badge">' + esc(n.domain) + '</span>' : '')
             + '<h2>' + esc(n.name) + '</h2>'
             + '<div class="desc">' + esc(n.desc || "설명 없음") + '</div>';

    var inc = ADJ[n.id] || [];
    var groups = {{}};
    inc.forEach(function(e){{
      var oid = (e.src === n.id) ? e.dst : e.src;
      var o = NODES[oid];
      if(!o) return;
      groups[e.rel] = groups[e.rel] || [];
      groups[e.rel].push({{edge:e, node:o, dir:(e.src===n.id?"→":"←")}});
    }});

    Object.keys(groups).forEach(function(rel){{
      var items = groups[rel].slice(0, 24);
      html += '<div class="sec-t">' + esc(rel) + ' (' + groups[rel].length + ')</div>';
      items.forEach(function(it){{
        var j = it.edge.join ? ' · 키: ' + it.edge.join : '';
        html += '<div class="rel-card" data-nid="' + esc(it.node.id) + '">'
              + '<strong>' + it.dir + ' [' + esc(it.node.type) + '] ' + esc(it.node.name) + '</strong>'
              + '<div class="n-sub">' + esc((it.node.desc || "") + j) + '</div></div>';
      }});
    }});

    if(n.type === "TABL" && n.fields && n.fields.length){{
      html += '<div class="sec-t">필드 스키마 (' + n.fields.length + ')</div>'
            + '<table class="tbl-f"><thead><tr><th>필드</th><th>형식</th><th>설명</th></tr></thead><tbody>';
      n.fields.slice(0, 60).forEach(function(f){{
        html += '<tr><td>' + (f.pk ? '🔑 ' : '') + esc(f.field) + '</td><td>' + esc((f.datatype||"") + (f.leng ? "(" + f.leng + ")" : "")) + '</td><td>' + esc(f.label || f.rollname || "") + '</td></tr>';
      }});
      html += '</tbody></table>';
    }}
    box.innerHTML = html;
    box.querySelectorAll(".rel-card").forEach(function(el){{
      el.onclick = function(){{ selectNode(el.getAttribute("data-nid"), state.view === "ont"); }};
    }});
  }}

  // 팬 · 줌 핸들러
  var drag = null;
  var canvas = $("canvas");
  canvas.addEventListener("mousedown", function(e){{ drag = {{x:e.clientX, y:e.clientY, vx:state.vx, vy:state.vy}}; }});
  window.addEventListener("mousemove", function(e){{
    if(!drag) return;
    var r = canvas.getBoundingClientRect();
    state.vx = drag.vx - (e.clientX - drag.x) * (state.vw / Math.max(1, r.width));
    state.vy = drag.vy - (e.clientY - drag.y) * (state.vh / Math.max(1, r.height));
    applyViewBox();
  }});
  window.addEventListener("mouseup", function(){{ drag = null; }});
  canvas.addEventListener("wheel", function(e){{
    e.preventDefault();
    var k = e.deltaY > 0 ? 1.12 : 0.89;
    state.vw *= k; state.vh *= k; applyViewBox();
  }}, {{passive:false}});

  $("z-in").onclick = function(){{ state.vw *= 0.85; state.vh *= 0.85; applyViewBox(); }};
  $("z-out").onclick = function(){{ state.vw *= 1.18; state.vh *= 1.18; applyViewBox(); }};
  $("z-rst").onclick = function(){{ renderCanvas(); }};
  $("v-erd").onclick = function(){{ state.view = "erd"; syncButtons(); renderList(); renderCanvas(); }};
  $("v-ont").onclick = function(){{ state.view = "ont"; syncButtons(); renderList(); renderCanvas(); }};
  $("q").addEventListener("input", function(){{
    state.query = this.value;
    if(this.value.trim()) state.typeFilter = "ALL";
    renderChips(); renderList();
  }});
  $("copy-md").onclick = function(){{
    var n = NODES[state.selected];
    if(!n) return;
    var lines = ["### [" + n.type + "] " + n.name + (n.desc ? " — " + n.desc : "")];
    (ADJ[n.id] || []).slice(0, 20).forEach(function(e){{
      var oid = e.src === n.id ? e.dst : e.src;
      var o = NODES[oid];
      if(o) lines.push("- " + e.rel + " " + (e.src===n.id?"→":"←") + " `" + o.name + "` (" + (o.desc||o.type) + ")" + (e.join ? " [키: " + e.join + "]" : ""));
    }});
    if(navigator.clipboard) navigator.clipboard.writeText(lines.join("\\n")).then(function(){{ alert("AI 요약 Markdown을 복사했습니다."); }});
  }};

  updateStats(); renderDomains(); renderChips(); renderList(); renderInspector(); renderCanvas();
}})();
</script>
</body>
</html>"""


if __name__ == "__main__":                 # 공개판: schema_ontology.json → erd-viewer.html 만 다시 만든다
    import io as _io, json as _json, os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    with _io.open(_os.path.join(_here, "schema_ontology.json"), encoding="utf-8") as _f:
        _html = render_viewer_html(_json.load(_f))
    with _io.open(_os.path.join(_here, "erd-viewer.html"), "w", encoding="utf-8") as _f:
        _f.write(_html)
    print("erd-viewer.html 생성")
