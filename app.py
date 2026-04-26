import os

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, url_for
from flask_login import current_user

from extensions import bcrypt, db, login_manager, migrate


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__)

    # ── Core config ────────────────────────────────────────────────────────────
    app.config["SECRET_KEY"] = _required_env("SECRET_KEY")

    # Railway (and most PaaS) issue postgres:// URLs; SQLAlchemy 1.4+ requires
    # postgresql://.  Rewrite the scheme transparently so both work.
    db_url = _required_env("DATABASE_URL")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ── Extensions ─────────────────────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to continue."
    login_manager.login_message_category = "info"

    # User loader must be registered after login_manager is initialised
    from models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    # ── Blueprints ─────────────────────────────────────────────────────────────
    from auth import bp as auth_bp
    from main import bp as main_bp
    from auth import LogoutForm

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_logout_form():
        if current_user.is_authenticated:
            return {"logout_form": LogoutForm()}
        return {}

    # ── Error handlers ─────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("main.dashboard"))
        return redirect(url_for("auth.login"))

    @app.errorhandler(403)
    def forbidden(e):
        return render_template(
            "error.html", code=403,
            title="Access Denied",
            message="That reading doesn't belong to your account.",
        ), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template(
            "error.html", code=404,
            title="Not Found",
            message="We couldn't find what you were looking for.",
        ), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template(
            "error.html", code=500,
            title="Something Went Wrong",
            message="An unexpected error occurred. Please try again.",
        ), 500

    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=False)
