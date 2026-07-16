# -*- coding: utf-8 -*-
"""make_signals.py — 신호이력 HTML 생성 (signals_history.html)

데이터 소스:
  1. watch_history.csv / us_watch_history.csv   — 날짜별 워치 후보 (신호수 기준)
  2. DB signals 테이블                           — 슈퍼시그널 / DART 공시 신호
  3. DB disclosures 테이블                       — notified 공시

출력: signals_history.html (BASE 기준)
규칙:
  - 신호수 3+ 종목 표시 (5+ 는 강조). 하루 상위 5종목 기본 노출, 나머지 <details> 접기
  - 워치리스트(watchlist.txt) 종목은 신호수 무관 최상단 📌 표시
  - 성과 추적: 발생가 → 최신가 (수익률%) — 종가 캐시, 렌더링 중 API 호출 없음
  - 최근 60일
  - JS 필터: KR / US / 전체 / 기간(10일·30일·전체)
  - 어떤 오류도 크래시 금지
"""

import os
import csv
import json
import datetime as dt
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))

CSS = "daekil_style.css"
JS_THEME = "daekil_theme.js"
TOP_N = 5  # 기본 노출 종목 수 (나머지 <details>)


def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── 워치리스트 로드 ──
def load_watchlist():
    """watchlist.txt → set of 6-digit ticker strings."""
    path = os.path.join(BASE, "watchlist.txt")
    tickers = set()
    if not os.path.exists(path):
        return tickers
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line and line.isdigit() and len(line) == 6:
                    tickers.add(line)
    except Exception:
        pass
    return tickers


# ── CSV 로드 ──
def load_watch_csv(fname):
    """watch_history.csv → [{date, ticker, name, sector, signals(int), price, off_high, market}]"""
    path = os.path.join(BASE, fname)
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        "date":     row.get("date", ""),
                        "ticker":   row.get("ticker", ""),
                        "name":     row.get("name", "") or row.get("ticker", ""),
                        "sector":   row.get("sector", ""),
                        "signals":  int(row.get("signals", 0) or 0),
                        "price":    float(row.get("price", 0) or 0),
                        "off_high": float(row.get("off_high_pct", 0) or 0),
                        "market":   "KR" if fname.startswith("watch") else "US",
                    })
                except Exception:
                    pass
    except Exception:
        pass
    return rows


# ── 성과 캐시: ticker → (latest_date, latest_price) ──
def build_price_cache(kr_rows, us_rows):
    """각 ticker의 가장 최신 날짜·종가를 반환. 렌더링 중 API 호출 없음."""
    cache = {}  # ticker → (latest_date_str, price, market)
    for r in (kr_rows + us_rows):
        ticker = r.get("ticker")
        d = r.get("date", "")
        p = r.get("price") or 0
        if ticker and p > 0:
            if ticker not in cache or d > cache[ticker][0]:
                cache[ticker] = (d, float(p), r.get("market", ""))
    return cache


# ── DB 로드 ──
def load_db_signals():
    try:
        from db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT signal_date, stock_code, signal_type, detail, price_at,"
                " graded, return_20d, return_5d, alpha_20d "
                "FROM signals ORDER BY signal_date DESC LIMIT 200"
            ).fetchall()
            return [{"date": r[0], "ticker": r[1], "signal_type": r[2],
                     "detail": r[3] or "", "price": r[4],
                     "graded": r[5], "return_20d": r[6],
                     "return_5d": r[7], "alpha_20d": r[8]} for r in rows]
    except Exception:
        return []


def load_db_disclosures():
    try:
        from db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT rcept_dt, stock_code, disc_type, summary "
                "FROM disclosures WHERE notified=1 ORDER BY rcept_dt DESC LIMIT 100"
            ).fetchall()
            return [{"date": r[0], "ticker": r[1], "disc_type": r[2], "summary": r[3] or ""}
                    for r in rows]
    except Exception:
        return []


# ── 날짜별 집계 ──
def group_by_date(rows):
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=60)).isoformat()
    grouped = defaultdict(list)
    for r in rows:
        d = str(r.get("date", ""))[:10]
        if d >= cutoff:
            grouped[d].append(r)
    return dict(sorted(grouped.items(), reverse=True))


