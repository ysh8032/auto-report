"""3단계 — 기계적 검증 게이트.

계산 결과를 **기계적으로 판정 가능한 것만** 점검한다. 판단이 필요한 검증은 하지 않고,
하지 않았다는 사실을 `자동검증하지_않은_것`으로 반환한다. 이 목록이 리포트 한계 절에
그대로 들어간다 (CLAUDE.md 6절).

검증 5종
    1. check_valid_range         유효구간 밖 / 구간확장
    2. check_min_sample          최소표본 미달
    3. check_derived_consistency 파생지표를 기초지표로 재조합해 대조
    4. check_month_over_month    전월 대비 이상 변동
    5. check_totals              세그먼트 합계 대조

모든 검사는 같은 형태의 결과를 리스트로 돌려준다.
    {검증명, 대상지표, 판정, 상세, 값}

**검사를 못 한 것을 통과로 적지 않는다.** 의존 지표가 없어 대조할 수 없으면 "검사 불가"
경고로 남긴다. 통과와 미검사를 섞으면 "검증 통과"가 실제보다 강한 신뢰를 준다.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import config
from pipeline import calculate, compare, profile

# ── 판정 ──────────────────────────────────────────────────────────────
PASS = "통과"
WARN = "경고"
BLOCK = "차단"

#: 재조합 대조에서 허용할 상대 오차. 0.01%.
DERIVED_TOLERANCE = 0.0001

#: 자동으로 하지 않는 검증. 리포트 한계 절에 그대로 들어간다 (CLAUDE.md 6절).
NOT_AUTOMATED: List[str] = ["혼입 변수 층화", "역인과 검토", "가설 검정"]

#: 리포트에 반드시 들어가는 문장. 빼면 "검증 통과"가 실제보다 강한 신뢰를 준다.
LIMITATION_SENTENCE = (
    "이 검증은 기계적 점검만 수행했으며, 혼입 변수·역인과 검토는 포함되지 않았다."
)

RESULT_KEYS = ["검증명", "대상지표", "판정", "상세", "값"]


def _result(name: str, target: str, verdict: str, detail: str, value: Any = None) -> Dict[str, Any]:
    return {"검증명": name, "대상지표": target, "판정": verdict, "상세": detail, "값": value}


def _number(value: Any) -> Optional[float]:
    """NaN·None을 하나로 모은다."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _rows(frame: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    if frame is None or len(frame) == 0:
        return []
    return [row.to_dict() for _, row in frame.iterrows()]


def _load_catalog() -> dict:
    return json.loads(config.METRICS_CATALOG_PATH.read_text(encoding="utf-8"))


def _join(names: List[str]) -> str:
    return ", ".join(sorted(set(names)))


# ── 1. 유효구간 ───────────────────────────────────────────────────────
def check_valid_range(
    metrics_df: pd.DataFrame, catalog: Optional[dict] = None, override: bool = False
) -> List[Dict[str, Any]]:
    """유효구간 밖은 차단, 승인받은 구간확장은 경고로 남긴다.

    **구간확장을 통과로 적지 않는 이유**: 사용자가 이번 실행에 한해 승인했을 뿐,
    정의서의 유효구간은 그대로다. 통과로 적으면 리포트를 읽는 사람이 "정의된 구간
    안에서 계산된 값"으로 오해한다. 승인 사실은 실행 기록에 남고, 여기서는 정의서
    기준으로 벗어났다는 사실을 남긴다.
    """
    name = "유효구간"
    results: List[Dict[str, Any]] = []

    for row in _rows(metrics_df):
        metric_id = str(row.get("metric_id"))
        status = str(row.get("status"))
        month = row.get("month")

        if status == calculate.OUT_OF_RANGE:
            results.append(
                _result(
                    name,
                    metric_id,
                    BLOCK,
                    f"{month}이 정의서 유효구간 밖이라 계산하지 않았습니다. 0으로 채우지 않습니다.",
                    None,
                )
            )
        elif status == calculate.EXTENDED or bool(row.get("구간확장")):
            value = _number(row.get("value"))
            # 확장 승인을 받았어도 값이 안 나온 경우가 있다(다른 기간 참조 등).
            # "계산했습니다"라고 적으면 없는 값을 낸 것처럼 읽힌다.
            note = (
                "확장 승인으로 계산했습니다."
                if value is not None
                else f"확장 승인 대상이었으나 값이 나오지 않았습니다({status})."
            )
            results.append(
                _result(
                    name,
                    metric_id,
                    WARN,
                    f"{month}은 정의서 유효구간 밖입니다. {note} 정의서 구간은 바뀌지 않았습니다.",
                    value,
                )
            )

    if not results:
        results.append(_result(name, "전체", PASS, "모든 지표가 정의서 유효구간 안입니다."))
    return results


