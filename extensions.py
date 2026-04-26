"""Shared Flask extension instances.

Instantiated here (not in app.py) to avoid circular imports:
  app.py imports extensions → models.py imports extensions → no cycle.
Each extension is initialised against the app in create_app() via .init_app().
"""

from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
bcrypt = Bcrypt()
