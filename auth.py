"""Authentication and onboarding Blueprint.

Registers the following routes (no url_prefix — mounted at root):
  GET/POST  /register
  GET/POST  /login
  POST      /logout
  GET/POST  /onboarding   (login_required)

Redirects to url_for("main.dashboard") after login/register/onboarding.
Requires the "main" blueprint to be registered in create_app().
"""

from urllib.parse import urlparse

import anthropic
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import (
    EmailField,
    HiddenField,
    PasswordField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError

from crypto import decrypt_api_key, encrypt_api_key
from extensions import bcrypt, db
from models import User, UserProfile

bp = Blueprint("auth", __name__)

_SESSION_QUESTION_KEY = "onboarding_question"

_ONBOARDING_TOPICS = [
    "Tell me a little about yourself — who are you and what's your life like right now?",
    "What areas of life are you most focused on or navigating right now?"
    " (e.g. relationships, career, health, family)",
    "How do you personally relate to tarot?"
    " Are you a skeptic, a believer, or somewhere in between?",
    "Is there anything specific you'd like your readings to focus on or be mindful of?",
]


# ── Forms ──────────────────────────────────────────────────────────────────────


class RegisterForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    username = StringField(
        "Username", validators=[DataRequired(), Length(min=2, max=80)]
    )
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "Confirm Password", validators=[DataRequired(), EqualTo("password")]
    )
    anthropic_api_key = StringField(
        "Anthropic API Key", validators=[DataRequired(), Length(min=10, max=512)]
    )
    submit = SubmitField("Create Account")

    def validate_email(self, field):
        if User.query.filter_by(email=field.data.strip().lower()).first():
            raise ValidationError("That email is already registered.")

    def validate_username(self, field):
        if User.query.filter_by(username=field.data.strip()).first():
            raise ValidationError("That username is already taken.")


class LoginForm(FlaskForm):
    identifier = StringField(
        "Email or Username", validators=[DataRequired(), Length(max=255)]
    )
    password = PasswordField("Password", validators=[DataRequired()])
    next_url = HiddenField()
    submit = SubmitField("Log In")


class OnboardingAnswerForm(FlaskForm):
    answer = TextAreaField(
        "Your answer", validators=[DataRequired(), Length(min=1, max=2000)]
    )
    submit = SubmitField("Next →")


class LogoutForm(FlaskForm):
    submit = SubmitField("Log Out")


# ── Onboarding helpers ─────────────────────────────────────────────────────────


def _generate_question(api_key: str, step: int, prior_qas: list) -> str:
    """Call claude-sonnet-4-20250514 to produce a warm, personalised question.

    Prior answers are included as context so later questions can acknowledge
    what the user has already shared. Falls back to the static topic on any error.
    """
    topic = _ONBOARDING_TOPICS[step]
    try:
        client = anthropic.Anthropic(api_key=api_key)
        prior_context = (
            "\n".join(f"Q: {qa['question']}\nA: {qa['answer']}" for qa in prior_qas)
            if prior_qas
            else "This is the first question — no prior context yet."
        )
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=256,
            system=(
                "You are a warm, curious tarot companion conducting a brief onboarding "
                "conversation. Ask ONE question based on the provided topic. "
                "Let prior answers subtly inform your tone where relevant. "
                "Keep it to 1–3 sentences. No numbering, no preamble, no sign-off."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Prior answers from this person:\n{prior_context}\n\n"
                        f"Topic for the next question: {topic}"
                    ),
                }
            ],
        )
        return msg.content[0].text.strip()
    except Exception:
        return topic


def _safe_next(url: str, fallback: str) -> str:
    """Return url only if it is a same-origin relative path; otherwise fallback.

    Prevents open-redirect attacks via the ?next= parameter.
    """
    if not url:
        return fallback
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return fallback
    return url


def _clear_onboarding_session() -> None:
    session.pop(_SESSION_QUESTION_KEY, None)


# ── Routes ─────────────────────────────────────────────────────────────────────


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        user = User(
            email=form.email.data.strip().lower(),
            username=form.username.data.strip(),
            password_hash=bcrypt.generate_password_hash(form.password.data).decode(),
            llm_api_key_encrypted=encrypt_api_key(form.anthropic_api_key.data.strip()),
        )
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for("auth.onboarding"))

    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if request.method == "GET":
        form.next_url.data = request.args.get("next", "")

    if form.validate_on_submit():
        identifier = form.identifier.data.strip()
        user = User.query.filter(
            (User.email == identifier.lower()) | (User.username == identifier)
        ).first()

        if user and bcrypt.check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            session["show_checkin"] = True
            next_url = form.next_url.data or request.args.get("next", "")
            return redirect(_safe_next(next_url, url_for("main.dashboard")))

        flash("Invalid email/username or password.", "error")

    return render_template("auth/login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    form = LogoutForm()
    if not form.validate_on_submit():
        flash("Invalid logout request. Please try again.", "error")
        return redirect(url_for("main.dashboard"))
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    total = len(_ONBOARDING_TOPICS)

    # Determine how far the user has progressed by counting saved answers
    existing = (
        UserProfile.query.filter_by(user_id=current_user.id)
        .order_by(UserProfile.created_at)
        .all()
    )
    if len(existing) >= total:
        _clear_onboarding_session()
        return redirect(url_for("main.dashboard"))

    step = len(existing)  # 0-based index of the next unanswered question
    form = OnboardingAnswerForm()

    if form.validate_on_submit():
        # Use the question text that was shown to the user, stored in session at GET time
        question_text = session.get(_SESSION_QUESTION_KEY) or _ONBOARDING_TOPICS[step]
        db.session.add(
            UserProfile(
                user_id=current_user.id,
                question=question_text,
                answer=form.answer.data.strip(),
            )
        )
        db.session.commit()

        _clear_onboarding_session()

        if step + 1 >= total:
            return redirect(url_for("main.dashboard"))

        return redirect(url_for("auth.onboarding"))

    # GET: generate the question for this step.
    # Re-use the cached version if the form just failed validation so the
    # user sees the same question they were answering.
    question_text = session.get(_SESSION_QUESTION_KEY)
    if not question_text:
        prior_qas = [{"question": p.question, "answer": p.answer} for p in existing]
        try:
            api_key = decrypt_api_key(current_user.llm_api_key_encrypted)
            question_text = _generate_question(api_key, step, prior_qas)
        except Exception:
            question_text = _ONBOARDING_TOPICS[step]
        session[_SESSION_QUESTION_KEY] = question_text

    return render_template(
        "auth/onboarding.html",
        form=form,
        question=question_text,
        step=step + 1,
        total=total,
    )
