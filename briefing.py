"""브리핑 모듈 — 아침(07:40) / 저녁(18:40) 메시지 조립·발송."""
import logging
import os
from datetime import date, datetime, timedelta
from collections import defaultdict

import yfinance as yf

from db import get_conn
from flow_watch import run_flow_watch
from pension_watch import run_pension_watch
from telegram_push import send_message, send_photo
from watchlist import load_watchlist

logger = logging.getLogger(__name__)


def _fear_greed_line(short: bool = True) -> str:
    """오늘 공포탐욕지수 한 줄. 실패하거나 데이터 없으면 빈 문자열."""
    try:
        from fear_greed import briefing_line
        return briefing_line(short=short)
    except Exception as exc:
        logger.warning("fear_greed 브리핑 라인 실패: %s", exc)
        return ""


# ── 에러 카운터 (연속 실패 감지, 2회 시 별도 경고) ──
_error_counts: dict = defaultdict(int)

def _safe(section_name: str, func, fallback="오늘 없음 ✅"):
    """섹션 단위 try/except — 실패해도 브리핑 발송은 계속."""
    global _error_counts
    try:
        result = func()
        _error_counts[section_name] = 0
        return result
    except Exception as exc:
        _error_counts[section_name] += 1
        count = _error_counts[section_name]
        logger.warning("[%s] 조회 실패 (%s)", section_name, exc)
        warn_msg = f"⚠️ {section_name} 조회 실패 ({type(exc).__name__}) — 해당 섹션 누락"
        if count >= 2:
            try:
                send_message(f"🚨 {section_name} {count}회 연속 실패\n{exc}", msg_type="error_alert")
            except Exception:
                pass
        if isinstance(fallback, str):
            return fallback + f"\n{warn_msg}"
        logger.warning(warn_msg)
        return fallback


# ── matplotlib 전역 다크테마 (HTML과 동일 팔레트) ──
def _apply_mpl_dark():
    try:
        import matplotlib
        matplotlib.rcParams.update({
            "figure.facecolor":  "#0f1115",
            "axes.facecolor":    "#1a1d24",
            "axes.edgecolor":    "#262b36",
            "axes.labelcolor":   "#e8eaed",
            "xtick.color":       "#8b93a7",
            "ytick.color":       "#8b93a7",
            "text.color":        "#e8eaed",
            "grid.color":        "#262b36",
            "grid.alpha":        0.5,
            "font.family":       "Malgun Gothic",
            "axes.spines.top":   False,
            "axes.spines.right": False,
            "axes.unicode_minus": False,   # 마이너스 기호 깨짐 방지
        })
    except Exception:
        pass

_apply_mpl_dark()

# ──────────────────────────────────────────────
# 매크로 캘린더 (월별 하드코딩, 필요 시 갱신)
# ──────────────────────────────────────────────
MACRO_EVENTS: dict[str, list[str]] = {
    "2026-07-15": ["美 소매판매 (6월)"],
    "2026-07-16": ["美 산업생산 (6월)"],
    "2026-07-23": ["한은 금통위"],
    "2026-07-28": ["美 FOMC 결정"],
    "2026-07-30": ["美 GDP 속보치 (Q2)"],
    "2026-08-01": ["美 고용보고서 (7월)"],
    "2026-08-13": ["美 CPI (7월)"],
}


def _today_and_week_events() -> list[str]:
    today = date.today()
    events = []
    for day in range(7):
        dt = today + timedelta(days=day)
        key = dt.isoformat()
        if key in MACRO_EVENTS:
            label = "오늘" if dt == today else dt.strftime("%m/%d")
            for ev in MACRO_EVENTS[key]:
                events.append(f"  {label}: {ev}")
    return events


# ──────────────────────────────────────────────
# yfinance 데이터 조회
# ──────────────────────────────────────────────
_YF_TICKERS = {
    "S&P500": "^GSPC",
    "나스닥": "^IXIC",
    "필라반도": "^SOX",
    "달러/원": "KRW=X",
    "미10년물": "^TNX",
}


