"""같은 파일을 넣으면 항상 같은 결과가 나오는지 검증한다 (CLAUDE.md 5-5).

**왜 지금 확인하는가**: 토요일에 본인 데이터로 돌릴 때 결과가 흔들리면 무엇이 맞는
값인지 알 수 없다. 8주차부터는 무인 실행이라 아무도 지켜보지 않으므로, 재현되지
않는 계산은 발견되지 않은 채 리포트로 나간다.

    python -m pytest tests/ -v
    python tests/test_reproducible.py     (pytest 없이도 돈다)

[테스트 1] 계산 재현성 — 같은 파일로 계산을 두 번 돌려 value 컬럼이 같은지
[테스트 2] 리포트 재현성 — 같은 run_context로 리포트를 두 번 만들어 생성일시
           줄만 빼고 텍스트가 같은지 (다르면 difflib로 어느 줄인지 보여준다)
[테스트 3] 현재 시각 의존성 — pipeline/의 계산 로직에 datetime.now() 등이
           없는지 정적으로 훑는다 (기록용 파일은 허용)
[테스트 4] 정렬 안정성 — 계산 결과의 행 순서가 두 번 실행에서 같은지

이 파일이 하는 계산은 **실제 BigQuery**를 쓴다. `dev_tools/fast_test_server.py`는
Claude 코드 검증 전용이라 학생이 돌리는 이 테스트에는 쓰지 않는다.
"""

from __future__ import annotations

import difflib
import functools
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pandas as pd

import config
from pipeline import calculate, profile, report

try:
    import pytest
except ImportError:  # pytest 없이 `python tests/test_reproducible.py`로 돌 때
    pytest = None


class _Skipped(Exception):
    """pytest 없이 실행할 때 '건너뜀'을 표현한다. pytest가 있으면 pytest.skip을 쓴다."""


def _skip(reason: str) -> None:
    if pytest is not None:
        pytest.skip(reason)
    raise _Skipped(reason)


# ── 준비물 ────────────────────────────────────────────────────────────
#: calculate()가 만드는 산출물 이름. run 폴더에서 원본 CSV를 찾을 때 이것들은 뺀다.
_OUTPUT_CSV_NAMES = {"metrics.csv", "comparison.csv"}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _find_sample_csv() -> Optional[Path]:
    """검증에 쓸 CSV 하나를 찾는다.

    **환경변수를 최우선으로 본다** (`AUTO_REPORT_TEST_CSV`). 없으면 `outputs/run_*/`에
    이미 복사돼 있는 원본 CSV 중 가장 최근 것을 쓴다 — Day4까지 왔다면 앱이나
    run_pipeline.py를 이미 여러 번 돌렸을 것이므로, 새 샘플 파일 없이도 돈다.
    """
    override = os.environ.get("AUTO_REPORT_TEST_CSV")
    if override:
        path = Path(override)
        return path if path.exists() else None

    if not config.OUTPUTS_DIR.exists():
        return None
    for run_dir in sorted(config.OUTPUTS_DIR.glob("run_*"), reverse=True):
        for csv_path in sorted(run_dir.glob("*.csv")):
            if csv_path.name not in _OUTPUT_CSV_NAMES:
                return csv_path
    return None


class _Inputs:
    """두 번 계산에 필요한 재료. 한 번만 준비해 여러 테스트가 나눠 쓴다."""

    __slots__ = ("df", "catalog", "schema", "table_name", "period", "targets", "client")

    def __init__(self, df, catalog, schema, table_name, period, targets, client):
        self.df = df
        self.catalog = catalog
        self.schema = schema
        self.table_name = table_name
        self.period = period
        self.targets = targets
        self.client = client


