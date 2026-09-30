FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html ./
COPY vendor ./vendor
COPY fonts ./fonts
RUN useradd -r -u 1000 app && mkdir /data && chown app /data
USER app
EXPOSE 8765
CMD ["python", "-u", "server.py", "--host", "0.0.0.0", "--port", "8765", "--data", "/data", "--no-browser"]
