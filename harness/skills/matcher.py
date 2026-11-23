"""Keyword match active skills before their instructions are loaded."""

import re

TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+")
STOP_WORDS = frozenset(
    {"a", "an", "and", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
)


def _keywords(text: str) -> set[str]:
    keywords = set()
    for token in TOKEN_PATTERN.findall(text.casefold()):
        if token[0].isascii():
            if token not in STOP_WORDS:
                keywords.add(token)
        elif len(token) == 1:
            keywords.add(token)
        else:
            keywords.update(token[index : index + 2] for index in range(len(token) - 1))
    return keywords


def rank_skills(query: str, skills: list[dict]) -> list[dict]:
    terms = _keywords(query)
    scores = []
    for skill in skills:
        name_matches = terms & _keywords(skill["name"])
        description_matches = terms & _keywords(skill["description"])
        score = 4 * len(name_matches) + len(description_matches)
        if score:
            scores.append((score, skill))
    scores.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [skill for _, skill in scores]