@functools.lru_cache(maxsize=1)
def _pipeline_inputs() -> _Inputs:
    """샘플 CSV로 판정까지 마친 재료 한 벌.

    `lru_cache`로 한 번만 만든다 — 재현성 검증의 핵심은 **계산 자체**를 두 번
    돌리는 것이지, 판정을 두 번 돌리는 게 아니다. 판정까지 매번 새로 하면
    테스트 3개가 합쳐 BigQuery를 불필요하게 여러 번 두드린다.
    """
    csv_path = _find_sample_csv()
    if csv_path is None:
        _skip(
            "참고할 CSV를 찾지 못했습니다. outputs/run_*/에 파일이 있어야 하거나, "
            "AUTO_REPORT_TEST_CSV 환경변수로 CSV 경로를 지정하세요."
        )

    catalog = _load_json(config.METRICS_CATALOG_PATH)
    schema = _load_json(config.SCHEMA_CATALOG_PATH)
    if not catalog or not schema:
        _skip("카탈로그가 없습니다. catalog/export_catalog.py를 먼저 실행하세요.")

    df = pd.read_csv(csv_path)
    judgement = profile.judge_table(df, schema)
    if not judgement["판정가능"]:
        _skip(f"'{csv_path.name}'의 스키마를 판정하지 못했습니다: {judgement.get('이유')}")

    table_name = judgement["테이블명"]
    data_profile = profile.profile_data(df, schema.get(table_name))
    period = data_profile["기간"]["최대"] or data_profile["기간"]["최소"]
    if not period:
        _skip(f"'{csv_path.name}'에서 기간을 판정하지 못했습니다.")

    judged = profile.judge_metrics(
        table_name, period, catalog, missing_columns=judgement["누락컬럼"]
    )
    targets = [
        row["metric_id"]
        for row in judged
        if row["상태"] in (profile.CALCULABLE, profile.NEEDS_RANGE)
    ]
    if not targets:
        _skip(f"'{csv_path.name}'로 계산할 수 있는 지표가 없습니다.")

    try:
        client = calculate.make_client()
    except Exception as exc:  # 인증 미설정 등 — 재현성 검증 이전의 문제다
        _skip(f"BigQuery에 연결하지 못했습니다: {exc}")

    return _Inputs(df, catalog, schema, table_name, period, targets, client)


def _calculate_once(inputs: _Inputs) -> pd.DataFrame:
    """지표를 한 번 계산한다. `calculate()`의 staging_map은 **짧은 이름**을 받는다.

    `load_staging()`은 "project.dataset.staging_x" 전체 경로를 돌려주는데, 그걸
    그대로 staging_map에 넣으면 dataset이 두 번 붙어 BadRequest가 난다
    (run_pipeline.py에서 실제로 겪은 버그다 — 여기서 같은 실수를 반복하지 않는다).
    """
    staging_name = f"{config.STAGING_PREFIX}{inputs.table_name}"
    calculate.load_staging(inputs.df, inputs.table_name, inputs.client)
    return calculate.calculate(
        inputs.targets,
        inputs.period,
        {inputs.table_name: staging_name},
        inputs.client,
        override=True,  # 재현성 검증이 목적이라, 유효구간 확장 승인 화면은 거치지 않는다
        metrics_catalog=inputs.catalog,
    )


def _sample_context() -> Dict[str, Any]:
    """리포트 재현성 테스트에 쓸 run_context. 계산은 한 번만 한다."""
    inputs = _pipeline_inputs()
    metrics = _calculate_once(inputs)
    insights = _load_json(config.INSIGHTS_CATALOG_PATH)
    return {
        "파일명": "재현성_테스트.csv",
        "테이블명": inputs.table_name,
        "기간": {"최소": inputs.period, "최대": inputs.period},
        "행수": int(len(inputs.df)),
        "metrics": metrics,
        "comparison": None,
        "validation": None,
        "metrics_catalog": inputs.catalog,
        "schema_catalog": inputs.schema,
        "insights_catalog": insights,
        "run_log": {},
        "source_freshness": None,
        # 일부러 "생성일시"를 안 준다. report.document_header()가 없으면
        # datetime.now()로 채우는데, 그게 바로 두 번째 테스트에서 "생성일시 줄만
        # 다를 수 있다"는 조건이 실제로 생기는 지점이다.
        "run_dir": None,
    }


# ── 테스트 1: 계산 재현성 ─────────────────────────────────────────────
def test_calc_reproducible() -> None:
    inputs = _pipeline_inputs()
    first = _calculate_once(inputs)
    second = _calculate_once(inputs)

    # metric_id로 맞춰 놓고 비교한다 — 행 "순서"가 같은지는 테스트 4의 몫이다.
    left = first.sort_values("metric_id").reset_index(drop=True)
    right = second.sort_values("metric_id").reset_index(drop=True)

    assert list(left["metric_id"]) == list(right["metric_id"]), (
        "두 번의 계산이 서로 다른 지표 집합을 돌려줬습니다 — "
        f"1회차: {sorted(left['metric_id'])}, 2회차: {sorted(right['metric_id'])}"
    )

    mismatches = []
    for metric_id, value1, value2 in zip(left["metric_id"], left["value"], right["value"]):
        both_nan = pd.isna(value1) and pd.isna(value2)
        if not both_nan and value1 != value2:
            mismatches.append(f"  {metric_id}: 1회차={value1!r} 2회차={value2!r}")

    assert not mismatches, "같은 파일인데 값이 달라진 지표가 있습니다:\n" + "\n".join(
        mismatches
    )


