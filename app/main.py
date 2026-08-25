"""Flask application factory + waitress entrypoint.

Lifespan: load + validate config (fails loud on a bad reason/route), connect to MySQL and
apply migrations, register the API, serve the dashboard, and — when run as a server —
start the background workers. Served by waitress (pure-Python WSGI), which suits the
Windows/NSSM on-prem target and sits behind the same nginx config as the design.
"""

from __future__ import annotations

import logging

from flask import Flask, redirect, send_from_directory

from . import config, db
from .api.admin_api import bp as admin_bp
from .api.config_api import bp as config_bp
from .api.health import bp as health_bp
from .api.query import bp as query_bp
from .api.webhooks import bp as webhooks_bp
from .workers import SUPERVISOR


def create_app(start_workers: bool = False, cfg: "config.Config | None" = None) -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = cfg or config.load()
    if cfg.shadow_mode:
        logging.getLogger("ops").warning(
            "SHADOW MODE — no message will be sent on any channel. Every outbound "
            "message is written to logs/notifications.log instead. Set "
            "OPS_SHADOW_MODE=false (and replace the placeholder roster) to go live.")
    else:
        logging.getLogger("ops").warning(
            "LIVE MODE — messages will be delivered to real people on real channels.")
    db.init(cfg.db_params(), cfg.table_prefix)

    # config.load() ran before the database existed, so load_overrides() failed inside it
    # and returned {} — meaning cfg currently holds the YAML files alone. Apply the stored
    # overrides now that we can actually read them.
    #
    # Without this every change made through the config API survives until the next
    # restart and then silently reverts, which is worse than not offering the API at all:
    # the operator watched it take effect.
    stored = config.load_overrides()
    if stored:
        config.reload_into(cfg, stored)
        logging.getLogger("ops").info(
            "applied stored config overrides for: %s", ", ".join(sorted(stored)))

    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["OPS_CFG"] = cfg
    app.register_blueprint(health_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(query_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(admin_bp)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "dashboard.html")

    @app.get("/dashboard")
    def dashboard():
        return redirect("/")

    @app.get("/admin")
    def admin():
        # no-store, not merely no-cache: this page changes far more often than it is
        # loaded, and a cached copy is indistinguishable from a deploy that did not
        # land. Revalidation is not worth the confusion it costs.
        resp = send_from_directory(app.static_folder, "admin.html")
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    if start_workers:
        SUPERVISOR.start(cfg)

    return app


def serve() -> None:
    cfg = config.load()
    app = create_app(start_workers=True, cfg=cfg)
    from waitress import serve as waitress_serve

    logging.getLogger("ops").info("ops-core serving on http://%s:%s", cfg.host, cfg.port)
    waitress_serve(app, host=cfg.host, port=cfg.port, threads=8)


if __name__ == "__main__":
    serve()
