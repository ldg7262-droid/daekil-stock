"""저녁 브리핑 런처 — Windows 작업 스케줄러에서 직접 호출.
수급 적재 + 슈퍼시그널 탐지 + 국민연금 추적 포함.
"""
import os
import sys
import logging

# .env 로드 (DART_API_KEY 등 Windows 환경변수에 없는 키를 보충)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        # 콘솔: utf-8 강제 (Windows cp949에서 한글·em dash 깨짐 방지)
        logging.StreamHandler(open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "daekil_bot.log"),
            encoding="utf-8",
        ),
    ],
)

import sys
from db import init_db

init_db()

# 1. 공포탐욕지수 산출 (캐시 활용 — 이미 오늘 산출됐으면 재사용)
try:
    from fear_greed import run_today as _fg_run
    _fg_run(force=False)
except Exception as _e:
    import logging; logging.getLogger(__name__).warning("fear_greed 산출 실패: %s", _e)

# 2. DC연금 리밸런싱 알림 체크
try:
    from pension_alert import check_and_alert as _pension_check
    _pension_check()
except Exception as _e:
    import logging; logging.getLogger(__name__).warning("pension_alert 실패: %s", _e)

# 3. 저녁 브리핑 발송
from briefing import evening_briefing
evening_briefing(test_mode="--test" in sys.argv)

# 4. 신호 채점 배치
try:
    from grade_signals import run_grading as _grade_run
    _r = _grade_run()
    if _r["graded"] > 0:
        import logging as _log
        _log.getLogger(__name__).info("신호 채점: %d건 완료", _r["graded"])
except Exception as _e:
    import logging as _log
    _log.getLogger(__name__).warning("신호 채점 실패: %s", _e)

# 5. GitHub Pages 게시 (git 미설치 or 미설정 시 무시)
try:
    from publish_docs import publish as _publish
    _publish()
except Exception as _e:
    import logging as _log
    _log.getLogger(__name__).info("publish_docs 스킵: %s", _e)
