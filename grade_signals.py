# -*- coding: utf-8 -*-
"""grade_signals.py — 신호 채점 배치

signals 테이블에서 20거래일 경과 미채점 신호를 찾아:
  - return_5d  : 신호 후 5거래일 수익률
  - return_20d : 신호 후 20거래일 수익률
  - alpha_5d   : return_5d - 동기간 KOSPI 수익률
  - alpha_20d  : return_20d - 동기간 KOSPI 수익률
  - graded=1, graded_date=오늘

매일 bot_evening 파이프라인 마지막에 호출.
채점 대상 없으면 로그만 남기고 정상 종료.
"""
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── 거래일 유틸 ──────────────────────────────────────────────────

def _trading_dates_between(start_yyyymmdd: str, end_yyyymmdd: str) -> list[str]:
    """start ~ end 사이의 KRX 거래일 목록 (YYYYMMDD). 실패 시 빈 리스트."""
    try:
        from pykrx import stock as krx
        df = krx.get_index_ohlcv_by_date(start_yyyymmdd, end_yyyymmdd, "1001")
        if df is None or df.empty:
            return []
        return [d.strftime("%Y%m%d") for d in df.index.tolist()]
    except Exception as e:
        logger.warning("거래일 조회 실패: %s", e)
        return []


def _nth_trading_day_after(base_iso: str, n: int) -> Optional[str]:
    """base_iso(YYYY-MM-DD) 이후 n번째 거래일 → YYYYMMDD. 충분한 날짜 없으면 None."""
    base = datetime.strptime(base_iso, "%Y-%m-%d").date()
    end  = base + timedelta(days=n * 2 + 30)
    if end > date.today():
        end = date.today()
    dates = _trading_dates_between(base.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    # dates[0] = base_date 자신(거래일이면), dates[n] = n거래일 후
    if len(dates) > n:
        return dates[n]
    return None


def _stock_close(code6: str, yyyymmdd: str) -> Optional[float]:
    """종가 조회. 당일 데이터 없으면 None."""
    try:
        from pykrx import stock as krx
        df = krx.get_market_ohlcv_by_date(yyyymmdd, yyyymmdd, code6)
        time.sleep(0.1)
        if df is None or df.empty:
            return None
        return float(df["종가"].iloc[0])
    except Exception:
        return None


def _kospi_close(yyyymmdd: str) -> Optional[float]:
    """KOSPI 종가."""
    try:
        from pykrx import stock as krx
        df = krx.get_index_ohlcv_by_date(yyyymmdd, yyyymmdd, "1001")
        time.sleep(0.05)
        if df is None or df.empty:
            return None
        return float(df["종가"].iloc[0])
    except Exception:
        return None


def _ret(entry: float, exit_: float) -> float:
    """단순 수익률 (%)."""
    if not entry:
        return 0.0
    return (exit_ - entry) / entry * 100.0


# ── 채점 배치 ─────────────────────────────────────────────────────

def run_grading() -> dict:
    """채점 실행. 반환: {graded: N, skipped: M, pending: K, next_date: 'YYYY-MM-DD'}"""
    from db import get_conn
    today_iso = date.today().isoformat()

    with get_conn() as conn:
        # 미채점 신호 전체 조회
        rows = conn.execute(
            "SELECT id, signal_date, stock_code, signal_type, price_at "
            "FROM signals WHERE graded = 0 ORDER BY signal_date"
        ).fetchall()

    if not rows:
        logger.info("채점 배치: 미채점 신호 없음 (signals 테이블 비어 있음)")
        return {"graded": 0, "skipped": 0, "pending": 0, "next_date": None}

    graded_count  = 0
    skipped_count = 0
    pending_dates = []

    for row in rows:
        sid         = row["id"]
        sig_date    = row["signal_date"]   # YYYY-MM-DD
        stock_code  = row["stock_code"]
        price_at    = row["price_at"]

        # 20거래일 후 날짜 계산
        date20_yyyymmdd = _nth_trading_day_after(sig_date, 20)
        if date20_yyyymmdd is None:
            # 아직 20거래일 미경과
            date5_yyyymmdd = _nth_trading_day_after(sig_date, 5)
            if date5_yyyymmdd:
                pending_dates.append(date5_yyyymmdd)
            continue

        # 오늘보다 미래면 아직 채점 불가
        if date20_yyyymmdd > date.today().strftime("%Y%m%d"):
            pending_dates.append(date20_yyyymmdd)
            continue

        date5_yyyymmdd = _nth_trading_day_after(sig_date, 5)
        sig_yyyymmdd   = datetime.strptime(sig_date, "%Y-%m-%d").strftime("%Y%m%d")

        # 가격 조회
        if not price_at or price_at <= 0:
            logger.warning("신호 %d: price_at 없음 → 채점 스킵", sid)
            skipped_count += 1
            continue

        kospi0 = _kospi_close(sig_yyyymmdd)

        ret5, alpha5 = None, None
        if date5_yyyymmdd:
            price5 = _stock_close(stock_code, date5_yyyymmdd)
            if price5:
                ret5 = _ret(price_at, price5)
                kospi5 = _kospi_close(date5_yyyymmdd)
                if kospi0 and kospi5:
                    alpha5 = ret5 - _ret(kospi0, kospi5)

        price20 = _stock_close(stock_code, date20_yyyymmdd)
        if not price20:
            logger.warning("신호 %d (%s %s): 20일후 종가 없음 → 스킵", sid, sig_date, stock_code)
            skipped_count += 1
            continue

        ret20 = _ret(price_at, price20)
        alpha20 = None
        kospi20 = _kospi_close(date20_yyyymmdd)
        if kospi0 and kospi20:
            alpha20 = ret20 - _ret(kospi0, kospi20)

        with get_conn() as conn:
            conn.execute(
                """UPDATE signals
                   SET graded=1, return_5d=?, return_20d=?, alpha_5d=?, alpha_20d=?,
                       graded_date=?
                   WHERE id=?""",
                (ret5, ret20, alpha5, alpha20, today_iso, sid),
            )
        logger.info(
            "채점 완료: id=%d %s %s → ret20=%.1f%% alpha20=%s",
            sid, sig_date, stock_code, ret20,
            f"{alpha20:.1f}%" if alpha20 is not None else "N/A",
        )
        graded_count += 1

    # 다음 채점 예정일 (pending 중 가장 이른 날)
    next_date = None
    if pending_dates:
        next_yyyymmdd = min(pending_dates)
        next_date = f"{next_yyyymmdd[:4]}-{next_yyyymmdd[4:6]}-{next_yyyymmdd[6:]}"

    pending_count = len(rows) - graded_count - skipped_count
    logger.info(
        "채점 배치 완료: 채점 %d건 / 스킵 %d건 / 대기 %d건 / 다음예정 %s",
        graded_count, skipped_count, pending_count, next_date or "없음"
    )
    return {
        "graded":    graded_count,
        "skipped":   skipped_count,
        "pending":   pending_count,
        "next_date": next_date,
    }


# ── 월간 성적표 ───────────────────────────────────────────────────

def monthly_report(year: int, month: int) -> Optional[str]:
    """지정 년월의 채점 완료 신호 성적표 텍스트 반환. 데이터 없으면 None."""
    try:
        from db import get_conn
        start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            end = f"{year + 1:04d}-01-01"
        else:
            end = f"{year:04d}-{month + 1:02d}-01"

        with get_conn() as conn:
            rows = conn.execute(
                """SELECT signal_type, return_20d, alpha_20d
                   FROM signals
                   WHERE graded=1 AND signal_date >= ? AND signal_date < ?""",
                (start, end),
            ).fetchall()

        if not rows:
            return None

        # 신호 유형별 집계
        from collections import defaultdict
        buckets: dict[str, list] = defaultdict(list)
        for r in rows:
            stype = r["signal_type"] or "기타"
            ret = r["return_20d"]
            alp = r["alpha_20d"]
            if ret is not None:
                buckets[stype].append((ret, alp))

        if not buckets:
            return None

        lines = [f"🏆 {month}월 신호 성적표 ({year})"]
        total_cnt = total_win = 0
        total_alpha_sum = 0.0
        total_alpha_n   = 0
        for stype, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
            cnt  = len(items)
            wins = sum(1 for ret, _ in items if ret > 0)
            wr   = wins / cnt * 100 if cnt else 0
            rets  = [ret for ret, _ in items]
            alphas = [a for _, a in items if a is not None]
            avg_ret  = sum(rets) / len(rets) if rets else 0
            avg_alp  = sum(alphas) / len(alphas) if alphas else None
            alpha_str = f" 알파 {avg_alp:+.1f}%" if avg_alp is not None else ""
            lines.append(
                f"  {stype}: {cnt}건 승률 {wr:.0f}% 평균 {avg_ret:+.1f}%{alpha_str}"
            )
            total_cnt  += cnt
            total_win  += wins
            total_alpha_sum += sum(alphas)
            total_alpha_n   += len(alphas)

        total_wr = total_win / total_cnt * 100 if total_cnt else 0
        total_alp_str = ""
        if total_alpha_n:
            total_alp_str = f" / 전체 알파 {total_alpha_sum / total_alpha_n:+.1f}%"
        lines.append(f"  ▶ 전체 {total_cnt}건 · 승률 {total_wr:.0f}%{total_alp_str}")
        return "\n".join(lines)

    except Exception as e:
        logger.warning("월간 성적표 생성 실패: %s", e)
        return None


def is_first_trading_day_of_month() -> bool:
    """오늘이 이번 달의 첫 거래일인지 확인."""
    today = date.today()
    if today.day > 5:
        return False
    try:
        from pykrx import stock as krx
        start = today.replace(day=1).strftime("%Y%m%d")
        end   = today.strftime("%Y%m%d")
        df = krx.get_index_ohlcv_by_date(start, end, "1001")
        if df is None or df.empty:
            return False
        return df.index[0].date() == today
    except Exception:
        return False


# ── 테스트용 유틸 ─────────────────────────────────────────────────

def insert_test_signal(code: str = "005930", price: float = 80000.0) -> int:
    """21거래일 전 날짜로 테스트 신호 삽입 → 삽입된 id 반환."""
    from db import get_conn
    # 21거래일 전 날짜 계산
    end   = date.today()
    start = (end - timedelta(days=60)).strftime("%Y%m%d")
    dates = _trading_dates_between(start, end.strftime("%Y%m%d"))
    if len(dates) < 22:
        raise RuntimeError("거래일 데이터 부족 (최소 22일 필요)")
    sig_date_yyyymmdd = dates[-22]   # 21거래일 전 (0-indexed: -22 = 21일 전)
    sig_date_iso = f"{sig_date_yyyymmdd[:4]}-{sig_date_yyyymmdd[4:6]}-{sig_date_yyyymmdd[6:]}"

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO signals (signal_date, stock_code, signal_type, detail, price_at, graded)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            (sig_date_iso, code, "테스트신호", "grade_signals.py 검증용", price),
        )
        new_id = cur.lastrowid
    logger.info("테스트 신호 삽입: id=%d date=%s code=%s price=%.0f", new_id, sig_date_iso, code, price)
    return new_id


