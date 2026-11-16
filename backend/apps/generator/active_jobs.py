"""At most one active (queued or processing) generation job per owner.

The limit is settings.GENERATION_MAX_ACTIVE_JOBS_PER_OWNER (1). It is not read
from the environment; the test suite raises it only for tests about other
features, which post several times from one session without a worker.

The frontend already waits for one video before sending the next, but that is
a convenience, not a control: any client can post again. The API enforces the
rule itself: a new generation request while the owner has an active job gets
409 `generation_in_progress`, and nothing is created or stored.

Owner: a signed-in user, or a guest's browser session (the same ownership the
read endpoints use).

Concurrency. "Check for an active job, then create one" is a race if two
requests from one owner run at once: both could see no active job. Each
owner's check and insert therefore run under a MySQL named lock (GET_LOCK),
which serializes them across every web process and database connection. The
insert commits (autocommit; ATOMIC_REQUESTS is off) before the lock is
released, so the next request for that owner always sees it. Named locks are
separate from row locks, so the worker's SKIP LOCKED claiming, heartbeats and
recovery are unaffected, and MySQL releases a lock by itself if the connection
that holds it closes. A request that cannot get the lock within
OWNER_LOCK_WAIT_SECONDS is itself proof that another request from the same
owner is in progress, so it gets 409 as well.
"""

import hashlib
from contextlib import contextmanager

from django.conf import settings
from django.db import connection
from rest_framework import status
from rest_framework.exceptions import APIException

from .models import GenerationJob

ACTIVE_STATUSES = (GenerationJob.Status.QUEUED, GenerationJob.Status.PROCESSING)
OWNER_LOCK_WAIT_SECONDS = 5
LOCK_PREFIX = 'geetha.generation.'  # MySQL lock names are limited to 64 characters


class GenerationInProgress(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'generation_in_progress'
    default_detail = 'Another video is still being generated. Wait for it to finish, then try again.'


class OwnerLockError(Exception):
    """GET_LOCK failed with an error (not a timeout)."""


def owner_identity(request, session_key):
    if request.user.is_authenticated:
        return f'user:{request.user.pk}'
    return f'session:{session_key}'


def lock_name(owner):
    # Hashed: the session key must never appear in MySQL's lock tables or process list.
    return LOCK_PREFIX + hashlib.sha256(owner.encode()).hexdigest()[:40]


@contextmanager
def owner_lock(owner):
    """Hold the owner's named lock for the block; raise GenerationInProgress on timeout."""
    name = lock_name(owner)
    with connection.cursor() as cursor:
        cursor.execute('SELECT GET_LOCK(%s, %s)', [name, OWNER_LOCK_WAIT_SECONDS])
        (acquired,) = cursor.fetchone()
    if acquired == 0:
        raise GenerationInProgress()
    if acquired != 1:
        raise OwnerLockError(f'GET_LOCK returned {acquired!r}')
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT RELEASE_LOCK(%s)', [name])


def ensure_no_active_job(owned_jobs):
    """Raise GenerationInProgress if the owner already has the maximum number of active jobs."""
    limit = settings.GENERATION_MAX_ACTIVE_JOBS_PER_OWNER
    if owned_jobs.filter(status__in=ACTIVE_STATUSES)[:limit].count() >= limit:
        raise GenerationInProgress()
