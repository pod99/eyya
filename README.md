# EYYA server migration

Migration export is in progress. This repository is not yet a complete backup.

Application source and restoration instructions belong here. Databases,
mailboxes, private keys, credentials, sessions, and production environment files
are exported separately to a private local archive. Do not retire the original
server until that archive is downloaded and verified.

`scripts/start-transfer.py` starts a temporary outbound Cloudflare QUIC transport
to the existing SSH service for the migration. It preserves SSH authentication
and must be stopped when the export is finished.

## Снимок исходников

Добавлены исходники основных приложений из `/opt` рабочего сервера; см. [RESTORE.md](RESTORE.md). Из публичной копии удалены обнаруженные рабочие секреты, данные и зависимости. Полная приватная копия готовится отдельно. Восстановление на новом сервере пока не проверено.
