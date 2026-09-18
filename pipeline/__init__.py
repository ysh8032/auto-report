"""파이프라인 단계 모듈.

intake(1) → profile·calculate(2) → validate(3) → report(6) → email_draft(7)
8단계(발송 확정)는 별도 모듈 없이 app.py의 승인 게이트가 담당한다 (CLAUDE.md 8절).
"""
