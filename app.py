"""월간 리포트 자동화 — Streamlit 진입점.

CLAUDE.md 2절의 8단계 흐름과 승인 게이트를 담당한다. 1~8단계 전부 구현되어 있다
(IMPLEMENTED_THROUGH = 8). 실제 발송(SMTP)만 8주차 몫으로 남아 있으며,
그 자리는 `pipeline/send.py`에 표시만 해 두었다 — 이 파일은 그 함수를 부르지 않는다.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

import common
import config
from pipeline import (
    calculate,
    charts,
    compare,
    demo,
    email_draft,
    intake,
    manual_sections,
    phrasing,
    profile,
    report,
    runlog,
    validate,
)

st.set_page_config(page_title="월간 리포트 자동화", layout="wide")


# ── 8단계 정의 (CLAUDE.md 2절) ────────────────────────────────────────
@dataclass(frozen=True)
class Step:
    number: int
    title: str
    actor: str
    output: str
    gate: bool = False


STEPS: Tuple[Step, ...] = (
    Step(1, "데이터 파일 투입", "사용자", "업로드된 원본 파일"),
    Step(2, "스키마 점검 → 지표 계산", "시스템", "계산 결과 표 (계산 가능 / 불가 + 이유)", gate=True),
    Step(3, "검증 실행", "시스템", "검증 결과 (통과 / 경고 / 차단)"),
    Step(4, "대시보드 렌더링", "시스템", "화면"),
    Step(5, "내용·검증 결과 확인", "사용자", "확인 기록", gate=True),
    Step(6, "리포트 생성", "시스템", "리포트 문서 (대시보드와 별개, PDF 포함)"),
    Step(7, "이메일 초안 생성", "시스템", "제목·수신자·본문·첨부 목록"),
    Step(8, "발송 확정", "사용자", "확정된 최종 파일", gate=True),
)

#: 여기까지만 코드가 있다. 나머지는 자리만 잡아 둔 상태다.
IMPLEMENTED_THROUGH = 8

DONE, ACTIVE, READY, WAITING = "완료", "진행중", "준비됨", "대기"

#: 진행 상태는 지표 status와 다른 축이라 emerald·slate 외에는 진행 전용 색을 쓴다.
#: 진행중과 준비됨은 둘 다 "지금 차례"라서 같은 색을 쓰고 라벨로만 구분한다.
PROGRESS_COLORS = {
    DONE: common.COLOR_OK,
    ACTIVE: common.COLOR_PROGRESS,
    READY: common.COLOR_PROGRESS,
    WAITING: common.COLOR_NODATA,
}

#: 지표 판정 상태 → 화면 색 (CLAUDE.md 7절)
METRIC_STATUS_COLORS = {
    profile.CALCULABLE: common.COLOR_OK,
    profile.NEEDS_RANGE: common.COLOR_WARN,
    profile.BLOCKED: common.COLOR_BLOCK,
    profile.UNRELATED: common.COLOR_NODATA,
    profile.UNDECIDABLE: common.COLOR_BLOCK,
}

#: 표에 쌓는 순서. 손댈 것이 있는 상태를 위로 올린다.
METRIC_STATUS_ORDER = (
    profile.UNDECIDABLE,
    profile.BLOCKED,
    profile.NEEDS_RANGE,
    profile.CALCULABLE,
)

#: 계산 결과 status → 화면 색 (CLAUDE.md 7절)
CALC_STATUS_COLORS = {
    calculate.OK: common.COLOR_OK,
    calculate.EXTENDED: common.COLOR_WARN,
    calculate.LOW_SAMPLE: common.COLOR_WARN,
    calculate.ERROR: common.COLOR_BLOCK,
    calculate.OUT_OF_RANGE: common.COLOR_NODATA,
    calculate.NO_DATA: common.COLOR_NODATA,
    calculate.UNSUPPORTED: common.COLOR_NODATA,
}

#: 손댈 것이 있는 상태를 위로 올린다.
CALC_STATUS_ORDER = (
    calculate.ERROR,
    calculate.LOW_SAMPLE,
    calculate.NO_DATA,
    calculate.UNSUPPORTED,
    calculate.OUT_OF_RANGE,
    calculate.EXTENDED,
    calculate.OK,
)

#: 검증 판정 → 화면 색 (CLAUDE.md 7절)
VALIDATION_COLORS = {
    validate.PASS: common.COLOR_OK,
    validate.WARN: common.COLOR_WARN,
    validate.BLOCK: common.COLOR_BLOCK,
}

AUTH_COMMAND = "gcloud auth application-default login"
_AUTH_HINTS = ("credential", "unauthorized", "permission", "reauth", "invalid_grant", "401", "403")
_AUTH_TYPES = {"DefaultCredentialsError", "RefreshError", "Unauthorized", "Forbidden"}



# ── 세션 상태 ─────────────────────────────────────────────────────────
def init_state() -> None:
    st.session_state.setdefault("step", 1)  # 지금 진행 중인 단계
    st.session_state.setdefault("upload_key", None)
    st.session_state.setdefault("upload_error", None)
    st.session_state.setdefault("upload_stamp", None)
    st.session_state.setdefault("frame", None)
    st.session_state.setdefault("raw", None)  # 원본 바이트. run 폴더에 그대로 복사한다
    st.session_state.setdefault("intake", None)
    st.session_state.setdefault("judgement_key", None)
    st.session_state.setdefault("table_judgement", None)
    st.session_state.setdefault("data_profile", None)
    st.session_state.setdefault("metric_judgement", None)
    st.session_state.setdefault("run_dir", None)  # 확정된 실행 폴더
    st.session_state.setdefault("last_run_dir", None)  # 취소된 실행 폴더 (기록은 남는다)
    st.session_state.setdefault("gate_error", None)
    st.session_state.setdefault("judgement_reset", False)
    st.session_state.setdefault("range_approved", False)
    st.session_state.setdefault("calc_key", None)
    st.session_state.setdefault("metrics_frame", None)
    st.session_state.setdefault("calc_error", None)
    st.session_state.setdefault("staging_table", None)
    st.session_state.setdefault("comparison_frame", None)
    st.session_state.setdefault("prev_period", None)
    st.session_state.setdefault("compare_error", None)
    st.session_state.setdefault("validation", None)
    st.session_state.setdefault("validation_error", None)
    st.session_state.setdefault("source_freshness", None)
    st.session_state.setdefault("review_error", None)
    st.session_state.setdefault("report_markdown", None)
    st.session_state.setdefault("report_error", None)
    st.session_state.setdefault("pdf_error", None)
    st.session_state.setdefault("manual_warnings", [])
    st.session_state.setdefault("manual_outcome", None)
    st.session_state.setdefault("email_draft", None)
    st.session_state.setdefault("email_error", None)
    st.session_state.setdefault("approval", None)
    st.session_state.setdefault("approval_error", None)
    st.session_state.setdefault("loaded_run", False)
    st.session_state.setdefault("load_run_error", None)


def discard_from_step1() -> None:
    """1단계 산출물과 그 뒤 단계 결과를 모두 폐기한다.

    파일이 바뀌면 이후 결과는 다른 파일에서 나온 값이다. 남겨 두면 어느 파일로
    계산한 숫자인지 알 수 없게 된다 (CLAUDE.md 2절 — 되돌아가면 다시 계산한다).
    """
    st.session_state.step = 1
    st.session_state.upload_key = None
    st.session_state.upload_error = None
    st.session_state.upload_stamp = None
    st.session_state.frame = None
    st.session_state.raw = None
    st.session_state.intake = None
    st.session_state.loaded_run = False
    st.session_state.load_run_error = None
    discard_judgement()
    discard_confirmation()


def discard_judgement() -> None:
    st.session_state.judgement_key = None
    st.session_state.table_judgement = None
    st.session_state.data_profile = None
    st.session_state.metric_judgement = None


def discard_confirmation() -> None:
    """확정 상태만 되돌린다. **run 폴더는 지우지 않는다** — 기록은 남긴다."""
    if st.session_state.get("run_dir"):
        st.session_state.last_run_dir = st.session_state.run_dir
    st.session_state.run_dir = None
    st.session_state.gate_error = None
    st.session_state.range_approved = False
    # 계산 결과도 폐기한다. 확정되지 않은 판정의 계산 결과가 화면에 남으면 안 된다.
    st.session_state.calc_key = None
    st.session_state.metrics_frame = None
    st.session_state.calc_error = None
    st.session_state.staging_table = None
    st.session_state.comparison_frame = None
    st.session_state.prev_period = None
    st.session_state.compare_error = None
    st.session_state.validation = None
    st.session_state.validation_error = None
    st.session_state.source_freshness = None
    st.session_state.review_error = None
    st.session_state.report_markdown = None
    st.session_state.report_error = None
    st.session_state.pdf_error = None
    st.session_state.manual_warnings = []
    st.session_state.manual_outcome = None
    st.session_state.email_draft = None
    st.session_state.email_error = None
    st.session_state.approval = None
    st.session_state.approval_error = None
    st.session_state.pop("manual_text", None)  # 다음 렌더에서 파일을 다시 읽는다
    for key in list(st.session_state.keys()):
        if str(key).startswith("approve_"):
            st.session_state[key] = False
    for key in list(st.session_state.keys()):
        if str(key).startswith("review_"):
            st.session_state[key] = False
    # 다음 판정에서 승인을 다시 받아야 한다. 이전 체크를 물려주지 않는다.
    for key in ("range_ok", "partial_ok"):
        if key in st.session_state:
            st.session_state[key] = False


def step_status(number: int) -> str:
    current = st.session_state.step
    if number < current:
        return DONE
    if number == current:
        # 게이트를 통과해 차례가 됐지만 아직 코드가 없는 단계는 '준비됨'이다.
        # '진행중'이라고 쓰면 뭔가 돌아가는 중으로 읽힌다.
        return ACTIVE if number <= IMPLEMENTED_THROUGH else READY
    return WAITING


# ── 1단계 처리 ────────────────────────────────────────────────────────
def upload_key(uploaded: Any) -> str:
    return str(getattr(uploaded, "file_id", None) or f"{uploaded.name}::{uploaded.size}")


def on_upload_change() -> None:
    """업로드 위젯이 바뀔 때 실행된다.

    콜백에서 상태를 확정해야 사이드바 진행 표시와 본문이 같은 렌더에서 일치한다.
    본문에서 처리하면 사이드바가 한 박자 늦게 갱신된다.
    """
    uploaded = st.session_state.get("uploader")

    if uploaded is None:  # 파일을 지웠다
        discard_from_step1()
        return

    key = upload_key(uploaded)
    if key == st.session_state.upload_key:
        return  # 같은 파일이면 다시 읽지 않는다

    discard_from_step1()
    st.session_state.upload_key = key  # 실패해도 기록해 매 렌더마다 재파싱하지 않는다

    raw = uploaded.getvalue()
    try:
        frame, encoding = intake.read_csv_bytes(raw)
    except intake.IntakeError as exc:
        st.session_state.upload_error = str(exc)
        return
    except Exception as exc:  # 예상 못 한 형식 — 화면에 이유를 남기고 멈춘다
        st.session_state.upload_error = f"파일을 읽는 중 오류가 발생했습니다: {exc}"
        return

    st.session_state.frame = frame
    st.session_state.raw = raw
    # 업로드 시각은 지금만 알 수 있다. 확정 시점에 찍으면 사람이 게이트에서
    # 머문 시간까지 투입 시각에 섞인다.
    st.session_state.upload_stamp = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    st.session_state.intake = intake.describe(
        frame, uploaded.name, uploaded.size, encoding, raw
    )
    st.session_state.step = 2  # 1단계 완료


# ── 2단계 처리 ────────────────────────────────────────────────────────
def ensure_judgement(metrics_catalog: dict, schema_catalog: dict) -> None:
    """판정을 한 번만 계산하고 세션에 담아 둔다.

    입력이 같으면 결과도 같아야 하므로(CLAUDE.md 5-5), 캐시 키를 **업로드 파일 +
    두 카탈로그의 생성일시**로 잡는다. 카탈로그를 다시 export하면 키가 달라져
    자동으로 다시 판정한다 — 낡은 판정을 화면에 남겨 두지 않기 위해서다.
    """
    frame = st.session_state.frame
    if frame is None:
        discard_judgement()
        return

    key = (
        st.session_state.upload_key,
        catalog_stamp(metrics_catalog),
        catalog_stamp(schema_catalog),
    )
    if st.session_state.judgement_key == key:
        return

    # 카탈로그가 바뀌어 다시 판정하는 경우, 이미 받은 승인은 다른 판정에 대한 것이다.
    # 2단계로 되돌리고 이후 결과를 폐기한다 (CLAUDE.md 2절).
    if (
        st.session_state.judgement_key is not None
        and st.session_state.step > 2
        and not st.session_state.loaded_run
    ):
        st.session_state.step = 2
        st.session_state.judgement_reset = True
        discard_confirmation()

    table_judgement = profile.judge_table(frame, schema_catalog)
    table_name = table_judgement["테이블명"]
    table_info = schema_catalog.get(table_name) if table_name else None
    data_profile = profile.profile_data(frame, table_info)

    st.session_state.table_judgement = table_judgement
    st.session_state.data_profile = data_profile
    st.session_state.metric_judgement = profile.judge_metrics(
        table_name,
        data_profile["기간"],
        metrics_catalog,
        missing_columns=table_judgement["누락컬럼"],
    )
    st.session_state.judgement_key = key


# ── 카탈로그 ──────────────────────────────────────────────────────────
def load_catalog(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """(카탈로그, 오류 사유). 캐시하지 않는다 — 다시 export한 결과가 바로 보여야 한다."""
    if not path.exists():
        return None, "파일이 없습니다"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"읽지 못했습니다 ({exc})"


# 카탈로그 메타를 읽는 규칙은 runlog가 갖는다. 화면이 자기 버전을 두면 사이드바에
# 찍히는 지표 개수와 실행 기록의 지표 개수가 달라질 수 있다.
catalog_stamp = runlog.catalog_stamp
catalog_count = runlog.catalog_count


# ── 데모 모드 배너 ────────────────────────────────────────────────────
def render_demo_banner() -> None:
    """데모 모드일 때 화면 맨 위에 세우는 경고. 끌 수 없다.

    **접을 수 있게 만들지 않는다.** 이 배너의 목적은 화면의 숫자가 가짜라는 것을
    한순간도 잊지 않게 하는 것 하나뿐이라, 접히는 순간 목적이 사라진다.
    색은 차단(rose)을 쓴다 — 경고(amber)로 두면 "값은 맞는데 주의하라"로 읽힌다
    (CLAUDE.md 7절 상태 색 대응).
    """
    if not demo.is_enabled():
        return

    color = common.COLOR_BLOCK
    st.markdown(
        f'<div style="padding:.85rem 1rem;border-radius:10px;'
        f'border:1px solid {color};border-left:6px solid {color};'
        f'background:{color}14;margin-bottom:1rem;">'
        f'<div style="font-weight:800;color:{color};font-size:1.02rem;">'
        f'{common.esc(demo.BANNER_TITLE)}</div>'
        f'<div style="margin-top:.35rem;line-height:1.6;">'
        f'{common.esc(demo.BANNER_BODY)}</div>'
        f'<div style="margin-top:.45rem;font-size:.86rem;opacity:.85;">'
        f'실제로 계산한 결과를 보려면 왼쪽 <b>기존 실행 불러오기</b>에서 '
        f'기록된 실행을 선택하세요 — 그 화면의 숫자는 진짜입니다.</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_sidebar(
    metrics_catalog: Optional[dict],
    metrics_error: Optional[str],
    schema_catalog: Optional[dict],
    schema_error: Optional[str],
) -> None:
    with st.sidebar:
        if demo.is_enabled():
            # 본문 배너는 위로 스크롤해야 보인다. 사이드바는 따라다니므로 여기에도 박는다.
            st.markdown(
                f'<div style="padding:.5rem .7rem;border-radius:8px;'
                f'border:1px solid {common.COLOR_BLOCK};'
                f'background:{common.COLOR_BLOCK}1a;margin-bottom:.8rem;'
                f'font-weight:700;color:{common.COLOR_BLOCK};font-size:.9rem;">'
                f'데모 모드 · 숫자는 가짜</div>',
                unsafe_allow_html=True,
            )

        st.markdown("### 카탈로그")

        if metrics_catalog is None or schema_catalog is None:
            st.warning("카탈로그가 없습니다. export를 먼저 실행하세요.")
            if metrics_catalog is None:
                st.caption(f"· metrics_catalog.json — {metrics_error}")
            if schema_catalog is None:
                st.caption(f"· schema_catalog.json — {schema_error}")
            st.code("python catalog/export_catalog.py", language="bash")
        else:
            left, right = st.columns(2)
            left.metric("지표", f"{catalog_count(metrics_catalog)}개")
            right.metric("테이블", f"{catalog_count(schema_catalog)}개")
            st.caption(f"카탈로그 갱신 · {common.format_timestamp(catalog_stamp(metrics_catalog))}")
            if catalog_stamp(metrics_catalog) != catalog_stamp(schema_catalog):
                st.warning("두 카탈로그의 생성일시가 다릅니다. export를 다시 실행하세요.")

        st.divider()
        st.markdown("### 기존 실행 불러오기")
        runs = list_existing_runs()
        if not runs:
            st.caption("outputs/에 실행 폴더가 없습니다.")
        else:
            options = [RUN_PICKER_PLACEHOLDER] + [str(path) for path in runs]
            st.selectbox(
                "run 폴더",
                options,
                key="run_picker",
                format_func=lambda value: (
                    RUN_PICKER_PLACEHOLDER
                    if value == RUN_PICKER_PLACEHOLDER
                    else run_pick_label(Path(value))
                ),
                on_change=on_pick_run,
                args=(metrics_catalog, schema_catalog),
                label_visibility="collapsed",
            )
            st.caption(
                "CLI(run_pipeline.py)로 미리 만든 초안을 화면에서 확인·확정할 때 씁니다."
            )
        if st.session_state.load_run_error:
            st.error(st.session_state.load_run_error)
        if st.session_state.loaded_run and st.session_state.run_dir:
            confirmed = bool(st.session_state.approval)
            st.success(
                f"불러옴 · {Path(st.session_state.run_dir).name}"
                + (" (확정 완료)" if confirmed else "")
            )

        st.divider()
        st.markdown("### 진행 상태")
        for step in STEPS:
            status = step_status(step.number)
            label = f"{status} ✓" if status == DONE else status
            muted = "" if status != WAITING else f"color:{common.COLOR_NODATA};"
            st.markdown(
                '<div style="display:flex;justify-content:space-between;align-items:center;'
                'gap:.5rem;padding:.15rem 0;">'
                f'<span style="font-size:.88rem;{muted}">'
                f"{step.number}. {html.escape(step.title)}</span>"
                f"{common.tag(label, PROGRESS_COLORS[status])}</div>",
                unsafe_allow_html=True,
            )
        st.caption("게이트는 2·5·8단계에 있습니다. 승인 없이 다음 단계로 넘어가지 않습니다.")


# ── 공통 렌더 ─────────────────────────────────────────────────────────
def render_step_header(step: Step) -> None:
    """큰 굵은 제목 + 상태 배지 하나만. 박스 테두리·배지 여러 개·산출물 캡션은 안 쓴다.

    강사 화면과 시각적으로 맞추기 위한 단순화다 — 정보가 줄어든 것이지 판정 로직이
    바뀐 건 아니다(step_status는 그대로 쓴다).
    """
    status = step_status(step.number)
    st.markdown(
        f'<div style="font-size:2rem;font-weight:700;margin-top:1.2rem;margin-bottom:.5rem;'
        'line-height:1.3;">'
        f"{step.number}단계 — {html.escape(step.title)}</div>",
        unsafe_allow_html=True,
    )
    common.render_badges(
        common.tag(f"{status} ✓" if status == DONE else status, PROGRESS_COLORS[status])
    )


def section_title(text: str) -> None:
    st.markdown(
        f'<div style="font-weight:700;margin:.6rem 0 .35rem;">{common.esc(text)}</div>',
        unsafe_allow_html=True,
    )


def column_list(label: str, columns: Sequence[str], status: str, note: str) -> None:
    common.render_badges(
        common.status_badge(status, f"{label} {len(columns)}개"),
        f'<span style="font-size:.85rem;color:{common.COLOR_NODATA};">{common.esc(note)}</span>',
    )
    st.code(", ".join(columns), language=None)


# ── 1단계 화면 ────────────────────────────────────────────────────────
def render_intake(info: Dict[str, Any], frame: Any) -> None:
    st.markdown(f"**{info['파일명']}**")

    columns = st.columns(3)
    columns[0].metric("행수", f"{info['행수']:,}")
    columns[1].metric("컬럼 수", f"{info['컬럼수']:,}")
    columns[2].metric("크기", common.format_kb(info["크기_bytes"]))
    st.caption(f"{info['인코딩']}로 읽었습니다.")

    duplicated = info.get("중복컬럼") or []
    if duplicated:
        st.warning(
            "파일 헤더에 같은 컬럼명이 두 번 이상 있습니다: "
            + ", ".join(duplicated)
            + " — 뒤쪽이 `.1`이 붙은 이름으로 읽혔습니다. "
            "2단계 스키마 판정이 어긋나므로 원본 파일을 확인하세요."
        )

    st.markdown("**컬럼 목록**")
    # 코드 블록으로 낸다. 마크다운으로 쓰면 snake_case의 밑줄이 기울임으로 먹힌다.
    st.code(", ".join(info["컬럼"]), language=None)

    st.markdown("**앞 5행 미리보기**")
    st.dataframe(frame.head(5), width="stretch")


def render_step1() -> None:
    with st.container():
        render_step_header(STEPS[0])
        st.file_uploader(
            "CSV 파일을 올립니다",
            type=["csv"],
            key="uploader",
            on_change=on_upload_change,
            help="알려진 스키마의 새 기간 데이터만 받습니다 (CLAUDE.md 5-1).",
        )

        if st.session_state.upload_error:
            st.error(st.session_state.upload_error)

        if st.session_state.loaded_run and st.session_state.run_dir:
            st.caption(
                f"사이드바에서 불러온 실행입니다 — {Path(st.session_state.run_dir).name}. "
                "위 업로드 칸은 새 실행을 시작할 때만 씁니다."
            )

        if st.session_state.intake:
            render_intake(st.session_state.intake, st.session_state.frame)


# ── 2단계 화면 ────────────────────────────────────────────────────────
def render_table_judgement(judgement: Dict[str, Any]) -> None:
    section_title("① 테이블 판정")

    passed = judgement["판정가능"]
    name = judgement["테이블명"] if passed else judgement.get("후보테이블명")
    accent = common.COLOR_OK if passed else common.COLOR_BLOCK

    badges = [
        f'<span style="font-size:1.05rem;font-weight:700;">{common.esc(name or "판정 불가")}</span>',
        common.status_badge(common.OK if passed else common.BLOCK, "통과" if passed else "판정 불가"),
        common.tag(
            f"일치율 {judgement['일치율']:.0%} ({judgement['일치컬럼수']}/{judgement['카탈로그컬럼수']})",
            accent,
        ),
    ]
    if judgement.get("테이블명_추정"):
        badges.append(common.tag("테이블명 추정", common.COLOR_NODATA))
    common.render_badges(*badges)
    st.caption(judgement["이유"])

    # 판정에 실패했을 때는 이름 유도 방식이 문제가 아니므로 안내를 띄우지 않는다.
    if passed and judgement.get("테이블명_추정"):
        st.caption(
            "노트명에서 유도한 테이블명입니다. 확정하려면 위키 스키마 노트에 "
            "`bq_table`을 적고 카탈로그를 다시 export하세요."
        )

    if judgement["누락컬럼"]:
        column_list(
            "누락 컬럼",
            judgement["누락컬럼"],
            common.WARN,
            "카탈로그에는 있는데 이 파일에 없습니다.",
        )
    if judgement["추가컬럼"]:
        column_list(
            "추가 컬럼",
            judgement["추가컬럼"],
            common.NODATA,
            "카탈로그에 없는 컬럼입니다. 계산에는 쓰이지 않습니다.",
        )

    if not passed:
        st.error(
            "카탈로그의 어느 테이블과도 충분히 맞지 않아 2단계에서 멈춥니다. "
            "추측해서 통과시키지 않습니다."
        )
        common.render_table(
            ["후보 테이블", "일치율", "일치/전체", "누락 컬럼"],
            [
                [
                    common.mono(item["테이블명"]),
                    f"{item['일치율']:.0%}",
                    f"{item['일치컬럼수']}/{item['카탈로그컬럼수']}",
                    common.esc(", ".join(item["누락컬럼"][:6]) or "없음"),
                ]
                for item in judgement["후보"]
            ],
        )


def render_data_profile(prof: Dict[str, Any], judgement: Dict[str, Any]) -> None:
    section_title("② 데이터 점검")
    rows: List[List[str]] = []

    rows.append(["행수", f"{prof['행수']:,}행", common.status_badge(common.OK), ""])

    missing_columns = judgement["누락컬럼"]
    rows.append(
        [
            "컬럼 수",
            f"{prof['컬럼수']}개",
            common.status_badge(common.WARN if missing_columns else common.OK),
            common.esc(
                f"카탈로그 기준 누락 {len(missing_columns)}개"
                if missing_columns
                else "카탈로그 컬럼을 모두 갖췄습니다"
            ),
        ]
    )

    period = prof["기간"]
    if period["컬럼"] and period["최소"]:
        rows.append(
            [
                "기간",
                f"{common.mono(period['컬럼'])} {common.esc(period['최소'])} ~ {common.esc(period['최대'])}",
                common.status_badge(common.OK),
                "파일 안의 값으로 판정했습니다. 현재 시각을 쓰지 않습니다.",
            ]
        )
    else:
        rows.append(
            [
                "기간",
                "기간 컬럼을 찾지 못했습니다",
                common.status_badge(common.WARN),
                "유효구간과 대조할 수 없어 지표가 모두 판정 불가가 됩니다.",
            ]
        )

    missing = prof["결측"]
    rows.append(
        [
            "결측",
            f"{len(missing)}개 컬럼" if missing else "없음",
            common.status_badge(common.WARN if missing else common.OK),
            common.esc(", ".join(f"{name} {count:,}건" for name, count in missing.items()))
            if missing
            else "",
        ]
    )

    grains = prof["그레인후보"]
    unique = next((g for g in grains if g["유일"]), None)
    if unique:
        rows.append(
            [
                "그레인 중복",
                " + ".join(common.mono(key) for key in unique["키"]),
                common.status_badge(common.OK),
                common.esc(f"{unique['출처']} 기준으로 중복 없음"),
            ]
        )
    elif grains:
        first = grains[0]
        rows.append(
            [
                "그레인 중복",
                " + ".join(common.mono(key) for key in first["키"]),
                common.status_badge(common.WARN),
                common.esc(f"중복 {first['중복행수']:,}행 ({first['출처']})"),
            ]
        )
    else:
        rows.append(
            [
                "그레인 중복",
                "후보 없음",
                common.status_badge(common.NODATA),
                "키를 추정할 근거가 없습니다.",
            ]
        )

    common.render_table(["항목", "값", "상태", "비고"], rows)


def metric_rows(judged: Sequence[Dict[str, Any]]) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in judged:
        reason = common.esc(row["이유"])
        if row.get("부분갱신") and row.get("다른원천"):
            reason += common.muted(f"{', '.join(row['다른원천'])}는 갱신되지 않음")
        rows.append(
            [
                common.esc(row["지표명"]),
                common.mono(row["metric_id"]),
                common.tag(row["상태"], METRIC_STATUS_COLORS.get(row["상태"], common.COLOR_NODATA)),
                reason,
                " ".join(common.mono(source) for source in row["원천"]) or "—",
            ]
        )
    return rows


def render_metric_judgement(judged: Sequence[Dict[str, Any]], judgement: Dict[str, Any]) -> None:
    section_title("③ 지표 판정")
    headers = ["지표명", "metric_id", "상태", "이유", "원천"]

    if not judgement["판정가능"]:
        st.info("테이블을 판정하지 못해 어느 지표가 대상인지 가릴 수 없습니다. ①을 먼저 해결하세요.")
    else:
        related = [row for row in judged if row["상태"] != profile.UNRELATED]
        related.sort(
            key=lambda row: (
                METRIC_STATUS_ORDER.index(row["상태"])
                if row["상태"] in METRIC_STATUS_ORDER
                else len(METRIC_STATUS_ORDER),
                row["metric_id"],
            )
        )
        if related:
            common.render_table(headers, metric_rows(related))
        else:
            st.info("이 파일을 원천으로 쓰는 지표가 없습니다.")

        unrelated = [row for row in judged if row["상태"] == profile.UNRELATED]
        if unrelated:
            with st.expander(f"이 파일과 무관한 지표 {len(unrelated)}종", expanded=False):
                common.render_table(headers, metric_rows(unrelated))


def render_summary(judged: Sequence[Dict[str, Any]], metrics_catalog: dict) -> None:
    summary = profile.summarize_metrics(judged)
    target = summary[profile.CALCULABLE] + summary[profile.NEEDS_RANGE]

    text = (
        f"계산 대상 {target}종 "
        f"(그중 유효구간 확장 필요 {summary[profile.NEEDS_RANGE]}종), "
        f"무관 {summary[profile.UNRELATED]}종"
    )
    if summary[profile.UNDECIDABLE]:
        text += f", 판정 불가 {summary[profile.UNDECIDABLE]}종"

    st.markdown(
        f'<div style="margin-top:.8rem;padding:.6rem .8rem;border-radius:8px;'
        f"border:1px solid rgba(128,128,128,0.3);font-weight:700;\">{common.esc(text)}"
        f'<div style="font-weight:400;font-size:.8rem;color:{common.COLOR_NODATA};'
        f'margin-top:.25rem;">지표 {catalog_count(metrics_catalog)}종 · 카탈로그 갱신 '
        f'{common.esc(common.format_timestamp(catalog_stamp(metrics_catalog)))} 기준</div></div>',
        unsafe_allow_html=True,
    )


def render_step2(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    with st.container():
        render_step_header(STEPS[1])

        if st.session_state.step < 2:
            st.caption("1단계가 끝나면 열립니다.")
            return

        if metrics_catalog is None or schema_catalog is None:
            st.error(
                "카탈로그가 없어 판정할 수 없습니다. "
                "`python catalog/export_catalog.py`를 먼저 실행하세요."
            )
            return

        ensure_judgement(metrics_catalog, schema_catalog)
        judgement = st.session_state.table_judgement
        if judgement is None:
            return

        render_table_judgement(judgement)
        st.divider()
        render_data_profile(st.session_state.data_profile, judgement)
        st.divider()
        render_metric_judgement(st.session_state.metric_judgement, judgement)
        render_summary(st.session_state.metric_judgement, metrics_catalog)


# ── 첫 승인 게이트 (2단계 뒤) ─────────────────────────────────────────
def gate_blockers(
    judgement: Optional[Dict[str, Any]], judged: Optional[Sequence[Dict[str, Any]]]
) -> List[str]:
    """확정을 막는 사유. 비어 있으면 진행할 수 있다.

    **테이블 미확정**은 당연히 차단이다. 어느 원천인지 모르면 SQL을 조립할 수 없다.

    **누락 컬럼은 여기서 일괄로 막지 않는다.** 어느 지표가 그 컬럼을 쓰는지는
    judge_metrics()가 지표 단위로 가려 "계산불가"로 표시한다. 컬럼 하나가 빠졌다고
    그 컬럼을 쓰지 않는 지표까지 막으면 멀쩡한 계산을 통째로 잃는다.

    **계산할 수 있는 지표가 하나도 없으면 막는다.** 계산할 것이 없는데 실행 폴더만
    만들면 빈 리포트가 남는다. 남은 게 없는 이유(누락 컬럼 때문인지, 애초에 무관한
    파일인지)를 구분해 적는다.
    """
    blockers: List[str] = []

    if not judgement or not judgement.get("판정가능"):
        blockers.append(
            "테이블을 판정하지 못했습니다. 카탈로그의 어느 원천인지 확정되어야 계산할 수 있습니다."
        )
        return blockers

    summary = profile.summarize_metrics(judged or [])
    if summary[profile.CALCULABLE] + summary[profile.NEEDS_RANGE] > 0:
        return blockers

    if summary[profile.BLOCKED]:
        blockers.append(
            "누락 컬럼 때문에 계산할 수 있는 지표가 하나도 없습니다. 빠진 컬럼: "
            + ", ".join(judgement["누락컬럼"])
            + " — 원본 파일에 컬럼을 채워 다시 올리세요."
        )
    else:
        blockers.append("이 파일을 원천으로 쓰는 지표가 없습니다. 계산할 대상이 없습니다.")
    return blockers


def unique_run_dir(now: datetime) -> Path:
    """실행 폴더. 같은 분에 두 번 확정해도 이전 실행을 덮어쓰지 않는다 (CLAUDE.md 8절)."""
    base = config.OUTPUTS_DIR / now.strftime("run_%Y%m%d_%H%M")
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    return candidate


def build_run_log(
    now: datetime,
    run_name: str,
    saved_name: str,
    range_approved: bool,
    metrics_catalog: dict,
    schema_catalog: dict,
    insights_catalog: Optional[dict] = None,
) -> Dict[str, Any]:
    """실행 기록의 0~2단계와 게이트 1.

    **구역의 모양은 runlog가 정한다.** 화면과 명령줄(run_pipeline.py)이 같은 함수를
    써야, 어느 쪽으로 돌렸는지에 따라 기록이 달라지지 않는다.
    """
    judged = st.session_state.metric_judgement or []
    needs_range = [row for row in judged if row["상태"] == profile.NEEDS_RANGE]
    stamp = now.isoformat(timespec="seconds")

    return {
        runlog.RUN: runlog.run_section(run_name, stamp, "화면"),
        runlog.CATALOG: runlog.catalog_section(
            metrics_catalog, schema_catalog, insights_catalog
        ),
        runlog.INTAKE: runlog.intake_section(
            st.session_state.intake, saved_name, st.session_state.get("upload_stamp")
        ),
        runlog.JUDGE: runlog.judge_section(
            st.session_state.table_judgement,
            st.session_state.data_profile,
            judged,
            bool(st.session_state.get("partial_ok")),
        ),
        runlog.GATE1: runlog.gate1_section(
            stamp, needs_range, range_approved, "화면 체크박스"
        ),
    }


def confirm_run(metrics_catalog: dict, schema_catalog: dict) -> None:
    """확정 — 실행 폴더를 만들고 원본과 기록을 남긴 뒤 3단계를 연다."""
    st.session_state.gate_error = None
    info = st.session_state.intake
    raw = st.session_state.raw
    if not info or raw is None:
        st.session_state.gate_error = "업로드 파일이 없어 확정할 수 없습니다."
        return

    now = datetime.now().astimezone()
    run_dir = unique_run_dir(now)
    saved_name = Path(str(info["파일명"])).name  # 경로가 섞여 들어와도 폴더 밖으로 나가지 않게

    try:
        run_dir.mkdir(parents=True)
        (run_dir / saved_name).write_bytes(raw)  # 원본 그대로 복사해야 재현할 수 있다
        insights_catalog = load_catalog(config.INSIGHTS_CATALOG_PATH)[0] or {}
        log = build_run_log(
            now,
            run_dir.name,
            saved_name,
            bool(st.session_state.get("range_ok")),
            metrics_catalog,
            schema_catalog,
            insights_catalog,
        )
        runlog.save(run_dir, log)
    except OSError as exc:
        st.session_state.gate_error = f"실행 폴더를 만들지 못했습니다: {exc}"
        return

    st.session_state.run_dir = str(run_dir)
    st.session_state.last_run_dir = None
    # 체크박스는 확정 후 화면에서 사라지므로, 승인 여부를 여기서 붙잡아 둔다.
    st.session_state.range_approved = bool(st.session_state.get("range_ok"))
    st.session_state.step = 3  # 3단계 준비됨


def cancel_run() -> None:
    """확정 취소 — 이후 단계를 다시 잠근다. 만들어진 폴더는 지우지 않는다."""
    run_dir = st.session_state.get("run_dir")
    if run_dir:
        try:
            runlog.amend(
                run_dir,
                runlog.RUN,
                상태="취소됨",
                취소시각=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        except OSError as exc:
            # 기록을 못 고쳐도 화면 흐름은 되돌린다. 대신 왜 못 고쳤는지 남긴다.
            st.session_state.gate_error = f"실행 기록에 취소를 적지 못했습니다: {exc}"

    discard_confirmation()
    st.session_state.step = 2


# ── 기존 실행 불러오기 ────────────────────────────────────────────────
#: 이 파일들은 원본 업로드가 아니라 파이프라인이 만든 산출물이다. run 폴더에서
#: 원본 CSV를 찾을 때 이 이름들은 후보에서 뺀다.
KNOWN_OUTPUT_FILES = frozenset(
    {
        "report.md",
        "report.pdf",
        "metrics.csv",
        "comparison.csv",
        "validation.json",
        "run_log.json",
        "email.html",
        "email.txt",
        "email_meta.json",
        "email_final.html",
        runlog.FILENAME,
        "APPROVED",
    }
)


def list_existing_runs() -> List[Path]:
    """`outputs/run_*` 목록. 이름이 `run_YYYYMMDD_HHMM`이라 최신순 정렬이 문자열 정렬과 같다."""
    if not config.OUTPUTS_DIR.exists():
        return []
    return sorted(
        (path for path in config.OUTPUTS_DIR.glob("run_*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )


def run_pick_label(run_dir: Path) -> str:
    """선택 목록에 보일 한 줄. 상태를 먼저 보여줘야 여러 실행 중에 고를 수 있다."""
    log = runlog.load(run_dir)
    if not log:
        return f"{run_dir.name} — 기록 없음"

    if (run_dir / "APPROVED").exists():
        status = "확정 완료"
    elif runlog.EMAIL in log:
        status = "이메일 초안까지"
    elif runlog.REPORT in log:
        status = "리포트까지"
    elif runlog.VALIDATE in log:
        status = "검증까지"
    elif runlog.JUDGE in log:
        status = "판정까지"
    else:
        status = "투입만"

    period = (runlog.section(runlog.section(log, runlog.JUDGE), "기간")).get("최대") or "기간 미상"
    entry = runlog.section(log, runlog.RUN).get("진입", "?")
    return f"{run_dir.name} · {period} · {status} ({entry})"


def find_source_file(run_dir: Path) -> Optional[Path]:
    """run 폴더에 복사된 원본 CSV. 산출물 파일명이 아닌 것을 찾는다.

    **원본을 다시 읽어 재계산하는 이유**: run_log만으로 판정 객체를 어설프게 복원하면
    실제 업로드와 다른 모양이 되어 화면이 깨진다. 계산은 결정적이므로(CLAUDE.md 5-5)
    같은 파일 + 같은 카탈로그를 다시 넣으면 실제 업로드와 똑같은 결과가 나온다.
    """
    for path in sorted(run_dir.iterdir()):
        if path.is_file() and path.name not in KNOWN_OUTPUT_FILES:
            return path
    return None


def load_existing_run(run_dir_str: str, metrics_catalog: dict, schema_catalog: dict) -> None:
    """선택한 run 폴더를 화면에 복원한다.

    **금지 표현 검사·SQL 재실행은 하지 않는다.** metrics.csv·report.md·email.*는
    이미 나온 결과라 파일 그대로 읽는다 — 다시 계산하면 CLI가 만든 값과 화면이
    보여주는 값이 갈릴 수 있다. 오직 1·2단계 판정 객체만 원본 CSV로 재계산한다.
    """
    run_dir = Path(run_dir_str)
    discard_from_step1()
    st.session_state.load_run_error = None

    log = runlog.load(run_dir)
    if not log:
        st.session_state.load_run_error = f"{run_dir.name}에 run_log.json이 없거나 읽을 수 없습니다."
        return

    source = find_source_file(run_dir)
    if source is None:
        st.session_state.load_run_error = f"{run_dir.name}에서 원본 업로드 파일을 찾지 못했습니다."
        return

    raw = source.read_bytes()
    try:
        frame, encoding = intake.read_csv_bytes(raw)
    except intake.IntakeError as exc:
        st.session_state.load_run_error = f"원본 파일을 읽지 못했습니다: {exc}"
        return

    info = intake.describe(frame, source.name, len(raw), encoding, raw)
    table_judgement = profile.judge_table(frame, schema_catalog)
    table_name = table_judgement.get("테이블명") or table_judgement.get("후보테이블명")
    data_profile = profile.profile_data(frame, schema_catalog.get(table_name))

    judge_section = runlog.section(log, runlog.JUDGE)
    # 그때 실제로 쓴 기간을 우선한다. --month로 지정했을 수 있어 방금 다시 읽은 값과
    # 다를 수 있다 — 이 실행이 실제로 계산한 기간을 보여줘야 한다.
    period = (
        (judge_section.get("기간") or {}).get("최대")
        or data_profile["기간"]["최대"]
        or data_profile["기간"]["최소"]
    )
    metric_judgement = profile.judge_metrics(
        table_name, period, metrics_catalog, missing_columns=table_judgement.get("누락컬럼")
    )

    st.session_state.frame = frame
    st.session_state.raw = raw
    st.session_state.intake = info
    st.session_state.upload_key = f"loaded::{run_dir.name}"
    st.session_state.upload_stamp = runlog.section(log, runlog.INTAKE).get("업로드시각")
    st.session_state.table_judgement = table_judgement
    st.session_state.data_profile = data_profile
    st.session_state.metric_judgement = metric_judgement
    st.session_state.judgement_key = (
        st.session_state.upload_key,
        catalog_stamp(metrics_catalog),
        catalog_stamp(schema_catalog),
    )

    gate1 = runlog.section(log, runlog.GATE1)
    st.session_state.range_approved = bool(gate1.get("유효구간확장_승인"))
    st.session_state.run_dir = str(run_dir)
    st.session_state.last_run_dir = None
    st.session_state.loaded_run = True

    calc = runlog.section(log, runlog.CALC)
    st.session_state.staging_table = calc.get("스테이징테이블")
    st.session_state.prev_period = (calc.get("전월비교") or {}).get("전월")

    metrics_csv = run_dir / "metrics.csv"
    if metrics_csv.exists():
        st.session_state.metrics_frame = pd.read_csv(metrics_csv)
        # render_calculation()은 calc_key != run_dir이면 계산을 자동으로 다시 돌린다.
        # 이걸 안 맞춰 두면 불러오자마자 BigQuery를 다시 때려 방금 읽은 결과를 덮어쓴다.
        st.session_state.calc_key = str(run_dir)
    comparison_csv = run_dir / "comparison.csv"
    if comparison_csv.exists():
        st.session_state.comparison_frame = pd.read_csv(comparison_csv)
    validation_json = run_dir / "validation.json"
    if validation_json.exists():
        try:
            st.session_state.validation = json.loads(validation_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    report_md = run_dir / "report.md"
    if report_md.exists():
        st.session_state.report_markdown = report_md.read_text(encoding="utf-8")
        report_section = runlog.section(log, runlog.REPORT)
        st.session_state.manual_outcome = {
            "치환": report_section.get("채운_자리") or [],
            "남김": report_section.get("미작성_자리") or [],
        }
        # 경고는 저장해 두지 않고 다시 읽는다 — 오래됨·기간 불일치 같은 판정은
        # "지금" 기준이라, 만들 때와 불러올 때 값이 다를 수 있다.
        st.session_state.manual_warnings = list(
            manual_sections.load_manual(period).get("경고") or []
        )

    email_meta = run_dir / "email_meta.json"
    email_html = run_dir / "email.html"
    email_txt = run_dir / "email.txt"
    if email_meta.exists() and email_html.exists():
        try:
            meta = json.loads(email_meta.read_text(encoding="utf-8"))
            st.session_state.email_draft = {
                "subject": meta.get("subject"),
                "to": meta.get("to") or [],
                "from": meta.get("from"),
                "body_html": email_html.read_text(encoding="utf-8"),
                "body_text": email_txt.read_text(encoding="utf-8") if email_txt.exists() else "",
                "attachments": meta.get("attachments") or [],
            }
        except json.JSONDecodeError:
            pass

    approved_path = run_dir / "APPROVED"
    if approved_path.exists():
        try:
            st.session_state.approval = json.loads(approved_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            st.session_state.approval = {}

    # 도달한 가장 먼 단계로 연다. app.py의 기존 관례(리포트 생성→7, 초안 생성→8,
    # 확정→9)와 같은 값을 써야 사이드바 진행 표시가 그대로 맞는다.
    if st.session_state.approval:
        st.session_state.step = 9
    elif st.session_state.email_draft:
        st.session_state.step = 8
    elif st.session_state.report_markdown:
        st.session_state.step = 7
    elif st.session_state.validation is not None:
        st.session_state.step = 4
    elif st.session_state.metrics_frame is not None:
        st.session_state.step = 3
    elif metric_judgement:
        st.session_state.step = 2
    else:
        st.session_state.step = 1


#: 선택 목록의 안내 문구. 실제 경로가 아니므로 이 값이 선택되면 아무 일도 하지 않는다.
RUN_PICKER_PLACEHOLDER = "— 선택 —"


def on_pick_run(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    picked = st.session_state.get("run_picker")
    if not picked or picked == RUN_PICKER_PLACEHOLDER:
        return
    if metrics_catalog is None or schema_catalog is None:
        st.session_state.load_run_error = "카탈로그가 없어 불러올 수 없습니다."
        return
    load_existing_run(picked, metrics_catalog, schema_catalog)


def render_gate_header() -> None:
    """render_step_header와 같은 단순화 — 큰 굵은 제목 + 상태 배지 하나만."""
    if st.session_state.step >= 3:
        status = DONE
    elif st.session_state.step == 2:
        status = ACTIVE
    else:
        status = WAITING

    st.markdown(
        '<div style="font-size:2rem;font-weight:700;margin-top:1.2rem;margin-bottom:.5rem;'
        'line-height:1.3;">'
        "지표 확인 후 확정</div>",
        unsafe_allow_html=True,
    )
    common.render_badges(
        common.tag(f"{status} ✓" if status == DONE else status, PROGRESS_COLORS[status])
    )


def render_confirmed(run_dir: str) -> None:
    st.success(f"확정되었습니다 — {Path(run_dir).name}")
    rows = [["실행 폴더", common.mono(run_dir), "", ""]]
    log = runlog.load(run_dir)
    if not log:
        st.warning("실행 기록을 읽지 못했습니다.")
    else:
        gate = runlog.section(log, runlog.GATE1)
        extension = {
            "필요": gate.get("유효구간확장_필요"),
            "승인": gate.get("유효구간확장_승인"),
            "승인시각": gate.get("승인시각"),
            "대상": gate.get("확장_대상") or [],
            "적용범위": gate.get("적용범위"),
        }
        common.render_table(
            ["항목", "값"],
            [
                [
                    "확정 시각",
                    common.esc(runlog.section(log, runlog.RUN).get("확정시각", "")),
                ],
                ["원본 사본", common.mono((log.get("파일") or {}).get("저장파일", ""))],
                ["실행 기록", common.mono("run_log.json")],
                [
                    "유효구간 확장 승인",
                    common.status_badge(common.WARN, "승인함 (이번 실행에만)")
                    if extension.get("승인")
                    else common.status_badge(common.OK, "해당 없음"),
                ],
            ],
        )
    st.button("확정 취소", key="cancel_run", on_click=cancel_run)
    st.caption("취소해도 이미 만들어진 실행 폴더는 지우지 않습니다. 기록으로 남습니다.")


def render_gate(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    with st.container():
        render_gate_header()

        if st.session_state.gate_error:
            st.error(st.session_state.gate_error)

        if st.session_state.judgement_reset:
            st.warning(
                "카탈로그가 바뀌어 다시 판정했습니다. 이전 승인은 다른 판정에 대한 것이라 "
                "2단계로 되돌렸습니다. 내용을 확인하고 다시 확정하세요."
            )
            st.session_state.judgement_reset = False

        if st.session_state.step < 2:
            st.caption("1단계가 끝나면 열립니다.")
            return

        if st.session_state.run_dir:
            render_confirmed(st.session_state.run_dir)
            return

        if st.session_state.last_run_dir:
            st.caption(f"직전 확정을 취소했습니다. 기록은 {st.session_state.last_run_dir}에 남아 있습니다.")

        judgement = st.session_state.table_judgement
        judged = st.session_state.metric_judgement
        if judgement is None or metrics_catalog is None or schema_catalog is None:
            st.caption("2단계 판정이 끝나면 열립니다.")
            return

        info = st.session_state.intake
        prof = st.session_state.data_profile
        summary = profile.summarize_metrics(judged or [])
        targets = [
            row for row in (judged or []) if row["상태"] in (profile.CALCULABLE, profile.NEEDS_RANGE)
        ]
        needs_range = [row for row in targets if row["상태"] == profile.NEEDS_RANGE]
        partial = [row for row in targets if row.get("부분갱신")]
        blocked = [row for row in (judged or []) if row["상태"] == profile.BLOCKED]

        period = prof["기간"]
        period_text = (
            f"{period['최소']} ~ {period['최대']}" if period["최소"] else "판정하지 못함"
        )

        common.render_table(
            ["확정 대상", "내용"],
            [
                ["파일", common.mono(info["파일명"])],
                [
                    "판정 테이블",
                    common.mono(judgement["테이블명"] or "판정 불가")
                    + (
                        " " + common.tag("추정", common.COLOR_NODATA)
                        if judgement.get("테이블명_추정")
                        else ""
                    ),
                ],
                ["기간", common.esc(period_text)],
                ["행수", f"{info['행수']:,}행"],
                [
                    "계산 대상 지표",
                    f"{len(targets)}종 "
                    + common.status_badge(common.OK, f"계산가능 {summary[profile.CALCULABLE]}")
                    + " "
                    + common.status_badge(common.WARN, f"유효구간 확장 필요 {len(needs_range)}")
                    + " "
                    + common.status_badge(common.BLOCK, f"계산불가 {len(blocked)}")
                    + " "
                    + common.tag(f"부분 갱신 {len(partial)}", common.COLOR_NODATA),
                ],
                [
                    "카탈로그 생성일시",
                    common.esc(common.format_timestamp(catalog_stamp(metrics_catalog))),
                ],
            ],
        )

        if partial:
            st.markdown("")
            common.render_badges(
                common.status_badge(common.WARN, f"부분 갱신 {len(partial)}종"),
                f'<span style="font-size:.85rem;">'
                f"{common.esc(', '.join(row['metric_id'] for row in partial))}</span>",
            )
            others = sorted({source for row in partial for source in row["다른원천"]})
            st.caption(
                f"{', '.join(others)}는 이번 업로드로 갱신되지 않습니다. "
                "기존 BigQuery 테이블과 조인해 계산하므로, 그쪽이 낡았으면 결과도 낡습니다. "
                "진행은 막지 않습니다."
            )

        # 누락 컬럼 때문에 못 세는 지표가 있고 계산할 것도 남아 있으면,
        # "일부만 계산"임을 사용자가 알고 고르게 한다.
        partial_ok = True
        if blocked and targets:
            st.markdown("")
            partial_ok = st.checkbox(
                f"계산불가 {len(blocked)}종을 빼고 나머지 {len(targets)}종만 계산합니다",
                key="partial_ok",
            )
            common.render_badges(
                common.status_badge(common.BLOCK, f"계산불가 {len(blocked)}종"),
                f'<span style="font-size:.85rem;">'
                f"{common.esc(', '.join(row['metric_id'] for row in blocked))}</span>",
            )
            missing_used = sorted(
                {column for row in blocked for column in (row.get("누락컬럼") or [])}
            )
            st.caption(
                f"이 지표들은 파일에 없는 컬럼({', '.join(missing_used)})을 계산에 씁니다. "
                "제외한 지표와 사유는 실행 기록에 남습니다. "
                "전부 계산하려면 원본 파일에 컬럼을 채워 다시 올리세요."
            )

        approved = True
        if needs_range:
            st.markdown("")
            approved = st.checkbox(
                "유효구간 확장을 승인합니다 (이번 실행에만 적용)",
                key="range_ok",
            )
            st.caption(
                f"{len(needs_range)}종이 정의서의 유효구간 밖입니다: "
                f"{', '.join(row['metric_id'] for row in needs_range)}. "
                "승인해도 **위키 정의서는 변경되지 않으며**, 이번 실행에만 적용됩니다. "
                "승인 사실과 대상 지표는 실행 기록과 리포트에 명시됩니다."
            )

        blockers = gate_blockers(judgement, judged)
        for reason in blockers:
            st.error(reason)

        st.button(
            "이 판정으로 계산 진행",
            key="confirm_run",
            type="primary",
            disabled=bool(blockers) or not approved or not partial_ok,
            on_click=confirm_run,
            args=(metrics_catalog, schema_catalog),
        )
        if not blockers:
            waiting = []
            if not approved:
                waiting.append("유효구간 확장 승인")
            if not partial_ok:
                waiting.append("일부만 계산 동의")
            if waiting:
                st.caption("확정 전에 필요 · " + " · ".join(waiting))


# ── 2단계 후반: 계산 ──────────────────────────────────────────────────
def is_auth_error(exc: BaseException) -> bool:
    """인증 문제인지 가른다. 맞으면 화면에 gcloud 명령을 띄운다."""
    if type(exc).__name__ in _AUTH_TYPES:
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _AUTH_HINTS)


def run_calculation(metrics_catalog: dict, schema_catalog: dict) -> None:
    """확정된 판정으로 스테이징 적재 → 지표 계산을 한 번 실행한다."""
    run_dir = Path(st.session_state.run_dir)
    judgement = st.session_state.table_judgement
    prof = st.session_state.data_profile
    judged = st.session_state.metric_judgement or []
    table_name = judgement["테이블명"]

    targets = [
        row for row in judged if row["상태"] in (profile.CALCULABLE, profile.NEEDS_RANGE)
    ]
    staging_map = {table_name: f"{config.STAGING_PREFIX}{table_name}"}

    try:
        with st.spinner("스테이징 적재 중…"):
            client = calculate.make_client()
            staging_table = calculate.load_staging(st.session_state.frame, table_name, client)
        with st.spinner("지표 계산 중…"):
            frame = calculate.calculate(
                targets,
                prof["기간"],
                staging_map,
                client,
                override=bool(st.session_state.range_approved),
                metrics_catalog=metrics_catalog,
            )
    except Exception as exc:  # 인증·권한·적재 실패 — 여기서 멈춘다
        st.session_state.calc_error = {"메시지": str(exc), "인증": is_auth_error(exc)}
        st.session_state.calc_key = st.session_state.run_dir
        return

    st.session_state.metrics_frame = frame
    st.session_state.staging_table = staging_table
    st.session_state.calc_error = None
    st.session_state.calc_key = st.session_state.run_dir

    # 전월 비교. 여기서 실패해도 당월 계산 결과는 잃지 않는다.
    comparison = None
    prev_period = None
    try:
        prev_period = compare.previous_month(prof["기간"])
        with st.spinner(f"전월({prev_period}) 계산 중…"):
            previous = compare.calc_previous(
                targets, prev_period, client, metrics_catalog=metrics_catalog
            )
        comparison = compare.compare(frame, previous, metrics_catalog)
        st.session_state.compare_error = None
    except Exception as exc:
        st.session_state.compare_error = str(exc)

    st.session_state.prev_period = prev_period
    st.session_state.comparison_frame = comparison
    st.session_state.source_freshness = _source_freshness(
        client, frame, table_name, schema_catalog
    )

    # 3단계 검증. 차단이 나오면 4단계로 넘어가지 않는다.
    validation = None
    try:
        with st.spinner("검증 실행 중…"):
            validation = validate.validate_all(
                frame,
                metrics_catalog,
                comparison_df=comparison,
                override=bool(st.session_state.range_approved),
            )
        st.session_state.validation_error = None
    except Exception as exc:
        st.session_state.validation_error = str(exc)

    st.session_state.validation = validation
    if validation and validation["전체판정"] != validate.BLOCK:
        # 4단계 대시보드는 같은 데이터로 바로 그려지므로, 검증을 통과하면
        # 5단계(사람 확인) 게이트까지 열어 둔다.
        st.session_state.step = 5
    else:
        st.session_state.step = 3  # 차단이거나 검증 실패 — 여기서 멈춘다

    _save_results(run_dir, frame, staging_table, comparison, prev_period, validation)

    # 계산·검증이 본문에서 돌기 때문에 사이드바는 이 시점에 이미 옛 상태로 그려져 있다.
    # 한 번 다시 그려 진행 표시와 본문을 맞춘다. calc_key가 채워졌으므로 재계산되지 않는다.
    st.rerun()


def _save_results(
    run_dir: Path,
    frame: pd.DataFrame,
    staging_table: str,
    comparison: Optional[pd.DataFrame] = None,
    prev_period: Optional[str] = None,
    validation: Optional[dict] = None,
) -> None:
    """결과 CSV를 남기고 실행 기록에 계산 사실을 덧붙인다."""
    try:
        # utf-8-sig — 엑셀에서 열었을 때 한글이 깨지지 않게 한다.
        frame.to_csv(run_dir / "metrics.csv", index=False, encoding="utf-8-sig")
        if comparison is not None:
            comparison.to_csv(run_dir / "comparison.csv", index=False, encoding="utf-8-sig")
        if validation is not None:
            (run_dir / "validation.json").write_text(
                json.dumps(validation, ensure_ascii=False, indent=2, default=str) + chr(10),
                encoding="utf-8",
            )
        runlog.write(
            run_dir,
            runlog.CALC,
            runlog.calc_section(
                frame,
                staging_table,
                comparison,
                prev_period,
                compare.summarize(comparison) if comparison is not None else None,
            ),
        )
        if validation is not None:
            runlog.write(run_dir, runlog.VALIDATE, runlog.validate_section(validation))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        st.session_state.calc_error = {
            "메시지": f"계산은 끝났지만 결과를 저장하지 못했습니다: {exc}",
            "인증": False,
        }


def retry_calculation() -> None:
    st.session_state.calc_key = None
    st.session_state.calc_error = None


def render_calc_error(error: Dict[str, Any]) -> None:
    st.error(error["메시지"])
    if error["인증"]:
        st.markdown("**BigQuery 인증이 필요합니다.** 터미널에서 아래를 실행한 뒤 다시 시도하세요.")
        st.code(AUTH_COMMAND, language="bash")
        st.caption(
            "프로젝트가 정해지지 않았다는 오류라면 `gcloud config set project <프로젝트ID>`를 "
            "실행하거나 config.py의 BQ_PROJECT에 직접 적으세요."
        )
    st.button("다시 시도", key="retry_calc", on_click=retry_calculation)


def render_metrics_table(frame: pd.DataFrame, metrics_catalog: dict) -> None:
    ordered = frame.copy()
    ordered["_순서"] = ordered["status"].map(
        lambda status: CALC_STATUS_ORDER.index(status)
        if status in CALC_STATUS_ORDER
        else len(CALC_STATUS_ORDER)
    )
    ordered = ordered.sort_values(["_순서", "month", "metric_id"])

    rows: List[List[str]] = []
    for _, row in ordered.iterrows():
        kind = profile.value_kind(str(row["metric_id"]), metrics_catalog)
        name = common.esc(row["지표명"])
        if bool(row["부분갱신"]):
            name += " " + common.tag("부분 갱신", common.COLOR_NODATA)
        sample = (
            f"{float(row['sample_size']):,.0f}"
            if pd.notna(row["sample_size"]) and pd.notna(row["min_sample"])
            else "—"
        )
        cell = common.format_metric(row["value"], kind)
        # 구간확장은 배지가 이미 말해 준다. 행마다 같은 문장을 되풀이하지 않고
        # 어느 구간을 벗어났는지만 짧게 남긴다.
        if row["status"] == calculate.EXTENDED:
            valid = (metrics_catalog.get(str(row["metric_id"])) or {}).get("유효구간")
            cell += common.muted(f"유효구간 {valid} 밖 · 확장 승인")
        elif str(row["이유"] or ""):
            cell += common.muted(str(row["이유"])[:140])
        rows.append(
            [
                name,
                cell,
                sample,
                common.tag(
                    row["status"], CALC_STATUS_COLORS.get(row["status"], common.COLOR_NODATA)
                ),
                " ".join(common.mono(t) for t in str(row["원천"]).split(" + ") if t),
            ]
        )
    common.render_table(["지표명", "값", "표본", "상태", "원천"], rows)


def direction_text(value: float, text: str) -> str:
    """방향만 색으로 나타낸다. **좋고 나쁨은 판단하지 않는다.**

    증가가 좋은 지표인지 나쁜 지표인지는 사람이 리포트에서 판단할 몫이다
    (CLAUDE.md 1절 — 원인 분석·개선 제안은 자동으로 쓰지 않는다).

    배지가 아니라 글자 색만 쓴다. 알약 배지는 상태(통과·경고·차단)의 시각 언어라,
    변화 방향에 같은 모양을 쓰면 감소가 '차단'으로 읽힌다.
    """
    if value > 0:
        color = common.COLOR_OK
    elif value < 0:
        color = common.COLOR_BLOCK
    else:
        color = common.COLOR_NODATA
    return f'<span style="color:{color};font-weight:600;">{common.esc(text)}</span>'


def change_cell(row: pd.Series, kind: str) -> str:
    """변화 칸. 비율 지표는 퍼센트포인트, 나머지는 절대값에 방향 화살표를 단다.

    **비율 지표에 절대값만 쓰면 오해를 부른다.** 0.366 → 0.390을 "+0.024"로 적으면
    크기를 가늠할 수 없다. 퍼센트포인트로 적어야 "+2.4%p"로 읽힌다.
    """
    points = row["퍼센트포인트변화"]
    if pd.notna(points):
        return direction_text(float(points), f"{float(points):+,.1f}%p")

    absolute = row["절대변화"]
    if pd.isna(absolute):
        return "—"
    absolute = float(absolute)
    arrow = "▲" if absolute > 0 else ("▼" if absolute < 0 else "―")
    return direction_text(absolute, f"{arrow} {common.format_metric(abs(absolute), kind)}")


def rate_cell(row: pd.Series) -> str:
    rate = row["상대변화율"]
    if pd.isna(rate):
        return "—"
    return direction_text(float(rate), f"{float(rate):+,.1f}%")


def render_comparison(metrics_catalog: dict) -> None:
    """당월 대비 전월. 방향만 보여주고 해석은 하지 않는다."""
    frame = st.session_state.comparison_frame
    prev_period = st.session_state.prev_period

    st.divider()
    label = f"전월 대비 ({prev_period} → 당월)" if prev_period else "전월 대비"
    st.markdown(
        f'<div style="font-weight:700;margin:.2rem 0 .35rem;">{common.esc(label)}</div>',
        unsafe_allow_html=True,
    )

    if st.session_state.compare_error:
        st.warning(f"전월을 계산하지 못했습니다: {st.session_state.compare_error}")
        return
    if frame is None or frame.empty:
        st.info("비교할 결과가 없습니다.")
        return

    rows: List[List[str]] = []
    for _, row in frame.iterrows():
        metric_id = str(row["metric_id"])
        kind = profile.value_kind(metric_id, metrics_catalog)
        comparable = row["비교상태"] == compare.COMPARABLE

        if comparable:
            change, rate = change_cell(row, kind), rate_cell(row)
        else:
            change = common.status_badge(common.NODATA, "비교 불가")
            rate = "—"
        if str(row["이유"] or ""):
            change += common.muted(str(row["이유"])[:110])

        rows.append(
            [
                common.esc(row["지표명"]),
                common.format_metric(row["당월"], kind),
                common.format_metric(row["전월"], kind),
                change,
                rate,
            ]
        )

    common.render_table(["지표명", "당월", "전월", "변화", "변화율"], rows)

    counts = compare.summarize(frame)
    st.caption(
        f"비교 가능 {counts[compare.COMPARABLE]}종 · 비교 불가 {counts[compare.INCOMPARABLE]}종 · "
        f"{Path(st.session_state.run_dir).name}/comparison.csv"
    )
    st.caption(
        "색은 **방향만** 나타냅니다. 증가가 좋은지 나쁜지는 판단하지 않습니다 — "
        "해석은 리포트 단계에서 사람이 씁니다."
    )


def render_calculation(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    if not st.session_state.run_dir or metrics_catalog is None or schema_catalog is None:
        return

    with st.container():
        st.markdown(
            '<div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;'
            'margin-bottom:.2rem;">'
            '<span style="font-size:1.05rem;font-weight:700;">2단계 계산 결과</span>'
            + common.tag("시스템", common.COLOR_NODATA)
            + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"산출물 · {Path(st.session_state.run_dir).name}/metrics.csv")

        if st.session_state.calc_key != st.session_state.run_dir:
            run_calculation(metrics_catalog, schema_catalog)

        if st.session_state.calc_error:
            render_calc_error(st.session_state.calc_error)
            if st.session_state.metrics_frame is None:
                return

        frame = st.session_state.metrics_frame
        if frame is None or frame.empty:
            st.info("계산된 지표가 없습니다.")
            return

        counts = frame["status"].value_counts()
        cards = st.columns(4)
        cards[0].metric("계산 성공", int(counts.get(calculate.OK, 0)))
        cards[1].metric("구간확장", int(counts.get(calculate.EXTENDED, 0)))
        cards[2].metric("데이터 없음", int(counts.get(calculate.NO_DATA, 0)))
        cards[3].metric("오류", int(counts.get(calculate.ERROR, 0)))

        low_sample = int(counts.get(calculate.LOW_SAMPLE, 0))
        if low_sample:
            st.caption(f"표본부족 {low_sample}종 — 값은 냈지만 결론 근거로 쓰지 않습니다.")

        render_metrics_table(frame, metrics_catalog)

        if st.session_state.staging_table:
            st.caption(
                f"스테이징 · {st.session_state.staging_table} "
                "(매 실행 교체됩니다. 원본 테이블은 읽기만 했습니다.)"
            )

        render_comparison(metrics_catalog)


# ── 3단계 화면: 검증 ──────────────────────────────────────────────────
def render_validation_verdict(validation: Dict[str, Any]) -> None:
    """전체 판정을 크게 하나. 세부 항목보다 먼저 보여야 할 것은 '지금 진행해도 되는가'다."""
    verdict = validation["전체판정"]
    color = VALIDATION_COLORS.get(verdict, common.COLOR_NODATA)
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;'
        f'padding:.7rem .9rem;border-radius:10px;border:1px solid {color}59;'
        f'background:{color}14;margin:.3rem 0 .6rem;">'
        f'<span style="font-size:1.35rem;font-weight:800;color:{color};">'
        f'{common.esc(verdict)}</span>'
        f'<span style="font-size:.92rem;">차단 {validation["차단수"]}건 · '
        f'경고 {validation["경고수"]}건</span></div>',
        unsafe_allow_html=True,
    )


def validation_rows(items: Sequence[Dict[str, Any]]) -> List[List[str]]:
    return [
        [
            common.esc(item["검증명"]),
            common.esc(item["대상지표"]),
            common.tag(item["판정"], VALIDATION_COLORS.get(item["판정"], common.COLOR_NODATA)),
            common.esc(item["상세"]),
        ]
        for item in items
    ]


def render_not_automated(validation: Dict[str, Any]) -> None:
    """자동으로 하지 않은 검증을 화면에도 남긴다.

    리포트 한계 절에만 적고 화면에서 빼면, 화면을 보는 사람은 "검증 통과"를 실제보다
    강하게 읽는다. 화면과 문서의 시각 언어를 맞춘다 (CLAUDE.md 6·7절).
    """
    items = validation.get("자동검증하지_않은_것") or []
    slate = common.COLOR_NODATA
    body = "".join(f"<li>{common.esc(name)}</li>" for name in items)
    st.markdown(
        f'<div style="padding:.7rem .9rem;border-radius:10px;border:1px solid {slate}59;'
        f'background:{slate}14;margin-top:.7rem;">'
        f'<div style="font-weight:700;color:{slate};margin-bottom:.3rem;">'
        f'이 검증은 수행되지 않았습니다</div>'
        f'<ul style="margin:.2rem 0 .4rem 1.1rem;padding:0;font-size:.9rem;">{body}</ul>'
        f'<div style="font-size:.85rem;color:{slate};">'
        f'이 항목들은 판단이 필요해 자동화하지 않았습니다. 리포트 한계 절에 명시됩니다.'
        f'</div></div>',
        unsafe_allow_html=True,
    )


def render_step3() -> None:
    with st.container():
        render_step_header(STEPS[2])

        if st.session_state.step < 3:
            st.caption("2단계 확정 후 열립니다.")
            return

        if st.session_state.validation_error:
            st.error(f"검증을 실행하지 못했습니다: {st.session_state.validation_error}")
            return

        validation = st.session_state.validation
        if validation is None:
            st.caption("계산이 끝나면 자동으로 실행됩니다.")
            return

        render_validation_verdict(validation)

        items = validation["항목별결과"]
        attention = [item for item in items if item["판정"] != validate.PASS]
        passed = [item for item in items if item["판정"] == validate.PASS]

        headers = ["검증", "대상", "판정", "상세"]
        if attention:
            common.render_table(headers, validation_rows(attention))
        else:
            st.success("경고·차단 항목이 없습니다.")

        if passed:
            with st.expander(f"통과 항목 {len(passed)}건", expanded=False):
                common.render_table(headers, validation_rows(passed))

        render_not_automated(validation)

        if validation["전체판정"] == validate.BLOCK:
            st.error(
                f"차단 {validation['차단수']}건이 있어 4단계로 넘어가지 않습니다. "
                "원인을 고치고 파일을 다시 올리거나 정의서를 고친 뒤 다시 실행하세요."
            )
            st.caption("차단을 우회하는 경로는 두지 않았습니다. 검증을 안 거친 숫자가 리포트에 들어가면 안 됩니다.")

        st.caption(f"{Path(st.session_state.run_dir).name}/validation.json")


# ── 4단계 화면: 대시보드 (상단) ───────────────────────────────────────
#: 한눈에 볼 핵심 지표. 카탈로그가 아니라 화면 구성이라 여기 둔다.
HEADLINE_METRICS = [
    "billed_revenue",
    "active_customers_contract",
    "arpu",
    "avg_data_usage",
]


def _metric_row(frame: Optional[pd.DataFrame], metric_id: str) -> Optional[Dict[str, Any]]:
    if frame is None or len(frame) == 0 or "metric_id" not in frame.columns:
        return None
    hit = frame[frame["metric_id"] == metric_id]
    return hit.iloc[-1].to_dict() if len(hit) else None


def render_dashboard_summary(validation: Dict[str, Any], metrics_catalog: dict) -> None:
    """검증 요약 한 줄과 이상 신호. 해석 문장은 넣지 않는다 (CLAUDE.md 1절)."""
    verdict = validation["전체판정"]
    color = VALIDATION_COLORS.get(verdict, common.COLOR_NODATA)
    common.render_badges(
        common.tag(verdict, color),
        f'<span style="font-size:.92rem;">차단 {validation["차단수"]} / '
        f'경고 {validation["경고수"]}</span>',
    )

    signals = [
        item
        for item in validation["항목별결과"]
        if item["검증명"] == "전월 대비 변동" and item["판정"] == validate.WARN
    ]
    if signals:
        body = "".join(
            f'<li>{common.mono(item["대상지표"])} '
            f'<span style="font-weight:600;">{float(item["값"]):+,.1f}%</span></li>'
            for item in signals
            if item.get("값") is not None
        )
        st.markdown(
            f'<div style="margin:.4rem 0 .2rem;">'
            f'<span style="font-size:.9rem;font-weight:700;color:{common.COLOR_WARN};">'
            f'이상 신호 {len(signals)}건</span>'
            f'<ul style="margin:.25rem 0 0 1.1rem;padding:0;font-size:.9rem;">{body}</ul>'
            f"</div>",
            unsafe_allow_html=True,
        )

    prof = st.session_state.data_profile or {}
    period = prof.get("기간") or {}
    span = (
        f'{period.get("최소")} ~ {period.get("최대")}'
        if period.get("최소") and period.get("최소") != period.get("최대")
        else (period.get("최소") or "기간 미상")
    )
    st.caption(
        f"대상 기간 {span} · 카탈로그 갱신 "
        f"{common.format_timestamp(catalog_stamp(metrics_catalog))}"
    )


def render_headline_cards(metrics_catalog: dict) -> None:
    """핵심 지표 카드 4개. 값과 전월 대비만 싣고 해석은 싣지 않는다."""
    metrics = st.session_state.metrics_frame
    comparison = st.session_state.comparison_frame

    columns = st.columns(len(HEADLINE_METRICS))
    notes: List[str] = []

    for column, metric_id in zip(columns, HEADLINE_METRICS):
        spec = metrics_catalog.get(metric_id) or {}
        label = spec.get("지표명", metric_id)
        kind = profile.value_kind(metric_id, metrics_catalog)
        row = _metric_row(metrics, metric_id)

        if row is None:
            # 이번 업로드와 무관한 원천을 쓰는 지표는 계산 대상이 아니다.
            # 0으로 채우지 않고 값 없음으로 둔다 (CLAUDE.md 9절).
            column.metric(label, "—")
            notes.append(f"{label}: 이번 업로드 대상이 아니라 계산하지 않았습니다.")
            continue

        value = common.format_metric(row.get("value"), kind, compact=True)
        delta = common.format_delta(_metric_row(comparison, metric_id), kind)
        # delta_color는 "normal"로 둔다 — 방향만 표시하고 좋고 나쁨은 판단하지 않는다.
        column.metric(label, value, delta=delta, delta_color="normal")

        if row.get("status") not in (calculate.OK, calculate.EXTENDED):
            notes.append(f"{label}: {row.get('status')} — {str(row.get('이유') or '')[:60]}")
        if bool(row.get("부분갱신")):
            others = str(row.get("원천") or "").split(" + ")
            rest = [t for t in others if t and t != st.session_state.table_judgement["테이블명"]]
            notes.append(f"{label}: 부분 갱신 — {', '.join(rest)}는 이번 업로드로 갱신되지 않았습니다.")
        if delta is None:
            notes.append(f"{label}: 전월 값이 없어 비교하지 않았습니다.")

    for note in notes:
        st.caption(note)


def _source_freshness(
    client: Any, frame: pd.DataFrame, staged_table: str, schema_catalog: dict
) -> Dict[str, Dict[str, Optional[str]]]:
    """이번에 갱신되지 않은 원천 테이블이 언제까지의 상태인지 조회한다.

    부분 갱신 지표는 스테이징과 기존 테이블을 조인해 계산한다. 기존 테이블이 언제까지의
    데이터인지 적어 두지 않으면, 읽는 사람이 전부 같은 시점이라고 오해한다.
    조회 실패는 조용히 넘긴다 — 화면에 안내 한 줄이 빠질 뿐 계산 결과는 유효하다.
    """
    used: set = set()
    for source in frame.get("원천", []):
        used.update(part.strip() for part in str(source).split("+") if part.strip())
    used.discard(staged_table)

    freshness: Dict[str, Dict[str, Optional[str]]] = {}
    for table in sorted(used):
        entry = schema_catalog.get(table) or {}
        date_columns = [
            str(column.get("컬럼명"))
            for column in entry.get("컬럼", [])
            if str(column.get("타입", "")).lower().startswith(("date", "timestamp", "datetime"))
        ]
        if not date_columns:
            continue
        try:
            freshness[table] = calculate.latest_dates(client, table, date_columns)
        except Exception:
            continue
    return freshness


def render_all_metrics_table(metrics_catalog: dict) -> None:
    """계산된 지표 전체. 핵심 카드에 나온 것도 다시 싣는다 — 카드는 요약이고 이 표가 원본이다."""
    metrics = st.session_state.metrics_frame
    comparison = st.session_state.comparison_frame
    if metrics is None or metrics.empty:
        st.info("계산된 지표가 없습니다.")
        return

    staged = (st.session_state.table_judgement or {}).get("테이블명")
    limit = config.MOM_THRESHOLD

    rows: List[List[str]] = []
    styles: List[str] = []
    for _, row in metrics.sort_values(["metric_id", "month"]).iterrows():
        metric_id = str(row["metric_id"])
        kind = profile.value_kind(metric_id, metrics_catalog)
        changed = _metric_row(comparison, metric_id)

        name = common.esc(row["지표명"])
        if bool(row.get("부분갱신")):
            others = [
                part.strip()
                for part in str(row.get("원천") or "").split("+")
                if part.strip() and part.strip() != staged
            ]
            name += common.hint(
                "부분",
                f"{', '.join(others)}은(는) 이번 업로드로 갱신되지 않았습니다. "
                "기존 테이블과 조인해 계산했습니다.",
            )

        if changed and str(changed.get("비교상태")) == compare.COMPARABLE:
            change = change_cell(pd.Series(changed), kind)
            rate = rate_cell(pd.Series(changed))
            rate_value = changed.get("상대변화율")
        else:
            change = common.status_badge(common.NODATA, "비교 불가")
            rate = "—"
            rate_value = None

        sample = (
            f"{float(row['sample_size']):,.0f}"
            if pd.notna(row["sample_size"]) and pd.notna(row["min_sample"])
            else "—"
        )

        rows.append(
            [
                name,
                common.format_metric(row["value"], kind),
                common.format_metric(changed.get("전월") if changed else None, kind),
                change,
                rate,
                sample,
                common.tag(
                    row["status"], CALC_STATUS_COLORS.get(row["status"], common.COLOR_NODATA)
                ),
            ]
        )
        # 임계값을 넘은 행은 배경으로 눈에 띄게 한다. 값이 틀렸다는 뜻은 아니다.
        over = rate_value is not None and pd.notna(rate_value) and abs(float(rate_value)) >= limit
        styles.append(f"background:{common.COLOR_WARN}1a;" if over else "")

    common.render_table(
        ["지표명", "당월", "전월", "변화", "변화율", "표본", "상태"], rows, styles
    )
    st.caption(
        f"배경이 옅게 칠해진 행은 전월 대비 변화율이 임계값 {limit:g}% 이상입니다. "
        "확인이 필요하다는 표시이지 값이 틀렸다는 뜻은 아닙니다."
    )


def render_unrelated_metrics() -> None:
    unrelated = [
        row
        for row in (st.session_state.metric_judgement or [])
        if row["상태"] == profile.UNRELATED
    ]
    if not unrelated:
        return
    with st.expander(f"이번 실행에서 계산하지 않은 지표 {len(unrelated)}종", expanded=False):
        common.render_table(
            ["지표명", "metric_id", "쓰는 테이블", "이유"],
            [
                [
                    common.esc(row["지표명"]),
                    common.mono(row["metric_id"]),
                    " ".join(common.mono(source) for source in row["원천"]) or "—",
                    common.esc(row["이유"]),
                ]
                for row in unrelated
            ],
        )


def render_partial_notice() -> None:
    """부분 갱신 안내. 어느 테이블이 언제까지의 상태인지 함께 적는다."""
    metrics = st.session_state.metrics_frame
    if metrics is None or metrics.empty:
        return
    partial = metrics[metrics["부분갱신"].astype(bool)]
    if partial.empty:
        return

    staged = (st.session_state.table_judgement or {}).get("테이블명")
    others: set = set()
    for source in partial["원천"]:
        others.update(
            part.strip()
            for part in str(source).split("+")
            if part.strip() and part.strip() != staged
        )

    names = ", ".join(sorted(partial["metric_id"].unique()))
    tables = ", ".join(sorted(others))
    freshness = st.session_state.source_freshness or {}
    detail = []
    for table in sorted(others):
        dates = freshness.get(table) or {}
        marks = [f"{column} 최대 {value}" for column, value in sorted(dates.items()) if value]
        if marks:
            detail.append(f"{table}는 이전 상태({', '.join(marks)})를 반영합니다.")
    tail = " ".join(detail) if detail else f"{tables}는 이전 상태를 반영합니다."

    slate = common.COLOR_NODATA
    st.markdown(
        f'<div style="padding:.7rem .9rem;border-radius:10px;border:1px solid {slate}59;'
        f'background:{slate}14;margin-top:.7rem;font-size:.9rem;">'
        f"{common.esc(names)}는 {common.esc(tables)} 테이블을 함께 사용합니다. "
        f"업로드된 것은 {common.mono(staged)}뿐이므로 {common.esc(tail)}"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_validation_recap(validation: Dict[str, Any]) -> None:
    """대시보드 안에서 3단계 검증 결과를 다시 보여준다.

    **3단계와 중복이 아니다. 목적이 다르다.**
      - 3단계는 검증 전용 화면이다. 무엇이 걸렸는지 *찾는* 곳이라 경고·차단을 펼쳐
        두고 통과는 접어 둔다.
      - 4단계는 결과 확인 화면이다. 숫자와 검증을 *함께* 보는 곳이다.

    게이트 2(5단계 "내용·검증 결과 확인")에서 사람이 진행 여부를 판단할 때, 지표를
    보다가 검증을 확인하려고 위로 스크롤해 올라가야 한다면 한쪽을 안 보고 넘기게 된다.
    **한 화면에서 다 보이는 것이 목적이다.**

    렌더링 함수(validation_rows·render_not_automated)는 3단계와 같은 것을 쓴다.
    표를 두 벌 만들면 한쪽만 고쳐져 화면끼리 어긋난다.
    """
    verdict = validation["전체판정"]
    color = VALIDATION_COLORS.get(verdict, common.COLOR_NODATA)
    label = (
        f"검증 상세 — {verdict} (차단 {validation['차단수']} / 경고 {validation['경고수']})"
    )

    with st.expander(label, expanded=False):
        common.render_badges(
            common.tag(verdict, color),
            f'<span style="font-size:.9rem;">'
            f"항목 {len(validation['항목별결과'])}건 · 3단계와 같은 결과입니다.</span>",
        )
        order = {validate.BLOCK: 0, validate.WARN: 1, validate.PASS: 2}
        items = sorted(
            validation["항목별결과"], key=lambda item: order.get(item["판정"], 9)
        )
        common.render_table(["검증", "대상", "판정", "상세"], validation_rows(items))
        render_not_automated(validation)


# ── 4단계 화면: 추이 차트 ─────────────────────────────────────────────
#: 차트 3종. 같은 단위끼리 묶어 단일 축을 쓰고, 단위가 다른 조합만 이중 축이다.
TREND_CHARTS: Tuple[Dict[str, Any], ...] = (
    {
        "제목": "매출 추이",
        "계열": [
            ("billed_revenue", common.CHART_BLUE),
            ("billed_revenue_active", common.CHART_INDIGO),
        ],
        "이중축": False,
    },
    {
        "제목": "사용량 · 저사용 고객 비율",
        "계열": [
            ("avg_data_usage", common.CHART_BLUE),
            ("low_usage_customer_rate", common.CHART_VIOLET),
        ],
        "이중축": True,
    },
    {
        "제목": "활성 고객 추이",
        "계열": [
            ("active_customers_contract", common.CHART_BLUE),
            ("active_users_product", common.CHART_CYAN),
        ],
        "이중축": False,
    },
)

#: 축 라벨. value_kind가 돌려주는 이름을 축에 그대로 쓰면 '금액'·'비율'로 나와
#: 캡션("좌축 GB / 우축 %")과 어긋난다. 단위 기호로 바꿔 준다.
AXIS_TITLES = {"금액": "원", "비율": "%", "GB": "GB", "명": "명", "건": "건", "수": ""}

#: 추이에 필요한 지표 전체(중복 제거). 차트 정의에서 유도해 손으로 맞추지 않는다.
TREND_METRICS: Tuple[str, ...] = tuple(
    dict.fromkeys(metric for chart in TREND_CHARTS for metric, _ in chart["계열"])
)


@st.cache_data(show_spinner=False)
def cached_trend(
    metric_ids: Tuple[str, ...],
    end_period: str,
    months: int,
    staging_items: Tuple[Tuple[str, str], ...],
    override: bool,
    catalog_stamp: Optional[str],
    run_dir: str,
    _client: Any,
    _metrics_catalog: dict,
    _progress: Any,
) -> pd.DataFrame:
    """추이 계산은 BigQuery를 여러 번 치므로 캐싱한다.

    밑줄로 시작하는 인자는 Streamlit이 캐시 키에서 제외한다 — 클라이언트·카탈로그·
    콜백은 해시할 수 없기 때문이다. 대신 `catalog_stamp`와 `run_dir`을 키에 넣어,
    카탈로그를 다시 export하거나 새로 실행하면 캐시가 갈린다.
    """
    return charts.build_trend(
        list(metric_ids),
        end_period,
        months,
        dict(staging_items),
        _client,
        metrics_catalog=_metrics_catalog,
        override=override,
        progress=_progress,
    )


def axis_range(values: Sequence[Any], pad: float = 0.08) -> Optional[Tuple[float, float]]:
    """축 범위를 값에서 계산해 **명시적으로** 지정한다.

    Plotly 자동 스케일에 맡기면 달마다 범위가 달라져, 같은 변화폭이 어떤 달에는 크게
    어떤 달에는 작게 보인다. 범위를 우리가 정하고 캡션에 적어 둔다.
    """
    clean = [float(v) for v in values if v is not None and v == v]
    if not clean:
        return None
    low, high = min(clean), max(clean)
    span = (high - low) * pad if high > low else (abs(low) * 0.1 or 1.0)
    return (low - span, high + span)


def _series(trend: pd.DataFrame, metric_id: str, kind: str) -> Tuple[List[str], List[Any]]:
    """차트에 넣을 (월, 값). 값이 없는 달은 **행을 지우지 않고 None으로 둔다.**

    행을 지우면 그 달이 축에서 사라져 앞뒤 점이 이어진다. 0으로 채우면 급락한 것처럼
    보인다. 둘 다 거짓이므로 None으로 두고 connectgaps=False로 선을 끊는다.
    """
    part = charts.to_wide(trend, metric_id)
    scale = 100.0 if kind == "비율" else 1.0
    values = [
        None if (v is None or v != v) else float(v) * scale for v in part["value"].tolist()
    ]
    return part["month"].tolist(), values


def build_trend_figure(
    trend: pd.DataFrame,
    chart: Dict[str, Any],
    metrics_catalog: dict,
    current: str,
    left_range: Optional[Tuple[float, float]] = None,
) -> Tuple[go.Figure, Dict[str, Any]]:
    """선 그래프 하나. **추세선을 넣지 않는다** — 6개월로는 계절성을 판별할 수 없다."""
    figure = go.Figure()
    # 축별로 값을 모아 **계열 전체**를 덮는 범위를 잡는다. 계열마다 따로 잡으면
    # 나중 계열의 범위가 축을 덮어써서 앞 계열이 그래프 밖으로 잘린다.
    axis_values: Dict[str, List[Any]] = {"좌": [], "우": []}
    axis_kind: Dict[str, str] = {}

    for index, (metric_id, color) in enumerate(chart["계열"]):
        spec = metrics_catalog.get(metric_id) or {}
        kind = profile.value_kind(metric_id, metrics_catalog)
        months, values = _series(trend, metric_id, kind)
        secondary = bool(chart["이중축"]) and index == 1

        # 당월은 업로드 파일로 계산한 달이다. 점을 키워 다른 달과 구분한다.
        sizes = [13 if month == current else 7 for month in months]
        lines = [2 if month == current else 0 for month in months]

        figure.add_trace(
            go.Scatter(
                x=months,
                y=values,
                name=spec.get("지표명", metric_id),
                mode="lines+markers",
                connectgaps=False,  # 값 없는 달에서 선을 끊는다
                line=dict(color=color, width=2.5),
                marker=dict(
                    color=color, size=sizes, line=dict(color=color, width=lines)
                ),
                yaxis="y2" if secondary else "y",
                hovertemplate="%{x} · %{y:,.2f}<extra>%{fullData.name}</extra>",
            )
        )
        side = "우" if secondary else "좌"
        axis_values[side].extend(values)
        axis_kind.setdefault(side, kind)

    ranges: Dict[str, Any] = {
        side: {"범위": axis_range(values), "단위": axis_kind.get(side, "수")}
        for side, values in axis_values.items()
        if values
    }
    # left_range를 주면 계산된 범위 대신 그것을 쓴다(축 범위 비교용).
    left = left_range or ranges.get("좌", {}).get("범위")
    layout: Dict[str, Any] = dict(
        height=320,
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
        xaxis=dict(title=None, showgrid=False),
        yaxis=dict(
            title=AXIS_TITLES.get(ranges.get("좌", {}).get("단위", ""), ""),
            range=list(left) if left else None,
        ),
    )
    if chart["이중축"]:
        right = ranges.get("우", {}).get("범위")
        layout["yaxis2"] = dict(
            title=AXIS_TITLES.get(ranges.get("우", {}).get("단위", ""), ""),
            overlaying="y",
            side="right",
            range=list(right) if right else None,
            showgrid=False,
        )
    figure.update_layout(**layout)
    return figure, ranges


def _range_text(bounds: Optional[Tuple[float, float]], kind: str) -> str:
    if not bounds:
        return "범위 없음"
    low, high = bounds
    return f"{common.format_metric(low, kind, compact=True)} ~ {common.format_metric(high, kind, compact=True)}"


#: 좌축 범위 비교안. 학습용이라 값을 코드에 박아 둔다 — 데이터에서 유도하면
#: "같은 데이터, 다른 축"이라는 비교 자체가 성립하지 않는다.
AXIS_VARIANTS = (
    {"이름": "A안 · 좌축 20~24 GB", "범위": (20.0, 24.0), "설명": "데이터 범위에 맞춘 좁은 축"},
    {"이름": "B안 · 좌축 0~50 GB", "범위": (0.0, 50.0), "설명": "0에서 시작하는 넓은 축"},
)


def render_axis_comparison(
    trend: pd.DataFrame, chart: Dict[str, Any], metrics_catalog: dict, current: str
) -> None:
    """같은 데이터를 좌축 범위만 바꿔 나란히 보여준다 (학습용).

    축 범위는 데이터를 바꾸지 않지만 인상은 바꾼다. 그래서 이 앱은 축 범위를 자동에
    맡기지 않고 **명시적으로 정하고 캡션에 적는다**. 그 이유를 눈으로 확인하는 자리다.
    """
    metric_id = chart["계열"][0][0]
    series = charts.to_wide(trend, metric_id)
    values = [float(v) for v in series["value"].tolist() if v is not None and v == v]
    if not values:
        return
    low, high = min(values), max(values)

    with st.expander("축 범위 비교 — 같은 데이터, 다른 인상 (학습용)", expanded=False):
        st.caption(
            f"두 그림의 데이터는 같습니다. 좌축 범위만 다릅니다. "
            f"평균 데이터 사용량 실제 범위 {low:,.2f} ~ {high:,.2f} GB "
            f"(최저 대비 최고 +{(high - low) / low * 100:,.1f}%)"
        )
        columns = st.columns(len(AXIS_VARIANTS))
        for column, variant in zip(columns, AXIS_VARIANTS):
            with column:
                st.markdown(
                    f'<div style="font-weight:700;font-size:.92rem;margin-bottom:.2rem;">'
                    f'{common.esc(variant["이름"])}</div>'
                    f'<div style="font-size:.82rem;color:{common.COLOR_NODATA};'
                    f'margin-bottom:.3rem;">{common.esc(variant["설명"])}</div>',
                    unsafe_allow_html=True,
                )
                figure, _ = build_trend_figure(
                    trend, chart, metrics_catalog, current, left_range=variant["범위"]
                )
                figure.update_layout(height=280, showlegend=False)
                st.plotly_chart(
                    figure, width="stretch", key=f"axis_variant_{variant['이름']}"
                )

                bottom, top = variant["범위"]
                clipped = [v for v in values if v < bottom or v > top]
                if clipped:
                    st.caption(
                        f"⚠ 축 밖으로 잘린 값 {len(clipped)}개: "
                        + ", ".join(f"{v:,.2f} GB" for v in sorted(clipped))
                    )
                else:
                    occupied = (high - low) / (top - bottom) * 100
                    st.caption(f"변동폭이 축 높이의 {occupied:,.0f}%를 차지합니다.")

        st.markdown(
            f'<div style="padding:.7rem .9rem;border-radius:10px;'
            f'border:1px solid {common.COLOR_NODATA}59;background:{common.COLOR_NODATA}14;'
            f'margin-top:.6rem;font-size:.9rem;">'
            "<b>판단 근거</b> — 둘 다 이 데이터에는 정직하지 않습니다. "
            f"A안은 실제 최대값 {high:,.2f} GB를 축 밖으로 잘라내 데이터를 숨기고, "
            "B안은 실제 변동을 축 높이의 몇 %로 눌러 변화가 없는 것처럼 보이게 합니다. "
            "평균 사용량은 0이 기준점으로 의미를 갖는 양이 아니므로 0에서 시작할 의무는 없고, "
            "<b>모든 값을 담되 여백을 조금 둔 범위</b>가 정직합니다. "
            "다만 어떤 범위를 쓰든 <b>축 범위를 캡션에 적는 것</b>이 조건입니다 — "
            "범위를 밝히지 않은 그래프는 어느 쪽으로든 읽는 사람을 오도할 수 있습니다."
            "</div>",
            unsafe_allow_html=True,
        )


def render_trend_charts(metrics_catalog: dict, schema_catalog: dict) -> None:
    judgement = st.session_state.table_judgement
    profile_data = st.session_state.data_profile
    if not judgement or not profile_data:
        return

    table_name = judgement["테이블명"]
    current = calculate.month_of((profile_data.get("기간") or {}).get("최대"))
    if not current:
        st.caption("기간을 읽지 못해 추이를 그리지 않았습니다.")
        return

    if demo.is_enabled() and st.session_state.get("loaded_run"):
        # 기록된 실행을 불러온 화면이다 — 표의 숫자는 파일에서 읽은 **진짜**다.
        # 추이만 지금 계산하면 데모 모드에서는 가짜가 나오므로, 한 화면에 진짜와
        # 가짜가 섞인다. 그리지 않고 이유를 적는다 (CLAUDE.md 9절 거짓 보고 금지).
        st.info(
            "추이 차트는 그리지 않았습니다 — 이 화면의 지표 값은 기록된 실제 결과인데, "
            "추이는 지금 계산해야 해서 데모 모드에서는 가짜 값이 나옵니다. "
            "한 화면에 진짜와 가짜를 섞지 않으려고 생략했습니다."
        )
        return

    bar = st.progress(0.0, text="추이 계산 준비 중…")

    def on_progress(done: int, total: int, month: str) -> None:
        bar.progress(done / total, text=f"추이 계산 중… {month} ({done}/{total}개월)")

    try:
        trend = cached_trend(
            TREND_METRICS,
            current,
            int(config.TREND_MONTHS),
            ((table_name, f"{config.STAGING_PREFIX}{table_name}"),),
            bool(st.session_state.range_approved),
            catalog_stamp(metrics_catalog),
            str(st.session_state.run_dir),
            calculate.make_client(),
            metrics_catalog,
            on_progress,
        )
    except Exception as exc:
        bar.empty()
        st.warning(f"추이를 계산하지 못했습니다: {exc}")
        return
    bar.empty()

    months = charts.trend_months(current, int(config.TREND_MONTHS))
    for chart in TREND_CHARTS:
        st.markdown(
            f'<div style="font-weight:700;margin:.6rem 0 .2rem;">'
            f'{common.esc(chart["제목"])}</div>',
            unsafe_allow_html=True,
        )
        figure, ranges = build_trend_figure(trend, chart, metrics_catalog, current)
        st.plotly_chart(figure, width="stretch", key=f"trend_{chart['제목']}")

        if chart["이중축"]:
            # 이 문장은 반드시 붙는다. 축이 둘이면 교차점이 우연이라는 것을
            # 적어 두지 않으면 "두 지표가 만났다"로 읽힌다.
            note = "좌축 GB / 우축 % — 축이 다르므로 두 선의 교차점은 의미가 없습니다."
            right = ranges.get("우", {}).get("범위")
            if right:
                note += (
                    f" 좌축 {_range_text(ranges.get('좌', {}).get('범위'), 'GB')}"
                    f" · 우축 {right[0]:,.1f}% ~ {right[1]:,.1f}%"
                )
            st.caption(note)
        else:
            kind = ranges.get("좌", {}).get("단위", "수")
            st.caption(
                f"y축 범위 {_range_text(ranges.get('좌', {}).get('범위'), kind)} "
                "— 0에서 시작하지 않습니다."
            )

        gaps = []
        for metric_id, _ in chart["계열"]:
            info = charts.coverage(trend, metric_id)
            if info["값없음"]:
                blanks = ", ".join(f"{b['month']}({b['status']})" for b in info["빈달"])
                gaps.append(f"{metric_id} {info['값있음']}/{info['전체']}개월 — {blanks}")
        if gaps:
            st.caption("값이 없어 선이 끊긴 구간 · " + " / ".join(gaps))

        st.caption(
            f"{current}은 업로드 파일로 계산, 그 이전은 기존 테이블 · "
            f"{months[0]} ~ {months[-1]} · 추세선 없음(6개월로는 계절성을 판별할 수 없음)"
        )

        if chart["이중축"]:
            render_axis_comparison(trend, chart, metrics_catalog, current)


def render_step4(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    with st.container():
        render_step_header(STEPS[3])

        if st.session_state.step < 4:
            validation = st.session_state.validation
            if validation and validation["전체판정"] == validate.BLOCK:
                common.render_badges(
                    common.status_badge(common.BLOCK, "3단계 차단"),
                    f'<span style="font-size:.88rem;">'
                    f'검증 차단 {validation["차단수"]}건이 해결되어야 열립니다.</span>',
                )
            else:
                st.caption("3단계 검증이 끝나면 열립니다.")
            return

        validation = st.session_state.validation
        if validation is None or metrics_catalog is None:
            st.caption("검증 결과가 없습니다.")
            return

        render_dashboard_summary(validation, metrics_catalog)
        st.divider()
        render_headline_cards(metrics_catalog)
        st.divider()
        render_all_metrics_table(metrics_catalog)
        render_unrelated_metrics()
        render_partial_notice()
        st.divider()
        render_trend_charts(metrics_catalog, schema_catalog or {})
        render_validation_recap(validation)


# ── 5단계 화면: 사람 확인 + 두 번째 게이트 ────────────────────────────
def review_items(validation: Dict[str, Any]) -> List[Dict[str, Any]]:
    """확인할 항목을 지금 실행 상태에서 만든다.

    **해당 없는 항목은 넣지 않는다.** 경고가 0건인데 "경고를 확인했는가"를 체크하게
    하면 의미 없는 클릭이 되고, 체크가 습관이 되면 게이트가 형식이 된다. 대신 빠진
    항목은 아래에 "해당 없음"으로 적어 무엇을 안 물었는지 남긴다.
    """
    profile_data = st.session_state.data_profile or {}
    period = profile_data.get("기간") or {}
    low, high = period.get("최소"), period.get("최대")
    span = low if low == high else f"{low} ~ {high}"

    metrics = st.session_state.metrics_frame
    partial = (
        metrics[metrics["부분갱신"].astype(bool)]
        if metrics is not None and not metrics.empty
        else None
    )
    warnings = [
        item for item in validation["항목별결과"] if item["판정"] == validate.WARN
    ]
    not_automated = validation.get("자동검증하지_않은_것") or []

    items: List[Dict[str, Any]] = [
        {
            "id": "period",
            "label": f"계산 대상 기간이 의도한 기간인가 ({span})",
            "kind": "period",
        }
    ]
    if warnings:
        items.append(
            {
                "id": "warnings",
                "label": f"경고 항목을 확인했는가 ({len(warnings)}건)",
                "kind": "warnings",
                "data": warnings,
            }
        )
    if partial is not None and not partial.empty:
        names = ", ".join(sorted(partial["metric_id"].unique()))
        items.append(
            {
                "id": "partial",
                "label": f"부분 갱신 지표의 한계를 이해했는가 ({names})",
                "kind": "partial",
            }
        )
    if not_automated:
        items.append(
            {
                "id": "not_automated",
                "label": f"자동 검증하지 않은 항목을 인지했는가 ({', '.join(not_automated)})",
                "kind": "not_automated",
            }
        )
    return items


def render_review_detail(item: Dict[str, Any], validation: Dict[str, Any]) -> None:
    """체크 항목마다 판단 근거를 바로 펼쳐 볼 수 있게 한다.

    다른 화면으로 스크롤해 올라가야 확인할 수 있으면 안 보고 체크하게 된다.
    """
    kind = item["kind"]
    if kind == "period":
        info = st.session_state.intake or {}
        period = (st.session_state.data_profile or {}).get("기간") or {}
        common.render_table(
            ["항목", "값"],
            [
                ["파일", common.mono(info.get("파일명", "—"))],
                ["행수", f"{info.get('행수', 0):,}행"],
                ["기간 컬럼", common.mono(period.get("컬럼", "—"))],
                ["기간", common.esc(f"{period.get('최소')} ~ {period.get('최대')}")],
            ],
        )
    elif kind == "warnings":
        common.render_table(
            ["검증", "대상", "판정", "상세"], validation_rows(item["data"])
        )
    elif kind == "partial":
        render_partial_notice()
    elif kind == "not_automated":
        render_not_automated(validation)


def confirm_review(items: Sequence[Dict[str, Any]], validation: Dict[str, Any]) -> None:
    """확인 완료 — 무엇을 확인했는지 실행 기록에 남기고 6단계를 연다."""
    st.session_state.review_error = None
    run_dir = st.session_state.get("run_dir")
    if not run_dir:
        st.session_state.review_error = "실행 폴더가 없어 확인을 기록할 수 없습니다."
        return

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        payload = {
            "상태": "확인 완료",
            "확인_완료_시각": stamp,
            "확인한_체크항목": [item["label"] for item in items],
            "검증_요약": {
                "전체판정": validation["전체판정"],
                "차단수": validation["차단수"],
                "경고수": validation["경고수"],
            },
        }
        runlog.write(run_dir, runlog.REVIEW, payload, stamp)
    except OSError as exc:
        st.session_state.review_error = f"확인 기록을 저장하지 못했습니다: {exc}"
        return

    st.session_state.step = 6  # 6단계 준비됨


def cancel_review() -> None:
    """확인 취소 — 6단계 이후를 다시 잠근다. 기록은 지우지 않고 취소를 덧붙인다."""
    run_dir = st.session_state.get("run_dir")
    if run_dir:
        try:
            runlog.amend(
                run_dir,
                runlog.REVIEW,
                상태="취소됨",
                취소시각=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        except OSError as exc:
            st.session_state.review_error = f"확인 기록에 취소를 적지 못했습니다: {exc}"

    for key in list(st.session_state.keys()):
        if str(key).startswith("review_"):
            st.session_state[key] = False
    st.session_state.step = 5


def render_review_confirmed() -> None:
    st.success("확인이 완료되었습니다 — 6단계(리포트 생성)로 넘어갈 수 있습니다.")
    run_dir = st.session_state.get("run_dir")
    review = runlog.load(run_dir).get(runlog.REVIEW) or {}

    if review:
        common.render_table(
            ["항목", "값"],
            [
                ["확인 완료 시각", common.esc(review.get("확인_완료_시각", ""))],
                [
                    "확인한 항목",
                    "<br>".join(
                        f"· {common.esc(label)}"
                        for label in review.get("확인한_체크항목", [])
                    )
                    or "—",
                ],
                [
                    "검증 요약",
                    common.esc(
                        f"{(review.get('검증_요약') or {}).get('전체판정', '')} · "
                        f"차단 {(review.get('검증_요약') or {}).get('차단수', 0)} · "
                        f"경고 {(review.get('검증_요약') or {}).get('경고수', 0)}"
                    ),
                ],
            ],
        )
    st.button("확인 취소", key="cancel_review", on_click=cancel_review)
    st.caption("취소하면 6단계 이후가 다시 잠깁니다. 실행 기록은 지우지 않고 취소를 덧붙입니다.")


def render_step5() -> None:
    with st.container():
        render_step_header(STEPS[4])

        validation = st.session_state.validation
        if validation and validation["전체판정"] == validate.BLOCK:
            # 차단이면 버튼 자체를 만들지 않는다. 우회 경로를 두지 않기 위해서다.
            common.render_badges(
                common.status_badge(common.BLOCK, "3단계 차단"),
                f'<span style="font-size:.88rem;">'
                f'검증 차단 {validation["차단수"]}건이 해결되어야 확인 단계가 열립니다.</span>',
            )
            st.caption("확인 버튼을 만들지 않았습니다. 차단을 넘기는 경로는 두지 않습니다.")
            return

        if st.session_state.step < 5:
            st.caption("4단계 대시보드가 그려지면 열립니다.")
            return

        if st.session_state.review_error:
            st.error(st.session_state.review_error)

        if st.session_state.step >= 6:
            render_review_confirmed()
            return

        if validation is None:
            st.caption("검증 결과가 없습니다.")
            return

        items = review_items(validation)
        st.caption("아래를 모두 확인해야 리포트 생성으로 넘어갈 수 있습니다.")

        checked: List[bool] = []
        for item in items:
            checked.append(st.checkbox(item["label"], key=f"review_{item['id']}"))
            with st.expander("확인하기", expanded=False):
                render_review_detail(item, validation)

        skipped = {"warnings", "partial"} - {item["kind"] for item in items}
        if skipped:
            labels = {"warnings": "경고 항목", "partial": "부분 갱신 지표"}
            st.caption(
                "해당 없어 묻지 않은 항목 · " + ", ".join(labels[kind] for kind in sorted(skipped))
            )

        st.button(
            "확인 완료, 리포트 생성 단계로",
            key="confirm_review",
            type="primary",
            disabled=not all(checked),
            on_click=confirm_review,
            args=(items, validation),
        )
        if not all(checked):
            st.caption(f"확인 {sum(checked)}/{len(checked)} — 모두 체크해야 진행할 수 있습니다.")


# ── 6단계 화면: 리포트 생성 ───────────────────────────────────────────
def run_context(metrics_catalog: dict, schema_catalog: dict) -> Dict[str, Any]:
    """리포트가 필요로 하는 재료를 세션에서 모은다.

    report.py는 화면 상태를 모르고 이 dict만 본다 — 나중에 무인 실행(cron)에서
    같은 dict를 파일로 만들어 넘기면 화면 없이도 같은 리포트가 나온다.
    """
    judgement = st.session_state.table_judgement or {}
    info = st.session_state.intake or {}
    prof = st.session_state.data_profile or {}

    run_dir = st.session_state.get("run_dir")
    log: Dict[str, Any] = runlog.load(run_dir) if run_dir else {}

    return {
        "파일명": info.get("파일명"),
        "테이블명": judgement.get("테이블명"),
        "기간": prof.get("기간"),
        "행수": info.get("행수"),
        "metrics": st.session_state.metrics_frame,
        "comparison": st.session_state.comparison_frame,
        "validation": st.session_state.validation,
        "metrics_catalog": metrics_catalog,
        "schema_catalog": schema_catalog,
        "insights_catalog": load_catalog(config.INSIGHTS_CATALOG_PATH)[0] or {},
        "run_log": log,
        "source_freshness": st.session_state.source_freshness,
        "생성일시": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def generate_report(metrics_catalog: dict, schema_catalog: dict) -> None:
    """리포트를 만들어 run 폴더에 저장한다."""
    st.session_state.report_error = None
    run_dir = st.session_state.get("run_dir")
    if not run_dir:
        st.session_state.report_error = "실행 폴더가 없어 리포트를 저장할 수 없습니다."
        return

    try:
        # 병합까지 한 번에. CLI도 같은 함수를 쓰므로 화면과 결과가 갈리지 않는다.
        markdown, manual, outcome = report.build_report_with_manual(
            run_context(metrics_catalog, schema_catalog)
        )
    except Exception as exc:
        st.session_state.report_error = f"리포트를 만들지 못했습니다: {type(exc).__name__}: {exc}"
        return

    st.session_state.manual_warnings = list(manual.get("경고") or [])
    st.session_state.manual_outcome = outcome

    path = Path(run_dir) / "report.md"
    try:
        path.write_text(markdown, encoding="utf-8")
        chapters = [number for number, _, _ in split_report(markdown)[1]]
        human = {manual_sections.chapter_of(slot) for slot in manual_sections.MANUAL_SLOTS}
        runlog.write(
            run_dir,
            runlog.REPORT,
            runlog.report_section(
                markdown,
                chapters,
                sorted(human),
                manual_sections.MANUAL_SLOTS,
                outcome,
                manual.get("파일"),
                phrasing.check_forbidden(markdown),
            ),
        )
    except OSError as exc:
        st.session_state.report_error = f"리포트를 저장하지 못했습니다: {exc}"
        return

    st.session_state.report_markdown = markdown
    # 리포트가 바뀌면 기존 초안은 옛 리포트에서 나온 것이다. 남기면 제목의
    # (초안) 표시나 미작성 개수가 화면의 리포트와 어긋난다.
    st.session_state.email_draft = None
    st.session_state.email_error = None
    if st.session_state.step < 7:
        st.session_state.step = 7  # 7단계 준비됨


def current_period() -> Optional[str]:
    """이번 실행의 대상 월. 사람 작성분의 기간 대조에 쓴다."""
    period = (st.session_state.data_profile or {}).get("기간") or {}
    return calculate.month_of(period.get("최대") or period.get("최소"))


def save_manual_text(metrics_catalog: dict, schema_catalog: dict) -> None:
    """편집 내용을 저장하고 리포트를 다시 만든다.

    저장과 재생성을 한 번에 하는 이유: 저장만 하면 화면의 리포트가 옛 내용이라
    "저장했는데 안 바뀐다"로 보인다. 사람이 버튼을 두 번 누르게 하지 않는다.
    """
    text = st.session_state.get("manual_text") or ""
    try:
        path = manual_sections.save_manual(text, current_period())
        # 저장하면서 프론트매터가 바뀌므로, 편집란도 파일에서 다시 읽어 맞춘다.
        # 안 하면 화면은 옛 날짜를 보여주면서 파일은 새 날짜인 상태가 된다.
        st.session_state["manual_text"] = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        st.session_state.report_error = f"사람 작성분을 저장하지 못했습니다: {exc}"
        return
    generate_report(metrics_catalog, schema_catalog)


def empty_slots() -> List[str]:
    """아직 사람이 안 쓴 자리. 병합 결과가 없으면 전부 비었다고 본다.

    `남김`이 빈 리스트인 것("다 썼다")과 병합을 아직 안 한 것("모른다")은 다르다.
    `or`로 뭉치면 다 쓴 순간 전부 미작성으로 되돌아간다.
    """
    outcome = st.session_state.manual_outcome
    if not outcome:
        return list(manual_sections.MANUAL_SLOTS)
    return list(outcome.get("남김") or [])


def render_manual_editor(metrics_catalog: dict, schema_catalog: dict) -> None:
    """`manual/sections.md`를 화면에서 바로 편집한다.

    사람이 쓰는 장을 리포트 파일에 직접 쓰면 다시 생성할 때 날아간다. 그래서 편집은
    이 파일에서만 하고, 리포트는 생성할 때마다 이 파일을 끼워 넣는다.
    """
    period = current_period()
    path, raw = manual_sections.read_raw(period=period)
    pending = empty_slots()

    # value= 와 key= 를 함께 주면 Streamlit이 세션 값을 우선해, 저장 뒤에도 편집란이
    # 옛 내용을 보여준다. 처음 한 번만 파일 내용으로 채운다.
    if "manual_text" not in st.session_state:
        st.session_state["manual_text"] = raw

    with st.expander("사람 작성분 편집", expanded=bool(pending)):
        st.caption(f"{path.parent.name}/{path.name}")
        st.text_area(
            "사람 작성분",
            height=320,
            key="manual_text",
            label_visibility="collapsed",
        )
        st.caption(
            "저장하면 프론트매터의 작성일·대상기간이 지금 값으로 갱신되고, "
            "리포트가 자동으로 다시 생성됩니다."
        )
        st.button(
            "저장",
            key="save_manual",
            disabled=is_approved(),  # 확정 뒤에는 리포트를 다시 만들지 않는다
            on_click=save_manual_text,
            args=(metrics_catalog, schema_catalog),
        )
        for warning in st.session_state.manual_warnings:
            st.caption(f"· {warning}")


#: 장 나누기·자체검사 마커는 pipeline.report가 정한다. PDF 생성(report.build_pdf)도
#: 같은 규칙을 써야 하므로, 화면만 따로 갖지 않는다.
SELF_CHECK_MARK = report.SELF_CHECK_MARK
split_report = report.split_report


def render_report_info(markdown: str, metrics_catalog: dict) -> None:
    """리포트가 무엇을 근거로 언제 만들어졌는지 상단에 고정으로 보여준다."""
    prof = st.session_state.data_profile or {}
    period = prof.get("기간") or {}
    low, high = period.get("최소"), period.get("최대")
    span = low if low == high else f"{low} ~ {high}"

    generated = "알 수 없음"
    run_dir = st.session_state.get("run_dir")
    if run_dir:
        report_log = runlog.load(run_dir).get(runlog.REPORT) or {}
        generated = report_log.get(runlog.STAMP) or generated

    common.render_table(
        ["항목", "값"],
        [
            ["대상 기간", common.esc(span or "기간 미상")],
            ["생성일시", common.esc(common.format_timestamp(generated))],
            [
                "카탈로그 버전",
                common.esc(
                    f"{common.format_timestamp(catalog_stamp(metrics_catalog))} "
                    f"(지표 {catalog_count(metrics_catalog)}종)"
                ),
            ],
            [
                "파일",
                common.mono(f"{Path(run_dir).name}/report.md") if run_dir else "—",
            ],
        ],
    )


def render_report_chapters(chapters: List[Tuple[int, str, str]]) -> None:
    """장별로 나눠 보여준다. 사람이 쓸 장은 접어 두고 배지를 붙인다.

    표는 마크다운 그대로 렌더링한다 — `st.dataframe`은 canvas 기반이라 문서 표의
    서식을 살릴 수 없다 (DESIGN.md 1절).
    """
    empty = empty_slots()
    # 장 전체가 빈 것과 장 안의 절만 빈 것을 구분한다. 1장은 대부분 자동 생성이라
    # 1-1이 비었다고 장을 접으면 자동으로 만든 내용까지 숨는다.
    whole = {manual_sections.chapter_of(slot) for slot in empty if manual_sections.is_whole_chapter(slot)}
    partial: Dict[int, List[str]] = {}
    for slot in empty:
        if not manual_sections.is_whole_chapter(slot):
            partial.setdefault(manual_sections.chapter_of(slot), []).append(slot)

    for number, title, body in chapters:
        if number in whole:
            common.render_badges(
                common.status_badge(common.WARN, "작성 필요"),
                f'<span style="font-weight:700;">{number}. {common.esc(title)}</span>',
            )
            # 라벨에 장 번호를 넣는다. 사람이 쓰는 장이 셋이라 "내용 보기"만 두면
            # 목록에서 같은 이름이 세 번 나온다.
            with st.expander(f"내용 보기 ({number}장)", expanded=False):
                st.markdown(body or "(비어 있습니다.)")
            continue

        with st.expander(f"{number}. {title}", expanded=True):
            for slot in partial.get(number, []):
                common.render_badges(
                    common.status_badge(common.WARN, "절 작성 필요"),
                    f'<span style="font-size:.85rem;">'
                    f"{common.esc(manual_sections.slot_label(slot))}</span>",
                )
            st.markdown(body or "(비어 있습니다.)")


@st.cache_data(show_spinner="PDF를 만드는 중…")
def cached_pdf_bytes(markdown: str) -> bytes:
    """같은 markdown이면 항상 같은 PDF가 나온다(CLAUDE.md 5-5) — 다시 만들 필요가 없다."""
    return report.build_pdf(markdown)


def report_pdf_bytes(run_dir: str, markdown: str) -> Optional[bytes]:
    """report.pdf 바이트를 돌려준다. 있으면 파일 그대로, 없으면 만들어 저장한다.

    **이미 나온 결과는 다시 계산하지 않는다.** CLI(run_pipeline.py)가 이미 만든
    PDF가 있으면(또는 불러온 실행이면) 그 파일을 그대로 쓴다 — 같은 markdown이라도
    재계산이 화면 결과와 CLI 결과를 갈라놓을 이유는 없다.
    """
    path = Path(run_dir) / "report.pdf"
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass

    if not report.fonts_available():
        return None

    try:
        pdf_bytes = cached_pdf_bytes(markdown)
    except Exception as exc:
        st.session_state.pdf_error = f"PDF를 만들지 못했습니다: {type(exc).__name__}: {exc}"
        return None

    try:
        path.write_bytes(pdf_bytes)
    except OSError:
        pass  # 저장은 안 돼도 다운로드는 여전히 가능하다
    return pdf_bytes


def render_step6(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    with st.container():
        render_step_header(STEPS[5])

        if st.session_state.step < 6:
            st.caption("5단계 확인이 끝나면 열립니다.")
            return
        if metrics_catalog is None or schema_catalog is None:
            st.error("카탈로그가 없어 리포트를 만들 수 없습니다.")
            return

        if st.session_state.report_error:
            st.error(st.session_state.report_error)

        render_manual_editor(metrics_catalog, schema_catalog)

        markdown = st.session_state.report_markdown
        if not markdown:
            st.button(
                "리포트 생성",
                key="generate_report",
                type="primary",
                on_click=generate_report,
                args=(metrics_catalog, schema_catalog),
            )
            st.caption(
                "위 편집란을 채우면 그 내용이 리포트에 들어갑니다. "
                "비워 두면 '사람이 작성합니다' 자리표시자가 남습니다."
            )
            return

        st.success(f"리포트 생성됨 — {Path(st.session_state.run_dir) / 'report.md'}")

        preface, chapters, self_check = split_report(markdown)
        render_report_info(markdown, metrics_catalog)

        # 병합 결과를 기준으로 센다. 사람이 채운 자리는 미작성에서 빠져야 한다.
        outcome = st.session_state.manual_outcome or {}
        filled = list(outcome.get("치환") or [])
        left = empty_slots()

        if filled:
            common.render_badges(
                common.status_badge(common.OK, f"작성 완료 {len(filled)}곳"),
                f'<span style="font-size:.85rem;">'
                f"{common.esc(', '.join(manual_sections.slot_label(slot) for slot in filled))}</span>",
            )
        if left:
            names = ", ".join(manual_sections.slot_label(slot) for slot in left)
            common.render_badges(
                common.status_badge(common.WARN, f"미작성 {len(left)}곳"),
                f'<span style="font-size:.85rem;">{common.esc(names)}</span>',
            )
            st.warning(
                f"{len(left)}곳이 미작성 상태입니다 — {names}. "
                "자동 생성하지 않는 자리이며, 사람이 채워야 리포트가 완성됩니다."
            )

        if preface:
            with st.expander("머리말", expanded=False):
                st.markdown(preface)

        render_report_chapters(chapters)

        if self_check:
            with st.expander("(개발용) 금지 표현 자체 검사", expanded=False):
                st.markdown(self_check.replace(SELF_CHECK_MARK, "").strip())

        st.divider()

        pdf_ready = report.fonts_available()
        if not pdf_ready:
            st.warning(
                "PDF용 한글 폰트가 없어 PDF를 만들 수 없습니다. 마크다운 리포트는 "
                "그대로 받을 수 있습니다. 아래 명령으로 받은 뒤 다시 열어보세요."
            )
            st.code("python scripts/get_fonts.py", language="bash")
        if st.session_state.pdf_error:
            st.warning(st.session_state.pdf_error)

        pdf_bytes = report_pdf_bytes(st.session_state.run_dir, markdown) if pdf_ready else None

        period_label = calculate.month_of(
            ((st.session_state.data_profile or {}).get("기간") or {}).get("최대")
        ) or "report"
        left, mid, right = st.columns([1, 1, 1])
        left.download_button(
            "마크다운 다운로드",
            data=markdown.encode("utf-8"),
            file_name=f"report_{period_label}.md",
            mime="text/markdown",
            key="download_report",
        )
        mid.download_button(
            "PDF 다운로드",
            data=pdf_bytes or b"",
            file_name=f"report_{period_label}.pdf",
            mime="application/pdf",
            key="download_report_pdf",
            disabled=pdf_bytes is None,
        )
        # 확정 뒤에 다시 만들면 확정본과 화면이 어긋난다. 새 실행으로만 바꾼다.
        right.button(
            "리포트 재생성",
            key="generate_report",
            disabled=is_approved(),
            on_click=generate_report,
            args=(metrics_catalog, schema_catalog),
        )
        st.caption(f"{len(markdown):,}자 · 장 {len(chapters)}개 — 다음은 7단계 이메일 초안입니다.")



# ── 7단계 화면: 이메일 초안 ───────────────────────────────────────────
def generate_email_draft(metrics_catalog: dict, schema_catalog: dict) -> None:
    """초안을 만들어 run 폴더에 세 파일로 남긴다.

    **세 파일로 나누는 이유**: `email.html`은 사람이 브라우저로 확인하는 것,
    `email.txt`는 HTML을 못 읽는 클라이언트용, `email_meta.json`은 제목·수신자·
    첨부처럼 8주차에 SMTP가 읽어야 할 값이다. 한 파일에 섞으면 발송 코드가
    HTML을 파싱해서 제목을 꺼내야 한다.
    """
    st.session_state.email_error = None
    run_dir = st.session_state.get("run_dir")
    markdown = st.session_state.report_markdown
    if not run_dir:
        st.session_state.email_error = "실행 폴더가 없어 초안을 저장할 수 없습니다."
        return
    if not markdown:
        st.session_state.email_error = "리포트가 없어 초안을 만들 수 없습니다."
        return

    context = run_context(metrics_catalog, schema_catalog)
    context["run_dir"] = run_dir
    try:
        draft = email_draft.build_email(context, markdown)
    except Exception as exc:
        st.session_state.email_error = (
            f"초안을 만들지 못했습니다: {type(exc).__name__}: {exc}"
        )
        return

    folder = Path(run_dir)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    meta = {
        "생성시각": stamp,
        "subject": draft["subject"],
        "to": draft["to"],
        "from": draft["from"],
        "attachments": draft["attachments"],
        # 발송하지 않았음을 파일 안에 남긴다. 파일만 보고 "보냈다"로 읽히지 않게 한다.
        "발송여부": "발송하지 않음 (6주차 범위는 초안·확정까지)",
    }
    try:
        (folder / "email.html").write_text(draft["body_html"], encoding="utf-8")
        (folder / "email.txt").write_text(draft["body_text"], encoding="utf-8")
        (folder / "email_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str) + chr(10),
            encoding="utf-8",
        )
        payload = runlog.email_section(
            draft,
            manual_sections.pending_slots(markdown),
            meta["발송여부"],
        )
        runlog.write(run_dir, runlog.EMAIL, payload, stamp)
    except OSError as exc:
        st.session_state.email_error = f"초안을 저장하지 못했습니다: {exc}"
        return

    st.session_state.email_draft = draft
    if st.session_state.step < 8:
        st.session_state.step = 8  # 8단계 준비됨


def render_email_header(draft: Dict[str, Any]) -> None:
    """제목·수신·발신. 메일 클라이언트에서 보이는 순서대로 둔다."""
    common.render_table(
        ["항목", "값"],
        [
            ["제목", f"<b>{common.esc(draft['subject'])}</b>"],
            ["수신", common.mono(", ".join(draft["to"]))],
            ["발신", common.mono(draft["from"])],
        ],
    )
    st.caption(
        "수신자는 config.EMAIL_TO의 예시값입니다. 실제 주소를 코드에 두지 않습니다."
    )


def render_email_attachments(draft: Dict[str, Any]) -> None:
    """첨부 목록. 없는 파일을 목록에서 빼지 않고 rose로 표시한다.

    조용히 빼면 받는 사람이 "PDF는 원래 안 오는 것"인지 "이번에 빠진 것"인지
    알 수 없다. 메일 본문도 같은 색을 쓴다 (DESIGN.md — 화면과 문서의 시각 언어 일치).
    """
    rows, styles = [], []
    for item in draft["attachments"]:
        exists = bool(item["exists"])
        size = item["size"]
        rows.append(
            [
                common.mono(item["filename"]),
                common.format_kb(size) if size is not None else common.muted("—"),
                common.status_badge(
                    common.OK if exists else common.BLOCK,
                    "있음" if exists else "없음",
                ),
            ]
        )
        styles.append("" if exists else f"background:{common.COLOR_BLOCK}14;")
    common.render_table(["파일명", "크기", "존재"], rows, styles)

    missing = [item["filename"] for item in draft["attachments"] if not item["exists"]]
    if missing:
        st.caption(
            f"없는 파일 · {', '.join(missing)} — 목록에는 남깁니다. "
            "빼면 받는 사람이 원래 없는 것인지 이번에 빠진 것인지 알 수 없습니다."
        )
    st.caption("이번 초안은 파일을 실제로 붙이지 않습니다. 첨부는 8주차입니다.")


def render_step7(metrics_catalog: Optional[dict], schema_catalog: Optional[dict]) -> None:
    with st.container():
        render_step_header(STEPS[6])

        if st.session_state.step < 7:
            st.caption("6단계 리포트가 생성되면 열립니다.")
            return
        if metrics_catalog is None or schema_catalog is None:
            st.error("카탈로그가 없어 초안을 만들 수 없습니다.")
            return

        if st.session_state.email_error:
            st.error(st.session_state.email_error)

        draft = st.session_state.email_draft
        if not draft:
            st.button(
                "이메일 초안 생성",
                key="generate_email",
                type="primary",
                on_click=generate_email_draft,
                args=(metrics_catalog, schema_catalog),
            )
            st.caption(
                "제목·수신자·본문·첨부 목록을 만들어 run 폴더에 저장합니다. "
                "메일 서버에 연결하지 않습니다."
            )
            return

        run_dir = Path(st.session_state.run_dir)
        st.success(f"초안 생성됨 — {run_dir / 'email.html'}")

        # ── 경고: 제목이 왜 그렇게 붙었는지 화면에서 알 수 있게 ──
        pending = manual_sections.pending_slots(st.session_state.report_markdown or "")
        if pending:
            names = ", ".join(manual_sections.slot_label(slot) for slot in pending)
            st.warning(
                f"리포트 {len(pending)}곳이 미작성 상태입니다 — {names}. "
                "제목에 (초안)이 붙습니다."
            )
        validation = st.session_state.validation or {}
        warn_count = int(validation.get("경고수") or 0)
        if warn_count:
            st.warning(
                f"검증 경고 {warn_count}건이 있습니다. 제목에 (확인 필요)가 붙습니다."
            )

        section_title("메일 헤더")
        render_email_header(draft)

        section_title("본문 미리보기")
        # 높이를 주지 않으면 iframe이 0px로 접힌다. 스크롤을 켜서 긴 본문도 다 보이게 한다.
        components.html(draft["body_html"], height=600, scrolling=True)
        st.caption(
            "실제 메일 본문 HTML을 그대로 렌더링한 것입니다. "
            "폭 600px·인라인 스타일 기준으로 만들었습니다."
        )
        with st.expander("텍스트 버전 보기", expanded=False):
            st.code(draft["body_text"], language=None)

        section_title("첨부 목록")
        render_email_attachments(draft)

        st.divider()
        period_label = current_period() or "draft"
        left, right = st.columns([1, 1])
        left.download_button(
            "HTML 다운로드",
            data=draft["body_html"].encode("utf-8"),
            file_name=f"email_{period_label}.html",
            mime="text/html",
            key="download_email",
        )
        # 확정본을 고정한 뒤에 초안을 갈아치우면 무엇을 확정했는지 알 수 없다.
        right.button(
            "초안 다시 생성",
            key="regenerate_email",
            disabled=is_approved(),
            on_click=generate_email_draft,
            args=(metrics_catalog, schema_catalog),
        )
        st.caption(
            f"{run_dir.name}/email.html · email.txt · email_meta.json — "
            "발송 확정은 8단계입니다. 이 앱은 메일을 보내지 않습니다."
        )



# ── 8단계 화면: 발송 확정 게이트 ──────────────────────────────────────
#: 확정 마커 파일. 있으면 그 실행은 확정된 것이다.
APPROVED_NAME = "APPROVED"


def is_approved() -> bool:
    """이 실행이 확정됐는가.

    확정 뒤에는 리포트·초안을 다시 만들 수 없게 막는 데도 쓴다. 재생성을 열어 두면
    `email_final.html`은 옛 내용인데 화면은 새 내용인 상태가 되고, 확정의 의미가 사라진다.
    """
    return bool(st.session_state.approval)


def approval_items(
    draft: Dict[str, Any], validation: Dict[str, Any], pending: Sequence[str]
) -> List[Dict[str, str]]:
    """확정 전에 물어야 할 것.

    **해당 없는 항목은 만들지 않는다.** 경고가 0건인데 "경고 0건을 확인했다"를
    체크하게 하면 체크가 형식이 되고, 정작 봐야 할 항목도 같이 흘려보내게 된다.
    """
    attachments = draft.get("attachments") or []
    missing = [item for item in attachments if not item.get("exists")]

    items: List[Dict[str, str]] = [
        {
            "id": "period",
            "label": f"대상 기간이 맞다 — {current_period() or '알 수 없음'}",
        },
        {
            "id": "recipients",
            "label": f"수신자가 맞다 — {', '.join(draft.get('to') or []) or '없음'}",
        },
    ]

    warn_count = int((validation or {}).get("경고수") or 0)
    if warn_count:
        items.append(
            {"id": "warnings", "label": f"검증 경고 {warn_count}건을 확인했다"}
        )

    if pending:
        # 교안의 강한 문구를 쓴다. "알고 있다"보다 "비어 있는 상태로 발송하는 것을
        # 확인했습니다"가 무엇에 동의하는지 분명하다.
        numbers = "·".join(
            f"{slot}절" if not manual_sections.is_whole_chapter(slot) else f"{slot}장"
            for slot in pending
        )
        titles = "·".join(manual_sections.SLOT_TITLES.get(slot, slot) for slot in pending)
        phrase = f"{numbers}({titles})"
        items.append(
            {
                "id": "pending",
                "label": f"{phrase}{phrasing.subject_particle(phrase)} 비어 있는 상태로 "
                "발송하는 것을 확인했습니다",
            }
        )

    label = f"첨부 파일 목록을 확인했다 — {len(attachments)}개"
    if missing:
        label += f" (없는 파일 {len(missing)}개: {', '.join(i['filename'] for i in missing)})"
    items.append({"id": "attachments", "label": label})
    return items


def confirm_send(
    items: Sequence[Dict[str, str]],
    draft: Dict[str, Any],
    validation: Dict[str, Any],
    pending: Sequence[str],
) -> None:
    """발송 확정 — 최종본을 고정하고 확정 기록을 남긴다.

    **메일을 보내지 않는다.** 하는 일은 "이 내용으로 보낸다"를 파일로 못박는 것이다.
    `email_final.html`을 따로 두는 이유는, 나중에 초안을 다시 만들어도 확정본은
    그대로 남아야 하기 때문이다.
    """
    st.session_state.approval_error = None
    run_dir = st.session_state.get("run_dir")
    if not run_dir:
        st.session_state.approval_error = "실행 폴더가 없어 확정할 수 없습니다."
        return

    folder = Path(run_dir)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    record = {
        "발송확정_시각": stamp,
        "확인한_체크항목": [item["label"] for item in items],
        "제목": draft.get("subject"),
        "수신자": list(draft.get("to") or []),
        "발신자": draft.get("from"),
        "미작성_자리": [manual_sections.slot_label(slot) for slot in pending],
        "검증_경고수": int((validation or {}).get("경고수") or 0),
        "검증_차단수": int((validation or {}).get("차단수") or 0),
        "첨부_목록": [
            {"파일명": item["filename"], "크기": item["size"], "존재": item["exists"]}
            for item in (draft.get("attachments") or [])
        ],
        "확정본": "email_final.html",
        "발송여부": "발송하지 않음 — 이 앱은 메일을 보내지 않습니다 (8주차에 구현)",
    }

    try:
        (folder / "email_final.html").write_text(
            draft.get("body_html") or "", encoding="utf-8"
        )
        (folder / APPROVED_NAME).write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str) + chr(10),
            encoding="utf-8",
        )
        runlog.write(run_dir, runlog.APPROVE, record, stamp)
        # 8주차 자리: 확정본이 여기서 고정되는 지점이 실제 발송을 걸 자리다.
        #   from pipeline import send
        #   send.send_email(draft, draft.get("attachments") or [])
        # 6주차 범위는 확정 저장까지이므로 지금은 부르지 않는다 (CLAUDE.md 5-3·9절).
    except OSError as exc:
        st.session_state.approval_error = f"확정을 저장하지 못했습니다: {exc}"
        return

    st.session_state.approval = record
    # 8단계를 완료로 만든다. STEPS는 8개까지이므로 9는 "완주"를 뜻한다.
    st.session_state.step = 9


def render_approved() -> None:
    """확정 완료 화면. 되돌리기 버튼을 만들지 않는다.

    되돌릴 수 있으면 확정의 의미가 없다. 잘못했으면 새 실행을 시작해서 새 확정본을
    만들고, 이전 확정 기록은 그대로 남긴다 — 기록을 지우지 않는 것이 감사의 기본이다.
    """
    record = st.session_state.approval or {}
    folder = Path(st.session_state.run_dir)

    common.render_badges(
        common.status_badge(common.OK, "확정 완료"),
        f'<span style="font-size:.88rem;">'
        f"{common.esc(common.format_timestamp(record.get('발송확정_시각')))}</span>",
    )
    common.render_table(
        ["항목", "값"],
        [
            ["제목", f"<b>{common.esc(record.get('제목'))}</b>"],
            ["수신", common.mono(", ".join(record.get("수신자") or []))],
            ["확정본", common.mono(str(folder / "email_final.html"))],
            ["확정 마커", common.mono(str(folder / APPROVED_NAME))],
            ["실행 기록", common.mono(str(folder / "run_log.json"))],
        ],
    )

    left = record.get("미작성_자리") or []
    if left:
        st.warning(
            f"미작성 {len(left)}곳을 그대로 두고 확정했습니다 — {', '.join(left)}. "
            "제목에 (초안)이 붙은 상태로 고정됐습니다."
        )

    section_title("확인한 항목")
    for label in record.get("확인한_체크항목") or []:
        st.markdown(f"- {common.esc(label)}")

    st.divider()
    st.caption(
        "이 앱은 메일을 보내지 않았습니다. 확정이 한 일은 발송 준비 최종본을 "
        "고정해 저장한 것입니다 — SMTP 연결은 8주차입니다."
    )
    st.button(
        "새 실행 시작",
        key="restart_run",
        on_click=discard_from_step1,
    )
    st.caption(
        "되돌리기 버튼은 만들지 않습니다. 확정을 되돌릴 수 있으면 확정의 의미가 "
        "없어집니다. 이전 확정 기록은 지우지 않고 남깁니다."
    )


def render_step8() -> None:
    with st.container():
        render_step_header(STEPS[7])

        validation = st.session_state.validation or {}
        if validation.get("전체판정") == validate.BLOCK:
            # 차단이면 확정 버튼 자체를 만들지 않는다. 우회 경로를 두지 않기 위해서다.
            common.render_badges(
                common.status_badge(common.BLOCK, "검증 차단"),
                f'<span style="font-size:.88rem;">'
                f"차단 {validation.get('차단수', 0)}건이 해결되어야 확정할 수 있습니다.</span>",
            )
            st.caption(
                "확정 버튼을 만들지 않았습니다. 차단된 검증 결과를 발송 준비 상태로 "
                "고정하는 경로는 두지 않습니다."
            )
            return

        if is_approved():
            render_approved()
            return

        if st.session_state.step < 8:
            st.caption("7단계 이메일 초안이 만들어지면 열립니다.")
            return

        draft = st.session_state.email_draft
        if not draft:
            st.caption("이메일 초안이 없습니다. 7단계에서 초안을 먼저 만드세요.")
            return

        if st.session_state.approval_error:
            st.error(st.session_state.approval_error)

        pending = manual_sections.pending_slots(st.session_state.report_markdown or "")
        if pending:
            st.info(
                "초안 공유가 목적일 수 있으므로 차단하지 않습니다. "
                "대신 제목에 (초안)이 표시됩니다."
            )

        items = approval_items(draft, validation, pending)
        st.caption(
            f"아래 {len(items)}개를 모두 확인해야 확정할 수 있습니다. "
            "다른 게이트보다 항목이 많습니다 — 확정은 되돌릴 수 없습니다."
        )

        checked: List[bool] = []
        for item in items:
            checked.append(st.checkbox(item["label"], key=f"approve_{item['id']}"))

        skipped = {"warnings", "pending"} - {item["id"] for item in items}
        if skipped:
            labels = {"warnings": "검증 경고", "pending": "미작성 자리"}
            st.caption(
                "해당 없어 묻지 않은 항목 · "
                + ", ".join(labels[name] for name in sorted(skipped))
            )

        st.divider()
        st.warning(
            "확정하면 발송 준비 최종본이 저장됩니다. "
            "이 앱은 실제로 메일을 보내지 않습니다(8주차에 구현)."
        )
        # 되돌릴 수 없음을 색으로도 알린다. rose는 차단·경고와 같은 축의 색이다
        # (CLAUDE.md 7절). type="primary"는 테마 색이라 이 의미를 못 낸다.
        st.markdown(
            "<style>"
            f'div.st-key-approve_send button {{background:{common.COLOR_BLOCK};'
            f"border-color:{common.COLOR_BLOCK};color:#fff;font-weight:700;}}"
            "div.st-key-approve_send button:hover{background:#e11d48;"
            "border-color:#e11d48;color:#fff;}"
            "</style>",
            unsafe_allow_html=True,
        )
        st.button(
            "발송 확정",
            key="approve_send",
            disabled=not all(checked),
            on_click=confirm_send,
            args=(items, draft, validation, pending),
        )
        if not all(checked):
            st.caption(
                f"확인 {sum(checked)}/{len(checked)} — 모두 체크해야 확정할 수 있습니다."
            )



# ── 실행 기록 화면 ────────────────────────────────────────────────────
def render_run_log() -> None:
    """`run_log.json`을 사람이 읽을 표로 펼친다.

    JSON을 그대로 띄우면 아무도 안 읽는다. 구역 순서와 무엇을 펼칠지는 runlog가
    정하므로, 기록에 구역이 늘어도 이 함수는 안 고친다.
    """
    run_dir = st.session_state.get("run_dir") or st.session_state.get("last_run_dir")
    if not run_dir:
        return

    log = runlog.load(run_dir)
    if not log:
        return

    sections = runlog.screen_rows(log)
    total = runlog.total_seconds(log)
    label = f"실행 기록 보기 — {len(sections)}개 구역"
    if total is not None:
        label += f" · 전체 {total:,.0f}초"

    with st.expander(label, expanded=False):
        st.caption(
            f"{Path(run_dir).name}/{runlog.FILENAME} — "
            "3개월 뒤 \"이 숫자 어떻게 나왔나요\"에 이 파일 하나로 답할 수 있어야 합니다."
        )

        elapsed = runlog.elapsed_rows(log)
        if elapsed:
            section_title("단계별 시각")
            common.render_table(
                ["구역", "기록 시각", "앞 단계 이후"],
                [[common.esc(a), common.mono(b), common.esc(c)] for a, b, c in elapsed],
            )
            st.caption(
                "게이트가 낀 구간에는 사람이 화면을 보는 시간이 들어 있습니다 — "
                "처리 시간이 아닙니다."
            )

        for title, rows in sections:
            section_title(title)
            if not rows:
                st.caption("기록된 항목이 없습니다.")
                continue
            common.render_table(
                ["항목", "값"],
                [[common.esc(key), common.esc(value)] for key, value in rows],
            )

        st.divider()
        st.download_button(
            "run_log.json 다운로드",
            data=json.dumps(log, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
            file_name=f"{Path(run_dir).name}_run_log.json",
            mime="application/json",
            key="download_run_log",
        )


# ── 아직 만들지 않은 단계 ─────────────────────────────────────────────
def render_pending_step(step: Step) -> None:
    with st.container():
        render_step_header(step)

        if step_status(step.number) == WAITING:
            st.caption(f"{step.number - 1}단계가 끝나면 열립니다.")
        else:
            st.caption("차례가 되었지만 이 단계는 아직 구현되지 않았습니다.")


def main() -> None:
    init_state()

    metrics_catalog, metrics_error = load_catalog(config.METRICS_CATALOG_PATH)
    schema_catalog, schema_error = load_catalog(config.SCHEMA_CATALOG_PATH)
    render_sidebar(metrics_catalog, metrics_error, schema_catalog, schema_error)

    st.title("월간 리포트 자동화")
    st.caption(
        "파일 하나를 넣으면 지표 계산부터 이메일 초안까지 흐릅니다. "
        "사람은 넣기 한 번, 승인 세 번만 합니다."
    )

    render_demo_banner()

    for problem in config.config_warnings():
        st.warning(problem)

    render_step1()
    render_step2(metrics_catalog, schema_catalog)
    render_gate(metrics_catalog, schema_catalog)
    render_calculation(metrics_catalog, schema_catalog)
    render_step3()
    render_step4(metrics_catalog, schema_catalog)
    render_step5()
    render_step6(metrics_catalog, schema_catalog)
    render_step7(metrics_catalog, schema_catalog)
    render_step8()
    render_run_log()
    for step in STEPS[IMPLEMENTED_THROUGH:]:
        render_pending_step(step)


main()
