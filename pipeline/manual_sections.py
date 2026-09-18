"""사람이 쓰는 자리(1-1·2·5·6)를 관리한다.

리포트의 2·5·6장과 1-1절은 자동으로 만들지 않는다 (CLAUDE.md 1절). 그렇다고 매달 손으로
리포트 파일을 고치면, 다시 생성할 때마다 사람이 쓴 글이 날아간다. 그래서 사람이 쓰는
글은 **`manual/sections.md`에 따로 두고**, 리포트를 만들 때 자리표시자에 끼워 넣는다.

    manual/sections.md   사람이 편집하는 유일한 파일
          │  load_manual(period)
          ▼
    {"1-1": "...", "2": "...", "5": "...", "6": "..."}
          │  merge_into_report(report_md, manual)
          ▼
    리포트의 "> 이 장은 사람이 작성합니다." 자리에 삽입

**자리를 장 번호가 아니라 슬롯 id로 센다.** 1장 핵심 시사점은 `### 1-1.`이라는 절이고
장 전체가 사람 몫이 아니다. 장 번호만 쓰면 이 자리를 가리킬 수 없어, 리포트에는 빈
자리가 있는데 편집란에는 채울 칸이 없는 상태가 된다. 슬롯 id는 `"2"`처럼 장 하나이거나
`"1-1"`처럼 장 안의 절이다.

**내용이 없는 자리는 자리표시자를 그대로 남긴다.** 빈 글로 덮으면 "썼는데 할 말이
없다"로 읽히고, 무엇이 미작성인지 화면에서 알 수 없게 된다.

**기간이 어긋나면 경고한다.** 지난달에 쓴 글이 이번 달 리포트에 조용히 들어가면,
읽는 사람은 이번 달을 보고 쓴 글이라고 믿는다.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

#: 사람이 쓰는 자리. 리포트에 나오는 순서대로 둔다.
#: `"1-1"`은 1장 안의 절, 나머지는 장 하나 전체다 (report.HUMAN_SECTIONS와 같은 집합).
MANUAL_SLOTS: Tuple[str, ...] = ("1-1", "2", "5", "6")

SLOT_TITLES: Dict[str, str] = {
    "1-1": "핵심 시사점",
    "2": "배경·목적",
    "5": "원인 분석",
    "6": "개선 제안",
}

#: 각 자리에 무엇을 쓸지. 템플릿에 HTML 주석으로 남긴다 — 마크다운 출력에 보이지 않는다.
SLOT_HINTS: Dict[str, str] = {
    "1-1": "위 사실 중 무엇이 중요한지, 어디에 주의를 둘지. 중요도는 조직의 목표에 달렸다",
    "2": "이 리포트를 왜 만드는가, 누가 읽는가, 어떤 결정에 쓰이는가",
    "5": '아래 "참고 — 위키에서 찾은 관련 분석"을 근거로 원인을 판정',
    "6": "우선순위와 근거. 실행 가능성·비용을 함께",
}

#: 작성일이 이만큼 지나면 오래됐다고 알린다.
STALE_DAYS = 60

_FRONTMATTER_RE = re.compile(r"^---\s*?\r?\n(.*?)\r?\n---\s*?(?:\r?\n|$)(.*)$", re.S)

#: 슬롯 제목 줄. `## 2. 배경·목적`과 `### 1-1. 핵심 시사점`을 모두 잡는다.
#: 리포트는 절을 `###`로, 편집 파일은 `##`로 쓰므로 깊이를 고정하지 않는다.
_SLOT_RE = re.compile(r"^#{2,3}\s*(\d+(?:-\d+)?)\.\s*(.*)$", re.MULTILINE)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_MONTH_RE = re.compile(r"(\d{4})-(\d{2})")

#: 리포트의 자리표시자. 첫 줄과 뒤따르는 인용 줄 전체를 한 덩어리로 잡는다.
_PLACEHOLDER_RE = re.compile(
    r"^>\s*이 (?:장|절)은 사람이 작성합니다\.[^\n]*(?:\n>[^\n]*)*", re.MULTILINE
)


def chapter_of(slot: Any) -> int:
    """슬롯 id가 속한 장 번호. `"1-1"` → 1, `"5"` → 5."""
    return int(str(slot).split("-")[0])


def is_whole_chapter(slot: Any) -> bool:
    """장 전체가 사람 몫인가. `"5"`는 참, `"1-1"`은 거짓.

    화면에서 갈리는 지점이다. 장 전체가 사람 몫이면 접어 둬도 되지만, 1장은 대부분
    자동 생성이라 접으면 자동으로 만든 내용까지 숨는다.
    """
    return "-" not in str(slot)


def slot_key(slot: Any) -> Tuple[int, ...]:
    """정렬 기준. 문자열 정렬은 10장이 2장보다 앞에 오므로 숫자로 쪼갠다."""
    return tuple(int(part) for part in str(slot).split("-"))


def slot_label(slot: Any) -> str:
    """`"1-1. 핵심 시사점"` 꼴. 화면·경고·이메일이 같은 이름을 쓰게 한다."""
    key = str(slot)
    title = SLOT_TITLES.get(key)
    return f"{key}. {title}" if title else key


def _month_of(value: Any) -> Optional[str]:
    match = _MONTH_RE.search(str(value or ""))
    return f"{match.group(1)}-{match.group(2)}" if match else None


def _strip_comments(text: str) -> str:
    """편집 힌트(HTML 주석)를 없앤다. 힌트는 리포트에 실릴 내용이 아니다."""
    return _COMMENT_RE.sub("", str(text or "")).strip()


def template_text(period: Any = None, today: Optional[dt.date] = None) -> str:
    """빈 템플릿. 프론트매터에 대상기간을 미리 채워 둔다."""
    day = today or dt.date.today()
    month = _month_of(period) or "(YYYY-MM)"
    lines = [
        "---",
        f"작성일: {day.isoformat()}",
        f"대상기간: {month}",
        "작성자: (이름)",
        "---",
        "",
        "<!-- 이 파일의 각 절이 리포트의 같은 번호 자리에 들어갑니다.",
        "     1-1은 1장 안의 절이고, 2·5·6은 장 전체입니다.",
        "     비워 두면 리포트에 '사람이 작성합니다' 자리표시자가 그대로 남습니다.",
        "     이런 주석은 리포트에 실리지 않습니다. -->",
        "",
    ]
    for slot in sorted(MANUAL_SLOTS, key=slot_key):
        lines += [
            f"## {slot_label(slot)}",
            "",
            f"<!-- {SLOT_HINTS[slot]} -->",
            "",
        ]
    return "\n".join(lines)


def ensure_template(
    path: Optional[Path] = None, period: Any = None, today: Optional[dt.date] = None
) -> Tuple[Path, bool]:
    """파일이 없으면 템플릿을 만든다. (경로, 새로 만들었는가)를 돌려준다.

    **있는 파일을 덮지 않는다.** 사람이 쓴 글을 템플릿으로 지우는 일이 있으면
    이 구조를 아무도 못 믿는다.
    """
    target = Path(path) if path else config.MANUAL_SECTIONS_PATH
    if target.exists():
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template_text(period, today), encoding="utf-8")
    return target, True


def save_manual(
    text: str,
    period: Any = None,
    path: Optional[Path] = None,
    today: Optional[dt.date] = None,
    author: Optional[str] = None,
) -> Path:
    """편집한 내용을 저장하면서 프론트매터의 작성일·대상기간을 지금 값으로 맞춘다.

    **저장할 때 갱신하는 이유**: 사람이 글은 고치고 날짜는 그대로 두는 일이 반드시
    생긴다. 그러면 "이전 기간 내용입니다" 경고가 계속 뜨거나, 반대로 지난달 글이
    이번 달 글로 위장된다. 저장 시점이 곧 작성 시점이므로 여기서 맞춘다.

    프론트매터가 없으면 새로 붙인다. 본문은 손대지 않는다.
    """
    target = Path(path) if path else config.MANUAL_SECTIONS_PATH
    day = (today or dt.date.today()).isoformat()
    month = _month_of(period) or "(YYYY-MM)"

    body = str(text or "")
    match = _FRONTMATTER_RE.match(body)
    meta: Dict[str, str] = {}
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        body = match.group(2)

    meta["작성일"] = day
    meta["대상기간"] = month
    if author:
        meta["작성자"] = author
    meta.setdefault("작성자", "(이름)")

    order = ["작성일", "대상기간", "작성자"]
    lines = ["---"]
    lines += [f"{key}: {meta[key]}" for key in order if key in meta]
    lines += [f"{key}: {value}" for key, value in meta.items() if key not in order]
    lines += ["---", ""]

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + body.lstrip("\n"), encoding="utf-8")
    return target


def read_raw(path: Optional[Path] = None, period: Any = None) -> Tuple[Path, str]:
    """편집 화면에 띄울 원문. 파일이 없으면 템플릿을 만들어 그 내용을 준다."""
    target, _created = ensure_template(path, period)
    try:
        return target, target.read_text(encoding="utf-8-sig")
    except OSError:
        return target, template_text(period)


def _parse(text: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(프론트매터, {슬롯id: 내용})으로 자른다. YAML 없이 단순 key: value만 읽는다."""
    body = text
    meta: Dict[str, str] = {}
    match = _FRONTMATTER_RE.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        body = match.group(2)

    slots: Dict[str, str] = {}
    found = list(_SLOT_RE.finditer(body))
    for index, heading in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(body)
        content = _strip_comments(body[heading.end() : end])
        if content:
            slots[heading.group(1)] = content
    return meta, slots


