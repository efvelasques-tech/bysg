FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN mkdir -p /app/data /app/backups
EXPOSE 4180
CMD ["python", "server.py"]