def _fetch_us_market() -> dict[str, tuple[float, float]]:
    """전날 종가와 전전날 종가를 비교해 등락률 반환. {이름: (종가, 등락률%)}"""
    result = {}
    for name, ticker in _YF_TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist is None or len(hist) < 2:
                continue
            prev_close = float(hist["Close"].iloc[-2])
            last_close = float(hist["Close"].iloc[-1])
            pct = (last_close - prev_close) / prev_close * 100
            result[name] = (last_close, pct)
        except Exception as exc:
            logger.warning("yfinance 조회 실패 (%s): %s", ticker, exc)
    return result


# ──────────────────────────────────────────────
# pykrx 국장 요약
# ──────────────────────────────────────────────
def _fetch_kr_market(trade_date: str) -> dict:
    try:
        from pykrx import stock as krx
        kospi = krx.get_index_ohlcv_by_date(trade_date, trade_date, "1001")
        kosdaq = krx.get_index_ohlcv_by_date(trade_date, trade_date, "2001")
        inv_kospi = krx.get_market_trading_value_by_date(trade_date, trade_date, "KOSPI")
        inv_kosdaq = krx.get_market_trading_value_by_date(trade_date, trade_date, "KOSDAQ")

        def idx_pct(df):
            if df is None or df.empty:
                return None, None
            row = df.iloc[0]
            close = float(row.get("종가", 0))
            open_ = float(row.get("시가", close))
            pct = (close - open_) / open_ * 100 if open_ else 0
            return close, pct

        def inv_sum(df):
            if df is None or df.empty:
                return None, None
            row = df.iloc[0]
            return float(row.get("외국인합계", 0)), float(row.get("기관합계", 0))

        kp_close, kp_pct = idx_pct(kospi)
        kq_close, kq_pct = idx_pct(kosdaq)
        kp_f, kp_i = inv_sum(inv_kospi)
        kq_f, kq_i = inv_sum(inv_kosdaq)

        return {
            "kospi": (kp_close, kp_pct),
            "kosdaq": (kq_close, kq_pct),
            "foreign_kospi": kp_f,
            "inst_kospi": kp_i,
            "foreign_kosdaq": kq_f,
            "inst_kosdaq": kq_i,
        }
    except Exception as exc:
        logger.warning("pykrx 국장 조회 실패: %s", exc)
        return {}


# ──────────────────────────────────────────────
# 오늘 공시 요약 (DB 조회)
# ──────────────────────────────────────────────
def _today_disc_summary() -> list[str]:
    today = date.today().strftime("%Y%m%d")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT stock_code, disc_type, summary
               FROM disclosures
               WHERE rcept_dt = ? AND notified = 1
               ORDER BY rcept_dt DESC LIMIT 10""",
            (today,),
        ).fetchall()
    return [f"  [{r['stock_code']}] {r['disc_type']}: {r['summary'] or '(내용 없음)'}" for r in rows]


# ──────────────────────────────────────────────
# 오늘 슈퍼시그널 요약 (DB 조회)
# ──────────────────────────────────────────────
def _today_super_signals() -> list[str]:
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.stock_code, s.detail
               FROM signals s
               WHERE s.signal_date = ? AND s.signal_type = '슈퍼시그널'""",
            (today,),
        ).fetchall()
    if not rows:
        return []
    lines = []
    try:
        wl = load_watchlist()
        code_to_name = dict(zip(wl["종목코드"], wl["종목명"]))
    except Exception:
        code_to_name = {}
    for r in rows:
        name = code_to_name.get(r["stock_code"], r["stock_code"])
        lines.append(f"  🔥 [{name}] {r['detail']}")
    return lines