def delete_test_signal(signal_id: int) -> None:
    """테스트 신호 삭제."""
    from db import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM signals WHERE id=? AND signal_type='테스트신호'", (signal_id,))
    logger.info("테스트 신호 삭제: id=%d", signal_id)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from db import init_db
    init_db()

    if "--test" in sys.argv:
        print("=== 채점 검증 테스트 ===")
        print("1. 삼성전자(005930) 테스트 신호 삽입 (21거래일 전)...")
        tid = insert_test_signal("005930")
        print(f"   삽입 완료: id={tid}")

        print("2. 채점 배치 실행...")
        result = run_grading()
        print(f"   결과: {result}")

        from db import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (tid,)).fetchone()
        if row:
            print(f"   return_5d={row['return_5d']:.1f}%" if row['return_5d'] is not None else "   return_5d=None")
            print(f"   return_20d={row['return_20d']:.1f}%" if row['return_20d'] is not None else "   return_20d=None")
            print(f"   alpha_20d={row['alpha_20d']:.1f}%" if row['alpha_20d'] is not None else "   alpha_20d=None")
            print(f"   graded={row['graded']} graded_date={row['graded_date']}")

        print("3. 테스트 데이터 삭제...")
        delete_test_signal(tid)
        print("   완료 ✅")
    else:
        result = run_grading()
        n_pending = result["pending"]
        next_d    = result["next_date"] or "없음"
        print(f"채점 완료 {result['graded']}건 | 대기 {n_pending}건 | 최초 채점 예정일 {next_d}")
