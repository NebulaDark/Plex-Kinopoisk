FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
RUN groupadd -g 10001 app && useradd -u 10001 -g app -M app && mkdir /data && chown app:app /data
COPY --chown=app:app app ./app
COPY --chown=app:app tests ./tests
USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3)"
CMD ["python", "-m", "app.server"]
