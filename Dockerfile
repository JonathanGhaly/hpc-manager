FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/aic-hpc-manager
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY main.py ./main.py
COPY config.yaml.example ./config.yaml.example

RUN useradd --system --uid 10001 --create-home hpcmanager \
    && mkdir -p /var/log/aic-hpc-manager \
    && chown -R 10001:10001 /opt/aic-hpc-manager /var/log/aic-hpc-manager

USER 10001:10001
CMD ["sh", "-c", "sleep infinity"]
