from datetime import datetime, timezone
from types import SimpleNamespace

from core.security import (
    MIN_PASSWORD_LENGTH, TEMPORARY_PASSWORD_LENGTH, _token_outlived_password,
    generate_temporary_password, password_problem,
)


def test_password_rules():
    assert password_problem("x" * (MIN_PASSWORD_LENGTH - 1))
    assert password_problem("x" * MIN_PASSWORD_LENGTH) is None
    assert password_problem("x" * 72) is None
    assert password_problem("x" * 73)
    assert password_problem("é" * 40)  # 80 bytes, which bcrypt wouldn't fully read


def test_temporary_passwords_are_valid_and_unique():
    passwords = {generate_temporary_password() for _ in range(200)}
    assert len(passwords) == 200
    for password in passwords:
        assert len(password) == TEMPORARY_PASSWORD_LENGTH and password_problem(password) is None
        assert not set(password) & set("0OIl1")


def test_token_from_before_a_password_change_is_ended():
    changed = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
    user = SimpleNamespace(password_changed_at=changed)
    assert _token_outlived_password(user, {"iat": int(changed.timestamp()) - 1})
    assert not _token_outlived_password(user, {"iat": int(changed.timestamp())})
    assert not _token_outlived_password(SimpleNamespace(password_changed_at=None), {"iat": 1})
