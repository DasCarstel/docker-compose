FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
      "fastapi>=0.115,<0.116" \
      "uvicorn[standard]>=0.30" \
      "psycopg[binary]>=3.2" \
      "python-multipart>=0.0.9" \
      "pillow>=10.4"

COPY app.py /app/app.py

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
