# -*- coding: utf-8 -*-
"""publish_docs.py — GitHub Pages 자동 게시

매일 bot_evening 마지막에 호출:
  1. docs/data.json 생성 (공개 안전: 수량/금액 제외, 종목명/등락률/신호만)
  2. git add docs/data.json → commit → push

git이 없거나 remote 미설정이면 data.json만 생성하고 git 부분은 스킵.
"""
import json
import logging
import os
import subprocess
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)
BASE  = os.path.dirname(os.path.abspath(__file__))
DOCS  = os.path.join(BASE, "docs")


# ── 데이터 수집 ───────────────────────────────────────────────────

def _fear_greed_snapshot() -> dict:
    try:
        from fear_greed import get_latest, gauge_bar, score_to_grade
        rows = get_latest(2)
        if not rows:
            return {}
        r = rows[0]
        score = r["score"]
        ma5   = r.get("score_ma5")
        return {
            "score":  score,
            "ma5":    round(ma5, 1) if ma5 is not None else None,
            "grade":  r["grade"],
            "gauge":  gauge_bar(score),
            "date":   r["date"],
            "trend":  (score - rows[1]["score"]) if len(rows) > 1 else 0,
        }
    except Exception as e:
        logger.warning("fear_greed snapshot 실패: %s", e)
        return {}


def _holdings_snapshot() -> list[dict]:
    """보유 KR 종목 등락률. 수량/평단 제외."""
    try:
        from pykrx import stock as krx
        import time
        holdings_path = os.path.join(BASE, "holdings.txt")
        codes = []
        with open(holdings_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                tk = line.split()[0]
                if tk.isdigit() and len(tk) == 6:
                    codes.append(tk)
        if not codes:
            return []

        today_str = date.today().strftime("%Y%m%d")
        from datetime import timedelta
        prev_str  = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
        result = []
        for code in codes:
            try:
                name = (krx.get_market_ticker_name(code) or code)[:8]
                ohlcv = krx.get_market_ohlcv_by_date(prev_str, today_str, code)
                time.sleep(0.1)
                pct = None
                if ohlcv is not None and not ohlcv.empty:
                    closes = ohlcv["종가"].dropna()
                    if len(closes) >= 2:
                        pct = round((float(closes.iloc[-1]) - float(closes.iloc[-2]))
                                    / float(closes.iloc[-2]) * 100, 2)
                result.append({"code": code, "name": name, "pct": pct})
            except Exception:
                result.append({"code": code, "name": code, "pct": None})
        return result
    except Exception as e:
        logger.warning("holdings snapshot 실패: %s", e)
        return []


def _signals_snapshot() -> list[dict]:
    """최근 30일 신호 (채점 포함)."""
    try:
        from db import get_conn
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT signal_date, stock_code, signal_type, detail,
                          graded, return_20d, alpha_20d
                   FROM signals WHERE signal_date >= ? ORDER BY signal_date DESC""",
                (cutoff,),
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "date":   r["signal_date"],
                "code":   r["stock_code"],
                "type":   r["signal_type"],
                "detail": (r["detail"] or "")[:60],
                "graded": bool(r["graded"]),
                "ret20":  round(r["return_20d"], 1) if r["return_20d"] is not None else None,
                "alp20":  round(r["alpha_20d"],  1) if r["alpha_20d"]  is not None else None,
            })
        return result
    except Exception as e:
        logger.warning("signals snapshot 실패: %s", e)
        return []


def _grade_summary_snapshot() -> list[dict]:
    """전체 채점 완료 신호 유형별 집계."""
    try:
        from db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT signal_type, return_20d, alpha_20d FROM signals WHERE graded=1"
            ).fetchall()
        from collections import defaultdict
        buckets = defaultdict(list)
        for r in rows:
            if r["return_20d"] is not None:
                buckets[r["signal_type"]].append((r["return_20d"], r["alpha_20d"]))
        summary = []
        for stype, items in sorted(buckets.items(), key=lambda x: -len(x[1])):
            cnt  = len(items)
            wins = sum(1 for ret, _ in items if ret > 0)
            rets   = [ret for ret, _ in items]
            alphas = [a for _, a in items if a is not None]
            summary.append({
                "type":     stype,
                "count":    cnt,
                "win_rate": round(wins / cnt * 100) if cnt else 0,
                "avg_ret":  round(sum(rets) / len(rets), 1) if rets else 0,
                "avg_alp":  round(sum(alphas) / len(alphas), 1) if alphas else None,
            })
        return summary
    except Exception as e:
        logger.warning("grade_summary snapshot 실패: %s", e)
        return []


def _disclosures_snapshot() -> list[dict]:
    """최근 7일 공시."""
    try:
        from db import get_conn
        from datetime import timedelta
        cutoff = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT rcept_dt, stock_code, disc_type, summary FROM disclosures"
                " WHERE notified=1 AND rcept_dt >= ? ORDER BY rcept_dt DESC LIMIT 20",
                (cutoff,),
            ).fetchall()
        return [{"date": r["rcept_dt"], "code": r["stock_code"],
                 "type": r["disc_type"], "summary": (r["summary"] or "")[:60]}
                for r in rows]
    except Exception as e:
        logger.warning("disclosures snapshot 실패: %s", e)
        return []


# ── 데이터 JSON 생성 ──────────────────────────────────────────────

def generate_data_json() -> str:
    """docs/data.json 생성 → 경로 반환."""
    os.makedirs(DOCS, exist_ok=True)
    data = {
        "generated":     datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "fear_greed":    _fear_greed_snapshot(),
        "holdings":      _holdings_snapshot(),
        "signals":       _signals_snapshot(),
        "grade_summary": _grade_summary_snapshot(),
        "disclosures":   _disclosures_snapshot(),
    }
    out = os.path.join(DOCS, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("data.json 생성: %s (%d bytes)", out, os.path.getsize(out))
    return out


# ── Git push ──────────────────────────────────────────────────────

def _git_available() -> bool:
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _is_git_repo() -> bool:
    r = subprocess.run(["git", "rev-parse", "--git-dir"],
                       capture_output=True, cwd=BASE, timeout=5)
    return r.returncode == 0


def _has_remote() -> bool:
    r = subprocess.run(["git", "remote"],
                       capture_output=True, text=True, cwd=BASE, timeout=5)
    return bool(r.stdout.strip())


def _git_push() -> bool:
    """data.json만 커밋·push. 실패 시 False."""
    today_str = date.today().isoformat()
    cmds = [
        ["git", "add", "docs/data.json"],
        ["git", "commit", "-m", f"data: {today_str}", "--allow-empty"],
        ["git", "push"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE, timeout=60)
        if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
            logger.warning("git 명령 실패: %s\n%s", " ".join(cmd), r.stderr[:300])
            return False
    logger.info("GitHub Pages push 완료 (%s)", today_str)
    return True


# ── 공개 API ─────────────────────────────────────────────────────

def publish() -> None:
    """data.json 생성 + git push (git 없으면 생성만)."""
    generate_data_json()

    if not _git_available():
        logger.info("git 미설치 — data.json 생성만 완료 (push 스킵)")
        return
    if not _is_git_repo():
        logger.info("git 저장소 아님 — data.json 생성만 완료 (push 스킵)")
        return
    if not _has_remote():
        logger.info("git remote 없음 — data.json 생성만 완료 (push 스킵)")
        return

    _git_push()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from db import init_db
    init_db()
    publish()
    print("완료.")
