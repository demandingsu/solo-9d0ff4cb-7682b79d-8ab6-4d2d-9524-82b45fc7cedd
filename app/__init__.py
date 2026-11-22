"""Flask 应用工厂。"""
import os

from flask import Flask


def create_app(config=None):
    app = Flask(__name__)
    app.config["DATABASE"] = os.environ.get(
        "MSO_DB", os.path.join(app.instance_path, "manuscript.db")
    )
    if config:
        app.config.update(config)
    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    from . import db

    db.init_app(app)

    from . import routes

    app.register_blueprint(routes.bp)
    return app