# ── 2. 최소표본 ───────────────────────────────────────────────────────
def check_min_sample(metrics_df: pd.DataFrame) -> List[Dict[str, Any]]:
    """표본이 최소표본에 못 미치면 경고. 값은 지우지 않는다.

    **값은 내되 결론 근거로 쓰지 말라는 표시다** (CLAUDE.md 6절). 최소표본이 선언되지
    않은 지표(`해당 없음`)는 표본 개념이 없어 검사 대상이 아니다.
    """
    name = "최소표본"
    results: List[Dict[str, Any]] = []
    checked = 0

    for row in _rows(metrics_df):
        minimum = _number(row.get("min_sample"))
        sample = _number(row.get("sample_size"))
        if minimum is None or sample is None:
            continue  # 최소표본 미선언 지표, 또는 값이 없어 다른 검사가 잡을 건
        checked += 1
        if sample < minimum:
            results.append(
                _result(
                    name,
                    str(row.get("metric_id")),
                    WARN,
                    f"표본 {sample:,.0f} < 최소표본 {minimum:,.0f}. "
                    "값은 내되 결론 근거로 쓰지 않습니다.",
                    sample,
                )
            )

    if not results:
        results.append(
            _result(name, "전체", PASS, f"최소표본이 선언된 {checked}종이 모두 기준을 넘었습니다.")
        )
    return results


# ── 3. 파생지표 정합성 ────────────────────────────────────────────────
def check_derived_consistency(
    metrics_df: pd.DataFrame, catalog: Optional[dict] = None
) -> List[Dict[str, Any]]:
    """파생지표를 기초지표로 재조합해 저장된 값과 대조한다.

    `arpu = billed_revenue_active / active_customers_contract`처럼 분자·분모가 다른
    지표인 경우, 두 기초지표를 다시 나눠 저장된 값과 같은지 본다. 다르면 계산 경로
    어딘가가 어긋난 것이므로 **차단**이다.

    **의존 지표가 이번 실행에서 계산되지 않았으면 "검사 불가" 경고다.** 이번 업로드와
    무관한 원천을 쓰는 기초지표는 계산 대상에서 빠지므로 흔히 일어난다. 이때 통과로
    적으면 대조하지 않은 것을 대조했다고 보고하게 된다.

    변화율형(`계산.시차`)은 같은 지표의 두 시점을 비교하는 구조라 이 검사로 확인할 수
    없다. 건너뛴 지표를 따로 적어 남긴다.
    """
    name = "파생지표 정합성"
    catalog = catalog or _load_catalog()
    rows = _rows(metrics_df)
    values = {(str(r.get("metric_id")), str(r.get("month"))): r for r in rows}

    results: List[Dict[str, Any]] = []
    skipped: List[str] = []

    for row in rows:
        metric_id = str(row.get("metric_id"))
        month = str(row.get("month"))
        calc = (catalog.get(metric_id) or {}).get("계산") or {}

        if "시차" in calc:
            skipped.append(metric_id)
            continue
        if not isinstance(calc.get("분자"), str):
            continue  # 기초형·비율형은 재조합할 상위 구조가 없다

        numerator_ids = profile._metric_refs({"분자": calc.get("분자")})
        denominator_ids = profile._metric_refs({"분모": calc.get("분모")})
        if not numerator_ids or not denominator_ids:
            continue

        numerator = values.get((numerator_ids[0], month))
        denominator = values.get((denominator_ids[0], month))
        missing = [
            dep
            for dep, found in ((numerator_ids[0], numerator), (denominator_ids[0], denominator))
            if found is None
        ]
        if missing:
            results.append(
                _result(
                    name,
                    metric_id,
                    WARN,
                    f"검사 불가 — 의존 지표 {_join(missing)}가 이번 실행에서 계산되지 않았습니다.",
                    None,
                )
            )
            continue

        stored = _number(row.get("value"))
        top = _number(numerator.get("value"))
        bottom = _number(denominator.get("value"))

        if stored is None or top is None or bottom is None or bottom == 0:
            results.append(
                _result(
                    name,
                    metric_id,
                    WARN,
                    "검사 불가 — 값이 비었거나 분모가 0이라 재조합할 수 없습니다.",
                    stored,
                )
            )
            continue

        expected = top / bottom
        error = abs(stored - expected) / abs(expected) if expected else abs(stored - expected)
        if error <= DERIVED_TOLERANCE:
            results.append(
                _result(
                    name,
                    metric_id,
                    PASS,
                    f"{numerator_ids[0]} / {denominator_ids[0]} 재조합값과 일치합니다 "
                    f"(상대 오차 {error:.4%}).",
                    stored,
                )
            )
        else:
            results.append(
                _result(
                    name,
                    metric_id,
                    BLOCK,
                    f"재조합값 {expected:,.6g}과 저장값 {stored:,.6g}이 다릅니다 "
                    f"(상대 오차 {error:.4%} > 허용 {DERIVED_TOLERANCE:.4%}).",
                    stored,
                )
            )

    if not results:
        detail = "재조합해 대조할 파생지표가 이번 결과에 없습니다."
        if skipped:
            detail += f" 변화율형 {len(set(skipped))}종은 이 검사 대상이 아닙니다: {_join(skipped)}."
        results.append(_result(name, "해당 없음", PASS, detail))
    elif skipped:
        results.append(
            _result(
                name,
                "변화율형",
                PASS,
                f"변화율형 {len(set(skipped))}종은 두 시점 비교라 이 검사로 확인하지 "
                f"않았습니다: {_join(skipped)}.",
            )
        )
    return results


