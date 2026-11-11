FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PAGE_SIZE=10

WORKDIR /srv
COPY server.py ./

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-u", "server.py"]
