"""데모 모드 — BigQuery 없이 앱 전체를 돌린다.

왜 있는가
    BigQuery는 익명 접근을 허용하지 않는다. 그래서 자격증명을 넣을 수 없는 자리
    (배포본을 공개해 두고 남에게 보여줘야 하는 경우)에서는 실제 계산을 할 방법이
    없다. 이 모듈은 그 자리에서 **계산을 흉내 내어 1~8단계 흐름 전체를 보여준다.**

무엇이 진짜이고 무엇이 가짜인가
    가짜 : BigQuery가 돌려주는 **숫자**. SQL을 해시해 만든 값이다.
    진짜 : 그 숫자를 가지고 하는 **판정 전부**. 스키마 대조, 유효구간, 최소표본,
           파생지표 정합성, 합계 대조, 리포트·이메일 조립은 실제 코드가 그대로 돈다.

    즉 "이 앱이 무엇을 하는가"는 정확히 보이고, "이번 달 수치가 얼마인가"만 거짓이다.

**명시적으로 켜야만 켜진다.** 자격증명이 없다고 조용히 이쪽으로 넘어오지 않는다.
인증이 끊긴 것을 모른 채 가짜 숫자를 진짜로 읽는 일이 CLAUDE.md 9절이 금지하는
거짓 보고이기 때문이다. 켜는 방법은 둘뿐이다.

    환경변수   AUTO_REPORT_DEMO=1
    secrets    demo_mode = true

켜져 있으면 `app.py`가 화면 맨 위에 걷을 수 없는 배너를 세운다. 배너를 지우려면
데모 모드를 끄는 수밖에 없다 — 가짜 숫자가 배너 없이 보이는 상태를 만들지 않는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from typing import Any, Dict, List, Sequence

#: 데모 모드의 프로젝트 이름. 화면·실행기록에 그대로 찍혀서 진짜 프로젝트와 구별된다.
FAKE_PROJECT = "demo-no-bigquery"

#: 데모 모드를 켜는 환경변수. 값이 아래 중 하나일 때만 켜진다.
ENV_FLAG = "AUTO_REPORT_DEMO"
TRUTHY = frozenset({"1", "true", "yes", "on"})

#: secrets에서 데모 모드를 켜는 키. secrets.toml의 `demo_mode = true`.
SECRETS_FLAG = "demo_mode"

#: 화면 배너 문구. 이 문장을 약하게 고치지 않는다 — 배너의 목적이 그것뿐이다.
BANNER_TITLE = "데모 모드 — 화면의 숫자는 전부 가짜입니다"
BANNER_BODY = (
    "BigQuery에 연결하지 않고 있습니다. 지표 값·추이·리포트의 수치는 "
    "실제 데이터가 아니라 만들어낸 값입니다. "
    "스키마 판정·유효구간·최소표본·검증 같은 판정 로직은 실제 코드가 그대로 돕니다."
)


# ── SQL에서 결과 컬럼 이름 뽑기 ───────────────────────────────────────
def _depth_positions(sql: str, word: str) -> List[int]:
    """괄호 밖(depth 0)에 있는 키워드 위치만 모은다.

    서브쿼리·CTE 안의 SELECT는 결과 컬럼을 정하지 않으므로 제외해야 한다.
    """
    upper = sql.upper()
    positions: List[int] = []
    depth = 0
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and upper.startswith(word, index):
            before = sql[index - 1] if index else " "
            after = sql[index + len(word)] if index + len(word) < len(sql) else " "
            if not before.isalnum() and before != "_" and not after.isalnum() and after != "_":
                positions.append(index)
                index += len(word)
                continue
        index += 1
    return positions


def _split_top_level(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def result_columns(sql: str) -> List[str]:
    """이 SQL이 돌려줄 컬럼 이름들. 바깥 SELECT의 별칭을 읽는다.

    비율형은 `WITH n AS (...), d AS (...) SELECT ... AS numerator, ... AS denominator,
    ... AS value` 형태라, 마지막 depth-0 SELECT가 실제 결과 컬럼을 정한다.
    """
    selects = _depth_positions(sql, "SELECT")
    if not selects:
        return ["value"]
    start = selects[-1] + len("SELECT")
    froms = [pos for pos in _depth_positions(sql, "FROM") if pos > start]
    projection = sql[start : froms[0]] if froms else sql[start:]

    columns: List[str] = []
    for item in _split_top_level(projection):
        alias = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", item, re.IGNORECASE)
        if alias:
            columns.append(alias.group(1))
            continue
        bare = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", item)
        columns.append(bare.group(1) if bare else "value")
    return columns or ["value"]


# ── 값 만들기 — 같은 SQL이면 항상 같은 값 ─────────────────────────────
def params_key(job_config: Any) -> str:
    """쿼리 파라미터를 해시 키에 넣기 위해 문자열로 만든다.

    **이게 없으면 달마다 같은 값이 나온다.** 월은 SQL 본문이 아니라 `@month`
    파라미터로 넘어가므로, SQL만 해시하면 2024-08과 2024-12가 같은 값이 되어
    추이 그래프가 평평한 선이 된다. 차트를 확인하려고 만든 도구가 차트를 못 보게
    만드는 셈이다.
    """
    parameters = getattr(job_config, "query_parameters", None) or []
    parts = []
    for parameter in parameters:
        name = getattr(parameter, "name", "?")
        value = getattr(parameter, "value", None)
        parts.append(f"{name}={value}")
    return ";".join(parts)


def _hash_unit(sql: str, column: str) -> float:
    """SQL+컬럼을 해시해 0~1 사이 실수로 만든다.

    난수를 쓰지 않는 이유: 코드를 고칠 때마다 화면 숫자가 흔들리면 "내가 고쳐서
    바뀐 것"인지 "원래 흔들리는 것"인지 구분할 수 없다. 같은 SQL은 항상 같은 값이
    나와야 화면 비교가 가능하다.
    """
    digest = hashlib.sha256(f"{sql}||{column}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def _scaled(unit: float, low: float, high: float) -> float:
    return low + unit * (high - low)


def fake_value(sql: str, column: str) -> Any:
    """컬럼 하나의 가짜 값. SQL에 적힌 집계 모양을 보고 자릿수를 맞춘다.

    자릿수를 맞추는 이유는 그럴듯해 보이려는 게 아니라, **판정 로직을 건드리지 않기
    위해서다.** 예를 들어 카운트를 3으로 돌려주면 최소표본(30) 미달로 판정이 바뀌어,
    화면 확인용 실행이 실제와 다른 경로를 타게 된다.
    """
    unit = _hash_unit(sql, column)
    lowered = sql.lower()
    name = column.lower()

    if name.endswith(("_date", "_at")):
        base = dt.date(2024, 1, 1)
        return base + dt.timedelta(days=int(unit * 700))
    if name.endswith("_month"):
        month = 1 + int(unit * 12)
        return f"2024-{month:02d}"

    if "safe_divide" in lowered and column == "value":
        return round(_scaled(unit, 0.20, 0.50), 6)  # 비율 (0~1)
    if "count(" in lowered:
        return int(_scaled(unit, 380, 520))  # 고객 수 규모. 최소표본 30을 넘긴다
    if "avg(" in lowered:
        return round(_scaled(unit, 18.0, 28.0), 5)  # 평균 사용량(GB) 규모
    if "sum(" in lowered:
        if any(token in lowered for token in ("billing_amount", "cost", "revenue", "mrr")):
            return int(_scaled(unit, 20_000_000, 30_000_000))  # 금액 규모
        return round(_scaled(unit, 100.0, 5000.0), 2)
    return round(_scaled(unit, 1.0, 1000.0), 4)


# ── 가짜 클라이언트 ───────────────────────────────────────────────────
class FakeRow:
    """속성 접근과 인덱스 접근을 모두 받는 결과 행."""

    def __init__(self, values: Dict[str, Any]) -> None:
        self._values = dict(values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __repr__(self) -> str:
        return f"FakeRow({self._values})"


class FakeJob:
    def __init__(self, rows: Sequence[FakeRow]) -> None:
        self._rows = list(rows)

    def result(self) -> List[FakeRow]:
        return self._rows


class FakeTable:
    def __init__(self, num_rows: int) -> None:
        self.num_rows = num_rows


_COUNT_CHECK = re.compile(r"SELECT\s+COUNT\(\*\)\s+AS\s+n\s+FROM\s+`([^`]+)`", re.IGNORECASE)


class FakeBigQueryClient:
    """앱이 실제로 쓰는 다섯 가지만 흉내 내는 클라이언트. 그 이상은 없다."""

    def __init__(self, project: str = FAKE_PROJECT) -> None:
        self.project = project
        self._loaded_rows: Dict[str, int] = {}
        self.queries: List[str] = []

    def load_table_from_dataframe(self, dataframe, table_id, job_config=None) -> FakeJob:
        # 적재 행수를 기억해 둔다. load_staging()이 get_table().num_rows와 대조해
        # 다르면 계산을 멈추므로, 여기서 틀리면 앱이 정상 경로를 타지 못한다.
        self._loaded_rows[str(table_id)] = int(len(dataframe))
        return FakeJob([])

    def get_table(self, table_id) -> FakeTable:
        return FakeTable(self._loaded_rows.get(str(table_id), 0))

    def query(self, sql: str, job_config=None) -> FakeJob:
        self.queries.append(sql)

        # 적재 검증용 COUNT(*)는 방금 올린 행수를 그대로 돌려줘야 한다.
        match = _COUNT_CHECK.search(sql)
        if match:
            return FakeJob([FakeRow({"n": self._loaded_rows.get(match.group(1), 0)})])

        # 파라미터까지 키에 넣어야 달마다 값이 달라진다(추이 그래프가 평평해지지 않게).
        key = f"{sql}##{params_key(job_config)}"
        columns = result_columns(sql)
        values = {column: fake_value(key, column) for column in columns}

        # 비율형은 분자/분모/값이 서로 맞아야 재조합 검사가 의미를 갖는다.
        if {"numerator", "denominator", "value"} <= set(values):
            denominator = max(int(values["denominator"]), 1)
            numerator = min(int(values["numerator"]), denominator)
            values["denominator"] = denominator
            values["numerator"] = numerator
            values["value"] = numerator / denominator

        return FakeJob([FakeRow(values)])


# ── 스위치 ────────────────────────────────────────────────────────────
def is_enabled() -> bool:
    """데모 모드가 켜져 있는지. **명시적으로 켠 경우에만** True.

    자격증명을 못 찾았다고 여기로 넘어오지 않는다. 그렇게 만들면 로컬에서 토큰이
    만료됐을 때 사용자가 눈치채지 못한 채 가짜 숫자를 보게 된다.
    """
    if os.environ.get(ENV_FLAG, "").strip().lower() in TRUTHY:
        return True

    try:
        import streamlit as st
    except ModuleNotFoundError:
        return False  # CLI 실행 — secrets라는 개념이 없다

    # secrets.toml이 없으면 streamlit 버전마다 예외가 다르다. 전부 "안 켜짐"으로 본다.
    try:
        return bool(st.secrets[SECRETS_FLAG])
    except Exception:
        return False


def make_client() -> "FakeBigQueryClient":
    """데모용 클라이언트. `calculate.make_client()`가 데모 모드일 때 이것을 돌려준다."""
    return FakeBigQueryClient()
