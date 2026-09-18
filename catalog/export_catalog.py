"""위키 → 카탈로그 JSON 스냅샷 (파이프라인 0단계, 수동 실행).

    python catalog/export_catalog.py

읽는 곳
    <WIKI_PATH>/06_metrics/*.md   지표 명세  → catalog/metrics_catalog.json
    <WIKI_PATH>/02_data/*.md      테이블 스키마 → catalog/schema_catalog.json

이 스크립트는 **위키를 고치지 않는다.** 편집 표시(취소선·화살표)가 섞인 행을 만나면
해석하지 않고 건너뛴 뒤 "정리가 필요한 노트"로 모아 출력한다. 파서에 예외 규칙을
쌓는 대신 위키 노트를 고치는 것이 규칙이다 (CLAUDE.md 3절).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

# 콘솔이 cp949여도 화살표·기호가 깨지지 않게 한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── 편집 표시 감지 ────────────────────────────────────────────────────
STRIKETHROUGH = re.compile(r"~~.+?~~", re.S)
#: 컬럼명·타입 칸에 있으면 값이 오염된 것으로 본다. 설명 칸은 화면 표시용이라 보지 않는다.
EDIT_MARKERS = ("→", "⇒", "실물명")
#: 본문에 적힌 인사이트 위키링크. `[[i-005-...|별칭]]`이나 `[[i-005-...#절]]`도 잡는다.
_INSIGHT_LINK_RE = re.compile(r"\[\[(i-[^\]|#]+)")


# ── 마크다운 유틸 ─────────────────────────────────────────────────────
def split_frontmatter(text: str) -> Tuple[Optional[dict], str]:
    """(프론트매터 dict, 본문)을 반환한다. 프론트매터가 없으면 (None, 원문)."""
    match = re.match(r"^---\s*?\r?\n(.*?)\r?\n---\s*?(?:\r?\n|$)(.*)$", text, re.S)
    if not match:
        return None, text
    loaded = yaml.safe_load(match.group(1))
    if not isinstance(loaded, dict):
        return None, match.group(2)
    return loaded, match.group(2)


def split_sections(body: str) -> List[Tuple[int, str, str]]:
    """본문을 (헤딩 레벨, 제목, 내용) 목록으로 자른다. 코드펜스 안의 #은 헤딩이 아니다."""
    sections: List[Tuple[int, str, str]] = []
    heading: Optional[Tuple[int, str]] = None
    buffer: List[str] = []
    in_fence = False

    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence

        match = None if in_fence else re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            if heading is not None:
                sections.append((heading[0], heading[1], "\n".join(buffer).strip()))
            heading = (len(match.group(1)), match.group(2).strip())
            buffer = []
        elif heading is not None:
            buffer.append(line)

    if heading is not None:
        sections.append((heading[0], heading[1], "\n".join(buffer).strip()))
    return sections


def _squeeze(text: str) -> str:
    """제목 비교용. 공백을 지워 '컬럼 정의 표'와 '컬럼정의표'를 같게 본다."""
    return re.sub(r"\s+", "", text)


def find_section(
    sections: List[Tuple[int, str, str]], predicate
) -> Optional[Tuple[str, str]]:
    """조건에 맞는 첫 절의 (제목, 내용)."""
    for _level, title, content in sections:
        if predicate(_squeeze(title)):
            return title, content
    return None


def find_table_block(text: str) -> List[str]:
    """절 안에서 처음 나오는 연속된 마크다운 표 블록만 잘라낸다.

    표 앞의 산문은 건너뛰고, 표가 끝나면 그 뒤는 보지 않는다 — 한 절에 표가
    둘 이상이어도 컬럼 표(첫 표)만 읽는다.
    """
    block: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            block.append(stripped)
        elif block:
            break
    return block


def split_row(row: str) -> List[str]:
    inner = row.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c)


def clean_cell(cell: str) -> str:
    """컬럼 표 셀 정제 — 백틱과 앞뒤 공백만 없앤다. 그 밖의 표시는 해석하지 않는다."""
    return cell.replace("`", "").strip()


def pick_column(header: List[str], candidates: List[str]) -> Optional[int]:
    """헤더에서 원하는 열 위치를 찾는다. candidates는 우선순위 순서다."""
    cleaned = [clean_cell(h) for h in header]
    for want in candidates:
        if want in cleaned:
            return cleaned.index(want)
    return None


