"""3단계 보조 — 당월과 전월을 비교한다.

계산 자체는 하지 않는다. `calculate.py`를 그대로 재사용해 **전월을 원본 테이블에서**
계산한 뒤, 당월 결과와 나란히 놓고 변화를 잰다.

전월 계산에서 스테이징을 쓰지 않는 이유
    스테이징 테이블에는 이번에 올린 달만 들어 있다. 전월 데이터는 이미 원본 테이블에
    적재돼 있으므로, staging_map을 빈 dict로 넘겨 원본을 읽는다.

**전월 값이 없으면 0으로 채우지 않는다.** `usage_history`의 시작월(2024-01)을 올리면
전월(2023-12)은 유효구간 밖이라 값이 없다. 이때 0으로 두면 "전월 대비 +100%"라는
거짓 보고가 된다. "비교 불가"로 남기고 이유를 적는다 (CLAUDE.md 6절·9절).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

import config
from pipeline import calculate, profile

#: 비교 상태
COMPARABLE = "비교 가능"
INCOMPARABLE = "비교 불가"

COMPARE_COLUMNS = [
    "metric_id",
    "지표명",
    "유형",
    "당월",
    "전월",
    "절대변화",
    "상대변화율",
    "퍼센트포인트변화",
    "비교상태",
    "이유",
]


class CompareError(Exception):
    """비교를 진행할 수 없을 때. 메시지는 화면에 그대로 보여줄 수 있어야 한다."""


# ── 함수 1: 전월 구하기 ───────────────────────────────────────────────
def previous_month(period: Any) -> str:
    """한 달 전을 'YYYY-MM'으로 돌려준다. 연도 경계를 넘어도 정확하다.

        "2025-01" -> "2024-12"      "2024-03" -> "2024-02"

    문자열 빼기가 아니라 **연·월을 하나의 정수 인덱스로 바꿔** 계산한다. 1월에서
    한 달을 빼면 전년 12월이 되어야 하는데, 월만 빼면 0월이 나온다.

    `profile.profile_data()`의 기간 dict나 (최소, 최대) 튜플도 받는다. 기간이 여러
    달이면 **가장 최근 달을 '당월'로 본다** — 그 달의 전월이 비교 대상이다.
    """
    month = _as_month(period)
    if not month:
        raise CompareError(f"기간에서 월을 읽지 못했습니다: {period!r}")
    year, mon = calculate.parse_year_month(month)
    prev_year, prev_mon = calculate.shift_year_month(year, mon, -1)
    return f"{prev_year:04d}-{prev_mon:02d}"


def _as_month(period: Any) -> Optional[str]:
    if isinstance(period, dict):
        return calculate.month_of(period.get("최대") or period.get("최소"))
    if isinstance(period, (tuple, list)) and len(period) == 2:
        return calculate.month_of(period[1] or period[0])
    return calculate.month_of(period)


# ── 함수 2: 전월 계산 ─────────────────────────────────────────────────
def calc_previous(
    metric_ids: Iterable[Any],
    prev_period: Any,
    client: Any,
    metrics_catalog: Optional[dict] = None,
    dataset: Optional[str] = None,
) -> pd.DataFrame:
    """전월 값을 **원본 테이블에서** 계산한다. 반환 형태는 calculate.calculate()와 같다.

    - `staging_map={}` — 전월 데이터는 이미 원본에 있다. 스테이징에는 이번 달만 있으므로
      스테이징을 쓰면 전월이 전부 비어 버린다.
    - `override=False` — 전월은 유효구간 안에 있는 것이 정상이다. 밖이라면 그건 사실이므로
      "유효구간 밖" 그대로 두고, 비교 단계에서 "비교 불가"로 흘려보낸다.
      여기서 override를 켜면 사용자가 승인하지 않은 확장이 조용히 일어난다.
    """
    return calculate.calculate(
        metric_ids,
        prev_period,
        {},
        client,
        override=False,
        metrics_catalog=metrics_catalog,
        dataset=dataset,
    )


# ── 함수 3: 비교 ──────────────────────────────────────────────────────
def compare(
    current_df: pd.DataFrame,
    previous_df: pd.DataFrame,
    metrics_catalog: Optional[dict] = None,
) -> pd.DataFrame:
    """당월·전월 결과를 metric_id로 맞춰 변화를 잰다.

    - 절대변화 = 당월 − 전월
    - 상대변화율 = (당월 − 전월) / 전월 × 100
    - 퍼센트포인트변화 = (당월 − 전월) × 100 — **비율 지표만** 낸다

    **비율 지표에 상대변화율만 내면 오해를 부른다.** 이탈률이 1%에서 2%로 오르면
    상대변화율은 +100%지만 실제 변화는 1%p다. 둘 다 내고 이름으로 구분한다.
    비율 여부는 정의서에서 유도한다(`profile.value_kind`) — `유형` 필드만 보면
    `monthly_churn_rate`처럼 파생형으로 적힌 비율 지표를 놓친다.

    **분모가 0이면 상대변화율은 None이다.** 0에서 늘어난 변화율은 정의되지 않는다.
    절대변화는 그대로 낸다 — 그건 잴 수 있다.

    **한쪽 값이라도 없으면 "비교 불가"다.** 없는 값을 0으로 보면 거짓 보고가 된다.
    """
    catalog = metrics_catalog or _load_catalog()
    current = _latest_by_metric(current_df)
    previous = _latest_by_metric(previous_df)

    rows: List[Dict[str, Any]] = []
    for metric_id, now in current.items():
        before = previous.get(metric_id)
        row: Dict[str, Any] = {
            "metric_id": metric_id,
            "지표명": now.get("지표명", metric_id),
            "유형": now.get("유형", ""),
            "당월": _number(now.get("value")),
            "전월": _number(before.get("value")) if before else None,
            "절대변화": None,
            "상대변화율": None,
            "퍼센트포인트변화": None,
            "비교상태": INCOMPARABLE,
            "이유": "",
        }

        reason = _blocking_reason(now, before)
        if reason:
            row["이유"] = reason
            rows.append(row)
            continue

        now_value = row["당월"]
        before_value = row["전월"]
        row["절대변화"] = now_value - before_value
        # 0으로 나누지 않는다. 절대변화는 이미 넣었으므로 정보가 사라지지는 않는다.
        row["상대변화율"] = (
            (now_value - before_value) / before_value * 100 if before_value != 0 else None
        )
        if profile.value_kind(metric_id, catalog) == "비율":
            row["퍼센트포인트변화"] = (now_value - before_value) * 100

        row["비교상태"] = COMPARABLE
        if row["상대변화율"] is None:
            row["이유"] = "전월이 0이라 상대변화율을 낼 수 없습니다. 절대변화만 보세요."
        rows.append(row)

    frame = pd.DataFrame(rows)
    return frame.reindex(columns=COMPARE_COLUMNS) if not frame.empty else pd.DataFrame(
        columns=COMPARE_COLUMNS
    )


def _blocking_reason(now: Dict[str, Any], before: Optional[Dict[str, Any]]) -> str:
    """비교할 수 없는 이유. 없으면 빈 문자열."""
    if before is None:
        return "전월 계산 결과에 이 지표가 없습니다."

    before_value = _number(before.get("value"))
    now_value = _number(now.get("value"))

    if before_value is None:
        status = str(before.get("status") or "값 없음")
        month = before.get("month") or "전월"
        return f"전월({month}) 값이 없습니다 — {status}. 0으로 채우지 않습니다."
    if now_value is None:
        status = str(now.get("status") or "값 없음")
        month = now.get("month") or "당월"
        return f"당월({month}) 값이 없습니다 — {status}."
    return ""


def _latest_by_metric(frame: Optional[pd.DataFrame]) -> Dict[str, Dict[str, Any]]:
    """metric_id별로 가장 최근 달의 행 하나만 남긴다.

    한 실행이 여러 달을 계산했을 수 있다. 그때 '당월'은 가장 최근 달이다.
    """
    if frame is None or len(frame) == 0 or "metric_id" not in frame.columns:
        return {}
    ordered = frame.sort_values("month") if "month" in frame.columns else frame
    return {
        str(row["metric_id"]): row.to_dict()
        for _, row in ordered.iterrows()
    }


def _number(value: Any) -> Optional[float]:
    """NaN·None을 하나로 모은다. pandas의 NaN이 섞여 들어와도 None으로 본다."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _load_catalog() -> dict:
    try:
        return json.loads(config.METRICS_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompareError(f"지표 카탈로그를 읽지 못했습니다: {exc}") from exc


def summarize(compared: pd.DataFrame) -> Dict[str, int]:
    """비교 상태별 개수. 화면·리포트가 같은 숫자를 쓰도록 여기서 센다."""
    if compared is None or len(compared) == 0:
        return {COMPARABLE: 0, INCOMPARABLE: 0}
    counts = compared["비교상태"].value_counts()
    return {
        COMPARABLE: int(counts.get(COMPARABLE, 0)),
        INCOMPARABLE: int(counts.get(INCOMPARABLE, 0)),
    }
