import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv
import anthropic


def _yaml_quote(value: str) -> str:
    """Escape a string for safe inclusion in a YAML double-quoted scalar."""
    if value is None:
        return ""
    # Replace newlines and escape double quotes
    safe = value.replace("\r", " ").replace("\n", " ")
    safe = safe.replace('"', '\\"')
    return safe


def _sanitize_filename(name: str) -> str:
    safe = re.sub(r"[\/:*?\"<>|]", "_", name)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:120]


def _today_ymd() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _generate_concise_title(
    prior: List[Dict[str, str]],
    *,
    spread_key: str,
    model: str = "claude-3-5-haiku-latest",
) -> Optional[str]:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    # Compress prior to essential facts
    compact = [
        {
            "index": p.get("position_index"),
            "label": p.get("position_label"),
            "card": p.get("card"),
            "orientation": p.get("orientation"),
            "summary": (p.get("interpretation") or "").strip(),
        }
        for p in prior
    ]

    import json

    system_prompt = (
        "You are a title generator for tarot readings."
        " Create a very concise, evocative title (max 7 words) capturing the central theme."
        " Do not include quotes, punctuation at the end, or extra text."
    )
    user_payload = {
        "spread_key": spread_key,
        "cards": compact,
        "instructions": "Return ONLY the title text."
    }

    try:
        completion = client.messages.create(
            model=model,
            max_tokens=48,
            temperature=0.7,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                }
            ],
        )
        title = (completion.content[0].text or "").strip()
        # Final safety trims
        title = title.strip().strip('"\'')
        # Limit word count to 7 words
        words = re.split(r"\s+", title)
        if len(words) > 7:
            title = " ".join(words[:7])
        return title
    except Exception:
        return None


def save_read_markdown(
    *,
    spread_key: str,
    prior: List[Dict[str, str]],
    summary_text: Optional[str] = None,
    save_dir: Optional[str] = None,
) -> Optional[str]:
    """Save a tarot reading as an Obsidian-friendly Markdown file with YAML frontmatter.

    Returns the full path to the saved file, or None if saving failed or PATH_TO_SAVE missing.
    """
    load_dotenv()

    target_dir = save_dir or os.getenv("PATH_TO_SAVE")
    if not target_dir:
        # Nothing to do if the user hasn't configured a save path
        return None

    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception:
        return None

    # Optional Anthropic title; fallback to deterministic title
    title = _generate_concise_title(prior, spread_key=spread_key) or (
        f"{('3-Card' if spread_key == '3card' else 'Celtic Cross')} Reading { _today_ymd() }"
    )

    # Single timestamp for consistency across fields and filename
    now = datetime.now()
    created = now.strftime("%Y-%m-%d %H:%M:%S")
    modified = created

    # Build YAML frontmatter
    frontmatter = (
        "---\n"
        f"title: \"{_yaml_quote(title)}\"\n"
        "aliases: []\n"
        "tags: [tarot]\n"
        f"created: \"{created}\"\n"
        f"modified: \"{modified}\"\n"
        f"tarot_type: \"{spread_key}\"\n"
        "---\n\n"
    )

    # Body: list cards and interpretations
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"Spread: {('Three-Card' if spread_key == '3card' else 'Celtic Cross')}")
    lines.append("")
    lines.append("## Cards")
    for p in prior:
        idx = p.get("position_index")
        label = p.get("position_label") or f"Card {idx}"
        card = p.get("card") or "Unknown"
        orientation = p.get("orientation") or "upright"
        interp = (p.get("interpretation") or "").strip()
        lines.append(f"- {idx}. {card} ({orientation}) — {label}")
        if interp:
            lines.append(f"  - {interp}")
    if summary_text:
        lines.append("")
        lines.append("## Summary")
        lines.append(summary_text)
        lines.append("")

    content = frontmatter + "\n".join(lines) + "\n"

    # Filename: YYYY-MM-DD HH-MM-SS - Title.md (colons replaced by dashes for cross-platform safety)
    filename_dt = now.strftime("%Y-%m-%d %H-%M-%S")
    filename = f"{filename_dt} - {_sanitize_filename(title)}.md"
    filepath = os.path.join(target_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath
    except Exception:
        return None