def signal_icon(n):
    if n >= 7:  return "🔥"
    if n >= 5:  return "⭐"
    if n >= 3:  return "💡"
    return "·"


def sector_badge(sector):
    colors = {
        "외국인순매수": "blue", "기관순매수": "green", "연기금순매수": "amber",
        "돌파A": "red", "관심": "amber",
    }
    c = colors.get(sector, "blue")
    return f'<span class="badge badge-{c}">{esc(sector)}</span>' if sector else ""


def _perf_html(r, price_cache):
    """발생가 → 현재가 수익률 HTML. 최신 날짜면 숨김."""
    try:
        ticker = r.get("ticker")
        sig_price = r.get("price") or 0
        cached = price_cache.get(ticker)
        if not cached or not sig_price:
            return ""
        latest_date, latest_price, _ = cached
        if r.get("date", "") >= latest_date:
            return ""  # 오늘 데이터 → 비교 의미 없음
        if sig_price <= 0 or latest_price <= 0:
            return ""
        ret = (latest_price - sig_price) / sig_price * 100.0
        ret_cls = "c-up" if ret >= 0 else "c-down"
        mkt = r.get("market", "")
        if mkt == "KR":
            cur_str = f"{latest_price:,.0f}원"
        else:
            cur_str = f"${latest_price:,.2f}"
        return f' <span class="{ret_cls}" style="font-size:11px">→{cur_str} ({ret:+.1f}%)</span>'
    except Exception:
        return ""


def _render_item(r, watchlist, price_cache, dim_class=""):
    """종목 1개 → timeline-item HTML."""
    sig_n = r["signals"]
    is_watch = r["ticker"] in watchlist
    pin = "📌 " if is_watch else ""
    dim = f' class="stale{dim_class}"' if sig_n < 5 and not is_watch else (f' class="{dim_class.strip()}"' if dim_class.strip() else "")
    mkt = "🇰🇷" if r["market"] == "KR" else "🇺🇸"
    icon = "📌" if is_watch else signal_icon(sig_n)
    price_str = (f"{r['price']:,.0f}원" if r["market"] == "KR" and r["price"]
                 else f"${r['price']:,.2f}" if r["market"] == "US" and r["price"]
                 else "")
    high_str = (f" · 고점대비 {r['off_high']:+.1f}%" if r["off_high"] else "")
    perf = _perf_html(r, price_cache)
    return f"""
<div class="timeline-item"{dim}>
  <div class="tl-icon">{icon}</div>
  <div class="tl-body">
    <div class="tl-title">{mkt} {pin}{esc(r['name'])} <span class="c-muted fs-12">({esc(r['ticker'])})</span>
      {sector_badge(r['sector'])}</div>
    <div class="tl-sub">신호 {sig_n}개{(" · " + price_str) if price_str else ""}{high_str}{perf}</div>
  </div>
</div>"""


