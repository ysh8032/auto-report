"""6단계 — 리포트 생성 (마크다운).

위키의 `report/고객서비스_만족도개선_리포트.md`와 같은 8장 구조를 따르되, **자동으로
쓸 수 있는 장만 채운다.** 나머지는 자리를 만들고 사람이 쓸 것이라고 적어 둔다.

    1. Executive Summary   (다음 단계)
    2. 배경·목적            사람이 작성
    3. 데이터·방법론         자동
    4. 현황                자동
    5. 원인 분석            사람이 작성
    6. 개선 제안            사람이 작성
    7. 한계                (다음 단계)
    8. 부록                (다음 단계)

**이 모듈은 사실과 변동만 적는다** (CLAUDE.md 1절).
  - 인과를 단정하지 않는다. "A 때문에 B가 줄었다"를 쓰지 않는다.
  - 제안하지 않는다. "~해야 한다"를 쓰지 않는다.
  - 가치판단을 하지 않는다. "개선·악화·우려"를 쓰지 않고 "증가·감소"만 쓴다.

빈 장을 그냥 비워 두지 않는 이유: 아무 말 없이 비어 있으면 "분석하지 않았다"가 아니라
"분석했는데 할 말이 없다"로 읽힌다. 왜 비어 있는지 적어야 한다.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from fpdf import FPDF
from fpdf.fonts import FontFace

import common
import config
from pipeline import calculate, compare, manual_sections, phrasing, profile, validate

TOOL_NAME = "auto-report"

# ── 마크다운 분해 (화면·PDF 공용) ──────────────────────────────────────
#: 개발용 자체 검사 블록의 시작 표시. self_check_block()이 만들고, 화면(app.py)과
#: PDF 생성이 "여기서부터는 본문이 아니다"를 판단하는 데 같이 쓴다.
SELF_CHECK_MARK = "### (개발용) 금지 표현 자체 검사"
_CHAPTER_RE = re.compile(r"^## (\d+)\.\s*(.+?)\s*$", re.MULTILINE)


def split_report(markdown: str) -> Tuple[str, List[Tuple[int, str, str]], str]:
    """리포트를 (머리말, [(번호, 제목, 본문)], 자체검사)로 자른다.

    화면(app.py)과 PDF 생성(build_pdf)이 함께 쓴다 — 장을 나누는 규칙이 두 곳에
    생기면 반드시 어긋난다.
    """
    text = str(markdown or "")
    self_check = ""
    if SELF_CHECK_MARK in text:
        head, _, tail = text.partition(SELF_CHECK_MARK)
        self_check = SELF_CHECK_MARK + tail
        text = head.rstrip().removesuffix("---").rstrip()

    matches = list(_CHAPTER_RE.finditer(text))
    if not matches:
        return text, [], self_check

    preface = text[: matches[0].start()].strip()
    chapters: List[Tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        chapters.append((int(match.group(1)), match.group(2), body))
    return preface, chapters, self_check

#: 사람이 쓰는 장에 남길 안내. 무엇을 쓸 자리인지 한 줄로 적는다.
HUMAN_SECTIONS = {
    2: (
        "배경·목적",
        "이 달 리포트를 왜 내는지, 어떤 질문에 답하려는지 적습니다. "
        "질문은 위키 `01_questions/`에 있습니다.",
    ),
    5: (
        "원인 분석",
        "아래 관련 분석을 근거로 원인을 판정하세요. 자동 생성하지 않습니다 — "
        "혼입 변수·역인과 검토는 기계적 판정 범위 밖입니다.",
    ),
    6: (
        "개선 제안",
        "무엇을 어떤 순서로 할지 적습니다. 자동 생성하지 않습니다 — "
        "임팩트·난이도 판단에는 조직 사정과 비용 정보가 필요합니다.",
    ),
}

PENDING_SECTIONS = {
    8: "부록 — 근거 데이터 상세",
}

#: 요약 문장으로 뽑을 지표 개수. 너무 적으면 상황이 안 보이고, 너무 많으면
#: 표를 문장으로 옮겨 적은 것이 되어 요약이 아니게 된다.
SUMMARY_MIN, SUMMARY_MAX = 3, 5

#: 지표 하나에 인용할 인사이트 최대 개수. 태그가 넓게 겹치는 지표는 후보가 5개 넘게
#: 나오는데, 다 붙이면 "관련 분석"이 아니라 목록이 된다.
INSIGHT_CITE_MAX = 3

#: 시사점 요약으로 옮길 최대 줄 수.
INSIGHT_SUMMARY_LINES = 3


# ── 공통 ──────────────────────────────────────────────────────────────
def _period_text(period: Any) -> str:
    if isinstance(period, dict):
        low, high = period.get("최소"), period.get("최대")
    elif isinstance(period, (tuple, list)) and len(period) == 2:
        low, high = period
    else:
        low = high = period
    if not low:
        return "기간 미상"
    return str(low) if low == high else f"{low} ~ {high}"


def _period_label(period: Any) -> str:
    """제목에 쓸 'YYYY-MM'. 여러 달이면 마지막 달을 쓴다."""
    text = _period_text(period)
    month = calculate.month_of(text.split("~")[-1].strip())
    return month or text


def _rows(frame: Optional[pd.DataFrame]) -> List[Dict[str, Any]]:
    if frame is None or len(frame) == 0:
        return []
    return [row.to_dict() for _, row in frame.iterrows()]


def _find(rows: Sequence[Dict[str, Any]], metric_id: str) -> Optional[Dict[str, Any]]:
    for row in rows:
        if str(row.get("metric_id")) == metric_id:
            return row
    return None


def _number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """마크다운 표. 셀에 파이프가 들어가면 표가 깨지므로 이스케이프한다."""
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    return "\n".join(lines)


def _threshold_of(metric_id: str, catalog: Dict[str, Any]) -> float:
    limit, _ = validate.metric_threshold(metric_id, catalog, config.MOM_THRESHOLD)
    return limit


def _by_movement(comparisons: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """변동이 큰 순으로 정렬한다. 비교 불가는 뒤로 보낸다.

    요약에 무엇을 먼저 쓸지는 "많이 움직인 것"이 기준이다. 어느 방향이 좋은지는
    판단하지 않으므로, 절대값만 본다.
    """
    def key(row: Dict[str, Any]) -> float:
        rate = _number(row.get("상대변화율"))
        return abs(rate) if rate is not None else -1.0

    return sorted(comparisons, key=key, reverse=True)


def _exceeded(
    comparisons: Sequence[Dict[str, Any]], catalog: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """지표별 임계값을 넘은 비교 행."""
    found = []
    for row in comparisons:
        rate = _number(row.get("상대변화율"))
        if str(row.get("비교상태")) != compare.COMPARABLE or rate is None:
            continue
        if abs(rate) >= _threshold_of(str(row.get("metric_id")), catalog):
            found.append(row)
    return found


# ── 머리말 ────────────────────────────────────────────────────────────
def document_header(context: Dict[str, Any]) -> str:
    catalog = context.get("metrics_catalog") or {}
    meta = catalog.get("_meta") or {}
    generated = context.get("생성일시") or dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    period = context.get("기간")

    rows = [
        ["생성일시", generated],
        ["대상 기간", _period_text(period)],
        ["생성 도구", TOOL_NAME],
        [
            "카탈로그 버전",
            f"{meta.get('생성일시', '알 수 없음')} (지표 {meta.get('항목_개수', '?')}종)",
        ],
    ]
    return (
        f"# 월간 지표 리포트 — {_period_label(period)}\n\n"
        + _table(["항목", "값"], rows)
        + "\n\n"
        f"> 이 문서는 자동 생성되었으며 "
        f"**{_human_slot_text()} 사람이 쓰는 자리입니다.**\n"
        "> 비어 있는 자리에는 '사람이 작성합니다' 안내가 그대로 남아 있습니다.\n"
        "> 자동 생성 부분은 계산 결과와 검증 결과를 그대로 옮긴 것으로, "
        "원인 해석이나 제안을 담지 않습니다."
    )


def _human_slot_text() -> str:
    """사람이 쓰는 자리 이름. 목록은 manual_sections 한 곳에서만 정한다.

    여기에 "2·5·6장"이라고 적어 두면 자리를 하나 늘릴 때 머리말이 조용히 거짓이 된다.
    제목까지 다 붙이면 한 줄이 넘으므로 번호만 묶어 쓴다 — 제목은 그 자리에 있다.
    """
    slots = sorted(manual_sections.MANUAL_SLOTS, key=manual_sections.slot_key)
    chapters = [s for s in slots if manual_sections.is_whole_chapter(s)]
    sections = [s for s in slots if not manual_sections.is_whole_chapter(s)]

    parts = []
    # 절 이름은 늘 "절"로 끝나므로 이어 붙이는 조사는 항상 "과"다.
    if sections:
        parts.append("·".join(sections) + "절")
    if chapters:
        parts.append("·".join(chapters) + "장")
    text = "과 ".join(parts)
    # 조사까지 붙여서 돌려준다. 호출하는 쪽이 "은/는"을 고르게 하면 조사 규칙이 두 곳에 생긴다.
    return text + phrasing.topic_particle(text)


# ── 1장: Executive Summary ────────────────────────────────────────────
def section_1_summary(context: Dict[str, Any]) -> str:
    """무엇을 계산했고, 무엇이 변했고, 검증은 어떤 상태인지만 적는다.

    **"그래서 무엇이 중요한가"는 자리만 만든다.** 중요도 판단은 조직의 목표에 달린
    것이라 계산 결과에서 끌어낼 수 없다 (CLAUDE.md 1절).
    """
    catalog = context.get("metrics_catalog") or {}
    metrics = _rows(context.get("metrics"))
    comparisons = _rows(context.get("comparison"))
    validation = context.get("validation") or {}
    period = context.get("기간")

    valued = [row for row in metrics if _number(row.get("value")) is not None]
    lines = ["## 1. Executive Summary", "", "**무엇을 계산했는가**", ""]
    lines.append(
        f"{_period_text(period)} 기준으로 지표 {len(metrics)}종을 계산했다. "
        f"이 중 {len(valued)}종에 값이 있고 {len(metrics) - len(valued)}종은 값이 없다."
    )

    lines += ["", "**무엇이 변했는가**", ""]
    exceeded = _exceeded(comparisons, catalog)
    if exceeded:
        for row in _by_movement(exceeded):
            metric_id = str(row.get("metric_id"))
            lines.append(f"- {phrasing.describe_change(row, _threshold_of(metric_id, catalog), catalog)}")
    else:
        lines.append("- 지표별 임계값을 넘은 지표는 없다.")

    incomparable = [
        row for row in comparisons if str(row.get("비교상태")) != compare.COMPARABLE
    ]
    if incomparable:
        names = ", ".join(str(row.get("지표명") or row.get("metric_id")) for row in incomparable)
        lines.append(f"- 전월 자료가 없어 비교하지 못한 지표: {names}")

    lines += ["", "**검증 상태**", ""]
    if validation:
        lines.append(
            f"검증 판정은 {validation.get('전체판정', '알 수 없음')}이며 "
            f"차단 {validation.get('차단수', 0)}건, 경고 {validation.get('경고수', 0)}건이다."
        )
        missing = validation.get("자동검증하지_않은_것") or []
        if missing:
            lines.append("")
            lines.append(f"자동으로 검증하지 않은 항목: {', '.join(missing)}.")
    else:
        lines.append("검증 결과가 없다.")

    lines += [
        "",
        "### 1-1. 핵심 시사점",
        "",
        "> 이 절은 사람이 작성합니다.",
        ">",
        "> 위 사실 중 무엇이 중요한지, 어디에 주의를 둘지 적습니다. "
        "중요도는 조직의 목표에 달린 것이라 계산 결과에서 끌어낼 수 없습니다.",
    ]
    return "\n".join(lines)


def _pending(number: int) -> str:
    title = PENDING_SECTIONS[number]
    return f"## {number}. {title}\n\n(아직 생성하지 않습니다.)"


def _human(number: int) -> str:
    title, hint = HUMAN_SECTIONS[number]
    return f"## {number}. {title}\n\n> 이 장은 사람이 작성합니다.\n>\n> {hint}"


# ── 2장 ───────────────────────────────────────────────────────────────
def section_2_background(context: Dict[str, Any]) -> str:
    return _human(2)


# ── 3장: 데이터·방법론 ────────────────────────────────────────────────
def definition_summary(metric_id: str, catalog: Dict[str, Any]) -> str:
    """정의서의 계산 명세를 한 줄로 줄인다. 유형마다 구조가 다르다."""
    calc = (catalog.get(metric_id) or {}).get("계산") or {}

    if "시차" in calc:
        base = profile._metric_refs({"기준지표": calc.get("기준지표")})
        return (
            f"기준지표 `{base[0] if base else '?'}` · 시차 {calc.get('시차')}개월 · "
            f"방향 {calc.get('방향', '증가')}"
        )
    if isinstance(calc.get("분자"), dict):  # 비율형
        # 분자·분모가 같은 원천·집계인데 조건만 다른 지표가 있다(저사용 고객 비율).
        # 조건 유무를 적지 않으면 두 항이 똑같아 보인다.
        return f"분자 {_leg_summary(calc.get('분자'))} / 분모 {_leg_summary(calc.get('분모'))}"
    if isinstance(calc.get("분자"), str):  # 파생형
        top = profile._metric_refs({"분자": calc.get("분자")})
        bottom = profile._metric_refs({"분모": calc.get("분모")})
        return f"분자 `{top[0] if top else '?'}` / 분모 `{bottom[0] if bottom else '?'}`"
    if "원천" in calc:  # 기초형
        return _leg_summary(calc)
    return "정의를 해석하지 못했습니다"


def _leg_summary(leg: Any) -> str:
    """계산 블록 하나(원천/집계/조건)를 짧게 적는다."""
    block = leg or {}
    text = f"`{block.get('원천')}` {block.get('집계')}"
    if block.get("조건"):
        text += " · 조건 있음"
    return text


def section_3_method(context: Dict[str, Any]) -> str:
    catalog = context.get("metrics_catalog") or {}
    meta = catalog.get("_meta") or {}
    metrics = _rows(context.get("metrics"))
    period = context.get("기간")

    lines = ["## 3. 데이터·방법론", "", "**대상 데이터**", ""]
    lines.append(
        _table(
            ["항목", "값"],
            [
                ["업로드 파일", f"`{context.get('파일명', '—')}`"],
                ["판정 테이블", f"`{context.get('테이블명', '—')}`"],
                ["기간", _period_text(period)],
                ["행수", f"{int(context.get('행수') or 0):,}행"],
            ],
        )
    )

    lines += ["", "**계산한 지표**", ""]
    rows = []
    for row in metrics:
        metric_id = str(row.get("metric_id"))
        spec = catalog.get(metric_id) or {}
        span = (spec.get("기간") or {}).get("자리표시자", "—")
        rows.append(
            [
                spec.get("지표명", metric_id),
                f"`{metric_id}`",
                definition_summary(metric_id, catalog),
                f"{(spec.get('기간') or {}).get('단위', '—')} / {span}",
                spec.get("유효구간", "—"),
            ]
        )
    lines.append(
        _table(["지표명", "metric_id", "정의 요약", "기간 단위 / 자리표시자", "유효구간"], rows)
        if rows
        else "계산한 지표가 없습니다."
    )

    lines += [
        "",
        "**사용한 정의 원천**",
        "",
        f"- 카탈로그 생성일시: {meta.get('생성일시', '알 수 없음')}",
        f"- 카탈로그에 담긴 지표: {meta.get('항목_개수', '?')}종 "
        f"(이 중 {len(metrics)}종을 이번 실행에서 계산)",
        "- 지표 정의는 위키에서 관리하며, 이 리포트는 위 시점의 스냅샷으로 계산했습니다.",
    ]

    partial = [row for row in metrics if bool(row.get("부분갱신"))]
    lines += ["", "**부분 갱신**", ""]
    if partial:
        staged = str(context.get("테이블명") or "")
        others = sorted(
            {
                part.strip()
                for row in partial
                for part in str(row.get("원천") or "").split("+")
                if part.strip() and part.strip() != staged
            }
        )
        names = ", ".join(f"`{row['metric_id']}`" for row in partial)
        lines.append(
            f"아래 {len(partial)}종은 업로드 파일 외의 테이블을 함께 사용합니다: {names}"
        )
        lines.append("")
        lines.append(
            f"이번 실행에서 갱신된 테이블은 `{staged}` 하나이며, "
            f"{', '.join(f'`{name}`' for name in others)}는 기존 상태를 사용했습니다."
        )
        freshness = context.get("source_freshness") or {}
        marks = []
        for table in others:
            dates = freshness.get(table) or {}
            filled = [f"{column} 최대 {value}" for column, value in sorted(dates.items()) if value]
            if filled:
                marks.append(f"`{table}` — {', '.join(filled)}")
        if marks:
            lines.append("")
            lines.append("갱신되지 않은 테이블의 최신 시점:")
            lines += [f"- {mark}" for mark in marks]
    else:
        lines.append("이번 실행에서 계산한 지표는 모두 업로드 파일만 사용했습니다.")

    return "\n".join(lines)


# ── 4장: 현황 ─────────────────────────────────────────────────────────
def _summary_sentences(
    comparisons: Sequence[Dict[str, Any]], catalog: Dict[str, Any]
) -> List[str]:
    """변동이 큰 지표부터 3~5문장으로 적는다.

    **임계값을 넘은 것을 먼저 놓는다.** 그 다음은 움직임이 큰 순서다. 문장은
    `phrasing.describe_change()`가 만들므로 표와 같은 규칙으로 서술된다.

    비교 불가 지표도 최소 개수를 채울 때는 넣는다 — 값이 없다는 것도 이번 달의
    사실이고, 요약에서 빠지면 표를 안 본 사람은 그런 지표가 있는 줄도 모른다.
    """
    if not comparisons:
        return ["전월 비교 결과가 없어 요약할 변동이 없습니다."]

    exceeded = _exceeded(comparisons, catalog)
    ordered = _by_movement(exceeded)
    seen = {str(row.get("metric_id")) for row in ordered}

    for row in _by_movement(comparisons):
        if len(ordered) >= SUMMARY_MAX:
            break
        metric_id = str(row.get("metric_id"))
        if metric_id in seen:
            continue
        ordered.append(row)
        seen.add(metric_id)
        if len(ordered) >= SUMMARY_MIN and not exceeded:
            continue

    sentences = []
    for row in ordered[:SUMMARY_MAX]:
        metric_id = str(row.get("metric_id"))
        sentences.append(f"- {phrasing.describe_change(row, _threshold_of(metric_id, catalog), catalog)}")
    return sentences


def section_4_status(context: Dict[str, Any]) -> str:
    catalog = context.get("metrics_catalog") or {}
    metrics = _rows(context.get("metrics"))
    comparisons = _rows(context.get("comparison"))
    period = context.get("기간")

    lines = [
        "## 4. 현황",
        "",
        f"{_period_text(period)} 기준 계산 결과입니다. 값과 전월 대비 변동만 적으며, "
        "변동의 배경은 5장에서 사람이 작성합니다.",
        "",
    ]

    # ── 요약 문장 ──
    # 표를 그대로 읽는 사람은 드물다. 많이 움직인 것부터 몇 문장으로 먼저 적는다.
    lines += _summary_sentences(comparisons, catalog)
    lines.append("")

    rows = []
    for row in metrics:
        metric_id = str(row.get("metric_id"))
        spec = catalog.get(metric_id) or {}
        kind = profile.value_kind(metric_id, catalog)
        changed = _find(comparisons, metric_id)
        rows.append(
            [
                spec.get("지표명", metric_id),
                phrasing.fmt_value(
                    row.get("value"), row.get("유형"), spec.get("지표명"), catalog, metric_id
                ),
                phrasing.fmt_value(
                    changed.get("전월") if changed else None,
                    row.get("유형"),
                    spec.get("지표명"),
                    catalog,
                    metric_id,
                ),
                phrasing.fmt_change(changed, catalog, metric_id),
                str(row.get("status") or "—"),
            ]
        )
    lines.append(
        _table(["지표명", "당월", "전월", "전월 대비", "상태"], rows)
        if rows
        else "계산된 지표가 없습니다."
    )

    # ── 임계값을 넘은 지표 ──
    exceeded = []
    for changed in comparisons:
        metric_id = str(changed.get("metric_id"))
        rate = _number(changed.get("상대변화율"))
        if str(changed.get("비교상태")) != compare.COMPARABLE or rate is None:
            continue
        # 기본값은 config에서 읽는다. 여기에 숫자를 박으면 화면과 리포트가 어긋난다.
        limit, declared = validate.metric_threshold(
            metric_id, catalog, config.MOM_THRESHOLD
        )
        if abs(rate) >= limit:
            exceeded.append((metric_id, changed, rate, limit, declared))

    lines += ["", "### 4-1. 전월 대비 변동이 큰 지표", ""]
    if exceeded:
        lines.append(
            f"아래 {len(exceeded)}종이 지표별 임계값을 넘었습니다. "
            "임계값 초과는 확인이 필요하다는 표시이며, 값이 틀렸다는 뜻이 아닙니다."
        )
        lines.append("")
        detail = []
        for metric_id, changed, rate, limit, declared in exceeded:
            spec = catalog.get(metric_id) or {}
            kind = profile.value_kind(metric_id, catalog)
            detail.append(
                [
                    spec.get("지표명", metric_id),
                    phrasing.fmt_value(
                        changed.get("전월"), None, None, catalog, metric_id
                    ),
                    phrasing.fmt_value(
                        changed.get("당월"), None, None, catalog, metric_id
                    ),
                    phrasing.fmt_change(changed, catalog, metric_id),
                    f"{limit:g}% ({'정의서' if declared else '기본값'})",
                ]
            )
        lines.append(_table(["지표명", "전월", "당월", "변동", "적용 임계값"], detail))
    else:
        lines.append("지표별 임계값을 넘은 지표가 없습니다.")

    # ── 값이 없는 지표 ──
    missing = [row for row in metrics if _number(row.get("value")) is None]
    incomparable = [
        row for row in comparisons if str(row.get("비교상태")) != compare.COMPARABLE
    ]
    if missing or incomparable:
        lines += ["", "### 4-2. 값이 없거나 비교하지 못한 지표", ""]
        detail = []
        for row in missing:
            metric_id = str(row.get("metric_id"))
            spec = catalog.get(metric_id) or {}
            detail.append(
                [spec.get("지표명", metric_id), "당월 값 없음", str(row.get("이유") or row.get("status") or "")]
            )
        for row in incomparable:
            metric_id = str(row.get("metric_id"))
            if _find(missing, metric_id):
                continue
            spec = catalog.get(metric_id) or {}
            detail.append(
                [spec.get("지표명", metric_id), "전월 대비 비교 불가", str(row.get("이유") or "")]
            )
        lines.append(_table(["지표명", "구분", "사유"], detail))
        lines.append("")
        lines.append("값이 없는 항목은 0으로 채우지 않았습니다. 값이 0인 것과 값이 없는 것은 다릅니다.")

    return "\n".join(lines)


# ── 5장: 원인 분석 (사람이 작성 + 인사이트 인용) ──────────────────────
def related_insights(
    metric_id: str, metrics_catalog: Dict[str, Any], insights_catalog: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """지표와 관련된 인사이트를 찾는다. 찾은 근거를 함께 돌려준다.

    **본문 링크를 먼저 본다.** 정의서 본문에 `[[i-005-...]]`라고 적혀 있으면 그건
    사람이 직접 연결해 둔 것이라 태그 우연 일치보다 근거가 강하다.

    **태그 겹침은 후보일 뿐이다.** '이탈분석' 태그 하나로 다섯 개가 걸리기도 한다.
    그래서 겹친 태그를 함께 적어 두고, 왜 걸렸는지 읽는 사람이 판단하게 한다.

    **여기서 하는 일은 찾아 주는 것까지다.** 어느 인사이트가 이번 변동의 원인인지는
    판단이므로 5장 본문에서 사람이 쓴다 (CLAUDE.md 1절).
    """
    found: List[Dict[str, Any]] = []
    seen = set()

    spec = metrics_catalog.get(metric_id) or {}
    for link in spec.get("관련인사이트_본문링크") or []:
        insight = insights_catalog.get(link)
        if insight and link not in seen:
            found.append({"insight_id": link, "인사이트": insight, "근거": "정의서 본문 링크"})
            seen.add(link)

    metric_tags = {str(tag) for tag in (spec.get("tags") or [])}
    if metric_tags:
        candidates = []
        for insight_id, insight in insights_catalog.items():
            if str(insight_id).startswith("_") or insight_id in seen:
                continue
            overlap = metric_tags & {str(tag) for tag in (insight.get("tags") or [])}
            if overlap:
                candidates.append((len(overlap), str(insight_id), sorted(overlap), insight))
        # 겹치는 태그가 많은 것부터. 같으면 id 순으로 — 실행마다 순서가 달라지면 안 된다.
        for _count, insight_id, overlap, insight in sorted(
            candidates, key=lambda item: (-item[0], item[1])
        ):
            found.append(
                {
                    "insight_id": insight_id,
                    "인사이트": insight,
                    "근거": f"태그 겹침({', '.join(overlap)})",
                }
            )
    return found


def _insight_summary(insight: Dict[str, Any]) -> List[str]:
    """시사점 절을 앞에서 몇 줄만 옮긴다. 전문은 위키에서 읽는다."""
    body = str(insight.get("본문") or "").strip()
    if not body:
        return []
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return lines[:INSIGHT_SUMMARY_LINES]


def section_5_cause(context: Dict[str, Any]) -> str:
    catalog = context.get("metrics_catalog") or {}
    insights = context.get("insights_catalog") or {}
    comparisons = _rows(context.get("comparison"))

    lines = [_human(5), "", "### 참고 — 위키에서 찾은 관련 분석", ""]

    if not insights:
        lines.append(
            "인사이트 카탈로그가 없어 관련 분석을 찾지 못했습니다. "
            "`python catalog/export_catalog.py`를 실행하면 만들어집니다."
        )
        return "\n".join(lines)

    exceeded = _by_movement(_exceeded(comparisons, catalog))
    if not exceeded:
        lines.append("임계값을 넘은 지표가 없어 관련 분석을 찾지 않았습니다.")
        return "\n".join(lines)

    lines.append(
        f"임계값을 넘은 {len(exceeded)}종에 대해 위키에서 관련 분석을 찾았습니다. "
        "인용만 하며, 어느 것이 이번 변동의 원인인지는 판단하지 않습니다."
    )

    for row in exceeded:
        metric_id = str(row.get("metric_id"))
        name = str(row.get("지표명") or metric_id)
        change = phrasing.fmt_change(row, catalog, metric_id)
        lines += ["", f"**{name}** (전월 대비 {change})", ""]

        matches = related_insights(metric_id, catalog, insights)
        if not matches:
            lines.append("- 관련 분석 없음 — 정의서 본문 링크도, 태그가 겹치는 인사이트도 없습니다.")
            continue

        for match in matches[:INSIGHT_CITE_MAX]:
            insight = match["인사이트"]
            title = str(insight.get("제목") or match["insight_id"])
            confidence = str(insight.get("confidence") or "미상")
            lines.append(
                f"- **{title}** · confidence {confidence} · 찾은 근거: {match['근거']}"
            )
            lines.append(f"  - 위키 노트: `04_insights/{insight.get('파일', match['insight_id'] + '.md')}`")
            summary = _insight_summary(insight)
            if summary:
                lines.append(f"  - {insight.get('본문_절') or '시사점'} 요약:")
                for line in summary:
                    lines.append(f"    > {line}")
            else:
                lines.append("  - 시사점 절이 비어 있어 요약할 내용이 없습니다.")

        if len(matches) > INSIGHT_CITE_MAX:
            rest = ", ".join(match["insight_id"] for match in matches[INSIGHT_CITE_MAX:])
            lines.append(f"- 태그가 겹치는 인사이트가 더 있습니다(인용 생략): {rest}")

    return "\n".join(lines)


def section_6_proposal(context: Dict[str, Any]) -> str:
    return _human(6)


# ── 7장: 한계 ─────────────────────────────────────────────────────────
def _threshold_note(metric_id: str, catalog: Dict[str, Any]) -> List[str]:
    """정의서가 임계값에 대해 적어 둔 설명을 찾아 인용거리로 돌려준다.

    `답할 수 없는 것` 절에서 '임계값'이 들어간 줄만 뽑는다. 이 절은 카탈로그가 담는
    유일한 본문이며(CLAUDE.md 3절), 임계값의 한계는 대개 여기 적혀 있다. 판단 근거·
    대안 검토 절은 카탈로그에 담지 않으므로 여기서 끌어오지 않는다.
    """
    section = (catalog.get(metric_id) or {}).get("답할_수_없는_것") or {}
    body = str(section.get("내용") or "")
    return [line.strip() for line in body.splitlines() if "임계값" in line and line.strip()]


def _limit_blocks(context: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """한계 소절을 (제목, 줄 목록)으로 모은다. 재료가 없는 소절은 만들지 않는다."""
    catalog = context.get("metrics_catalog") or {}
    metrics = _rows(context.get("metrics"))
    validation = context.get("validation") or {}
    freshness = context.get("source_freshness") or {}
    period = _period_text(context.get("기간"))
    staged = str(context.get("테이블명") or "")

    def name_of(metric_id: Any) -> str:
        return (catalog.get(str(metric_id)) or {}).get("지표명", str(metric_id))

    blocks: List[Tuple[str, List[str]]] = []

    extended = [
        row
        for row in metrics
        if str(row.get("status")) == calculate.EXTENDED or bool(row.get("구간확장"))
    ]
    if extended:
        lines = [
            f"정의서 유효구간을 넘어선 {period}을 계산했다. "
            "확장은 이번 실행에만 승인되었고 위키 정의서는 변경되지 않았다.",
            "",
            _table(
                ["지표명", "정의서 유효구간", "계산한 기간"],
                [
                    [
                        name_of(row.get("metric_id")),
                        (catalog.get(str(row.get("metric_id"))) or {}).get("유효구간", "—"),
                        str(row.get("month") or period),
                    ]
                    for row in extended
                ],
            ),
        ]
        blocks.append(("유효구간", lines))

    partial = [row for row in metrics if bool(row.get("부분갱신"))]
    if partial:
        others = sorted(
            {
                part.strip()
                for row in partial
                for part in str(row.get("원천") or "").split("+")
                if part.strip() and part.strip() != staged
            }
        )
        names = ", ".join(f"`{row['metric_id']}`" for row in partial)
        lines = [
            f"이번 실행에서 갱신된 테이블은 `{staged}` 하나다. "
            f"아래 {len(partial)}종은 갱신되지 않은 테이블을 함께 사용해 계산했다: {names}"
        ]
        for table in others:
            dates = freshness.get(table) or {}
            marks = [f"{column} 최대 {value}" for column, value in sorted(dates.items()) if value]
            lines.append("")
            if marks:
                lines.append(
                    f"`{table}` 테이블은 {', '.join(marks)} 상태이며, "
                    f"{period}의 변경은 반영되지 않았다."
                )
            else:
                lines.append(
                    f"`{table}` 테이블은 이번 실행에서 갱신되지 않았다. "
                    "어느 시점까지의 상태인지는 확인하지 못했다."
                )
        blocks.append(("데이터 갱신 범위", lines))

    not_automated = validation.get("자동검증하지_않은_것") or []
    if not_automated:
        lines = [
            validation.get("한계문장") or validate.LIMITATION_SENTENCE,
            "",
        ]
        lines += [f"- {item}" for item in not_automated]
        blocks.append(("수행하지 않은 검증", lines))

    provisional = [
        row
        for row in metrics
        if str((catalog.get(str(row.get("metric_id"))) or {}).get("임계값_상태") or "")
        == phrasing.PROVISIONAL
    ]
    if provisional:
        lines = [
            f"아래 {len(provisional)}종은 정의서가 임계값을 `잠정`으로 선언한 지표다. "
            "기준이 확정되면 값의 해석이 달라질 수 있다."
        ]
        for row in provisional:
            metric_id = str(row.get("metric_id"))
            lines.append("")
            lines.append(f"- **{name_of(metric_id)}** (`{metric_id}`)")
            quotes = _threshold_note(metric_id, catalog)
            if quotes:
                lines.append("  - 정의서가 적어 둔 설명:")
                lines += [f"    > {quote}" for quote in quotes]
            else:
                lines.append("  - 정의서에 임계값 설명이 없다.")
        blocks.append(("잠정 기준", lines))

    per_metric = [
        (str(row.get("metric_id")), (catalog.get(str(row.get("metric_id"))) or {}).get("답할_수_없는_것") or {})
        for row in metrics
    ]
    per_metric = [(metric_id, section) for metric_id, section in per_metric if section.get("내용")]
    if per_metric:
        lines = ['계산한 지표에 한해, 정의서의 "이 지표로 답할 수 없는 것" 절을 옮긴다.']
        for metric_id, section in per_metric:
            lines += ["", f"**{name_of(metric_id)}** (`{metric_id}`)", "", str(section["내용"]).strip()]
        blocks.append(("지표별 한계", lines))

    low_sample = [row for row in metrics if str(row.get("status")) == calculate.LOW_SAMPLE]
    if low_sample:
        lines = [
            f"아래 {len(low_sample)}종은 표본이 정의서의 최소표본에 미달했다. "
            "값은 계산했으나 결론의 근거로 쓰지 않는다.",
            "",
            _table(
                ["지표명", "표본", "최소표본"],
                [
                    [
                        name_of(row.get("metric_id")),
                        f"{_number(row.get('sample_size')) or 0:,.0f}",
                        f"{_number(row.get('min_sample')) or 0:,.0f}",
                    ]
                    for row in low_sample
                ],
            ),
        ]
        blocks.append(("표본", lines))

    return blocks


def section_7_limits(context: Dict[str, Any]) -> str:
    """한계를 재료에서 조립한다.

    **재료가 없는 소절은 만들지 않는다.** 빈 소절을 남기면 "확인했고 문제 없다"로
    읽히는데, 실제로는 해당 사항이 없었을 뿐이다. 소절 번호는 남은 것만으로 순서대로
    붙여 문서에 빈 번호가 생기지 않게 한다.

    **한계를 축소하거나 완화하는 문장을 붙이지 않는다.** "다만 큰 영향은 없을 것으로
    보인다" 같은 말은 영향 크기에 대한 판단이고, 그 판단은 사람이 5장에서 한다.
    여기서는 무엇이 한계인지만 적는다.
    """
    blocks = _limit_blocks(context)
    if not blocks:
        return "## 7. 한계\n\n한계로 적을 항목이 없다."

    out = ["## 7. 한계", ""]
    for index, (title, lines) in enumerate(blocks, start=1):
        out += [f"### 7-{index}. {title}", ""] + lines + [""]
    return "\n".join(out).rstrip()


# ── 8장 (다음 단계) ───────────────────────────────────────────────────
def section_8_appendix(context: Dict[str, Any]) -> str:
    return _pending(8)


# ── 조립 ──────────────────────────────────────────────────────────────
SECTIONS = (
    section_1_summary,
    section_2_background,
    section_3_method,
    section_4_status,
    section_5_cause,
    section_6_proposal,
    section_7_limits,
    section_8_appendix,
)


def self_check_block(markdown: str) -> str:
    """금지 표현 자체 검사 결과를 문서 끝에 붙인다.

    **지금은 학습용으로 화면에 보이게 둔다.** 운영에서는 로그로만 남기고 문서에는
    넣지 않는 것이 맞다 — 리포트를 받는 사람이 볼 내용이 아니다.

    걸린 것이 전부 위반은 아니다. 장 제목("6. 개선 제안")이나 "이 장은 사람이
    작성합니다" 같은 안내문에도 같은 단어가 들어간다. 그래서 제목·인용 줄에는
    표시를 달아 구분한다.
    """
    findings = phrasing.check_forbidden(markdown)
    if not findings:
        return (
            "---\n\n"
            f"{SELF_CHECK_MARK}\n\n"
            "금지 표현이 발견되지 않았습니다."
        )

    body = [item for item in findings if not item["제목줄"] and not item["인용줄"]]
    lines = [
        "---",
        "",
        SELF_CHECK_MARK,
        "",
        "> 학습용으로 문서에 표시합니다. 운영에서는 로그로만 남깁니다.",
        "",
        f"{len(findings)}건이 걸렸고, 이 중 제목·인용을 뺀 본문은 {len(body)}건입니다.",
        "",
        _table(
            ["줄", "분류", "표현", "위치", "문맥"],
            [
                [
                    item["줄"],
                    item["분류"],
                    item["표현"],
                    "제목" if item["제목줄"] else ("인용" if item["인용줄"] else "본문"),
                    item["문맥"],
                ]
                for item in findings
            ],
        ),
    ]
    return "\n".join(lines)


def build_report(run_context: Dict[str, Any]) -> str:
    """8장 구조 리포트를 마크다운 문자열로 만든다.

    `run_context`에 기대하는 키:
        파일명, 테이블명, 기간, 행수
        metrics (DataFrame), comparison (DataFrame), validation (dict)
        metrics_catalog, schema_catalog (dict)
        run_log (dict), source_freshness (dict, 선택)

    없는 키는 "—"나 "알 수 없음"으로 적는다. **빈 값을 0으로 바꾸지 않는다.**
    """
    context = dict(run_context or {})
    parts = [document_header(context)]
    parts += [section(context) for section in SECTIONS]
    markdown = "\n\n".join(part.strip() for part in parts if part and part.strip()) + "\n"

    # 자체 검사는 본문을 다 만든 뒤에 돌린다. 검사 결과 자체가 금지 표현을 담고 있어,
    # 먼저 붙이면 자기 자신을 검사하게 된다.
    if context.get("자체검사", True):
        markdown += "\n" + self_check_block(markdown) + "\n"
    return markdown


def build_report_with_manual(
    run_context: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, List[str]]]:
    """리포트를 만들고 사람이 쓴 자리를 끼워 넣는다.

    `(마크다운, manual dict, {"치환": [...], "남김": [...]})`을 돌려준다.

    **화면과 명령줄이 이 함수를 함께 쓴다.** 병합을 화면 쪽에만 두면 CLI로 만든
    리포트에는 사람이 쓴 글이 빠지고, "화면과 CLI 결과가 다르다"가 된다.

    `build_report`는 병합 없이 남겨 둔다 — 자동 생성만 검증하는 테스트가 있고,
    사람 작성분이 섞이면 무엇을 검증했는지 흐려진다.
    """
    markdown = build_report(run_context)
    period = ((run_context or {}).get("기간") or {})
    month = period.get("최대") or period.get("최소") if isinstance(period, dict) else period
    manual = manual_sections.load_manual(month)
    markdown, outcome = manual_sections.merge_into_report(markdown, manual)
    return markdown, manual, outcome


# ── PDF ───────────────────────────────────────────────────────────────
# CLAUDE.md 7절 하드 제약: kaleido(Plotly 정적 이미지)가 이 환경에서 불안정하므로
# 차트 이미지를 넣지 않는다. 텍스트와 표만으로 구성한다.
#
# fpdf2는 상태 저장형이다(DESIGN.md 함정) — set_draw_color()로 한 번 색을 바꾸면
# 이후 그려지는 모든 선(표 테두리 포함)이 그 색을 물려받는다. 강조색을 쓴 자리마다
# 반드시 INK로 되돌린다.

PDF_FONT = "NotoSansKR"
INK = "#111827"  # 거의 검정. email_draft.py 본문 텍스트 색과 맞춘다.

#: calculate.py 상태·validate.py 판정 "문자열" → 색 (CLAUDE.md 7절). 화면(app.py)·
#: 이메일(email_draft.py)과 같은 대응이다. 문자열로 매핑하는 이유: 이 시점에는
#: 마크다운 표 셀의 텍스트만 남아 있고, 원래의 status 값(파이썬 상수)은 사라진 뒤다.
_STATUS_TEXT_COLORS: Dict[str, str] = {
    "OK": common.COLOR_OK,
    "구간확장": common.COLOR_WARN,
    "표본부족": common.COLOR_WARN,
    "계산오류": common.COLOR_BLOCK,
    "유효구간 밖": common.COLOR_NODATA,
    "데이터 없음": common.COLOR_NODATA,
    "미지원": common.COLOR_NODATA,
    "통과": common.COLOR_OK,
    "경고": common.COLOR_WARN,
    "차단": common.COLOR_BLOCK,
}


def fonts_available() -> bool:
    """Regular·Bold가 둘 다 있는가. 화면이 PDF 버튼을 켤지 이걸로 판단한다."""
    return all(
        (config.FONTS_DIR / name).exists()
        for name in ("NotoSansKR-Regular.ttf", "NotoSansKR-Bold.ttf")
    )


class _ReportPDF(FPDF):
    """쪽번호만 자동으로 붙인다. 그 외 폰트·색은 build_pdf가 직접 관리한다."""

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(PDF_FONT, "", 8)
        self.set_text_color(INK)
        self.cell(0, 10, f"{self.page_no()} / {{nb}}", align="C")


def _register_fonts(pdf: FPDF) -> None:
    pdf.add_font(PDF_FONT, "", str(config.FONTS_DIR / "NotoSansKR-Regular.ttf"))
    pdf.add_font(PDF_FONT, "B", str(config.FONTS_DIR / "NotoSansKR-Bold.ttf"))


def _reset_ink(pdf: FPDF) -> None:
    """강조색을 쓴 뒤 반드시 부른다.

    draw_color·text_color뿐 아니라 **fill_color도 되돌린다.** 표지의 경고 박스에서
    `set_fill_color`를 쓴 뒤 이걸 빼먹으면, `cell_fill_mode`를 안 켰는데도 이후
    모든 `pdf.table()` 셀이 그 색으로 채워진다 — 실제로 겪은 문제다. fpdf2는
    `cell_fill_mode=NONE`이어도 칠할 색 자체는 현재 상태(fill_color)를 그대로 쓴다.
    """
    pdf.set_draw_color(INK)
    pdf.set_text_color(INK)
    pdf.set_fill_color("#ffffff")


def _clean_cell_text(value: str) -> str:
    """백틱을 없앤다. Noto Sans KR만 쓰므로 고정폭 표시가 안 돼 백틱만 남으면 눈에 띈다."""
    return value.replace("`", "")


def _parse_table_block(lines: Sequence[str]) -> Optional[Tuple[List[str], List[List[str]]]]:
    """마크다운 표 한 덩어리를 (헤더, 행들)로 뜯는다. 구분선(`|---|---|`)은 버린다."""

    def split_row(line: str) -> List[str]:
        inner = line.strip().strip("|")
        # 이스케이프된 파이프(\|)는 셀 구분자가 아니다 — 잠깐 다른 문자로 바꿔 나눈 뒤 되돌린다.
        placeholder = "\x00"
        cells = inner.replace("\\|", placeholder).split("|")
        return [_clean_cell_text(cell.replace(placeholder, "|")).strip() for cell in cells]

    rows = [split_row(line) for line in lines if line.strip()]
    if len(rows) < 2:
        return None

    header, separator, *body = rows
    is_separator = all(set(cell) <= {"-", ":", ""} for cell in separator)
    if not is_separator:
        body = [separator] + body  # 구분선이 없는 표(드묾) — 그냥 데이터 행으로 취급한다
    return header, body


def _find_first_table(text: str) -> Optional[Tuple[List[str], List[List[str]]]]:
    """텍스트에서 처음 나오는 마크다운 표 하나. 표지가 머리말 표를 읽을 때 쓴다."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), None)
    if start is None:
        return None
    block: List[str] = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        block.append(line)
    return _parse_table_block(block)


