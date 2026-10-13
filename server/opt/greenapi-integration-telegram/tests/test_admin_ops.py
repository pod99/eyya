import base64
from types import SimpleNamespace


def _basic(username, password):
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {raw}"


def test_admin_security_protects_admin_paths(monkeypatch):
    from app.config import settings
    from app.admin_security import check_admin_auth, is_protected_path

    monkeypatch.setattr(settings, "admin_username", "u")
    monkeypatch.setattr(settings, "admin_password", "p")

    assert is_protected_path("/admin") is True
    assert is_protected_path("/api/admin/health") is True
    assert is_protected_path("/api/widget/send") is False
    assert is_protected_path("/bitrix/events") is False

    ok_request = SimpleNamespace(headers={"authorization": _basic("u", "p")})
    bad_request = SimpleNamespace(headers={"authorization": _basic("u", "bad")})
    empty_request = SimpleNamespace(headers={})

    assert check_admin_auth(ok_request) == "u"
    assert check_admin_auth(bad_request) is None
    assert check_admin_auth(empty_request) is None


def test_admin_security_rejects_when_credentials_not_configured(monkeypatch):
    from app.config import settings
    from app.admin_security import check_admin_auth

    monkeypatch.setattr(settings, "admin_username", "")
    monkeypatch.setattr(settings, "admin_password", "")

    ok_request = SimpleNamespace(headers={"authorization": _basic("u", "p")})
    assert check_admin_auth(ok_request) is None


def test_backup_cleanup_uses_retention(monkeypatch, tmp_path):
    import os
    from datetime import datetime, timezone, timedelta
    from app import maintenance

    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path))
    old_path = tmp_path / "app-20200101-000000.db"
    new_path = tmp_path / "app-20990101-000000.db"
    old_path.write_text("old")
    new_path.write_text("new")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(old_path, (old_ts, old_ts))

    removed = maintenance.cleanup_backups(retention_days=14)

    assert removed == 1
    assert not old_path.exists()
    assert new_path.exists()
