"""2단계 — 스키마 판정·데이터 점검·계산 대상 지표 판정.

세 함수가 순서대로 답하는 질문은 이렇다.

    judge_table()    이 파일은 어느 원천 테이블인가?      (모르면 중단한다)
    profile_data()   이 파일은 어떤 데이터인가?            (기간·결측·그레인)
    judge_metrics()  이 파일로 어느 지표를 계산할 수 있나? (계산 가능/불가 + 이유)

세 함수 모두 **판정만 하고 계산하지 않는다.** BigQuery에 붙지 않고, 현재 시각을
쓰지 않는다 — 같은 파일과 같은 카탈로그면 항상 같은 판정이 나와야 한다
(CLAUDE.md 5-5 재현성).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

import config

# ── 판정 상태 ─────────────────────────────────────────────────────────
CALCULABLE = "계산가능"
NEEDS_RANGE = "유효구간 확장 필요"
BLOCKED = "계산불가"
UNRELATED = "이 파일과 무관"
UNDECIDABLE = "판정 불가"

#: 기간 컬럼으로 볼 이름. 앞에 있는 것을 우선한다.
PERIOD_EXACT = ("year_month",)
PERIOD_SUFFIXES = ("_month", "_date")

_MONTH_RE = re.compile(r"(\d{4})-(\d{2})")
_RANGE_RE = re.compile(r"(\d{4}-\d{2})\s*~\s*(\d{4}-\d{2})")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")

#: 계산식이 들어 있는 필드. 여기서만 컬럼명을 찾는다.
_SQL_FIELDS = ("집계", "조건", "조인")


def _catalog_items(catalog: Dict[str, Any]) -> List[Tuple[str, dict]]:
    """카탈로그에서 `_meta` 같은 메타 키를 뺀 실제 항목만 돌려준다."""
    return [
        (key, value)
        for key, value in (catalog or {}).items()
        if not str(key).startswith("_") and isinstance(value, dict)
    ]


# ── 함수 1: 어느 테이블인가 ───────────────────────────────────────────
def judge_table(
    df: pd.DataFrame,
    schema_catalog: Dict[str, Any],
    min_match: Optional[float] = None,
) -> Dict[str, Any]:
    """업로드 파일이 카탈로그의 어느 원천 테이블인지 판정한다.

    **컬럼명만 보고, 순서는 보지 않는다.** 같은 테이블이라도 CSV로 내보낼 때 순서가
    바뀔 수 있고, 순서를 조건에 넣으면 멀쩡한 파일이 반려된다 (CLAUDE.md 5-1).

    **일치율의 분모는 카탈로그 테이블의 컬럼 수다.** 업로드 파일 기준으로 나누면,
    카탈로그 컬럼을 절반만 담은 파일이 "추가 컬럼 없음 = 100% 일치"로 통과해 버린다.
    분모를 카탈로그로 고정해야 "정의된 컬럼을 얼마나 갖췄는가"를 재게 된다.

    **기준 미달이면 후보를 반환하되 판정가능=False로 못 박는다.** 카탈로그에 없는
    테이블을 추측해서 통과시키면 잘못된 스키마로 계산이 돌아간다 (CLAUDE.md 9절).
    이 경우 2단계에서 중단하고 이유를 화면에 표시하는 것이 규칙이다.

    컬럼명은 공백만 정제해 **대소문자까지 그대로** 비교한다. `Customer_ID`와
    `customer_id`를 같은 것으로 봐주면 그것도 추측이다.
    """
    if min_match is None:
        min_match = config.MIN_SCHEMA_MATCH

    uploaded = [str(name).strip() for name in df.columns]
    uploaded_set = set(uploaded)

    ranked: List[Dict[str, Any]] = []
    for table_name, entry in _catalog_items(schema_catalog):
        catalog_columns = [
            str(column.get("컬럼명", "")).strip()
            for column in entry.get("컬럼", [])
            if str(column.get("컬럼명", "")).strip()
        ]
        if not catalog_columns:
            continue  # 컬럼을 못 읽은 노트는 대조 기준이 될 수 없다

        catalog_set = set(catalog_columns)
        matched = catalog_set & uploaded_set
        ranked.append(
            {
                "테이블명": table_name,
                "일치율": len(matched) / len(catalog_set),
                "일치컬럼수": len(matched),
                "카탈로그컬럼수": len(catalog_set),
                "누락컬럼": [c for c in catalog_columns if c not in uploaded_set],
                "추가컬럼": [c for c in uploaded if c not in catalog_set],
                "테이블명_추정": bool(entry.get("테이블명_추정")),
            }
        )

    if not ranked:
        return {
            "테이블명": None,
            "일치율": 0.0,
            "누락컬럼": [],
            "추가컬럼": uploaded,
            "판정가능": False,
            "이유": "스키마 카탈로그가 비어 있습니다. export_catalog.py를 먼저 실행하세요.",
            "후보": [],
            "기준": min_match,
        }

    # 동점이면 이름순으로 고른다 — 실행할 때마다 다른 테이블이 뽑히면 재현성이 깨진다.
    ranked.sort(key=lambda item: (-item["일치율"], -item["일치컬럼수"], item["테이블명"]))
    best = ranked[0]
    passed = best["일치율"] >= min_match

    if passed:
        reason = f"{best['테이블명']} 컬럼 {best['카탈로그컬럼수']}개 중 {best['일치컬럼수']}개가 일치합니다."
    else:
        reason = (
            f"가장 가까운 테이블은 {best['테이블명']}이지만 일치율 "
            f"{best['일치율']:.0%}로 기준 {min_match:.0%}에 미달합니다. "
            "카탈로그에 없는 테이블은 추측해서 통과시키지 않습니다."
        )

    return {
        "테이블명": best["테이블명"] if passed else None,
        "후보테이블명": best["테이블명"],
        "일치율": best["일치율"],
        "일치컬럼수": best["일치컬럼수"],
        "카탈로그컬럼수": best["카탈로그컬럼수"],
        "누락컬럼": best["누락컬럼"],
        "추가컬럼": best["추가컬럼"],
        "테이블명_추정": best["테이블명_추정"],
        "판정가능": passed,
        "이유": reason,
        "후보": ranked[:3],
        "기준": min_match,
    }


# ── 함수 2: 어떤 데이터인가 ───────────────────────────────────────────
def _period_columns(columns: Sequence[str]) -> List[str]:
    """기간으로 볼 만한 컬럼을 우선순위대로 고른다."""
    names = [str(c) for c in columns]
    picked: List[str] = [c for c in names if c in PERIOD_EXACT]
    for suffix in PERIOD_SUFFIXES:
        picked += [c for c in names if c.endswith(suffix) and c not in picked]
    return picked


def _min_max(series: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    values = series.dropna()
    if values.empty:
        return None, None
    try:
        return str(values.min()), str(values.max())
    except TypeError:  # 타입이 섞여 비교가 안 되면 추측하지 않는다
        return None, None


def profile_data(df: pd.DataFrame, table_info: Optional[dict] = None) -> Dict[str, Any]:
    """업로드 파일의 상태를 점검한다. 값을 고치지 않고 사실만 적는다.

    **결측은 0인 컬럼을 생략한다.** 전 컬럼을 나열하면 대부분 0이라, 정작 결측이 있는
    컬럼이 묻힌다. 화면에서 눈에 띄어야 할 것은 "결측이 있는 컬럼"이다.

    **기간은 파일 안에서 읽는다.** 현재 시각이나 업로드 시각을 쓰지 않는다. 같은
    파일을 다음 달에 다시 넣어도 같은 기간이 나와야 한다 (CLAUDE.md 5-5).

    **그레인은 판정이 아니라 후보 제시다.** 어느 조합이 이 테이블의 진짜 키인지는
    업무 지식이 필요하다. 여기서는 두 갈래로 후보를 만들고 중복 여부만 재서 보여준다.

      1. 스키마 노트의 "연결" 절에 백틱으로 적힌 키 (+ 기간 컬럼)
      2. `customer_id` + 기간 컬럼

    `table_info`는 schema_catalog에서 판정된 테이블의 항목이다. 판정에 실패했으면
    None을 넘겨도 되고, 그때는 연결 텍스트 기반 후보가 빠진다.
    """
    columns = [str(c) for c in df.columns]
    column_set = set(columns)

    missing_counts = df.isna().sum()
    missing = {
        str(name): int(count) for name, count in missing_counts.items() if int(count) > 0
    }

    period_candidates: Dict[str, Dict[str, Optional[str]]] = {}
    for column in _period_columns(columns):
        low, high = _min_max(df[column])
        period_candidates[column] = {"최소": low, "최대": high}

    primary = next(iter(period_candidates), None)
    period: Dict[str, Any] = {
        "컬럼": primary,
        "최소": period_candidates[primary]["최소"] if primary else None,
        "최대": period_candidates[primary]["최대"] if primary else None,
        "후보": period_candidates,
    }

    # ── 그레인 후보 ──
    link_text = str((table_info or {}).get("연결") or "")
    link_keys = [
        token.strip()
        for token in _BACKTICK_RE.findall(link_text)
        if token.strip() in column_set  # 실제 컬럼만. `data_customers.csv` 같은 건 뺀다
    ]
    link_keys = list(dict.fromkeys(link_keys))  # 순서 유지 중복 제거

    combos: List[Tuple[Tuple[str, ...], str]] = []

    def add(keys: Iterable[str], origin: str) -> None:
        unique = tuple(dict.fromkeys(k for k in keys if k in column_set))
        if unique and all(unique != existing for existing, _ in combos):
            combos.append((unique, origin))

    if link_keys:
        add(link_keys, "연결 텍스트")
        if primary:
            add(link_keys + [primary], "연결 텍스트 + 기간 컬럼")
    if primary:
        add(["customer_id", primary], "customer_id + 기간 컬럼")
    else:
        add(["customer_id"], "customer_id")

    grain: List[Dict[str, Any]] = []
    for keys, origin in combos:
        duplicated = int(len(df) - len(df.drop_duplicates(subset=list(keys))))
        grain.append(
            {
                "키": list(keys),
                "출처": origin,
                "중복행수": duplicated,
                "유일": duplicated == 0,
            }
        )

    return {
        "행수": int(len(df)),
        "컬럼수": int(df.shape[1]),
        "컬럼": columns,
        "결측": missing,
        "결측있는컬럼수": len(missing),
        "기간": period,
        "그레인후보": grain,
    }


# ── 함수 3: 어느 지표를 계산할 수 있나 ────────────────────────────────
def _sources_in(node: Any, found: Set[str]) -> None:
    """계산 명세 어디에 있든 `원천` 값을 모은다.

    기초형은 계산.원천에, 비율형은 계산.분자.원천·계산.분모.원천에 있다. 형태별로
    경로를 하드코딩하지 않고 훑는 이유는, 새 형태가 생겨도 코드를 안 고치기 위해서다.
    """
    if isinstance(node, dict):
        if "원천" in node:
            for token in str(node["원천"]).split("+"):
                token = token.strip()
                if token:
                    found.add(token)
        for value in node.values():
            _sources_in(value, found)
    elif isinstance(node, list):
        for value in node:
            _sources_in(value, found)


def _metric_refs(calc: Any) -> List[str]:
    """계산.분자·분모·기준지표에 적힌 `[[metric_id]]` 참조를 모은다."""
    refs: List[str] = []
    for key in ("분자", "분모", "기준지표"):
        value = (calc or {}).get(key) if isinstance(calc, dict) else None
        if isinstance(value, str):
            refs += [ref.strip() for ref in _WIKILINK_RE.findall(value)]
    return list(dict.fromkeys(refs))


def _resolve_sources(
    metric_id: str,
    metrics_catalog: Dict[str, Any],
    seen: Optional[Set[str]] = None,
) -> Tuple[Set[str], List[str]]:
    """지표가 실제로 건드리는 테이블 전부와, 못 찾은 참조 지표 목록.

    파생형은 자기 원천이 없고 다른 지표를 가리킨다. 그 참조를 따라가지 않으면
    `arpu` 같은 지표가 "이 파일과 무관"으로 잘못 분류된다 — 실제로는 분자가
    `usage_history`를 쓰는데도 그렇다.
    """
    seen = seen or set()
    if metric_id in seen:  # 순환 참조가 있어도 멈춘다
        return set(), []
    seen.add(metric_id)

    entry = metrics_catalog.get(metric_id)
    if not isinstance(entry, dict):
        return set(), [metric_id]

    calc = entry.get("계산") or {}
    sources: Set[str] = set()
    _sources_in(calc, sources)

    unresolved: List[str] = []
    for ref in _metric_refs(calc):
        ref_sources, ref_unresolved = _resolve_sources(ref, metrics_catalog, seen)
        sources |= ref_sources
        unresolved += ref_unresolved

    return sources, list(dict.fromkeys(unresolved))


def _required_lag(
    metric_id: str,
    metrics_catalog: Dict[str, Any],
    seen: Optional[Set[str]] = None,
) -> int:
    """이 지표를 계산하는 데 필요한 과거 개월 수 (변화율형의 `계산.시차`).

    시차가 있는 지표는 원천 테이블이 하나여도 **업로드한 달만으로는 계산되지 않는다.**
    3개월 전 값이 기존 테이블에 있어야 한다. 이걸 표시하지 않으면 "이 파일만으로
    계산됩니다"가 거짓말이 된다.
    """
    seen = seen or set()
    if metric_id in seen:
        return 0
    seen.add(metric_id)

    entry = metrics_catalog.get(metric_id)
    if not isinstance(entry, dict):
        return 0

    calc = entry.get("계산") or {}
    lag = 0
    raw = calc.get("시차") if isinstance(calc, dict) else None
    if isinstance(raw, (int, float)):
        lag = abs(int(raw))

    for ref in _metric_refs(calc):
        lag = max(lag, _required_lag(ref, metrics_catalog, seen))
    return lag


def _sql_fragments(
    metric_id: str, metrics_catalog: Dict[str, Any], seen: Optional[Set[str]] = None
) -> List[str]:
    """지표(와 참조 지표)의 계산식 문자열을 모은다."""
    seen = seen or set()
    if metric_id in seen:
        return []
    seen.add(metric_id)

    entry = metrics_catalog.get(metric_id)
    if not isinstance(entry, dict):
        return []
    calc = entry.get("계산") or {}

    fragments: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for field in _SQL_FIELDS:
                if field in node:
                    fragments.append(str(node[field]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(calc)
    for ref in _metric_refs(calc):
        fragments += _sql_fragments(ref, metrics_catalog, seen)
    return fragments


def columns_used(
    metric_id: str, metrics_catalog: Dict[str, Any], table: Optional[str] = None
) -> Set[str]:
    """지표가 쓰는 컬럼 이름. 파생형은 참조 지표까지 따라간다.

    `계산.집계`·`조건`·`조인`은 실행되는 SQL이므로, 여기 등장하는 식별자가 곧
    필요한 컬럼이다. SQL 키워드·함수명도 함께 잡히지만, 이 결과는 **누락 컬럼 목록과
    교집합을 낼 때만** 쓰므로 문제가 되지 않는다.

    `table`을 주면 **다른 테이블로 한정된 컬럼은 뺀다.** `customers.churn_date`는
    업로드한 `usage_history`에 없어도 되는 컬럼이라, 빼지 않으면 멀쩡한 지표를
    계산불가로 막게 된다.
    """
    text = " ".join(_sql_fragments(metric_id, metrics_catalog))
    if not text:
        return set()

    names = set(_IDENTIFIER_RE.findall(text))
    if not table:
        return names

    qualified = _QUALIFIED_RE.findall(text)
    own = {column for owner, column in qualified if owner == table}
    others = {column for owner, column in qualified if owner != table}
    return (names - others) | own


#: 금액으로 볼 집계 대상. 이 토큰이 없으면 금액으로 보지 않는다 —
#: avg_data_usage는 유형이 금액형이지만 집계가 AVG(data_gb)라 실제로는 GB다.
MONEY_TOKENS = ("billing_amount", "acquisition_cost", "mrr_change", "revenue", "cost", "price")

#: 데이터 사용량으로 볼 집계 대상. 컬럼 이름의 단위 접미사를 근거로 삼는다.
GB_TOKENS = ("data_gb", "_gb")


def value_kind(
    metric_id: str, metrics_catalog: Dict[str, Any], seen: Optional[Set[str]] = None
) -> str:
    """값의 단위를 정의서에서 유도한다. 금액 / 비율 / 명 / 건 / 수.

    **`유형`만으로는 부족하다.** 파생형은 분자·분모가 무엇이냐에 따라 금액도 되고
    비율도 된다 — `arpu`는 금액(분자가 매출), `monthly_churn_rate`는 비율(분자·분모가
    둘 다 고객 수)이다. 그래서 `[[metric_id]]` 참조를 따라간다.

    금액형이어도 집계 대상이 돈이 아니면 금액으로 보지 않는다. 틀린 단위를 붙이느니
    숫자만 내는 편이 낫다.

    화면·비교·리포트·이메일이 같은 판별을 써야 하므로 여기 한 곳에만 둔다.
    """
    seen = seen or set()
    if metric_id in seen:
        return "수"
    seen.add(metric_id)

    spec = metrics_catalog.get(metric_id) or {}
    kind = str(spec.get("유형", ""))
    calc = spec.get("계산") or {}

    if "시차" in calc or kind.startswith("비율형"):
        return "비율"
    if kind.startswith("카운트형"):
        text = f"{calc.get('집계', '')} {spec.get('지표명', '')}"
        return "명" if ("customer" in text or "고객" in text or "사용자" in text) else "건"
    if kind.startswith("금액형"):
        aggregate = json.dumps(calc, ensure_ascii=False).lower()
        if any(token in aggregate for token in MONEY_TOKENS):
            return "금액"
        if any(token in aggregate for token in GB_TOKENS):
            return "GB"
        return "수"
    if kind.startswith("파생형"):
        numerator = _metric_refs({"분자": calc.get("분자")})
        denominator = _metric_refs({"분모": calc.get("분모")})
        top = value_kind(numerator[0], metrics_catalog, seen) if numerator else "수"
        bottom = value_kind(denominator[0], metrics_catalog, seen) if denominator else "수"
        if top == "금액":
            return "금액"
        if top in ("명", "건") and bottom in ("명", "건"):
            return "비율"
    return "수"


def resolve_sources(
    metric_id: str, metrics_catalog: Dict[str, Any]
) -> Tuple[Set[str], List[str]]:
    """지표가 실제로 건드리는 테이블 전부와, 못 찾은 참조 지표 목록.

    파생형·변화율형은 참조를 따라간다. calculate.py가 스테이징 치환 대상을 정할 때도
    같은 규칙을 써야 하므로 여기서 공개한다.
    """
    return _resolve_sources(metric_id, metrics_catalog)


def _to_month(value: Any) -> Optional[str]:
    """'2025-01', '2025-01-15', Timestamp 무엇이 와도 'YYYY-MM'으로 줄인다."""
    if value is None:
        return None
    match = _MONTH_RE.search(str(value))
    return f"{match.group(1)}-{match.group(2)}" if match else None


def _normalize_period(period: Any) -> Tuple[Optional[str], Optional[str]]:
    """profile_data()의 기간 dict, (최소, 최대) 튜플, 단일 문자열을 모두 받는다."""
    if period is None:
        return None, None
    if isinstance(period, dict):
        return _to_month(period.get("최소")), _to_month(period.get("최대"))
    if isinstance(period, (tuple, list)) and len(period) == 2:
        return _to_month(period[0]), _to_month(period[1])
    single = _to_month(period)
    return single, single


def _parse_range(value: Any) -> Tuple[Optional[str], Optional[str]]:
    match = _RANGE_RE.search(str(value or ""))
    if match:
        return match.group(1), match.group(2)
    single = _to_month(value)
    return (single, single) if single else (None, None)


def judge_metrics(
    table_name: Optional[str],
    period: Any,
    metrics_catalog: Dict[str, Any],
    missing_columns: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """업로드된 테이블·기간으로 각 지표를 계산할 수 있는지 판정한다.

    **`table_name`은 schema_catalog의 키, 즉 테이블명이어야 한다 — 노트명이 아니다.**
    노트명은 `data_usage_history`, 지표 정의서의 계산.원천은 `usage_history`다.
    노트명을 그대로 넘기면 어느 지표의 원천과도 맞지 않아 **계산 대상이 0개**가 된다
    (CLAUDE.md 3절). judge_table()이 돌려주는 테이블명은 이미 이 기준을 따른다.

    **부분 갱신을 따로 표시하는 이유**: 원천이 `usage_history + customers`인 지표는
    업로드 파일만으로 계산되지 않고 기존 테이블과 조인해야 한다. "이 파일 하나로
    끝나는 계산"과 구분해 두지 않으면, 조인 대상이 낡았을 때 알아챌 수 없다.

    **유효구간 밖을 계산가능으로 넘기지 않는다.** 유효구간은 사람이 위키에서
    큐레이션한 사실이므로 앱이 늘리지 않는다. 대신 "유효구간 확장 필요"로 표시해
    무엇을 고쳐야 하는지 알린다 — 0으로 채우거나 조용히 계산하지 않는다
    (CLAUDE.md 6절·9절).

    **누락 컬럼은 지표 단위로 본다.** `missing_columns`를 주면, 그 컬럼을 실제로 쓰는
    지표만 "계산불가"가 된다. 컬럼 하나가 빠졌다고 그 컬럼을 안 쓰는 지표까지 막으면
    멀쩡한 계산을 잃는다.

    상태는 다섯 중 하나다.
      계산가능 / 유효구간 확장 필요 / 계산불가 / 이 파일과 무관 / 판정 불가
    """
    low, high = _normalize_period(period)
    missing = {str(name).strip() for name in (missing_columns or []) if str(name).strip()}
    results: List[Dict[str, Any]] = []

    for metric_id, entry in sorted(_catalog_items(metrics_catalog)):
        sources, unresolved = _resolve_sources(metric_id, metrics_catalog)
        others = sorted(sources - {table_name})
        lag = _required_lag(metric_id, metrics_catalog)
        row: Dict[str, Any] = {
            "metric_id": metric_id,
            "지표명": entry.get("지표명", metric_id),
            "유형": entry.get("유형"),
            "원천": sorted(sources),
            "다른원천": others,
            "부분갱신": False,
            "필요과거개월": lag,
            "누락컬럼": [],
            "유효구간": entry.get("유효구간"),
            "업로드기간": [low, high],
        }

        if not table_name:
            row.update(
                상태=UNDECIDABLE,
                이유="업로드 파일의 테이블을 판정하지 못해 지표를 가릴 수 없습니다.",
            )
            results.append(row)
            continue

        if table_name not in sources:
            reason = f"이 파일을 원천으로 쓰지 않습니다. 원천: {' + '.join(sorted(sources)) or '미상'}"
            if unresolved:
                reason += f" (참조 지표 {', '.join(unresolved)}를 카탈로그에서 찾지 못했습니다)"
            row.update(상태=UNRELATED, 이유=reason)
            results.append(row)
            continue

        row["부분갱신"] = bool(others)

        # 이 지표가 실제로 쓰는 컬럼이 파일에 없으면, 유효구간을 따질 것도 없이 못 센다.
        if missing:
            blocked_by = sorted(missing & columns_used(metric_id, metrics_catalog, table_name))
            if blocked_by:
                row["누락컬럼"] = blocked_by
                row.update(
                    상태=BLOCKED,
                    이유=(
                        f"계산에 쓰는 컬럼이 업로드 파일에 없습니다: {', '.join(blocked_by)}. "
                        "원본 파일에 컬럼을 채워 다시 올리세요."
                    ),
                )
                results.append(row)
                continue

        if unresolved:
            row.update(
                상태=UNDECIDABLE,
                이유=f"참조 지표 {', '.join(unresolved)}가 카탈로그에 없어 원천을 다 확인하지 못했습니다.",
            )
            results.append(row)
            continue

        valid_low, valid_high = _parse_range(entry.get("유효구간"))
        if low is None or high is None:
            row.update(
                상태=UNDECIDABLE,
                이유="업로드 파일에서 기간을 읽지 못해 유효구간과 대조할 수 없습니다.",
            )
        elif valid_low is None or valid_high is None:
            row.update(
                상태=UNDECIDABLE,
                이유=f"지표의 유효구간을 해석하지 못했습니다: {entry.get('유효구간')!r}",
            )
        elif valid_low <= low and high <= valid_high:
            needs = []
            if others:
                needs.append(f"기존 테이블({', '.join(others)})과의 조인")
            if lag:
                needs.append(f"{lag}개월 전 {table_name} 데이터")
            note = f"추가로 필요: {', '.join(needs)}" if needs else "이 파일만으로 계산됩니다."
            row.update(
                상태=CALCULABLE,
                이유=f"업로드 기간 {low}~{high}이 유효구간 {valid_low}~{valid_high} 안입니다. {note}",
            )
        else:
            row.update(
                상태=NEEDS_RANGE,
                이유=(
                    f"업로드 기간 {low}~{high}이 유효구간 {valid_low}~{valid_high}을 벗어납니다. "
                    "위키 노트의 유효구간을 갱신하고 카탈로그를 다시 export하세요."
                ),
            )

        results.append(row)

    return results


def summarize_metrics(judged: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """상태별 개수. 화면·리포트에 같은 숫자를 찍기 위해 여기서 센다."""
    summary = {CALCULABLE: 0, NEEDS_RANGE: 0, BLOCKED: 0, UNRELATED: 0, UNDECIDABLE: 0}
    for row in judged:
        summary[row["상태"]] = summary.get(row["상태"], 0) + 1
    return summary
