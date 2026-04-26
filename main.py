"""Main application Blueprint.

Registers (all login_required):
  GET       /dashboard
  GET/POST  /life-update
  GET       /reading/setup
  POST      /reading/start
  GET       /reading/<id>/card/<n>
  GET       /reading/<id>/card/<n>/stream    (SSE)
  GET       /reading/<id>/summary
  GET       /history
  GET       /cards/<filename>               (card image assets)
"""

import os
from typing import Dict, List, Optional, Tuple

import anthropic as _anthropic
from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy.orm.attributes import flag_modified
from wtforms import RadioField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length
from wtforms.validators import Optional as WTOptional

from crypto import decrypt_api_key, encrypt_api_key
from extensions import db
# _CARD_SYSTEM / _SUMMARY_SYSTEM are module-level constants; imported here so the
# streaming endpoint can reuse the exact same prompts without duplicating them.
from interpreter import (
    TarotInterpreter,
    _CARD_SYSTEM,
    _SUMMARY_SYSTEM,
    parse_spread_markdown,
)
from models import Reading, UserProfile
from spread import (
    DEFAULT_REVERSAL_PROBABILITY,
    create_standard_tarot_deck,
    draw_celtic_cross_spread,
    draw_three_card_spread,
)

bp = Blueprint("main", __name__)


class _ApiKeyError(Exception):
    """Raised when the user has no API key or the stored token can't be decrypted."""

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_STANDARD_CARDS_DIR = os.path.join(_PROJECT_ROOT, "cards", "standard")

# ── Card image filename mapping (authoritative copy; app.py will defer to this) ─

_MAJOR_TO_FILENAME: Dict[str, str] = {
    "The Fool": "RWS_Tarot_00_Fool.jpg",
    "The Magician": "RWS_Tarot_01_Magician.jpg",
    "The High Priestess": "RWS_Tarot_02_High_Priestess.jpg",
    "The Empress": "RWS_Tarot_03_Empress.jpg",
    "The Emperor": "RWS_Tarot_04_Emperor.jpg",
    "The Hierophant": "RWS_Tarot_05_Hierophant.jpg",
    "The Lovers": "RWS_Tarot_06_Lovers.jpg",
    "The Chariot": "RWS_Tarot_07_Chariot.jpg",
    "Strength": "RWS_Tarot_08_Strength.jpg",
    "The Hermit": "RWS_Tarot_09_Hermit.jpg",
    "Wheel of Fortune": "RWS_Tarot_10_Wheel_of_Fortune.jpg",
    "Justice": "RWS_Tarot_11_Justice.jpg",
    "The Hanged Man": "RWS_Tarot_12_Hanged_Man.jpg",
    "Death": "RWS_Tarot_13_Death.jpg",
    "Temperance": "RWS_Tarot_14_Temperance.jpg",
    "The Devil": "RWS_Tarot_15_Devil.jpg",
    "The Tower": "RWS_Tarot_16_Tower.jpg",
    "The Star": "RWS_Tarot_17_Star.jpg",
    "The Moon": "RWS_Tarot_18_Moon.jpg",
    "The Sun": "RWS_Tarot_19_Sun.jpg",
    "Judgement": "RWS_Tarot_20_Judgement.jpg",
    "The World": "RWS_Tarot_21_World.jpg",
}

_RANK_TO_NUMBER: Dict[str, int] = {
    "Ace": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5,
    "Six": 6, "Seven": 7, "Eight": 8, "Nine": 9, "Ten": 10,
    "Page": 11, "Knight": 12, "Queen": 13, "King": 14,
}

_SUIT_TO_PREFIX: Dict[str, str] = {
    "Wands": "Wands",
    "Cups": "Cups",
    "Swords": "Swords",
    "Pentacles": "Pents",
}

_CARD_WIDTH = 140
_CARD_HEIGHT = 240
_GAP_X = 24
_GAP_Y = 24


def _split_card_orientation(card: str) -> Tuple[str, str]:
    if card.endswith("(Reversed)"):
        return card.replace("(Reversed)", "").strip(), "reversed"
    return card, "upright"


def _card_filename(title: str) -> Optional[str]:
    if title in _MAJOR_TO_FILENAME:
        return _MAJOR_TO_FILENAME[title]
    if " of " in title:
        rank, suit = title.split(" of ", 1)
        num = _RANK_TO_NUMBER.get(rank)
        prefix = _SUIT_TO_PREFIX.get(suit)
        if num and prefix:
            return f"{prefix}{num:02d}.jpg"
    return None