# ── 06_metrics → metrics_catalog.json ─────────────────────────────────
def export_metrics(metrics_dir: Path) -> Tuple[Dict[str, Any], dict]:
    entries: Dict[str, Any] = {}
    report = {"skipped": [], "warnings": [], "no_answer_section": []}

    for path in sorted(metrics_dir.glob("*.md")):
        if path.name.startswith("_") or path.stem.upper() == "README":
            reason = "_ 로 시작" if path.name.startswith("_") else "README"
            report["skipped"].append((path.name, f"제외 규칙({reason})"))
            continue

        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            report["skipped"].append((path.name, f"파일을 읽지 못함: {exc}"))
            continue

        try:
            frontmatter, body = split_frontmatter(text)
        except yaml.YAMLError as exc:
            report["skipped"].append((path.name, f"프론트매터 YAML 파싱 실패: {exc}"))
            continue

        if frontmatter is None:
            report["skipped"].append((path.name, "프론트매터가 없음"))
            continue

        metric_id = frontmatter.get("metric_id")
        if not metric_id:
            report["skipped"].append((path.name, "프론트매터에 metric_id가 없음"))
            continue
        if metric_id in entries:
            report["warnings"].append(
                (path.name, f"metric_id '{metric_id}' 중복 — 먼저 읽은 노트를 덮어씀")
            )

        entry: Dict[str, Any] = {"노트명": path.stem, "파일": path.name}
        entry.update(frontmatter)

        # 본문에 적힌 인사이트 링크를 모은다. 프론트매터가 아니라 본문에서만 찾는다 —
        # 정의서가 어떤 발견을 근거로 삼았는지는 서술 안에 적혀 있다.
        entry["관련인사이트_본문링크"] = list(
            dict.fromkeys(link.strip() for link in _INSIGHT_LINK_RE.findall(body))
        )
        # tags는 프론트매터에서 이미 들어온다. 없으면 빈 배열로 자리를 만들어,
        # 읽는 쪽이 키 존재 여부를 확인하지 않아도 되게 한다.
        entry.setdefault("tags", [])

        # 본문에서 가져오는 것은 "답할 수 없는 것" 절 하나뿐이다.
        # 판단 근거·검토한 대안 정의는 사람이 위키에서 읽을 몫이라 담지 않는다.
        found = find_section(split_sections(body), lambda t: "답할수없" in t)
        if found:
            entry["답할_수_없는_것"] = {"제목": found[0], "내용": found[1]}
        else:
            entry["답할_수_없는_것"] = None
            report["no_answer_section"].append(path.name)

        entries[str(metric_id)] = entry

    return entries, report


# ── 02_data → schema_catalog.json ─────────────────────────────────────
def resolve_table_name(frontmatter: dict, note_name: str) -> Tuple[str, bool]:
    """(테이블명, 추정 여부)를 반환한다.

    bq_table이 있으면 그대로 쓰고(추정 아님), 없으면 노트명에서 'data_' 접두사와
    '.csv' 확장자를 떼어 유도한다(추정). 노트명↔테이블명 매핑을 코드에 하드코딩하지
    않기 위한 규칙이다 (CLAUDE.md 3절).
    """
    declared = frontmatter.get("bq_table")
    if declared:
        return str(declared).strip(), False

    derived = note_name
    if derived.lower().endswith(".csv"):
        derived = derived[: -len(".csv")]
    if derived.startswith("data_"):
        derived = derived[len("data_") :]
    return derived, True


def parse_column_table(section_text: str) -> Tuple[List[dict], List[str]]:
    """컬럼 표를 (컬럼 목록, 경고 목록)으로 파싱한다."""
    warnings: List[str] = []
    block = find_table_block(section_text)
    if len(block) < 2:
        return [], ["'컬럼' 절에서 마크다운 표를 찾지 못했습니다"]

    header = split_row(block[0])
    name_i = pick_column(header, ["컬럼명", "컬럼", "필드명"])
    type_i = pick_column(header, ["타입", "자료형", "형"])
    desc_i = pick_column(header, ["설명", "의미", "비고"])

    if name_i is None:
        return [], [f"컬럼 표에 '컬럼명' 열이 없습니다 (헤더: {' | '.join(header)})"]

    rows = block[2:] if is_separator_row(split_row(block[1])) else block[1:]

    columns: List[dict] = []
    for row in rows:
        cells = split_row(row)
        if len(cells) <= name_i:
            continue

        # 취소선으로 지운 행 — 실물에 없는 컬럼일 가능성이 높다. 임의로 살리지 않는다.
        if STRIKETHROUGH.search(row):
            warnings.append(f"취소선이 있는 행을 건너뜀: {row}")
            continue

        raw_name = cells[name_i]
        raw_type = cells[type_i] if type_i is not None and len(cells) > type_i else ""

        # 화살표·'실물명'이 컬럼명이나 타입 칸에 있으면 값이 두 개 섞인 상태다.
        # 해석하지 않고 건너뛴다 — 위키 노트를 고쳐야 할 문제다.
        marker = next((m for m in EDIT_MARKERS if m in raw_name or m in raw_type), None)
        if marker:
            warnings.append(f"편집 표시('{marker}')가 섞인 행을 건너뜀: {row}")
            continue

        name = clean_cell(raw_name)
        if not name:
            continue

        columns.append(
            {
                "컬럼명": name,
                "타입": clean_cell(raw_type),
                "설명": clean_cell(cells[desc_i])
                if desc_i is not None and len(cells) > desc_i
                else "",
            }
        )

    if not columns and not warnings:
        warnings.append("컬럼 표에서 읽어낸 컬럼이 없습니다")
    return columns, warnings


