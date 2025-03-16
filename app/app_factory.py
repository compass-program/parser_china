from fastapi import FastAPI
from app.router import route
from transfer_data.socketio_server import app as socket_app, origins
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from app.auth.router import router as auth_router

def create_app() -> FastAPI:
    """
    Creates and configures the FastAPI application instance.

    :return: FastAPI application instance.
    """
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=['*'],
            allow_headers=['*']
        )
    ]
    app = FastAPI(middleware=middleware)
    app.include_router(auth_router)
    app.include_router(route)

    # Mount SocketIO application into FastAPI
    app.mount("/socket.io", socket_app)

    return app
