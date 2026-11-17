# support-twin container (experimental: the reference platform is Windows + Python 3.12).
FROM python:3.12-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e ".[test]" \
 && mkdir -p /data/config /data/data \
 && cp config.example.yaml /data/config/config.yaml
ENV TWIN_AGENT_HOME=/data
VOLUME ["/data"]
ENTRYPOINT ["support-twin"]
CMD ["demo"]