def export_schema(data_dir: Path) -> Tuple[Dict[str, Any], dict]:
    entries: Dict[str, Any] = {}
    report = {"skipped": [], "warnings": [], "dirty_notes": {}}

    for path in sorted(data_dir.glob("*.md")):
        if path.stem.upper() == "README":
            report["skipped"].append((path.name, "제외 규칙(README)"))
            continue

        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            report["skipped"].append((path.name, f"파일을 읽지 못함: {exc}"))
            continue

        try:
            frontmatter, body = split_frontmatter(text)
        except yaml.YAMLError as exc:
            report["skipped"].append((path.name, f"프론트매터 YAML 파싱 실패: {exc}"))
            continue

        if frontmatter is None:
            report["warnings"].append((path.name, "프론트매터가 없어 파일명으로 노트명을 정함"))
            frontmatter = {}

        note_name = str(frontmatter.get("data") or path.stem).strip()
        table_name, guessed = resolve_table_name(frontmatter, note_name)

        sections = split_sections(body)
        column_section = find_section(sections, lambda t: t.startswith("컬럼"))
        if column_section is None:
            report["skipped"].append((path.name, "'## 컬럼' 절이 없음"))
            continue

        columns, column_warnings = parse_column_table(column_section[1])
        if column_warnings:
            report["dirty_notes"].setdefault(path.name, []).extend(column_warnings)
        if not columns:
            report["skipped"].append((path.name, "컬럼을 하나도 읽지 못함"))
            continue

        link_section = find_section(sections, lambda t: t.startswith("연결"))

        if table_name in entries:
            report["warnings"].append(
                (path.name, f"테이블명 '{table_name}' 중복 — 먼저 읽은 노트를 덮어씀")
            )

        entries[table_name] = {
            "노트명": note_name,
            "테이블명": table_name,
            "테이블명_추정": guessed,
            "파일": path.name,
            "컬럼_절": column_section[0],
            "컬럼": columns,
            "연결_절": link_section[0] if link_section else None,
            "연결": link_section[1] if link_section else None,
        }

    return entries, report


# ── 04_insights → insights_catalog.json ───────────────────────────────
def export_insights(insights_dir: Path) -> Tuple[Dict[str, Any], dict]:
    """인사이트 노트를 스냅샷으로 뽑는다.

    **본문에서 가져오는 것은 "시사점" 절 하나다.** 근거·해석 전문을 담으면 카탈로그가
    위키 사본이 되어 버린다. 리포트가 필요로 하는 것은 "그래서 무엇을 알게 됐는가"이고,
    자세한 근거는 사람이 위키에서 읽는다.

    시사점 절이 없으면 해석 절을 대신 쓴다. 둘 다 없으면 비워 두고 경고한다 —
    임의로 다른 절을 끌어다 쓰면 무엇을 읽은 것인지 알 수 없게 된다.
    """
    entries: Dict[str, Any] = {}
    report = {"skipped": [], "warnings": [], "no_body": []}

    for path in sorted(insights_dir.glob("*.md")):
        if path.name.startswith("_") or path.stem.upper() == "README":
            reason = "_ 로 시작" if path.name.startswith("_") else "README"
            report["skipped"].append((path.name, f"제외 규칙({reason})"))
            continue

        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            report["skipped"].append((path.name, f"파일을 읽지 못함: {exc}"))
            continue

        try:
            frontmatter, body = split_frontmatter(text)
        except yaml.YAMLError as exc:
            report["skipped"].append((path.name, f"프론트매터 YAML 파싱 실패: {exc}"))
            continue

        if frontmatter is None:
            report["warnings"].append((path.name, "프론트매터가 없어 본문만 담았습니다"))
            frontmatter = {}

        sections = split_sections(body)
        title = next((title for level, title, _ in sections if level == 1), path.stem)

        picked = find_section(sections, lambda t: t.startswith("시사점"))
        source = "시사점"
        if picked is None:
            picked = find_section(sections, lambda t: t.startswith("해석"))
            source = "해석"
        if picked is None:
            report["no_body"].append(path.name)
            source = None

        entry: Dict[str, Any] = {"insight_id": path.stem, "제목": title, "파일": path.name}
        entry.update(frontmatter)
        entry["본문_절"] = source
        entry["본문"] = picked[1] if picked else None
        entries[path.stem] = entry

    return entries, report


