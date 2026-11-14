import os
import modal

app = modal.App("witnessed-gemini")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .add_local_dir("app", "/root/app")
    .add_local_dir("witnessed", "/root/witnessed")
)

volume = modal.Volume.from_name("witnessed-gemini-data", create_if_missing=True)


@app.function(
    image=image,
    volumes={"/data": volume},
    secrets=[modal.Secret.from_name("witnessed-gemini")],
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def fastapi_app():
    os.environ.setdefault("DATA_DIR", "/data/files")
    os.environ.setdefault("DB_PATH", "/data/witnessed.db")
    os.environ.setdefault("SEAL_KEY_PATH", "/data/seal_key")
    os.environ.setdefault("MODEL_PROVIDER", "gemini")
    os.environ.setdefault("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.5-flash")

    from starlette.middleware.base import BaseHTTPMiddleware
    from app.main import app as web_app

    class VolumeCommitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if request.method != "GET":
                try:
                    volume.commit()
                except Exception:
                    pass
            return response

    web_app.add_middleware(VolumeCommitMiddleware)
    return web_app
