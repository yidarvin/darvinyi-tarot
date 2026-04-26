import os

from dotenv import load_dotenv
from flask import Flask, render_template

from extensions import bcrypt, db, login_manager


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__)

    # ── Core config ────────────────────────────────────────────────────────────
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]

    # Railway (and most PaaS) issue postgres:// URLs; SQLAlchemy 1.4+ requires
    # postgresql://.  Rewrite the scheme transparently so both work.
    db_url = os.environ["DATABASE_URL"]
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ── Extensions ─────────────────────────────────────────────────────────────
    db.init_app(app)
    login_manager.init_app(app)
    bcrypt.init_app(app)

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    # ── Error handlers ─────────────────────────────────────────────────────────
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

    # ── Database ───────────────────────────────────────────────────────────────
    # create_all() is a no-op for tables that already exist, so it's safe to
    # call on every startup without a migration tool.  Use Flask-Migrate for
    # schema changes once the app is in production.
    with app.app_context():
        db.create_all()

    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    create_app().run(host="0.0.0.0", port=port, debug=False)