# ── 저장·출력 ─────────────────────────────────────────────────────────
def write_catalog(path: Path, entries: Dict[str, Any], wiki_dir: Path, generated_at: str) -> None:
    payload: Dict[str, Any] = {
        "_meta": {
            "생성일시": generated_at,
            "원천": str(wiki_dir),
            "항목_개수": len(entries),
        }
    }
    payload.update(entries)  # 최상단에 metric_id / 테이블명을 키로 둔다
    path.parent.mkdir(parents=True, exist_ok=True)
    # default=str: 프론트매터의 date 필드가 datetime.date로 파싱되므로 필요하다.
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def print_report(label: str, read_count: int, report: dict) -> None:
    skipped = report.get("skipped", [])
    print(f"\n== {label} ==")
    print(f"  읽음 {read_count}개 / 건너뜀 {len(skipped)}개")

    if skipped:
        print("  건너뛴 파일:")
        for name, reason in skipped:
            print(f"    - {name}: {reason}")

    for name, message in report.get("warnings", []):
        print(f"  [경고] {name}: {message}")

    missing = report.get("no_answer_section", [])
    if missing:
        print(f"  '답할 수 없는 것' 절이 없는 노트 {len(missing)}개: {', '.join(missing)}")


def main() -> int:
    problems = config.config_warnings()
    if config.WIKI_PATH is None or problems:
        for message in problems:
            print(f"[중단] {message}")
        return 1

    metrics_dir = config.WIKI_METRICS_PATH
    data_dir = config.WIKI_DATA_PATH
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    print(f"위키: {config.WIKI_PATH}")

    metrics, metrics_report = export_metrics(metrics_dir)
    schema, schema_report = export_schema(data_dir)
    insights_dir = config.WIKI_INSIGHTS_PATH
    if insights_dir and insights_dir.is_dir():
        insights, insights_report = export_insights(insights_dir)
    else:
        insights, insights_report = {}, {"skipped": [], "warnings": [], "no_body": []}
        print(f"[경고] 인사이트 폴더가 없습니다: {insights_dir}")

    print_report("06_metrics", len(metrics), metrics_report)
    print_report("02_data", len(schema), schema_report)
    print_report("04_insights", len(insights), insights_report)

    missing = insights_report.get("no_body", [])
    if missing:
        print(f"  '시사점'·'해석' 절이 모두 없는 노트 {len(missing)}개: {', '.join(missing)}")

    # ── 지표 ↔ 인사이트 연결 ──
    print("\n== 지표 본문의 인사이트 링크 ==")
    linked = 0
    unknown: List[str] = []
    for metric_id in sorted(metrics):
        links = metrics[metric_id].get("관련인사이트_본문링크") or []
        if links:
            linked += 1
        for link in links:
            if link not in insights:
                unknown.append(f"{metric_id} → {link}")
        marker = ", ".join(links) if links else "-"
        print(f"  {metric_id:26s} {len(links)}개  {marker}")
    print(f"  링크가 있는 지표 {linked}개 / 전체 {len(metrics)}개")
    if unknown:
        print("  [경고] 인사이트 카탈로그에 없는 링크:")
        for item in unknown:
            print(f"    - {item}")

    guessed = [name for name, item in schema.items() if item["테이블명_추정"]]
    if guessed:
        print(
            f"  테이블명을 노트명에서 유도한 항목 {len(guessed)}개(추정): {', '.join(guessed)}"
        )
        print("    → 정확히 하려면 위키 노트 프론트매터에 bq_table을 적습니다.")

    dirty = schema_report.get("dirty_notes", {})
    print("\n== 정리가 필요한 노트 ==")
    if dirty:
        for name, messages in dirty.items():
            print(f"  {name}")
            for message in messages:
                print(f"    - {message}")
        print("  → 파서를 고치지 말고 위키 노트의 편집 표시를 정리하세요.")
    else:
        print("  없음")

    write_catalog(config.METRICS_CATALOG_PATH, metrics, metrics_dir, generated_at)
    write_catalog(config.SCHEMA_CATALOG_PATH, schema, data_dir, generated_at)
    write_catalog(
        config.INSIGHTS_CATALOG_PATH, insights, insights_dir or Path("-"), generated_at
    )

    print("\n저장:")
    print(f"  {config.METRICS_CATALOG_PATH}")
    print(f"  {config.SCHEMA_CATALOG_PATH}")
    print(f"  {config.INSIGHTS_CATALOG_PATH}")
    print(
        f"\nmetrics {len(metrics)}개 / tables {len(schema)}개 / insights {len(insights)}개"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
