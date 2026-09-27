# Image du job quotidien (cadrage §11.1). Lancée par le cron de l'hôte via `docker compose run --rm -T watcher`,
# jamais en permanence. Les configs (agents/), les fixtures et les données (data/) sont montées par compose.yaml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY watcher/ watcher/

# L'UID doit être celui du propriétaire de ./data sur l'hôte, sinon le conteneur ne peut pas écrire la base
# ni les logs (bind mount). 1000 = premier utilisateur d'Ubuntu (`ubuntu` sur les VPS OVHcloud).
ARG UID=1000
RUN useradd --create-home --uid "${UID}" watcher
USER watcher

CMD ["python", "-m", "watcher.run", "--all"]
