from __future__ import annotations

try:
    from celery import Celery  # type: ignore[import-untyped]

    from app.core.config import get_settings

    settings = get_settings()

    celery_app = Celery(
        "clipper",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
    )

    celery_app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_reject_on_worker_lost=True,
        task_time_limit=3600,
        task_soft_time_limit=3300,
        result_expires=3600,
        task_routes={
            "app.workers.tasks.transcribe_chunk": {"rate_limit": "10/m"},
        },
    )

    # Ensure task modules are imported
    celery_app.autodiscover_tasks(["app.workers"])
except ImportError:
    # Fallback stub when celery not installed (e.g. CI without redis)
    class _StubConf(dict):  # type: ignore[no-redef]
        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name)

        def __setattr__(self, name, value):  # type: ignore[no-untyped-def]
            self[name] = value

    class _StubApp:  # type: ignore[no-redef]
        def __init__(self):  # type: ignore[no-untyped-def]
            self.conf = _StubConf()

        def task(self, *a, **kw):  # type: ignore[no-untyped-def]
            def decorator(fn):
                # Attach .delay that just calls fn directly for stub
                def delay(*args, **kwargs):  # type: ignore[no-untyped-def]
                    class _Res:
                        def get(self):  # type: ignore[no-untyped-def]
                            return fn(*args, **kwargs)

                    return _Res()

                fn.delay = delay  # type: ignore[attr-defined]
                fn.s = lambda *aa, **kka: fn  # type: ignore[attr-defined]
                return fn

            return decorator

        def autodiscover_tasks(self, *a, **kw):  # type: ignore[no-untyped-def]
            pass

    celery_app = _StubApp()  # type: ignore[assignment]
    from app.core.config import get_settings as _get_settings  # type: ignore[import-not-found]

    settings = _get_settings()
