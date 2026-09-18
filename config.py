"""auto-report 설정.

경로·BigQuery 대상·이메일 예시값·판정 임계값만 담는다.
지표 정의는 여기에 쓰지 않는다 — 앱이 읽는 유일한 정의 원천은 catalog/*.json 이다 (CLAUDE.md 3절).

────────────────────────────────────────────────────────────────────────
 위키 경로를 직접 지정하려면 아래 WIKI_PATH_OVERRIDE 한 줄만 고친다.
   예)  WIKI_PATH_OVERRIDE = r"C:\\Users\\내이름\\Documents\\my-wiki-02"
        WIKI_PATH_OVERRIDE = "/Users/me/Documents/my-wiki-02"

 None으로 두면 이 파일이 있는 폴더의 **형제 폴더** 중 `06_metrics/`를 가진
 것을 자동으로 찾는다. 못 찾으면 WIKI_PATH는 None이 되고, 실행할 때
 "위키 경로를 직접 지정하세요" 경고가 나온다.

 이 경로는 카탈로그를 export할 때(catalog/export_catalog.py)만 쓴다.
 앱 본체는 위키를 읽지 않고 catalog/*.json만 읽는다.
────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import List, Optional

# ── 사람이 고치는 자리 ────────────────────────────────────────────────
WIKI_PATH_OVERRIDE: Optional[str] = None
# ─────────────────────────────────────────────────────────────────────


# ── 프로젝트 경로 ─────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).resolve().parent

CATALOG_DIR: Path = BASE_DIR / "catalog"
PIPELINE_DIR: Path = BASE_DIR / "pipeline"
OUTPUTS_DIR: Path = BASE_DIR / "outputs"
FONTS_DIR: Path = BASE_DIR / "fonts"
MANUAL_DIR: Path = BASE_DIR / "manual"

METRICS_CATALOG_PATH: Path = CATALOG_DIR / "metrics_catalog.json"
SCHEMA_CATALOG_PATH: Path = CATALOG_DIR / "schema_catalog.json"
INSIGHTS_CATALOG_PATH: Path = CATALOG_DIR / "insights_catalog.json"

#: 사람이 쓰는 장(2·5·6)을 담는 파일. 없으면 템플릿을 만든다.
MANUAL_SECTIONS_PATH: Path = MANUAL_DIR / "sections.md"


# ── 위키 경로 ─────────────────────────────────────────────────────────
# 위키 안에서 읽는 두 폴더. 06_metrics는 위키를 식별하는 표지이기도 하다.
WIKI_METRICS_DIRNAME: str = "06_metrics"
WIKI_DATA_DIRNAME: str = "02_data"
WIKI_INSIGHTS_DIRNAME: str = "04_insights"

WIKI_MISSING_MESSAGE: str = (
    "위키 경로를 직접 지정하세요 — config.py 상단의 WIKI_PATH_OVERRIDE에 "
    f"'{WIKI_METRICS_DIRNAME}'가 들어 있는 위키 폴더(예: my-wiki-02) 경로를 적습니다."
)


def _find_wiki_beside(base: Path) -> Optional[Path]:
    """base의 형제 폴더 중 06_metrics/를 가진 첫 폴더를 반환한다.

    이름순으로 훑는다 — 후보가 둘 이상이어도 실행마다 같은 것을 고르게 하기 위해서다
    (CLAUDE.md 5-5 재현성). 못 찾으면 None.
    """
    try:
        siblings = sorted(p for p in base.parent.iterdir() if p.is_dir())
    except OSError:
        return None

    for sibling in siblings:
        if sibling == base:
            continue
        if (sibling / WIKI_METRICS_DIRNAME).is_dir():
            return sibling.resolve()
    return None


def _resolve_wiki_path() -> Optional[Path]:
    if WIKI_PATH_OVERRIDE:
        return Path(WIKI_PATH_OVERRIDE).expanduser().resolve()
    return _find_wiki_beside(BASE_DIR)


#: 위키 루트. 자동 탐색에 실패하면 None — 이때 export는 실행하지 않는다.
WIKI_PATH: Optional[Path] = _resolve_wiki_path()

#: 위키 하위 경로. WIKI_PATH가 None이면 함께 None이다.
WIKI_METRICS_PATH: Optional[Path] = WIKI_PATH / WIKI_METRICS_DIRNAME if WIKI_PATH else None
WIKI_DATA_PATH: Optional[Path] = WIKI_PATH / WIKI_DATA_DIRNAME if WIKI_PATH else None
WIKI_INSIGHTS_PATH: Optional[Path] = (
    WIKI_PATH / WIKI_INSIGHTS_DIRNAME if WIKI_PATH else None
)


# ── BigQuery ──────────────────────────────────────────────────────────
#: None이면 ADC(Application Default Credentials)의 기본 프로젝트를 쓴다.
#: 고정하려면 문자열로 프로젝트 ID를 적는다. 예) "my-gcp-project-123456"
BQ_PROJECT: Optional[str] = None

BQ_DATASET: str = "project1_day1"

#: 업로드 파일을 올릴 스테이징 테이블 접두사. 매 실행마다 CREATE OR REPLACE로 교체된다.
#: DML(INSERT/UPDATE/DELETE/MERGE)은 쓰지 않는다 — 샌드박스에서 차단된다 (CLAUDE.md 5-2).
STAGING_PREFIX: str = "staging_"


# ── 이메일 (예시값) ───────────────────────────────────────────────────
# 실제 주소를 코드에 쓰지 않는다 (CLAUDE.md 5-3). 6주차 범위는 초안 생성과 확정까지이며
# 메일 서버에 연결하지 않는다.
EMAIL_TO: List[str] = ["report-recipient@example.com"]
EMAIL_FROM: str = "auto-report@example.com"
EMAIL_SUBJECT_PREFIX: str = "[월간]"


# ── 판정 임계값 ───────────────────────────────────────────────────────
#: 업로드 파일이 카탈로그의 원천 테이블과 같은 스키마인지 판정하는 최소 일치율.
#: 이 값 미만이면 2단계에서 중단하고 이유를 표시한다 (CLAUDE.md 5-1).
MIN_SCHEMA_MATCH: float = 0.8

#: 전월 대비 이상 변동으로 볼 상대변화율(%)의 절대값. 이 값 이상이면 3단계에서 경고한다.
#: 임계값을 넘었다는 것은 "확인해 보라"는 뜻이지 "틀렸다"는 뜻이 아니다 (CLAUDE.md 6절).
MOM_THRESHOLD: float = 5.0

#: 추이 그래프에 그릴 개월 수(당월 포함). 늘리면 BigQuery 조회도 그만큼 늘어난다.
TREND_MONTHS: int = 6


# ── 설정 점검 ─────────────────────────────────────────────────────────
def config_warnings() -> List[str]:
    """설정 상태를 점검해 사람이 읽을 경고 문구를 반환한다. 정상이면 빈 리스트.

    화면(app.py)과 CLI(export_catalog.py) 양쪽에서 같은 문구를 쓰기 위해 여기 둔다.
    """
    messages: List[str] = []

    if WIKI_PATH is None:
        messages.append(WIKI_MISSING_MESSAGE)
        return messages

    if not WIKI_PATH.is_dir():
        messages.append(f"위키 경로가 없습니다: {WIKI_PATH} — {WIKI_MISSING_MESSAGE}")
        return messages

    for label, path in (
        (WIKI_METRICS_DIRNAME, WIKI_METRICS_PATH),
        (WIKI_DATA_DIRNAME, WIKI_DATA_PATH),
    ):
        if path is None or not path.is_dir():
            messages.append(f"위키에 {label}/ 폴더가 없습니다: {path}")

    return messages


# import 시점에 한 번 알린다. 화면에는 config_warnings()로 다시 띄운다.
for _message in config_warnings():
    warnings.warn(_message, stacklevel=2)
