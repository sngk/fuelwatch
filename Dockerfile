FROM python:3.12-alpine
WORKDIR /app
COPY fuelwatch_bot.py config.json ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "fuelwatch_bot.py"]
