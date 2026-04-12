FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir flask requests

COPY app.py .
COPY index.html .
COPY static/ ./static/

EXPOSE 5000

CMD ["python3", "app.py"]