# ── Forms ──────────────────────────────────────────────────────────────────────


class LifeUpdateForm(FlaskForm):
    update_text = TextAreaField(
        "What's been going on?",
        validators=[DataRequired(), Length(min=1, max=2000)],
    )
    submit = SubmitField("Save & Start Reading")


class ReadingSetupForm(FlaskForm):
    spread_type = RadioField(
        "Choose your spread",
        choices=[
            ("3card", "3-Card Spread — Past, Present, Future"),
            ("celticcross", "Celtic Cross — A deeper 10-card exploration"),
        ],
        default="3card",
        validators=[DataRequired()],
    )
    intention = TextAreaField(
        "Question or intention for this reading (optional)",
        validators=[WTOptional(), Length(max=500)],
    )
    submit = SubmitField("Draw Cards")


class SettingsForm(FlaskForm):
    new_api_key = StringField(
        "New Anthropic API Key",
        validators=[DataRequired(), Length(min=10, max=512)],
    )
    submit = SubmitField("Update Key")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _build_user_context(user_id: int) -> str:
    """Concatenate all UserProfile rows for this user into a readable string for the LLM."""
    entries = (
        UserProfile.query.filter_by(user_id=user_id)
        .order_by(UserProfile.created_at)
        .all()
    )
    if not entries:
        return ""
    parts = []
    for e in entries:
        if e.question == "Life update":
            when = e.created_at.strftime("%B %Y") if e.created_at else "recently"
            parts.append(f"Life update ({when}):\n{e.answer}")
        else:
            parts.append(f"Q: {e.question}\nA: {e.answer}")
    return "\n\n".join(parts)


def _make_interpreter(spread_key: str) -> TarotInterpreter:
    """Create a TarotInterpreter using the current user's decrypted API key.

    Raises _ApiKeyError if the key is absent or cannot be decrypted.
    """
    if not current_user.llm_api_key_encrypted:
        raise _ApiKeyError("no_api_key")
    try:
        api_key = decrypt_api_key(current_user.llm_api_key_encrypted)
    except Exception as exc:
        raise _ApiKeyError("decrypt_failed") from exc
    return TarotInterpreter(spread_key, api_key)


def _prior_from_cards(cards: List[Dict], up_to_index: int) -> List[Dict]:
    """Build the prior-interpretations list for cards[:up_to_index] (0-based slice)."""
    return [
        {
            "position_index": c["index"],
            "position_label": c["position_label"],
            "card": c["card"],
            "orientation": c["orientation"],
            "interpretation": c.get("interpretation", ""),
        }
        for c in cards[:up_to_index]
    ]


def _sse(text: str) -> str:
    """Format a text chunk as an SSE data event, handling embedded newlines."""
    if not text:
        return ""
    return "".join(f"data: {line}\n" for line in text.splitlines()) + "\n"


_SSE_DONE = "data: [DONE]\n\n"
_SSE_ERROR = "data: [ERROR]\n\n"

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # prevent nginx from buffering SSE chunks
}


# ── Routes ─────────────────────────────────────────────────────────────────────


@bp.get("/dashboard")
@login_required
def dashboard():
    # Consumed once per login; session.pop means refreshing won't re-show the prompt.
    show_checkin = session.pop("show_checkin", False)
    last_reading = (
        Reading.query.filter_by(user_id=current_user.id)
        .order_by(Reading.created_at.desc())
        .first()
    )
    reading_count = Reading.query.filter_by(user_id=current_user.id).count()
    return render_template(
        "main/dashboard.html",
        show_checkin=show_checkin,
        last_reading=last_reading,
        reading_count=reading_count,
    )


@bp.route("/life-update", methods=["GET", "POST"])
@login_required
def life_update():
    form = LifeUpdateForm()
    if form.validate_on_submit():
        db.session.add(
            UserProfile(
                user_id=current_user.id,
                question="Life update",
                answer=form.update_text.data.strip(),
            )
        )
        db.session.commit()
        return redirect(url_for("main.reading_setup"))
    return render_template("main/life_update.html", form=form)


@bp.get("/reading/setup")
@login_required
def reading_setup():
    form = ReadingSetupForm()
    return render_template("main/reading_setup.html", form=form)


