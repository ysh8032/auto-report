"""Claude 코드 검증 전용 — 실제 수업에서는 쓰지 않는다.

===========================================================================
 이 스크립트는 코드를 고친 뒤 화면이 깨지지 않았는지 **빠르게 확인**하려고 만든
 것이다. BigQuery에 실제 쿼리를 보내지 않고, SQL 문자열을 해시해서 만든 **가짜
 숫자**를 돌려준다. 따라서 **여기 뜨는 숫자는 전부 거짓이다.**

 수업·시연·리포트에는 반드시 진짜를 쓴다:

     streamlit run app.py

 이 파일은 CLAUDE.md 8절의 폴더 구조에 없는 dev_tools/에 둔다. 앱 코드가 이
 파일을 import하지 않으며, 앱은 이런 것이 있는지도 모른다.
===========================================================================

사용법
    python dev_tools/fast_test_server.py --port 8502

무엇을 가짜로 만드는가
    `pipeline/calculate.py`의 `make_client()`가 돌려주는 BigQuery 클라이언트 하나만
    바꿔치기한다. 앱이 그 클라이언트에서 실제로 쓰는 것은 아래 다섯 가지뿐이라,
    그만큼만 흉내 내고 그 이상은 구현하지 않는다.

        client.project
        client.load_table_from_dataframe(df, table_id, job_config=...).result()
        client.get_table(table_id).num_rows
        client.query(sql).result()            → 행 목록
        client.query(sql, job_config=...).result()

    행에는 속성 접근(row.value, row.numerator, row.denominator, row.n)과
    인덱스 접근(row[name])이 모두 쓰이므로 둘 다 지원한다.

무엇을 건드리지 않는가
    **판정 로직은 손대지 않는다.** 유효구간·최소표본·파생지표 정합성 같은 판단은
    쿼리 결과가 아니라 카탈로그(catalog/*.json)로 갈리는 부분이라, 실제 코드가
    그대로 돌아야 의미가 있다. 이 파일은 `pipeline/`과 `app.py`를 수정하지 않는다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 콘솔이 cp949여도 배너의 기호(—)가 깨지지 않게 한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FAKE_PROJECT = "fake-project-for-code-check"

BANNER = (
    "가짜 BigQuery로 실행 중입니다 — 화면의 숫자는 전부 거짓입니다. "
    "수업에는 `streamlit run app.py`를 쓰세요."
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


# ── 설치 ──────────────────────────────────────────────────────────────
def install() -> FakeBigQueryClient:
    """`calculate.make_client`만 바꿔치기한다. 판정 로직은 손대지 않는다."""
    from pipeline import calculate

    client = FakeBigQueryClient()
    calculate.make_client = lambda: client  # type: ignore[assignment]
    return client


# ── 실행 ──────────────────────────────────────────────────────────────
def _inside_streamlit() -> bool:
    try:
        from streamlit.runtime import exists

        return bool(exists())
    except Exception:
        return False


def serve() -> None:
    """Streamlit이 이 파일을 스크립트로 실행할 때 — 가짜를 심고 진짜 app.py를 돌린다."""
    import streamlit as st

    install()
    app_path = ROOT / "app.py"
    source = app_path.read_text(encoding="utf-8")
    # import가 아니라 exec으로 돌린다. Streamlit은 재실행 때마다 스크립트를 다시
    # 실행하는데, import는 모듈 캐시에 걸려 두 번째부터 아무것도 그리지 않는다.
    exec(  # noqa: S102 — 검증 전용 도구에서 실제 앱을 그대로 돌리기 위한 의도된 사용
        compile(source, str(app_path), "exec"),
        {"__name__": "__main__", "__file__": str(app_path)},
    )
    # 화면 아래쪽에 남겨 두어, 이 화면을 수업용으로 착각하지 않게 한다.
    st.sidebar.error(BANNER)


def launch(port: int) -> int:
    script = Path(__file__).resolve()
    print("=" * 78)
    print(" Claude 코드 검증 전용 서버 — 화면의 숫자는 전부 가짜입니다.")
    print(" 수업·시연에는 반드시 `streamlit run app.py`를 쓰세요.")
    print(f" http://localhost:{port}")
    print("=" * 78)
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Claude 코드 검증 전용 빠른 테스트 서버 (가짜 BigQuery)"
    )
    parser.add_argument("--port", type=int, default=8502, help="포트 (기본 8502)")
    args = parser.parse_args()
    return launch(args.port)


if __name__ == "__main__":
    if _inside_streamlit():
        serve()
    else:
        raise SystemExit(main())
