"""데모 모드 — 켜지는 조건과 배너가 붙는지 (CLAUDE.md 5-6).

여기서 지키려는 것은 기능이 아니라 **안전장치**다.
  · 자격증명이 없다고 조용히 데모로 넘어가면 안 된다
  · 데모인데 배너가 없으면 안 된다
  · 같은 SQL이 매번 다른 값을 내면 안 된다 (재현성, CLAUDE.md 5-5)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import demo  # noqa: E402


@pytest.fixture
def demo_off(monkeypatch):
    monkeypatch.delenv(demo.ENV_FLAG, raising=False)


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv(demo.ENV_FLAG, "1")


# ── 스위치 ────────────────────────────────────────────────────────────
def test_기본은_꺼짐(demo_off):
    """아무것도 안 하면 데모가 아니다. 이게 뒤집히면 로컬에서 가짜를 진짜로 읽게 된다."""
    assert demo.is_enabled() is False


def test_환경변수로_켜진다(demo_on):
    assert demo.is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  ", "아니오"])
def test_애매한_값은_꺼진_것으로_본다(monkeypatch, value):
    monkeypatch.setenv(demo.ENV_FLAG, value)
    assert demo.is_enabled() is False


def test_자격증명_실패로는_켜지지_않는다(demo_off, monkeypatch):
    """ADC가 없어도 데모로 넘어가지 않고 오류를 낸다 — 조용한 전환 금지."""
    from pipeline import calculate

    monkeypatch.setattr(calculate.config, "BQ_PROJECT", None)
    monkeypatch.setattr(
        calculate, "_service_account_from_secrets", lambda: (None, None)
    )

    import google.auth

    def 없음(*args, **kwargs):
        raise RuntimeError("자격증명 없음")

    monkeypatch.setattr(google.auth, "default", 없음)

    with pytest.raises(calculate.CalculationError):
        calculate.make_client()


def test_데모면_가짜_클라이언트가_나온다(demo_on):
    from pipeline import calculate

    client = calculate.make_client()
    assert isinstance(client, demo.FakeBigQueryClient)
    assert client.project == demo.FAKE_PROJECT


# ── 값 ────────────────────────────────────────────────────────────────
def test_같은_SQL은_같은_값(demo_off):
    """재현성. 값이 흔들리면 화면이 바뀐 이유를 추적할 수 없다."""
    sql = "SELECT COUNT(DISTINCT customer_id) AS value FROM `t` WHERE month = @month"
    a = demo.FakeBigQueryClient().query(sql).result()[0].value
    b = demo.FakeBigQueryClient().query(sql).result()[0].value
    assert a == b


def test_비율형은_분자가_분모를_넘지_않는다(demo_off):
    """파생지표 정합성 검사가 의미를 가지려면 이 관계가 깨지면 안 된다."""
    sql = (
        "SELECT n AS numerator, d AS denominator, SAFE_DIVIDE(n, d) AS value FROM `t`"
    )
    row = demo.FakeBigQueryClient().query(sql).result()[0]
    assert 0 <= row.numerator <= row.denominator
    assert row.value == pytest.approx(row.numerator / row.denominator)


def test_적재행수를_그대로_돌려준다(demo_off):
    """load_staging()이 적재 행수를 대조한다. 여기가 틀리면 앱이 정상 경로를 못 탄다."""
    import pandas as pd

    client = demo.FakeBigQueryClient()
    frame = pd.DataFrame({"a": range(500)})
    client.load_table_from_dataframe(frame, "p.d.staging_t").result()
    assert client.get_table("p.d.staging_t").num_rows == 500

    rows = client.query("SELECT COUNT(*) AS n FROM `p.d.staging_t`").result()
    assert rows[0].n == 500


# ── 배너 ──────────────────────────────────────────────────────────────
def _배너_개수() -> int:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
    app.run()
    assert not app.exception, [str(e) for e in app.exception]
    return sum(1 for block in app.markdown if demo.BANNER_TITLE[:5] in block.value)


def test_데모면_배너가_뜬다(demo_on):
    """본문 맨 위와 사이드바 두 곳. 하나라도 빠지면 스크롤한 사용자가 못 본다."""
    assert _배너_개수() == 2


def test_데모가_아니면_배너가_없다(demo_off):
    assert _배너_개수() == 0
