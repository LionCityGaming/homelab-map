# draw.io's official export server, plus a colour emoji font (the stock image has none, so the
# emoji in group titles and the 🌐 marker would render as empty boxes).
FROM jgraph/export-server:latest
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f
USER pptruser
