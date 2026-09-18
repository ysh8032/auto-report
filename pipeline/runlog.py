"""실행 기록(`outputs/run_*/run_log.json`)의 구조를 한 곳에서 정한다.

**왜 상세해야 하는가**: 3개월 뒤 "이 숫자 어떻게 나왔나요"라는 질문을 받으면 이 파일
하나로 답해야 한다. 어느 파일, 어느 정의, 어느 승인으로 나온 값인지가 다 들어 있어야
한다. 그래서 화면에서 보여준 것은 전부 여기에도 남긴다.

**왜 8단계 키로 나누는가**: 기록이 늘어나면 최상위 키가 스무 개씩 생기고, 어느 단계에서
남긴 것인지 알 수 없게 된다. 단계 번호를 키로 쓰면 파일을 열자마자 흐름이 보이고,
화면 렌더링도 순서를 따로 정할 필요가 없다.

    runlog.write(run_dir, runlog.CALC, {...})   한 단계 기록
    runlog.load(run_dir)                        전체 읽기
    runlog.screen_rows(log)                     화면 표로 펼치기

**시각과 경과 초**: 각 단계를 기록할 때 `시각`을 찍고, 앞 단계와의 차이를 `경과초`로
남긴다. 게이트가 낀 구간에는 사람이 화면을 보는 시간이 포함되므로 "처리 시간"이 아니라
"앞 단계 이후 경과"다. 이름을 그렇게 붙여야 나중에 성능 지표로 오해하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 기록 구역 키. 이름에 단계 번호를 넣어 파일을 열자마자 흐름이 보이게 한다.
RUN = "실행"
CATALOG = "0. 카탈로그"
INTAKE = "1. 투입"
JUDGE = "2. 판정"
GATE1 = "게이트1 — 지표 확정"
CALC = "2. 계산"
VALIDATE = "3. 검증"
REVIEW = "4·5. 확인"
REPORT = "6. 리포트"
EMAIL = "7. 이메일"
APPROVE = "8. 확정"

#: 저장·렌더링 순서. 실제로 기록되는 순서는 실행 순서지만, 파일은 이 순서로 정렬한다.
SECTIONS: Tuple[Tuple[str, str], ...] = (
    (RUN, "실행 정보"),
    (CATALOG, "0. 카탈로그"),
    (INTAKE, "1. 데이터 파일 투입"),
    (JUDGE, "2. 스키마 점검 · 지표 판정"),
    (GATE1, "게이트 1 — 지표 확정"),
    (CALC, "2. 지표 계산"),
    (VALIDATE, "3. 검증"),
    (REVIEW, "4·5. 내용 · 검증 결과 확인"),
    (REPORT, "6. 리포트 생성"),
    (EMAIL, "7. 이메일 초안"),
    (APPROVE, "8. 발송 확정"),
)

STAMP = "시각"
ELAPSED = "앞_단계_이후_초"

FILENAME = "run_log.json"

#: 실행 환경에 적을 패키지. 계산 결과가 달라질 수 있는 것만 고른다.
#: 전부 적으면 300줄이 되고, 정작 봐야 할 버전이 묻힌다.
PACKAGES: Tuple[str, ...] = (
    "streamlit",
    "pandas",
    "plotly",
    "google-cloud-bigquery",
    "pyarrow",
    "fpdf2",
)


def path_of(run_dir: Any) -> Path:
    return Path(run_dir) / FILENAME


def environment() -> Dict[str, Any]:
    """Python·패키지 버전. 같은 파일로 다른 결과가 나올 때 먼저 보는 곳이다."""
    from importlib import metadata

    versions: Dict[str, str] = {}
    for name in PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "설치되지 않음"
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "패키지": versions,
    }


def load(run_dir: Any) -> Dict[str, Any]:
    """읽는다. 없거나 깨졌으면 빈 dict — 기록을 못 읽는 것이 실행을 막을 이유는 아니다."""
    try:
        return json.loads(path_of(run_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def section(log: Dict[str, Any], key: str) -> Dict[str, Any]:
    """`log[key]`를 dict로 돌려준다. 없거나 dict가 아니면 빈 dict다.

    **왜 필요한가**: `RUN = "실행"`은 이 리팩터 이전의 옛 기록에서 최상위 문자열
    필드 이름과 우연히 같다 — 옛 기록에서는 `log["실행"]`이 dict가 아니라
    run 이름을 담은 문자열이었다. `.get(key) or {}`만 쓰면 그 문자열이 그대로
    나와서, 뒤이어 `.get(...)`을 부르면 `'str' object has no attribute 'get'`으로
    죽는다. 옛 기록도 목록에서 걷어내지 않으므로(사이드바 "기존 실행 불러오기"가
    `outputs/`의 모든 run_* 폴더를 보여준다) 여기서 한 번 막는다.
    """
    value = log.get(key)
    return value if isinstance(value, dict) else {}


def _ordered(log: Dict[str, Any]) -> Dict[str, Any]:
    """SECTIONS 순서로 정렬한다. 모르는 키는 뒤에 그대로 붙인다 — 버리지 않는다."""
    known = [key for key, _ in SECTIONS]
    out: Dict[str, Any] = {key: log[key] for key in known if key in log}
    for key, value in log.items():
        if key not in out:
            out[key] = value
    return out


def _clean(value: Any) -> Any:
    """`NaN`·`Infinity`를 None으로 바꾼다.

    pandas가 준 값에는 `NaN`이 섞인다. Python의 `json`은 이걸 `NaN`이라는 글자로
    쓰는데 그건 표준 JSON이 아니라, 다른 도구로 이 기록을 읽으면 파싱이 깨진다.
    "값이 없다"는 뜻이므로 null로 적는 것이 맞다.
    """
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else value
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def save(run_dir: Any, log: Dict[str, Any]) -> Path:
    target = path_of(run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_clean(_ordered(log)), ensure_ascii=False, indent=2, default=str)
        + chr(10),
        encoding="utf-8",
    )
    return target


def _stamped(log: Dict[str, Any]) -> List[Tuple[str, dt.datetime]]:
    """시각이 찍힌 구역을 시각 순으로. 경과 초와 표 순서의 기준이다."""
    found: List[Tuple[str, dt.datetime]] = []
    for key, value in log.items():
        if not isinstance(value, dict) or not value.get(STAMP):
            continue
        try:
            found.append((key, dt.datetime.fromisoformat(str(value[STAMP]))))
        except ValueError:
            continue
    return sorted(found, key=lambda pair: pair[1])


def _recompute_elapsed(log: Dict[str, Any]) -> None:
    """모든 구역의 경과 초를 다시 계산한다.

    **쓸 때 한 번만 재면 안 된다.** 리포트를 다시 만들면 그 구역의 시각이 뒤 단계보다
    늦어지고, "앞 단계"가 미래가 되어 음수가 나온다. 매번 전체를 시각 순으로 다시
    줄 세워야 기록이 앞뒤가 맞는다.
    """
    order = _stamped(log)
    previous: Optional[dt.datetime] = None
    for key, moment in order:
        if previous is None:
            log[key].pop(ELAPSED, None)  # 첫 구역은 견줄 앞 단계가 없다
        else:
            log[key][ELAPSED] = round((moment - previous).total_seconds(), 1)
        previous = moment


def write(
    run_dir: Any,
    key: str,
    payload: Dict[str, Any],
    stamp: Optional[str] = None,
) -> Dict[str, Any]:
    """한 구역을 기록하고 전체 로그를 돌려준다.

    **덮어쓴다.** 같은 단계를 다시 실행하면 새 결과가 맞는 기록이다. 이력을 쌓으면
    "어느 것이 이번 결과인지"를 읽는 사람이 판단해야 한다.
    """
    log = load(run_dir)
    body = dict(payload)
    body[STAMP] = stamp or dt.datetime.now().astimezone().isoformat(timespec="seconds")
    log[key] = body
    _recompute_elapsed(log)
    save(run_dir, log)
    return log


def amend(run_dir: Any, key: str, **fields: Any) -> Dict[str, Any]:
    """이미 있는 구역에 값을 덧붙인다. 취소 표시처럼 나중에 알게 된 사실을 남길 때 쓴다."""
    log = load(run_dir)
    if key in log and isinstance(log[key], dict):
        log[key].update(fields)
        save(run_dir, log)
    return log


# ── 구역 조립 ─────────────────────────────────────────────────────────
# 화면(app.py)과 명령줄(run_pipeline.py)이 **같은 기록**을 남겨야 한다. 두 곳에서
# 각자 dict를 만들면 어느 쪽으로 돌렸는지에 따라 기록의 모양이 달라지고, 나중에
# "화면 결과와 CLI 결과가 다르다"의 원인을 여기서부터 찾게 된다.


def catalog_stamp(catalog: Optional[dict]) -> Optional[str]:
    return ((catalog or {}).get("_meta") or {}).get("생성일시")


def catalog_count(catalog: Optional[dict]) -> int:
    meta = (catalog or {}).get("_meta") or {}
    count = meta.get("항목_개수")
    if count is not None:
        return int(count)
    return sum(1 for key in (catalog or {}) if not str(key).startswith("_"))


def run_section(run_name: str, stamp: str, entry: str) -> Dict[str, Any]:
    """실행 정보. `진입`은 화면으로 돌렸는지 명령줄로 돌렸는지다."""
    return {
        "실행": run_name,
        "상태": "확정",
        "확정시각": stamp,
        "진입": entry,
        "실행환경": environment(),
    }


def catalog_section(
    metrics_catalog: Optional[dict],
    schema_catalog: Optional[dict],
    insights_catalog: Optional[dict],
) -> Dict[str, Any]:
    return {
        "생성일시": catalog_stamp(metrics_catalog),
        "지표수": catalog_count(metrics_catalog),
        "테이블수": catalog_count(schema_catalog),
        "인사이트수": catalog_count(insights_catalog),
        "스키마생성일시": catalog_stamp(schema_catalog),
        "인사이트생성일시": catalog_stamp(insights_catalog),
    }


def intake_section(
    info: Dict[str, Any], saved_name: str, upload_stamp: Optional[str]
) -> Dict[str, Any]:
    return {
        "파일명": info["파일명"],
        "저장파일": saved_name,
        "행수": info["행수"],
        "컬럼수": info["컬럼수"],
        "인코딩": info["인코딩"],
        "크기_bytes": info["크기_bytes"],
        "업로드시각": upload_stamp,
    }


def judge_section(
    judgement: Dict[str, Any],
    prof: Dict[str, Any],
    judged: Sequence[Dict[str, Any]],
    partial_agreed: bool = False,
) -> Dict[str, Any]:
    """2단계 판정. 결측·그레인 중복까지 남긴다 — 숫자가 이상할 때 먼저 보는 곳이다."""
    from pipeline import profile

    targets = [
        row for row in judged if row["상태"] in (profile.CALCULABLE, profile.NEEDS_RANGE)
    ]
    partial = [row for row in targets if row.get("부분갱신")]
    blocked = [row for row in judged if row["상태"] == profile.BLOCKED]
    candidates = prof.get("그레인후보") or []

    return {
        "테이블명": judgement["테이블명"],
        "테이블명_추정": judgement["테이블명_추정"],
        "일치율": round(judgement["일치율"], 4),
        "누락컬럼": judgement["누락컬럼"],
        "추가컬럼": judgement["추가컬럼"],
        "기간": {
            "컬럼": prof["기간"]["컬럼"],
            "최소": prof["기간"]["최소"],
            "최대": prof["기간"]["최대"],
        },
        # 결측이 0인 컬럼은 남기지 않는다 (profile.profile_data와 같은 이유).
        "결측있는컬럼수": prof.get("결측있는컬럼수", 0),
        "결측": prof.get("결측") or {},
        "그레인후보": candidates,
        "그레인중복": [
            {"키": item["키"], "출처": item["출처"], "중복행수": item["중복행수"]}
            for item in candidates
            if not item["유일"]
        ],
        "유일한_그레인": [item["키"] for item in candidates if item["유일"]],
        "지표판정": {
            "계산대상": [
                {
                    "metric_id": row["metric_id"],
                    "지표명": row["지표명"],
                    "상태": row["상태"],
                    "부분갱신": bool(row.get("부분갱신")),
                    "원천": row["원천"],
                }
                for row in targets
            ],
            "요약": profile.summarize_metrics(judged),
            "부분갱신": [
                {"metric_id": row["metric_id"], "갱신되지_않는_원천": row["다른원천"]}
                for row in partial
            ],
            "제외지표": {
                "사유": "누락 컬럼" if blocked else None,
                "일부만_계산_동의": bool(blocked) and bool(partial_agreed),
                "목록": [
                    {
                        "metric_id": row["metric_id"],
                        "지표명": row["지표명"],
                        "없는컬럼": row.get("누락컬럼") or [],
                    }
                    for row in blocked
                ],
            },
        },
    }


def gate1_section(
    stamp: str,
    needs_range: Sequence[Dict[str, Any]],
    approved: bool,
    approved_by: str,
) -> Dict[str, Any]:
    """게이트 1. **누가 승인했는지 남긴다** — 화면의 체크박스와 CLI 플래그는 다른 행위다."""
    required = bool(needs_range)
    return {
        "확정시각": stamp,
        "유효구간확장_필요": required,
        "유효구간확장_승인": required and approved,
        "승인주체": approved_by if (required and approved) else None,
        "승인시각": stamp if (required and approved) else None,
        "적용범위": "이번 실행에만 적용. 위키 정의서는 변경되지 않았습니다.",
        "확장_대상": [row["metric_id"] for row in needs_range],
    }


def calc_section(
    frame: Any,
    staging_table: Optional[str],
    comparison: Any = None,
    prev_period: Optional[str] = None,
    comparison_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """2단계 계산. 지표별 값·상태를 남긴다.

    `metrics.csv`에도 있지만, "이 숫자 어떻게 나왔나요"에 파일 하나로 답하려면
    기록 안에서 값이 보여야 한다.
    """
    rows = frame.to_dict("records")
    counts: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("status"))
        counts[key] = counts.get(key, 0) + 1

    return {
        "스테이징테이블": staging_table,
        "결과파일": "metrics.csv",
        "행수": int(len(rows)),
        "상태별": counts,
        "지표별": [
            {
                "metric_id": row.get("metric_id"),
                "지표명": row.get("지표명"),
                "값": row.get("value"),
                "상태": row.get("status"),
                "이유": row.get("이유"),
                "표본수": row.get("sample_size"),
            }
            for row in rows
        ],
        "전월비교": (
            None
            if comparison is None
            else {
                "전월": prev_period,
                "결과파일": "comparison.csv",
                "상태별": comparison_summary or {},
            }
        ),
    }


def validate_section(validation: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "전체판정": validation["전체판정"],
        "차단수": validation["차단수"],
        "경고수": validation["경고수"],
        "결과파일": "validation.json",
        "자동검증하지_않은_것": validation["자동검증하지_않은_것"],
        "한계문장": validation.get("한계문장"),
        "항목별결과": validation.get("항목별결과") or [],
    }


def report_section(
    markdown: str,
    chapters: Sequence[int],
    human_chapters: Sequence[int],
    manual_slots: Sequence[str],
    outcome: Dict[str, Any],
    manual_path: Any,
    findings: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """6단계 리포트. 금지 표현은 본문과 제목·인용을 나눠 센다 — 제목줄은 오탐이 많다."""
    body = [
        item for item in findings if not item.get("제목줄") and not item.get("인용줄")
    ]
    human = set(human_chapters)
    return {
        "파일": "report.md",
        "글자수": len(markdown),
        "장_목록": list(chapters),
        "자동생성_장": [number for number in chapters if number not in human],
        "사람이_쓰는_자리": list(manual_slots),
        "채운_자리": (outcome or {}).get("치환", []),
        "미작성_자리": (outcome or {}).get("남김", []),
        "사람_작성분_파일": str(manual_path),
        "금지표현_검사": {
            "본문_건수": len(body),
            "제목·인용_건수": len(findings) - len(body),
            "본문_항목": list(body),
        },
    }


def email_section(
    draft: Dict[str, Any], pending: Sequence[str], note: str
) -> Dict[str, Any]:
    return {
        "제목": draft["subject"],
        "수신자": list(draft["to"]),
        "발신자": draft["from"],
        "첨부_목록": [
            {"파일명": item["filename"], "크기": item["size"], "존재": item["exists"]}
            for item in draft["attachments"]
        ],
        "미작성_자리": list(pending),
        "파일": ["email.html", "email.txt", "email_meta.json"],
        "발송여부": note,
    }


# ── 화면 렌더링 ───────────────────────────────────────────────────────
#: 표에 펼치지 않고 "N건"으로만 적을 키. 목록을 다 펼치면 표가 수백 줄이 된다.
_COUNT_ONLY = ("항목별결과", "지표별", "계산대상", "그레인후보", "결측", "첨부_목록")


def _plain(value: Any) -> str:
    """표 한 칸에 넣을 문자열. 긴 목록은 개수로 줄인다."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value:,.4g}"
    if isinstance(value, dict):
        return f"{len(value)}개 항목"
    if isinstance(value, (list, tuple)):
        if not value:
            return "없음"
        if all(isinstance(item, (str, int, float)) for item in value):
            text = ", ".join(str(item) for item in value)
            return text if len(text) <= 120 else f"{len(value)}건"
        return f"{len(value)}건"
    return str(value)


