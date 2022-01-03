FROM python:3.10

ENV PIP_NO_CACHE_DIR 1
ENV POETRY_VIRTUALENVS_CREATE 0

WORKDIR /app
COPY --chown=1000:1000 . ./

RUN pip install poetry
RUN poetry install --no-dev

USER 1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "error"]
