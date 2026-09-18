"""7단계 — 이메일 초안 생성.

**메일을 보내지 않는다.** SMTP에 연결하지 않고, 보낼 내용만 만든다. 6주차 범위는
초안 생성과 발송 확정까지이며, 실제 전송은 8주차에 이 지점에 SMTP를 꽂아 넣는다
(CLAUDE.md 5-3). 그래서 반환값은 "그대로 전송에 넘길 수 있는 모양"이다.

**수신자는 config의 예시값을 쓴다.** 실제 주소를 코드에 적지 않는다.

본문에 담지 않는 것
    - 리포트 전문 — 첨부로 보낸다. 본문이 길어지면 아무도 읽지 않는다.
    - 차트 이미지 — 이메일 클라이언트가 외부 이미지를 막고, 정적 이미지 변환(kaleido)도
      이 환경에서 불안정하다 (DESIGN.md 1절).
    - 해석·제안 문장 — 리포트 5·6장의 몫이고, 그 장은 미작성일 수 있다. 없는 해석을
      메일 본문이 대신 쓰면 안 된다.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import config
from pipeline import calculate, compare, manual_sections, phrasing, profile, validate

TOOL_NAME = "auto-report"

#: 본문 표에 넣을 지표 개수. 4개 미만이면 상황이 안 보이고, 6개를 넘으면 메일에서
#: 표를 읽지 않게 된다.
HEADLINE_MIN, HEADLINE_MAX = 4, 6

#: 이메일 본문 최대 폭. 600px이 이메일 클라이언트의 사실상 표준이고,
#: 넘기면 Outlook 계열에서 오른쪽이 잘린다.
MAX_WIDTH_PX = 600

#: 첨부 목록에 올릴 파일. 실제로 붙이지는 않고 이름·경로·크기만 적는다.
ATTACHMENT_NAMES: Tuple[str, ...] = (
    "report.md",
    "report.pdf",
    "metrics.csv",
    "comparison.csv",
)

_LIMIT_HEADING_RE = re.compile(r"^###\s*(7-\d+\.\s*.+?)\s*$", re.MULTILINE)


# ── 공통 ──────────────────────────────────────────────────────────────
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
    return calculate.month_of(_period_text(period).split("~")[-1].strip()) or "기간 미상"


def _threshold_of(metric_id: str, catalog: Dict[str, Any]) -> float:
    limit, _ = validate.metric_threshold(metric_id, catalog, config.MOM_THRESHOLD)
    return limit


def _exceeded(
    comparisons: Sequence[Dict[str, Any]], catalog: Dict[str, Any]
) -> List[Dict[str, Any]]:
    found = []
    for row in comparisons:
        rate = _number(row.get("상대변화율"))
        if str(row.get("비교상태")) != compare.COMPARABLE or rate is None:
            continue
        if abs(rate) >= _threshold_of(str(row.get("metric_id")), catalog):
            found.append(row)
    return sorted(found, key=lambda row: abs(_number(row.get("상대변화율")) or 0), reverse=True)


# ── 제목 ──────────────────────────────────────────────────────────────
def build_subject(
    period: Any, validation: Dict[str, Any], pending: Sequence[str]
) -> str:
    """제목에 상태를 적는다.

    **받는 사람이 열기 전에 알아야 할 것**이 두 가지다 — 확인할 경고가 있는지,
    아직 사람이 안 쓴 자리가 있는지. 제목에 없으면 본문을 열어야 알게 된다.
    """
    subject = f"{config.EMAIL_SUBJECT_PREFIX} CS 지표 리포트 {_period_label(period)}"
    if int((validation or {}).get("경고수") or 0) or int((validation or {}).get("차단수") or 0):
        subject += " (확인 필요)"
    if pending:
        subject += " (초안)"
    return subject


# ── 재료 ──────────────────────────────────────────────────────────────
def headline_rows(
    context: Dict[str, Any], limit: int = HEADLINE_MAX
) -> List[Dict[str, Any]]:
    """본문 표에 넣을 지표를 고른다.

    **지표 이름을 코드에 박지 않는다.** 임계값을 넘은 것을 먼저 넣고, 남는 자리를
    값이 있는 지표로 채운다. 정의서가 바뀌어도 코드를 고칠 필요가 없다.
    """
    catalog = context.get("metrics_catalog") or {}
    metrics = _rows(context.get("metrics"))
    comparisons = _rows(context.get("comparison"))

    picked: List[Dict[str, Any]] = []
    seen: set = set()

    for changed in _exceeded(comparisons, catalog):
        metric_id = str(changed.get("metric_id"))
        row = _find(metrics, metric_id)
        if row and metric_id not in seen:
            picked.append(row)
            seen.add(metric_id)

    for row in metrics:
        if len(picked) >= limit:
            break
        metric_id = str(row.get("metric_id"))
        if metric_id in seen or _number(row.get("value")) is None:
            continue
        picked.append(row)
        seen.add(metric_id)

    # 값이 있는 지표만으로 최소 개수를 못 채우면 값 없는 지표도 넣는다 —
    # 값이 없다는 것도 이번 달의 사실이라 표에서 빠지면 안 된다.
    for row in metrics:
        if len(picked) >= HEADLINE_MIN:
            break
        metric_id = str(row.get("metric_id"))
        if metric_id not in seen:
            picked.append(row)
            seen.add(metric_id)

    return picked[:limit]


def limit_headings(report_md: str) -> List[str]:
    """리포트 7장의 소절 제목만 뽑는다. 내용은 첨부에서 읽는다."""
    return [match.group(1).strip() for match in _LIMIT_HEADING_RE.finditer(str(report_md or ""))]


def attachment_list(run_dir: Any) -> List[Dict[str, Any]]:
    """첨부할 파일의 이름·경로·크기. **실제로 붙이지 않는다** (8주차).

    없는 파일도 목록에 남기고 크기를 None으로 둔다. 조용히 빼면 받는 사람이
    "PDF는 원래 안 오는 것"인지 "이번에 빠진 것"인지 알 수 없다.
    """
    folder = Path(run_dir) if run_dir else None
    items: List[Dict[str, Any]] = []
    for name in ATTACHMENT_NAMES:
        path = (folder / name) if folder else None
        exists = bool(path and path.exists())
        items.append(
            {
                "filename": name,
                "path": str(path) if path else None,
                "size": path.stat().st_size if exists else None,
                "exists": exists,
            }
        )
    return items


def _size_text(size: Optional[int]) -> str:
    if size is None:
        return "아직 생성되지 않음"
    if size >= 1_048_576:
        return f"{size / 1_048_576:,.2f} MB"
    if size >= 1024:
        return f"{size / 1024:,.1f} KB"
    return f"{size:,} B"


# ── 본문 ──────────────────────────────────────────────────────────────
def _metric_cells(
    row: Dict[str, Any], context: Dict[str, Any]
) -> Tuple[str, str, str, str, str]:
    """(지표명, 당월, 전월, 변화율, 상태)"""
    catalog = context.get("metrics_catalog") or {}
    comparisons = _rows(context.get("comparison"))
    metric_id = str(row.get("metric_id"))
    spec = catalog.get(metric_id) or {}
    changed = _find(comparisons, metric_id)

    current = phrasing.fmt_value(row.get("value"), row.get("유형"), spec.get("지표명"), catalog, metric_id)
    previous = phrasing.fmt_value(
        changed.get("전월") if changed else None,
        row.get("유형"),
        spec.get("지표명"),
        catalog,
        metric_id,
    )
    change = phrasing.fmt_change(changed, catalog, metric_id) if changed else "전월 자료 없음 — 비교 불가"
    return (
        str(spec.get("지표명", metric_id)),
        current,
        previous,
        change,
        str(row.get("status") or "—"),
    )


#: 상태별 글자색. 화면·리포트와 같은 대응을 쓴다 (CLAUDE.md 7절).
_STATUS_COLORS = {
    calculate.OK: "#10b981",
    calculate.EXTENDED: "#f59e0b",
    calculate.LOW_SAMPLE: "#f59e0b",
    calculate.ERROR: "#f43f5e",
    calculate.OUT_OF_RANGE: "#64748b",
    calculate.NO_DATA: "#64748b",
    calculate.UNSUPPORTED: "#64748b",
}


def build_body_text(context: Dict[str, Any], report_md: str) -> str:
    """HTML을 못 읽는 클라이언트용 본문. 내용은 HTML과 같아야 한다."""
    catalog = context.get("metrics_catalog") or {}
    meta = catalog.get("_meta") or {}
    validation = context.get("validation") or {}
    comparisons = _rows(context.get("comparison"))
    period = context.get("기간")
    pending = manual_sections.pending_slots(report_md)

    lines = [
        f"CS 지표 리포트 — {_period_text(period)}",
        "",
        f"대상 기간: {_period_text(period)}",
        f"생성일시: {context.get('생성일시') or dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "[ 핵심 지표 ]",
    ]
    for row in headline_rows(context):
        name, current, previous, change, status = _metric_cells(row, context)
        lines.append(f"  - {name}: {current} (전월 {previous}, {change}) [{status}]")

    lines += ["", "[ 전월 대비 변동이 큰 지표 ]"]
    exceeded = _exceeded(comparisons, catalog)
    if exceeded:
        for changed in exceeded:
            metric_id = str(changed.get("metric_id"))
            lines.append(
                f"  - {phrasing.describe_change(changed, _threshold_of(metric_id, catalog), catalog)}"
            )
    else:
        lines.append("  - 지표별 임계값을 넘은 지표는 없습니다.")

    lines += [
        "",
        "[ 검증 요약 ]",
        f"  판정 {validation.get('전체판정', '알 수 없음')} · "
        f"차단 {validation.get('차단수', 0)}건 · 경고 {validation.get('경고수', 0)}건",
    ]

    headings = limit_headings(report_md)
    lines += ["", "[ 한계 ]"]
    if headings:
        lines += [f"  - {heading}" for heading in headings]
        lines.append("  자세한 내용은 첨부 리포트 7장을 보세요.")
    else:
        lines.append("  리포트에 한계 절이 없습니다.")

    if pending:
        lines += [
            "",
            "[ 미작성 ]",
            f"  {len(pending)}곳이 아직 비어 있습니다"
            f"({', '.join(manual_sections.slot_label(slot) for slot in pending)}). "
            "사람이 채워야 완성됩니다.",
        ]

    lines += ["", "[ 첨부 ]"]
    for item in attachment_list(context.get("run_dir")):
        mark = "" if item["exists"] else "  ← 없음"
        lines.append(
            f"  - {item['filename']} ({_size_text(item['size'])}){mark}"
        )
    lines.append("  ※ 이번 초안에는 파일이 실제로 첨부되지 않았습니다. 목록만 표시합니다.")

    lines += [
        "",
        "-" * 60,
        f"생성 도구: {TOOL_NAME}",
        f"카탈로그 버전: {meta.get('생성일시', '알 수 없음')} (지표 {meta.get('항목_개수', '?')}종)",
        "이 메일은 초안입니다. 실제 발송은 수행되지 않았습니다.",
    ]
    return "\n".join(lines)


def build_body_html(context: Dict[str, Any], report_md: str) -> str:
    """HTML 본문. **스타일을 인라인으로 넣는다** — 메일 클라이언트가 <style>을 지운다."""
    catalog = context.get("metrics_catalog") or {}
    meta = catalog.get("_meta") or {}
    validation = context.get("validation") or {}
    comparisons = _rows(context.get("comparison"))
    period = context.get("기간")
    pending = manual_sections.pending_slots(report_md)

    def esc(value: Any) -> str:
        return html.escape(str(value))

    font = "font-family:-apple-system,'Segoe UI','Malgun Gothic',sans-serif;"
    cell = "padding:6px 10px;border-bottom:1px solid #e5e7eb;"
    head = "padding:6px 10px;border-bottom:2px solid #9ca3af;text-align:left;white-space:nowrap;"

    parts = [
        f'<div style="{font}font-size:14px;line-height:1.6;color:#111827;max-width:{MAX_WIDTH_PX}px;">',
        f'<h2 style="margin:0 0 4px;font-size:19px;">CS 지표 리포트 — {esc(_period_text(period))}</h2>',
        f'<p style="margin:0 0 16px;color:#6b7280;font-size:13px;">'
        f"대상 기간 {esc(_period_text(period))} · 생성일시 "
        f"{esc(context.get('생성일시') or dt.datetime.now().astimezone().isoformat(timespec='seconds'))}</p>",
        '<h3 style="margin:18px 0 6px;font-size:15px;">핵심 지표</h3>',
        '<table style="border-collapse:collapse;width:100%;font-size:13px;">',
        f'<tr><th style="{head}">지표</th><th style="{head}">당월</th>'
        f'<th style="{head}">전월</th><th style="{head}">전월 대비</th>'
        f'<th style="{head}">상태</th></tr>',
    ]
    for row in headline_rows(context):
        name, current, previous, change, status = _metric_cells(row, context)
        color = _STATUS_COLORS.get(status, "#64748b")
        parts.append(
            f'<tr><td style="{cell}">{esc(name)}</td>'
            f'<td style="{cell}">{esc(current)}</td>'
            f'<td style="{cell}color:#6b7280;">{esc(previous)}</td>'
            f'<td style="{cell}">{esc(change)}</td>'
            f'<td style="{cell}color:{color};font-weight:600;">{esc(status)}</td></tr>'
        )
    parts.append("</table>")

    parts.append('<h3 style="margin:18px 0 6px;font-size:15px;">전월 대비 변동이 큰 지표</h3>')
    exceeded = _exceeded(comparisons, catalog)
    if exceeded:
        parts.append('<ul style="margin:0;padding-left:20px;">')
        for changed in exceeded:
            metric_id = str(changed.get("metric_id"))
            parts.append(
                "<li>"
                + esc(phrasing.describe_change(changed, _threshold_of(metric_id, catalog), catalog))
                + "</li>"
            )
        parts.append("</ul>")
    else:
        parts.append('<p style="margin:0;">지표별 임계값을 넘은 지표는 없습니다.</p>')

    verdict = str(validation.get("전체판정") or "알 수 없음")
    verdict_color = {"통과": "#10b981", "경고": "#f59e0b", "차단": "#f43f5e"}.get(verdict, "#64748b")
    parts += [
        '<h3 style="margin:18px 0 6px;font-size:15px;">검증 요약</h3>',
        f'<p style="margin:0;">판정 <b style="color:{verdict_color};">{esc(verdict)}</b> · '
        f"차단 {esc(validation.get('차단수', 0))}건 · 경고 {esc(validation.get('경고수', 0))}건</p>",
    ]

    headings = limit_headings(report_md)
    parts.append('<h3 style="margin:18px 0 6px;font-size:15px;">한계</h3>')
    if headings:
        parts.append('<ul style="margin:0;padding-left:20px;">')
        parts += [f"<li>{esc(heading)}</li>" for heading in headings]
        parts.append("</ul>")
        parts.append(
            '<p style="margin:6px 0 0;color:#6b7280;font-size:13px;">'
            "자세한 내용은 첨부 리포트 7장을 보세요.</p>"
        )
    else:
        parts.append('<p style="margin:0;">리포트에 한계 절이 없습니다.</p>')

    if pending:
        parts.append(
            '<p style="margin:16px 0 0;padding:10px 12px;border-radius:6px;'
            'background:#fef3c7;border:1px solid #f59e0b;font-size:13px;">'
            f"<b>미작성 {len(pending)}곳</b> — "
            f"{esc(', '.join(manual_sections.slot_label(slot) for slot in pending))}이 "
            "아직 비어 있습니다. "
            "사람이 채워야 완성됩니다.</p>"
        )

    parts += [
        '<h3 style="margin:18px 0 6px;font-size:15px;">첨부</h3>',
        '<table style="border-collapse:collapse;font-size:13px;">',
    ]
    for item in attachment_list(context.get("run_dir")):
        missing = not item["exists"]
        # rose. _STATUS_COLORS의 ERROR와 같은 값이다 (CLAUDE.md 7절 상태 색).
        name_style = f"{cell}color:#f43f5e;font-weight:700;" if missing else f"{cell}"
        size_style = f"{cell}color:#f43f5e;" if missing else f"{cell}color:#6b7280;"
        parts.append(
            f'<tr><td style="{name_style}">{esc(item["filename"])}</td>'
            f'<td style="{size_style}">{esc(_size_text(item["size"]))}</td></tr>'
        )
    parts += [
        "</table>",
        '<p style="margin:6px 0 0;color:#6b7280;font-size:13px;">'
        "※ 이번 초안에는 파일이 실제로 첨부되지 않았습니다. 목록만 표시합니다.</p>",
        '<hr style="margin:20px 0 10px;border:none;border-top:1px solid #e5e7eb;">',
        f'<p style="margin:0;color:#6b7280;font-size:12px;">생성 도구 {TOOL_NAME} · '
        f"카탈로그 버전 {esc(meta.get('생성일시', '알 수 없음'))} "
        f"(지표 {esc(meta.get('항목_개수', '?'))}종)<br>"
        "이 메일은 초안입니다. 실제 발송은 수행되지 않았습니다.</p>",
        "</div>",
    ]
    return "\n".join(parts)


# ── 조립 ──────────────────────────────────────────────────────────────
def build_email(run_context: Dict[str, Any], report_md: str = "") -> Dict[str, Any]:
    """이메일 초안을 만든다. **전송하지 않는다.**

    반환 형태는 8주차에 SMTP로 그대로 넘길 수 있게 맞춰 둔다.

        {subject, to, from, body_html, body_text, attachments}

    `attachments`는 이름·경로·크기만 담는다. 파일을 읽어 붙이는 일은 전송 단계의
    몫이고, 지금 붙이면 "보낼 준비가 됐다"가 "보냈다"로 오해될 수 있다.
    """
    context = dict(run_context or {})
    validation = context.get("validation") or {}
    pending = manual_sections.pending_slots(report_md)

    return {
        "subject": build_subject(context.get("기간"), validation, pending),
        # 실제 주소를 코드에 쓰지 않는다. config의 예시값을 그대로 쓴다 (CLAUDE.md 5-3).
        "to": list(config.EMAIL_TO),
        "from": config.EMAIL_FROM,
        "body_html": build_body_html(context, report_md),
        "body_text": build_body_text(context, report_md),
        "attachments": attachment_list(context.get("run_dir")),
    }
