from pathlib import Path

backup = sorted(Path(".").glob("docker-compose.yml.bak.envfile-*"))[-1]
text = backup.read_text()
keys = [
    "PORT",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "CDEK_CLIENT_ID",
    "CDEK_CLIENT_SECRET",
    "DASHBOARD_USER",
    "DASHBOARD_PASSWORD",
    "CDEK_COMBO_LOOKBACK_DAYS",
    "CDEK_COMBO_EXO_PRICE",
]
values = {}
for line in text.splitlines():
    stripped = line.strip()
    for key in keys:
        prefix = key + ":"
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip().strip('"').strip("'")
            values.setdefault(key, value)

defaults = {
    "PORT": "8010",
    "PGHOST": "127.0.0.1",
    "PGPORT": "5433",
    "PGDATABASE": "bitrix_export",
    "PGUSER": "bitrix",
    "CDEK_COMBO_LOOKBACK_DAYS": "14",
    "CDEK_COMBO_EXO_PRICE": "5290",
}
for key, value in defaults.items():
    values.setdefault(key, value)

missing = [key for key in keys if key not in values]
if missing:
    raise SystemExit("missing keys: " + ",".join(missing))

Path(".env").write_text("\n".join(f"{key}={values[key]}" for key in keys) + "\n")
Path(".env").chmod(0o600)
print(f"env_created {backup.name} {len(keys)}")
