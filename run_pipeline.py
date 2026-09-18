"""명령줄에서 1~7단계를 실행한다. 8단계(발송 확정)는 화면에서만 한다.

    python run_pipeline.py --file 수업자료/usage_history_2025-01.csv
    python run_pipeline.py --file xxx.csv --approve-extension
    python run_pipeline.py --file xxx.csv --month 2025-01

**게이트는 화면에만 있는 것이 아니다.** 유효구간 확장이 필요한데 승인 플래그가 없으면
여기서 멈춘다. CLI라고 게이트를 건너뛰면 "자동화"가 아니라 방치가 된다 (CLAUDE.md 9절).

**8단계를 넣지 않는 이유**: 발송 확정은 사람이 최종본을 눈으로 보고 하는 판정이다.
플래그 하나로 확정할 수 있으면 확정의 의미가 없다. 마지막에 화면으로 넘기는 안내만 낸다.

**화면과 같은 코드를 쓴다.** 판정·계산·검증·리포트·이메일은 전부 `pipeline/` 모듈이고,
기록 구조는 `pipeline/runlog.py`가 정한다. CLI가 자기 버전을 따로 갖지 않아야
"화면 결과와 CLI 결과가 다르다"가 생기지 않는다.

종료 코드
    0   정상
    1   검증 차단 또는 게이트에서 멈춤 (사람이 판단할 일이 남았다)
    2   오류
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import config

EXIT_OK, EXIT_GATE, EXIT_ERROR = 0, 1, 2

# 파이프나 파일로 넘기면 stdout이 블록 버퍼링되어 진행 로그가 stderr의 중단 메시지보다
# 늦게 나온다 — 로그만 보면 순서가 거꾸로 읽힌다. 그리고 Windows 콘솔이 cp949면
# "—"·"·" 같은 글자에서 UnicodeEncodeError로 죽는다. 둘 다 여기서 막는다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", line_buffering=True)
    except (AttributeError, ValueError):  # 리다이렉트된 스트림은 못 바꿀 수 있다
        pass

#: 유효구간 확장을 CLI에서 승인했다는 표시. 화면 체크박스와 구분해서 기록한다.
APPROVED_BY_CLI = "CLI --approve-extension"


# ── 출력 ──────────────────────────────────────────────────────────────
def _now() -> str:
    return dt.datetime.now().astimezone().strftime("%H:%M:%S")


def log(step: str, message: str) -> None:
    """단계별 진행 상황. 무인 실행 로그로 남으므로 시각을 함께 찍는다."""
    print(f"[{_now()}] {step:<12} {message}", flush=True)


def fail(message: str, code: int = EXIT_ERROR) -> int:
    print(f"[{_now()}] {'중단':<12} {message}", file=sys.stderr, flush=True)
    return code


def bullet(lines: Sequence[str], prefix: str = "  - ") -> None:
    for line in lines:
        print(f"{prefix}{line}", flush=True)


# ── 준비 ──────────────────────────────────────────────────────────────
def load_catalog(path: Path, label: str) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(
            fail(f"{label} 카탈로그가 없습니다: {path}. catalog/export_catalog.py를 먼저 실행하세요.")
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(fail(f"{label} 카탈로그를 읽지 못했습니다: {exc}")) from exc


def unique_run_dir(base: Path, now: dt.datetime) -> Path:
    """`run_YYYYMMDD_HHMM`. 같은 분에 두 번 돌려도 이전 실행을 덮지 않는다."""
    stem = f"run_{now.strftime('%Y%m%d_%H%M')}"
    target = base / stem
    suffix = 2
    while target.exists():
        target = base / f"{stem}_{suffix}"
        suffix += 1
    return target


# ── 본체 ──────────────────────────────────────────────────────────────
def run(args: argparse.Namespace) -> int:
    # import를 함수 안에서 하는 이유: --help가 streamlit·bigquery 로딩을 기다리지 않게 한다.
    import pandas as pd

    from pipeline import (
        calculate,
        compare,
        email_draft,
        intake,
        manual_sections,
        phrasing,
        profile,
        report,
        runlog,
        validate,
    )

    source = Path(args.file)
    if not source.exists():
        return fail(f"파일이 없습니다: {source}")

    metrics_catalog = load_catalog(config.METRICS_CATALOG_PATH, "지표")
    schema_catalog = load_catalog(config.SCHEMA_CATALOG_PATH, "스키마")
    try:
        insights_catalog = json.loads(
            config.INSIGHTS_CATALOG_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        insights_catalog = {}  # 인사이트는 없어도 리포트가 나온다

    log(
        "0. 카탈로그",
        f"지표 {runlog.catalog_count(metrics_catalog)}종 · "
        f"테이블 {runlog.catalog_count(schema_catalog)}종 · "
        f"인사이트 {runlog.catalog_count(insights_catalog)}종 "
        f"(갱신 {runlog.catalog_stamp(metrics_catalog)})",
    )

    # ── 1단계: 파일 읽기 ──
    raw = source.read_bytes()
    upload_stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        frame, encoding = intake.read_csv_bytes(raw)
    except intake.IntakeError as exc:
        return fail(str(exc))
    info = intake.describe(frame, source.name, len(raw), encoding, raw)
    log("1. 투입", f"{source.name} — {info['행수']:,}행 {info['컬럼수']}컬럼 ({encoding})")

    # ── 2단계: 판정 ──
    judgement = profile.judge_table(frame, schema_catalog)
    if not judgement["판정가능"]:
        print()
        return fail(judgement.get("이유") or "스키마가 맞는 원천 테이블이 없습니다.")
    table = judgement["테이블명"]
    prof = profile.profile_data(frame, schema_catalog.get(table))
    period = args.month or prof["기간"]["최대"] or prof["기간"]["최소"]
    if not period:
        return fail("기간을 판정하지 못했습니다. --month 로 지정하세요.")

    judged = profile.judge_metrics(
        table, period, metrics_catalog, missing_columns=judgement["누락컬럼"]
    )
    targets = [
        row for row in judged if row["상태"] in (profile.CALCULABLE, profile.NEEDS_RANGE)
    ]
    needs_range = [row for row in targets if row["상태"] == profile.NEEDS_RANGE]
    log(
        "2. 판정",
        f"{table} (일치율 {judgement['일치율']:.0%}) · 기간 {period} · "
        f"계산 대상 {len(targets)}종",
    )
    if prof.get("결측있는컬럼수"):
        log("2. 판정", f"결측 있는 컬럼 {prof['결측있는컬럼수']}개 — 리포트 한계에 기록됩니다")

    # ── 게이트 1 ──
    if needs_range and not args.approve_extension:
        print()
        print("유효구간 밖이라 확장 승인이 필요한 지표가 있습니다:")
        bullet(
            [
                f"{row['지표명']} ({row['metric_id']}) — {row.get('이유') or '유효구간 밖'}"
                for row in needs_range
            ]
        )
        print()
        print("확장은 이번 실행에만 적용되며 위키 정의서는 바뀌지 않습니다.")
        print("승인하려면 --approve-extension 을 붙여 다시 실행하세요.")
        return fail(f"게이트 1에서 멈췄습니다 — 확장 승인 필요 {len(needs_range)}종", EXIT_GATE)

    if not targets:
        return fail("계산할 수 있는 지표가 없습니다.")

    # ── 실행 폴더 ──
    now = dt.datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    base = Path(args.output_dir) if args.output_dir else config.OUTPUTS_DIR
    run_dir = unique_run_dir(base, now)
    try:
        run_dir.mkdir(parents=True)
        shutil.copyfile(source, run_dir / source.name)  # 원본이 있어야 재현할 수 있다
    except OSError as exc:
        return fail(f"실행 폴더를 만들지 못했습니다: {exc}")

    runlog.write(run_dir, runlog.RUN, runlog.run_section(run_dir.name, stamp, "CLI"), stamp)
    runlog.write(
        run_dir,
        runlog.CATALOG,
        runlog.catalog_section(metrics_catalog, schema_catalog, insights_catalog),
        stamp,
    )
    runlog.write(
        run_dir, runlog.INTAKE, runlog.intake_section(info, source.name, upload_stamp), stamp
    )
    runlog.write(run_dir, runlog.JUDGE, runlog.judge_section(judgement, prof, judged), stamp)
    runlog.write(
        run_dir,
        runlog.GATE1,
        runlog.gate1_section(stamp, needs_range, args.approve_extension, APPROVED_BY_CLI),
        stamp,
    )
    log("게이트1", f"{run_dir.name} 생성 · 확장 승인 {'예 (CLI)' if needs_range and args.approve_extension else '해당 없음'}")

    # ── 2단계: 계산 ──
    try:
        client = calculate.make_client()
        # load_staging은 "project.dataset.staging_x" 전체 경로를 돌려준다(기록·화면 표시용).
        # calculate()의 staging_map은 짧은 이름만 받고 dataset을 스스로 앞에 붙이므로,
        # 전체 경로를 그대로 넘기면 dataset이 두 번 붙는다 — 반드시 짧은 이름을 써야 한다.
        staging_full = calculate.load_staging(frame, table, client)
        staging_short = f"{config.STAGING_PREFIX}{table}"
    except Exception as exc:  # 인증·권한·네트워크 — 이유를 그대로 보여준다
        return fail(f"BigQuery 연결에 실패했습니다: {type(exc).__name__}: {exc}")

    ids = [row["metric_id"] for row in targets]
    try:
        metrics = calculate.calculate(
            ids,
            period,
            {table: staging_short},
            client,
            override=bool(args.approve_extension),
            metrics_catalog=metrics_catalog,
        )
    except Exception as exc:
        return fail(f"계산 중 오류: {type(exc).__name__}: {exc}")

    counts = metrics["status"].value_counts().to_dict()
    log(
        "2. 계산",
        f"{len(metrics)}종 — "
        + ", ".join(f"{status} {count}" for status, count in counts.items()),
    )

    prev_period = compare.previous_month(period)
    comparison = None
    try:
        previous = compare.calc_previous(
            ids, prev_period, client, metrics_catalog=metrics_catalog
        )
        comparison = compare.compare(metrics, previous, metrics_catalog)
        log("2. 계산", f"전월({prev_period}) 대비 — " + _summary_text(compare.summarize(comparison)))
    except Exception as exc:
        log("2. 계산", f"전월 비교를 건너뜁니다: {type(exc).__name__}: {exc}")

    metrics.to_csv(run_dir / "metrics.csv", index=False, encoding="utf-8-sig")
    if comparison is not None:
        comparison.to_csv(run_dir / "comparison.csv", index=False, encoding="utf-8-sig")
    runlog.write(
        run_dir,
        runlog.CALC,
        runlog.calc_section(
            metrics,
            staging_full,  # 기록에는 전체 경로를 남긴다 — 화면(app.py)과 같은 값
            comparison,
            prev_period,
            compare.summarize(comparison) if comparison is not None else None,
        ),
    )

    # ── 3단계: 검증 ──
    validation = validate.validate_all(
        metrics,
        metrics_catalog,
        comparison_df=comparison,
        override=bool(args.approve_extension),
    )
    (run_dir / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, default=str) + chr(10),
        encoding="utf-8",
    )
    runlog.write(run_dir, runlog.VALIDATE, runlog.validate_section(validation))
    log(
        "3. 검증",
        f"{validation['전체판정']} — 차단 {validation['차단수']}건 · 경고 {validation['경고수']}건",
    )

    if validation["차단수"]:
        print()
        print("검증 차단 항목:")
        bullet(
            [
                f"{item['검증명']} · {item.get('대상지표') or '—'} — {item.get('상세') or ''}"
                for item in validation["항목별결과"]
                if item["판정"] == validate.BLOCK
            ]
        )
        print()
        print(f"실행 폴더: {run_dir}")
        return fail("검증 차단이 있어 리포트를 만들지 않았습니다.", EXIT_GATE)

    # 4·5단계는 화면 전용이다. CLI에는 대시보드가 없고, 사람 확인 게이트도 없다.
    log("4·5단계", "대시보드·사람 확인은 화면 전용이라 건너뜁니다")

    # ── 6단계: 리포트 (사람 작성분 병합 포함) ──
    context = {
        "파일명": info["파일명"],
        "테이블명": table,
        "기간": prof["기간"],
        "행수": info["행수"],
        "metrics": metrics,
        "comparison": comparison,
        "validation": validation,
        "metrics_catalog": metrics_catalog,
        "schema_catalog": schema_catalog,
        "insights_catalog": insights_catalog,
        "run_log": runlog.load(run_dir),
        "source_freshness": None,
        "생성일시": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
    }
    try:
        markdown, manual, outcome = report.build_report_with_manual(context)
    except Exception as exc:
        return fail(f"리포트를 만들지 못했습니다: {type(exc).__name__}: {exc}")

    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    chapters = _chapter_numbers(markdown)
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
    pending = manual_sections.pending_slots(markdown)
    log(
        "6. 리포트",
        f"report.md {len(markdown):,}자 · 장 {len(chapters)}개 · "
        f"사람 작성분 {len(outcome['치환'])}곳 병합 · 미작성 {len(pending)}곳",
    )
    for warning in manual.get("경고") or []:
        log("6. 리포트", f"· {warning}")

    if args.skip_pdf:
        log("6. 리포트", "PDF는 --skip-pdf 로 건너뜁니다")
    elif not report.fonts_available():
        log("6. 리포트", "PDF용 한글 폰트가 없어 건너뜁니다 (python scripts/get_fonts.py)")
    else:
        try:
            (run_dir / "report.pdf").write_bytes(report.build_pdf(markdown))
            log("6. 리포트", "report.pdf 생성")
        except Exception as exc:
            log("6. 리포트", f"PDF를 만들지 못했습니다: {type(exc).__name__}: {exc}")

    # ── 7단계: 이메일 초안 ──
    try:
        draft = email_draft.build_email(context, markdown)
    except Exception as exc:
        return fail(f"이메일 초안을 만들지 못했습니다: {type(exc).__name__}: {exc}")

    note = "발송하지 않음 (6주차 범위는 초안·확정까지)"
    (run_dir / "email.html").write_text(draft["body_html"], encoding="utf-8")
    (run_dir / "email.txt").write_text(draft["body_text"], encoding="utf-8")
    (run_dir / "email_meta.json").write_text(
        json.dumps(
            {
                "생성시각": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "subject": draft["subject"],
                "to": draft["to"],
                "from": draft["from"],
                "attachments": draft["attachments"],
                "발송여부": note,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + chr(10),
        encoding="utf-8",
    )
    runlog.write(run_dir, runlog.EMAIL, runlog.email_section(draft, pending, note))
    log("7. 이메일", draft["subject"])
    missing = [item["filename"] for item in draft["attachments"] if not item["exists"]]
    if missing:
        log("7. 이메일", f"첨부 목록에 없는 파일: {', '.join(missing)}")

    # ── 요약 ──
    print()
    print("=" * 70)
    print(f"  계산 지표      {len(metrics)}종")
    print(f"  검증 판정      {validation['전체판정']} (차단 {validation['차단수']} · 경고 {validation['경고수']})")
    print(
        f"  미작성 자리    {len(pending)}곳"
        + (f" — {', '.join(manual_sections.slot_label(s) for s in pending)}" if pending else "")
    )
    print(f"  run 폴더       {run_dir}")
    print("=" * 70)
    print()
    print("이메일 초안이 준비되었습니다. 발송 확정은 화면에서 진행하세요:")
    print(f"  streamlit run app.py  (run 폴더: {run_dir})")
    return EXIT_OK


def _summary_text(summary: Dict[str, Any]) -> str:
    return ", ".join(f"{key} {value}" for key, value in (summary or {}).items()) or "—"


def _chapter_numbers(markdown: str) -> List[int]:
    """리포트에 실제로 들어간 장 번호. 기록의 `장_목록`과 화면이 같은 값을 쓰게 한다."""
    import re

    return [
        int(match.group(1))
        for match in re.finditer(r"^##\s*(\d+)\.\s", markdown, re.MULTILINE)
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="명령줄에서 1~7단계를 실행합니다. 8단계(발송 확정)는 화면에서만 합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python run_pipeline.py --file 수업자료/usage_history_2025-01.csv\n"
            "  python run_pipeline.py --file xxx.csv --approve-extension\n"
            "  python run_pipeline.py --file xxx.csv --month 2025-01\n\n"
            "종료 코드: 0 정상 / 1 검증 차단·게이트 정지 / 2 오류"
        ),
    )
    parser.add_argument("--file", required=True, help="업로드할 CSV 경로")
    parser.add_argument("--month", help="대상 월(YYYY-MM). 생략하면 파일에서 판정합니다")
    parser.add_argument(
        "--approve-extension",
        action="store_true",
        help="유효구간 확장을 승인합니다. 없으면 확장이 필요할 때 멈춥니다",
    )
    parser.add_argument("--output-dir", help=f"실행 폴더의 부모. 기본 {config.OUTPUTS_DIR}")
    parser.add_argument("--skip-pdf", action="store_true", help="PDF 생성을 건너뜁니다")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except SystemExit as exc:  # load_catalog가 낸 종료
        return int(exc.code or EXIT_ERROR)
    except KeyboardInterrupt:
        return fail("사용자가 중단했습니다.")
    except Exception as exc:  # 예상 못 한 오류 — 무엇이 터졌는지 남긴다
        return fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