# ──────────────────────────────────────────────
# 포맷 헬퍼
# ──────────────────────────────────────────────
def _pct_str(pct: float | None) -> str:
    if pct is None:
        return "N/A"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def _won_str(val: float) -> str:
    """억 단위로 표시."""
    billion = val / 1e8
    sign = "+" if billion >= 0 else ""
    return f"{sign}{billion:,.0f}억"


# ──────────────────────────────────────────────
# 브리핑 조립
# ──────────────────────────────────────────────
def _is_trading_day() -> bool:
    """오늘 KRX 거래일 여부. pykrx 조회 → 실패 시 평일로 fallback."""
    try:
        from pykrx import stock as krx
        today = date.today().strftime("%Y%m%d")
        df = krx.get_index_ohlcv_by_date(today, today, "1001")
        return df is not None and not df.empty
    except Exception:
        return date.today().weekday() < 5


def _next_trading_day() -> str:
    try:
        from pykrx import stock as krx
        d = date.today() + timedelta(days=1)
        for _ in range(10):
            s = d.strftime("%Y%m%d")
            df = krx.get_index_ohlcv_by_date(s, s, "1001")
            if df is not None and not df.empty:
                return d.strftime("%m/%d")
            d += timedelta(days=1)
    except Exception:
        pass
    return "(확인 필요)"


def _market_temperature(us: dict) -> str:
    """미장 전체 분위기 한 줄 — S&P500 기준."""
    sp = us.get("S&P500")
    if not sp:
        return ""
    close, pct = sp
    if pct >= 1.5:
        return "미장 강세. 위험자산 선호 🔥"
    if pct >= 0.3:
        return "미장 소폭 상승. 무난한 하루 🙂"
    if pct >= -0.3:
        return "미장 보합. 눈치 보는 중 😐"
    if pct >= -1.5:
        return "미장 소폭 하락. 주의 😟"
    return "미장 급락. 변동성 주의 ⚠️"


def _kr_evening_oneliner(kr: dict) -> str:
    """저녁 한 줄 결론 — KOSPI 방향 + 외인 수급 + 공포탐욕 온도."""
    kp_close, kp_pct = kr.get("kospi", (None, None))
    if kp_pct is None:
        return ""
    kp_f = kr.get("foreign_kospi")  # None = 장중/미집계

    if kp_pct >= 1.5:
        direction = "국장 강세"
    elif kp_pct >= 0.3:
        direction = "국장 소폭 상승"
    elif kp_pct >= -0.3:
        direction = "국장 보합"
    elif kp_pct >= -1.5:
        direction = "국장 소폭 하락"
    else:
        direction = "국장 급락"

    f_str = "외인 미집계" if kp_f is None else f"외인 {_won_str(kp_f)}"

    try:
        from fear_greed import get_today, score_to_grade
        fg = get_today()
        fg_str = f"공포탐욕 {fg['score']}도({score_to_grade(fg['score'])})" if fg else ""
    except Exception:
        fg_str = ""

    parts = [f"{direction}({_pct_str(kp_pct)})", f_str]
    if fg_str:
        parts.append(fg_str)
    return " / ".join(parts)


