# 배포 — Streamlit Community Cloud

이 문서는 `auto-report`를 Streamlit Community Cloud에 올리는 절차다.
로컬에서 돌던 앱이 배포 환경에서 깨지는 원인은 거의 항상 **인증** 하나이므로, 그 부분을 먼저 본다.

---

## 1. 무엇이 다른가

| | 로컬 | Streamlit Cloud |
|---|---|---|
| BigQuery 인증 | ADC (`gcloud auth application-default login`) | **없음** → 서비스 계정 키를 secrets로 주입 |
| 프로젝트 ID | ADC의 `quota_project_id`에서 유도 | 서비스 계정 JSON의 `project_id` |
| 파일 저장 | `outputs/run_*/`가 디스크에 남는다 | 컨테이너가 재시작하면 사라진다. 산출물은 화면에서 내려받는다 |

`pipeline/calculate.py`의 `make_client()`는 **secrets → `config.BQ_PROJECT` → ADC** 순으로 본다.
secrets가 없으면 로컬 동작이 그대로이므로, 이 문서를 따라도 로컬 실행 방식은 바뀌지 않는다.

---

## 2. 두 갈래 — 키를 만들 것인가

| | 경로 A · 서비스 계정 | 경로 B · 데모 모드 |
|---|---|---|
| GCP에서 할 일 | 서비스 계정 키(JSON) 발급 | **없음** |
| Secrets에 넣을 것 | 서비스 계정 10여 줄 | `demo_mode = true` **한 줄** |
| 배포본의 숫자 | 진짜 | **가짜** (배너가 계속 뜬다) |
| 되는 것 | 아무 파일이나 넣어 1~8단계 실제 계산 | 아무 파일이나 넣어 1~8단계 흐름 |
| 진짜 결과 보기 | 그 자리에서 계산 | **기존 실행 불러오기**로 기록된 실행 열람 |

**BigQuery는 익명 접근이 없다.** 공개 데이터셋조차 자격증명을 요구한다. 그래서 "키 없이 실제 계산"은 존재하지 않고, 키를 안 만들겠다면 배포본이 BigQuery를 **안 부르게** 하는 수밖에 없다. 그것이 경로 B다.

경로 B를 골라도 배포본에 실제 계산 결과가 남는다. `outputs/run_20260822_1424/`에 실제 BigQuery로 돌린 실행 한 벌(지표 8종·검증·리포트·PDF·이메일 초안)이 저장소에 들어 있고, 왼쪽 사이드바 **기존 실행 불러오기**로 열면 그 화면의 숫자는 진짜다. 불러온 화면에서는 추이 차트만 빠지는데, 추이는 그때 계산해야 해서 데모 모드에서는 가짜가 나오기 때문이다 — 한 화면에 진짜와 가짜를 섞지 않는다.

---

## 2-1. 경로 B — 키 없이 배포하기

Secrets 칸에 이 한 줄만 넣는다.

```toml
demo_mode = true
```

그게 전부다. 3절(GitHub)과 4절(배포)은 그대로 따르되, 4절 3번의 서비스 계정 블록 대신 위 한 줄을 넣는다.

배포 후 화면 맨 위와 사이드바에 **"데모 모드 — 화면의 숫자는 전부 가짜입니다"** 배너가 뜬다. 이 배너는 접히지 않는다. 지우려면 데모 모드를 끄는 수밖에 없다 — 가짜 숫자가 배너 없이 보이는 상태를 만들지 않는다 (CLAUDE.md 5-6).

가짜인 것은 **BigQuery가 돌려주는 숫자뿐이다.** 스키마 대조, 유효구간 판정, 최소표본, 파생지표 정합성, 합계 대조, 승인 게이트, 리포트·이메일 조립은 실제 코드가 그대로 돈다. 즉 "이 앱이 무엇을 하는가"는 정확히 보이고 "이번 달 수치가 얼마인가"만 거짓이다.

로컬에서 같은 상태를 확인하려면:

```bash
AUTO_REPORT_DEMO=1 streamlit run app.py --server.port 8502    # 화면
AUTO_REPORT_DEMO=1 python run_pipeline.py --file <csv> --approve-extension   # CLI
```

경로 B로 배포했다가 나중에 실제 계산이 필요해지면, Secrets에서 `demo_mode` 줄을 지우고 아래 2-2의 서비스 계정 블록을 넣으면 된다. **코드는 고치지 않는다.**

---

## 2-2. 경로 A — 서비스 계정 만들기 (GCP 콘솔)

