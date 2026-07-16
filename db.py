"""SQLite DB 초기화 및 공통 연결 유틸."""
import sqlite3
import logging
from pathlib import Path

DB_PATH = Path(__file__).parent / "daekil_stock.db"

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS disclosures (
                rcept_no    TEXT PRIMARY KEY,
                stock_code  TEXT NOT NULL,
                disc_type   TEXT NOT NULL,
                summary     TEXT,
                rcept_dt    TEXT NOT NULL,
                notified    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS flows (
                trade_date  TEXT NOT NULL,
                stock_code  TEXT NOT NULL,
                foreign_net REAL,
                inst_net    REAL,
                close_price REAL,
                PRIMARY KEY (trade_date, stock_code)
            );

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_date TEXT NOT NULL,
                stock_code  TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                detail      TEXT,
                price_at    REAL,
                graded      INTEGER DEFAULT 0,
                return_20d  REAL
            );

            CREATE TABLE IF NOT EXISTS message_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at     TEXT NOT NULL,
                msg_type    TEXT NOT NULL,
                content     TEXT
            );

            CREATE TABLE IF NOT EXISTS pension_holdings (
                report_date  TEXT NOT NULL,
                stock_code   TEXT NOT NULL,
                quarter      TEXT NOT NULL,
                holding_pct  REAL,
                prev_pct     REAL,
                change_pct   REAL,
                rcept_no     TEXT,
                PRIMARY KEY (quarter, stock_code)
            );

            CREATE TABLE IF NOT EXISTS fear_greed (
                date         TEXT PRIMARY KEY,
                score        INTEGER NOT NULL,
                score_ma5    REAL,
                grade        TEXT NOT NULL,
                kospi_pct    REAL,
                adr          REAL,
                krw_pct      REAL,
                vol_std      REAL,
                credit_pct   REAL,
                deposit_pct  REAL,
                n_valid      INTEGER,
                detail_json  TEXT
            );

            CREATE TABLE IF NOT EXISTS pension_alert_state (
                key          TEXT PRIMARY KEY,
                value        TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
        """)
    # 컬럼 마이그레이션 (이미 있으면 무시)
    _migrations = [
        "ALTER TABLE signals ADD COLUMN return_5d   REAL",
        "ALTER TABLE signals ADD COLUMN alpha_20d   REAL",
        "ALTER TABLE signals ADD COLUMN alpha_5d    REAL",
        "ALTER TABLE signals ADD COLUMN graded_date TEXT",
    ]
    for sql in _migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass
    logger.info("DB 초기화 완료: %s", DB_PATH)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
