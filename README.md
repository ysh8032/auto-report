# auto-report

월간 리포트 자동화 도구. 데이터 파일 하나를 넣으면 지표 계산부터 이메일 초안까지
흐르고, 사람은 **넣기 한 번, 승인 세 번**만 한다.

설계 근거와 금지 목록은 [CLAUDE.md](CLAUDE.md)에, 화면·색·PDF 원칙은
[DESIGN.md](DESIGN.md)에 있다. 이 문서는 실행 방법과 이식 안내만 담는다.

---

## 이 앱이 하는 일

매달 같은 형식의 데이터가 도착할 때, 지표 정의는 위키(Obsidian)에 그대로 두고
앱은 **계산·검증·리포트·이메일 초안**만 자동으로 만든다. 정의를 앱 코드에 옮기지
않는다 — 지표를 고치려면 위키 노트를 고치고 카탈로그를 다시 뽑는다.

```
위키(정의) ──export──▶ catalog/*.json ──▶ 앱이 읽기만 한다
```

---

## 설치

```bash
pip install -r requirements.txt
```

BigQuery에 접속하려면 [Google Cloud SDK](https://cloud.google.com/sdk)가 설치돼 있고
아래로 인증돼 있어야 한다.

```bash
gcloud auth application-default login
```

---

## 실행

### 1) 카탈로그 만들기 (맨 처음, 그리고 위키를 고칠 때마다)

```bash
python catalog/export_catalog.py
```

위키의 `06_metrics/`·`02_data/`·`04_insights/`를 읽어
`catalog/metrics_catalog.json`·`schema_catalog.json`·`insights_catalog.json`을 만든다.
앱은 이 세 파일만 읽는다 — 위키 폴더를 직접 읽지 않는다(배포 환경에서 위키가
없을 수 있기 때문).

### 2) 화면으로 — 처음 쓸 때, 대시보드를 보고 싶을 때

```bash
streamlit run app.py
```

파일 업로드부터 발송 확정까지 8단계를 화면에서 그대로 밟는다. 게이트(승인 필요
지점)는 2·5·8단계에 있다.

### 3) 명령줄로 — 반복 실행, 무인 실행을 흉내낼 때

```bash
python run_pipeline.py --file usage_history_2025-01.csv
python run_pipeline.py --file usage_history_2025-01.csv --approve-extension
python run_pipeline.py --file usage_history_2025-01.csv --month 2025-01
```

1~7단계를 한 번에 돈다. 유효구간 확장이 필요한데 `--approve-extension`이 없으면
그 자리에서 멈추고 이유를 출력한다(`--help`로 인자 전체를 볼 수 있다). **8단계
(발송 확정)는 CLI로 할 수 없다** — 만들어진 `outputs/run_*/`를 화면에서 열어
사이드바의 "기존 실행 불러오기"로 이어서 확정한다.

### 4) 재현성 확인

```bash
python -m pytest tests/ -v
```

같은 파일을 넣으면 항상 같은 결과가 나오는지 4가지를 검증한다(계산·리포트
재현성, 현재 시각 의존성, 정렬 안정성). pytest가 없으면
`python tests/test_reproducible.py`로도 돈다.

---

## 8단계 흐름

| # | 단계 | 주체 | 산출물 |
|---|---|---|---|
| 1 | 데이터 파일 투입 | 사용자 | 업로드된 원본 파일 |
| 2 | 스키마 점검 → 지표 계산 | 시스템 | 계산 결과 표 |
| — | **지표 확인 후 확정 (게이트 1)** | **사용자** | 승인 기록 |
| 3 | 검증 실행 | 시스템 | 검증 결과 (통과/경고/차단) |
| 4 | 대시보드 렌더링 | 시스템 | 화면 |
| 5 | **내용·검증 결과 확인 (게이트 2)** | **사용자** | 확인 기록 |
| 6 | 리포트 생성 | 시스템 | 리포트 문서 (마크다운, PDF는 다음 단계) |
| 7 | 이메일 초안 생성 | 시스템 | 제목·수신자·본문·첨부 목록 |
| 8 | **발송 확정 (게이트 3)** | **사용자** | 확정된 최종 파일 |

승인 지점에서 사람이 하는 일은 판정(진행/중단)이지, 내용을 직접 쓰거나 고치는
일이 아니다. 자세한 원칙은 [CLAUDE.md](CLAUDE.md) 2절.

---

## 지금 구현된 범위 / 8주차에 남은 것

**6주차 범위는 전부 구현되어 있다** — 1~8단계가 화면(`app.py`)과 명령줄
(`run_pipeline.py`, 1~7단계) 양쪽에서 동작한다.

| 구현됨 | 8주차에 추가할 것 |
|---|---|
| 판정·계산·검증·대시보드 | — |
| 리포트 생성(마크다운). PDF는 `fpdf2` | — |
| 이메일 **초안** 생성(제목·수신자·본문·첨부 목록) | **실제 SMTP 발송** |
| 발송 **확정** 게이트 (최종본 파일로 고정) | 확정 직후 자동 발송 연결 |
| 실행 기록(`pipeline/runlog.py`), 재현성 테스트 | — |

실제 발송 코드는 [pipeline/send.py](pipeline/send.py)에 자리만 있다.
`send_email(email_meta, attachments)`가 `NotImplementedError`를 내고, `app.py`는
이 함수를 부르지 않는다(호출 지점은 `confirm_send()` 안에 주석으로 표시돼 있다).
이 앱은 지금 **메일을 보내지 않는다.**

---

## 본인 프로젝트로 이식할 때 고칠 곳

이 앱은 `customer-churn-dashboard`와 독립된 폴더다. 다른 프로젝트로 옮길 때는
**[config.py](config.py) 상단부터 확인한다** — 정의(지표·스키마)는 앱 코드가 아니라
위키와 카탈로그에 있으므로, 코드를 고칠 일은 거의 없고 설정만 바꾸면 된다.

| 항목 | 위치 | 무엇을 바꾸나 |
|---|---|---|
| 위키 경로 | `config.py`의 `WIKI_PATH_OVERRIDE` | 기본은 `auto-report`의 형제 폴더 중 `06_metrics/`가 있는 폴더를 자동으로 찾는다. 위키가 다른 곳에 있으면 이 한 줄에 경로를 적는다 |
| BigQuery 프로젝트·데이터셋 | `config.py`의 `BQ_PROJECT`·`BQ_DATASET` | `BQ_PROJECT`가 `None`이면 ADC 기본 프로젝트를 쓴다. `BQ_DATASET`은 본인 데이터셋 이름으로 바꾼다 |
| 스테이징 테이블 접두사 | `config.py`의 `STAGING_PREFIX` | 그대로 둬도 되지만, 같은 데이터셋을 여러 프로젝트가 같이 쓰면 접두사를 구분해 준다 |
| 수신자·발신자 | `config.py`의 `EMAIL_TO`·`EMAIL_FROM`·`EMAIL_SUBJECT_PREFIX` | 지금은 예시 주소다. 실제 주소를 코드에 쓰지 말고 이 자리(또는 8주차에 환경변수)로 관리한다 |
| 판정 임계값 | `config.py`의 `MIN_SCHEMA_MATCH`·`MOM_THRESHOLD`·`TREND_MONTHS` | 스키마 일치율 기준, 전월 대비 경고 임계값(지표별로 다르게 두려면 위키 정의서의 `변동임계값` 필드를 쓴다), 추이 그래프 개월 수 |

**지표·스키마 정의는 config.py를 건드리지 않는다.** 새 지표를 추가하거나 컬럼을
바꾸려면 위키 노트를 고치고 `python catalog/export_catalog.py`를 다시 돌린다
(CLAUDE.md 3절). `if 테이블명 == "..."` 같은 코드를 앱에 추가하는 것은 금지돼
있다 — 노트명·테이블명이 다르면 위키 정의서에 `bq_table` 필드를 쓴다.