def load_manual(
    period: Any = None,
    path: Optional[Path] = None,
    today: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """`manual/sections.md`를 읽어 자리별 내용과 경고를 돌려준다.

    반환 키
        장       {슬롯id: 내용} — 내용이 비어 있는 자리는 넣지 않는다
        메타     작성일·대상기간·작성자
        경고     사람이 읽을 문구 목록
        파일     읽은 경로
        생성됨   이번 호출에서 템플릿을 새로 만들었는가

    `today`는 검사용으로 받는다. 기본값은 오늘이며, 오래됨 판정에만 쓴다 —
    계산에는 현재 시각을 쓰지 않는다 (CLAUDE.md 5-5).
    """
    target, created = ensure_template(path, period, today)
    warnings: List[str] = []
    ordered = sorted(MANUAL_SLOTS, key=slot_key)

    if created:
        return {
            "장": {},
            "메타": {},
            "경고": [
                f"사람이 쓰는 자리를 담을 템플릿을 만들었습니다: {target}. "
                f"{', '.join(slot_label(slot) for slot in ordered)}을 채운 뒤 "
                "리포트를 다시 생성하세요."
            ],
            "파일": target,
            "생성됨": True,
        }

    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {
            "장": {},
            "메타": {},
            "경고": [f"{target}를 읽지 못했습니다: {exc}"],
            "파일": target,
            "생성됨": False,
        }

    meta, slots = _parse(text)

    # ── 기간 대조 ──
    wanted = _month_of(period)
    written = _month_of(meta.get("대상기간"))
    if wanted and written and wanted != written:
        warnings.append(
            f"이전 기간({written}) 내용입니다. 이번 리포트의 대상 기간은 {wanted}입니다. "
            "그대로 쓰면 지난달을 보고 쓴 글이 이번 달 리포트에 들어갑니다."
        )
    elif wanted and not written:
        warnings.append(
            f"`대상기간`이 비어 있어 이번 기간({wanted})의 글인지 확인할 수 없습니다."
        )

    # ── 작성일 확인 ──
    day = today or dt.date.today()
    written_day = None
    raw_day = str(meta.get("작성일") or "").strip()
    if raw_day:
        try:
            written_day = dt.date.fromisoformat(raw_day[:10])
        except ValueError:
            warnings.append(f"`작성일`을 날짜로 읽지 못했습니다: {raw_day!r}")
    if written_day:
        age = (day - written_day).days
        if age >= STALE_DAYS:
            warnings.append(
                f"작성일({written_day.isoformat()})이 {age}일 지났습니다. "
                f"{STALE_DAYS}일 이상 된 글이라 지금 상황과 어긋날 수 있습니다."
            )

    # ── 템플릿에서 없어진 자리 ──
    # 슬롯 제목을 지우면 그 자리는 영원히 못 채운다. 조용히 넘기지 않는다.
    missing = [
        slot
        for slot in ordered
        if not re.search(rf"^#{{2,3}}\s*{re.escape(slot)}\.", text, re.MULTILINE)
    ]
    if missing:
        warnings.append(
            "편집 파일에 자리 제목이 없습니다: "
            f"{', '.join(slot_label(slot) for slot in missing)}. "
            f"`## {slot_label(missing[0])}` 줄을 넣으면 그 자리를 채울 수 있습니다."
        )

    # ── 빈 자리 ──
    empty = [slot for slot in ordered if slot not in slots and slot not in missing]
    if empty:
        warnings.append(
            f"아직 비어 있는 자리: {', '.join(slot_label(slot) for slot in empty)}"
        )

    return {
        "장": slots,
        "메타": meta,
        "경고": warnings,
        "파일": target,
        "생성됨": False,
    }


def pending_slots(report_md: str) -> List[str]:
    """리포트에서 아직 자리표시자가 남아 있는 슬롯 id. 나오는 순서대로 돌려준다.

    "미작성이 있는가"를 묻는 곳이 여러 군데다(화면, 이메일 제목). 자리표시자
    문구를 아는 곳이 이 모듈이므로 판정도 여기서 한다 — 문구가 바뀌면 한 곳만 고친다.
    """
    text = str(report_md or "")
    headings = list(_SLOT_RE.finditer(text))
    pending: List[str] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        if _PLACEHOLDER_RE.search(text[heading.start() : end]):
            pending.append(heading.group(1))
    return pending


def merge_into_report(
    report_md: str, manual_dict: Dict[str, Any]
) -> Tuple[str, Dict[str, List[str]]]:
    """리포트의 자리표시자를 사람이 쓴 글로 바꾼다.

    반환은 `(합친 마크다운, {"치환": [...], "남김": [...]})`다. 요청은 문자열 하나였지만
    "치환된 자리와 남은 자리 목록을 함께 반환"하려면 둘을 같이 돌려줘야 한다.

    **자리표시자만 바꾸고 나머지는 건드리지 않는다.** 5장에는 자리표시자 아래에
    "참고 — 위키에서 찾은 관련 분석"이 붙는데, 장 전체를 갈아치우면 그 인용이 사라진다.

    **내용이 없는 자리는 그대로 둔다.** 자리표시자가 남아 있어야 화면과 리포트에서
    미작성임이 드러난다.
    """
    text = str(report_md or "")
    slots = (manual_dict or {}).get("장") or {}
    replaced: List[str] = []
    kept: List[str] = []

    headings = list(_SLOT_RE.finditer(text))
    if not headings:
        return text, {"치환": [], "남김": sorted(MANUAL_SLOTS, key=slot_key)}

    pieces: List[str] = [text[: headings[0].start()]]
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.start() : end]
        slot = heading.group(1)

        if slot in MANUAL_SLOTS and _PLACEHOLDER_RE.search(block):
            content = _strip_comments(slots.get(slot, ""))
            if content:
                block = _PLACEHOLDER_RE.sub(lambda _match: content, block, count=1)
                replaced.append(slot)
            else:
                kept.append(slot)
        pieces.append(block)

    return "".join(pieces), {
        "치환": sorted(replaced, key=slot_key),
        "남김": sorted(kept, key=slot_key),
    }
