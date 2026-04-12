FROM python:3.11-slim

RUN useradd -m -u 1000 rzd

WORKDIR /app

RUN pip install --no-cache-dir flask requests

COPY app.py .
COPY index.html .
COPY static/ ./static/

RUN mkdir -p /app/data && chown rzd:rzd /app/data

USER rzd

EXPOSE 5000

CMD ["python3", "app.py"]