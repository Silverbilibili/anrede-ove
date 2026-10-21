# Cloud Deployment

## Docker (recommended)
```
docker-compose up -d
```

## Manual VPS
```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8030
```

## Railway/Render/Fly.io
- Build: `pip install -r requirements.txt`
- Start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- Health: `GET /api/v1/real/feed`

## Notes
- UID/template auto-persist to `runtime/`
- First boot login via HTTP (Chrome optional)
- Poll interval 0.5s, 8 market types