def _render_table(pdf: FPDF, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """마크다운 표를 pdf.table()로 그린다. 상태 단어가 있는 칸만 CLAUDE.md 7절 색으로."""
    pdf.set_x(pdf.l_margin)
    pdf.set_font(PDF_FONT, "", 9)
    _reset_ink(pdf)  # 표 테두리를 그리기 전, 혹시 남아있을 강조색을 지운다
    with pdf.table(borders_layout="ALL", text_align="LEFT", line_height=5, markdown=True) as table:
        header_row = table.row()
        for value in headers:
            header_row.cell(value)
        for values in rows:
            row = table.row()
            for value in values:
                color = _STATUS_TEXT_COLORS.get(value.strip())
                if color:
                    row.cell(value, style=FontFace(color=color))
                else:
                    row.cell(value)
    _reset_ink(pdf)
    pdf.set_font(PDF_FONT, "", 11)
    pdf.ln(2)


def _indent_x(pdf: FPDF, spaces: int) -> float:
    """공백 2칸을 한 단계로 본다 — 실제 리포트의 중첩 글머리·인용이 이 폭으로 들여쓴다."""
    return pdf.l_margin + (spaces // 2) * 5


def _heading(pdf: FPDF, text: str, size: float = 12) -> None:
    _reset_ink(pdf)
    pdf.set_x(pdf.l_margin)
    pdf.set_font(PDF_FONT, "B", size)
    pdf.multi_cell(0, 7, _clean_cell_text(text), markdown=True)
    pdf.set_font(PDF_FONT, "", 11)
    pdf.ln(1)


def _paragraph(pdf: FPDF, text: str) -> None:
    _reset_ink(pdf)
    pdf.set_x(pdf.l_margin)
    pdf.set_font(PDF_FONT, "", 11)
    pdf.multi_cell(0, 6, _clean_cell_text(text), markdown=True)
    pdf.ln(2)


def _bullet_line(pdf: FPDF, text: str, indent: int) -> None:
    _reset_ink(pdf)
    pdf.set_font(PDF_FONT, "", 11)
    x = _indent_x(pdf, indent)
    pdf.set_x(x)
    width = pdf.w - pdf.r_margin - x
    pdf.multi_cell(width, 6, _clean_cell_text(f"- {text}"), markdown=True)


def _quote_line(pdf: FPDF, text: str, indent: int) -> None:
    if not text:
        pdf.ln(2)
        return
    # 이탤릭 서체가 없다(Regular·Bold만 등록했다) — 색으로만 인용을 구분한다.
    pdf.set_font(PDF_FONT, "", 10)
    pdf.set_text_color(common.COLOR_NODATA)
    x = _indent_x(pdf, indent) + 4
    pdf.set_x(x)
    width = pdf.w - pdf.r_margin - x
    pdf.multi_cell(width, 6, _clean_cell_text(text), markdown=True)
    _reset_ink(pdf)
    pdf.set_font(PDF_FONT, "", 11)


def _render_body(pdf: FPDF, body: str) -> None:
    """장 본문을 줄 단위로 그린다. 표는 pdf.table로, 나머지는 문단·글머리·인용으로 나눈다.

    블록(빈 줄 기준)이 아니라 **줄 단위**로 처리한다. 인용 안에 글머리가 중첩되는
    경우(5장의 인사이트 인용)가 있어, 빈 줄 없이도 종류가 바뀔 수 있기 때문이다.
    """
    lines = body.strip("\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if not stripped or stripped == "---":
            index += 1
            continue

        if stripped.startswith("|"):
            block: List[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            parsed = _parse_table_block(block)
            if parsed:
                _render_table(pdf, *parsed)
            continue

        if stripped.startswith("#"):
            _heading(pdf, stripped.lstrip("#").strip())
            index += 1
            continue

        indent = len(raw) - len(raw.lstrip())
        if stripped.startswith(">"):
            _quote_line(pdf, stripped[1:].strip(), indent)
            index += 1
            continue

        if stripped.startswith("- "):
            _bullet_line(pdf, stripped[2:].strip(), indent)
            index += 1
            continue

        # 이어지는 문단 — 표·헤딩·글머리·인용이 나오기 전까지 한 문단으로 모은다.
        paragraph = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if not nxt or nxt.startswith(("|", "#", ">", "- ")):
                break
            paragraph.append(nxt)
            index += 1
        _paragraph(pdf, " ".join(paragraph))


def _cover_page(pdf: FPDF, preface: str, markdown: str) -> None:
    """표지: 제목, 대상 기간, 생성일시, 미작성 자리 개수.

    **다른 값을 따로 받지 않는다.** 제목·기간·생성일시는 본문 머리말 표(document_header
    가 만든 것)에서 그대로 읽는다 — 표지와 본문이 다른 소리를 하지 않게 하기 위해서다.
    """
    pdf.add_page()
    lines = preface.splitlines()
    title = lines[0].lstrip("#").strip() if lines else "월간 지표 리포트"
    table = _find_first_table(preface)
    info = {row[0]: row[1] for row in table[1] if row} if table else {}

    pdf.ln(25)
    pdf.set_x(pdf.l_margin)
    pdf.set_font(PDF_FONT, "B", 20)
    pdf.multi_cell(0, 12, title, align="C")
    pdf.ln(12)

    pdf.set_font(PDF_FONT, "", 12)
    for label in ("대상 기간", "생성일시"):
        value = info.get(label)
        if not value:
            continue
        pdf.set_x(pdf.l_margin + 25)
        pdf.cell(32, 9, label)
        pdf.multi_cell(0, 9, value)

    pending = manual_sections.pending_slots(markdown)
    pdf.ln(10)
    box_color = common.COLOR_WARN if pending else common.COLOR_OK
    pdf.set_fill_color(box_color)
    pdf.set_text_color("#ffffff")
    pdf.set_font(PDF_FONT, "B", 12)
    label_text = (
        f"미작성 {len(pending)}곳 — 사람이 채워야 완성됩니다"
        if pending
        else "사람이 쓰는 자리까지 모두 채워졌습니다"
    )
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 12, label_text, align="C", fill=True)
    _reset_ink(pdf)
    pdf.set_font(PDF_FONT, "", 11)


def build_pdf(markdown: str) -> bytes:
    """리포트 마크다운을 PDF 바이트로 만든다.

    **markdown 하나만 받는다.** 표지에 쓰는 제목·대상 기간·생성일시는 전부 이
    문서 자신의 머리말 표에서 읽으므로, 같은 markdown을 넣으면 항상 같은 PDF가
    나온다(CLAUDE.md 5-5) — 계산에 현재 시각을 새로 쓰지 않는다.

    폰트가 없으면 `FileNotFoundError`를 낸다. 화면은 이걸로 PDF 버튼을 끄고,
    마크다운 리포트는 그대로 보여준다(폰트 하나 없다고 리포트 전체를 막지 않는다).
    자체 검사 블록(개발용)은 8장 구조가 아니므로 PDF에 넣지 않는다.
    """
    if not fonts_available():
        raise FileNotFoundError(
            f"{config.FONTS_DIR}에 NotoSansKR-Regular.ttf·Bold.ttf가 없습니다. "
            "python scripts/get_fonts.py로 받으세요."
        )

    pdf = _ReportPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.alias_nb_pages()
    _register_fonts(pdf)
    pdf.set_font(PDF_FONT, "", 11)
    _reset_ink(pdf)

    preface, chapters, _self_check = split_report(markdown)
    _cover_page(pdf, preface, markdown)
    for number, title, body in chapters:
        pdf.add_page()
        _heading(pdf, f"{number}. {title}", size=14)
        _render_body(pdf, body)

    return bytes(pdf.output())