def _position_warnings() -> list[str]:
    """position_summary.json에서 오늘 경고·이상신호 라인 추출."""
    import json
    path = os.path.join(os.path.dirname(__file__), "position_summary.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        feed_date = data.get("date", "")
        today_iso = date.today().isoformat()
        if feed_date != today_iso:
            return [f"  ⏳ position_flow 미실행 — {feed_date or '날짜 불명'} 기준 (오늘 재실행 필요)"]
        lines_out = []
        for line in data.get("lines", []):
            lines_out.append(f"  {line}")
        if not lines_out:
            status = data.get("status", "")
            headline = data.get("headline", "")
            if "🟢" in status or not headline:
                lines_out.append("  ✅ 보유종목 이상신호 없음")
            else:
                lines_out.append(f"  {headline}")
        return lines_out
    except FileNotFoundError:
        return ["  ⚠️ position_flow 미실행 (파일 없음)"]
    except Exception as exc:
        return [f"  (오류: {exc})"]


def _pension_weighted_return(us: dict) -> str:
    """연금 가중 합산 등락. 하드코딩 비중 (나스닥40/S&P30/필라반도20/금10)."""
    weights = {"나스닥": 0.40, "S&P500": 0.30, "필라반도": 0.20}
    total, n = 0.0, 0
    for name, w in weights.items():
        if name in us:
            total += us[name][1] * w
            n += 1
    if n == 0:
        return ""
    sign = "+" if total >= 0 else ""
    mood = "😊" if total > 0.5 else ("😐" if total >= -0.5 else "😟")
    return f"→ 연금 가중 합산 약 {sign}{total:.1f}% {mood}"


def _make_morning_chart(us: dict) -> str | None:
    """미장 등락 막대 차트 PNG → 임시 파일 경로 반환. 실패 시 None."""
    try:
        import matplotlib.pyplot as plt
        import tempfile

        labels, values, colors = [], [], []
        order = ["S&P500", "나스닥", "필라반도", "달러/원", "미10년물"]
        for name in order:
            if name not in us:
                continue
            _, pct = us[name]
            labels.append(name)
            values.append(pct)
            colors.append("#f04452" if pct >= 0 else "#3182f6")

        if not labels:
            return None

        fig, ax = plt.subplots(figsize=(6, 3.2))
        bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1],
                       height=0.55, zorder=3)
        ax.axvline(0, color="#5d6578", linewidth=0.8)
        ax.set_xlabel("등락(%)", fontsize=10)
        ax.grid(axis="x", zorder=0, alpha=0.3)

        for bar, val in zip(bars, values[::-1]):
            ax.text(val + (0.06 if val >= 0 else -0.06), bar.get_y() + bar.get_height() / 2,
                    f"{'+' if val >= 0 else ''}{val:.2f}%",
                    va="center", ha="left" if val >= 0 else "right",
                    fontsize=9, color="#e8eaed")

        today_str = date.today().strftime("%m/%d")
        ax.set_title(f"미장 마감 ({today_str})", fontsize=12, fontweight="bold",
                     color="#e8eaed", pad=8)
        fig.tight_layout()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        fig.savefig(tmp.name, dpi=130, bbox_inches="tight",
                    facecolor="#0f1115", edgecolor="none")
        plt.close(fig)
        return tmp.name
    except Exception as exc:
        logger.warning("아침 차트 생성 실패: %s", exc)
        return None


