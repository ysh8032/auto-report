"""색상 상수·상태 배지·공통 유틸.

상태 색은 **여기서만** 정의한다. 화면·리포트·이메일이 같은 값을 읽어야
"화면에서 경고인 항목이 PDF에서 정상으로 보이는" 일이 생기지 않는다
(CLAUDE.md 7절, DESIGN.md 원칙 8).
"""

from __future__ import annotations

import html
from typing import Optional, Sequence

import streamlit as st


def esc(value: object) -> str:
    """HTML 조각에 값을 넣기 전에 반드시 통과시킨다."""
    return html.escape(str(value))

# ── 지표·검증 상태 색 (CLAUDE.md 7절 — 이 대응을 바꾸지 않는다) ──────
COLOR_OK = "#10b981"      # emerald — 통과 / OK
COLOR_WARN = "#f59e0b"    # amber   — 경고 / 표본부족
COLOR_BLOCK = "#f43f5e"   # rose    — 차단 / 검증 실패
COLOR_NODATA = "#64748b"  # slate   — 데이터 없음 / 유효구간 밖

#: 진행 상태 전용. 지표 status와는 **다른 축**이라 위 4색을 쓰지 않는다.
#: 진행중에 amber를 쓰면 "경고"로 읽히기 때문이다. Tailwind blue 500.
COLOR_PROGRESS = "#3b82f6"

#: 차트 계열 색 (Tailwind 500). DESIGN.md 원칙 1 — 임의로 고른 색을 쓰지 않는다.
#: **상태 4색(emerald·amber·rose·slate)은 계열 색으로 쓰지 않는다** — 선 색이
#: 상태로 읽히면 "빨간 선 = 문제"처럼 보인다.
CHART_BLUE = "#3b82f6"    # blue
CHART_INDIGO = "#6366f1"  # indigo
CHART_VIOLET = "#8b5cf6"  # violet
CHART_CYAN = "#06b6d4"    # cyan

# 상태 이름. 문자열 리터럴을 흩뿌리지 말고 이 상수를 쓴다.
OK = "ok"
WARN = "warn"
BLOCK = "block"
NODATA = "nodata"

STATUS_COLORS = {
    OK: COLOR_OK,
    WARN: COLOR_WARN,
    BLOCK: COLOR_BLOCK,
    NODATA: COLOR_NODATA,
}

#: status 값 → 화면에 쓸 한국어 라벨
STATUS_LABELS = {
    OK: "통과",
    WARN: "경고",
    BLOCK: "차단",
    NODATA: "데이터 없음",
}


# ── 배지 ──────────────────────────────────────────────────────────────
def tag(label: str, color: str = COLOR_NODATA) -> str:
    """색을 직접 지정하는 알약 배지 HTML.

    상태(지표 status·검증 결과)에는 status_badge()를 쓴다. 이 함수는 '게이트',
    '사용자' 같은 **상태가 아닌 꼬리표**에만 쓴다.
    """
    return (
        '<span style="display:inline-block;padding:1px 9px;border-radius:999px;'
        "font-size:0.75rem;font-weight:600;line-height:1.7;white-space:nowrap;"
        f'color:{color};background:{color}1f;border:1px solid {color}59;">'
        f"{html.escape(str(label))}</span>"
    )


def status_badge(status: str, label: Optional[str] = None) -> str:
    """상태 배지 HTML. 모르는 status는 slate(판단 근거 없음)로 떨어진다."""
    color = STATUS_COLORS.get(status, COLOR_NODATA)
    return tag(label if label is not None else STATUS_LABELS.get(status, status), color)


def render_badges(*badges: str, gap: str = "0.4rem") -> None:
    """배지 여러 개를 한 줄로 렌더링한다."""
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:{gap};flex-wrap:wrap;">'
        + "".join(badges)
        + "</div>",
        unsafe_allow_html=True,
    )


