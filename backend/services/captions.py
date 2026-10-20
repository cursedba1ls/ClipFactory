"""Local caption and hashtag suggestions (templates + transcript keywords, no paid LLM).

Templates are deliberately neutral: they never claim events that might not be in
the source ("shocking twist" etc.).
"""
from __future__ import annotations

import re
from collections import Counter

STOPWORDS = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до его ее её
если есть еще ещё же за здесь и из или им их к как ко когда кто ли либо мне может мы на надо наш не него нее неё
нет ни них но ну о об однако он она они оно от очень по под при с со так также такой там те тем то того тоже той
только том ты у уже хотя чего чей чем что чтобы чье чья эта эти это этого этой этом этот я вот просто тебя тебе
меня себя себе сейчас потом тогда теперь здесь туда сюда этим этих свой своей свои своих который которая которые
давай давайте знаешь хорошо ладно спасибо привет нужно можно будет буду будем будут было может могу хочу хочешь
the a an and or but if then so to of in on at for with by from as is are was were be been being it its this that
these those i you he she we they me him her us them my your his our their what which who whom how why when where
not no yes do does did done have has had will would can could should just really okay ok oh yeah hey like get got
""".split())

MIDDLE_TEMPLATES = [
    "Продолжение 👀",
    "Смотрим дальше 🍿",
    "Что будет дальше? 🤔",
    "Продолжаем 🔥",
]


def keywords(text: str, n: int = 2) -> list[str]:
    words = re.findall(r"[A-Za-zА-Яа-яЁё]{5,}", text or "")
    counts = Counter(w.lower() for w in words if w.lower() not in STOPWORDS)
    return [w for w, c in counts.most_common(n) if c >= 2]


def make_caption(n: int, total: int, title: str = "") -> str:
    title = (title or "").strip()
    if title:
        # Format used by popular series accounts: "как я Лысым стал часть 2"
        return title if total <= 1 else f"{title} часть {n}"
    if total <= 1:
        return "Смотри до конца 🎬"
    if n == 1:
        body = "Начало истории 🔥"
    elif n == total:
        body = "Финальная часть 😳"
    else:
        body = MIDDLE_TEMPLATES[(n - 2) % len(MIDDLE_TEMPLATES)]
    return f"{body} | Часть {n}/{total}"


def normalize_hashtags(raw: str) -> list[str]:
    out: list[str] = []
    for tok in re.split(r"[\s,;]+", raw or ""):
        tok = tok.strip().lstrip("#")
        tok = re.sub(r"[^\w]", "", tok, flags=re.UNICODE)
        if tok and ("#" + tok).lower() not in {t.lower() for t in out}:
            out.append("#" + tok)
    return out


def make_hashtags(default_tags: str, project_tags: str = "", transcript: str = "", limit: int = 8) -> str:
    tags = normalize_hashtags(project_tags) + normalize_hashtags(default_tags)
    for kw in keywords(transcript):
        tags += normalize_hashtags(kw)
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return " ".join(out[:limit])
