"""PDF용 한글 폰트 준비 (한 번만 실행하는 준비 스크립트).

    python scripts/get_fonts.py

무엇을 하는가
    1. Google Fonts에서 Noto Sans KR **가변 폰트**를 받는다.
    2. fontTools로 **정적 TTF** 두 개(Regular=wght 400, Bold=wght 700)로 인스턴싱한다.
    3. `fonts/`에 저장하고, 라이선스(OFL.txt)도 함께 둔다.
    4. fpdf2로 실제 한글을 찍어 `fonts/_font_test.pdf`를 만든다.

왜 가변 폰트를 받아 정적으로 바꾸는가
    google/fonts 저장소에는 `NotoSansKR[wght].ttf`(가변) **하나만** 있다. 정적 TTF가
    올라와 있지 않다. 그리고 fpdf2는 가변 폰트의 wght 축을 해석하지 못해 굵기가
    엉키거나 등록이 실패할 수 있다 (DESIGN.md 1절). 그래서 받아서 축을 고정한다.

왜 Noto Sans KR인가
    Malgun Gothic은 재배포가 불가능해 쓸 수 없다. Noto Sans KR은 OFL 라이선스라
    폰트 파일을 프로젝트에 넣어 배포할 수 있다 (CLAUDE.md 7절).

내려받기가 막히면
    `--manual` 을 붙여 실행하면 수동 다운로드 안내만 출력한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAW_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanskr"
VARIABLE_NAME = "NotoSansKR[wght].ttf"
LICENSE_NAME = "OFL.txt"

#: (파일명, wght 값). fpdf2에 Regular/Bold 두 굵기만 등록한다.
STATIC_TARGETS: Tuple[Tuple[str, int], ...] = (
    ("NotoSansKR-Regular.ttf", 400),
    ("NotoSansKR-Bold.ttf", 700),
)

TEST_TEXT = "한글 테스트"
TEST_PDF_NAME = "_font_test.pdf"

MANUAL_GUIDE = f"""
수동 다운로드 방법
──────────────────────────────────────────────────────────────────────
자동 내려받기가 막히면 아래 중 하나로 받아 `fonts/`에 둡니다.

방법 A — Google Fonts 웹사이트 (가장 쉬움)
  1. https://fonts.google.com/noto/specimen/Noto+Sans+KR 접속
  2. 오른쪽 위 "Get font" → "Download all" 클릭
  3. 받은 zip을 풀면 `static/` 폴더에 정적 TTF가 들어 있습니다.
     그중 아래 두 개만 `auto-report/fonts/`로 복사합니다.
       NotoSansKR-Regular.ttf
       NotoSansKR-Bold.ttf
     ※ zip 루트의 `NotoSansKR-VariableFont_wght.ttf`는 **쓰지 않습니다**(가변 폰트).

방법 B — GitHub에서 가변 폰트만 받아 직접 변환
  1. {RAW_BASE}/{VARIABLE_NAME} 를 내려받아 `fonts/`에 둡니다 (약 10.4MB)
  2. `python scripts/get_fonts.py --from-local` 실행 — 받아둔 파일로 정적 변환만 합니다

라이선스
  {RAW_BASE}/{LICENSE_NAME} 도 함께 받아 `fonts/OFL.txt`로 둡니다.
  Noto Sans KR은 OFL이므로 폰트 파일을 프로젝트에 포함해 배포할 수 있습니다.

확인
  두 파일을 두었으면 `python scripts/get_fonts.py --test-only` 로
  한글 렌더링만 다시 검사할 수 있습니다.
