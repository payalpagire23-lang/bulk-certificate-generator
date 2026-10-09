"""Application factory."""
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify

from . import db, service
from .config import default_config
from .routes import bp


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(default_config())
    if overrides:
        app.config.update(overrides)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(app.config["STORAGE_DIR"], exist_ok=True)
    db.init_db(app.config["DATABASE"])

    if app.config["ASYNC_PROCESSING"]:
        executor = ThreadPoolExecutor(max_workers=app.config["MAX_WORKERS"], thread_name_prefix="certgen")
        app.extensions["executor"] = executor
        # Jobs interrupted by a restart are picked up again (process_job only handles 'pending' certificates).
        conn = db.connect(app.config["DATABASE"])
        try:
            for job_id in service.recover_unfinished_jobs(conn):
                executor.submit(service.process_job, dict(app.config), job_id)
        finally:
            conn.close()

    app.register_blueprint(bp)

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_):
        return jsonify({"error": "Method not allowed"}), 405

    @app.errorhandler(500)
    def server_error(_):
        return jsonify({"error": "Internal server error"}), 500

    return app
