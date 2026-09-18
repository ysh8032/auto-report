"""Claude 코드 검증 전용 — 실제 수업에서는 쓰지 않는다.

===========================================================================
 이 스크립트는 코드를 고친 뒤 화면이 깨지지 않았는지 **빠르게 확인**하려고 만든
 것이다. BigQuery에 실제 쿼리를 보내지 않고, SQL 문자열을 해시해서 만든 **가짜
 숫자**를 돌려준다. 따라서 **여기 뜨는 숫자는 전부 거짓이다.**

 수업·시연·리포트에는 반드시 진짜를 쓴다:

     streamlit run app.py

 이 파일은 CLAUDE.md 8절의 폴더 구조에 없는 dev_tools/에 둔다. 앱 코드는 이
 파일을 import하지 않는다 — 의존 방향은 반대다. 가짜 클라이언트 구현 자체는
 `pipeline/demo.py`에 있고(배포본의 데모 모드가 같은 것을 쓰기 때문에),
 이 파일이 그것을 가져다 쓴다.
===========================================================================

사용법
    python dev_tools/fast_test_server.py --port 8502

데모 모드와 무엇이 다른가
    돌려주는 가짜 숫자는 **똑같다**(같은 `pipeline/demo.py`를 쓴다). 차이는 용도뿐이다.
        이 파일   : 개발자가 코드를 고친 뒤 화면이 깨지지 않았는지 빠르게 본다
        데모 모드 : 자격증명 없는 배포본에서 흐름을 남에게 보여준다 — 화면에 배너가 선다
    즉 배너 없이 가짜 숫자를 보는 것은 이 파일로 개발자가 직접 띄웠을 때뿐이다.

무엇을 가짜로 만드는가
    `pipeline/calculate.py`의 `make_client()`가 돌려주는 BigQuery 클라이언트 하나만
    바꿔치기한다. 앱이 그 클라이언트에서 실제로 쓰는 것은 아래 다섯 가지뿐이라,
    그만큼만 흉내 내고 그 이상은 구현하지 않는다.

        client.project
        client.load_table_from_dataframe(df, table_id, job_config=...).result()
        client.get_table(table_id).num_rows
        client.query(sql).result()            → 행 목록
        client.query(sql, job_config=...).result()

    행에는 속성 접근(row.value, row.numerator, row.denominator, row.n)과
    인덱스 접근(row[name])이 모두 쓰이므로 둘 다 지원한다.

무엇을 건드리지 않는가
    **판정 로직은 손대지 않는다.** 유효구간·최소표본·파생지표 정합성 같은 판단은
    쿼리 결과가 아니라 카탈로그(catalog/*.json)로 갈리는 부분이라, 실제 코드가
    그대로 돌아야 의미가 있다. 이 파일은 `pipeline/`과 `app.py`를 수정하지 않는다
    — `make_client`만 런타임에 바꿔치기한다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 콘솔이 cp949여도 배너의 기호(—)가 깨지지 않게 한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BANNER = (
    "가짜 BigQuery로 실행 중입니다 — 화면의 숫자는 전부 거짓입니다. "
    "수업에는 `streamlit run app.py`를 쓰세요."
)


# ── 가짜 클라이언트는 pipeline/demo.py에 있다 ─────────────────────────
# 예전에는 이 파일이 직접 들고 있었으나, 배포본의 데모 모드가 같은 것을 필요로 하게
# 되면서 구현을 pipeline/demo.py 한 곳으로 옮겼다. 구현이 두 벌이면 한쪽만 고쳐져
# 서로 다른 숫자를 내는 일이 생긴다.
#
# 이 파일과 데모 모드의 차이는 **무엇을 위해 켜느냐**뿐이다.
#   이 파일   : 코드를 고친 뒤 화면이 깨지지 않았는지 개발자가 빠르게 본다
#   데모 모드 : 자격증명 없는 배포본에서 흐름을 남에게 보여준다 (배너가 선다)
from pipeline.demo import (  # noqa: F401  — 이름을 그대로 쓰던 자리를 위해 남긴다
    FAKE_PROJECT,
    FakeBigQueryClient,
    FakeJob,
    FakeRow,
    FakeTable,
    fake_value,
    params_key,
    result_columns,
)


# ── 설치 ──────────────────────────────────────────────────────────────
def install() -> FakeBigQueryClient:
    """`calculate.make_client`만 바꿔치기한다. 판정 로직은 손대지 않는다."""
    from pipeline import calculate

    client = FakeBigQueryClient()
    calculate.make_client = lambda: client  # type: ignore[assignment]
    return client


# ── 실행 ──────────────────────────────────────────────────────────────
def _inside_streamlit() -> bool:
    try:
        from streamlit.runtime import exists

        return bool(exists())
    except Exception:
        return False


def serve() -> None:
    """Streamlit이 이 파일을 스크립트로 실행할 때 — 가짜를 심고 진짜 app.py를 돌린다."""
    import streamlit as st

    install()
    app_path = ROOT / "app.py"
    source = app_path.read_text(encoding="utf-8")
    # import가 아니라 exec으로 돌린다. Streamlit은 재실행 때마다 스크립트를 다시
    # 실행하는데, import는 모듈 캐시에 걸려 두 번째부터 아무것도 그리지 않는다.
    exec(  # noqa: S102 — 검증 전용 도구에서 실제 앱을 그대로 돌리기 위한 의도된 사용
        compile(source, str(app_path), "exec"),
        {"__name__": "__main__", "__file__": str(app_path)},
    )
    # 화면 아래쪽에 남겨 두어, 이 화면을 수업용으로 착각하지 않게 한다.
    st.sidebar.error(BANNER)


def launch(port: int) -> int:
    script = Path(__file__).resolve()
    print("=" * 78)
    print(" Claude 코드 검증 전용 서버 — 화면의 숫자는 전부 가짜입니다.")
    print(" 수업·시연에는 반드시 `streamlit run app.py`를 쓰세요.")
    print(f" http://localhost:{port}")
    print("=" * 78)
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(script),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Claude 코드 검증 전용 빠른 테스트 서버 (가짜 BigQuery)"
    )
    parser.add_argument("--port", type=int, default=8502, help="포트 (기본 8502)")
    args = parser.parse_args()
    return launch(args.port)


if __name__ == "__main__":
    if _inside_streamlit():
        serve()
    else:
        raise SystemExit(main())
