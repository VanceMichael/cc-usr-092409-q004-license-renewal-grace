"""Flask 应用入口。"""

from flask import Flask, jsonify
from sqlalchemy import text

from .api import create_api_blueprint
from .database import create_database_engine
from .models import Base


def create_app(engine=None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()
    app.config["ENGINE"] = storage

    # 测试与本地内存库直接建表；正式环境由 Alembic 迁移管理结构。
    Base.metadata.create_all(storage)

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(status="ok", storage="sqlite")

    app.register_blueprint(create_api_blueprint(storage))
    return app
