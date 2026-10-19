# Backend

This directory contains the initial Django and Django REST Framework project scaffolding. It will eventually validate requests, create and track generation jobs, authorize access, store metadata in MySQL, manage file references, and communicate with the protected AI service.

The generated `manage.py`, `config/settings.py`, `config/urls.py`, `config/asgi.py`, and `config/wsgi.py` provide only the project foundation. Domain applications, migrations, API endpoints, authentication, and `requirements.txt` remain deferred to later phases. Django generated these files; they were not manually fabricated.

Settings load local environment variables from the project-root `.env` file when present. The committed `.env.example` contains placeholders only. MySQL configuration is prepared but no database connection or migrations are run by this setup step.
