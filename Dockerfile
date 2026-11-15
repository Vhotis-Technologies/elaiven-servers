FROM python:3.13-slim

# Set the environment variable
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONHASHSEED=random

WORKDIR /app

# gcc for source builds; git for the django-celery-beat pin in requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY elaiven_server/ .

ENV DJANGO_SETTINGS_MODULE=config.settings
RUN python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
