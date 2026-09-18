"""1단계 — 파일 수신·기본 검사.

업로드된 CSV를 DataFrame으로 읽고, 화면에 표시할 기본 정보를 만든다.
스키마 판정(카탈로그와 대조)은 여기서 하지 않는다 — 2단계 profile.py 몫이다.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Tuple

import pandas as pd

#: 시도 순서. utf-8-sig가 먼저다 — 엑셀에서 저장한 CSV는 BOM이 붙는다.
ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "cp949")


class IntakeError(Exception):
    """파일을 받아들일 수 없을 때. 메시지는 화면에 그대로 보여줄 수 있어야 한다."""


def read_csv_bytes(raw: bytes) -> Tuple[pd.DataFrame, str]:
    """CSV 바이트를 (DataFrame, 사용한 인코딩)으로 읽는다.

    utf-8-sig로 먼저 시도하고 디코딩에 실패하면 cp949로 재시도한다. 어느 쪽으로도
    못 읽으면 IntakeError를 낸다 — 깨진 글자를 섞어 통과시키지 않는다.
    """
    if not raw:
        raise IntakeError("빈 파일입니다.")

    for encoding in ENCODINGS:
        try:
            frame = pd.read_csv(io.BytesIO(raw), encoding=encoding)
        except UnicodeDecodeError:
            continue  # 다음 인코딩으로
        except pd.errors.EmptyDataError as exc:
            raise IntakeError(f"CSV에서 읽을 내용이 없습니다: {exc}") from exc
        except pd.errors.ParserError as exc:
            raise IntakeError(
                f"CSV 형식을 해석하지 못했습니다({encoding}): {exc}"
            ) from exc

        if frame.shape[1] == 0:
            raise IntakeError("컬럼을 하나도 찾지 못했습니다. CSV가 맞는지 확인하세요.")
        if len(frame) == 0:
            raise IntakeError("헤더만 있고 데이터 행이 0건입니다.")
        return frame, encoding

    raise IntakeError(
        f"인코딩을 판별하지 못했습니다. {' · '.join(ENCODINGS)} 순으로 시도했습니다. "
        "파일을 UTF-8로 다시 저장한 뒤 올려 주세요."
    )


def header_names(raw: bytes, encoding: str) -> List[str]:
    """파일에 적힌 그대로의 헤더 행.

    pandas는 중복 컬럼을 읽으면서 뒤쪽을 `a.1`로 바꾼다. 그래서 DataFrame만 봐서는
    중복이 있었다는 사실을 알 수 없어, 원본 헤더를 따로 읽는다.
    """
    reader = csv.reader(io.StringIO(raw.decode(encoding, errors="replace")))
    try:
        return [name.strip() for name in next(reader)]
    except StopIteration:
        return []


def duplicate_columns(raw: bytes, encoding: str) -> List[str]:
    """원본 헤더에서 중복된 컬럼명. 있으면 2단계 스키마 판정이 어긋난다."""
    names = header_names(raw, encoding)
    return sorted({name for name in names if names.count(name) > 1})


def describe(
    frame: pd.DataFrame, filename: str, size_bytes: int, encoding: str, raw: bytes
) -> Dict[str, Any]:
    """화면·실행 기록에 쓸 기본 정보. 판정은 하지 않고 사실만 담는다."""
    return {
        "파일명": filename,
        "크기_bytes": int(size_bytes),
        "인코딩": encoding,
        "행수": int(len(frame)),
        "컬럼수": int(frame.shape[1]),
        "컬럼": [str(c) for c in frame.columns],
        "중복컬럼": duplicate_columns(raw, encoding),
    }
