FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir discord.py plexapi aiohttp docker requests

COPY debuggarr.py /app/debuggarr.py

ENTRYPOINT ["python", "-u", "/app/debuggarr.py"]
CMD ["--env-file", "/run/secrets/debuggarr.env"]