# ── HTML 렌더 ──
def render_html(kr_grouped, us_grouped, db_signals, db_disc, watchlist, price_cache):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    today = dt.date.today().isoformat()

    all_dates = sorted(set(list(kr_grouped.keys()) + list(us_grouped.keys())), reverse=True)
    n_dates = len(all_dates)

    # ── DB 신호 섹션 ──
    items_html = ""
    for s in db_signals[:20]:
        graded = s.get("graded", 0)
        ret20  = s.get("return_20d")
        ret5   = s.get("return_5d")
        alp20  = s.get("alpha_20d")
        if graded and ret20 is not None:
            ret_cls = "c-up" if ret20 >= 0 else "c-down"
            alp_str = f" 알파 {alp20:+.1f}%" if alp20 is not None else ""
            grade_badge = (
                f' <span class="{ret_cls}" style="font-size:11px">'
                f'✅ 20일 {ret20:+.1f}%{alp_str}'
                + (f" (5일 {ret5:+.1f}%)" if ret5 is not None else "")
                + "</span>"
            )
        else:
            grade_badge = ' <span style="font-size:11px;color:var(--text-muted)">⏳ 채점 대기</span>'
        items_html += f"""
<div class="timeline-item">
  <div class="tl-icon">⚡</div>
  <div class="tl-body">
    <div class="tl-date">{esc(s['date'])}</div>
    <div class="tl-title">[{esc(s['ticker'])}] {esc(s['signal_type'])}{grade_badge}</div>
    <div class="tl-sub">{esc(s['detail'])}</div>
  </div>
</div>"""
    for d in db_disc[:20]:
        items_html += f"""
<div class="timeline-item">
  <div class="tl-icon">📣</div>
  <div class="tl-body">
    <div class="tl-date">{esc(d['date'])}</div>
    <div class="tl-title">[{esc(d['ticker'])}] {esc(d['disc_type'])}</div>
    <div class="tl-sub">{esc(d['summary'])}</div>
  </div>
</div>"""
    if items_html:
        db_section = f"""
<section>
  <div class="section-q">⚡ 슈퍼시그널 / 공시 알림</div>
  <div class="card">{items_html}</div>
</section>"""
    else:
        db_section = """
<section>
  <div class="section-q">⚡ 슈퍼시그널 / 공시 알림</div>
  <div class="card"><div class="empty-state">아직 슈퍼시그널·공시 알림이 없습니다.</div></div>
</section>"""

    # ── 워치 타임라인 ──
    timeline_html = ""
    for d in all_dates[:45]:
        kr_rows = kr_grouped.get(d, [])
        us_rows = us_grouped.get(d, [])
        all_rows = kr_rows + us_rows

        # 신호 3+ 필터, 워치리스트 우선 정렬
        eligible = [r for r in all_rows if r["signals"] >= 3]
        if not eligible:
            continue

        def sort_key(r):
            return (r["ticker"] not in watchlist, -r["signals"])  # watchlist 먼저, 그다음 신호수 내림

        eligible.sort(key=sort_key)
        top = eligible[:TOP_N]
        rest = eligible[TOP_N:]

        try:
            dobj = dt.date.fromisoformat(d)
            weekday = ["월", "화", "수", "목", "금", "토", "일"][dobj.weekday()]
            d_label = f"{dobj.month}/{dobj.day}({weekday})"
        except Exception:
            d_label = d

        is_today = (d == today)
        date_cls = ' style="color:var(--amber);font-weight:800"' if is_today else ""
        has_kr = 1 if kr_rows else 0
        has_us = 1 if us_rows else 0

        top_html = "".join(_render_item(r, watchlist, price_cache) for r in top)
        rest_html = ""
        if rest:
            rest_items = "".join(_render_item(r, watchlist, price_cache) for r in rest)
            rest_html = f"""
<details style="margin-top:4px">
  <summary class="c-muted fs-12" style="cursor:pointer;padding:4px 0">{len(rest)}개 더 보기…</summary>
  {rest_items}
</details>"""

        timeline_html += f"""
<div class="date-group" data-date="{d}"
     data-kr="{has_kr}" data-us="{has_us}">
  <div class="section-q"{date_cls}>{d_label}
    <span class="c-muted fs-12"> · KR {len(kr_rows)}종 / US {len(us_rows)}종</span>
  </div>
  <div class="card" style="padding:10px 14px">{top_html}{rest_html}</div>
</div>"""

    if not timeline_html:
        timeline_html = '<div class="miss">최근 60일 내 신호 데이터가 없습니다.</div>'

    # ── 통계 배너 ──
    total_kr = sum(len(v) for v in kr_grouped.values())
    total_us = sum(len(v) for v in us_grouped.values())
    strong_kr = sum(1 for rows in kr_grouped.values() for r in rows if r["signals"] >= 5)
    strong_us = sum(1 for rows in us_grouped.values() for r in rows if r["signals"] >= 5)

    stats_html = f"""
<div class="signal-row">
  <div class="signal-badge">
    <span class="signal-icon">🇰🇷</span>
    <div class="signal-info">
      <span class="signal-label">국내 누적 (60일)</span>
      <span class="signal-value">{total_kr}건 · 강신호 {strong_kr}</span>
    </div>
  </div>
  <div class="signal-badge">
    <span class="signal-icon">🇺🇸</span>
    <div class="signal-info">
      <span class="signal-label">미국 누적 (60일)</span>
      <span class="signal-value">{total_us}건 · 강신호 {strong_us}</span>
    </div>
  </div>
  <div class="signal-badge">
    <span class="signal-icon">📅</span>
    <div class="signal-info">
      <span class="signal-label">확인 거래일</span>
      <span class="signal-value">{n_dates}일</span>
    </div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>신호이력 — 대길스톡</title>
<link rel="stylesheet" href="{CSS}">
<style>
.badge {{ display:inline-block; font-size:10px; font-weight:600;
         padding:1px 6px; border-radius:4px; margin-left:4px; }}
.badge-blue  {{ background:var(--blue-bg);  color:var(--blue); }}
.badge-green {{ background:var(--green-bg); color:var(--green); }}
.badge-amber {{ background:var(--amber-bg); color:var(--amber); }}
.badge-red   {{ background:var(--red-bg);   color:var(--red); }}
.filter-bar {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; }}
.filter-btn {{
  padding:6px 14px; border-radius:20px; border:1px solid var(--border);
  background:var(--card); color:var(--text-muted); font-size:12px;
  cursor:pointer; user-select:none;
}}
.filter-btn.active {{ background:var(--blue-bg); color:var(--blue);
                      border-color:var(--blue); }}
details summary {{ list-style:none; }}
details summary::-webkit-details-marker {{ display:none; }}
</style>
</head>
<body>
<div class="page-header">
  <div class="page-title">📡 신호이력</div>
  <div class="page-meta">생성: {now} · 상위 {TOP_N}종 기본 표시 · 📌=워치리스트 · 성과 추적 포함</div>
</div>

{stats_html}

{db_section}

<div class="section-q">📋 워치 후보 타임라인</div>

<div class="filter-bar">
  <span class="filter-btn active" onclick="filterMkt(this,'all')">전체</span>
  <span class="filter-btn" onclick="filterMkt(this,'kr')">🇰🇷 국내</span>
  <span class="filter-btn" onclick="filterMkt(this,'us')">🇺🇸 미국</span>
  <span style="margin-left:8px"></span>
  <span class="filter-btn active" onclick="filterDays(this,10)">10일</span>
  <span class="filter-btn" onclick="filterDays(this,30)">30일</span>
  <span class="filter-btn" onclick="filterDays(this,0)">전체</span>
</div>

<div id="timeline">
{timeline_html}
</div>

<div class="page-footer">대길스톡 신호이력 · {now}</div>

<script src="{JS_THEME}"></script>
<script>
var _mkt = 'all', _days = 10;

function filterMkt(btn, mkt) {{
  _mkt = mkt;
  document.querySelectorAll('.filter-bar .filter-btn').forEach(function(b) {{
    if (b.onclick && b.onclick.toString().includes("filterMkt")) b.classList.remove('active');
  }});
  btn.classList.add('active');
  applyFilter();
}}

function filterDays(btn, days) {{
  _days = days;
  document.querySelectorAll('.filter-bar .filter-btn').forEach(function(b) {{
    if (b.onclick && b.onclick.toString().includes("filterDays")) b.classList.remove('active');
  }});
  btn.classList.add('active');
  applyFilter();
}}

function applyFilter() {{
  var today = new Date();
  var groups = document.querySelectorAll('.date-group');
  groups.forEach(function(g) {{
    var d = new Date(g.dataset.date);
    var diffDays = Math.round((today - d) / 86400000);
    var dayOk = (_days === 0) || (diffDays <= _days);
    var mktOk = (_mkt === 'all') ||
                (_mkt === 'kr' && g.dataset.kr === '1') ||
                (_mkt === 'us' && g.dataset.us === '1');
    g.style.display = (dayOk && mktOk) ? '' : 'none';
  }});
}}

applyFilter();
</script>
</body>
</html>"""


def main():
    kr_rows = load_watch_csv("watch_history.csv")
    us_rows = load_watch_csv("us_watch_history.csv")
    db_signals = load_db_signals()
    db_disc = load_db_disclosures()
    watchlist = load_watchlist()
    price_cache = build_price_cache(kr_rows, us_rows)

    kr_grouped = group_by_date(kr_rows)
    us_grouped = group_by_date(us_rows)

    html = render_html(kr_grouped, us_grouped, db_signals, db_disc, watchlist, price_cache)
    out = os.path.join(BASE, "signals_history.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK: {out}")


if __name__ == "__main__":
    main()