# ── 표 ────────────────────────────────────────────────────────────────
def html_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    row_styles: Optional[Sequence[str]] = None,
) -> str:
    """셀에 배지를 넣을 수 있는 표를 직접 그린다.

    `st.dataframe`은 canvas(glide-data-grid) 기반이라 셀에 HTML·CSS를 넣을 수 없다
    (DESIGN.md 1절). 상태 색이 표 안에 보여야 하므로 표를 손으로 만든다.

    **셀 내용은 호출하는 쪽이 안전하게 만들어 넘긴다** — 이 함수는 이스케이프하지
    않는다. 사람이 쓴 문자열은 esc()를 통과시키고, 배지는 그대로 넣는다.

    색은 테마에 따라 달라지도록 회색 계열을 rgba로 둔다. 흰색·검은색을 박으면
    다크 모드에서 글자가 사라진다.
    """
    line = "1px solid rgba(128,128,128,0.25)"
    head = "".join(
        f'<th style="text-align:left;padding:.45rem .6rem;white-space:nowrap;'
        f'border-bottom:2px solid rgba(128,128,128,0.4);font-weight:700;">{h}</th>'
        for h in headers
    )
    styles = list(row_styles or [])
    body = "".join(
        f'<tr style="{styles[index] if index < len(styles) else ""}">'
        + "".join(
            f'<td style="padding:.45rem .6rem;border-bottom:{line};'
            f'vertical-align:top;">{cell}</td>'
            for cell in row
        )
        + "</tr>"
        for index, row in enumerate(rows)
    )
    return (
        '<div style="overflow-x:auto;">'
        '<table style="border-collapse:collapse;width:100%;font-size:.88rem;'
        'line-height:1.5;">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    row_styles: Optional[Sequence[str]] = None,
) -> None:
    st.markdown(html_table(headers, rows, row_styles), unsafe_allow_html=True)


def hint(text: str, tooltip: str) -> str:
    """마우스를 올리면 설명이 뜨는 작은 표시. 표 안에서 자리를 적게 쓴다."""
    return (
        f'<span title="{esc(tooltip)}" style="cursor:help;font-size:.72rem;'
        f"font-weight:700;color:{COLOR_NODATA};border:1px solid {COLOR_NODATA}59;"
        f'border-radius:4px;padding:0 4px;margin-left:.25rem;">{esc(text)}</span>'
    )


def mono(value: object) -> str:
    """컬럼명·테이블명처럼 그대로 읽혀야 하는 값. 밑줄이 기울임으로 먹지 않게 한다."""
    return (
        '<code style="font-size:.85em;padding:1px 4px;border-radius:4px;'
        f'background:rgba(128,128,128,0.15);">{esc(value)}</code>'
    )


def muted(text: str) -> str:
    return f'<div style="font-size:.82em;color:{COLOR_NODATA};margin-top:.2rem;">{esc(text)}</div>'


# ── 포맷 ──────────────────────────────────────────────────────────────
def format_metric(value: object, kind: str, compact: bool = False) -> str:
    """지표 값 서식. 단위는 profile.value_kind()가 정의서에서 유도한 것을 받는다.

    화면·리포트·이메일이 같은 숫자를 같은 모양으로 내야 하므로 여기 한 곳에만 둔다.

    **값이 없으면 0으로 채우지 않고 '—'로 둔다.** "값이 0"과 "값이 없다"는 다르다
    (CLAUDE.md 9절).

    `compact=True`면 백만 이상 금액을 '27.8백만원'으로 줄인다. 카드처럼 폭이 좁은
    자리에서만 쓰고, 표에는 원 단위를 그대로 쓴다.
    """
    if value is None:
        return "—"
    try:
        if value != value:  # NaN
            return "—"
        number = float(value)
    except (TypeError, ValueError):
        return "—"

    if kind == "금액":
        if compact and abs(number) >= 1_000_000:
            return f"{number / 1_000_000:,.1f}백만원"
        return f"{number:,.0f}원"
    if kind == "비율":
        return f"{number * 100:,.1f}%"
    if kind == "GB":
        return f"{number:,.1f} GB"
    if kind in ("명", "건"):
        return f"{number:,.0f}{kind}"
    return f"{number:,.2f}"


def format_delta(row: object, kind: str) -> Optional[str]:
    """st.metric의 delta 문자열. 비율 지표는 퍼센트포인트, 나머지는 상대변화율.

    방향만 나타내고 좋고 나쁨은 판단하지 않는다 — 해석은 리포트에서 사람이 쓴다.
    """
    if row is None:
        return None
    points = row.get("퍼센트포인트변화")
    if points is not None and points == points:
        return f"{float(points):+,.1f}%p"
    rate = row.get("상대변화율")
    if rate is not None and rate == rate:
        return f"{float(rate):+,.1f}%"
    return None


def format_kb(size_bytes: int) -> str:
    """파일 크기. 1KB 미만은 '0.0 KB'(빈 파일처럼 읽힌다) 대신 바이트로 적는다."""
    if size_bytes < 1024:
        return f"{size_bytes:,} B"
    return f"{size_bytes / 1024:,.1f} KB"


def format_timestamp(value: Optional[str]) -> str:
    """카탈로그 _meta의 ISO 문자열을 화면용으로 줄인다. 못 읽으면 원문 그대로."""
    if not value:
        return "알 수 없음"
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)
