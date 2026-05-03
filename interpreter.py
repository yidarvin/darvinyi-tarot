import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import anthropic


@dataclass
class PositionInfo:
    index: int
    label: str
    represents: str
    coordinates: Tuple[int, int]


def _project_root_path() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _default_path(filename: str) -> str:
    return os.path.join(_project_root_path(), filename)


def parse_spread_markdown(md_path: Optional[str] = None) -> Dict[str, Dict[int, PositionInfo]]:
    """Parse spread.MD into a mapping of spread key -> position index -> info.

    Supported spread keys:
    - "3card" -> "Three-Card Spread"
    - "celticcross" -> "Celtic Cross"
    """
    if md_path is None:
        md_path = _default_path("spread.MD")

    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    spreads: Dict[str, Dict[int, PositionInfo]] = {}

    section_patterns = [
        ("3card", r"###\s+Three-Card Spread([\s\S]*?)(?:\n---|\Z)"),
        ("celticcross", r"###\s+Celtic Cross.*?([\s\S]*?)(?:\n---|\Z)"),
    ]

    card_header_re = re.compile(r"####\s+Card\s+(\d+)\s+—\s+([^\n]+)")
    represents_re = re.compile(r"-\s+\*\*Represents\*\*:\s+([^\n]+)")
    coords_re = re.compile(r"-\s+\*\*Coordinates\*\*:\s*\(([-\d]+),\s*([-\d]+)\)")

    for key, sec_pat in section_patterns:
        match = re.search(sec_pat, text)
        if not match:
            continue
        section = match.group(1)

        positions: Dict[int, PositionInfo] = {}

        # Iterate over each card subsection
        for card_match in card_header_re.finditer(section):
            idx = int(card_match.group(1))
            label = card_match.group(2).strip()

            # Slice from this header to the next header or end of section
            start = card_match.end()
            next_match = card_header_re.search(section, start)
            end = next_match.start() if next_match else len(section)
            body = section[start:end]

            rep_match = represents_re.search(body)
            represents = rep_match.group(1).strip() if rep_match else ""

            coord_match = coords_re.search(body)
            if coord_match:
                x = int(coord_match.group(1))
                y = int(coord_match.group(2))
                coords = (x, y)
            else:
                coords = (0, 0)

            positions[idx] = PositionInfo(index=idx, label=label, represents=represents, coordinates=coords)

        if positions:
            spreads[key] = positions

    return spreads


def parse_tarot_markdown(md_path: Optional[str] = None) -> Dict[str, Dict[str, List[str]]]:
    """Parse tarot.MD into a mapping of card title -> {upright: [...], reversed: [...]} keywords.

    Extracts Major and Minor Arcana uniformly by looking for #### headings
    followed by - Upright and - Reversed lines.
    """
    if md_path is None:
        md_path = _default_path("tarot.MD")

    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    # Match headings like: #### 0 The Fool or #### Ace of Wands
    heading_re = re.compile(r"^####\s+(?:\d+\s+)?(.+)$", re.MULTILINE)
    upright_re = re.compile(r"-\s+Upright:\s+([^\n]+)")
    reversed_re = re.compile(r"-\s+Reversed:\s+([^\n]+)")

    cards: Dict[str, Dict[str, List[str]]] = {}

    headings = list(heading_re.finditer(text))
    for i, h in enumerate(headings):
        title = h.group(1).strip()
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[start:end]

        upr = upright_re.search(body)
        rev = reversed_re.search(body)

        upright_keywords = [s.strip() for s in (upr.group(1).split(",") if upr else []) if s.strip()]
        reversed_keywords = [s.strip() for s in (rev.group(1).split(",") if rev else []) if s.strip()]

        if upright_keywords or reversed_keywords:
            cards[title] = {"upright": upright_keywords, "reversed": reversed_keywords}

    return cards


# ── Interpreter ────────────────────────────────────────────────────────────────

_CARD_SYSTEM = """\
You are a perceptive, grounded tarot reader interpreting a single card in context.
Address all four of the following in flowing prose — do not use headers or bullet points:

1. The card's intrinsic meaning in its current orientation (upright or reversed), \
drawing on its keywords.
2. What this specific position in the spread typically asks of the querent.
3. Given what you know about this person from their profile, what story is this \
card telling them right now?
4. How this card connects to or builds upon the cards already drawn in this reading.

Write in a warm, direct second-person voice. 4–7 sentences total.\
"""

_SUMMARY_SYSTEM = """\
You are a perceptive, grounded tarot reader giving a closing synthesis of a full spread.
Weave all the cards drawn into a single cohesive narrative — the arc, the tensions, \
and the gifts this reading reveals as a whole.
Draw meaningfully on what you know about this person from their profile where it \
illuminates the reading.
Close with one grounding reflection or open question for the querent to sit with — \
something that invites honest self-examination rather than a tidy answer.

Write in a warm, direct second-person voice. 6–10 sentences. No bullet points or headers.\
"""


