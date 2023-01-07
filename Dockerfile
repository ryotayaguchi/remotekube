FROM python:3.11
WORKDIR /usr/src/app
RUN pip install --no-cache-dir -Iv requests_http_signature==v0.1.0
RUN pip install --no-cache-dir kubernetes
RUN pip install --no-cache-dir schedule
COPY src/ .
CMD ["python","remotekube.py"]
