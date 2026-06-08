FROM python:3.12-alpine

RUN pip install --no-cache-dir cryptography

COPY encrypt.py /encrypt.py

RUN mkdir -p /encrypted /decrypted

ENTRYPOINT ["python", "/encrypt.py"]