# ── 4. 전월 대비 이상 변동 ────────────────────────────────────────────
def metric_threshold(
    metric_id: str, catalog: Optional[dict], fallback: float
) -> Tuple[float, bool]:
    """(적용할 임계값, 정의서에서 왔는가).

    지표마다 "얼마나 움직이면 이상한가"가 다르다. 활성 고객 수는 계약 기반이라 1%만
    움직여도 확인할 일이지만, 데이터 사용량은 계절 변동이 커서 5%로는 경고가 쏟아진다.

    **임계값은 정의서(카탈로그)에서 읽는다.** 지표별 판단 기준은 지표 정의의 일부이고,
    정의는 위키에서만 고친다 (CLAUDE.md 3절). 앱 코드에 지표별 숫자를 쓰지 않는다.
    """
    entry = (catalog or {}).get(metric_id) or {}
    declared = entry.get("변동임계값")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool):
        return float(declared), True
    return float(fallback), False


def check_month_over_month(
    comparison_df: Optional[pd.DataFrame],
    threshold: Optional[float] = None,
    catalog: Optional[dict] = None,
) -> List[Dict[str, Any]]:
    """상대변화율의 절대값이 **지표별** 임계값 이상이면 경고한다.

    임계값은 정의서의 `변동임계값`을 쓰고, 없으면 `config.MOM_THRESHOLD`(기본 5.0)를
    쓴다. `threshold` 인자를 주면 그것이 기본값 자리를 대신한다.

    **적용한 임계값과 그 출처를 상세에 적는다.** 같은 −7% 변동이 어떤 지표에서는
    경고고 어떤 지표에서는 아닌데, 근거를 안 적으면 읽는 사람이 기준을 알 수 없다.

    **임계값 초과는 "확인해 보라"는 뜻이지 "틀렸다"는 뜻이 아니다.** 그래서 차단이
    아니라 경고다. 왜 변했는지는 기계가 알 수 없다.

    **비교 불가는 경고가 아니다.** 전월 값이 없는 것은 이상 변동이 아니라 비교할 수
    없는 상태다. 정보로만 남긴다 (CLAUDE.md 6절 — "비교 불가"로 표시).
    """
    name = "전월 대비 변동"
    fallback = config.MOM_THRESHOLD if threshold is None else float(threshold)
    rows = _rows(comparison_df)

    if not rows:
        return [_result(name, "해당 없음", PASS, "전월 비교 결과가 없어 검사하지 않았습니다.")]

    results: List[Dict[str, Any]] = []
    incomparable: List[str] = []
    applied: List[str] = []
    from_catalog: List[str] = []
    checked = 0

    for row in rows:
        metric_id = str(row.get("metric_id"))
        rate = _number(row.get("상대변화율"))
        if str(row.get("비교상태")) != compare.COMPARABLE or rate is None:
            incomparable.append(metric_id)
            continue

        limit, declared = metric_threshold(metric_id, catalog, fallback)
        source = "정의서" if declared else "기본값"
        checked += 1
        applied.append(f"{metric_id} {limit:g}%({source})")
        if declared:
            from_catalog.append(metric_id)

        if abs(rate) >= limit:
            results.append(
                _result(
                    name,
                    metric_id,
                    WARN,
                    f"전월 대비 {rate:+,.1f}%로 적용 임계값 {limit:g}%({source}) 이상 "
                    "움직였습니다. 값이 틀렸다는 뜻이 아니라 확인이 필요하다는 표시입니다.",
                    rate,
                )
            )

    if not results:
        results.append(
            _result(
                name,
                "전체",
                PASS,
                f"비교 가능한 {checked}종이 각자 임계값 안에 있습니다. "
                f"적용 임계값 · {', '.join(applied) if applied else '없음'}",
            )
        )
    else:
        # 경고가 난 경우에도 어느 기준을 썼는지 한 줄로 남긴다.
        results.append(
            _result(
                name,
                "적용 임계값",
                PASS,
                f"기본값 {fallback:g}% · 정의서 지정 {len(set(from_catalog))}종"
                + (f" ({_join(from_catalog)})" if from_catalog else "")
                + f" · {', '.join(applied)}",
            )
        )

    if incomparable:
        results.append(
            _result(
                name,
                "비교 불가",
                PASS,
                f"{len(set(incomparable))}종은 전월 값이 없어 비교하지 않았습니다"
                f"(이상 변동 아님): {_join(incomparable)}.",
            )
        )
    return results


