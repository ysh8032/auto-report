"""2단계 — 스테이징 적재 후 지표 계산.

업로드 파일을 BigQuery 스테이징 테이블로 올리고, 지표 정의서의 `계산` 필드로
SQL을 조립해 계산한다. 5주차 `calc_metrics.py`의 구조를 그대로 따른다 —
원천/집계/조인/조건을 읽어 SQL을 만들고, 지표별 하드코딩 SQL은 만들지 않는다.

지켜야 할 것 (CLAUDE.md 5-2·9절)
  - **DML을 쓰지 않는다.** INSERT·UPDATE·DELETE·MERGE는 BigQuery 샌드박스에서
    차단된다. 적재는 로드 잡(DML 아님)으로, 테이블 생성은 WRITE_TRUNCATE로 한다.
  - **원본 테이블을 고치지 않는다.** 업로드분은 스테이징 테이블에만 올리고,
    기존 테이블과는 조인해서 읽기만 한다.
  - **유효구간 밖을 0으로 채우지 않는다.** 계산하지 않고 상태로 남긴다.
  - MCP가 아니라 이 모듈이 직접 BigQuery에 연결한다 (무인 실행 요건).
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from google.cloud import bigquery

import config
from pipeline import profile

# ── 상태 ──────────────────────────────────────────────────────────────
OK = "OK"
OUT_OF_RANGE = "유효구간 밖"
EXTENDED = "구간확장"
LOW_SAMPLE = "표본부족"
NO_DATA = "데이터 없음"
ERROR = "계산오류"
UNSUPPORTED = "미지원"

PLACEHOLDER_PATTERN = re.compile(r"@month(?:_start|_end)?\b")
_RANGE_RE = re.compile(r"(\d{4}-\d{2})\s*~\s*(\d{4}-\d{2})")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")

RESULT_COLUMNS = [
    "metric_id",
    "지표명",
    "유형",
    "month",
    "value",
    "sample_size",
    "min_sample",
    "status",
    "이유",
    "구간확장",
    "원천",
    "부분갱신",
]


class CalculationError(Exception):
    """계산을 진행할 수 없을 때. 메시지는 화면에 그대로 보여줄 수 있어야 한다."""


@dataclass
class Result:
    metric_id: str
    지표명: str
    유형: str
    month: str
    value: Optional[float] = None
    sample_size: Optional[float] = None
    min_sample: Optional[float] = None
    status: str = OK
    이유: str = ""
    구간확장: bool = False
    원천: str = ""
    부분갱신: bool = False


# ── BigQuery 클라이언트 ───────────────────────────────────────────────
#: Streamlit secrets에서 서비스 계정을 찾을 키. secrets.toml의 [gcp_service_account] 절.
SECRETS_SA_KEY = "gcp_service_account"


def _service_account_from_secrets() -> Tuple[Optional[Any], Optional[str]]:
    """Streamlit secrets의 서비스 계정으로 (자격증명, 프로젝트ID)를 만든다. 없으면 (None, None).

    **배포 환경에는 ADC가 없다.** 로컬에서는 `gcloud auth application-default login`이
    남긴 자격증명을 쓰지만, Streamlit Cloud 같은 곳에는 그런 것이 없으므로 서비스 계정
    키를 secrets로 주입해야 한다.

    **로컬 동작은 바뀌지 않는다.** secrets.toml이 없거나 이 절이 비어 있으면 (None, None)을
    돌려주고, 호출부는 기존 ADC 경로로 간다. streamlit을 지연 import하는 것은
    `run_pipeline.py`(CLI)가 streamlit 없이도 돌아야 하기 때문이다.
    """
    try:
        import streamlit as st
    except ModuleNotFoundError:
        return None, None

    # secrets.toml이 아예 없으면 streamlit 버전에 따라 예외 종류가 다르다. 전부 "없음"으로 본다.
    try:
        raw = st.secrets[SECRETS_SA_KEY]
    except Exception:
        return None, None
    if not raw:
        return None, None

    from google.oauth2 import service_account

    info = dict(raw)
    missing = [k for k in ("project_id", "client_email", "private_key") if not info.get(k)]
    if missing:
        raise CalculationError(
            f"secrets의 [{SECRETS_SA_KEY}] 절에 {', '.join(missing)}가 없습니다. "
            "서비스 계정 JSON의 필드를 그대로 옮겨 적으세요."
        )

    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except ValueError as exc:
        raise CalculationError(
            f"secrets의 서비스 계정 키를 읽을 수 없습니다: {exc} — private_key의 줄바꿈이 "
            "\n 그대로 남아 있는지 확인하세요."
        ) from exc

    return credentials, info.get("project_id")


def make_client() -> bigquery.Client:
    """BigQuery 클라이언트를 만든다. secrets → config.BQ_PROJECT → ADC 순으로 본다.

    배포 환경에서는 Streamlit secrets의 서비스 계정을, 로컬에서는 ADC를 쓴다.
    ADC에 기본 프로젝트가 없으면 `quota_project_id`를 쓴다 — `gcloud auth
    application-default login`만 한 환경에서는 이쪽에만 프로젝트가 적혀 있다.
    끝내 못 찾으면 **추측하지 않고 멈춘다.** 엉뚱한 프로젝트에 테이블을 만드는 것보다 낫다.
    """
    sa_credentials, sa_project = _service_account_from_secrets()
    if sa_credentials is not None:
        resolved = config.BQ_PROJECT or sa_project
        if not resolved:
            raise CalculationError(
                "BigQuery 프로젝트를 정할 수 없습니다. secrets의 서비스 계정에 project_id가 "
                "없다면 config.py의 BQ_PROJECT에 프로젝트 ID를 적으세요."
            )
        return bigquery.Client(project=resolved, credentials=sa_credentials)

    if config.BQ_PROJECT:
        return bigquery.Client(project=config.BQ_PROJECT)

    import google.auth

    try:
        credentials, default_project = google.auth.default()
    except Exception as exc:
        raise CalculationError(
            "BigQuery 자격증명을 찾을 수 없습니다. 로컬이면 "
            "`gcloud auth application-default login`을 실행하고, 배포 환경이면 "
            f"Streamlit secrets에 [{SECRETS_SA_KEY}] 절을 추가하세요. ({exc})"
        ) from exc

    resolved = default_project or getattr(credentials, "quota_project_id", None)
    if not resolved:
        raise CalculationError(
            "BigQuery 프로젝트를 정할 수 없습니다. config.py의 BQ_PROJECT에 프로젝트 ID를 "
            "적거나 `gcloud config set project <ID>`를 실행하세요."
        )
    return bigquery.Client(project=resolved, credentials=credentials)


# ── 함수 1: 스테이징 적재 ─────────────────────────────────────────────
def load_staging(
    df: pd.DataFrame,
    table_name: str,
    client: bigquery.Client,
    dataset: Optional[str] = None,
) -> str:
    """업로드 파일을 `staging_<table_name>` 테이블로 올리고 전체 경로를 반환한다.

    **로드 잡을 쓰는 이유**: `INSERT`로 행을 넣으면 DML이라 BigQuery 샌드박스에서
    차단된다. 로드 잡은 DML이 아니라서 결제 계정 없이도 동작한다. 같은 이유로
    `WRITE_TRUNCATE`를 써서 매 실행 테이블을 통째로 교체한다 — 이어붙이지 않는다.

    **적재 후 행수를 대조하는 이유**: 로드 잡은 일부 행을 조용히 건너뛸 수 있다
    (타입 불일치 등). 행수가 다른 채로 계산이 돌면 그 사실을 아무도 모른 채
    틀린 숫자가 리포트에 실린다.
    """
    dataset = dataset or config.BQ_DATASET
    staging_name = f"{config.STAGING_PREFIX}{table_name}"
    table_id = f"{client.project}.{dataset}.{staging_name}"

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    try:
        client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    except Exception as exc:  # 권한·타입·데이터셋 없음 등
        raise CalculationError(f"스테이징 적재에 실패했습니다 ({table_id}): {exc}") from exc

    loaded = client.get_table(table_id).num_rows
    if loaded != len(df):
        # 메타데이터가 늦게 갱신될 수 있어 한 번 더 직접 센다.
        counted = list(client.query(f"SELECT COUNT(*) AS n FROM `{table_id}`").result())
        loaded = counted[0].n if counted else loaded
    if loaded != len(df):
        raise CalculationError(
            f"적재 행수가 다릅니다: 업로드 {len(df):,}행 → 테이블 {loaded:,}행 ({table_id}). "
            "계산을 진행하지 않습니다."
        )
    return table_id


def latest_dates(
    client: bigquery.Client,
    table: str,
    columns: Sequence[str],
    dataset: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """테이블의 날짜 컬럼별 최대값. 스테이징으로 갱신되지 않은 테이블이 언제까지의
    상태인지 화면에 적기 위해 쓴다.

    **어느 날짜가 중요한지는 판정하지 않는다.** 카탈로그에서 날짜 타입인 컬럼을 모두
    조회해 그대로 보여준다 — "이 테이블의 기준일은 churn_date다" 같은 판단은 업무
    지식이라 앱이 정할 몫이 아니다.
    """
    names = [str(c) for c in columns if str(c).strip()]
    if not names:
        return {}
    dataset = dataset or config.BQ_DATASET
    selects = ", ".join(f"MAX({name}) AS {name}" for name in names)
    sql = f"SELECT {selects} FROM `{dataset}.{table}`"
    rows = list(client.query(sql).result())
    if not rows:
        return {name: None for name in names}
    row = rows[0]
    return {name: (str(row[name]) if row[name] is not None else None) for name in names}


# ── 기간 처리 ─────────────────────────────────────────────────────────
def parse_year_month(text: str) -> Tuple[int, int]:
    year, month = str(text).strip().split("-")[:2]
    return int(year), int(month)


def shift_year_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_params(month: str) -> Dict[str, Any]:
    """@month(YYYY-MM 문자열) / @month_start / @month_end 세 값.

    @month_end는 실제 달력 말일이다. 정의서 4종(acquired_customers·
    acquisition_cost_total·churned_customers·mrr)이 이 값을 쓴다.
    """
    year, mon = parse_year_month(month)
    last_day = calendar.monthrange(year, mon)[1]
    return {
        "month": f"{year:04d}-{mon:02d}",
        "month_start": dt.date(year, mon, 1),
        "month_end": dt.date(year, mon, last_day),
    }


def in_valid_range(month: str, range_text: Optional[str]) -> bool:
    """유효구간 문자열('2024-07 ~ 2024-12')과 대조한다. 구간 표기가 없으면 제약 없음."""
    match = _RANGE_RE.search(str(range_text or ""))
    if not match:
        return True
    return match.group(1) <= month <= match.group(2)


def months_between(low: str, high: str, limit: int = 24) -> List[str]:
    year, mon = parse_year_month(low)
    end_year, end_mon = parse_year_month(high)
    months: List[str] = []
    while (year, mon) <= (end_year, end_mon):
        months.append(f"{year:04d}-{mon:02d}")
        if len(months) > limit:
            raise CalculationError(
                f"기간이 {limit}개월을 넘습니다 ({low} ~ {high}). "
                "계산할 달을 좁혀서 다시 실행하세요."
            )
        year, mon = shift_year_month(year, mon, 1)
    return months


def normalize_period(period: Any, limit: int = 24) -> List[str]:
    """profile.profile_data()의 기간 dict, (최소,최대) 튜플, 'YYYY-MM' 문자열을 모두 받는다."""
    if isinstance(period, dict):
        low, high = period.get("최소"), period.get("최대")
    elif isinstance(period, (tuple, list)) and len(period) == 2:
        low, high = period
    else:
        low = high = period

    low_month = month_of(low)
    high_month = month_of(high)
    if not low_month or not high_month:
        raise CalculationError(f"계산할 기간을 읽지 못했습니다: {period!r}")
    return months_between(low_month, high_month, limit)


def month_of(value: Any) -> Optional[str]:
    """아무 형태의 값에서 'YYYY-MM'만 뽑아낸다. 못 찾으면 None."""
    match = re.search(r"(\d{4})-(\d{2})", str(value or ""))
    return f"{match.group(1)}-{match.group(2)}" if match else None


# ── 함수 2: SQL 조립 ──────────────────────────────────────────────────
def _table_ref(name: str, dataset: str, staging_map: Dict[str, str]) -> str:
    """원천 테이블 하나를 FROM 절 조각으로 만든다.

    **별칭은 항상 정의서에 적힌 원래 이름으로 둔다.** 정의서의 `조인`·`조건`이
    `usage_history.customer_id`처럼 테이블명으로 컬럼을 한정하기 때문에, 스테이징
    테이블로 바꿔치기해도 별칭이 그대로여야 문자열이 그대로 들어맞는다.
    """
    physical = staging_map.get(name, name)
    return f"`{dataset}.{physical}` AS {name}"


def _leg_sql(spec: Dict[str, Any], dataset: str, staging_map: Dict[str, str]) -> str:
    """계산 블록 하나(원천/집계/조인/조건)를 SELECT 문으로 만든다."""
    tables = [t.strip() for t in str(spec.get("원천", "")).split("+") if t.strip()]
    if not tables:
        raise CalculationError("계산 명세에 `원천`이 없습니다.")
    if "집계" not in spec:
        raise CalculationError("계산 명세에 `집계`가 없습니다.")

    from_clause = " JOIN ".join(_table_ref(t, dataset, staging_map) for t in tables)
    if len(tables) > 1:
        join = spec.get("조인")
        if not join:
            raise CalculationError(
                f"원천이 {len(tables)}개({' + '.join(tables)})인데 `조인`이 없습니다."
            )
        from_clause += f" ON {join}"

    sql = f"SELECT {spec['집계']} AS value FROM {from_clause}"
    condition = spec.get("조건")
    if condition:
        sql += f" WHERE {condition}"
    return sql


def build_sql(
    metric_spec: Dict[str, Any],
    dataset: Optional[str] = None,
    staging_map: Optional[Dict[str, str]] = None,
) -> str:
    """지표 하나의 SQL을 조립한다. 기초형(카운트·금액)과 비율형만 직접 SQL을 갖는다.

    파생형·변화율형은 다른 지표의 결과를 조합할 뿐이라 자기 SQL이 없다 —
    calculate()가 의존 지표를 먼저 계산한 뒤 나눗셈으로 만든다.

    `staging_map`은 {"usage_history": "staging_usage_history"} 형태다. 여기 있는
    테이블만 스테이징으로 바뀌고, 없는 테이블은 원본을 그대로 읽는다(부분 갱신).
    """
    dataset = dataset or config.BQ_DATASET
    staging_map = staging_map or {}
    calc = metric_spec.get("계산") or {}
    kind = str(metric_spec.get("유형", ""))

    if calc.get("그룹핑"):
        raise CalculationError(
            "`계산.그룹핑`이 있는 지표는 채널별로 여러 행을 반환해 구조가 다릅니다. "
            "지표별 전용 SQL을 만들지 않기 위해 지원하지 않습니다."
        )
    if "시차" in calc:
        raise CalculationError("변화율형은 직접 SQL이 없습니다. 기준 지표를 두 시점 계산해 만듭니다.")

    if "분자" in calc and isinstance(calc.get("분자"), dict):  # 비율형
        numerator = _leg_sql(calc["분자"], dataset, staging_map)
        denominator = _leg_sql(calc["분모"], dataset, staging_map)
        return (
            f"WITH n AS ({numerator}), d AS ({denominator}) "
            "SELECT (SELECT value FROM n) AS numerator, "
            "(SELECT value FROM d) AS denominator, "
            "SAFE_DIVIDE((SELECT value FROM n), (SELECT value FROM d)) AS value"
        )

    if "분자" in calc:  # 파생형 — 분자·분모가 다른 지표다
        raise CalculationError("파생형은 직접 SQL이 없습니다. 의존 지표를 먼저 계산합니다.")

    if "원천" in calc:  # 기초형
        return _leg_sql(calc, dataset, staging_map)

    raise CalculationError(f"계산 명세를 해석하지 못했습니다 (유형: {kind or '미상'}).")


# ── 실행 ──────────────────────────────────────────────────────────────
def _run(client: bigquery.Client, sql: str, params: Dict[str, Any]) -> List[Any]:
    """SQL에 실제로 등장하는 자리표시자만 골라 파라미터로 바인딩한다."""
    used = set(PLACEHOLDER_PATTERN.findall(sql))
    query_params = []
    if "@month_start" in used:
        query_params.append(
            bigquery.ScalarQueryParameter("month_start", "DATE", params["month_start"])
        )
    if "@month_end" in used:
        query_params.append(
            bigquery.ScalarQueryParameter("month_end", "DATE", params["month_end"])
        )
    if "@month" in used:
        query_params.append(bigquery.ScalarQueryParameter("month", "STRING", params["month"]))
    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    return list(client.query(sql, job_config=job_config).result())


def check_placeholder(metric_id: str, spec: Dict[str, Any], sql: str) -> None:
    """`기간.자리표시자` 선언이 실제 SQL과 맞는지 본다.

    정의서가 실제 계산을 잘못 설명하고 있는 상태로 무인 실행이 도는 것을 막는다.
    """
    declared = str(((spec.get("기간") or {}).get("자리표시자") or "")).strip()
    if not declared:
        return
    used = set(PLACEHOLDER_PATTERN.findall(sql))
    if declared.startswith("없음"):
        if used:
            raise CalculationError(
                f"자리표시자를 '없음'으로 선언했는데 SQL이 {sorted(used)}를 씁니다. "
                "정의서의 기간 선언이 실제 계산과 어긋납니다."
            )
        return
    if declared not in sql:
        raise CalculationError(
            f"선언한 자리표시자({declared})가 SQL에 없습니다. 정의서의 계산 필드와 "
            "기간 선언이 어긋났을 수 있습니다."
        )


def _min_sample(spec: Dict[str, Any]) -> Optional[float]:
    value = spec.get("최소표본")
    return float(value) if isinstance(value, (int, float)) else None


def _dep_ids(text: Any) -> List[str]:
    return [ref.strip() for ref in _WIKILINK_RE.findall(str(text or ""))]


def _safe_divide(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """0 나누기·None을 조용히 0으로 만들지 않는다. 값이 없으면 없는 채로 둔다."""
    if a is None or b in (None, 0):
        return None
    return a / b


# ── 함수 3: 계산 ──────────────────────────────────────────────────────
def calculate(
    metrics_to_run: Iterable[Any],
    period: Any,
    staging_map: Dict[str, str],
    client: bigquery.Client,
    override: bool = False,
    metrics_catalog: Optional[dict] = None,
    dataset: Optional[str] = None,
    max_months: int = 24,
    cache: Optional[Dict[Tuple[str, str], "Result"]] = None,
) -> pd.DataFrame:
    """지표를 계산해 결과 표를 반환한다.

    `metrics_to_run`은 metric_id 문자열 목록이거나 profile.judge_metrics()가 낸
    dict 목록이다. 의존 지표를 따라가야 하므로 정의는 `metrics_catalog`에서 읽는다
    (없으면 catalog/metrics_catalog.json을 읽는다).

    **유효구간 밖 처리** (CLAUDE.md 6절·9절)
      - `override=False`: SQL을 실행하지 않고 status "유효구간 밖". 0을 넣지 않는다.
      - `override=True`: 계산하고 status "구간확장". 사용자가 게이트에서 승인한 경우다.

    **상태 우선순위는 표본부족 > 구간확장 > OK다.** 둘 다 해당하면 표본부족을 남기고
    구간확장 여부는 `구간확장` 열에 따로 남긴다 — 값의 신뢰도에 대한 경고가 더 중요하다.

    **한 지표에서 오류가 나도 나머지는 계속 계산한다.** 그 지표만 status "계산오류"가
    되고 이유가 붙는다. 무인 실행 중 하나가 깨졌다고 전부 잃지 않게 한다.
    """
    dataset = dataset or config.BQ_DATASET
    catalog = metrics_catalog or _load_catalog()
    months = normalize_period(period, max_months)
    metric_ids = _metric_ids(metrics_to_run)

    # 호출자가 캐시를 넘기면 여러 번 호출해도 같은 (지표, 월)을 다시 계산하지 않는다.
    # **staging_map이 다르면 캐시를 공유하면 안 된다** — 같은 (지표, 월)이라도 스테이징을
    # 쓰느냐 원본을 쓰느냐에 따라 결과가 다르기 때문이다 (charts.build_trend 참고).
    cache = {} if cache is None else cache
    rows: List[Result] = []
    for metric_id in metric_ids:
        for month in months:
            rows.append(
                _compute(metric_id, month, catalog, client, dataset, staging_map, override, cache)
            )

    frame = pd.DataFrame([vars(row) for row in rows])
    return frame.reindex(columns=RESULT_COLUMNS) if not frame.empty else pd.DataFrame(
        columns=RESULT_COLUMNS
    )


def _load_catalog() -> dict:
    try:
        return json.loads(config.METRICS_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalculationError(f"지표 카탈로그를 읽지 못했습니다: {exc}") from exc


def _metric_ids(metrics_to_run: Iterable[Any]) -> List[str]:
    ids: List[str] = []
    for item in metrics_to_run or []:
        if isinstance(item, dict):
            value = item.get("metric_id")
        else:
            value = item
        if value and value not in ids:
            ids.append(str(value))
    return ids


def _compute(
    metric_id: str,
    month: str,
    catalog: dict,
    client: bigquery.Client,
    dataset: str,
    staging_map: Dict[str, str],
    override: bool,
    cache: Dict[Tuple[str, str], Result],
) -> Result:
    """지표 하나를 한 달치 계산한다. (metric_id, month) 조합은 한 번만 계산한다."""
    key = (metric_id, month)
    if key in cache:
        return cache[key]

    spec = catalog.get(metric_id)
    if not isinstance(spec, dict):
        result = Result(metric_id, metric_id, "", month, status=ERROR, 이유="카탈로그에 없는 지표입니다.")
        cache[key] = result
        return result

    sources, unresolved = profile.resolve_sources(metric_id, catalog)
    result = Result(
        metric_id=metric_id,
        지표명=spec.get("지표명", metric_id),
        유형=str(spec.get("유형", "")),
        month=month,
        min_sample=_min_sample(spec),
        원천=" + ".join(sorted(sources)),
        부분갱신=bool(sources - set(staging_map)),
    )
    cache[key] = result  # 순환 참조가 있어도 여기서 멈춘다

    if unresolved:
        result.status = ERROR
        result.이유 = f"참조 지표를 카탈로그에서 찾지 못했습니다: {', '.join(unresolved)}"
        return result

    # 유효구간 밖이면 계산 자체를 하지 않는다. 0을 넣지 않는다.
    within = in_valid_range(month, spec.get("유효구간"))
    if not within and not override:
        result.status = OUT_OF_RANGE
        result.이유 = f"{month}은 유효구간({spec.get('유효구간')}) 밖입니다. 계산하지 않았습니다."
        return result
    if not within:
        result.구간확장 = True
        result.이유 = f"{month}은 유효구간({spec.get('유효구간')}) 밖이지만 확장 승인으로 계산했습니다."

    try:
        _fill_value(result, spec, month, catalog, client, dataset, staging_map, override, cache)
    except CalculationError as exc:
        result.status = UNSUPPORTED if "지원하지 않습니다" in str(exc) else ERROR
        result.이유 = str(exc)
    except Exception as exc:  # BigQuery 오류 등 — 이 지표만 포기하고 나머지는 계속한다
        result.status = ERROR
        result.이유 = f"{type(exc).__name__}: {exc}"
    return result


def _fill_value(
    result: Result,
    spec: Dict[str, Any],
    month: str,
    catalog: dict,
    client: bigquery.Client,
    dataset: str,
    staging_map: Dict[str, str],
    override: bool,
    cache: Dict[Tuple[str, str], Result],
) -> None:
    calc = spec.get("계산") or {}
    params = month_params(month)

    if "시차" in calc:  # 변화율형 — 명세로 판별한다(metric_id 화이트리스트가 아니다)
        _fill_change_rate(result, calc, month, catalog, client, dataset, staging_map, override, cache)
        return

    if "분자" in calc and not isinstance(calc.get("분자"), dict):  # 파생형
        _fill_derived(result, calc, month, catalog, client, dataset, staging_map, override, cache)
        return

    sql = build_sql(spec, dataset, staging_map)  # 기초형·비율형
    check_placeholder(result.metric_id, spec, sql)
    rows = _run(client, sql, params)
    row = rows[0] if rows else None

    if "분자" in calc:  # 비율형 — 분모가 표본이다
        result.value = row.value if row else None
        result.sample_size = row.denominator if row else None
    else:  # 기초형
        result.value = row.value if row else None
        # 카운트형만 값 자체가 표본 크기다. 금액형에 표본을 붙이면 화면에
        # "표본 27,557,480원" 같은 뜻 없는 숫자가 나온다.
        if result.유형.startswith("카운트형"):
            result.sample_size = result.value

    _apply_sample_check(result)


def _fill_derived(
    result: Result,
    calc: Dict[str, Any],
    month: str,
    catalog: dict,
    client: bigquery.Client,
    dataset: str,
    staging_map: Dict[str, str],
    override: bool,
    cache: Dict[Tuple[str, str], Result],
) -> None:
    """파생형 — 분자·분모가 다른 지표다. 먼저 계산하고 나눈다."""
    numerator_ids = _dep_ids(calc.get("분자"))
    denominator_ids = _dep_ids(calc.get("분모"))
    if not numerator_ids or not denominator_ids:
        raise CalculationError("파생형인데 분자·분모의 [[metric_id]] 참조를 찾지 못했습니다.")

    numerator = _compute(
        numerator_ids[0], month, catalog, client, dataset, staging_map, override, cache
    )
    denominator = _compute(
        denominator_ids[0], month, catalog, client, dataset, staging_map, override, cache
    )

    if _propagate(result, (numerator, denominator)):
        return

    result.value = _safe_divide(numerator.value, denominator.value)
    result.sample_size = denominator.value  # 분모가 표본이다
    empty = ""
    if numerator.value is None or denominator.value in (None, 0):
        missing = numerator if numerator.value is None else denominator
        empty = (
            f"의존 지표 {missing.metric_id}의 값이 없거나 0이라 나눌 수 없습니다. "
            f"({missing.metric_id}: {missing.status})"
        )
    _apply_sample_check(result, empty)


def _fill_change_rate(
    result: Result,
    calc: Dict[str, Any],
    month: str,
    catalog: dict,
    client: bigquery.Client,
    dataset: str,
    staging_map: Dict[str, str],
    override: bool,
    cache: Dict[Tuple[str, str], Result],
) -> None:
    """변화율형 = (당월 − N개월 전) / N개월 전. 같은 지표의 두 시점을 비교한다."""
    base_ids = _dep_ids(calc.get("기준지표"))
    lag = calc.get("시차")
    if not base_ids or not isinstance(lag, int) or lag >= 0:
        raise CalculationError(
            f"변화율형에는 `계산.기준지표`와 음수 정수 `계산.시차`가 필요합니다 "
            f"(받은 값: 기준지표={base_ids or None}, 시차={lag!r})"
        )

    year, mon = parse_year_month(month)
    prev_year, prev_mon = shift_year_month(year, mon, lag)
    previous_month = f"{prev_year:04d}-{prev_mon:02d}"

    current = _compute(base_ids[0], month, catalog, client, dataset, staging_map, override, cache)
    previous = _compute(
        base_ids[0], previous_month, catalog, client, dataset, staging_map, override, cache
    )

    if _propagate(result, (current, previous)):
        return

    delta = None
    if current.value is not None and previous.value is not None:
        delta = current.value - previous.value
        if str(calc.get("방향", "증가")).startswith("감소"):
            delta = -delta  # 감소율은 줄어든 만큼을 양수로 보고한다
    result.value = _safe_divide(delta, previous.value)
    empty = ""
    if previous.value in (None, 0) or current.value is None:
        empty = (
            f"기준 지표 {base_ids[0]}의 {month} 또는 {previous_month} 값이 없습니다. "
            "스테이징 테이블에는 이번에 올린 기간만 있으므로, 과거 달은 원본 테이블에서 읽어야 합니다."
        )
    _apply_sample_check(result, empty)


def _propagate(result: Result, dependencies: Sequence[Result]) -> bool:
    """의존 지표의 상태를 물려받는다. 물려받았으면 True."""
    for dependency in dependencies:
        if dependency.status in (OUT_OF_RANGE, ERROR, UNSUPPORTED):
            result.status = dependency.status
            result.이유 = f"의존 지표 {dependency.metric_id}: {dependency.이유}"
            return True
        if dependency.구간확장:
            result.구간확장 = True
    return False


EMPTY_DEFAULT = (
    "조건에 맞는 행이 없어 값이 비었습니다(NULL). 값이 0인 것과 다릅니다. "
    "스테이징 테이블에는 이번에 올린 기간만 들어 있으므로, 다른 기간을 참조하는 "
    "조건이면 원본 테이블이 필요합니다."
)


def _apply_sample_check(result: Result, empty_reason: str = "") -> None:
    """상태를 확정한다. 우선순위는 데이터 없음 > 표본부족 > 구간확장 > OK다.

    **값이 없는데 OK로 두지 않는다.** 빈 값에 OK가 붙으면 리포트에서 빈칸이
    "정상"으로 읽힌다. "값이 0"과 "값이 없다"는 다르다 (CLAUDE.md 9절).
    """
    if result.value is None:
        result.status = NO_DATA
        result.이유 = f"{result.이유} {empty_reason or EMPTY_DEFAULT}".strip()
        return

    # 표본부족은 값을 지우지 않는다 — 값은 내되 결론 근거로 쓰지 말라는 표시다.
    if (
        result.min_sample is not None
        and result.sample_size is not None
        and result.sample_size < result.min_sample
    ):
        result.status = LOW_SAMPLE
        note = f"표본 {result.sample_size:,.0f} < 최소표본 {result.min_sample:,.0f}. 결론 근거로 쓰지 않습니다."
        result.이유 = f"{result.이유} {note}".strip()
    elif result.구간확장:
        result.status = EXTENDED
    else:
        result.status = OK
