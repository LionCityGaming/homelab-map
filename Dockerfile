FROM python:3.12-slim

# tzdata: "Last updated" in your TZ; openssh-client: reading a Caddyfile over SSH
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata openssh-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY homelab_map ./homelab_map

RUN useradd --uid 1000 --create-home homelab \
 && mkdir -p /data /config \
 && chown homelab:homelab /data
USER homelab
VOLUME /data
EXPOSE 8080
ENV HOMELAB_MAP_CONFIG=/config/config.yaml PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "homelab_map"]
CMD ["run"]
