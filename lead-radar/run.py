"""Run Lead Radar using APP_HOST / APP_PORT values from .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')

if __name__ == '__main__':
    import uvicorn
    host = os.getenv('APP_HOST', '127.0.0.1')
    port = int(os.getenv('APP_PORT', '8000'))
    if host not in ('127.0.0.1', 'localhost', '::1') and not os.getenv('APP_PASSWORD'):
        raise SystemExit('允许其他设备访问前，请在 .env 设置 APP_PASSWORD，避免公开暴露线索数据。')
    uvicorn.run('app.main:app', host=host, port=port)