대상은 로컬과 같은 프로젝트·데이터셋이다 — `config.py`의 `BQ_DATASET`(`project1_day1`)이 있는 프로젝트.

1. GCP 콘솔 → **IAM 및 관리자 → 서비스 계정 → 서비스 계정 만들기**
2. 이름은 알아볼 수 있게 (예: `auto-report-streamlit`)
3. 역할 두 개를 준다. 더 주지 않는다.
   - **BigQuery 데이터 편집자** — 스테이징 테이블을 만들고 교체해야 한다
   - **BigQuery 작업 사용자** — 쿼리를 실행해야 한다
4. 만든 계정 → **키 → 키 추가 → 새 키 만들기 → JSON** → 파일이 내려받아진다

> 이 JSON은 비밀번호와 같다. 저장소에 넣지 않는다. `.gitignore`가 `*credentials*.json`·
> `*service-account*.json`을 막고 있지만, 파일명을 바꾸면 그 방어를 지나간다. **아예 프로젝트
> 폴더 밖에 둔다.**

---

## 3. GitHub에 올리기

Streamlit Community Cloud는 GitHub 저장소에서만 배포한다.

```bash
git init
git add .
git commit -m "auto-report 초기 커밋"
git branch -M main
git remote add origin <저장소 주소>
git push -u origin main
```

올리기 전에 한 번 확인한다.

```bash
git status --short          # 키 파일이 목록에 없어야 한다
git ls-files | grep -i -E "secret|credential|service"
```

마지막 명령의 결과는 `.streamlit/secrets.toml.example` **하나뿐**이어야 한다.
이 파일은 값이 비어 있는 템플릿이라 올라가도 된다.

---

## 4. Streamlit Cloud에 배포

1. https://share.streamlit.io 에 GitHub 계정으로 로그인
2. **New app** → 저장소·브랜치(`main`)·메인 파일(`app.py`) 선택
3. **Advanced settings → Secrets** — 경로 B라면 `demo_mode = true` 한 줄이면 끝이다. 경로 A라면 2-2에서 받은 JSON을 아래 형식으로 붙여 넣는다

```toml
[gcp_service_account]
type = "service_account"
project_id = "여기에-프로젝트-ID"
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "auto-report-streamlit@....iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"
```

JSON의 필드를 그대로 옮기되 **TOML 문법**으로 쓴다 — `:`가 아니라 `=`, 값은 큰따옴표.
`private_key`는 JSON에 있던 `\n`을 **그대로 둔다.** 실제 줄바꿈으로 펴면 키를 읽지 못한다.

4. **Deploy** — 첫 배포는 `requirements.txt` 설치에 몇 분 걸린다

---

## 5. 배포 후 확인

| 확인할 것 | 정상 | 실패하면 |
|---|---|---|
| 앱이 뜬다 | 1단계 업로드 화면 | 로그에서 `ModuleNotFoundError` → `requirements.txt`에 빠진 패키지 |
| 파일 업로드 → 2단계 계산 | 지표 표가 나온다 | `403`·`Permission denied` → 서비스 계정 역할 부족 (2-2절) |
| 계산 결과 숫자 | 로컬과 같은 값 | 다르면 프로젝트·데이터셋이 다른 곳을 보고 있다 |
| 6단계 PDF | 한글이 깨지지 않는다 | `fonts/NotoSansKR-Regular.ttf`·`-Bold.ttf`가 저장소에 올라갔는지 확인 |

인증이 안 되면 `make_client()`가 무엇을 하라는지 화면에 그대로 띄운다. 그 메시지를 먼저 읽는다.

---

## 6. 알려진 제약

- **`outputs/run_*/`는 영구 저장이 아니다.** Streamlit Cloud 컨테이너는 재시작하면 디스크가
  초기화된다. 8단계에서 확정한 파일은 그 자리에서 내려받는다.
- **카탈로그와 실제 테이블명 불일치는 배포로 해결되지 않는다.** `data_` 접두사가 붙은 테이블
  6종은 `schema_catalog.json`의 추정값과 다르므로 해당 지표에서 404가 난다. 해결은 위키
  스키마 노트에 `bq_table`을 적고 `catalog/export_catalog.py`를 다시 돌리는 것이다
  (CLAUDE.md 3절). **앱 코드에 매핑을 넣지 않는다.**
- 실제 이메일 발송은 여전히 범위 밖이다. `pipeline/send.py`는 자리만 있다 (CLAUDE.md 5-3).
