# Ops Console — 공통 색 이름(토큰) · 위쪽 메뉴 규격 (정본)

> 2026-11-17 도입(2026-11-27 8개 탭 통합). 담당자용 화면(문의 데스크 · 대시보드 · 아카이브 · ERD · 지표 2종 · KPI 진단 성적표 · 트리니티봇 종합소개서)이 **같은 색 이름과 같은 위쪽 메뉴**를 쓴다.
> 각 화면은 단독 실행형이라 아래 CSS·마크업을 **각 파일(또는 그 생성기 · 템플릿)에 그대로 복사**한다 — 고칠 때는 이 문서를 먼저 고치고 각 화면에 옮긴다.
> 현업 화면(문의창 5179)에는 이 메뉴를 넣지 않는다(내부 도구 노출 금지).

## 1. 색 이름 (`<style id="oc-console">` 안에 그대로)

```css
:root{--oc-bg:#eef1f5;--oc-panel:#f4f6f9;--oc-card:#ffffff;--oc-ink:#18212c;--oc-sub:#5d6978;--oc-line:#d9dfe6;
  --oc-pt:#1a6fb5;--oc-pt-ink:#ffffff;--oc-chip:#eef1f5;--oc-warn-bg:#fff4dc;--oc-warn-ink:#7a5200;
  --oc-good:#1f7a33;--oc-good-bg:#e8f6ea;--oc-bad:#b3261e;--oc-bad-bg:#fdecea;--oc-nav:#18212c;--oc-nav-ink:#e7ecf2;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--oc-bg:#0f141a;--oc-panel:#151b23;--oc-card:#1c242e;--oc-ink:#e7ecf2;--oc-sub:#9aa7b6;--oc-line:#2b3542;
  --oc-pt:#5b9fe0;--oc-pt-ink:#0b1220;--oc-chip:#232d39;--oc-warn-bg:#3a2e12;--oc-warn-ink:#f3d58a;
  --oc-good:#6fd19c;--oc-good-bg:#17291f;--oc-bad:#ff8a80;--oc-bad-bg:#3a1d1b;--oc-nav:#0b1016;--oc-nav-ink:#e7ecf2;color-scheme:dark}}
.oc-nav{display:flex;align-items:center;gap:2px;height:44px;padding:0 10px;background:var(--oc-nav);color:var(--oc-nav-ink);
  font:13px/1 "Malgun Gothic","맑은 고딕",system-ui,sans-serif;overflow-x:auto;scrollbar-width:none;position:relative;z-index:500}
.oc-nav b{margin:0 10px 0 4px;white-space:nowrap;font-size:13.5px}
.oc-nav a{color:inherit;text-decoration:none;padding:8px 10px;border-radius:8px;white-space:nowrap;opacity:.78}
.oc-nav a:hover{opacity:1;background:rgba(255,255,255,.08)}
.oc-nav a[aria-current="page"]{opacity:1;font-weight:700;background:rgba(255,255,255,.16)}
.oc-nav a:focus-visible{outline:2px solid #5b9fe0;outline-offset:-2px;opacity:1}
```

- 다크 모드는 기기 설정(`prefers-color-scheme`)을 따른다. `<html data-theme="light">` 이면 밝게 고정.
- 화면 고유 색을 다크 모드에 맞출 때는 이 변수(`var(--oc-…)`)를 쓴다.

## 2. 위쪽 메뉴 (`<body>` 바로 다음, 화면의 다른 머리말보다 위)

```html
<nav class="oc-nav" aria-label="Ops Console" data-noexport>
  <b>Ops Console</b>
  <a href="http://127.0.0.1:5177/" data-hub="/">문의 데스크</a>
  <a href="{R}task-dashboard/task-board.html" data-hub="/board">대시보드</a>
  <a href="{R}archive.html" data-hub="/archive">아카이브</a>
  <a href="{R}erd-graph/erd-viewer.html" data-hub="/erd">ERD·온톨로지</a>
  <a href="{R}task-dashboard/bot-metrics.html" data-hub="/bot-metrics">봇 지표</a>
  <a href="{R}task-dashboard/metrics.html" data-hub="/metrics">협업 지표</a>
</nav>
<script>/* Ops Console 메뉴: 문의 데스크 서버(5177)로 열었으면 서버 주소로, 파일로 열었으면 파일 상대 경로로 */
(function(){var h=location.host;var hub=["127.0.0.1:5177","localhost:5177","127.0.0.1:5187","localhost:5187"].indexOf(h)>=0;
var p=decodeURIComponent(location.pathname).replace(/\\/g,"/");var inDiag=!hub&&/\/diagnostics\/[^/]+\.html$/i.test(p);
function rw(root){root.querySelectorAll("a[data-hub]").forEach(function(a){var dh=a.getAttribute("data-hub");if(hub){if(!dh){a.hidden=true;return;}a.setAttribute("href",dh);}});}
rw(document);if(hub)document.addEventListener("DOMContentLoaded",function(){rw(document);});
document.querySelectorAll(".oc-nav a[data-hub]").forEach(function(a){var dh=a.getAttribute("data-hub");
var me=hub?location.pathname:p.split("/").pop();var t=hub?dh:a.getAttribute("href").split("/").pop();
if(me===t||(hub&&me==="/"&&t==="/")||(hub&&dh==="/kpi"&&me.indexOf("/diagnostics/")===0)||(inDiag&&dh==="/kpi")||(!hub&&me==="desk.html"&&dh==="/"))a.setAttribute("aria-current","page");});})();</script>
```

- `{R}` = 그 화면 파일에서 아카이브 루트까지의 상대 경로: `archive.html` · `trinity_overview.html` → 빈 문자열, `task-dashboard/*.html` · `erd-graph/erd-viewer.html` → `../`, `task-dashboard/desk/desk.html` · `task-dashboard/diagnostics/*.html` → `../../`.
- 대시보드는 💾저장 때 화면째 저장하므로 메뉴에 `data-noexport` 를 붙여도 **저장본에 남지 않는다** → 대시보드에서는 `data-noexport` 를 **빼고** 넣는다.
- 문의 데스크(5177)가 허브: `/board` `/archive` `/erd` `/bot-metrics` `/metrics` `/kpi` `/overview` (및 `/diagnostics/*.html`) 를 **읽기 전용**으로 내준다(이 PC 전용 127.0.0.1). 허브가 못 내주는 화면은 메뉴에서 그 항목에 `data-hub=""` 를 두면 허브에서 숨긴다.
