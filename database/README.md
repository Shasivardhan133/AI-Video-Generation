# Database design artifacts

This folder holds documentation and placeholders only. It does not define the database: there is no SQL schema or migration here, and `schema/` contains only a `.gitkeep` placeholder.

The actual database is managed by Django:

- **Model:** `GenerationJob` in [`backend/apps/generator/models.py`](../backend/apps/generator/models.py), stored in the MySQL table `generation_jobs`.
- **Migration:** [`backend/apps/generator/migrations/0001_initial.py`](../backend/apps/generator/migrations/0001_initial.py), the only migration for that table. It is applied with `python manage.py migrate` from `backend/`, which also creates Django's own tables (sessions, users and permissions, admin log, content types, migration history).
- **Description:** [`docs/database-design.md`](../docs/database-design.md) documents the columns, indexes, statuses, constraints and retention policy.

MySQL stores application metadata, not image or video files. Uploaded images and generated videos are files under `media/`, and the database stores only their names.
