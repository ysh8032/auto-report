"""숫자를 문장으로 바꾸는 규칙.

리포트·이메일이 같은 값을 같은 말로 적게 하려고 문장 규칙을 한 곳에 모은다.
`report.py`가 표를 만든다면 이 모듈은 **문장**을 만든다.

**여기서 만드는 문장은 사실과 변동만 서술한다** (CLAUDE.md 1절).
  - 왜 그렇게 됐는지 쓰지 않는다 — 인과는 사람이 5장에 쓴다.
  - 무엇을 하라고 쓰지 않는다 — 제안은 사람이 6장에 쓴다.
  - 좋다·나쁘다를 쓰지 않는다 — "증가/감소"는 방향이지 평가가 아니다.

숫자 서식은 `common.format_metric`에 위임한다. 서식을 여기 새로 구현하면 화면의
표와 리포트 문장이 같은 값을 다르게 적게 된다 (CLAUDE.md 7절).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import pandas as pd

import common
from pipeline import profile

#: 상태별 문장 꼬리. status 문자열은 calculate.py가 정한 값을 그대로 받는다.
STATUS_OK = "OK"
STATUS_EXTENDED = "구간확장"
STATUS_LOW_SAMPLE = "표본부족"

#: 임계값이 아직 확정되지 않았다는 표시. 정의서에 근거가 없거나 검토 중일 때 쓴다.
PROVISIONAL = "잠정"


# ── 조사 ──────────────────────────────────────────────────────────────
def _has_final_consonant(word: str) -> Optional[bool]:
    """마지막 글자에 받침이 있는가. 한글이 아니면 None."""
    if not word:
        return None
    last = word.strip()[-1]
    if "가" <= last <= "힣":
        return (ord(last) - 0xAC00) % 28 != 0
    if last.isdigit():
        # 숫자는 읽는 법으로 판단한다. 0(영)·1(일)·3(삼)·6(육)·7(칠)·8(팔)에 받침이 있다.
        return last in "013678"
    return None


def topic_particle(word: str) -> str:
    """'은/는'을 고른다.

    조사를 틀리면 자동 생성 티가 나고, 읽는 사람이 문장 전체의 정확성을 의심하게 된다.
    판별할 수 없는 경우(영문 등)에는 '는'을 쓴다 — 외래어 뒤에 더 자연스럽다.
    """
    final = _has_final_consonant(word)
    if final is None:
        return "는"
    return "은" if final else "는"


#: 문구 끝에 붙는 닫는 기호. 조사는 소리 나는 글자로 고르므로 이것들은 건너뛴다.
_TRAILING_MARKS = ")]}»’”'\"）】"


def subject_particle(word: str) -> str:
    """'이/가'를 고른다.

    괄호로 끝나는 문구는 괄호 안의 마지막 글자로 판단한다 — "개선 제안)이"처럼
    읽을 때 소리 나는 글자가 기준이다. 닫는 괄호로 판정하면 늘 '가'가 되어 틀린다.
    판별할 수 없는 경우(영문 등)에는 '가'를 쓴다.
    """
    text = str(word or "").strip()
    while text and text[-1] in _TRAILING_MARKS:
        text = text[:-1]
    final = _has_final_consonant(text)
    if final is None:
        return "가"
    return "이" if final else "가"


def instrumental_particle(word: str) -> str:
    """'으로/로'를 고른다.

    "39.0%으로"처럼 틀리면 문장이 기계가 쓴 티가 난다. 받침이 없거나 ㄹ받침이면 '로'다
    — 퍼센트(트)·GB(비)는 받침이 없고, 원(ㄴ)·명(ㅇ)·건(ㄴ)은 받침이 있다.
    """
    text = str(word or "").strip()
    if not text:
        return "로"
    last = text[-1]
    if "가" <= last <= "힣":
        code = (ord(last) - 0xAC00) % 28
        return "로" if code in (0, 8) else "으로"  # 8 = ㄹ
    if last == "%":
        return "로"  # 퍼센트
    final = _has_final_consonant(text)
    return "로" if final in (False, None) else "으로"


# ── 함수 1: 값 ────────────────────────────────────────────────────────
def unit_of(
    유형: Any,
    지표명: Any,
    metrics_catalog: Optional[dict] = None,
    metric_id: Optional[str] = None,
) -> str:
    """지표의 단위를 정한다.

    **카탈로그를 줄 수 있으면 그쪽이 정답이다.** `profile.value_kind()`는 정의서의
    계산 명세를 따라가므로 파생형도 바르게 가른다 — `arpu`는 분자가 매출이라 금액,
    `monthly_churn_rate`는 분자·분모가 둘 다 고객 수라 비율이다. 이름과 유형만 보면
    둘 다 "파생형"이라 구분할 수 없다.

    카탈로그가 없을 때만 유형·이름으로 어림한다. 이때 **비율 표시를 먼저 본다** —
    "3개월 데이터 사용량 감소율"은 이름에 '데이터 사용량'이 들어 있지만 GB가 아니라
    비율이다. 이름 규칙의 순서를 잘못 두면 이런 지표가 "0.0 GB"로 나온다.
    """
    if metrics_catalog and metric_id:
        resolved = profile.value_kind(str(metric_id), metrics_catalog)
        if resolved:
            return resolved

    kind = str(유형 or "")
    name = str(지표명 or "")

    if kind.startswith("비율형") or name.endswith(("율", "률", "비율")):
        return "비율"
    if "데이터 사용량" in name:
        return "GB"
    if kind.startswith("카운트형"):
        return "건" if "건" in name else "명"
    if kind.startswith("금액형"):
        return "금액"
    return "수"


def fmt_value(
    value: Any,
    유형: Any = None,
    지표명: Any = None,
    metrics_catalog: Optional[dict] = None,
    metric_id: Optional[str] = None,
) -> str:
    """지표 값 하나를 문장에 넣을 수 있는 문자열로 만든다.

    **값이 없으면 "값 없음"이라고 적는다.** 0으로 바꾸거나 빈칸으로 두지 않는다 —
    "값이 0"과 "값이 없다"는 다른 사실이다 (CLAUDE.md 9절).
    """
    if value is None:
        return "값 없음"
    try:
        if pd.isna(value):
            return "값 없음"
    except (TypeError, ValueError):
        return "값 없음"
    return common.format_metric(
        value, unit_of(유형, 지표명, metrics_catalog, metric_id)
    )


# ── 함수 2: 변화 ──────────────────────────────────────────────────────
def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def fmt_change(
    row: Any,
    metrics_catalog: Optional[dict] = None,
    metric_id: Optional[str] = None,
) -> str:
    """전월 대비 변화를 한 덩어리 문자열로 만든다.

    **절대 변화와 변화율을 함께 적는다.** 변화율만 적으면 작은 수의 큰 변화율이
    과장돼 보이고(1건→2건이 +100%), 절대 변화만 적으면 규모를 가늠할 수 없다.

    **비율 지표에는 퍼센트포인트를 앞세운다.** 36.6%→39.0%을 "+6.56%"로만 적으면
    2.4%p 움직인 것이 6.56% 움직인 것으로 읽힌다.

    **전월이 없으면 0으로 채우지 않는다.** 비교할 수 없다는 사실을 그대로 적는다.
    """
    if row is None:
        return "전월 자료 없음 — 비교 불가"
    get = row.get if hasattr(row, "get") else (lambda key, default=None: default)

    absolute = _number(get("절대변화"))
    rate = _number(get("상대변화율"))
    points = _number(get("퍼센트포인트변화"))
    previous = _number(get("전월"))

    if previous is None or (absolute is None and points is None):
        return "전월 자료 없음 — 비교 불가"

    if absolute is not None and absolute == 0:
        return "변화 없음 (0.00%)"

    rate_text = f" ({rate:+,.2f}%)" if rate is not None else ""
    if points is not None:
        return f"{points:+,.1f}%p{rate_text}"

    unit = unit_of(
        get("유형"), get("지표명"), metrics_catalog, metric_id or get("metric_id")
    )
    magnitude = common.format_metric(abs(absolute), unit)
    sign = "+" if absolute > 0 else "-"
    return f"{sign}{magnitude}{rate_text}"


# ── 함수 3: 값 서술 ───────────────────────────────────────────────────
def describe_metric(row: Any, status: Optional[str] = None) -> str:
    """지표 하나를 근거 강도에 맞는 한 문장으로 적는다.

    **상태마다 문장을 다르게 쓰는 이유**: 같은 형식으로 적으면 표본이 모자란 값과
    충분한 값이 같은 무게로 읽힌다. 표본부족은 "값은 이렇지만 근거로 쓰지 않는다"까지
    문장 안에 넣어야, 표를 건너뛰고 문장만 읽는 사람에게도 한계가 전달된다.

    구간확장은 사용자가 이번 실행에 한해 승인한 것이므로 "정의서 유효구간 밖"이라는
    사실을 함께 적는다 — 정의서가 바뀐 것이 아니다.
    """
    get = row.get if hasattr(row, "get") else (lambda key, default=None: default)
    name = str(get("지표명") or get("metric_id") or "지표")
    state = str(status if status is not None else (get("status") or STATUS_OK))
    value = fmt_value(get("value") if get("value") is not None else get("당월"),
                      get("유형"), name)
    particle = topic_particle(name)

    if state == STATUS_LOW_SAMPLE:
        sample = _number(get("sample_size"))
        sample_text = f"{sample:,.0f}" if sample is not None else "미상"
        sentence = (
            f"{name}{particle} {value}{instrumental_particle(value)} 나타나나 "
            f"표본 {sample_text}건으로 기준 미달이어서 근거로 쓰지 않는다."
        )
    elif state == STATUS_EXTENDED:
        sentence = f"{name}{particle} {value}이다(정의서 유효구간 밖, 확장 승인)."
    elif state == STATUS_OK:
        sentence = f"{name}{particle} {value}이다."
    elif value == "값 없음":
        # 값이 없을 때 "값 없음이다"는 어색하다. 없다는 사실을 그대로 적는다.
        sentence = f"{name}{particle} 값이 없다({state})."
    else:
        # 계산오류 등은 값을 단정하지 않고 상태를 그대로 적는다.
        sentence = f"{name}{particle} {value}이다({state})."

    if str(get("임계값_상태") or "") == PROVISIONAL:
        sentence = sentence.rstrip(".") + " (임계값 잠정)."
    return sentence


# ── 함수 4: 변화 서술 ─────────────────────────────────────────────────
def describe_change(
    row: Any,
    threshold: Any,
    metrics_catalog: Optional[dict] = None,
) -> str:
    """전월 대비 변화를 한 문장으로 적는다.

    **방향에 가치판단을 붙이지 않는다.** "매출이 늘어 양호하다"는 판단이고,
    "매출은 전월 대비 +1.15%다"는 사실이다. 늘어난 것이 좋은지는 지표마다 다르고,
    그 판단은 5·6장에서 사람이 한다.

    **임계값 초과도 "확인이 필요한 상태"가 아니라 "초과했다"로만 적는다.** 무엇을
    해야 하는지는 제안이므로 여기서 쓰지 않는다.
    """
    get = row.get if hasattr(row, "get") else (lambda key, default=None: default)
    name = str(get("지표명") or get("metric_id") or "지표")
    particle = topic_particle(name)
    change = fmt_change(row, metrics_catalog, str(get("metric_id") or ""))

    if change == "전월 자료 없음 — 비교 불가":
        return f"{name}{particle} 전월 자료가 없어 비교하지 못했다."

    rate = _number(get("상대변화율"))
    limit = _number(threshold)
    if rate is None or limit is None:
        return f"{name}{particle} 전월 대비 {change}다."
    if abs(rate) >= limit:
        return f"{name}{particle} 전월 대비 {change}로 임계값 {limit:g}%를 초과했다."
    return f"{name}{particle} 전월 대비 {change}로 임계값 이내다."


# ── 함수 5: 금지 표현 검사 ────────────────────────────────────────────
#: 분류별 금지 표현. 자동 생성 문장이 판단으로 넘어가는 것을 잡는다.
FORBIDDEN = {
    "인과": [r"때문", r"탓", r"원인은", r"따라서[^.\n]{0,40}이다"],
    "제안": [r"해야", r"필요하다", r"권장", r"제안"],
    "가치판단": [
        r"개선",
        r"악화",
        r"우려",
        r"심각",
        r"양호",
        r"positive",
        r"negative",
    ],
}


def check_forbidden(text: str) -> List[Dict[str, Any]]:
    """금지 표현을 찾아 목록으로 돌려준다. 없으면 빈 리스트.

    **자동 생성 문장이 슬그머니 판단으로 넘어가는 것을 잡는 장치다.** 사람이 쓸 때는
    "매출이 개선됐다"가 자연스럽지만, 기계가 그렇게 쓰면 근거 없이 평가한 것이 된다.

    걸린 표현이 **정말 위반인지는 사람이 판단한다.** 장 제목("6. 개선 제안")이나
    "이 장은 사람이 작성합니다" 같은 안내문에도 같은 단어가 들어가므로, 이 함수는
    찾아서 보여줄 뿐 자동으로 고치지 않는다. 그래서 줄 번호와 문맥을 함께 돌려준다.
    """
    findings: List[Dict[str, Any]] = []
    lines = str(text or "").splitlines()
    for number, line in enumerate(lines, start=1):
        for category, patterns in FORBIDDEN.items():
            for pattern in patterns:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    findings.append(
                        {
                            "분류": category,
                            "표현": match.group(0),
                            "줄": number,
                            "문맥": line.strip()[:120],
                            "제목줄": line.lstrip().startswith("#"),
                            "인용줄": line.lstrip().startswith(">"),
                        }
                    )
    return findings


def format_findings(findings: List[Dict[str, Any]]) -> str:
    """검사 결과를 사람이 읽을 경고 문구로 만든다."""
    if not findings:
        return "금지 표현이 발견되지 않았습니다."
    lines = [f"금지 표현 {len(findings)}건이 발견되었습니다:"]
    for item in findings:
        mark = " [제목]" if item["제목줄"] else (" [인용]" if item["인용줄"] else "")
        lines.append(f"  {item['줄']:>4}줄 [{item['분류']}] {item['표현']}{mark} · {item['문맥']}")
    return "\n".join(lines)