class TarotInterpreter:
    """Interprets tarot draws incrementally using the Anthropic API.

    Each interpret_card call considers:
    - The card's upright/reversed keyword meanings from tarot.MD
    - The spread position's semantics from spread.MD
    - A string summary of the user's profile context
    - All prior cards and their interpretations in the current spread

    summarize_spread weaves all cards and user context into a closing narrative.
    """

    def __init__(
        self,
        spread_key: str,
        anthropic_api_key: str,
        *,
        spread_md_path: Optional[str] = None,
        tarot_md_path: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 1.0,
    ) -> None:
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        # Default matches current Claude API; override with ANTHROPIC_MODEL env (e.g. on Railway).
        self.model = model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-6"
        )
        self.temperature = temperature
        self.spread_key = spread_key

        self.spread_map = parse_spread_markdown(spread_md_path)
        if spread_key not in self.spread_map:
            raise ValueError(f"Unsupported spread key: {spread_key}")
        self.positions = self.spread_map[spread_key]

        self.card_meanings = parse_tarot_markdown(tarot_md_path)

    @staticmethod
    def _split_card_orientation(card: str) -> Tuple[str, str]:
        if card.endswith("(Reversed)"):
            return card.replace("(Reversed)", "").strip(), "reversed"
        return card, "upright"

    def _lookup_card_keywords(self, base_title: str) -> Dict[str, List[str]]:
        return self.card_meanings.get(base_title, {"upright": [], "reversed": []})

    def _call(self, system: str, user_content: str, *, max_tokens: int) -> str:
        """Make a single Anthropic messages.create call and return the text."""
        kwargs: Dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "thinking": {"type": "disabled"},
        }
        if self.temperature != 1.0:
            kwargs["temperature"] = self.temperature
        try:
            msg = self.client.messages.create(**kwargs)
        except anthropic.BadRequestError:
            kwargs.pop("thinking", None)
            msg = self.client.messages.create(**kwargs)
        for block in msg.content:
            if block.type == "text":
                return block.text.strip()
        raise RuntimeError("Anthropic returned no text content block")

    # ── Card interpretation ────────────────────────────────────────────────────

    def _card_payload(
        self,
        *,
        card: str,
        position_index: int,
        prior: List[Dict],
        user_context: str,
    ) -> str:
        base_title, orientation = self._split_card_orientation(card)
        keywords = self._lookup_card_keywords(base_title)
        pos = self.positions.get(position_index)

        payload = {
            "card": {
                "title": base_title,
                "orientation": orientation,
                "keywords_for_orientation": keywords.get(orientation, []),
                "all_keywords": keywords,
            },
            "position": {
                "index": position_index,
                "label": pos.label if pos else f"Card {position_index}",
                "represents": pos.represents if pos else "",
            },
            "user_context": user_context or "No profile context available.",
            "prior_cards": [
                {
                    "position_label": p.get("position_label"),
                    "card": p.get("card"),
                    "orientation": p.get("orientation"),
                    "interpretation": p.get("interpretation", ""),
                }
                for p in prior
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    def interpret_card(
        self,
        *,
        card: str,
        position_index: int,
        prior_interpretations: Optional[List[Dict]] = None,
        user_context: str = "",
    ) -> str:
        payload = self._card_payload(
            card=card,
            position_index=position_index,
            prior=prior_interpretations or [],
            user_context=user_context,
        )
        return self._call(_CARD_SYSTEM, payload, max_tokens=1024)

    # ── Spread summary ─────────────────────────────────────────────────────────

    def _summary_payload(
        self,
        prior: List[Dict],
        user_context: str,
    ) -> str:
        positions_info = [
            {
                "index": p.index,
                "label": p.label,
                "represents": p.represents,
            }
            for p in sorted(self.positions.values(), key=lambda x: x.index)
        ]

        payload = {
            "spread": {
                "key": self.spread_key,
                "positions": positions_info,
            },
            "cards": [
                {
                    "position_index": item.get("position_index"),
                    "position_label": item.get("position_label"),
                    "card": item.get("card"),
                    "orientation": item.get("orientation"),
                    "interpretation": item.get("interpretation", ""),
                }
                for item in prior
            ],
            "user_context": user_context or "No profile context available.",
        }
        return json.dumps(payload, ensure_ascii=False)

    def summarize_spread(
        self,
        prior_interpretations: List[Dict],
        *,
        user_context: str = "",
    ) -> str:
        payload = self._summary_payload(prior_interpretations, user_context)
        return self._call(_SUMMARY_SYSTEM, payload, max_tokens=2048)
