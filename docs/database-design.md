# Database design

This describes the database as implemented. The source of truth is `backend/apps/generator/models.py` (`GenerationJob`) and its only migration, `backend/apps/generator/migrations/0001_initial.py`. The database is MySQL 8, accessed through Django's ORM; all timestamps are stored in UTC (`USE_TZ = True`, `TIME_ZONE = 'UTC'`).

## Connection and configuration

- **Driver:** `mysqlclient` (Django's MySQL backend). Django 5.2 connects with the `utf8mb4` character set, so 4-byte characters such as emoji are stored correctly.
- **Settings:** `DATABASES['default']` in `backend/config/settings.py`, from `MYSQL_DATABASE` (default `ai_video_generator`), `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST` (`127.0.0.1`) and `MYSQL_PORT` (`3306`), read from the environment or `.env`. There are no extra `OPTIONS`; Django's defaults apply:
  - transaction isolation `READ COMMITTED`;
  - no persistent connections (`CONN_MAX_AGE = 0`);
  - `ATOMIC_REQUESTS` off, which the active-job lock relies on.
- **Strict mode:** the server runs with `STRICT_TRANS_TABLES`, so a value that does not fit its column raises an error instead of being cut. Exception: MySQL trims trailing spaces and newlines beyond the column length with only a warning. Django's `manage.py check --database default` warns if strict mode is off.
- **Tests** use Django's test database, `test_<MYSQL_DATABASE>`, created and dropped by the test runner, and never the application database.
- **Migrations:** apply them with `python manage.py migrate` from `backend/`. `python manage.py migrate --check` (exit code 0 when nothing is unapplied) and `python manage.py makemigrations --check --dry-run` check the state without changing anything.

## Tables

The application defines one table, `generation_jobs`. The other tables are Django's own:

| Table(s) | Source | Use in this project |
|---|---|---|
| `generation_jobs` | `apps.generator` | One row per generation request (below). |
| `django_session` | `django.contrib.sessions` | Browser sessions. Guests own their jobs through their session key. |
| `auth_user`, `auth_group`, `auth_permission` and their link tables | `django.contrib.auth` | Django's standard user model. There is no sign-up or login API; users are created with `createsuperuser` or in the admin. A request from a signed-in session owns its jobs as that user. |
| `django_admin_log`, `django_content_type`, `django_migrations` | Django | Admin history, model registry, applied migrations. |

Media bytes are never stored in the database. Images and videos are files under `MEDIA_ROOT`; the database stores only their relative names.

## `generation_jobs`

| Column | Django field | Null / default | Purpose |
|---|---|---|---|
| `id` | `UUIDField`, primary key | `uuid4()`, not editable | Public job ID used in API URLs. |
| `user_id` | `ForeignKey(AUTH_USER_MODEL)`, `on_delete=CASCADE` | nullable | Owner when the request came from a signed-in user; `NULL` for guests. |
| `session_key` | `CharField(40)`, indexed | `''` | Owner when the request came from a guest: the Django session key. Empty for signed-in owners. Not a foreign key to `django_session`. |
| `mode` | `CharField(20)`, choices | required | `text_to_video` or `image_to_video`. |
| `prompt_text` | `TextField` | `''` | The prompt as submitted, after trimming (at most 1,000 characters, enforced by the API). Empty is allowed for image-to-video. |
| `settings` | `JSONField` | `{}` | Validated, complete generation settings: `model_profile`, `width`, `height`, `frame_count`, `fps`, `seed`, `style`, `enhance_prompt`. |
| `status` | `CharField(20)`, choices | `queued` | `queued`, `processing`, `completed` or `failed`. |
| `input_file` | `FileField(upload_to='inputs/%Y/%m/')` | `''` | Uploaded image for image-to-video jobs, stored as `<prompt keyword>-<uuid>.<ext>`; empty for text-to-video. |
| `output_file` | `FileField(upload_to='outputs/%Y/%m/')` | `''` | The verified MP4, set only when the job completes. |
| `remote_job_id` | `CharField(100)` | `''` | The AI service's own job ID, for diagnostics (`mock-<id>` in mock mode). |
| `error_code` | `CharField(50)` | `''` | Stable failure category, for example `service_timeout`, `queue_timeout`, `cancelled`, `worker_interrupted`. |
| `error_message` | `CharField(500)` | `''` | User-safe failure message. Technical details are only in server logs. |
| `request_id` | `CharField(64)` | `''` | The creating request's ID, for correlating with logs. |
| `created_at` | `DateTimeField` | `timezone.now` | When the job was created. |
| `started_at` | `DateTimeField` | nullable | When a worker claimed it. |
| `completed_at` | `DateTimeField` | nullable | When it completed or failed. |
| `updated_at` | `DateTimeField(auto_now=True)` | set on every write | Also the worker lease: a `processing` job's heartbeat refreshes it. |

Default ordering is newest first (`-created_at`).

**MySQL storage.** The migrated table:
- is InnoDB, `utf8mb4_0900_ai_ci`;
- stores `id` as `char(32)` (a UUID without dashes) and `user_id` as `int`;
- stores `prompt_text` as `longtext` and `settings` as `json`;
- stores the four timestamps as `datetime(6)`, which keeps microseconds;
- stores the two file columns as `varchar(100)`, Django's default `FileField` length.

Stored names always fit those file columns:
- **Inputs** are at most 77 characters: `inputs/YYYY/MM/`, a keyword of at most 24 characters, `-`, 32 hex digits and the extension. Storage's collision suffix adds 8 more.
- **Outputs** are 56 characters: `outputs/YYYY/MM/<job id>.mp4`.

### Indexes

| Index | Used by |
|---|---|
| Primary key `(id)` | Job detail, video and cancel. |
| `(user_id, created_at)` | A signed-in user's history. It also serves the foreign key. |
| `(session_key, created_at)` | A guest's history and active-job check. |
| `(session_key)` | Nothing that the composite index above does not already cover (see below). |
| `(status, created_at)` | Claiming the oldest queued job, stale-job recovery and queue expiry. MySQL also chose it for a signed-in user's active-job check, because few jobs are active. |

**Review (Section 3.6).** I ran `EXPLAIN` on the application's actual query shapes against the test database, with 6,000 rows across 1,500 guest sessions and one user with 300 jobs. Every query used an index:
- history pages, newest first, read in index order without a sort step, including page offsets and the `status` and `mode` filters;
- claiming, recovery and expiry used `(status, created_at)`;
- detail used the primary key.

The only sort step was in a signed-in user's active-job check, over a few rows. **No additional index is needed.** The single-column `session_key` index is redundant with `(session_key, created_at)`. It costs a little on every insert but is otherwise harmless, and removing it would need a migration, so it is left in place (see Future work).

**Ordering ties.** History is ordered by `created_at` alone. Two jobs of one owner cannot share a timestamp in practice: the per-owner lock serialises job creation, and timestamps keep microseconds. So pages are stable. A tie-breaker on `id` would only matter if timestamps could repeat.

## Statuses and transitions

| From | Allowed to |
|---|---|
| `queued` | `processing` (claimed by a worker), `failed` (queue timeout or cancelled by its owner) |
| `processing` | `completed`, `failed` |
| `completed` | none (final) |
| `failed` | none (final) |

There is no separate `cancelled` status: a cancelled job is `failed` with `error_code = 'cancelled'`. See [architecture.md](architecture.md#generation-job-lifecycle) for how each transition is made safe.

## Constraints and what enforces them

| Rule | Enforced by |
|---|---|
| Primary key, `user_id` foreign key, `NOT NULL` columns, column lengths, column types (strict mode) | MySQL (from the migration). The foreign key is created without an `ON DELETE` rule (MySQL reports `NO ACTION`); Django's ORM performs the `CASCADE`. |
| `status` and `mode` are one of the listed values | The application (Django `choices`, the API and the worker). There is no database `CHECK` constraint. |
| Only the transitions above happen | The application: conditional updates and the guarded `transition_to()` / `save()` in `models.py`. Not the database. |
| A `completed` job has an `output_file`; a `failed` job has an `error_code` | The worker and the API. Not the database. |
| One active (`queued` or `processing`) job per owner | The API, under a per-owner MySQL named lock (`GET_LOCK`). Not a database constraint. |

Writes that bypass the model, such as raw SQL, `QuerySet.update()` or `bulk_update()`, are not checked by these application rules. The application's own conditional updates are written to respect them.

## Relationships and deletion

- A user owns many jobs. Deleting a user through Django (the admin, `user.delete()`) deletes their jobs and nobody else's (`CASCADE`, carried out by the ORM). A raw SQL `DELETE` of a user who still has jobs is refused by MySQL, because the foreign key has no `ON DELETE` rule, so jobs can never point at a missing user.
- A guest's jobs are linked to the session only by `session_key`. Deleting or expiring a session (for example with `manage.py clearsessions`) does not delete its jobs, but they can no longer be reached through the API, even with the old cookie.
- A job references at most one input file and one output file.
- **Deleting a row does not delete its files.** Deleting a job or a user (and its cascaded jobs) leaves the files in media storage.

## Retention

**Policy (decided for development and the final-year demo):**

- **Keep everything.** Uploaded images and generated videos are kept indefinitely, and so are job rows. Failed and cancelled jobs keep their input images.
- **No automatic deletion.** No code deletes media on a schedule or when rows are deleted. Deleting a job or user leaves its files in media storage.
- **Report first, then delete by hand.** `python manage.py find_orphaned_media` is a read-only report of files that no row refers to; it has no deletion option. Any deletion is a manual step, made only after someone has reviewed that report. See `backend/README.md`.
- **Revisit before public deployment.** A public deployment needs a policy with time limits for inputs, outputs and failed jobs, and a matching cleanup process.

The application itself deletes a file only when that file was just created by the current operation and no row refers to it:

- a video the worker has just written but could not record, because the job was recovered or deleted while it ran;
- a partly written upload or video, left behind when the write failed (for example, a full disk);
- an uploaded image whose job row could not be inserted, once a fresh read confirms that no job refers to it. If that read fails, the image is kept, and `find_orphaned_media` reports it.

**Consistency of references.**

- A job refers to an output only once it is `completed`. The worker records the name only after the verified file is saved, and only while the job is still `processing`.
- Failed, cancelled, expired and recovered jobs never refer to an output. They keep their input image reference.
- A reference that leads outside `MEDIA_ROOT` (traversal, an absolute path, a link to outside) or to a directory is treated as missing. The video endpoint returns `404`, and the worker fails the job with `input_missing`.

## Tests

`backend/tests/test_database.py` checks this document against the migrated test database:
- migrations match the models, and all are applied;
- the table, columns, character set, indexes and foreign key are as described;
- defaults, UTC and microsecond timestamps, JSON settings and 4-byte text are stored as described;
- stored file names fit their columns;
- deleting users, jobs and guest sessions behaves as described.

The lifecycle, concurrency and ownership rules are covered by the job, worker, transition, active-job, cancellation and queue-timeout test modules.

## Future work (not implemented)

- A database `CHECK` constraint on `status` values, or rules such as "a completed job has an output". These need a migration.
- Removing the redundant single-column `session_key` index (`db_index=True` on the field). This needs a migration; the gain is only slightly cheaper inserts.
- A normalized settings table. Settings are a validated JSON object on the job, which is enough while nothing queries individual settings.
- Accounts with sign-up and login, and a deletion policy for users that also removes their files.