@bp.post("/reading/start")
@login_required
def reading_start():
    form = ReadingSetupForm()
    if not form.validate_on_submit():
        # CSRF failure or invalid spread_type — send back to setup
        return redirect(url_for("main.reading_setup"))

    spread_type = form.spread_type.data
    intention = (form.intention.data or "").strip() or None

    # Draw cards
    deck = create_standard_tarot_deck()
    if spread_type == "3card":
        drawn = draw_three_card_spread(
            deck,
            allow_reversed=True,
            reversal_probability=DEFAULT_REVERSAL_PROBABILITY,
        )
    elif spread_type == "celticcross":
        drawn = draw_celtic_cross_spread(
            deck,
            allow_reversed=True,
            reversal_probability=DEFAULT_REVERSAL_PROBABILITY,
        )
    else:
        abort(400)

    # Resolve spread positions and compute board layout geometry
    spread_positions = parse_spread_markdown()
    positions = spread_positions.get(spread_type, {})

    if positions:
        xs = [p.coordinates[0] for p in positions.values()]
        ys = [p.coordinates[1] for p in positions.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
    else:
        min_x = min_y = max_x = max_y = 0

    step_x = _CARD_WIDTH + _GAP_X
    step_y = _CARD_HEIGHT + _GAP_Y

    # Build the initial cards_json — interpretations start empty and are filled
    # card-by-card as the user clicks through the reveal flow.
    cards_json: List[Dict] = []
    for idx, card in enumerate(drawn, start=1):
        title, orientation = _split_card_orientation(card)
        pos = positions.get(idx)
        coord = pos.coordinates if pos else (0, 0)
        cards_json.append(
            {
                "index": idx,
                "card": card,
                "title": title,
                "orientation": orientation,
                "position_label": pos.label if pos else f"Card {idx}",
                "represents": pos.represents if pos else "",
                "filename": _card_filename(title),
                "left": (coord[0] - min_x) * step_x,
                "top": (max_y - coord[1]) * step_y,
                "interpretation": "",
            }
        )

    board_width = (max_x - min_x + 1) * step_x
    board_height = (max_y - min_y + 1) * step_y

    reading = Reading(
        user_id=current_user.id,
        spread_type=spread_type,
        intention=intention,
        cards_json=cards_json,
        narrative=None,
    )
    db.session.add(reading)
    db.session.commit()

    # Stash board dimensions in session — needed by summary to rebuild the board view
    session["board"] = {"width": board_width, "height": board_height}

    return redirect(url_for("main.reading_card", reading_id=reading.id, card_index=1))


# ── Card reveal ────────────────────────────────────────────────────────────────


def _get_reading_or_403(reading_id: int) -> Reading:
    reading = db.session.get(Reading, reading_id)
    if reading is None:
        abort(404)
    if reading.user_id != current_user.id:
        abort(403)
    return reading


@bp.get("/reading/<int:reading_id>/card/<int:card_index>")
@login_required
def reading_card(reading_id: int, card_index: int):
    reading = _get_reading_or_403(reading_id)
    cards = reading.cards_json
    total = len(cards)

    if card_index < 1 or card_index > total:
        abort(404)

    card_data = cards[card_index - 1]
    is_last = card_index == total
    next_url = (
        url_for("main.reading_card", reading_id=reading_id, card_index=card_index + 1)
        if not is_last
        else url_for("main.reading_summary", reading_id=reading_id)
    )

    return render_template(
        "main/reading_card.html",
        reading=reading,
        card=card_data,
        card_index=card_index,
        total=total,
        is_last=is_last,
        next_url=next_url,
        # If interpretation is already stored, the template renders it immediately.
        # If empty, the template opens an EventSource to the /stream endpoint.
        interpretation=card_data.get("interpretation", ""),
        stream_url=url_for(
            "main.reading_card_stream",
            reading_id=reading_id,
            card_index=card_index,
        ),
    )


@bp.get("/reading/<int:reading_id>/card/<int:card_index>/stream")
@login_required
def reading_card_stream(reading_id: int, card_index: int):
    reading = _get_reading_or_403(reading_id)
    cards = reading.cards_json
    total = len(cards)

    if card_index < 1 or card_index > total:
        abort(404)

    card_data = cards[card_index - 1]

    # If already interpreted, replay the cached text so the client gets consistent output
    existing = card_data.get("interpretation", "")
    if existing:
        def _cached():
            yield _sse(existing)
            yield _SSE_DONE
        return Response(
            stream_with_context(_cached()),
            mimetype="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # Capture context before entering the generator so closures have stable values
    user_context = _build_user_context(current_user.id)
    prior = _prior_from_cards(cards, card_index - 1)

    def _generate():
        # Create interpreter inside generator — typed errors propagate as SSE codes
        try:
            interpreter = _make_interpreter(reading.spread_type)
        except _ApiKeyError:
            yield "data: [ERROR:nokey]\n\n"
            return
        except Exception:
            yield _SSE_ERROR
            return

        payload = interpreter._card_payload(
            card=card_data["card"],
            position_index=card_index,
            prior=prior,
            user_context=user_context,
        )

        chunks: List[str] = []
        try:
            with interpreter.client.messages.stream(
                model=interpreter.model,
                max_tokens=1024,
                system=_CARD_SYSTEM,
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                for text in stream.text_stream:
                    chunks.append(text)
                    yield _sse(text)
        except _anthropic.AuthenticationError:
            yield "data: [ERROR:authfail]\n\n"
            return
        except _anthropic.RateLimitError:
            yield "data: [ERROR:ratelimit]\n\n"
            return
        except Exception:
            yield _SSE_ERROR
            return

        yield _SSE_DONE

        # Persist the completed interpretation to DB.
        # stream_with_context keeps the request context alive through here.
        full_text = "".join(chunks).strip()
        if full_text:
            updated = [
                {**c, "interpretation": full_text} if i == card_index - 1 else c
                for i, c in enumerate(reading.cards_json)
            ]
            reading.cards_json = updated
            flag_modified(reading, "cards_json")
            db.session.commit()

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ── Summary ────────────────────────────────────────────────────────────────────


@bp.get("/reading/<int:reading_id>/summary")
@login_required
def reading_summary(reading_id: int):
    reading = _get_reading_or_403(reading_id)
    user_context = _build_user_context(current_user.id)

    # Ensure every card has an interpretation before we summarise.
    # A user could arrive here without having clicked through all cards.
    cards = list(reading.cards_json)
    missing_interp = any(not c.get("interpretation") for c in cards)
    if missing_interp:
        try:
            interpreter = _make_interpreter(reading.spread_type)
            for i, card in enumerate(cards):
                if not card.get("interpretation"):
                    prior = _prior_from_cards(cards, i)
                    interp = interpreter.interpret_card(
                        card=card["card"],
                        position_index=card["index"],
                        prior_interpretations=prior,
                        user_context=user_context,
                    )
                    cards[i] = {**card, "interpretation": interp}
            reading.cards_json = cards
            flag_modified(reading, "cards_json")
            db.session.commit()
        except Exception:
            pass  # show summary with whatever interpretations exist

    # Generate narrative if not yet stored
    if not reading.narrative:
        try:
            interpreter = _make_interpreter(reading.spread_type)
            prior = _prior_from_cards(cards, len(cards))
            reading.narrative = interpreter.summarize_spread(
                prior, user_context=user_context
            )
            db.session.commit()
        except Exception:
            pass

    board = session.get("board", {"width": 800, "height": 500})

    return render_template(
        "main/reading_summary.html",
        reading=reading,
        cards=reading.cards_json,
        board=board,
    )


# ── History ────────────────────────────────────────────────────────────────────


@bp.get("/history")
@login_required
def history():
    readings = (
        Reading.query.filter_by(user_id=current_user.id)
        .order_by(Reading.created_at.desc())
        .all()
    )
    return render_template("main/history.html", readings=readings)


# ── Settings ───────────────────────────────────────────────────────────────────


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = SettingsForm()
    if form.validate_on_submit():
        current_user.llm_api_key_encrypted = encrypt_api_key(
            form.new_api_key.data.strip()
        )
        db.session.commit()
        flash("API key updated successfully.", "success")
        return redirect(url_for("main.settings"))
    return render_template("main/settings.html", form=form)


# ── Card image assets ──────────────────────────────────────────────────────────


@bp.get("/cards/<path:filename>")
def serve_card_image(filename: str):
    return send_from_directory(_STANDARD_CARDS_DIR, filename)