def _load_kr_holdings() -> list[tuple[str, str]]:
    """holdings.txt → [(code6, name), ...] KR 종목만. pykrx로 종목명 해결."""
    holdings_path = os.path.join(os.path.dirname(__file__), "holdings.txt")
    codes = []
    try:
        with open(holdings_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                tk = line.split()[0]
                if tk.isdigit() and len(tk) == 6:
                    codes.append(tk)
    except FileNotFoundError:
        return []
    if not codes:
        return []
    try:
        from pykrx import stock as krx
        result = []
        for code in codes:
            try:
                name = krx.get_market_ticker_name(code) or code
                result.append((code, name[:6]))
            except Exception:
                result.append((code, code))
        return result
    except Exception:
        return [(c, c) for c in codes]


def _fetch_holdings_chart_data(trade_date: str) -> list[dict]:
    """보유 KR 종목의 당일 등락률(%) + 외인 순매수(원) 조회. 실패 종목은 건너뜀."""
    holdings = _load_kr_holdings()
    if not holdings:
        return []
    try:
        from pykrx import stock as krx
        import time as _time
        from datetime import timedelta as _td
        # 등락률 계산용: 당일 포함 최근 5거래일 조회
        td = date.today()
        prev_str = (td - _td(days=10)).strftime("%Y%m%d")
        results = []
        for code, name in holdings:
            try:
                ohlcv = krx.get_market_ohlcv_by_date(prev_str, trade_date, code)
                _time.sleep(0.15)
                pct = None
                if ohlcv is not None and not ohlcv.empty:
                    closes = ohlcv["종가"].dropna()
                    if len(closes) >= 2:
                        pct = (float(closes.iloc[-1]) - float(closes.iloc[-2])) / float(closes.iloc[-2]) * 100
                flow_df = krx.get_market_trading_value_by_date(trade_date, trade_date, code)
                _time.sleep(0.15)
                foreign_net = None  # None = API 미반환(마감 전·오류) / 0.0 = 실제 거래 없음
                if flow_df is not None and not flow_df.empty:
                    foreign_net = float(flow_df.iloc[0].get("외국인합계", 0))
                results.append({"code": code, "name": name, "pct": pct, "foreign_net": foreign_net})
            except Exception as exc:
                logger.warning("보유종목 차트 데이터 조회 실패 (%s): %s", code, exc)
        return results
    except Exception as exc:
        logger.warning("보유종목 차트 데이터 전체 실패: %s", exc)
        return []


def _evening_chart_interp(holdings_data: list, kr: dict) -> str:
    """보유종목 외인 수급 패턴 → 한 줄 해석 문장."""
    if not holdings_data:
        return "보유종목 수급 데이터 없음"
    # None = 마감 전·API 오류. 실제 수집된 것만 판단
    valid = [h for h in holdings_data if h["foreign_net"] is not None]
    if not valid:
        return "외인 수급 미집계 (마감 전 또는 API 준비 중)"
    buy   = [h for h in valid if h["foreign_net"] > 0]
    sell  = [h for h in valid if h["foreign_net"] < 0]
    total = len(valid)
    kp_f  = (kr or {}).get("foreign_kospi")  # None = 장중/미집계
    kp_f_str = _won_str(kp_f) if kp_f is not None else "미집계"
    if len(buy) == total:
        return f"외인 우주주 전종목 순매수 — KOSPI 외인 {kp_f_str}"
    if len(sell) == total:
        return f"외인 우주주 전종목 이탈 — KOSPI 외인 {kp_f_str}"
    if kp_f is not None and kp_f > 0 and len(sell) > len(buy):
        return f"외인 시장 순매수({_won_str(kp_f)}) — 우주주엔 아직 안 옴"
    if kp_f is not None and kp_f < 0 and len(buy) > len(sell):
        return f"외인 시장 이탈 중 — 우주주 {len(buy)}종목 선별 매수"
    if len(buy) > len(sell):
        top = buy[0]["name"]
        return f"외인 {top} 중심 순매수 ({len(buy)}/{total}종목)"
    if len(buy) == 0 and len(sell) == 0:
        return f"외인 우주주 전종목 거래 없음"
    return f"외인 우주주 혼조 ({len(buy)}매수/{len(sell)}매도)"


def _make_evening_chart(kr: dict, flow_results: list) -> str | None:
    """저녁 차트 PNG — 좌: 보유종목+지수 등락률 / 우: 보유종목 외인 순매수 (당일, 억원)."""
    try:
        import matplotlib.pyplot as plt
        import tempfile

        trade_date = date.today().strftime("%Y%m%d")
        today_str  = date.today().strftime("%m/%d")

        holdings_data = _fetch_holdings_chart_data(trade_date)

        # ── 좌 패널: KOSPI·KOSDAQ(회색) + 보유종목 등락률(색) ──
        left_names, left_vals, left_colors = [], [], []
        for key, label in [("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")]:
            pair = (kr or {}).get(key, (None, None))
            if pair[1] is not None:
                left_names.append(label)
                left_vals.append(pair[1])
                left_colors.append("#888888")  # 지수 = 회색 (보유종목과 구분)
        for h in holdings_data:
            if h["pct"] is not None:
                left_names.append(h["name"])
                left_vals.append(h["pct"])
                left_colors.append("#f04452" if h["pct"] >= 0 else "#3182f6")

        # ── 우 패널: 보유종목 외인 순매수 (당일, 억원) — None 항목 제외 ──
        right_items  = [(h["name"], h["foreign_net"] / 1e8) for h in holdings_data
                        if h["foreign_net"] is not None]
        right_names  = [x[0] for x in right_items]
        right_vals   = [x[1] for x in right_items]
        right_colors = ["#f04452" if v >= 0 else "#3182f6" for v in right_vals]

        has_left  = bool(left_names)
        has_right = bool(right_names)
        if not has_left and not has_right:
            return None

        n_rows = max(len(left_names), len(right_names), 2)
        fig_h  = max(3.5, 0.9 + n_rows * 0.46)
        ncols  = 2 if (has_left and has_right) else 1
        fig, axes = plt.subplots(
            1, ncols,
            figsize=(5.5 * ncols, fig_h),
            gridspec_kw={"width_ratios": [1, 1.1]} if ncols == 2 else None,
        )
        ax_left  = (axes[0] if ncols == 2 else axes) if has_left  else None
        ax_right = (axes[1] if ncols == 2 else axes) if has_right else None

        def _draw_bar(ax, names, vals, colors, title, xlabel=None):
            ax.barh(names[::-1], vals[::-1], color=colors[::-1], height=0.52)
            ax.axvline(0, color="#5d6578", linewidth=0.7)
            ax.set_title(title, fontsize=10, color="#e8eaed", pad=6)
            if xlabel:
                ax.set_xlabel(xlabel, fontsize=9, color="#8b93a7")
            for i, v in enumerate(vals[::-1]):
                offset = abs(max(vals + [0.1], key=abs)) * 0.04
                ax.text(v + (offset if v >= 0 else -offset), i,
                        f"{'+' if v>=0 else ''}{v:.2f}{'억' if xlabel else '%'}",
                        va="center", ha="left" if v >= 0 else "right",
                        fontsize=8.5, color="#e8eaed")
            ax.grid(axis="x", alpha=0.3)

        if ax_left and left_names:
            _draw_bar(ax_left, left_names, left_vals, left_colors,
                      f"보유종목 등락률 ({today_str}, %)")
        if ax_right and right_names:
            _draw_bar(ax_right, right_names, right_vals, right_colors,
                      "보유종목 외인 순매수 (당일, 억원)", xlabel="억원")

        # 한 줄 해석 (차트 상단)
        interp = _evening_chart_interp(holdings_data, kr)
        fig.suptitle(interp, fontsize=10, fontweight="bold", color="#f0c040", y=1.03)

        fig.tight_layout()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        fig.savefig(tmp.name, dpi=130, bbox_inches="tight",
                    facecolor="#0f1115", edgecolor="none")
        plt.close(fig)
        return tmp.name
    except Exception as exc:
        logger.warning("저녁 차트 생성 실패: %s", exc)
        return None


def morning_briefing() -> None:
    """평일 07:40 아침 브리핑 — 미장 마감 데이터 + 매크로 일정."""
    # ── 휴장일 처리 ──
    if not _is_trading_day():
        next_d = _next_trading_day()
        send_message(f"🏖️ 오늘 휴장. 다음 거래일 {next_d}", msg_type="morning_briefing")
        return

    today = date.today()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    us = _safe("미장데이터", _fetch_us_market, {})
    if not us:
        us = {}
    events = _today_and_week_events()

    temp = _market_temperature(us)
    pension_ret = _pension_weighted_return(us)

    # ── 헤더 ──
    lines = [
        f"☀️ <b>{today.month}/{today.day} 아침 브리핑</b>",
        "━━━━━━━━━━",
    ]

    if temp:
        lines.append(f"📌 한 줄: {temp}")

    fg = _fear_greed_line(short=True)
    if fg:
        lines.append(fg)

    # ── 내 연금 ──
    pension_items = []
    pension_map = {"나스닥": "나스닥100", "S&P500": "S&P500", "필라반도": "필라반도체"}
    for key, label in pension_map.items():
        if key in us:
            _, pct = us[key]
            em = "🟢" if pct >= 0 else "🔴"
            pension_items.append(f"· {label} {_pct_str(pct)} {em}")
    if "달러/원" not in us and not pension_items:
        pass
    else:
        lines.append("")
        lines.append("💰 <b>내 연금 어제 성적</b>")
        lines.extend(pension_items)
        if pension_ret:
            lines.append(pension_ret)

    # ── 미장 전체 ──
    lines.append("")
    lines.append("📊 <b>미장 마감</b>")
    for name, (close, pct) in us.items():
        pct_s = _pct_str(pct)
        emoji = "📈" if pct >= 0 else "📉"
        if name == "달러/원":
            lines.append(f"  {emoji} 환율 {close:,.0f}원 ({pct_s})")
        elif name == "미10년물":
            lines.append(f"  {emoji} 미10년물 {close:.3f}% ({pct_s})")
        else:
            lines.append(f"  {emoji} {name}: {close:,.2f} ({pct_s})")

    # ── 오늘 일정 ──
    today_evs = MACRO_EVENTS.get(today.isoformat(), [])
    lines.append("")
    lines.append("📅 <b>오늘 밤 일정</b>")
    if today_evs:
        for ev in today_evs:
            lines.append(f"· 🔴 {ev} — 결과 내일 아침 확인")
    elif events:
        lines.append("  특이 이벤트 없음")
        lines.append("<b>이번 주 남은 일정</b>")
        lines.extend(events[:3])
    else:
        lines.append("  특이 이벤트 없음 ✅")

    # ── 월간 신호 성적표 (매월 첫 거래일에만) ──
    try:
        from grade_signals import is_first_trading_day_of_month, monthly_report
        if is_first_trading_day_of_month():
            prev_month = today.replace(day=1) - timedelta(days=1)
            report_txt = monthly_report(prev_month.year, prev_month.month)
            if report_txt:
                lines.append("")
                lines.append(report_txt)
    except Exception as _e:
        logger.warning("월간 신호 성적표 실패: %s", _e)

    # ── 대시보드 링크 ──
    dash_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if os.path.exists(dash_path):
        lines.append("")
        lines.append(f"📊 <a href='file:///{dash_path.replace(chr(92), '/')}'>자세히 보기 →</a>")

    lines.append("━━━━━━━━━━")

    send_message("\n".join(lines), msg_type="morning_briefing")

    # ── 차트 이미지 첨부 ──
    chart_path = _safe("아침차트", lambda: _make_morning_chart(us), None)
    if chart_path:
        try:
            send_photo(chart_path,
                       caption=f"📊 미장 마감 {today.month}/{today.day}",
                       msg_type="morning_chart")
        finally:
            try:
                os.remove(chart_path)
            except Exception:
                pass

    logger.info("아침 브리핑 발송 완료")


def evening_briefing(test_mode: bool = False) -> None:
    """평일 18:40 저녁 브리핑 — 국장 확정 + 워치리스트 수급 + 공시 요약."""
    # ── 휴장일 처리 ──
    if not _is_trading_day():
        next_d = _next_trading_day()
        prefix = "⚠️ [TEST] " if test_mode else ""
        send_message(f"{prefix}🏖️ 오늘 휴장. 다음 거래일 {next_d}", msg_type="evening_briefing")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    trade_date = date.today().strftime("%Y%m%d")

    kr           = _safe("국장데이터",    lambda: _fetch_kr_market(trade_date), {})
    flow_results = _safe("수급적재",      run_flow_watch,   [])
    _safe("국민연금추적", run_pension_watch, "")

    header = f"⚠️ [TEST] 🌙 <b>저녁 브리핑</b> ({now_str})" if test_mode else f"🌙 <b>저녁 브리핑</b> ({now_str})"
    lines = [header, ""]

    fg = _fear_greed_line(short=False)
    if fg:
        lines.append(fg)
        lines.append("")

    # 📌 한 줄 결론
    oneliner = _safe("한줄결론", lambda: _kr_evening_oneliner(kr or {}), "")
    if oneliner:
        lines.append(f"📌 {oneliner}")
        lines.append("")

    # 코스피/코스닥
    lines.append("📊 <b>국장 마감</b>")
    if kr:
        kp_close, kp_pct = kr.get("kospi", (None, None))
        kq_close, kq_pct = kr.get("kosdaq", (None, None))
        kp_f = kr.get("foreign_kospi")  # None = 장중/미집계
        kp_i = kr.get("inst_kospi")     # None = 장중/미집계

        if kp_close:
            emoji = "📈" if (kp_pct or 0) >= 0 else "📉"
            if kp_f is None:
                inv_str = "외인 미집계"
            else:
                inv_str = f"외인 {_won_str(kp_f)} / 기관 {_won_str(kp_i) if kp_i is not None else '미집계'}"
            lines.append(
                f"  {emoji} KOSPI: {kp_close:,.2f} ({_pct_str(kp_pct)}) | {inv_str}"
            )
        if kq_close:
            emoji = "📈" if (kq_pct or 0) >= 0 else "📉"
            lines.append(f"  {emoji} KOSDAQ: {kq_close:,.2f} ({_pct_str(kq_pct)})")
    else:
        lines.append("  (국장 데이터 조회 실패)")

    # 보유종목 수급 (watchlist 수급감시=Y → 보유 6종으로 교체 반영)
    if flow_results:
        lines.append("")
        lines.append("📋 <b>보유종목 수급</b>")
        for r in flow_results:
            name = r["name"]
            f_net = r["foreign_net"]
            i_net = r["inst_net"]
            close = r["close"]
            f_streak = r["foreign_streak"]
            i_streak = r["inst_streak"]
            threshold = r["threshold"]

            tag = ""
            if abs(f_streak) >= threshold and abs(i_streak) >= threshold:
                direction = "매수" if f_streak > 0 else "매도"
                tag = f" ⚡동반{direction} {abs(f_streak)}일째"

            close_str = f"{close:,.0f}원" if close else "N/A"
            lines.append(
                f"  [{name}] {close_str} | "
                f"외인 {_won_str(f_net)}({f_streak}일) / 기관 {_won_str(i_net)}({i_streak}일)"
                f"{tag}"
            )

    # 보유종목 이상신호 (position_summary.json 연동)
    pos_warn = _safe("보유이상신호", _position_warnings, [])
    if pos_warn:
        lines.append("")
        lines.append("💼 <b>보유종목 이상신호</b>")
        lines.extend(pos_warn)

    # 슈퍼시그널
    super_lines = _safe("슈퍼시그널", _today_super_signals, [])
    if super_lines:
        lines.append("")
        lines.append("🔥 <b>슈퍼시그널</b>")
        lines.extend(super_lines)

    # 공시 요약
    disc_lines = _safe("공시요약", _today_disc_summary, [])
    if disc_lines:
        lines.append("")
        lines.append("📣 <b>오늘 공시</b>")
        lines.extend(disc_lines)

    send_message("\n".join(lines), msg_type="evening_briefing")

    # ── 차트 이미지 첨부 ──
    chart_path = _safe("저녁차트", lambda: _make_evening_chart(kr, flow_results or []), None)
    if chart_path:
        try:
            send_photo(chart_path,
                       caption=f"📈 국장 마감 + 워치 수급 {now_str}",
                       msg_type="evening_chart")
        finally:
            try:
                os.remove(chart_path)
            except Exception:
                pass

    logger.info("저녁 브리핑 발송 완료")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from db import init_db
    init_db()

    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "evening":
        evening_briefing()
    else:
        morning_briefing()
