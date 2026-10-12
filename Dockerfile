# ShopAssist Chat API — phone-app facing layer over agent.run_turn().
# Slim image: only fastapi+uvicorn (no streamlit needed for /api/chat).
FROM python:3.11-slim

WORKDIR /app

COPY requirements-api.txt /app/requirements-api.txt
RUN pip install --no-cache-dir -r requirements-api.txt

COPY agent.py database.py tools.py action_gateway.py mock_warden.py abuse_guard.py chat_api.py /app/

# WARDEN_BASE_URL, WARDEN_AGENT_ID, WARDEN_API_KEY, APP_API_KEY,
# LLM_* come from Render env vars (never baked into the image).
ENV PYTHONPATH=/app

EXPOSE 8502

CMD ["sh", "-c", "python -m uvicorn chat_api:app --host 0.0.0.0 --port ${PORT:-8502}"]