# ── 5. 합계 대조 ──────────────────────────────────────────────────────
def check_totals(metrics_df: pd.DataFrame) -> List[Dict[str, Any]]:
    """세그먼트별 값의 합이 전체 지표와 맞는지 본다.

    `cac_by_channel`처럼 `계산.그룹핑`이 있어 채널별로 여러 행을 내는 지표가 있을 때만
    대상이 된다. 그런 지표가 이번 결과에 없으면 **"해당 없음"으로 통과**를 반환한다 —
    검사할 것이 없었다는 사실 자체를 리포트에 남기기 위해서다.
    """
    name = "합계 대조"
    rows = _rows(metrics_df)
    columns = list(metrics_df.columns) if metrics_df is not None and len(metrics_df) else []

    if not rows or "group" not in columns:
        return [
            _result(
                name,
                "해당 없음",
                PASS,
                "세그먼트로 나뉘는 지표가 이번 결과에 없어 대조할 합계가 없습니다.",
            )
        ]

    segments: Dict[str, List[Dict[str, Any]]] = {}
    totals: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = f"{row.get('metric_id')}::{row.get('month')}"
        if _blank(row.get("group")):
            totals[key] = row
        else:
            segments.setdefault(key, []).append(row)

    if not segments:
        return [
            _result(name, "해당 없음", PASS, "세그먼트 값이 있는 지표가 없어 대조할 합계가 없습니다.")
        ]

    results: List[Dict[str, Any]] = []
    for key, members in segments.items():
        metric_id = key.split("::")[0]
        segment_sum = sum(v for v in (_number(m.get("value")) for m in members) if v is not None)
        total_row = totals.get(key)
        total = _number(total_row.get("value")) if total_row else None

        if total is None:
            results.append(
                _result(
                    name,
                    metric_id,
                    WARN,
                    f"검사 불가 — 세그먼트 {len(members)}개는 있으나 대조할 전체 값이 없습니다.",
                    segment_sum,
                )
            )
            continue

        error = abs(segment_sum - total) / abs(total) if total else abs(segment_sum - total)
        results.append(
            _result(
                name,
                metric_id,
                PASS if error <= DERIVED_TOLERANCE else BLOCK,
                f"세그먼트 합 {segment_sum:,.6g} vs 전체 {total:,.6g} (상대 오차 {error:.4%}).",
                segment_sum,
            )
        )
    return results


def _blank(value: Any) -> bool:
    if value is None or value == "":
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


# ── 요약 ──────────────────────────────────────────────────────────────
def validate_all(
    metrics_df: pd.DataFrame,
    catalog: Optional[dict] = None,
    comparison_df: Optional[pd.DataFrame] = None,
    override: bool = False,
    threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """5종을 모두 실행하고 전체 판정을 낸다.

    전체 판정은 **가장 나쁜 항목을 따른다** — 차단이 하나라도 있으면 차단, 없고 경고가
    있으면 경고, 둘 다 없으면 통과. 평균이나 다수결을 쓰지 않는다. 하나라도 막을 이유가
    있으면 막아야 게이트다.

    `자동검증하지_않은_것`을 **반드시 함께 반환한다.** 이 목록과 한계 문장이 리포트에
    그대로 들어가야 "검증 통과"가 실제보다 강한 신뢰를 주지 않는다 (CLAUDE.md 6절).
    """
    catalog = catalog or _load_catalog()

    items: List[Dict[str, Any]] = []
    items += check_valid_range(metrics_df, catalog, override)
    items += check_min_sample(metrics_df)
    items += check_derived_consistency(metrics_df, catalog)
    items += check_month_over_month(comparison_df, threshold, catalog)
    items += check_totals(metrics_df)

    blocks = sum(1 for item in items if item["판정"] == BLOCK)
    warns = sum(1 for item in items if item["판정"] == WARN)

    return {
        "전체판정": BLOCK if blocks else (WARN if warns else PASS),
        "차단수": blocks,
        "경고수": warns,
        "항목별결과": items,
        "자동검증하지_않은_것": list(NOT_AUTOMATED),
        "한계문장": LIMITATION_SENTENCE,
    }
