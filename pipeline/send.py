"""8단계(발송 확정) 다음에 올 실제 발송 자리 — 6주차 범위 밖이다.

CLAUDE.md 5-3·9절: 6주차는 이메일 **초안 생성과 확정**까지만 한다. 실제로 메일 서버에
연결해 발송하는 것은 8주차 범위다. 이 파일은 그 자리를 코드로 표시해 둔 것이지,
지금 무엇을 실행하지 않는다 — `app.py`도 이 함수를 부르지 않는다 (호출 지점은
`confirm_send()` 안에 주석으로만 표시돼 있다).

8주차에 구현할 때 필요한 것
    - 앱 비밀번호 (또는 OAuth 토큰) — config.py에 실제 값을 쓰지 않고 환경변수·시크릿
      매니저에서 읽어야 한다 (CLAUDE.md 5-3: 실제 주소·자격증명을 코드에 쓰지 않는다)
    - SMTP 서버 설정 — host·port·TLS 여부. 회사 메일 서버인지 Gmail 등 외부
      서비스인지에 따라 인증 방식이 달라진다
    - 첨부 파일 인코딩 — report.md/report.pdf/metrics.csv/comparison.csv를
      실제로 읽어 MIME 파트로 붙이는 처리. 지금은 email_draft.attachment_list()가
      이름·경로·크기만 나열하고 실제로 붙이지는 않는다
    - 수신자 검증 — config.EMAIL_TO가 실제 운영 주소로 바뀐 뒤, 형식이 맞는 주소인지
      발송 전에 확인하는 절차
    - 인증 오류 처리 — SMTP 인증 실패·타임아웃·수신 거부(bounce)를 구분해 사용자에게
      알리는 처리. 지금 이 함수는 무조건 예외를 낸다
"""

from __future__ import annotations

from typing import Any, Dict, Sequence


def send_email(email_meta: Dict[str, Any], attachments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """실제 발송. **8주차에 구현한다 — 지금은 아무것도 하지 않는다.**

    `email_meta`는 `pipeline.email_draft.build_email()`이 돌려주는 dict다
    (subject/to/from/body_html/body_text). `attachments`는 그 dict의
    `attachments` 목록(`{filename, path, size, exists}`)을 그대로 받는다.

    구현되면 이런 모양의 dict를 돌려줄 것이다(지금은 아무 값도 내지 않는다):
        {"발송됨": bool, "발송_시각": str, "실패한_수신자": [...], "오류": str | None}
    """
    raise NotImplementedError(
        "8주차에 구현한다. 필요한 것: 앱 비밀번호, SMTP 서버 설정, "
        "첨부 파일 인코딩, 수신자 검증, 인증 오류 처리"
    )
