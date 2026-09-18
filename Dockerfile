# DF VPN Bot — образ для запуска в Docker / docker-compose
# Автор: @Dolov07KBR (https://github.com/Dolov07KBR/DF-VPN-Bot)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала зависимости — так кешируется слой
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Данные (база, логи) живут в томе
RUN mkdir -p /app/data && useradd --create-home --shell /usr/sbin/nologin dfvpn \
    && chown -R dfvpn:dfvpn /app
USER dfvpn

VOLUME ["/app/data"]

# Порт нужен только при WEBHOOK_ENABLED=true
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import pathlib,sys; sys.exit(0 if pathlib.Path('/app/bot.py').exists() else 1)"

CMD ["python", "bot.py"]