def _flatten(body: Any, prefix: str = "") -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    if not isinstance(body, dict):
        return [(prefix or "값", _plain(body))]
    for key, value in body.items():
        label = f"{prefix}{key}" if not prefix else f"{prefix} · {key}"
        if isinstance(value, dict) and key not in _COUNT_ONLY and len(value) <= 8:
            rows.extend(_flatten(value, label))
        else:
            rows.append((label, _plain(value)))
    return rows


def screen_rows(log: Dict[str, Any]) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """`[(구역 제목, [(항목, 값), ...]), ...]`. 기록이 없는 구역은 넣지 않는다.

    화면이 이 함수를 쓰는 이유: 구역 순서와 무엇을 펼칠지를 기록 쪽에서 정해야
    새 구역이 생겨도 화면 코드를 안 고친다.

    옛 형식(단계 키가 없던 시절)의 기록도 버리지 않고 보여준다. 모르는 구역은
    뒤에 붙이고, 값 하나짜리 키는 "기타"로 묶는다 — 그러지 않으면 구역이 스무 개가 된다.
    """
    titles = dict(SECTIONS)
    out: List[Tuple[str, List[Tuple[str, str]]]] = []
    for key, title in SECTIONS:
        if key in log:
            out.append((title, _flatten(log[key])))

    others: List[Tuple[str, str]] = []
    for key, value in log.items():
        if key in titles:
            continue
        if isinstance(value, dict):
            out.append((key, _flatten(value)))
        else:
            others.append((key, _plain(value)))
    if others:
        out.append(("기타 (옛 형식 기록)", others))
    return out


def total_seconds(log: Dict[str, Any]) -> Optional[float]:
    """첫 기록부터 마지막 기록까지. 사람이 게이트에서 머문 시간이 포함된다."""
    order = _stamped(log)
    if len(order) < 2:
        return None
    return round((order[-1][1] - order[0][1]).total_seconds(), 1)


def elapsed_rows(log: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """`[(구역 제목, 시각, 경과)]`. 어디에서 시간이 걸렸는지 한눈에 보이게 한다.

    **시각 순으로 낸다.** 단계 순으로 내면 리포트를 다시 만든 실행에서 시각이
    거꾸로 보인다 — 실제로 일어난 순서가 읽는 사람이 알아야 할 순서다.
    """
    titles = dict(SECTIONS)
    rows: List[Tuple[str, str, str]] = []
    for key, moment in _stamped(log):
        elapsed = log[key].get(ELAPSED)
        rows.append(
            (
                titles.get(key, key),
                moment.strftime("%H:%M:%S"),
                "—" if elapsed is None else f"{elapsed:,.1f}초",
            )
        )
    return rows