# ── 테스트 2: 리포트 재현성 ───────────────────────────────────────────
def _strip_generated_lines(markdown: str) -> List[str]:
    """비교에서 뺄 줄을 걸러낸다. "생성일시"가 들어간 줄은 datetime.now()로 채워질 수 있다."""
    return [line for line in markdown.splitlines() if "생성일시" not in line]


def test_report_reproducible() -> None:
    context = _sample_context()
    first = report.build_report(context)
    second = report.build_report(context)

    left = _strip_generated_lines(first)
    right = _strip_generated_lines(second)

    if left == right:
        return

    diff = "\n".join(
        difflib.unified_diff(left, right, fromfile="1회차", tofile="2회차", lineterm="")
    )
    raise AssertionError(
        "같은 run_context인데 리포트 내용이 달라졌습니다 (생성일시 줄 제외):\n" + diff
    )


# ── 테스트 3: 현재 시각 의존성 검사 ───────────────────────────────────
#: 이 파일들은 "기록"이 목적이라 현재 시각을 써도 된다 — 생성일시·작성일·run_log
#: 타임스탬프가 여기 해당한다. 나머지 pipeline/ 파일에서 발견되면 계산 로직에
#: 현재 시각이 섞였다는 뜻이라 실패시킨다 (CLAUDE.md 5-5).
_TIME_ALLOWED_FILES = {"report.py", "email_draft.py", "manual_sections.py", "runlog.py"}
_NOW_PATTERN = re.compile(r"(?:datetime\.now|date\.today|time\.time)\s*\(")


def _scan_now_usage() -> List[Tuple[str, int, str, bool]]:
    """`(파일명, 줄번호, 줄내용, 허용여부)` 목록. pipeline/ 전체를 훑는다."""
    findings: List[Tuple[str, int, str, bool]] = []
    pipeline_dir = BASE_DIR / "pipeline"
    for path in sorted(pipeline_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if _NOW_PATTERN.search(line):
                findings.append((path.name, number, line.strip(), path.name in _TIME_ALLOWED_FILES))
    return findings


def test_no_now_in_calc_logic() -> None:
    findings = _scan_now_usage()

    if findings:
        print("\n  현재 시각 사용 지점:")
        for filename, number, line, allowed in findings:
            mark = "허용(기록용)" if allowed else "금지(계산 로직)"
            print(f"    [{mark}] pipeline/{filename}:{number}  {line}")

    forbidden = [item for item in findings if not item[3]]
    assert not forbidden, "계산 로직에 현재 시각 의존이 있습니다:\n" + "\n".join(
        f"  pipeline/{filename}:{number}  {line}" for filename, number, line, _ in forbidden
    )


# ── 테스트 4: 정렬 안정성 ─────────────────────────────────────────────
def test_sort_order_stable() -> None:
    inputs = _pipeline_inputs()
    first = _calculate_once(inputs)
    second = _calculate_once(inputs)

    left_order = list(first["metric_id"])
    right_order = list(second["metric_id"])

    assert left_order == right_order, (
        "계산 결과의 행 순서가 두 번의 실행에서 달랐습니다 — 정렬 기준이 없다는 뜻입니다.\n"
        f"  1회차: {left_order}\n"
        f"  2회차: {right_order}"
    )


# ── pytest 없이 직접 실행 ─────────────────────────────────────────────
def _run_standalone() -> int:
    tests = [
        ("계산 재현성", test_calc_reproducible),
        ("리포트 재현성", test_report_reproducible),
        ("현재 시각 의존성", test_no_now_in_calc_logic),
        ("정렬 안정성", test_sort_order_stable),
    ]
    failed = 0
    for name, func in tests:
        print(f"[{name}] ", end="", flush=True)
        try:
            func()
        except _Skipped as exc:
            print(f"건너뜀 — {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"실패\n{exc}\n")
        except Exception as exc:  # 예상 못 한 오류도 실패로 취급하되 무엇인지 남긴다
            failed += 1
            print(f"오류 — {type(exc).__name__}: {exc}\n")
        else:
            print("통과")

    print()
    if failed:
        print(f"{failed}개 실패")
    else:
        print("전부 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
