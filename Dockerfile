FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Ganti vinder.py menjadi svetiktok.py sesuai nama file aslimu
CMD ["python", "svetiktok.py"]