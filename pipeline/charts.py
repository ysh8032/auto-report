"""4단계 보조 — 추이 데이터 준비.

여러 달을 계산해 하나의 long format 표로 만든다. 그리는 일은 하지 않는다 —
화면(app.py)이 이 표를 받아 차트를 그린다.

**유효구간 밖인 달을 0으로 그리지 않는다.** 값을 0으로 채우면 그래프가 급락한 것처럼
보여서, 데이터가 없다는 사실이 "지표가 떨어졌다"로 읽힌다. 값은 None으로 두고 status에
이유를 남겨 선이 끊기게 한다 (CLAUDE.md 6절·9절).

**스테이징은 당월에만 적용한다.** 스테이징 테이블에는 이번에 올린 달만 들어 있으므로,
이전 달을 스테이징으로 조회하면 전부 빈 값이 된다. 이전 달은 원본 테이블에서 읽는다.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

import config
from pipeline import calculate

TREND_COLUMNS = ["metric_id", "month", "value", "status"]

#: 진행 상황 콜백. (끝낸 개수, 전체 개수, 방금 끝낸 월)
ProgressCallback = Callable[[int, int, str], None]


def trend_months(end_period: Any, months: Optional[int] = None) -> List[str]:
    """end_period를 마지막으로 하는 월 목록을 오래된 순으로 만든다.

        trend_months("2025-01", 6) -> ["2024-08", ..., "2025-01"]
    """
    count = int(config.TREND_MONTHS if months is None else months)
    if count < 1:
        raise calculate.CalculationError(f"추이 개월 수는 1 이상이어야 합니다: {count}")

    end = calculate.month_of(end_period)
    if not end:
        raise calculate.CalculationError(f"기준 월을 읽지 못했습니다: {end_period!r}")

    year, month = calculate.parse_year_month(end)
    result: List[str] = []
    for offset in range(count - 1, -1, -1):
        shifted_year, shifted_month = calculate.shift_year_month(year, month, -offset)
        result.append(f"{shifted_year:04d}-{shifted_month:02d}")
    return result


def build_trend(
    metric_ids: Iterable[Any],
    end_period: Any,
    months: Optional[int] = None,
    staging_map: Optional[Dict[str, str]] = None,
    client: Any = None,
    metrics_catalog: Optional[dict] = None,
    dataset: Optional[str] = None,
    override: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> pd.DataFrame:
    """end_period부터 거꾸로 months개월분을 계산해 long format으로 돌려준다.

    반환 컬럼: `metric_id`, `month`, `value`, `status`

    **캐시를 두 개로 나눈다.** 당월은 스테이징 테이블을, 이전 달은 원본 테이블을 읽으므로
    같은 (지표, 월)이라도 결과가 다르다. 캐시를 하나로 합치면, 당월 계산이 시차 때문에
    끌어다 쓴 과거 달의 **빈** 결과가 그 달의 추이 값으로 재사용된다. 그래서 이전 달들만
    캐시를 공유한다 — 그 안에서는 시차 참조가 겹쳐도 한 번만 계산된다.

    **`override`는 당월에만 적용한다.** 사용자가 게이트에서 승인한 것은 이번에 올린
    기간이다. 과거 달까지 조용히 확장하면 승인 범위를 넘어선다. 과거 달이 유효구간
    밖이면 값 없이 status만 남아 선이 끊긴다.
    """
    staging_map = staging_map or {}
    schedule = trend_months(end_period, months)
    current = schedule[-1]

    history_cache: Dict[Tuple[str, str], Any] = {}
    frames: List[pd.DataFrame] = []

    for index, month in enumerate(schedule, start=1):
        is_current = month == current
        frame = calculate.calculate(
            metric_ids,
            month,
            staging_map if is_current else {},
            client,
            override=override and is_current,
            metrics_catalog=metrics_catalog,
            dataset=dataset,
            cache=None if is_current else history_cache,
        )
        frames.append(frame)
        if progress is not None:
            progress(index, len(schedule), month)

    if not frames:
        return pd.DataFrame(columns=TREND_COLUMNS)

    trend = pd.concat(frames, ignore_index=True)
    trend = trend.reindex(columns=TREND_COLUMNS)
    return trend.sort_values(["metric_id", "month"]).reset_index(drop=True)


def to_wide(trend: pd.DataFrame, metric_id: str) -> pd.DataFrame:
    """한 지표의 추이를 차트에 넣기 좋은 형태로 자른다.

    값이 없는 달의 행을 **지우지 않는다.** 행을 지우면 없는 달이 그래프에서 사라져
    앞뒤 점이 이어져 버린다. None으로 남겨야 선이 끊긴다.
    """
    if trend is None or len(trend) == 0:
        return pd.DataFrame(columns=["month", "value", "status"])
    part = trend[trend["metric_id"] == metric_id]
    return part[["month", "value", "status"]].reset_index(drop=True)


def coverage(trend: pd.DataFrame, metric_id: str) -> Dict[str, Any]:
    """그린 달과 못 그린 달을 센다. 화면에 "6개월 중 4개월만 값이 있습니다"를 적기 위해."""
    part = to_wide(trend, metric_id)
    total = len(part)
    filled = int(part["value"].notna().sum()) if total else 0
    missing = [
        {"month": row["month"], "status": row["status"]}
        for _, row in part.iterrows()
        if pd.isna(row["value"])
    ]
    return {"전체": total, "값있음": filled, "값없음": total - filled, "빈달": missing}