──────────────────────────────────────────────────────────────────────
"""


def _size(path: Path) -> str:
    value = path.stat().st_size
    return f"{value / 1_048_576:,.2f} MB" if value >= 1_048_576 else f"{value:,} B"


def download(url: str, dest: Path) -> bool:
    """URL을 파일로 내려받는다. 실패하면 이유를 찍고 False."""
    try:
        import requests
    except ImportError:
        print("  requests가 없습니다. `pip install requests` 후 다시 실행하세요.")
        return False

    try:
        print(f"  내려받는 중: {url}")
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                handle.write(chunk)
    except Exception as exc:  # 네트워크·프록시·권한 등
        print(f"  실패: {type(exc).__name__}: {exc}")
        return False
    print(f"  저장: {dest.name} ({_size(dest)})")
    return True


def make_static(variable_path: Path, dest: Path, weight: int) -> bool:
    """가변 폰트를 wght 한 값으로 고정해 정적 TTF로 저장한다.

    `updateFontNames=True`로 이름 레코드까지 갱신한다. 이름이 그대로면 두 파일이
    똑같이 "Noto Sans KR Regular"로 보여서, 굵기를 구분해 등록할 수 없다.
    """
    try:
        from fontTools import ttLib
        from fontTools.varLib import instancer
    except ImportError:
        print("  fontTools가 없습니다. `pip install fonttools` 후 다시 실행하세요.")
        return False

    try:
        font = ttLib.TTFont(str(variable_path))
        try:
            static = instancer.instantiateVariableFont(
                font, {"wght": weight}, inplace=False, updateFontNames=True
            )
        except Exception:
            # STAT 테이블이 없거나 이름 갱신이 막히면 이름은 그대로 두고 축만 고정한다.
            print("    (이름 레코드 갱신 실패 — 축만 고정합니다)")
            font = ttLib.TTFont(str(variable_path))
            static = instancer.instantiateVariableFont(
                font, {"wght": weight}, inplace=False, updateFontNames=False
            )
        static.save(str(dest))
    except Exception as exc:
        print(f"  변환 실패({weight}): {type(exc).__name__}: {exc}")
        return False

    print(f"  생성: {dest.name} (wght={weight}, {_size(dest)})")
    return True


def inspect(path: Path) -> Dict[str, object]:
    """정적인지, 한글 글리프가 있는지 확인한다."""
    from fontTools import ttLib

    font = ttLib.TTFont(str(path))
    codepoints = set()
    for table in font["cmap"].tables:
        codepoints.update(table.cmap.keys())

    missing = [char for char in TEST_TEXT if char != " " and ord(char) not in codepoints]
    hangul = sum(1 for code in codepoints if 0xAC00 <= code <= 0xD7A3)
    return {
        "가변축(fvar)": "있음 — 정적이 아님" if "fvar" in font else "없음 (정적)",
        "한글 음절 글리프": f"{hangul:,}자",
        f"'{TEST_TEXT}' 누락 문자": missing or "없음",
    }


def render_test(fonts_dir: Path) -> Optional[Path]:
    """받은 폰트를 fpdf2에 등록해 한글 1페이지 PDF를 만든다."""
    try:
        from fpdf import FPDF
    except ImportError:
        print("  fpdf2가 없습니다. `pip install fpdf2` 후 다시 실행하세요.")
        return None

    regular = fonts_dir / STATIC_TARGETS[0][0]
    bold = fonts_dir / STATIC_TARGETS[1][0]
    for path in (regular, bold):
        if not path.exists():
            print(f"  폰트가 없습니다: {path}")
            return None

    pdf = FPDF()
    pdf.add_page()
    # fpdf2는 등록한 TTF에서 실제 쓰인 글리프만 PDF에 심는다. 원본이 10MB여도
    # 결과 PDF는 수십 KB에 그친다.
    pdf.add_font("NotoSansKR", "", str(regular))
    pdf.add_font("NotoSansKR", "B", str(bold))

    pdf.set_font("NotoSansKR", "B", 20)
    pdf.cell(0, 14, TEST_TEXT, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("NotoSansKR", "", 12)
    for line in (
        "Regular 굵기로 찍은 한글 문장입니다.",
        "숫자와 기호: 27,804,305원 / 39.0% / 21.7 GB / +2.4%p",
        "지표명 예시: 저사용 고객 비율, 고객당 평균 매출, 3개월 데이터 사용량 감소율",
        "English mixed with 한글 in one line.",
    ):
        pdf.cell(0, 9, line, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("NotoSansKR", "B", 12)
    pdf.cell(0, 9, "Bold 굵기로 찍은 한글 문장입니다.", new_x="LMARGIN", new_y="NEXT")

    out = fonts_dir / TEST_PDF_NAME
    pdf.output(str(out))
    print(f"  생성: {out.name} ({_size(out)})")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF용 한글 폰트(Noto Sans KR) 준비")
    parser.add_argument("--manual", action="store_true", help="수동 다운로드 안내만 출력")
    parser.add_argument("--from-local", action="store_true", help="이미 받아둔 가변 폰트로 변환만")
    parser.add_argument("--test-only", action="store_true", help="렌더링 테스트만 실행")
    parser.add_argument("--force", action="store_true", help="폰트가 있어도 다시 만든다")
    args = parser.parse_args()

    fonts_dir = ROOT / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)

    if args.manual:
        print(MANUAL_GUIDE)
        return 0

    print(f"폰트 폴더: {fonts_dir}")

    # ── 1. 이미 있는지 확인 ──
    existing = [name for name, _ in STATIC_TARGETS if (fonts_dir / name).exists()]
    if existing and not args.force and not args.test_only:
        print("\n== 이미 있는 폰트 ==")
        for name in existing:
            print(f"  {name} ({_size(fonts_dir / name)})")
        if len(existing) == len(STATIC_TARGETS):
            print("  두 굵기가 모두 있습니다. 다시 만들려면 --force를 붙이세요.")
            args.test_only = True

    if not args.test_only:
        variable = fonts_dir / VARIABLE_NAME
        if not variable.exists() or args.force:
            if args.from_local:
                print(f"\n가변 폰트를 찾지 못했습니다: {variable}")
                print(MANUAL_GUIDE)
                return 1
            print("\n== 가변 폰트 내려받기 ==")
            if not download(f"{RAW_BASE}/{VARIABLE_NAME}", variable):
                print(MANUAL_GUIDE)
                return 1
        else:
            print(f"\n가변 폰트가 이미 있습니다: {variable.name} ({_size(variable)})")

        license_path = fonts_dir / LICENSE_NAME
        if not license_path.exists():
            print("\n== 라이선스 내려받기 ==")
            if not download(f"{RAW_BASE}/{LICENSE_NAME}", license_path):
                print("  라이선스를 받지 못했습니다. 배포 전에 직접 넣어야 합니다.")

        print("\n== 정적 TTF 만들기 ==")
        for name, weight in STATIC_TARGETS:
            if not make_static(variable, fonts_dir / name, weight):
                print(MANUAL_GUIDE)
                return 1

    # ── 2. 검사 ──
    print("\n== 폰트 검사 ==")
    for name, _ in STATIC_TARGETS:
        path = fonts_dir / name
        if not path.exists():
            print(f"  {name}: 없음")
            continue
        print(f"  {name} ({_size(path)})")
        for key, value in inspect(path).items():
            print(f"    {key}: {value}")

    # ── 3. 렌더링 테스트 ──
    print("\n== 한글 렌더링 테스트 ==")
    if render_test(fonts_dir) is None:
        return 1

    print("\n== 결과 ==")
    for name, _ in STATIC_TARGETS:
        path = fonts_dir / name
        if path.exists():
            print(f"  {path}  {_size(path)}")
    test_pdf = fonts_dir / TEST_PDF_NAME
    if test_pdf.exists():
        print(f"  {test_pdf}  {_size(test_pdf)}")
        print("\n  이 PDF를 열어 '한글 테스트'가 네모(□□□)로 보이지 않으면 성공입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
