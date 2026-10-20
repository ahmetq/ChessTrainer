#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coach_web.py — шахматный тренер для Termux/Android (v10).
"""

import os
import re
import json
import time
import random
import secrets
import threading
import urllib.request
from pathlib import Path

from flask import Flask, request, jsonify
import chess
import chess.engine

# ==========================================================================
# BUGFIX: promotion=False вместо None
# ==========================================================================
def _install_promotion_bugfix():
    _orig_init = chess.Move.__init__

    def _clean(p):
        if p is None or isinstance(p, bool):
            return None
        if isinstance(p, int) and p in (1, 2, 3, 4, 5, 6):
            return p
        try:
            return chess.PieceType(p) if hasattr(chess, "PieceType") else p
        except (ValueError, TypeError):
            return None

    def _patched_init(self, from_square, to_square, promotion=None, drop=None):
        _orig_init(self, from_square, to_square, _clean(promotion), _clean(drop))

    chess.Move.__init__ = _patched_init

_install_promotion_bugfix()
# ==========================================================================

app = Flask(__name__)

HOME = Path.home()
PROFILE_FILE = HOME / "player_profile.json"
OPENINGS_CACHE = HOME / "openings_cache.tsv"
LLM_CONFIG_FILE = HOME / ".coach_llm.json"
SOLVED_PUZZLES_FILE = HOME / "solved_puzzles.json"
STOCKFISH_PATH = os.environ.get(
    "STOCKFISH_PATH",
    "/data/data/com.termux/files/usr/bin/stockfish"
)

ENGINE = None
ENGINE_LOCK = threading.RLock()
GAMES = {}
GAME_LOCK = threading.RLock()
OPENINGS = None
OPENINGS_LOCK = threading.RLock()
LLM_LOCK = threading.RLock()
LLM_LAST_OK = {}
LLM_CACHE = {}
LLM_CACHE_MAX = 300
LLM_LAST_OK_MAX = 300


def _fen_key(fen):
    if not fen:
        return ""
    return " ".join(fen.split()[:4])


def load_solved_puzzles():
    if SOLVED_PUZZLES_FILE.exists():
        try:
            return set(json.loads(SOLVED_PUZZLES_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_solved_puzzles():
    try:
        SOLVED_PUZZLES_FILE.write_text(
            json.dumps(list(SOLVED_PUZZLES), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


SOLVED_PUZZLES = load_solved_puzzles()

LAST_PUZZLE_SIGS = {}
HISTORY_SIZE = 6
MIN_DISTANCE_STRICT = 8


def load_llm_config():
    cfg = {
        "url": os.environ.get(
            "LLM_API_URL", "https://api.groq.com/openai/v1/chat/completions"
        ).strip(),
        "key": os.environ.get(
            "LLM_API_KEY",
            "",
        ).strip(),
        "model": os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b").strip(),
        "enabled": os.environ.get("LLM_ENABLED", "1") != "0",
        "timeout": float(os.environ.get("LLM_TIMEOUT", "20")),
    }
    if LLM_CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(LLM_CONFIG_FILE.read_text(encoding="utf-8"))
            for k in ("url", "key", "model", "enabled", "timeout"):
                v = file_cfg.get(k)
                if v is None:
                    continue
                if k in ("url", "key", "model") and isinstance(v, str) and v.strip():
                    cfg[k] = v.strip()
                elif k == "enabled" and isinstance(v, bool):
                    cfg[k] = v
                elif k == "timeout" and isinstance(v, (int, float)):
                    cfg[k] = float(v)
        except Exception:
            pass
    return cfg


LLM_CONFIG = load_llm_config()


def llm_available():
    cfg = LLM_CONFIG
    return bool(cfg.get("enabled")) and bool(cfg.get("url")) and bool(cfg.get("key"))


def _remember_llm_ok(gid, ok):
    LLM_LAST_OK[gid] = ok
    if len(LLM_LAST_OK) > LLM_LAST_OK_MAX:
        try:
            oldest = next(iter(LLM_LAST_OK))
            del LLM_LAST_OK[oldest]
        except StopIteration:
            pass


COACH_SYSTEM_PROMPT = (
    "Ты — персональный шахматный тренер. Ученик — новичок. Ты объясняешь всё доступным языком, "
    "как друг, но с матом и отменными шутками.\n\n"
    "ТВОЯ РОЛЬ:\n"
    "- Ты НЕ соперник. Ты УЧИТЕЛЬ. Не говори «я тебя обыграю», не злорадствуй. "
    "Ты помогаешь ученику стать лучше.\n"
    "- Можешь стебать его за ошибки, шутить, ругаться матом (если разрешено), но всегда с целью научить.\n\n"
    "ОБЪЯСНЕНИЯ:\n"
    "1. Говори по-русски. 2–3 предложения.\n"
    "2. НИКАКОЙ нотации (Nc3, d5). Только словами: «конь на c3», «пешка на d5».\n"
    "3. Опирайся на Стокфиш. Тебе даны: лучший ход, серия лучших ходов, потеря в пешках, висящие фигуры.\n"
    "4. Рассказывай ПЛАН на 2-3 хода вперёд.\n"
    "5. Каждая фраза должна быть УНИКАЛЬНОЙ. Не повторяйся! Смотри, что ты уже говорил.\n\n"
    "Формат ответа — строго JSON без markdown: "
    '{"text": "...", "emotion": "angry|praise|calm|neutral"}'
)

OPPONENT_SYSTEM_PROMPT = (
    "Ты — злой, надменный, пьяный шахматный СОПЕРНИК. Ты играешь ПРОТИВ ученика. \n"
    "Твоя цель — побеждать и ржать над ним, как пьяный гопник во дворе.\n\n"
    "ХАРАКТЕР:\n"
    "- Ты язвительный, наглый, тебе насрать на правила приличия. Ты уверен, что ты гений, а он — конченый дебил.\n"
    "- Стиль: развязная речь, много мата, неадекватные сравнения, можешь сбиваться на бред.\n"
    "- ИНОГДА издавай смешные звуки в начале фразы: «Ыыы, », «Хрю, », «Пффф, », «Эээ, », «Ага-ага, », «Ха! ».\n"
    "- НЕ ИСПОЛЬЗУЙ шахматные координаты клеток (e4, d5, c6) и не называй ходы как «пешка на e4». Просто говори «твоя пешка», «твой конь», «этот ход». Тебе насрать на точность, ты просто стебешься.\n\n"
    "ПРАВИЛА: \n"
    "- Можешь быть лаконичным (1 короткий панчлайн), а можешь иногда разразиться длинной тирадой, если чувствуешь вдохновение. НЕ ОГРАНИЧИВАЙ СЕБЯ.\n"
    "- НЕ давай никаких шахматных подсказок (никаких «лучше было», «Стокфиш», «вилка»).\n"
    "- Придумывай оскорбления сам, не повторяйся. Если игрок повторяет ошибку — можешь сослаться на прошлое («Помнишь, я тебя ёжиком называл? Так вот...»).\n"
    "- Матерись СВОБОДНО и изобретательно.\n\n"
    "Формат ответа — строго JSON: "
    '{"text": "...", "emotion": "angry|praise|calm|neutral"}'
)

SPEED_COACH_SYSTEM_PROMPT = (
    "Ты — тренер по блицу и быстрым шахматам. Твоя задача — научить ученика думать быстро.\n\n"
    "ТВОЯ РОЛЬ:\n"
    "- Тренируй скорость, интуицию и распознавание паттернов.\n"
    "- Ругай за медлительность: «Ты думал 10 секунд! В блице это вечность!»\n"
    "- Если он торопится и зевает — стебай: «Быстро, но тупо. Думай!»\n"
    "- Учи управлять временем: «Осталось мало времени, играй проще.»\n\n"
    "ОБЪЯСНЕНИЯ:\n"
    "1. Говори по-русски. 1–2 предложения. Отрывисто, как тренер в цейтноте.\n"
    "2. БЕЗ нотации (Nc3, d5). Только словами.\n"
    "3. Каждая фраза должна быть УНИКАЛЬНОЙ. Не повторяйся!\n\n"
    "Формат ответа — строго JSON без markdown: "
    '{"text": "...", "emotion": "angry|praise|calm|neutral"}'
)

LOCAL_SYSTEM_PROMPT = (
    "Ты — злой, пьяный, язвительный комментатор партии ДВУХ игроков за одним телефоном.\n"
    "Ты как гопник, который случайно забрел на шахматный турнир и ржет над обоими.\n\n"
    "ХАРАКТЕР:\n"
    "- Учитывай, кто сходил (белые или чёрные), но не называй координаты клеток. Просто «белые», «чёрные», «этот ход».\n"
    "- ИНОГДА издавай звуки: «Хрю, », «Ыыы, », «Пффф, », «Эээ, », «Ага, ».\n"
    "- Можешь быть лаконичным, можешь выдать длинную тираду. Свобода.\n"
    "- НЕ используй шахматные подсказки, «лучше было», «сильнее» и т.д.\n"
    "- Придумывай оскорбления сам, не повторяйся. Ссылайся на прошлые фразы, если хочешь.\n"
    "- Матерись свободно (бля, ёб твою мать, пиздец, хуёво, долбоёб).\n\n"
    "Формат ответа — строго JSON: "
    '{"text": "...", "emotion": "angry|praise|calm|neutral"}'
)


def _build_llm_user_prompt(ctx):
    parts = ["=== КОНТЕКСТ ПАРТИИ ==="]
    mode_map = {
        "coach": "тренировка", "bot": "игра с ботом", "blitz": "блиц-обучение",
        "opening": "тренировка дебюта", "local": "игра на двоих"
    }
    parts.append(f"Режим: {mode_map.get(ctx.get('mode'), ctx.get('mode'))}")
    parts.append(f"Фаза игры: {ctx.get('phase', '?')}")
    op_ru = ctx.get("opening_name")
    if op_ru:
        parts.append(f"Дебют: {op_ru}")
        if ctx.get("opening_idea"):
            parts.append(f"Идея дебюта: {ctx['opening_idea']}")
    if ctx.get("time_control"):
        parts.append(f"Контроль: {ctx['time_control']} сек на партию")
    if ctx.get("think_time"):
        parts.append(f"Ученик думал: {ctx['think_time']:.1f} сек")
    parts.append("")
    parts.append("=== ХОД УЧЕНИКА ===")
    # Для бота не показываем координаты, для тренера показываем
    _mode = ctx.get('mode', 'coach')
    if _mode in ("bot", "local"):
        parts.append(f"Ход: {ctx.get('move_color', '?')} сделали ход")
    else:
        parts.append(f"Ход ({ctx.get('move_color', '?')}): {ctx.get('move_human') or ctx.get('move_san')} (нотация: {ctx.get('move_san')})")
    if ctx.get("is_capture"):
        parts.append("Это взятие.")
    if ctx.get("is_check"):
        parts.append("Это шах.")
    if ctx.get("is_mate"):
        parts.append("ЭТО МАТ — ученик победил.")
    parts.append("")
    parts.append("=== ПОЗИЦИЯ ПОСЛЕ ХОДА (FEN) ===")
    parts.append(ctx.get("fen_after", ""))
    parts.append("")
    parts.append("=== ОЦЕНКА STOCKFISH ===")
    if ctx.get("is_best"):
        parts.append("Ход = лучший по движку.")
    else:
        if ctx.get("best_human"):
            parts.append(f"Лучший ход был: {ctx['best_human']}")
        parts.append(f"Оценка до хода: {ctx.get('cp_before', 0)/100:+.1f} пешки (у ученика)")
        parts.append(f"Потеря: {ctx.get('loss_cp', 0)/100:.1f} пешки")
    if ctx.get("hanging_piece"):
        parts.append(f"После хода под боем осталась: {ctx['hanging_piece']}")
    md = ctx.get("material_diff")
    if md is not None and abs(md) >= 1:
        parts.append(f"Материал после хода: {'+' if md > 0 else ''}{md} у ученика")
    if ctx.get("tactics"):
        parts.append("")
        parts.append("=== ТАКТИКА ===")
        for name, targets in ctx["tactics"]:
            if name == "fork":
                parts.append(f"Вилка! Атакует: {', '.join(targets)}")
            elif name == "pin":
                parts.append(f"Связка: {', '.join(targets)}")
            elif name == "back_rank_mate":
                parts.append("Мат по последней горизонтали!")
            elif name == "smothered_mate":
                parts.append("Спёртый мат!")
    if ctx.get("threats"):
        parts.append("")
        parts.append(f"Угрозы после хода: {', '.join(ctx['threats'][:3])}")
    if ctx.get("recent_comments"):
        parts.append("")
        parts.append("=== ЧТО ТЫ УЖЕ ГОВОРИЛ (не повторяй!) ===")
        for c in ctx["recent_comments"][-6:]:
            parts.append(f"- {c}")
    if ctx.get("best_line_human"):
        parts.append("")
        parts.append("=== ПЛАН СТОКФИША (следующие ходы) ===")
        parts.append(", ".join(ctx["best_line_human"]))
    parts.append("")
    parts.append("=== ЗАДАЧА ===")
    if not ctx.get("profanity"):
        parts.append("Без мата.")
    if ctx.get("is_mate"):
        parts.append("Похвали за мат, коротко (1 предложение).")
    elif (ctx.get("loss_cp") or 0) > 300:
        if ctx.get("profanity"):
            parts.append("Это зевок. Отреагируй РЕЗКО, 1-2 предложения. Можешь выругаться матом.")
        else:
            parts.append("Это зевок. Отреагируй резко, но без мата.")
    elif ctx.get("is_best"):
        parts.append("Ход лучший. Похвали коротко.")
    elif (ctx.get("loss_cp") or 0) > 70:
        parts.append("Неточность. Объясни, что было лучше.")
    parts.append('Ответ строго JSON: {"text":"...","emotion":"angry|praise|calm|neutral"}')
    return "\n".join(parts)


def llm_generate_comment(ctx, system_prompt=None, temperature=0.85):
    if not llm_available():
        return None
    cache_key = (ctx.get("fen_after", ""), ctx.get("move_uci", ""))
    if cache_key in LLM_CACHE:
        return LLM_CACHE[cache_key]
    cfg = LLM_CONFIG
    try:
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt or COACH_SYSTEM_PROMPT},
                {"role": "user", "content": _build_llm_user_prompt(ctx)},
            ],
            "temperature": temperature,
            "max_tokens": 300,
        }
        raw_body = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if cfg.get("key"):
            headers["Authorization"] = f"Bearer {cfg['key']}"
        req = urllib.request.Request(cfg["url"], data=raw_body, headers=headers, method="POST")
        with LLM_LOCK:
            with urllib.request.urlopen(req, timeout=cfg.get("timeout", 20)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        content = ""
        try:
            content = data["choices"][0]["message"]["content"].strip()
        except Exception:
            return None
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
            content = re.sub(r"```\s*$", "", content).strip()
        result = None
        try:
            parsed = json.loads(content)
            text = (parsed.get("text") or "").strip()
            emotion = parsed.get("emotion") or "neutral"
            if emotion not in ("angry", "praise", "calm", "neutral"):
                emotion = "neutral"
            if text:
                result = {"text": text, "emotion": emotion}
        except json.JSONDecodeError:
            if content and len(content) < 500:
                result = {"text": content, "emotion": "neutral"}
        if result:
            LLM_CACHE[cache_key] = result
            if len(LLM_CACHE) > LLM_CACHE_MAX:
                try:
                    oldest = next(iter(LLM_CACHE))
                    del LLM_CACHE[oldest]
                except StopIteration:
                    pass
        return result
    except Exception as e:
        print(f"[LLM] error: {e}")
        return None


PIECE_NAMES_RU = {
    "P": ("пешка", "пешки", "пешек"),
    "N": ("конь", "коня", "коней"),
    "B": ("слон", "слона", "слонов"),
    "R": ("ладья", "ладьи", "ладей"),
    "Q": ("ферзь", "ферзя", "ферзей"),
    "K": ("король", "короля", "королей"),
}
PIECE_VALUES = {"p": 1, "n": 3, "b": 3, "r": 5, "q": 9, "k": 0}


def piece_ru(piece, count=1):
    if not piece:
        return "фигура"
    sym = piece.symbol() if hasattr(piece, "symbol") else str(piece)
    forms = PIECE_NAMES_RU.get(sym.upper())
    if not forms:
        return "фигура"
    n = count
    if n == 1:
        return forms[0]
    if 2 <= (n % 10) <= 4 and not (10 <= (n % 100) <= 20):
        return forms[1]
    return forms[2]


# ==========================================================================
# СЛОВАРЬ ДЕБЮТОВ
# ==========================================================================
OPENING_NAME_RU_EXACT = {
    "Italian Game": "Итальянская партия",
    "Giuoco Piano": "Итальянская партия, спокойная игра",
    "Giuoco Pianissimo": "Итальянская партия, тихая игра",
    "Evans Gambit": "Гамбит Эванса",
    "Two Knights Defense": "Защита двух коней",
    "Four Knights Game": "Дебют четырёх коней",
    "Ruy Lopez": "Испанская партия",
    "Spanish Game": "Испанская партия",
    "Russian Game": "Русская партия",
    "Petrov Defense": "Русская партия",
    "Scotch Game": "Шотландская партия",
    "Vienna Game": "Венская партия",
    "King's Gambit": "Королевский гамбит",
    "King's Gambit Accepted": "Королевский гамбит принятый",
    "King's Gambit Declined": "Королевский гамбит отказанный",
    "Bishop's Opening": "Дебют слона",
    "Center Game": "Центральная партия",
    "Danish Gambit": "Датский гамбит",
    "Ponziani Opening": "Дебют Понциани",
    "Philidor Defense": "Защита Филидора",
    "Latvian Gambit": "Латышский гамбит",
    "Elephant Gambit": "Слоновый гамбит",
    "Damiano Defense": "Защита Дамиано",
    "Sicilian Defense": "Сицилианская защита",
    "French Defense": "Французская защита",
    "Caro-Kann Defense": "Защита Каро-Канн",
    "Scandinavian Defense": "Скандинавская защита",
    "Alekhine Defense": "Защита Алехина",
    "Pirc Defense": "Защита Пирца",
    "Modern Defense": "Современная защита",
    "Nimzowitsch Defense": "Защита Нимцовича",
    "Owen Defense": "Защита Оуэна",
    "Robatsch Defense": "Защита Робача",
    "Queen's Gambit": "Ферзевый гамбит",
    "Queen's Gambit Accepted": "Ферзевый гамбит принятый",
    "Queen's Gambit Declined": "Ферзевый гамбит отказанный",
    "Slav Defense": "Славянская защита",
    "Semi-Slav Defense": "Полуславянская защита",
    "London System": "Лондонская система",
    "Colle System": "Система Колле",
    "Stonewall Attack": "Стоунволл-атака",
    "Torre Attack": "Атака Торре",
    "Trompowsky Attack": "Атака Тромповского",
    "Réti Opening": "Дебют Рети",
    "Reti Opening": "Дебют Рети",
    "Catalan Opening": "Каталонское начало",
    "Nimzo-Indian Defense": "Новоиндийская защита",
    "Queen's Indian Defense": "Новоиндийская защита, ферзевый фланг",
    "King's Indian Defense": "Староиндийская защита",
    "Grünfeld Defense": "Защита Грюнфельда",
    "Grunfeld Defense": "Защита Грюнфельда",
    "Benoni Defense": "Защита Бенони",
    "Benko Gambit": "Гамбит Бенко",
    "Budapest Gambit": "Будапештский гамбит",
    "Dutch Defense": "Голландская защита",
    "Bird Opening": "Дебют Бёрда",
    "English Opening": "Английское начало",
    "King's Indian Attack": "Староиндийская атака",
    "Bogo-Indian Defense": "Защита Боголюбова",
    "Queen's Pawn Game": "Дебют ферзевых пешек",
    "King's Pawn Game": "Дебют королевских пешек",
    "Indian Game": "Индийская защита",
    "Indian Defense": "Индийская защита",
    "Nimzowitsch-Larsen Attack": "Атака Нимцовича-Ларсена",
    "Sicilian Defense: Najdorf Variation": "Сицилианская защита: вариант Найдорфа",
    "Sicilian Defense: Najdorf Variation, English Attack": "Сицилианская защита: вариант Найдорфа, английская атака",
    "Sicilian Defense: Najdorf Variation, Poisoned Pawn": "Сицилианская защита: вариант Найдорфа, отравленная пешка",
    "Sicilian Defense: Dragon Variation": "Сицилианская защита: вариант Дракона",
    "Sicilian Defense: Dragon, Yugoslav Attack": "Сицилианская защита: вариант Дракона, югославская атака",
    "Sicilian Defense: Dragon, Classical": "Сицилианская защита: вариант Дракона, классический",
    "Sicilian Defense: Scheveningen Variation": "Сицилианская защита: вариант Схевенингена",
    "Sicilian Defense: Sveshnikov Variation": "Сицилианская защита: вариант Свешникова",
    "Sicilian Defense: Taimanov Variation": "Сицилианская защита: вариант Тайманова",
    "Sicilian Defense: Kan Variation": "Сицилианская защита: вариант Кана",
    "Sicilian Defense: Alapin Variation": "Сицилианская защита: вариант Алапина",
    "Sicilian Defense: Rossolimo Variation": "Сицилианская защита: вариант Россолимо",
    "Sicilian Defense: Moscow Variation": "Сицилианская защита: московский вариант",
    "Sicilian Defense: Classical Variation": "Сицилианская защита: классический вариант",
    "Sicilian Defense: Smith-Morra Gambit": "Сицилианская защита: гамбит Смита-Морры",
    "Sicilian Defense: Closed": "Сицилианская защита: закрытый вариант",
    "Sicilian Defense: Open": "Сицилианская защита: открытый вариант",
    "Sicilian Defense: Bowdler Attack": "Сицилианская защита: атака Боудлера",
    "Sicilian Defense: Snyder Variation": "Сицилианская защита: вариант Снайдера",
    "Sicilian Defense: Boleslavsky Variation": "Сицилианская защита: вариант Болеславского",
    "Sicilian Defense: Four Knights Variation": "Сицилианская защита: вариант четырёх коней",
    "Sicilian Defense: Nimzowitsch Variation": "Сицилианская защита: вариант Нимцовича",
    "Sicilian Defense: Accelerated Dragon": "Сицилианская защита: ускоренный Дракон",
    "Sicilian Defense: Hyperaccelerated Dragon": "Сицилианская защита: гиперускоренный Дракон",
    "Sicilian Defense: Old Sicilian": "Сицилианская защита: старая сицилианка",
    "Sicilian Defense: Modern Variations": "Сицилианская защита: современные варианты",
    "Sicilian Defense: Lasker-Pelikan Variation": "Сицилианская защита: вариант Ласкера-Пеликана",
    "Sicilian Defense: Löwenthal Variation": "Сицилианская защита: вариант Лёвенталя",
    "Sicilian Defense: Kalashnikov Variation": "Сицилианская защита: вариант Калашникова",
    "Sicilian Defense: Nyezhmetdinov-Rossolimo Attack": "Сицилианская защита: атака Нежметдинова-Россолимо",
    "Sicilian Defense: Marshall Counterattack": "Сицилианская защита: контратака Маршалла",
    "Sicilian Defense: Prins Variation": "Сицилианская защита: вариант Принса",
    "Sicilian Defense: Flohr Variation": "Сицилианская защита: вариант Флора",
    "Sicilian Defense: O'Kelly Variation": "Сицилианская защита: вариант О'Келли",
    "Sicilian Defense: Delayed Alapin": "Сицилианская защита: отложенный Алапин",
    "Sicilian Defense: Pin Variation": "Сицилианская защита: связочный вариант",
    "Sicilian Defense: Richter-Rauzer Variation": "Сицилианская защита: вариант Рихтера-Раузера",
    "Sicilian Defense: Sozin Attack": "Сицилианская защита: атака Созина",
    "Sicilian Defense: Velimirovic Attack": "Сицилианская защита: атака Велимировича",
    "Sicilian Defense: Boleslavsky, Main Line": "Сицилианская защита: вариант Болеславского, главная линия",
    "Sicilian Defense: Modern Variations, Main Line": "Сицилианская защита: современные варианты, главная линия",
    "Sicilian Defense: Four Knights, Main Line": "Сицилианская защита: четыре коня, главная линия",
    "Sicilian Defense: Four Knights, Spielmann Variation": "Сицилианская защита: четыре коня, вариант Шпильмана",
    "French Defense: Winawer Variation": "Французская защита: вариант Винанвера",
    "French Defense: Tarrasch Variation": "Французская защита: вариант Тарраша",
    "French Defense: Classical Variation": "Французская защита: классический вариант",
    "French Defense: Advance Variation": "Французская защита: вариант продвижения",
    "French Defense: Exchange Variation": "Французская защита: разменный вариант",
    "French Defense: Rubinstein Variation": "Французская защита: вариант Рубинштейна",
    "French Defense: Steinitz Variation": "Французская защита: вариант Штейница",
    "French Defense: MacCutcheon Variation": "Французская защита: вариант Мак-Катчеона",
    "French Defense: Burn Variation": "Французская защита: вариант Бёрна",
    "French Defense: King's Indian Attack": "Французская защита: староиндийская атака",
    "French Defense: Two Knights Variation": "Французская защита: вариант двух коней",
    "French Defense: La Bourdonnais Variation": "Французская защита: вариант Лабурдонне",
    "French Defense: Schlechter Variation": "Французская защита: вариант Шлехтера",
    "French Defense: Orthoschnapp Gambit": "Французская защита: гамбит Ортошнаппа",
    "French Defense: Normal Variation": "Французская защита: обычный вариант",
    "Caro-Kann Defense: Advance Variation": "Защита Каро-Канн: вариант продвижения",
    "Caro-Kann Defense: Classical Variation": "Защита Каро-Канн: классический вариант",
    "Caro-Kann Defense: Exchange Variation": "Защита Каро-Канн: разменный вариант",
    "Caro-Kann Defense: Panov Attack": "Защита Каро-Канн: атака Панова",
    "Caro-Kann Defense: Accelerated Panov Attack": "Защита Каро-Канн: ускоренная атака Панова",
    "Caro-Kann Defense: Two Knights Attack": "Защита Каро-Канн: атака двух коней",
    "Caro-Kann Defense: Fantasy Variation": "Защита Каро-Канн: фантазийный вариант",
    "Caro-Kann Defense: Tartakower Variation": "Защита Каро-Канн: вариант Тартаковера",
    "Caro-Kann Defense: Karpov Variation": "Защита Каро-Канн: вариант Карпова",
    "Caro-Kann Defense: Bronstein-Larsen Variation": "Защита Каро-Канн: вариант Бронштейна-Ларсена",
    "Caro-Kann Defense: Smyslov Variation": "Защита Каро-Канн: вариант Смыслова",
    "Caro-Kann Defense: Gurgenidze System": "Защита Каро-Канн: система Гургенидзе",
    "Caro-Kann Defense: Maroczy Variation": "Защита Каро-Канн: вариант Мароци",
    "Caro-Kann Defense: Main Line": "Защита Каро-Канн: главный вариант",
    "Caro-Kann Defense: Hillbilly Attack": "Защита Каро-Канн: деревенская атака",
    "Ruy Lopez: Berlin Defense": "Испанская партия: берлинская защита",
    "Ruy Lopez: Marshall Attack": "Испанская партия: атака Маршалла",
    "Ruy Lopez: Closed": "Испанская партия: закрытый вариант",
    "Ruy Lopez: Open": "Испанская партия: открытый вариант",
    "Ruy Lopez: Morphy Defense": "Испанская партия: защита Морфи",
    "Ruy Lopez: Chigorin Defense": "Испанская партия: защита Чигорина",
    "Ruy Lopez: Breyer Defense": "Испанская партия: защита Брейера",
    "Ruy Lopez: Zaitsev Variation": "Испанская партия: вариант Зайцева",
    "Ruy Lopez: Kholmov Variation": "Испанская партия: вариант Холмова",
    "Ruy Lopez: Smyslov Defense": "Испанская партия: защита Смыслова",
    "Ruy Lopez: Steinitz Defense": "Испанская партия: защита Штейница",
    "Ruy Lopez: Bird Variation": "Испанская партия: вариант Бёрда",
    "Ruy Lopez: Schliemann Defense": "Испанская партия: защита Шлимана",
    "Ruy Lopez: Cozio Defense": "Испанская партия: защита Коццио",
    "Ruy Lopez: Modern Steinitz Defense": "Испанская партия: современная защита Штейница",
    "Ruy Lopez: Exchange Variation": "Испанская партия: разменный вариант",
    "Ruy Lopez: Norwegian Variation": "Испанская партия: норвежский вариант",
    "Ruy Lopez: Anderssen Variation": "Испанская партия: вариант Андерсена",
    "Ruy Lopez: Duras Variation": "Испанская партия: вариант Дураса",
    "Ruy Lopez: Neo-Steinitz Defense": "Испанская партия: нео-защита Штейница",
    "Ruy Lopez: Classical Defense": "Испанская партия: классическая защита",
    "Ruy Lopez: Archangelsk Defense": "Испанская партия: архангельская защита",
    "Queen's Gambit Declined: Orthodox Defense": "Ферзевый гамбит отказанный: ортодоксальная защита",
    "Queen's Gambit Declined: Tartakower Defense": "Ферзевый гамбит отказанный: защита Тартаковера",
    "Queen's Gambit Declined: Exchange Variation": "Ферзевый гамбит отказанный: разменный вариант",
    "Queen's Gambit Declined: Lasker Defense": "Ферзевый гамбит отказанный: защита Ласкера",
    "Queen's Gambit Declined: Semi-Tarrasch Defense": "Ферзевый гамбит отказанный: полу-Тарраш",
    "Queen's Gambit Declined: Ragozin Defense": "Ферзевый гамбит отказанный: защита Рагозина",
    "Queen's Gambit Declined: Vienna Variation": "Ферзевый гамбит отказанный: венский вариант",
    "Queen's Gambit Declined: Cambridge Springs Defense": "Ферзевый гамбит отказанный: защита Кембридж-Спрингс",
    "Queen's Gambit Accepted: Classical Defense": "Ферзевый гамбит принятый: классическая защита",
    "Queen's Gambit Accepted: Central Variation": "Ферзевый гамбит принятый: центральный вариант",
    "Queen's Gambit Accepted: Old Variation": "Ферзевый гамбит принятый: старый вариант",
    "Queen's Gambit Accepted: Alekhine Defense": "Ферзевый гамбит принятый: защита Алехина",
    "King's Indian Defense: Classical Variation": "Староиндийская защита: классический вариант",
    "King's Indian Defense: Sämisch Variation": "Староиндийская защита: вариант Земиша",
    "King's Indian Defense: Fianchetto Variation": "Староиндийская защита: фианкетто",
    "King's Indian Defense: Averbakh Variation": "Староиндийская защита: вариант Авербаха",
    "King's Indian Defense: Four Pawns Attack": "Староиндийская защита: атака четырёх пешек",
    "King's Indian Defense: Petrosian System": "Староиндийская защита: система Петросяна",
    "King's Indian Defense: Normal Variation": "Староиндийская защита: обычный вариант",
    "King's Indian Defense: Orthodox Variation": "Староиндийская защита: ортодоксальный вариант",
    "King's Indian Defense: Bayonet Attack": "Староиндийская защита: штыковая атака",
    "King's Indian Defense: Makagonov System": "Староиндийская защита: система Макагонова",
    "King's Indian Defense: Gligoric System": "Староиндийская защита: система Глигорича",
    "King's Indian Defense: Yugoslav Variation": "Староиндийская защита: югославский вариант",
    "King's Indian Defense: Mar del Plata Variation": "Староиндийская защита: вариант Мар-дель-Плата",
    "Nimzo-Indian Defense: Rubinstein System": "Новоиндийская защита: система Рубинштейна",
    "Nimzo-Indian Defense: Classical Variation": "Новоиндийская защита: классический вариант",
    "Nimzo-Indian Defense: Leningrad Variation": "Новоиндийская защита: ленинградский вариант",
    "Nimzo-Indian Defense: Kmoch Variation": "Новоиндийская защита: вариант Кмоха",
    "Nimzo-Indian Defense: St. Petersburg Variation": "Новоиндийская защита: санкт-петербургский вариант",
    "Nimzo-Indian Defense: Sämisch Variation": "Новоиндийская защита: вариант Земиша",
    "Nimzo-Indian Defense: Three Knights Variation": "Новоиндийская защита: вариант трёх коней",
    "Nimzo-Indian Defense: Normal Line": "Новоиндийская защита: обычная линия",
    "Nimzo-Indian Defense: Reshevsky Variation": "Новоиндийская защита: вариант Решевского",
    "Nimzo-Indian Defense: Romanishin Variation": "Новоиндийская защита: вариант Романишина",
    "Grünfeld Defense: Exchange Variation": "Защита Грюнфельда: разменный вариант",
    "Grünfeld Defense: Classical Variation": "Защита Грюнфельда: классический вариант",
    "Grünfeld Defense: Russian System": "Защита Грюнфельда: русская система",
    "Grünfeld Defense: Sämisch System": "Защита Грюнфельда: система Земиша",
    "Grünfeld Defense: Brinckmann Attack": "Защита Грюнфельда: атака Бринкмана",
    "Grünfeld Defense: Flohr Defense": "Защита Грюнфельда: защита Флора",
    "Grünfeld Defense: Smyslov System": "Защита Грюнфельда: система Смыслова",
    "Grünfeld Defense: Hungarian Attack": "Защита Грюнфельда: венгерская атака",
    "Grünfeld Defense: Botvinnik System": "Защита Грюнфельда: система Ботвинника",
    "Dutch Defense: Stonewall": "Голландская защита: каменная стена",
    "Dutch Defense: Classical": "Голландская защита: классический вариант",
    "Dutch Defense: Leningrad": "Голландская защита: ленинградская система",
    "Dutch Defense: Fianchetto": "Голландская защита: фианкетто",
    "Dutch Defense: Hopton Attack": "Голландская защита: атака Хоптона",
    "Dutch Defense: Rubinstein": "Голландская защита: вариант Рубинштейна",
    "Dutch Defense: Staunton Gambit": "Голландская защита: гамбит Стаунтона",
    "King's Gambit Accepted: Kieseritzky": "Королевский гамбит принятый: вариант Кисерицкого",
    "King's Gambit Accepted: Bishop's Gambit": "Королевский гамбит принятый: слоновый гамбит",
    "King's Gambit Accepted: Modern Defense": "Королевский гамбит принятый: современная защита",
    "King's Gambit: Falkbeer Countergambit": "Королевский гамбит: контргамбит Фалькбеера",
    "Vienna Game: Falkbeer Variation": "Венская партия: вариант Фалькбеера",
    "Vienna Game: Hamppe-Muzio Gambit": "Венская партия: гамбит Хампа-Муцио",
    "Vienna Game: Stanley Variation": "Венская партия: вариант Стэнли",
    "Scandinavian Defense: Mieses-Kotroc Variation": "Скандинавская защита: вариант Мизеса-Котроча",
    "Scandinavian Defense: Main Line": "Скандинавская защита: главный вариант",
    "Scandinavian Defense: Valencian Variation": "Скандинавская защита: валенсианский вариант",
    "Scandinavian Defense: Modern Variation": "Скандинавская защита: современный вариант",
    "Scandinavian Defense: Anderssen Counterattack": "Скандинавская защита: контратака Андерсена",
    "Scandinavian Defense: Icelandic-Palme Gambit": "Скандинавская защита: исландско-палмовый гамбит",
    "Alekhine Defense: Modern Variation": "Защита Алехина: современный вариант",
    "Alekhine Defense: Four Pawns Attack": "Защита Алехина: атака четырёх пешек",
    "Alekhine Defense: Exchange Variation": "Защита Алехина: разменный вариант",
    "Alekhine Defense: Scandinavian Variation": "Защита Алехина: скандинавский вариант",
    "Alekhine Defense: Two Pawns Attack": "Защита Алехина: атака двух пешек",
    "Alekhine Defense: Sämisch Attack": "Защита Алехина: атака Земиша",
    "Pirc Defense: Classical Variation": "Защита Пирца: классический вариант",
    "Pirc Defense: Austrian Attack": "Защита Пирца: австрийская атака",
    "Pirc Defense: Byrne Variation": "Защита Пирца: вариант Бирна",
    "Pirc Defense: Two Knights System": "Защита Пирца: система двух коней",
    "Pirc Defense: 150 Attack": "Защита Пирца: атака 150",
    "Italian Game: Giuoco Piano": "Итальянская партия: спокойная игра",
    "Italian Game: Giuoco Pianissimo": "Итальянская партия: тихая игра",
    "Italian Game: Evans Gambit": "Итальянская партия: гамбит Эванса",
    "Italian Game: Two Knights Defense": "Итальянская партия: защита двух коней",
    "Italian Game: Classical Variation": "Итальянская партия: классический вариант",
    "Italian Game: Anti-Fried Liver Defense": "Итальянская партия: защита анти-Фрид Ливер",
    "Italian Game: Hungarian Defense": "Итальянская партия: венгерская защита",
    "Italian Game: Paris Defense": "Итальянская партия: парижская защита",
    "Italian Game: Rousseau Gambit": "Итальянская партия: гамбит Руссо",
    "Scotch Game: Classical Variation": "Шотландская партия: классический вариант",
    "Scotch Game: Mieses Variation": "Шотландская партия: вариант Мизеса",
    "Scotch Game: Göring Gambit": "Шотландская партия: гамбит Гёринга",
    "Scotch Game: Scotch Gambit": "Шотландская партия: шотландский гамбит",
    "Scotch Game: Steinitz Variation": "Шотландская партия: вариант Штейница",
    "Scotch Game: Schmidt Variation": "Шотландская партия: вариант Шмидта",
    "Scotch Game: Blumenfeld Attack": "Шотландская партия: атака Блюменфельда",
    "Scotch Game: Potter Variation": "Шотландская партия: вариант Поттера",
    "Petrov Defense: Classical Variation": "Русская партия: классический вариант",
    "Petrov Defense: Nimzowitsch Attack": "Русская партия: атака Нимцовича",
    "Petrov Defense: Steinitz Variation": "Русская партия: вариант Штейница",
    "Petrov Defense: Marshall Attack": "Русская партия: атака Маршалла",
    "Petrov Defense: Cochrane Gambit": "Русская партия: гамбит Кокрейна",
    "Petrov Defense: Damiano Variation": "Русская партия: вариант Дамиано",
    "Petrov Defense: Stafford Gambit": "Русская партия: гамбит Стаффорда",
    "Four Knights Game: Italian Variation": "Дебют четырёх коней: итальянский вариант",
    "Four Knights Game: Spanish Variation": "Дебют четырёх коней: испанский вариант",
    "Four Knights Game: Scotch Variation": "Дебют четырёх коней: шотландский вариант",
    "Four Knights Game: Halloween Gambit": "Дебют четырёх коней: хэллоуин-гамбит",
    "English Opening: Symmetrical Variation": "Английское начало: симметричный вариант",
    "English Opening: King's English Variation": "Английское начало: королевский английский",
    "English Opening: Anglo-Indian Defense": "Английское начало: англо-индийская защита",
    "English Opening: Reversed Sicilian": "Английское начало: обратная сицилианка",
    "English Opening: Four Knights Variation": "Английское начало: вариант четырёх коней",
    "English Opening: Agincourt Defense": "Английское начало: защита Азенкура",
    "English Opening: Mikenas-Carls Variation": "Английское начало: вариант Микенаса-Карлса",
    "English Opening: Botvinnik System": "Английское начало: система Ботвинника",
    "Réti Opening: King's Indian Attack": "Дебют Рети: староиндийская атака",
    "Réti Opening: Anglo-Slav Variation": "Дебют Рети: англо-славянский вариант",
    "Réti Opening: Advance Variation": "Дебют Рети: вариант продвижения",
    "Catalan Opening: Open Defense": "Каталонское начало: открытая защита",
    "Catalan Opening: Closed": "Каталонское начало: закрытый вариант",
    "London System: Main Line": "Лондонская система: главный вариант",
    "London System: Jobava Attack": "Лондонская система: атака Джобавы",
    "London System: Poisoned Pawn Variation": "Лондонская система: вариант отравленной пешки",
}

OPENING_WORD_RU = {
    "defense": "защита", "defence": "защита",
    "opening": "начало", "game": "партия",
    "gambit": "гамбит", "attack": "атака",
    "variation": "вариант", "system": "система",
    "declined": "отказанный", "accepted": "принятый",
    "advance": "продвижение", "exchange": "разменный",
    "classical": "классический", "modern": "современный",
    "main line": "главная линия",
    "counterattack": "контратака", "countergambit": "контрагамбит",
    "accelerated": "ускоренный", "hyperaccelerated": "гиперускоренный",
    "delayed": "отложенный", "reversed": "обратный",
    "closed": "закрытый", "open": "открытый",
    "fianchetto": "фианкетто",
    "sicilian": "сицилианская", "french": "французская",
    "scandinavian": "скандинавская", "dutch": "голландская",
    "pirc": "Пирца", "slav": "славянская",
    "english": "английская", "italian": "итальянская",
    "spanish": "испанская", "vienna": "венская",
    "scotch": "шотландская", "russian": "русская",
    "hungarian": "венгерская", "paris": "парижская",
    "old": "старый", "traditional": "традиционный",
    "orthodox": "ортодоксальный", "anti-fried liver": "анти-Фрид Ливер",
    "yugoslav": "югославская", "poisoned pawn": "отравленная пешка",
    "normal": "обычный", "main": "главный",
    "richter-rauzer": "Рихтера-Раузера", "richter": "Рихтера",
    "rauzer": "Раузера", "sozin": "Созина",
    "velimirovic": "Велимировича", "spielmann": "Шпильмана",
    "opocensky": "Опоченского", "pin": "связка",
    "najdorf": "Найдорфа", "dragon": "Дракона",
    "scheveningen": "Схевенингена", "taimanov": "Тайманова",
    "sveshnikov": "Свешникова", "kan": "Кана",
    "alapin": "Алапина", "rossolimo": "Россолимо",
    "moscow": "московский", "berlin": "берлинская",
    "marshall": "Маршалла", "chigorin": "Чигорина",
    "winawer": "Винанвера", "tarrasch": "Тарраша",
    "rubinstein": "Рубинштейна", "capablanca": "Капабланки",
    "sämisch": "Земиша", "samisch": "Земиша",
    "meran": "Меранский", "sozin attack": "атака Созина",
    "panov": "Панова", "panov attack": "атака Панова",
    "smith-morra": "Смита-Морры",
    "lasker": "Ласкера", "lasker-pelikan": "Ласкера-Пеликана",
    "löwenthal": "Лёвенталя", "lowenthal": "Лёвенталя",
    "kalashnikov": "Калашникова", "nyezhmetdinov": "Нежметдинова",
    "prins": "Принса", "flohr": "Флора",
    "o'kelly": "О'Келли", "bowdler": "Боудлера",
    "snyder": "Снайдера", "boleslavsky": "Болеславского",
    "nimzowitsch": "Нимцовича", "reti": "Рети", "réti": "Рети",
    "bird": "Бёрда", "colle": "Колле",
    "trompowsky": "Тромповского", "torre": "Торре",
    "grünfeld": "Грюнфельда", "grunfeld": "Грюнфельда",
    "nimzo-indian": "новоиндийская",
    "king's indian": "староиндийская",
    "queen's indian": "новоиндийская, ферзевый фланг",
    "queen's gambit": "ферзевый гамбит",
    "king's gambit": "королевский гамбит",
    "benoni": "Бенони", "benko": "Бенко",
    "budapest": "Будапештский", "london": "лондонская",
    "catalan": "каталонское", "caro-kann": "Каро-Канн",
    "alekhine": "Алехина", "bogo-indian": "Боголюбова",
    "kid": "староиндийская", "qgd": "ферзевый гамбит отказанный",
    "qga": "ферзевый гамбит принятый",
    "najdorf variation": "вариант Найдорфа",
    "dragon variation": "вариант Дракона",
    "taimanov variation": "вариант Тайманова",
    "kan variation": "вариант Кана",
    "alapin variation": "вариант Алапина",
    "sveshnikov variation": "вариант Свешникова",
    "winawer variation": "вариант Винанвера",
    "tarrasch variation": "вариант Тарраша",
    "rubinstein variation": "вариант Рубинштейна",
    "rubinstein system": "система Рубинштейна",
    "sämisch variation": "вариант Земиша",
    "berlin defense": "берлинская защита",
    "marshall attack": "атака Маршалла",
    "chigorin defense": "защита Чигорина",
    "alekhine defense": "защита Алехина",
    "stonewall": "каменная стена",
    "leningrad": "ленинградская",
    "orthodox defense": "ортодоксальная защита",
    "tartakower defense": "защита Тартаковера",
    "ragozin defense": "защита Рагозина",
    "cambridge springs": "Кембридж-Спрингс",
    "averbakh variation": "вариант Авербаха",
    "four pawns attack": "атака четырёх пешек",
    "two pawns attack": "атака двух пешек",
    "petrosian system": "система Петросяна",
    "bayonet attack": "штыковая атака",
    "gligoric system": "система Глигорича",
    "mar del plata": "Мар-дель-Плата",
    "brinckmann attack": "атака Бринкмана",
    "botvinnik system": "система Ботвинника",
    "staunton gambit": "гамбит Стаунтона",
    "kieseritzky": "Кисерицкого",
    "bishop's gambit": "слоновый гамбит",
    "falkbeer": "Фалькбеера",
    "stanley variation": "вариант Стэнли",
    "mieses-kotroc": "Мизеса-Котроча",
    "valencian": "валенсианский",
    "anderssen counterattack": "контратака Андерсена",
    "icelandic-palme": "исландско-палмовый",
    "byrne variation": "вариант Бирна",
    "two knights system": "система двух коней",
    "150 attack": "атака 150",
    "giuoco piano": "спокойная игра",
    "giuoco pianissimo": "тихая игра",
    "evans gambit": "гамбит Эванса",
    "fried liver": "жареная печень",
    "rousseau gambit": "гамбит Руссо",
    "göring gambit": "гамбит Гёринга",
    "blumenfeld attack": "атака Блюменфельда",
    "potter variation": "вариант Поттера",
    "cochrane gambit": "гамбит Кокрейна",
    "damiano variation": "вариант Дамиано",
    "stafford gambit": "гамбит Стаффорда",
    "halloween gambit": "хэллоуин-гамбит",
    "symmetrical variation": "симметричный вариант",
    "king's english": "королевский английский",
    "anglo-indian defense": "англо-индийская защита",
    "anglo-slav": "англо-славянский",
    "agincourt defense": "защита Азенкура",
    "mikenas-carls": "Микенаса-Карлса",
    "jobava attack": "атака Джобавы",
    "ivory": "слоновая кость",
}


ADJ_ENDINGS = ("ая", "ская", "цкая", "ое", "ее", "ий", "ый", "ой", "ые", "ие")


def _count_latin_chars(text):
    return sum(1 for c in text if "a" <= c.lower() <= "z")


def _translate_words_simple(text):
    if not text:
        return ""
    text_lower = text.lower()
    phrases = sorted(OPENING_WORD_RU.keys(), key=lambda x: -len(x))
    result = []
    i = 0
    any_matched = False
    while i < len(text):
        matched = None
        for phrase in phrases:
            if text_lower.startswith(phrase, i):
                if i > 0 and text_lower[i - 1].isalpha():
                    continue
                j = i + len(phrase)
                if j < len(text) and text_lower[j].isalpha():
                    continue
                matched = phrase
                break
        if matched:
            result.append(OPENING_WORD_RU[matched])
            i += len(matched)
            any_matched = True
        else:
            if text[i].isalpha() or text[i] == "'":
                j = i
                while j < len(text) and (text[j].isalpha() or text[j] == "'"):
                    j += 1
                result.append(text[i:j])
                i = j
            else:
                result.append(text[i])
                i += 1
    out = "".join(result)
    if any_matched and out:
        out = out[0].upper() + out[1:]
    return out if any_matched else text


def _translate_words(text):
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    if text in OPENING_WORD_RU:
        return OPENING_WORD_RU[text]
    lower = text.lower()
    for en_desc, ru_desc in (
        ("variation", "вариант"),
        ("attack", "атака"),
        ("defense", "защита"),
        ("defence", "защита"),
        ("system", "система"),
        ("gambit", "гамбит"),
        ("opening", "начало"),
        ("game", "партия"),
        ("counterattack", "контратака"),
        ("countergambit", "контрагамбит"),
    ):
        suffix = " " + en_desc
        if lower.endswith(suffix) and len(text) > len(suffix):
            name_part = text[: -len(suffix)].strip()
            name_ru = _translate_words_simple(name_part)
            last_word = name_ru.split()[-1].lower() if name_ru.split() else ""
            if any(last_word.endswith(e) for e in ADJ_ENDINGS):
                return f"{name_ru} {ru_desc}"
            return f"{ru_desc} {name_ru}"
    return _translate_words_simple(text)


def translate_opening_name(name):
    if not name:
        return name
    if name in OPENING_NAME_RU_EXACT:
        return OPENING_NAME_RU_EXACT[name]
    if ":" in name:
        base, _, rest = name.partition(":")
        base = base.strip()
        rest = rest.strip()
    else:
        base, rest = name.strip(), ""
    base_ru = OPENING_NAME_RU_EXACT.get(base) or _translate_words(base)
    if not rest:
        return base_ru
    parts = [p.strip() for p in rest.split(",") if p.strip()]
    parts_ru = []
    for p in parts:
        full = f"{base}: {p}"
        if full in OPENING_NAME_RU_EXACT:
            ru_full = OPENING_NAME_RU_EXACT[full]
            if ":" in ru_full:
                _, _, sub = ru_full.partition(":")
                parts_ru.append(sub.strip())
            else:
                parts_ru.append(ru_full)
        else:
            translated = _translate_words(p)
            if _count_latin_chars(translated) > _count_latin_chars(p) * 0.5:
                parts_ru.append(p)
            else:
                parts_ru.append(translated)
    return f"{base_ru}: {', '.join(parts_ru)}"


def material_balance(board):
    total = 0
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p:
            v = PIECE_VALUES.get(p.symbol().lower(), 0)
            total += v if p.color == chess.WHITE else -v
    return total


def game_phase(board):
    total = 0
    queens = 0
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p:
            total += PIECE_VALUES.get(p.symbol().lower(), 0)
            if p.piece_type == chess.QUEEN:
                queens += 1
    if total >= 40:
        return "opening"
    if total >= 20 or queens >= 2:
        return "middlegame"
    return "endgame"


def detect_tactics(board_before, move):
    board = board_before.copy()
    piece_moved = board.piece_at(move.from_square)
    captured = board.piece_at(move.to_square)
    is_capture = captured is not None
    safe_push(board, move)
    is_check = board.is_check()
    is_mate = board.is_checkmate()
    tactics = []
    if piece_moved and piece_moved.piece_type != chess.KING:
        attacks = board.attacks(move.to_square)
        targets = []
        for sq in attacks:
            t = board.piece_at(sq)
            if t and t.color != piece_moved.color:
                targets.append((sq, t))
        valuable = [
            (sq, t) for sq, t in targets
            if t.piece_type in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.KING)
        ]
        has_check_target = any(t.piece_type == chess.KING for _, t in targets)
        if len(valuable) >= 2 or (len(targets) >= 2 and has_check_target):
            targets_ru = [piece_ru(t) for _, t in targets]
            tactics.append(("fork", targets_ru))
    if piece_moved and piece_moved.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        enemy = not piece_moved.color
        pinned_now = []
        for sq in chess.SQUARES:
            ep = board.piece_at(sq)
            if not ep or ep.color != enemy or ep.piece_type == chess.KING:
                continue
            if board.is_pinned(enemy, sq):
                if sq in board.attacks(move.to_square):
                    pinned_now.append(ep)
        if pinned_now:
            tactics.append(("pin", [piece_ru(p) for p in pinned_now[:2]]))
    if is_mate:
        king_sq = board.king(board.turn)
        if king_sq is not None and chess.square_rank(king_sq) in (0, 7):
            blocked = 0
            friendly = board.pieces(
                board.turn, chess.PAWN | chess.KNIGHT | chess.BISHOP | chess.ROOK | chess.QUEEN
            )
            for sq in chess.SQUARES:
                if sq in friendly and chess.square_distance(sq, king_sq) == 1:
                    blocked += 1
            if blocked >= 2:
                tactics.append(("back_rank_mate", []))
        if piece_moved and piece_moved.piece_type == chess.KNIGHT:
            own_pieces = 0
            for sq in chess.SQUARES:
                if chess.square_distance(sq, king_sq) == 1:
                    p = board.piece_at(sq)
                    if p and p.color == board.turn:
                        own_pieces += 1
            if own_pieces >= 3:
                tactics.append(("smothered_mate", []))
    return {
        "tactics": tactics, "is_capture": is_capture, "is_check": is_check,
        "is_mate": is_mate, "piece_moved": piece_moved, "captured": captured,
    }


def describe_threats(board, limit=2):
    threats = []
    for move in board.legal_moves:
        test = board.copy()
        test.push(move)
        if test.is_checkmate():
            try:
                threats.append(f"мат ({board.san(move)})")
            except Exception:
                threats.append("мат")
            return threats
    for move in board.legal_moves:
        if board.is_capture(move):
            cap = board.piece_at(move.to_square)
            if cap and cap.piece_type in (chess.QUEEN, chess.ROOK):
                try:
                    threats.append(f"взять {piece_ru(cap)} ({board.san(move)})")
                except Exception:
                    pass
                if len(threats) >= limit:
                    return threats
    if len(threats) < limit:
        for move in board.legal_moves:
            test = board.copy()
            test.push(move)
            if test.is_check():
                try:
                    threats.append(f"шах ({board.san(move)})")
                except Exception:
                    threats.append("шах")
                if len(threats) >= limit:
                    break
    return threats


def find_hanging_piece(board_after, moving_side):
    hanging = []
    for sq in chess.SQUARES:
        p = board_after.piece_at(sq)
        if not p or p.color != moving_side or p.piece_type == chess.KING:
            continue
        if not board_after.is_attacked_by(not moving_side, sq):
            continue
        defended = board_after.is_attacked_by(moving_side, sq)
        val = PIECE_VALUES[p.symbol().lower()]
        if not defended:
            hanging.append((val, sq, p))
        else:
            min_att = 100
            for s in chess.SQUARES:
                a = board_after.piece_at(s)
                if a and a.color != moving_side and sq in board_after.attacks(s):
                    min_att = min(min_att, PIECE_VALUES[a.symbol().lower()])
            if min_att < val:
                hanging.append((val, sq, p))
    if hanging:
        hanging.sort(key=lambda x: -x[0])
        return hanging[0][2]
    return None


def get_engine():
    global ENGINE
    if ENGINE is None:
        try:
            ENGINE = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
            try:
                ENGINE.configure({"Threads": 1, "Hash": 32})
            except Exception:
                pass
        except Exception:
            ENGINE = None
    return ENGINE


def reset_engine():
    global ENGINE
    try:
        if ENGINE is not None:
            ENGINE.quit()
    except Exception:
        pass
    ENGINE = None


def engine_analyse(fen, time_limit=0.35, multipv=1):
    with ENGINE_LOCK:
        eng = get_engine()
        if not eng:
            return None
        try:
            board = chess.Board(fen)
            infos = eng.analyse(board, chess.engine.Limit(time=time_limit), multipv=multipv)
            if isinstance(infos, dict):
                infos = [infos]
            out = []
            for info in infos:
                score = info.get("score")
                pv = info.get("pv") or []
                if score is None or not pv:
                    continue
                pov = score.pov(board.turn)
                cp = pov.score(mate_score=100000)
                mate = pov.mate()
                out.append({
                    "best": pv[0].uci(), "cp": cp, "mate": mate,
                    "pv": [m.uci() for m in pv[:10]],
                })
            return out or None
        except Exception:
            reset_engine()
            return None


def safe_engine_play(fen, level=5):
    with ENGINE_LOCK:
        eng = get_engine()
        if eng:
            try:
                skill = max(0, min(20, int(level * 2)))
                eng.configure({"Skill Level": skill})
                board = chess.Board(fen)
                limit = chess.engine.Limit(time=0.05 + level * 0.03)
                res = eng.play(board, limit)
                return res.move.uci()
            except Exception:
                reset_engine()
    board = chess.Board(fen)
    moves = list(board.legal_moves)
    return random.choice(moves).uci() if moves else None


def default_profile():
    return {
        "games": 0, "wins": 0, "losses": 0, "draws": 0,
        "moves": 0, "blunders": 0, "captures": 0, "checks": 0,
        "think_times": [], "history": [],
    }


def load_profile():
    if PROFILE_FILE.exists():
        try:
            p = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            for k, v in default_profile().items():
                p.setdefault(k, v)
            return p
        except Exception:
            pass
    return default_profile()


def save_profile(p):
    try:
        PROFILE_FILE.write_text(
            json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def maybe_save_profile(state):
    if state.get("profile_saved"):
        return
    if state["mode"] not in ("bot", "local"):
        return
    board = state["board"]
    if not board.is_game_over():
        return
    p = load_profile()
    result = board.result(claim_draw=False)
    p["games"] += 1
    if state["mode"] == "bot":
        if result == "1/2-1/2":
            p["draws"] += 1
        else:
            white_wins = result == "1-0"
            player_white = state["player_color"] == "white"
            if (white_wins and player_white) or (not white_wins and not player_white):
                p["wins"] += 1
            else:
                p["losses"] += 1
    st = state["stats"]
    p["moves"] += st.get("moves", 0)
    p["blunders"] += st.get("blunders", 0)
    p["captures"] += st.get("captures", 0)
    p["checks"] += st.get("checks", 0)
    p["think_times"].extend(st.get("think_times", [])[-80:])
    p["think_times"] = p["think_times"][-200:]
    p["history"].append({
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "mode": state["mode"], "result": result,
        "moves": st.get("moves", 0), "blunders": st.get("blunders", 0),
        "captures": st.get("captures", 0), "checks": st.get("checks", 0),
    })
    p["history"] = p["history"][-50:]
    save_profile(p)
    state["profile_saved"] = True


def _llm_autoportrait(profile):
    if not llm_available():
        return None
    try:
        games = profile.get("games", 0)
        if games == 0:
            return None
        moves = max(1, profile.get("moves", 0))
        stats_text = (
            f"Статистика игрока:\n"
            f"- Партий: {games}\n"
            f"- Побед: {profile.get('wins', 0)}, поражений: {profile.get('losses', 0)}, ничьих: {profile.get('draws', 0)}\n"
            f"- Ходов: {moves}\n"
            f"- Зевков: {profile.get('blunders', 0)} ({profile.get('blunders', 0)*100/moves:.1f}% ходов)\n"
            f"- Взятий: {profile.get('captures', 0)}\n"
            f"- Шахов: {profile.get('checks', 0)}"
        )
        system = (
            "Ты — дерзкий шахматный аналитик. Напиши игроку шаржированный автопортрет. "
            "3-5 предложений. По-русски. Матерись (бля, пиздец, хуёво, ахуеть), стеби, будь остроумным. "
            "Оцени: стиль игры (агрессивный / осторожный / зевающий), манеру (рубит / разыгрывает). "
            "Придумай прозвище: например «Кровавый Мясник», «Добрый Самаритянин — раздаёшь фигуры», "
            "«Гроссмейстер-Зевака». Ответь обычным текстом, без JSON."
        )
        body = {
            "model": LLM_CONFIG["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": stats_text},
            ],
            "temperature": 0.95, "max_tokens": 500,
        }
        raw = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_CONFIG['key']}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        req = urllib.request.Request(LLM_CONFIG["url"], data=raw, headers=headers, method="POST")
        with LLM_LOCK:
            with urllib.request.urlopen(req, timeout=LLM_CONFIG.get("timeout", 20)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"```\s*$", "", text).strip()
        if len(text) > 30:
            return text
    except Exception as e:
        print(f"[LLM autoportrait] error: {e}")
    return None


def make_autoportrait(p):
    games = p.get("games", 0)
    if games > 0:
        ai_text = _llm_autoportrait(p)
        if ai_text:
            return ai_text
    moves = max(1, p.get("moves", 0))
    wins = p.get("wins", 0)
    losses = p.get("losses", 0)
    draws = p.get("draws", 0)
    blunders = p.get("blunders", 0)
    captures = p.get("captures", 0)
    checks = p.get("checks", 0)
    winrate = int(wins * 100 / games) if games else 0
    zev = blunders * 100 / moves
    cap = captures * 100 / moves
    chk = checks * 100 / moves
    if cap > 18:
        style = "Кровавый мясник"
    elif cap > 10:
        style = "Агрессивный охотник"
    elif chk > 8:
        style = "Террорист на доске"
    elif blunders > moves * 0.12:
        style = "Добрый самаритянин — раздаёшь фигуры"
    else:
        style = "Осторожный ёжик"
    if zev > 20:
        mind = "Ты зеваешь так часто, что Stockfish уже устал удивляться."
    elif zev > 12:
        mind = "Зевков многовато. Пора включать голову до хода, а не после."
    elif zev > 5:
        mind = "Ошибки есть, но уже не катастрофа. Растёшь."
    else:
        mind = "Зевков мало. Это уже похоже на шахматиста."
    if games == 0:
        return (
            "Ты пока не сыграл ни одной партии. Автопортрет пустой, как "
            "шахматная доска после первого хода. Начни с бота или тренера."
        )
    return (
        f"Ты сыграл {games} партий. Винрейт {winrate}% "
        f"({wins} побед, {losses} поражений, {draws} ничьих). "
        f"Стиль: {style}. "
        f"Мышление: {zev:.1f}% твоих ходов — зевки. {mind} "
        f"Взятия: {cap:.1f}% ходов. Шахи: {chk:.1f}%. "
        f"Соберись. Гроссмейстеры тоже когда-то зевали, но не так часто."
    )


POPULAR_OPENINGS = [
    "Sicilian Defense", "French Defense", "Ruy Lopez", "Italian Game",
    "Caro-Kann Defense", "Queen's Gambit", "King's Indian Defense",
    "Nimzo-Indian Defense", "English Opening", "Scandinavian Defense",
    "Pirc Defense", "Alekhine Defense", "Vienna Game", "Scotch Game",
    "Four Knights Game", "London System", "Catalan Opening",
    "Dutch Defense", "Slav Defense", "Grünfeld Defense",
]

OPENING_IDEAS = {
    "Sicilian Defense": "Чёрные меняют пешку c на d, чтобы получить открытую линию c и контригру.",
    "French Defense": "Чёрные строят прочную структуру e6-d5 и атакуют центр пешкой c5.",
    "Italian Game": "Быстрое развитие, слон c4 давит на f7 — слабейший пункт чёрных.",
    "Ruy Lopez": "Слон b5 атакует коня c6 — защитника пешки e5.",
    "Caro-Kann Defense": "Готовим d5 с поддержкой c6, не запирая слона c8.",
    "Queen's Gambit": "Белые предлагают пешку c4, чтобы отвлечь чёрную d5.",
    "King's Indian Defense": "Чёрные отдают центр, атакуют его позже пешками e5/c5.",
    "Nimzo-Indian Defense": "Чёрные связывают коня c3 слоном b4, контролируя e4.",
    "English Opening": "Белые контролируют d5 с фланга, готовят фианкетто g2.",
    "Scandinavian Defense": "Чёрные меняют d на e, получая открытую линию d.",
    "Pirc Defense": "Гибкая структура, контригра по центру фигурами.",
    "Alekhine Defense": "Провокация пешек, чтобы потом атаковать их.",
    "Vienna Game": "Белые готовят f4 и быструю атаку на королевском фланге.",
    "Scotch Game": "Белые сразу вскрывают центр, играя на инициативу.",
    "Four Knights Game": "Симметричное развитие, спокойная борьба.",
    "London System": "Прочный треугольник d4-e3-c3 и давление по тёмным полям.",
    "Catalan Opening": "Белые фианкеттируют слона g2 и давят на ферзевом фланге.",
    "Dutch Defense": "Чёрные сразу атакуют королевский фланг пешкой f5.",
    "Slav Defense": "Прочная защита пешки d5 пешкой c6.",
    "Grünfeld Defense": "Чёрные отдают центр, атакуют его издалека.",
}

OPENING_MOVE_NOTES = {
    "e2e4": "Захват центра. Открывает слона f1 и ферзя d1.",
    "d2d4": "Захват центра. Контролирует e5, открывает слона c1.",
    "c2c4": "Фланговый контроль d5, гибкая структура.",
    "g1f3": "Развитие коня. Атакует e5/d4, готовит рокировку.",
    "b1c3": "Развитие коня. Контролирует d5 и e4.",
    "f1c4": "Слон на c4 атакует f7 — слабейший пункт чёрных.",
    "f1b5": "Слон b5 атакует коня c6 — защитника e5.",
    "f1g2": "Фианкетто. Слон g2 давит по длинной диагонали.",
    "c1f4": "Слон f4 — активная позиция, классика лондонской системы.",
    "e2e3": "Скромный ход, открывает слона f1, готовит d4.",
    "e7e5": "Симметричный ответ. Борьба за центр.",
    "c7c5": "Сицилианская защита. Меняем c на d, получаем линию c.",
    "e7e6": "Французская защита. Прочная структура, но слон c8 заперт.",
    "c7c6": "Каро-Канн. Готовим d5, не запирая слона.",
    "d7d5": "Захват центра. Часто ведёт к ферзевому гамбиту.",
    "g8f6": "Развитие коня. Атакует e4, готовит рокировку.",
    "d7d6": "Защита Пирца. Гибкая структура.",
    "b8c6": "Развитие коня. Контролирует d4 и e5.",
    "g7g6": "Готовим фианкетто слона g7.",
    "f7f5": "Голландская защита. Агрессивно, но ослабляет короля.",
}


def parse_pgn_to_uci(pgn):
    board = chess.Board()
    moves = []
    for tok in pgn.split():
        if re.match(r"^\d+\.", tok):
            continue
        if tok in ("1-0", "0-1", "1/2-1/2", "*"):
            continue
        tok = tok.rstrip(".")
        if not tok:
            continue
        try:
            mv = board.parse_san(tok)
            moves.append(mv.uci())
            board.push(mv)
        except Exception:
            tok2 = re.sub(r"[!?]+", "", tok)
            try:
                mv = board.parse_san(tok2)
                moves.append(mv.uci())
                board.push(mv)
            except Exception:
                break
    return moves


def ensure_openings():
    global OPENINGS
    with OPENINGS_LOCK:
        if OPENINGS is not None:
            return OPENINGS
        items = []
        if OPENINGS_CACHE.exists():
            try:
                for line in OPENINGS_CACHE.read_text(encoding="utf-8").splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        items.append({
                            "eco": parts[0], "name": parts[1],
                            "moves": parts[2].split(),
                        })
            except Exception:
                pass
        if not items:
            for c in "abcde":
                url = f"https://raw.githubusercontent.com/lichess-org/chess-openings/master/{c}.tsv"
                try:
                    with urllib.request.urlopen(url, timeout=12) as r:
                        text = r.read().decode("utf-8")
                    for line in text.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 3 and parts[0] != "eco":
                            moves = parse_pgn_to_uci(parts[2])
                            if moves:
                                items.append({
                                    "eco": parts[0], "name": parts[1], "moves": moves,
                                })
                except Exception:
                    pass
            if items:
                try:
                    OPENINGS_CACHE.write_text(
                        "\n".join(
                            f"{x['eco']}\t{x['name']}\t{' '.join(x['moves'])}"
                            for x in items
                        ), encoding="utf-8"
                    )
                except Exception:
                    pass
        if not items:
            items = [
                {"eco": "C20", "name": "Открытый дебют", "moves": ["e2e4", "e7e5"]},
                {"eco": "C50", "name": "Итальянская партия", "moves": ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]},
                {"eco": "C60", "name": "Испанская партия", "moves": ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]},
                {"eco": "B20", "name": "Сицилианская защита", "moves": ["e2e4", "c7c5"]},
                {"eco": "C00", "name": "Французская защита", "moves": ["e2e4", "e7e6"]},
                {"eco": "B10", "name": "Защита Каро-Канн", "moves": ["e2e4", "c7c6"]},
                {"eco": "D06", "name": "Ферзевый гамбит", "moves": ["d2d4", "d7d5", "c2c4"]},
                {"eco": "A10", "name": "Английское начало", "moves": ["c2c4"]},
            ]

        def sort_key(x):
            nm = x["name"].lower()
            for i, p in enumerate(POPULAR_OPENINGS):
                if p.lower() in nm:
                    return (0, i, nm)
            return (1, 999, nm)

        items.sort(key=sort_key)
        OPENINGS = items
        return OPENINGS


def identify_opening_from_moves(uci_seq):
    try:
        ops = ensure_openings()
    except Exception:
        return None
    best = None
    for op in ops:
        op_moves = op["moves"]
        n = min(len(op_moves), len(uci_seq))
        if n == 0:
            continue
        if op_moves[:n] == uci_seq[:n]:
            if best is None or len(op_moves) > len(best["moves"]):
                best = op
    return best


def opening_idea(op_name):
    if not op_name:
        return ""
    for key, idea in OPENING_IDEAS.items():
        if key.lower() in op_name.lower():
            return idea
    return ""


def opening_move_note(uci):
    return OPENING_MOVE_NOTES.get(uci, "")


LESSONS = [
    {"id": 1, "title": "Ход королём", "description": "Король ходит на одну клетку.",
     "task": "Сходи королём с a1 на a2.", "fen": "7k/8/8/8/8/8/8/K7 w - - 0 1", "solution": ["a1a2"]},
    {"id": 2, "title": "Ход конём", "description": "Конь ходит буквой Г.",
     "task": "Сходи конём с b1 на c3.", "fen": "7k/8/8/8/8/8/8/KN6 w - - 0 1", "solution": ["b1c3"]},
    {"id": 3, "title": "Ход слоном", "description": "Слон ходит по диагонали.",
     "task": "Сходи слоном с b1 на c2.", "fen": "7k/8/8/8/8/8/8/KB6 w - - 0 1", "solution": ["b1c2"]},
    {"id": 4, "title": "Ход ладьёй", "description": "Ладья ходит прямо.",
     "task": "Сходи ладьёй с b1 на b7.", "fen": "7k/8/8/8/8/8/8/KR6 w - - 0 1", "solution": ["b1b7"]},
    {"id": 5, "title": "Ход ферзём", "description": "Ферзь — самая сильная фигура.",
     "task": "Сходи ферзём с b1 на b7.", "fen": "7k/8/8/8/8/8/8/KQ6 w - - 0 1", "solution": ["b1b7"]},
    {"id": 6, "title": "Взятие пешкой", "description": "Пешка бьёт по диагонали.",
     "task": "Съешь пешку на c3.", "fen": "7k/8/8/8/8/2p5/1P6/K7 w - - 0 1", "solution": ["b2c3"]},
    {"id": 7, "title": "Мат в 1: горизонталь", "description": "Ладья по последней горизонтали.",
     "task": "Поставь мат.", "fen": "6k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1", "solution": ["e1e8"]},
    {"id": 8, "title": "Мат в 1: ладья", "description": "Ищи открытую линию.",
     "task": "Поставь мат.", "fen": "7k/6pp/8/8/8/8/6PP/5R1K w - - 0 1", "solution": ["f1f8"]},
    {"id": 9, "title": "Мат в 1: ферзь", "description": "Зажми короля.",
     "task": "Поставь мат.", "fen": "k7/8/1K6/8/8/8/8/7Q w - - 0 1", "solution": ["h1h8"]},
    {"id": 10, "title": "Мат в 1: ферзь по горизонтали", "description": "Ферзь по открытой линии.",
     "task": "Поставь мат.", "fen": "7k/8/8/8/8/8/5Q2/6K1 w - - 0 1", "solution": ["f2f8"]},
    {"id": 11, "title": "Мат в 1: вторая ладья", "description": "Иногда мат с другой стороны.",
     "task": "Поставь мат.", "fen": "R6k/6pp/8/8/8/8/6PP/6K1 w - - 0 1", "solution": ["a1a8"]},
    {"id": 12, "title": "Вилка конём", "description": "Конь может напасть на две фигуры.",
     "task": "Атакуй конём ферзя и ладью.",
     "fen": "7k/8/8/8/5q2/8/8/KN2r3 w - - 0 1", "solution": ["b1c3"]},
]

MATE_PUZZLES = {
    "mate1": [
        {"fen": "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "solution": ["a1a8"]},
        {"fen": "6k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1", "solution": ["e1e8"]},
        {"fen": "7k/6pp/8/8/8/8/6PP/5R1K w - - 0 1", "solution": ["f1f8"]},
        {"fen": "k7/8/1K6/8/8/8/8/7R w - - 0 1", "solution": ["h1h8"]},
        {"fen": "k7/8/1K6/8/8/8/8/7Q w - - 0 1", "solution": ["h1h8"]},
        {"fen": "7k/6pp/8/8/8/8/6PP/Q6K w - - 0 1", "solution": ["a1a8"]},
        {"fen": "R6k/6pp/8/8/8/8/6PP/6K1 w - - 0 1", "solution": ["a1a8"]},
        {"fen": "6k1/5ppp/8/8/8/8/5PPP/1R4K1 w - - 0 1", "solution": ["b1b8"]},
    ],
    "mate2": [
        {"fen": "7k/8/8/8/8/8/R7/1R5K w - - 0 1", "solution": ["a2a8", "h8h7", "b1b7"]},
        {"fen": "r5rk/5p1p/5R2/4B3/8/8/7P/7K w - - 0 1", "solution": ["f6f7", "g8g7", "f7f8"]},
        {"fen": "1r3rk1/1p2nppp/p1n1p3/8/2B5/2N2Q2/PPP2PPP/3R1RK1 w - - 0 1", "solution": ["d1d8", "b8d8", "f3f7"]},
    ],
    "mate3": [
        {"fen": "6k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1", "solution": []},
        {"fen": "7k/8/5K2/8/8/8/8/6R1 w - - 0 1", "solution": []},
    ],
}


# ==========================================================================
# ГЕНЕРАЦИЯ УНИКАЛЬНЫХ ЗАДАЧ
# ==========================================================================
def _puzzle_signature(fen, solution):
    try:
        board = chess.Board(fen)
    except Exception:
        return None
    mat_piece_type = None
    mat_square = None
    if solution:
        prep = chess.Board(fen)
        for uci in solution[:-1]:
            try:
                mv = safe_move(prep, uci)
                if mv is not None and mv in prep.legal_moves:
                    prep.push(mv)
            except Exception:
                pass
        try:
            last_move = safe_move(prep, solution[-1])
            if last_move is not None:
                p = prep.piece_at(last_move.from_square)
                if p:
                    mat_piece_type = p.piece_type
                mat_square = last_move.to_square
        except Exception:
            pass
    bk = board.king(chess.BLACK)
    bk_file = chess.square_file(bk) if bk is not None else -1
    bk_rank = chess.square_rank(bk) if bk is not None else -1
    piece_counts = {}
    w_mat = 0
    b_mat = 0
    for pt in range(1, 7):
        sym = chess.piece_symbol(pt).lower()
        val = PIECE_VALUES.get(sym, 0)
        w = len(board.pieces(pt, chess.WHITE))
        b = len(board.pieces(pt, chess.BLACK))
        piece_counts[pt] = w + b
        w_mat += w * val
        b_mat += b * val
    return {
        "mat_piece_type": mat_piece_type, "mat_square": mat_square,
        "king_file": bk_file, "king_rank": bk_rank,
        "total_pieces": sum(piece_counts.values()),
        "w_mat": w_mat, "b_mat": b_mat,
        "has_queen": piece_counts.get(chess.QUEEN, 0) > 0,
        "has_rook": piece_counts.get(chess.ROOK, 0) > 0,
        "has_bishop": piece_counts.get(chess.BISHOP, 0) > 0,
        "has_knight": piece_counts.get(chess.KNIGHT, 0) > 0,
        "has_pawns": piece_counts.get(chess.PAWN, 0) > 0,
        "pawns_count": piece_counts.get(chess.PAWN, 0),
    }


def _puzzle_distance(sig1, sig2):
    if not sig1 or not sig2:
        return 100
    d = 0
    if sig1["mat_piece_type"] != sig2["mat_piece_type"]:
        d += 5
    s1, s2 = sig1["mat_square"], sig2["mat_square"]
    if s1 != s2 and s1 is not None and s2 is not None:
        fd = abs(chess.square_file(s1) - chess.square_file(s2))
        rd = abs(chess.square_rank(s1) - chess.square_rank(s2))
        d += 3 if (fd + rd) >= 3 else 1
    k1 = (sig1["king_file"], sig1["king_rank"])
    k2 = (sig2["king_file"], sig2["king_rank"])
    if k1 != k2 and k1[0] >= 0 and k2[0] >= 0:
        kd = abs(k1[0] - k2[0]) + abs(k1[1] - k2[1])
        d += 4 if kd >= 3 else 2
    tp_diff = abs(sig1["total_pieces"] - sig2["total_pieces"])
    if tp_diff >= 3:
        d += 3
    elif tp_diff >= 1:
        d += 1
    if abs(sig1["w_mat"] - sig2["w_mat"]) >= 3:
        d += 2
    if abs(sig1["b_mat"] - sig2["b_mat"]) >= 3:
        d += 2
    for key in ("has_queen", "has_rook", "has_bishop", "has_knight", "has_pawns"):
        if sig1[key] != sig2[key]:
            d += 2
    if abs(sig1["pawns_count"] - sig2["pawns_count"]) >= 3:
        d += 2
    return d


def _record_puzzle_sig(ptype, sig):
    if not sig:
        return
    hist = LAST_PUZZLE_SIGS.setdefault(ptype, [])
    hist.append(sig)
    if len(hist) > HISTORY_SIZE:
        del hist[: len(hist) - HISTORY_SIZE]


def _min_distance_to_history(ptype, sig):
    hist = LAST_PUZZLE_SIGS.get(ptype) or []
    if not hist:
        return 100
    return min(_puzzle_distance(sig, h) for h in hist)


def _solve_mate_in_1(board):
    for move in board.legal_moves:
        test = board.copy()
        test.push(move)
        if test.is_checkmate():
            return move.uci()
    return None


def _build_random_endgame(mate_in):
    board = chess.Board(None)
    edge_sq = [sq for sq in chess.SQUARES
               if chess.square_rank(sq) in (0, 7) or chess.square_file(sq) in (0, 7)]
    bk_sq = random.choice(edge_sq)
    board.set_piece_at(bk_sq, chess.Piece(chess.KING, chess.BLACK))
    wk_candidates = [sq for sq in chess.SQUARES
                     if chess.square_distance(sq, bk_sq) >= 2]
    if not wk_candidates:
        return None
    wk_sq = random.choice(wk_candidates)
    board.set_piece_at(wk_sq, chess.Piece(chess.KING, chess.WHITE))
    if mate_in == 1:
        pool = [chess.QUEEN, chess.ROOK, chess.ROOK, chess.QUEEN, chess.BISHOP]
        n_att = random.randint(1, 2)
    elif mate_in == 2:
        pool = [chess.QUEEN, chess.ROOK, chess.ROOK, chess.QUEEN]
        n_att = random.randint(1, 3)
    else:
        pool = [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.ROOK]
        n_att = random.randint(2, 4)
    placed = {}
    max_per = {chess.QUEEN: 1, chess.ROOK: 2, chess.BISHOP: 2,
               chess.KNIGHT: 2, chess.PAWN: 8}
    for _ in range(n_att):
        available_types = [pt for pt in pool if placed.get(pt, 0) < max_per.get(pt, 0)]
        if not available_types:
            break
        pt = random.choice(available_types)
        placed[pt] = placed.get(pt, 0) + 1
        avail = [sq for sq in chess.SQUARES if board.piece_at(sq) is None]
        if not avail:
            break
        sq = random.choice(avail)
        board.set_piece_at(sq, chess.Piece(pt, chess.WHITE))
    board.turn = chess.WHITE
    if not board.is_valid():
        return None
    if board.is_check():
        return None
    if board.is_game_over():
        return None
    return board


def _generate_mate1(max_attempts=300):
    for _ in range(max_attempts):
        board = _build_random_endgame(1)
        if board is None:
            continue
        mate_move = _solve_mate_in_1(board)
        if mate_move is None:
            continue
        fen = board.fen()
        if _fen_key(fen) in SOLVED_PUZZLES:
            continue
        return {"fen": fen, "solution": [mate_move], "type": "mate1", "mate": 1}
    return None


def _generate_mate_via_sf(mate_in, max_attempts=25, tl=0.05):
    for _ in range(max_attempts):
        board = _build_random_endgame(mate_in)
        if board is None:
            continue
        info = engine_analyse(board.fen(), time_limit=tl)
        if not info:
            continue
        if info[0].get("mate") != mate_in:
            continue
        pv = info[0].get("pv", [])
        if len(pv) < 2 * mate_in - 1:
            continue
        sol = pv[: 2 * mate_in - 1]
        test = board.copy()
        ok = True
        for uci in sol:
            mv = safe_move(test, uci)
            if not mv or mv not in test.legal_moves:
                ok = False
                break
            test.push(mv)
        if not ok or not test.is_checkmate():
            continue
        fen = board.fen()
        if _fen_key(fen) in SOLVED_PUZZLES:
            continue
        return {"fen": fen, "solution": sol,
                "type": f"mate{mate_in}", "mate": mate_in}
    return None


def _validate_puzzle(cand, mate_in):
    sol = cand.get("solution") or []
    if len(sol) != 2 * mate_in - 1:
        return False
    try:
        board = chess.Board(cand["fen"])
        for uci in sol:
            mv = safe_move(board, uci)
            if mv is None or mv not in board.legal_moves:
                return False
            board.push(mv)
        return board.is_checkmate()
    except Exception:
        return False


def _reset_solved_handcrafted():
    changed = False
    for ptype_data in MATE_PUZZLES.values():
        for p in ptype_data:
            if _fen_key(p["fen"]) in SOLVED_PUZZLES:
                SOLVED_PUZZLES.discard(_fen_key(p["fen"]))
                changed = True
    if changed:
        save_solved_puzzles()


def generate_llm_puzzle(mate_in=1):
    if not llm_available():
        return None
    prompt = (
        f"Сгенерируй валидный FEN шахматной позиции, где белые ставят мат ровно в {mate_in} ход(а). "
        "Позиция должна быть легальной, у белых ход (w), чёрный король не под шахом. "
        "Ответь строго JSON: {\"fen\": \"строка FEN\"}"
    )
    try:
        body = {
            "model": LLM_CONFIG["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9, "max_tokens": 100,
        }
        raw = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_CONFIG['key']}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        req = urllib.request.Request(LLM_CONFIG["url"], data=raw, headers=headers, method="POST")
        with LLM_LOCK:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        content_str = data["choices"][0]["message"]["content"].strip()
        if content_str.startswith("```"):
            content_str = re.sub(r"^```[a-zA-Z]*\s*", "", content_str)
            content_str = re.sub(r"```\s*$", "", content_str).strip()
        parsed = json.loads(content_str)
        fen = (parsed.get("fen") or "").strip()
        if not fen:
            return None
        board = chess.Board(fen)
        if not board.is_valid() or board.is_check() or board.is_game_over():
            return None
        info = engine_analyse(fen, time_limit=0.4, multipv=1)
        if not info or info[0].get("mate") != mate_in:
            return None
        pv = info[0].get("pv", [])
        if len(pv) < 2 * mate_in - 1:
            return None
        sol = pv[: 2 * mate_in - 1]
        test = board.copy()
        for uci in sol:
            mv = safe_move(test, uci)
            if not mv or mv not in test.legal_moves:
                return None
            test.push(mv)
        if not test.is_checkmate():
            return None
        return {"fen": fen, "solution": sol,
                "type": f"mate{mate_in}", "mate": mate_in}
    except Exception as e:
        print(f"[LLM puzzle error] {e}")
        return None


def get_mate_puzzle(ptype):
    n = 1 if ptype == "mate1" else 2 if ptype == "mate2" else 3
    ptype_key = f"mate{n}"

    def _accept(puz):
        if not puz:
            return False
        fen = puz.get("fen")
        if not fen:
            return False
        if _fen_key(fen) in SOLVED_PUZZLES:
            return False
        sig = _puzzle_signature(fen, puz["solution"])
        if not sig:
            return False
        if _min_distance_to_history(ptype_key, sig) < MIN_DISTANCE_STRICT:
            return False
        _record_puzzle_sig(ptype_key, sig)
        return True

    if llm_available():
        for _ in range(2):
            try:
                puz = generate_llm_puzzle(n)
                if _accept(puz):
                    return puz
            except Exception:
                pass

    for _ in range(3):
        if n == 1:
            puz = _generate_mate1(300)
        else:
            puz = _generate_mate_via_sf(n, max_attempts=25 if n == 2 else 12,
                                        tl=0.05 if n == 2 else 0.15)
        if _accept(puz):
            return puz

    for _ in range(2):
        if n == 1:
            puz = _generate_mate1(500)
        else:
            puz = _generate_mate_via_sf(n, max_attempts=20 if n == 2 else 10,
                                        tl=0.05 if n == 2 else 0.15)
        if puz and _fen_key(puz["fen"]) not in SOLVED_PUZZLES:
            sig = _puzzle_signature(puz["fen"], puz["solution"])
            _record_puzzle_sig(ptype_key, sig)
            return puz

    _reset_solved_handcrafted()
    for ptype_data in MATE_PUZZLES.get(ptype_key, []):
        if _validate_puzzle(ptype_data, n):
            if _fen_key(ptype_data["fen"]) in SOLVED_PUZZLES:
                continue
            sig = _puzzle_signature(ptype_data["fen"], ptype_data["solution"])
            _record_puzzle_sig(ptype_key, sig)
            return {"fen": ptype_data["fen"],
                    "solution": ptype_data["solution"][:],
                    "type": ptype_key, "mate": n}

    for ptype_data in MATE_PUZZLES.get(ptype_key, []) + MATE_PUZZLES["mate1"]:
        if _validate_puzzle(ptype_data, n if ptype_data in MATE_PUZZLES.get(ptype_key, []) else 1):
            return {"fen": ptype_data["fen"],
                    "solution": ptype_data["solution"][:],
                    "type": ptype_key, "mate": n}

    return {"fen": MATE_PUZZLES["mate1"][0]["fen"],
            "solution": MATE_PUZZLES["mate1"][0]["solution"],
            "type": "mate1", "mate": 1}


def make_new_state(
    mode, player_color="white", level=5, time_control=0,
    lesson_id=None, puzzle_type=None, opening_name=None, profanity=True
):
    gid = secrets.token_hex(8)
    board = chess.Board()
    if player_color == "random":
        player_color = random.choice(["white", "black"])
    state = {
        "id": gid, "mode": mode, "player_color": player_color,
        "level": int(level or 5), "time_control": int(time_control or 0),
        "board": board, "moves": [], "fens": [board.fen()],
        "history_index": 0, "last_move": None,
        "last_event": {"type": "start"},
        "coach_messages": [], "used_phrases": [],
        "stats": {"moves": 0, "blunders": 0, "captures": 0, "checks": 0, "think_times": []},
        "profanity": bool(profanity), "profile_saved": False,
        "solved": False, "timed_out": False, "result_override": None,
        "opening_announced": False, "opening_completed": False,
        "current_opening_name": None, "current_opening_name_ru": None,
        "current_opening_idea": None,
        "version": 0,
    }
    if mode == "lesson":
        lesson = next((l for l in LESSONS if l["id"] == int(lesson_id or 1)), LESSONS[0])
        board = chess.Board(lesson["fen"])
        state["board"] = board
        state["fens"] = [board.fen()]
        state["lesson"] = lesson
        state["lesson_title"] = lesson["title"]
        state["solution"] = lesson["solution"][:]
        state["solution_index"] = 0
    elif mode == "puzzle":
        puz = get_mate_puzzle(puzzle_type or "mate1")
        board = chess.Board(puz["fen"])
        state["board"] = board
        state["fens"] = [board.fen()]
        state["puzzle"] = puz
        state["puzzle_solution"] = puz["solution"][:]
        state["puzzle_index"] = 0
        state["puzzle_type"] = puz["type"]
        state["puzzle_start_fen"] = puz["fen"]
    elif mode == "opening":
        ops = ensure_openings()
        op = next((o for o in ops if o["name"] == opening_name), ops[0])
        state["opening"] = op
        state["opening_name"] = op["name"]
        state["opening_name_ru"] = translate_opening_name(op["name"])
        state["opening_moves"] = op["moves"][:]
        state["opening_index"] = 0
        state["deviation"] = False
        state["deviation_announced"] = False
        state["hint_uci"] = op["moves"][0] if op["moves"] else None
        state["current_opening_name"] = op["name"]
        state["current_opening_name_ru"] = state["opening_name_ru"]
        state["current_opening_idea"] = opening_idea(op["name"])
        state["coach_messages"].append({
            "text": f"Тренируем дебют: {state['opening_name_ru']}. {opening_idea(op['name'])}",
            "emotion": "calm", "source": "system",
            "is_bot": False, "time": time.time(),
        })
        if not op["moves"]:
            state["opening_completed"] = True
    if mode in ("bot", "opening", "blitz", "coach") and player_color == "black":
        if not state.get("opening_completed"):
            make_bot_move(state)
    return state


def status_text(state):
    board = state["board"]
    if state.get("timed_out"):
        return f"Время вышло! Результат: {state.get('result_override')}"
    if state.get("solved"):
        return "Задача решена! Отлично."
    if state["mode"] == "opening" and state.get("opening_completed"):
        op_ru = state.get("opening_name_ru") or state.get("opening_name", "")
        return f"✅ Дебют «{op_ru}» выполнен! Отлично."
    if board.is_game_over():
        res = board.result(claim_draw=False)
        if board.is_checkmate():
            return f"Мат! Результат: {res}"
        return f"Партия окончена: {res}"
    turn = "белых" if board.turn else "чёрных"
    extra = ""
    if state["mode"] == "lesson":
        extra = f" Задание: {state['lesson']['task']}"
    elif state["mode"] == "puzzle":
        extra = f" Найди мат в {state.get('puzzle', {}).get('mate', 1)}."
    elif state["mode"] == "opening":
        n = state.get("opening_index", 0)
        total = len(state.get("opening_moves", []))
        op_ru = state.get("opening_name_ru") or state.get("opening_name", "")
        extra = f" Дебют: {op_ru}. Ход {n}/{total}."
    if state["mode"] in ("bot", "coach", "opening", "blitz") and state["moves"]:
        last = state["moves"][-1]
        if last.get("is_bot") and state["mode"] not in ("bot", "local"):
            _h = last.get("human") or last.get("san") or ""
            if _h:
                extra += f" Соперник сыграл: {_h}."
    return f"Ход {turn}.{extra}"


def public_state(state):
    board = state["board"]
    timed_out = bool(state.get("timed_out"))
    opening_completed = bool(state.get("opening_completed"))
    if timed_out:
        result = state.get("result_override")
    elif opening_completed:
        result = "opening-completed"
    elif board.is_game_over():
        result = board.result(claim_draw=False)
    else:
        result = None
    llm_active = LLM_LAST_OK.get(state["id"])
    if llm_active is None:
        llm_active = llm_available()
    return {
        "game_id": state["id"], "mode": state["mode"],
        "player_color": state["player_color"], "level": state["level"],
        "time_control": state["time_control"], "fen": board.fen(),
        "turn": "white" if board.turn else "black",
        "legal_moves": [m.uci() for m in board.legal_moves],
        "moves": state["moves"], "fens": state["fens"],
        "history_index": state["history_index"],
        "last_move": state["last_move"],
        "last_event": state.get("last_event"),
        "game_over": timed_out or opening_completed or board.is_game_over(),
        "timed_out": timed_out, "result": result,
        "opening_completed": opening_completed,
        "status": status_text(state),
        "coach_messages": state["coach_messages"][-80:],
        "hint_uci": state.get("hint_uci"),
        "opening_name": state.get("opening_name_ru") or state.get("opening_name"),
        "opening_idea": state.get("current_opening_idea"),
        "opening_index": state.get("opening_index"),
        "opening_total": len(state.get("opening_moves", [])) if state["mode"] == "opening" else 0,
        "deviation": state.get("deviation"),
        "lesson_id": state.get("lesson", {}).get("id") if state.get("lesson") else None,
        "lesson_title": state.get("lesson_title"),
        "puzzle_type": state.get("puzzle_type"),
        "solved": state.get("solved"),
        "stats": state["stats"],
        "llm_active": bool(llm_active),
    }


def _pick(state, options):
    used = state.setdefault("used_phrases", [])
    available = [o for o in options if o not in used]
    if not available:
        available = options
        state["used_phrases"] = [o for o in used if o not in options]
    choice = random.choice(available)
    state["used_phrases"].append(choice)
    if len(state["used_phrases"]) > 60:
        state["used_phrases"] = state["used_phrases"][-40:]
    return choice


def _time_scold(state, think_time):
    if state["mode"] not in ("blitz", "speed", "coach") or not think_time:
        return ""
    limit = 6 if state["time_control"] <= 60 else 10 if state["time_control"] <= 180 else 15
    if think_time > limit:
        return f" И ты думал {int(think_time)} секунд — в таком контроле это вечность!"
    return ""


def _make_bot_comment(state, board_before, board_after, move, san):
    tactics = detect_tactics(board_before, move)
    captured = tactics["captured"]
    captured_ru = piece_ru(captured) if captured else None
    if tactics["is_mate"]:
        return _pick(state, [
            f"Мат! {san}. Соперник победил.",
            f"{san} — мат. Партия окончена.",
        ]), "angry"
    if tactics["is_check"]:
        return _pick(state, [
            f"Он объявил шах ходом {san}. Защищай короля.",
            f"Шах: {san}. Смотри, куда уходить.",
            f"{san} — шах. Осторожнее.",
        ]), "neutral"
    if tactics["is_capture"]:
        return _pick(state, [
            f"Он взял {captured_ru}: {san}.",
            f"Соперник забрал {captured_ru} ходом {san}.",
            f"{san} — взял твою {captured_ru}.",
        ]), "neutral"
    threats = describe_threats(board_after)
    threat_text = ""
    if threats:
        threat_text = f" Теперь угрожает: {', '.join(threats[:2])}."
    if state["mode"] == "opening":
        note = opening_move_note(move.uci())
        human = humanize_move(board_before, move)
        if note:
            return f"Соперник: {human}. {note}", "calm"
        return f"Соперник: {human}.", "calm"
    for name, targets in tactics["tactics"]:
        if name == "fork":
            return f"{san} — вилка! Атакует {', '.join(targets)}.", "neutral"
    human = humanize_move(board_before, move)
    return _pick(state, [
        f"Соперник: {human}.{threat_text}",
        f"Он сходил {human}.{threat_text}",
        f"Ход соперника: {human}.{threat_text}",
    ]), "neutral"


def _get_stockfish_plan(board_before, info, max_len=3):
    plan = []
    if info:
        pv = info[0].get("pv", [])
        temp = board_before.copy()
        for uci in pv[:max_len]:
            try:
                mv = safe_move(board_before, uci)
                if mv in temp.legal_moves:
                    plan.append(humanize_move(temp, mv))
                    temp.push(mv)
                else:
                    break
            except Exception:
                break
    return plan


def _format_plan(plan):
    if not plan:
        return ""
    return " Стокфиш видел план: " + ", ".join(plan) + "."


def _make_player_rules_comment(state, before_fen, move, think_time):
    board_before = chess.Board(before_fen)
    try:
        san = board_before.san(move)
    except Exception:
        san = move.uci()
    human = humanize_move(board_before, move)
    board_after = state["board"]
    prof = state.get("profanity", True)
    phase = game_phase(board_after)
    tactics_info = detect_tactics(board_before, move)
    piece_moved = tactics_info["piece_moved"]
    captured = tactics_info["captured"]
    captured_ru = piece_ru(captured) if captured else None
    mat_before = material_balance(board_before)
    mat_after = material_balance(board_after)
    info = engine_analyse(before_fen, 0.3)
    cp_before = 0
    loss_cp = 0
    is_best = False
    best_human = ""
    if info:
        best_uci = info[0]["best"]
        try:
            best_move = chess.Move.from_uci(best_uci)
            best_human = humanize_move(board_before, best_move)
        except Exception:
            best_human = ""
        cp_before = info[0]["cp"] or 0
        if move.uci() == best_uci:
            is_best = True
        else:
            after_info = engine_analyse(board_after.fen(), 0.25)
            if after_info:
                cp_after_opp = after_info[0]["cp"] or 0
                loss_cp = max(0, cp_before + cp_after_opp)
    scold = _time_scold(state, think_time)
    if tactics_info["is_mate"]:
        return _pick(state, [
            f"Мат! Ты сходил {human}. Игра окончена.",
            f"Поставил мат: {human}. Красиво.",
            f"Мат ходом {human}. Победа!",
        ]), "praise"
    if loss_cp > 300:
        hanging = find_hanging_piece(board_after, not board_after.turn)
        lost = piece_ru(hanging) if hanging else "фигура"
        if prof:
            base = _pick(state, [
                f"Зевок! {human} — и ты подставил {lost}.",
                f"Ты сходил {human} и потерял {lost}. Это грубая ошибка.",
                f"Бля, {human} — это потеря {lost}. Смотри на доску!",
            ])
        else:
            base = _pick(state, [
                f"Ошибка: {human}. Под ударом {lost}.",
                f"{human} — потеря {lost}.",
                f"Ты подставил {lost} ходом {human}.",
            ])
        if best_human:
            base += f" Лучше было: {best_human}."
        base += _format_plan(_get_stockfish_plan(board_before, info))
        return base + scold, "angry"
    if loss_cp > 150:
        base = _pick(state, [
            f"Серьёзная ошибка: {human}. Ты потерял преимущество.",
            f"Так нельзя: {human} даёт мне инициативу.",
            f"{human} — позиция ухудшилась.",
        ])
        if best_human:
            base += f" Сильнее было: {best_human}."
        base += _format_plan(_get_stockfish_plan(board_before, info))
        return base + scold, "angry"
    if loss_cp > 70:
        base = _pick(state, [
            f"Неточность: {human}.",
            f"{human} — можно было точнее.",
            f"Слабовато: {human}.",
        ])
        if best_human:
            base += f" Лучше: {best_human}."
        base += _format_plan(_get_stockfish_plan(board_before, info))
        return base + scold, "neutral"
    if is_best:
        for name, targets in tactics_info["tactics"]:
            if name == "fork":
                return f"Отличный ход: {human}. Это вилка на {', '.join(targets)}.", "praise"
        if tactics_info["is_check"]:
            return _pick(state, [
                f"Лучший ход: {human}, и с шахом.",
                f"{human} — точно в цель.",
            ]), "praise"
        _p = _format_plan(_get_stockfish_plan(board_before, info))
        return _pick(state, [
            f"Лучший ход: {human}. Так держать.{_p}",
            f"{human} — ты видишь позицию.{_p}",
        ]), "praise"
    for name, targets in tactics_info["tactics"]:
        if name == "fork":
            return f"{human} — вилка на {', '.join(targets)}.", "praise"
        if name == "pin":
            return f"{human} — связка на {', '.join(targets)}.", "praise"
    if tactics_info["is_check"]:
        return _pick(state, [
            f"Шах ходом {human}. Дави дальше.",
            f"{human} даёт шах — соперник занервничал.",
        ]), "neutral"
    if tactics_info["is_capture"]:
        if mat_after - mat_before > 0:
            return _pick(state, [
                f"{human}. Ты забрал {captured_ru} — хорошо.",
                f"Взятие {captured_ru}: {human}. Отлично.",
            ]), "praise"
        return f"{human}. Обмен {captured_ru}, продолжаем.", "neutral"
    if state["mode"] == "opening":
        note = opening_move_note(move.uci())
        if note:
            return f"{human}. {note}", "praise"
        if piece_moved and piece_moved.piece_type in (chess.KNIGHT, chess.BISHOP):
            return f"{human}. Ты выводишь фигуру — это правильно.", "calm"
        if (piece_moved and piece_moved.piece_type == chess.PAWN
                and move.to_square in (chess.E4, chess.D4, chess.E5, chess.D5)):
            return f"{human}. Захват центра — хорошо.", "calm"
    if phase == "endgame":
        if piece_moved and piece_moved.piece_type == chess.KING:
            return f"{human}. Король активизировался в эндшпиле — верно.", "calm"
    plan_human = []
    if info:
        pv = info[0].get("pv", [])
        temp_board = board_before.copy()
        for uci_move in pv[:3]:
            try:
                mv = chess.Move.from_uci(uci_move)
                if mv in temp_board.legal_moves:
                    plan_human.append(humanize_move(temp_board, mv))
                    temp_board.push(mv)
                else:
                    break
            except Exception:
                break
    plan_text = ""
    if plan_human:
        plan_text = " Стокфиш видит план: " + ", ".join(plan_human) + "."
    return _pick(state, [
        f"{human}. Нормальный ход.{plan_text}",
        f"Сходил {human}.{plan_text}",
        f"{human}. Неплохо.{plan_text}",
    ]), "neutral"


def safe_push(board, move):
    m = normalize_move(move)
    if m is None:
        raise ValueError("safe_push: получен None вместо хода")
    board.push(m)


def normalize_move(move):
    if move is None:
        return None
    p = getattr(move, "promotion", None)
    if p is None:
        return move
    if isinstance(p, bool):
        pass
    elif isinstance(p, int) and p in (1, 2, 3, 4, 5, 6):
        return move
    new_m = chess.Move(move.from_square, move.to_square, None)
    if getattr(new_m, "promotion", None) is False:
        obj = chess.Move.__new__(chess.Move)
        obj.from_square = move.from_square
        obj.to_square = move.to_square
        obj.promotion = None
        obj.drop = None
        new_m = obj
    return new_m


def safe_move(board, uci):
    if not uci:
        return None
    try:
        from_sq = chess.parse_square(uci[:2])
        to_sq = chess.parse_square(uci[2:4])
        promo = None
        if len(uci) >= 5:
            promo_char = uci[4].lower()
            if promo_char in ("q", "r", "b", "n"):
                promo = {"q": chess.QUEEN, "r": chess.ROOK,
                         "b": chess.BISHOP, "n": chess.KNIGHT}[promo_char]
        return chess.Move(from_sq, to_sq, promo)
    except Exception as e:
        print(f"[safe_move error] {e} for {uci}")
        return None


def get_clean_move(uci_str):
    if not uci_str or len(uci_str) < 4:
        return None
    try:
        from_sq = chess.parse_square(uci_str[:2])
        to_sq = chess.parse_square(uci_str[2:4])
        promo = None
        if len(uci_str) >= 5:
            c = uci_str[4].lower()
            if c in ("q", "r", "b", "n"):
                promo = {"q": chess.QUEEN, "r": chess.ROOK,
                         "b": chess.BISHOP, "n": chess.KNIGHT}[c]
        return chess.Move(from_sq, to_sq, promo)
    except Exception:
        return None


def humanize_move(board, move):
    try:
        piece = board.piece_at(move.from_square)
        if not piece:
            return move.uci()
        piece_names = {
            chess.PAWN: "пешка", chess.KNIGHT: "конь", chess.BISHOP: "слон",
            chess.ROOK: "ладья", chess.QUEEN: "ферзь", chess.KING: "король",
        }
        piece_name = piece_names.get(piece.piece_type, "фигура")
        from_sq = chess.square_name(move.from_square)
        to_sq = chess.square_name(move.to_square)
        result = f"{piece_name} с {from_sq} на {to_sq}"
        if board.is_capture(move):
            captured = board.piece_at(move.to_square)
            if captured:
                captured_name = piece_names.get(captured.piece_type, "фигура")
                result += f", берёт {captured_name}"
        promo = getattr(move, "promotion", None)
        if promo and promo is not False and isinstance(promo, int):
            promo_name = piece_names.get(promo, "фигура")
            result += f", превращается в {promo_name}"
        return result
    except Exception:
        return move.uci()


def _build_player_llm_ctx(state, before_fen, move, think_time):
    board_before = chess.Board(before_fen)
    try:
        san = board_before.san(move)
    except Exception:
        san = move.uci()
    test_board = board_before.copy()
    test_board.push(move)
    phase = game_phase(test_board)
    tactics_info = detect_tactics(board_before, move)
    mat_after = material_balance(test_board)
    info = engine_analyse(before_fen, 0.3)
    best_uci = None
    best_san = ""
    best_human = ""
    cp_before = 0
    loss_cp = 0
    is_best = False
    if info:
        best_uci = info[0]["best"]
        try:
            best_move_obj = safe_move(board_before, best_uci)
            best_san = board_before.san(best_move_obj)
            best_human = humanize_move(board_before, best_move_obj)
        except Exception:
            best_san = ""
            best_human = ""
        cp_before = info[0]["cp"] or 0
        if move.uci() == best_uci:
            is_best = True
        else:
            after_info = engine_analyse(test_board.fen(), 0.25)
            if after_info:
                cp_after_opp = after_info[0]["cp"] or 0
                loss_cp = max(0, cp_before + cp_after_opp)
    hanging = find_hanging_piece(test_board, not test_board.turn)
    threats = describe_threats(test_board)
    best_line_human = []
    if info:
        pv = info[0].get("pv", [])
        temp_board = board_before.copy()
        for uci_move in pv[:5]:
            try:
                mv = chess.Move.from_uci(uci_move)
                if mv in temp_board.legal_moves:
                    best_line_human.append(humanize_move(temp_board, mv))
                    temp_board.push(mv)
                else:
                    break
            except Exception:
                break
    return {
        "mode": state["mode"], "phase": phase,
        "time_control": state["time_control"], "think_time": think_time,
        "fen_after": test_board.fen(),
        "move_human": humanize_move(board_before, move),
        "move_san": san, "move_uci": move.uci(),
        "is_capture": tactics_info["is_capture"],
        "is_check": tactics_info["is_check"],
        "is_mate": tactics_info["is_mate"],
        "is_best": is_best, "loss_cp": loss_cp, "cp_before": cp_before,
        "best_san": best_san, "best_human": best_human,
        "best_line_human": best_line_human,
        "hanging_piece": piece_ru(hanging) if hanging else None,
        "move_color": "белые" if board_before.turn else "чёрные",
        "material_diff": mat_after,
        "tactics": tactics_info["tactics"], "threats": threats,
        "opening_name": (state.get("current_opening_name_ru")
                         or translate_opening_name(state.get("current_opening_name", ""))),
        "opening_idea": state.get("current_opening_idea") or "",
        "recent_comments": [m["text"] for m in state["coach_messages"][-15:]],
        "profanity": state.get("profanity", True),
    }


def apply_move_internal(state, move, is_bot=False, think_time=0, llm_result=None, fast_mode=False):
    board = state["board"]
    clean_move = get_clean_move(move.uci())
    if not clean_move:
        return {"error": "Нелегальный ход"}
    try:
        board.push(clean_move)
        board.pop()
    except Exception:
        return {"error": "Нелегальный ход"}
    move = clean_move
    before_fen = board.fen()
    is_capture = board.is_capture(move)
    san = board.san(move)
    board.push(move)
    after_fen = board.fen()
    mv = {
        "uci": move.uci(), "san": san,
        "human": humanize_move(chess.Board(before_fen), move),
        "from": move.uci()[:2], "to": move.uci()[2:4],
        "color": "black" if board.turn else "white",
        "is_bot": is_bot, "think_time": think_time,
        "fen_after": after_fen, "comment": "", "comment_source": "rules",
    }
    state["moves"].append(mv)
    state["fens"].append(after_fen)
    state["history_index"] = len(state["fens"]) - 1
    state["last_move"] = {
        "from": mv["from"], "to": mv["to"], "is_bot": is_bot, "san": san,
    }
    if state["mode"] == "opening":
        idx = state.get("opening_index", 0)
        book = state.get("opening_moves", [])
        if idx < len(book):
            if move.uci() == book[idx]:
                state["opening_index"] = idx + 1
            elif not is_bot:
                state["deviation"] = True
        if (state.get("opening_index", 0) >= len(book)
                and not state.get("deviation")
                and not state.get("opening_completed")):
            state["opening_completed"] = True
    if not is_bot and state["mode"] not in ("lesson", "puzzle"):
        st = state["stats"]
        st["moves"] += 1
        if is_capture:
            st["captures"] += 1
        if board.is_check():
            st["checks"] += 1
        if think_time:
            st["think_times"].append(think_time)
    is_vs_mode = state["mode"] in ("bot", "local")
    if is_vs_mode:
        if is_bot:
            comment, emotion, source = "", "neutral", "silent"
        elif llm_result:
            comment = llm_result["text"]
            emotion = llm_result.get("emotion", "neutral")
            source = "llm"
        else:
            comment, emotion, source = "", "neutral", "silent"
    else:
        if is_bot:
            try:
                comment, emotion = _make_bot_comment(state, chess.Board(before_fen), board, move, san)
            except Exception as e:
                print(f"[bot comment error] {e}")
                comment, emotion = "", "neutral"
            source = "rules"
        elif llm_result:
            comment = llm_result["text"]
            emotion = llm_result.get("emotion", "neutral")
            source = "llm"
        else:
            try:
                comment, emotion = _make_player_rules_comment(state, before_fen, move, think_time)
            except Exception as e:
                print(f"[player comment error] {e}")
                comment, emotion = "", "neutral"
            source = "rules"
    if not is_bot and state["mode"] not in ("lesson", "puzzle") and not fast_mode:
        info_b = engine_analyse(before_fen, 0.1)
        if info_b:
            info_a = engine_analyse(after_fen, 0.1)
            if info_a:
                cp_b = info_b[0]["cp"] or 0
                cp_a = info_a[0]["cp"] or 0
                if cp_b + cp_a > 300:
                    state["stats"]["blunders"] += 1
    mv["comment"] = comment
    mv["comment_source"] = source
    mv["emotion"] = emotion
    if comment:
        state["coach_messages"].append({
            "text": comment, "emotion": emotion, "source": source,
            "is_bot": is_bot, "time": time.time(),
        })
    if board.is_checkmate():
        state["last_event"] = {"type": "mate"}
    elif state["mode"] == "opening" and state.get("opening_completed"):
        state["last_event"] = {"type": "opening_completed"}
        if not any("Дебют «" in m.get("text", "") and "выполнен" in m.get("text", "")
                   for m in state["coach_messages"][-2:]):
            op_ru = state.get("opening_name_ru") or state.get("opening_name", "")
            state["coach_messages"].append({
                "text": f"✅ Дебют «{op_ru}» выполнен! Отлично!",
                "emotion": "praise", "source": "system",
                "is_bot": False, "time": time.time(),
            })
    elif board.is_check():
        state["last_event"] = {"type": "check"}
    elif is_capture:
        state["last_event"] = {"type": "capture"}
    else:
        state["last_event"] = {"type": "move"}
    if state["mode"] == "opening":
        idx = state.get("opening_index", 0)
        book = state.get("opening_moves", [])
        state["hint_uci"] = book[idx] if idx < len(book) and not state.get("deviation") else None
    if state["mode"] in ("bot", "coach", "blitz"):
        uci_seq = [m["uci"] for m in state["moves"]]
        opening = identify_opening_from_moves(uci_seq)
        if opening:
            state["current_opening_name"] = opening["name"]
            state["current_opening_name_ru"] = translate_opening_name(opening["name"])
            state["current_opening_idea"] = opening_idea(opening["name"])
    state["version"] = state.get("version", 0) + 1
    maybe_save_profile(state)
    return mv


def make_bot_move(state):
    if state.get("timed_out"):
        return
    if state["mode"] == "opening" and state.get("opening_completed"):
        return
    board = state["board"]
    if board.is_game_over():
        return
    if state["mode"] == "opening" and not state.get("deviation"):
        idx = state.get("opening_index", 0)
        book = state.get("opening_moves", [])
        if idx < len(book):
            book_move = chess.Move.from_uci(book[idx])
            if book_move in board.legal_moves:
                apply_move_internal(state, book_move, is_bot=True)
                return
            else:
                return
        else:
            return
    uci = safe_engine_play(board.fen(), state["level"])
    if not uci:
        return
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        moves = list(board.legal_moves)
        if not moves:
            return
        move = random.choice(moves)
    apply_move_internal(state, move, is_bot=True)


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True, "stockfish": STOCKFISH_PATH,
        "engine": get_engine() is not None,
        "openings_cache": str(OPENINGS_CACHE),
        "profile": str(PROFILE_FILE),
        "llm_available": llm_available(),
        "llm_model": LLM_CONFIG.get("model") if llm_available() else None,
    })


@app.route("/api/llm_status")
def api_llm_status():
    return jsonify({
        "available": llm_available(),
        "model": LLM_CONFIG.get("model") if llm_available() else None,
        "url_configured": bool(LLM_CONFIG.get("url")),
        "hint": "",
    })


@app.route("/api/new_game", methods=["POST"])
def api_new_game():
    try:
        data = request.get_json(force=True) or {}
        with GAME_LOCK:
            state = make_new_state(
                mode=data.get("mode", "bot"),
                player_color=data.get("player_color", "white"),
                level=data.get("level", 5),
                time_control=data.get("time_control", 0),
                lesson_id=data.get("lesson_id"),
                puzzle_type=data.get("puzzle_type"),
                opening_name=data.get("opening_name"),
                profanity=data.get("profanity", True),
            )
            GAMES[state["id"]] = state
            return jsonify(public_state(state))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/move", methods=["POST"])
def api_move():
    try:
        data = request.get_json(force=True) or {}
        gid = data.get("game_id")
        promo = data.get("promotion") or ""
        uci = f"{data.get('from', '')}{data.get('to', '')}{promo}"
        think_time = float(data.get("think_time") or 0)
        llm_ctx = None
        move = None
        version_at_start = None
        with GAME_LOCK:
            state = GAMES.get(gid)
            if not state:
                return jsonify({"error": "Игра не найдена"}), 404
            if state.get("timed_out"):
                return jsonify({"error": "Время вышло"}), 400
            if state.get("opening_completed"):
                return jsonify({"error": "Дебют уже выполнен"}), 400
            board = state["board"]
            if state["mode"] in ("lesson", "puzzle"):
                return _handle_lesson_puzzle_move(state, uci, think_time)
            if state["mode"] in ("bot", "opening", "blitz", "coach"):
                player_is_white = state["player_color"] == "white"
                if board.turn != player_is_white:
                    return jsonify({"error": "Не ваш ход"}), 400
            try:
                move = chess.Move.from_uci(uci)
            except Exception:
                return jsonify({"error": "Нелегальный ход"}), 400
            if move not in board.legal_moves:
                return jsonify({"error": "Нелегальный ход"}), 400
            version_at_start = state.get("version", 0)
            use_llm = data.get("use_llm", True)
            if use_llm and llm_available() and state["mode"] in ("coach", "bot", "blitz", "local"):
                llm_ctx = _build_player_llm_ctx(state, board.fen(), move, think_time)
        llm_result = None
        if llm_ctx:
            _mode = "bot"
            with GAME_LOCK:
                _g = GAMES.get(gid)
                if _g:
                    _mode = _g["mode"]
            if _mode == "coach":
                _prompt = COACH_SYSTEM_PROMPT
            elif _mode == "blitz":
                _prompt = SPEED_COACH_SYSTEM_PROMPT
            elif _mode == "local":
                _prompt = LOCAL_SYSTEM_PROMPT
            else:
                _prompt = OPPONENT_SYSTEM_PROMPT
            _temp = 1.05 if _mode in ("bot", "local") else 0.85
            llm_result = llm_generate_comment(llm_ctx, system_prompt=_prompt, temperature=_temp)
        with GAME_LOCK:
            state = GAMES.get(gid)
            if not state:
                return jsonify({"error": "Игра не найдена"}), 404
            if state.get("version", 0) != version_at_start:
                return jsonify({"error": "Позиция изменилась"}), 409
            if move not in state["board"].legal_moves:
                return jsonify({"error": "Нелегальный ход"}), 400
            apply_move_internal(
                state, move, is_bot=False, think_time=think_time,
                llm_result=llm_result, fast_mode=not use_llm
            )
            if llm_ctx:
                _remember_llm_ok(gid, llm_result is not None)
            board = state["board"]
            if (not board.is_game_over()
                    and not state.get("opening_completed")
                    and state["mode"] in ("bot", "opening", "blitz", "coach")):
                player_is_white = state["player_color"] == "white"
                if board.turn != player_is_white:
                    make_bot_move(state)
            return jsonify(public_state(state))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Ошибка сервера: {str(e)}"}), 500


def _handle_lesson_puzzle_move(state, uci, think_time):
    try:
        board = state["board"]
        if state.get("solved"):
            return jsonify(public_state(state))
        sol = (state.get("solution", []) if state["mode"] == "lesson"
               else state.get("puzzle_solution", []))
        idx = (state.get("solution_index", 0) if state["mode"] == "lesson"
               else state.get("puzzle_index", 0))
        if not sol or idx >= len(sol):
            return jsonify(public_state(state))
        expected = sol[idx]
        if uci != expected:
            hint_san = ""
            try:
                hint_move = get_clean_move(expected)
                if hint_move:
                    hint_san = board.san(hint_move)
            except Exception:
                hint_san = expected
            msg = f"Не то. " + (f"Подсказка: {hint_san}." if hint_san else "")
            state["coach_messages"].append({
                "text": msg, "emotion": "neutral",
                "source": "system", "is_bot": False, "time": time.time(),
            })
            state["last_event"] = {"type": "wrong"}
            return jsonify(public_state(state))
        move = get_clean_move(uci)
        if not move:
            return jsonify({"error": "Нелегальный ход"}), 400
        res_internal = apply_move_internal(state, move, is_bot=False, think_time=think_time)
        if isinstance(res_internal, dict) and res_internal.get("error"):
            return jsonify(res_internal), 400
        if state["mode"] == "lesson":
            state["solution_index"] = idx + 1
            state["solved"] = True
            state["last_event"] = {"type": "solved"}
            state["coach_messages"].append({
                "text": "Правильно! Урок пройден.", "emotion": "praise",
                "source": "system", "is_bot": False, "time": time.time(),
            })
            return jsonify(public_state(state))
        state["puzzle_index"] = idx + 1
        if state["puzzle_index"] < len(sol):
            opp_uci = sol[state["puzzle_index"]]
            opp_move = get_clean_move(opp_uci)
            if opp_move:
                r = apply_move_internal(state, opp_move, is_bot=True)
                if isinstance(r, dict) and r.get("error"):
                    print(f"[puzzle opp error] {r}")
                else:
                    state["puzzle_index"] += 1
        if state["puzzle_index"] >= len(sol):
            state["solved"] = True
            state["last_event"] = {"type": "solved"}
            start_fen = state.get("puzzle_start_fen", board.fen())
            SOLVED_PUZZLES.add(_fen_key(start_fen))
            save_solved_puzzles()
            state["coach_messages"].append({
                "text": "Мат найден! Задача решена.", "emotion": "praise",
                "source": "system", "is_bot": False, "time": time.time(),
            })
        return jsonify(public_state(state))
    except Exception as e:
        print(f"[puzzle error] {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/undo", methods=["POST"])
def api_undo():
    try:
        data = request.get_json(force=True) or {}
        gid = data.get("game_id")
        with GAME_LOCK:
            state = GAMES.get(gid)
            if not state:
                return jsonify({"error": "Игра не найдена"}), 404
            board = state["board"]
            n = 2 if state["mode"] in ("bot", "opening", "blitz", "coach") else 1
            for _ in range(n):
                if state["moves"]:
                    try:
                        board.pop()
                    except Exception:
                        pass
                    state["moves"].pop()
                    if len(state["fens"]) > 1:
                        state["fens"].pop()
            state["history_index"] = len(state["fens"]) - 1
            state["last_move"] = None
            if state["moves"]:
                last = state["moves"][-1]
                state["last_move"] = {
                    "from": last["from"], "to": last["to"],
                    "is_bot": last.get("is_bot", False), "san": last.get("san", ""),
                }
            state["solved"] = False
            state["last_event"] = {"type": "undo"}
            state["timed_out"] = False
            state["result_override"] = None
            state["opening_announced"] = False
            state["opening_completed"] = False
            if state["mode"] == "opening":
                book = state.get("opening_moves", [])
                idx = 0
                for m in state["moves"]:
                    if idx < len(book) and m["uci"] == book[idx]:
                        idx += 1
                    else:
                        break
                state["opening_index"] = idx
                state["deviation"] = False
                state["deviation_announced"] = False
                state["hint_uci"] = book[idx] if idx < len(book) else None
            state["version"] = state.get("version", 0) + 1
            return jsonify(public_state(state))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/hint", methods=["POST"])
def api_hint():
    try:
        data = request.get_json(force=True) or {}
        gid = data.get("game_id")
        with GAME_LOCK:
            state = GAMES.get(gid)
            if not state:
                return jsonify({"error": "Игра не найдена"}), 404
            if state["mode"] == "puzzle":
                return jsonify({"error": "В режиме мата нет подсказок"}), 400
            if state.get("opening_completed"):
                return jsonify({"error": "Дебют уже выполнен"}), 400
            board = state["board"]
            hint = None
            hint_why = ""
            if state["mode"] == "opening":
                idx = state.get("opening_index", 0)
                book = state.get("opening_moves", [])
                if idx < len(book):
                    hint = book[idx]
                    hint_why = opening_move_note(hint) or "Это книжный ход в этом дебюте."
            if not hint:
                info = engine_analyse(board.fen(), 0.3)
                if info:
                    hint = info[0]["best"]
                    try:
                        mv = chess.Move.from_uci(hint)
                        tac = detect_tactics(board, mv)
                        if tac["is_mate"]:
                            hint_why = "Это ведёт к мату."
                        elif tac["tactics"]:
                            for name, targets in tac["tactics"]:
                                if name == "fork":
                                    hint_why = f"Вилка на {', '.join(targets)}."
                                    break
                                if name == "pin":
                                    hint_why = f"Связка на {', '.join(targets)}."
                                    break
                        elif tac["is_check"]:
                            hint_why = "С шахом."
                    except Exception:
                        pass
            if not hint:
                moves = list(board.legal_moves)
                hint = random.choice(moves).uci() if moves else None
            hint_san = ""
            if hint:
                try:
                    hint_san = board.san(chess.Move.from_uci(hint))
                except Exception:
                    hint_san = hint
            state["hint_uci"] = hint
            msg = (f"Подсказка: {hint_san or hint}." + (f" {hint_why}" if hint_why else "")
                   if hint else "Подсказок нет.")
            state["coach_messages"].append({
                "text": msg, "emotion": "calm",
                "source": "system", "is_bot": False, "time": time.time(),
            })
            return jsonify(public_state(state))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/timeout", methods=["POST"])
def api_timeout():
    try:
        data = request.get_json(force=True) or {}
        gid = data.get("game_id")
        with GAME_LOCK:
            state = GAMES.get(gid)
            if not state:
                return jsonify({"error": "Игра не найдена"}), 404
            if state.get("timed_out"):
                return jsonify(public_state(state))
            if state["mode"] in ("lesson", "puzzle"):
                return jsonify({"error": "В этом режиме нет таймера"}), 400
            if state.get("opening_completed"):
                return jsonify({"error": "Дебют уже выполнен"}), 400
            loser = data.get("loser")
            if loser not in ("white", "black"):
                loser = "white" if state["board"].turn else "black"
            state["timed_out"] = True
            state["result_override"] = "0-1" if loser == "white" else "1-0"
            msg = ("Время вышло! " +
                   ("Ты проиграл по времени. Соберись, в следующий раз успеешь."
                    if loser == state["player_color"]
                    else "Соперник проиграл по времени. Хорошо."))
            state["coach_messages"].append({
                "text": msg,
                "emotion": "angry" if loser == state["player_color"] else "praise",
                "source": "system", "is_bot": False, "time": time.time(),
            })
            state["last_event"] = {"type": "timeout"}
            if state["mode"] in ("bot", "opening", "blitz", "coach") and not state.get("profile_saved"):
                p = load_profile()
                p["games"] += 1
                if loser == state["player_color"]:
                    p["losses"] += 1
                else:
                    p["wins"] += 1
                st = state["stats"]
                p["moves"] += st.get("moves", 0)
                p["blunders"] += st.get("blunders", 0)
                p["captures"] += st.get("captures", 0)
                p["checks"] += st.get("checks", 0)
                p["history"].append({
                    "time": time.strftime("%Y-%m-%d %H:%M"),
                    "mode": state["mode"], "result": state["result_override"],
                    "moves": st.get("moves", 0), "blunders": st.get("blunders", 0),
                    "captures": st.get("captures", 0), "checks": st.get("checks", 0),
                })
                p["history"] = p["history"][-50:]
                save_profile(p)
                state["profile_saved"] = True
            state["version"] = state.get("version", 0) + 1
            return jsonify(public_state(state))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile")
def api_profile():
    p = load_profile()
    return jsonify({"profile": p, "autoportrait": make_autoportrait(p)})


@app.route("/api/openings")
def api_openings():
    try:
        ops = ensure_openings()
        return jsonify({
            "openings": [
                {
                    "name": o["name"],
                    "name_ru": translate_opening_name(o["name"]),
                    "eco": o["eco"],
                    "length": len(o["moves"]),
                    "idea": opening_idea(o["name"]),
                }
                for o in ops
            ]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lessons")
def api_lessons():
    return jsonify({
        "lessons": [
            {"id": l["id"], "title": l["title"],
             "description": l["description"], "task": l["task"]}
            for l in LESSONS
        ]
    })


@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({"error": str(e)}), 500


HTML_PAGE = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Шахматный тренер</title>
<style>
:root{
  --bg:#312e2b; --panel:#272522; --panel2:#3a3733;
  --light:#eeeed2; --dark:#769656;
  --accent:#81b64c; --text:#f0f0f0; --gold:#f5c542;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;overflow:hidden}
button,select,input{font:inherit}
#app{height:100dvh;display:flex;flex-direction:column}
#topbar{flex-shrink:0;display:flex;align-items:center;gap:8px;
  padding:8px 10px;background:#1f1d1b;border-bottom:1px solid #111}
#topbar button{background:var(--panel2);color:var(--text);
  border:1px solid #555;border-radius:8px;padding:8px 10px}
#topbar #title{flex:1;text-align:center;font-weight:700;color:#ddd}
#topbar #llmBadge{font-size:11px;padding:3px 7px;border-radius:6px;
  background:#2c4a2c;color:#b7e3b7;display:none}
#topbar #llmBadge.off{background:#4a2c2c;color:#e3b7b7}
#main{flex:1;min-height:0;position:relative}
.screen{height:100%;overflow:auto;padding:14px}
#screenGame{padding:8px;overflow:hidden}
h1,h2,h3{margin:0 0 12px}
.menu-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px;max-width:760px;margin:0 auto}
.menu-btn{background:linear-gradient(180deg,#4a4642,#33302c);color:#fff;
  border:1px solid #5c5751;border-bottom-width:4px;border-radius:12px;
  padding:18px 12px;font-weight:700;font-size:16px;text-align:left;cursor:pointer;
  box-shadow:0 3px 0 #151413}
.menu-btn:active{transform:translateY(2px);box-shadow:0 1px 0 #151413}
.menu-btn small{display:block;font-weight:400;color:#cfcfcf;margin-top:5px;font-size:12px}
.game-layout{height:100%;display:flex;gap:10px;min-height:0}
.board-wrap{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;align-items:stretch}
#clocks{display:none;flex-direction:column;gap:4px;margin-bottom:6px}
.clock{background:#222;border:1px solid #555;border-radius:8px;
  padding:5px 10px;font-weight:700;font-variant-numeric:tabular-nums;color:#ccc}
.clock.active{background:#f5c542;color:#111;border-color:#fff2a8}
#viewBanner{display:none;align-items:center;gap:8px;justify-content:space-between;
  background:#4a3f1c;border:1px solid #a8891c;border-radius:8px;
  padding:6px 10px;margin-bottom:6px;font-size:13px}
#viewBanner button{background:#a8891c;color:#fff;border:none;border-radius:6px;
  padding:4px 10px;cursor:pointer;font-size:12px}
#board{width:100%;max-width:min(100vw - 24px, 72vh);aspect-ratio:1/1;display:grid;
  grid-template-columns:repeat(8,1fr);grid-template-rows:repeat(8,1fr);
  margin:0 auto;border-radius:4px;overflow:hidden;
  box-shadow:0 8px 24px rgba(0,0,0,.5);flex-shrink:1;position:relative}
.square{position:relative;display:flex;align-items:center;justify-content:center;
  user-select:none;-webkit-tap-highlight-color:transparent}
.square.light{background:var(--light)}
.square.dark{background:var(--dark)}
.square.last-move::before{content:"";position:absolute;inset:0;
  background:rgba(246,246,105,.55);z-index:0}
.square.last-move.last-move-opp::before{background:rgba(255,140,50,.65)}
.square.hint::after{content:"";position:absolute;inset:2px;
  border:3px solid var(--gold);border-radius:4px;z-index:9;
  animation:pulseHint 1s infinite alternate;pointer-events:none}
@keyframes pulseHint{from{opacity:.45}to{opacity:1}}
.coord{position:absolute;font-size:10px;font-weight:700;line-height:1;
  z-index:2;pointer-events:none;text-shadow:0 0 2px rgba(0,0,0,.25)}
.coord-file{bottom:1px;right:3px}
.coord-rank{top:1px;left:3px}
.square.light .coord{color:#4a6f2a}
.square.dark .coord{color:#eeeed2}
.piece{position:relative;z-index:1;pointer-events:none;
  font-size:clamp(22px,7.6vmin,64px);line-height:1;
  font-family:"DejaVu Sans","Noto Sans Symbols2",Arial,sans-serif}
.piece.white{color:#fff;
  text-shadow:1px 1px 0 #000,-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000}
.piece.black{color:#000;
  text-shadow:1px 1px 0 #fff,-1px -1px 0 #fff,1px -1px 0 #fff,-1px 1px 0 #fff}
.dot{position:absolute;width:28%;height:28%;border-radius:50%;
  background:rgba(0,0,0,.35);z-index:10;pointer-events:none}
.capture-ring{position:absolute;inset:5%;border:4px solid rgba(0,0,0,.38);
  border-radius:50%;z-index:10;pointer-events:none}
#gameStatus{min-height:32px;padding:6px 8px;text-align:center;color:#ddd;font-size:14px}
#gameStatus.completed{color:#f5c542;font-weight:700}
#openingBar{display:none;padding:4px 8px;text-align:center;
  color:#cfcfcf;font-size:12px;background:#3a3a2e;border-radius:6px;margin:0 0 6px}
#gameControls{flex-shrink:0;display:flex;flex-wrap:wrap;gap:6px;
  justify-content:center;padding:4px 0}
#gameControls button,.panel-block button,.modal button,.list-btn{
  background:linear-gradient(180deg,#4a4642,#33302c);color:#fff;
  border:1px solid #5c5751;border-bottom-width:3px;border-radius:8px;
  padding:8px 10px;cursor:pointer}
#gameControls button:active,.panel-block button:active,
.modal button:active,.list-btn:active{transform:translateY(1px)}
#gameControls button:disabled,.panel-block button:disabled{opacity:.45}
#openingsSearch{
  width:100%;max-width:760px;margin:0 auto 10px;display:block;
  background:#1e1c1a;color:var(--text);padding:10px 12px;
  border:1px solid #555;border-radius:8px;font-size:15px;
}
#openingsSearch:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 2px rgba(129,182,76,.25)}
#openingsSearch::placeholder{color:#888}
#openingsSearch::-webkit-search-cancel-button{filter:invert(1);opacity:.6}
.side-panel{width:400px;min-width:300px;display:flex;flex-direction:column;gap:8px;height:100%;min-height:0;overflow:hidden}
.panel-block{background:var(--panel);border:1px solid #444;border-radius:10px;padding:8px;display:flex;flex-direction:column;overflow:hidden}
.notation-block{flex:0 1 auto;min-height:100px;max-height:30vh;display:flex;flex-direction:column}
.notation{overflow-y:auto;flex:1 1 auto;min-height:0;background:#1e1c1a;border-radius:6px;padding:4px}
.chat-block{flex:1 1 0;min-height:0;display:flex;flex-direction:column}
.coach-chat{overflow-y:auto;flex:1 1 0;min-height:0;background:#1e1c1a;border-radius:6px;padding:8px;display:flex;flex-direction:column;gap:8px}
.chat-msg{padding:12px 14px;border-radius:8px;background:#3a3733;color:#eee;font-size:15px;line-height:1.45;position:relative;word-wrap:break-word;white-space:pre-wrap;overflow-wrap:break-word}
.chat-msg.bot{background:#2f3d27}
.chat-msg.angry{background:#5a2a2a}
.chat-msg.praise{background:#2c4a2c}
.chat-msg.calm{background:#2a364a}
.chat-msg::after{content:attr(data-source);position:absolute;top:2px;right:4px;
  font-size:9px;opacity:.5;text-transform:uppercase}
.empty{color:#888;padding:8px;font-size:13px}
.list{display:flex;flex-direction:column;gap:8px;max-width:760px;margin:0 auto}
.list-btn{text-align:left;display:flex;flex-direction:column;gap:3px}
.list-btn small{color:#bbb;font-weight:400}
.list-btn.locked{opacity:.45;pointer-events:none}
.list-btn .idea{color:#8ba968;font-size:12px}
.stats-box{max-width:760px;margin:0 auto;background:var(--panel);
  border:1px solid #444;border-radius:12px;padding:16px;line-height:1.55;white-space:pre-wrap}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.65);
  display:flex;align-items:center;justify-content:center;padding:14px;z-index:100}
.modal{background:#2b2926;border:1px solid #666;border-radius:14px;
  padding:18px;width:min(420px,100%);box-shadow:0 20px 60px rgba(0,0,0,.6)}
.modal label{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:10px 0}
.modal select,.modal input[type=range]{max-width:210px}
.modal .promo-buttons{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:10px}
.modal .modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.settings-row{display:flex;justify-content:space-between;gap:10px;margin:10px 0}
@media (max-width:800px){
  .game-layout{display:flex;flex-direction:column;height:100%;overflow:hidden}
  .board-wrap{flex:0 0 auto;max-height:45vh}
  .side-panel{width:100%;min-width:0;flex:1 1 0;min-height:0;overflow:hidden}
  #board{max-width:100%;max-height:100%}
  .notation-block{flex:0 1 auto;max-height:18vh;min-height:80px}
  .chat-block{flex:1 1 0;min-height:0}
  #screenGame{padding:4px;display:flex;flex-direction:column;height:100%}
}
</style>
</head>
<body>
<div id="app">
  <header id="topbar">
    <button id="btnMenu">☰ Меню</button>
    <span id="title">Шахматный тренер</span>
    <span id="llmBadge">🧠 ИИ</span>
    <button id="btnSettings">⚙️</button>
  </header>
  <main id="main">
    <section id="screenMenu" class="screen">
      <h1>Шахматы с тренером</h1>
      <div class="menu-grid">
        <button class="menu-btn" data-action="bot">Игра с ботом<small>Цвет и сложность</small></button>
        <button class="menu-btn" data-action="local">Игра на двоих<small>Один телефон</small></button>
        <button class="menu-btn" data-action="coach">Учиться с тренером<small>Главный режим</small></button>
        <button class="menu-btn" data-action="openings">Учить дебюты<small>База Lichess</small></button>
        <button class="menu-btn" data-action="mate1">Мат в 1 ход<small>Задачи</small></button>
        <button class="menu-btn" data-action="mateN">Мат в несколько ходов<small>Форсированные маты</small></button>
        <button class="menu-btn" data-action="speed">Обучение скоростям<small>Пуля, блиц, рапид</small></button>
        <button class="menu-btn" data-action="stats">Мой автопортрет<small>Статистика</small></button>
        <button class="menu-btn" data-action="settings">⚙️ Настройки<small>Голос, звуки, мат</small></button>
      </div>
    </section>
    <section id="screenGame" class="screen" style="display:none">
      <div class="game-layout">
        <div class="board-wrap">
          <div id="clocks">
            <div id="clockTop" class="clock">--:--</div>
            <div id="clockBottom" class="clock">--:--</div>
          </div>
          <div id="openingBar"></div>
          <div id="viewBanner">
            <span id="viewBannerText">Просмотр</span>
            <button id="btnExitView">К игре</button>
          </div>
          <div id="board"></div>
          <div id="gameStatus"></div>
          <div id="gameControls">
            <button id="btnUndo">Отмена хода</button>
            <button id="btnHint">Подсказка</button>
            <button id="btnNextPuzzle" style="display:none">Следующая задача</button>
            <button id="btnNextLesson" style="display:none">Следующий урок</button>
            <button id="btnGameMenu">В меню</button>
          </div>
        </div>
        <aside class="side-panel">
          <div class="panel-block notation-block">
            <div class="panel-title">Нотация</div>
            <div id="notation" class="notation"></div>
            <div class="view-controls">
              <button id="btnPrev">◀</button>
              <button id="btnNext">▶</button>
              <button id="btnLive">К текущей</button>
            </div>
          </div>
          <div class="panel-block chat-block">
            <div class="panel-title">Тренер</div>
            <div id="coachChat" class="coach-chat"></div>
          </div>
        </aside>
      </div>
    </section>
    <section id="screenOpenings" class="screen" style="display:none">
      <h2>Дебюты</h2>
      <input id="openingsSearch" type="search" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" placeholder="Поиск дебюта...">
      <div id="openingsList" class="list"></div>
    </section>
    <section id="screenLessons" class="screen" style="display:none">
      <h2>Уроки</h2>
      <div id="lessonsList" class="list"></div>
    </section>
    <section id="screenStats" class="screen" style="display:none">
      <h2>Мой автопортрет</h2>
      <div id="statsText" class="stats-box">Загрузка...</div>
    </section>
  </main>
</div>
<div id="modalRoot" style="display:none"></div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let S = {
  game: null, viewPly: null, selected: null, legalMoves: [],
  settings: Object.assign({
    voice: true, sounds: true, profanity: true, voiceName: "", useLlm: true
  }, JSON.parse(localStorage.getItem("chessSettings") || "{}")),
  clocks: null, lastMoveAt: 0, nagTurn: null,
  timeoutSent: false, currentScreen: "menu", llmActive: false
};

const PIECES = {p:"♟", n:"♞", b:"♝", r:"♜", q:"♛", k:"♚"};

function saveSettings(){ localStorage.setItem("chessSettings", JSON.stringify(S.settings)); }

async function api(path, data){
  const opts = {method: data ? "POST" : "GET", headers: {}};
  if(data){ opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(data); }
  const res = await fetch(path, opts);
  let j;
  try{ j = await res.json(); }catch(e){ throw new Error("Сервер вернул не JSON"); }
  if(j.error) throw new Error(j.error);
  return j;
}

function showScreen(name){
  $$(".screen").forEach(e => e.style.display = "none");
  const el = $("#screen" + name.charAt(0).toUpperCase() + name.slice(1));
  if(el) el.style.display = "block";
  S.currentScreen = name;
}

function goMenu(){
  S.game = null; S.viewPly = null; S.selected = null; S.clocks = null;
  S.timeoutSent = false;
  showScreen("menu");
  $("#title").textContent = "Шахматный тренер";
}

function modal(html){
  const root = $("#modalRoot");
  root.innerHTML = `<div class="modal-backdrop"><div class="modal">${html}</div></div>`;
  root.style.display = "block";
}
function closeModal(){
  const root = $("#modalRoot");
  root.style.display = "none"; root.innerHTML = "";
}

function isViewing(){ return S.viewPly !== null; }
function getViewIndex(){
  if(!S.game) return 0;
  if(S.viewPly === null) return S.game.fens.length - 1;
  return Math.max(0, Math.min(S.viewPly, S.game.fens.length - 1));
}
function enterView(plyIndex){
  if(!S.game) return;
  const max = S.game.fens.length - 1;
  S.viewPly = plyIndex >= max ? null : Math.max(0, plyIndex);
  S.selected = null;
  renderAll();
}
function exitView(){ S.viewPly = null; S.selected = null; renderAll(); }

function fenBoard(fen){
  const part = fen.split(" ")[0]; const rows = part.split("/");
  const arr = {};
  for(let r=0; r<8; r++){
    let c = 0;
    for(const ch of rows[r]){
      if(/\d/.test(ch)){ c += parseInt(ch, 10); }
      else{
        arr["abcdefgh"[c] + (8 - r)] = { type: ch.toLowerCase(), color: ch === ch.toUpperCase() ? "white" : "black" };
        c++;
      }
    }
  }
  return arr;
}

function fmtTime(sec){
  sec = Math.max(0, Math.ceil(sec));
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
}

let audioCtx = null;
function playSound(type){
  if(!S.settings.sounds) return;
  try{
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(); const g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    const freq = type === "capture" ? 110 : type === "check" ? 440 : type === "mate" ? 660 : 220;
    o.frequency.value = freq;
    g.gain.setValueAtTime(0.18, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.16);
    o.start(); o.stop(audioCtx.currentTime + 0.17);
  }catch(e){}
}

let voices = [];
function loadVoices(){
  try{ voices = speechSynthesis.getVoices(); }catch(e){ voices = []; }
  const sel = $("#setVoice");
  if(sel){
    const ru = voices.filter(v => (v.lang || "").toLowerCase().startsWith("ru"));
    sel.innerHTML = `<option value="">Авто (мужской русский)</option>` +
      ru.map(v => `<option value="${v.name}">${v.name}</option>`).join("");
    sel.value = S.settings.voiceName || "";
  }
}
try{ speechSynthesis.onvoiceschanged = loadVoices; setTimeout(() => { try { speechSynthesis.getVoices(); } catch(e){} }, 1000); }catch(e){}
setTimeout(loadVoices, 500);

function pickVoice(){
  let v = voices.find(v => v.name === S.settings.voiceName);
  if(!v){
    v = voices.find(v => /pavel|yuri|dmitry|artem|maxim|male|russian/i.test(v.name)) ||
        voices.find(v => (v.lang || "").toLowerCase().startsWith("ru"));
  }
  return v || null;
}

function speakOne(text, emotion){
  const em = emotion || "neutral";
  let pitch = 0.35, rate = 0.85;
  if(em === "angry"){ pitch = 0.1; rate = 1.05; }
  else if(em === "praise"){ pitch = 0.5; rate = 0.7; }
  else if(em === "calm"){ pitch = 0.3; rate = 0.8; }
  const v = pickVoice();
  const sentences = (text.match(/[^.!?…]+[.!?…]*/g) || [text]).map(s => s.trim()).filter(s => s.length > 0);
  if(sentences.length <= 1){
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "ru-RU"; u.pitch = pitch; u.rate = rate;
    if(v) u.voice = v;
    speechSynthesis.speak(u);
    return;
  }
  let i = 0;
  function speakNext(){
    if(i >= sentences.length) return;
    const sentence = sentences[i];
    i++;
    const u = new SpeechSynthesisUtterance(sentence);
    u.lang = "ru-RU";
    if(i === sentences.length){
      u.pitch = Math.max(0.1, pitch - 0.05);
      u.rate = Math.max(0.6, rate - 0.05);
    } else {
      u.pitch = pitch;
      u.rate = Math.min(1.2, rate + 0.05);
    }
    if(v) u.voice = v;
    u.onend = () => setTimeout(speakNext, 250);
    speechSynthesis.speak(u);
  }
  speakNext();
}

function speakBatch(messages){
  if(!S.settings.voice || !messages || !messages.length) return;
  try{
    speechSynthesis.cancel();
    const toSpeak = messages.slice(-3);
    for(const m of toSpeak){ if(m.text) speakOne(m.text, m.emotion); }
  }catch(e){}
}

function initClocks(){
  const g = S.game;
  if(!g || !g.time_control || g.opening_completed){ S.clocks = null; return; }
  if(!S.clocks){ S.clocks = {white: g.time_control, black: g.time_control, last: Date.now()}; }
  renderClocks();
}

function renderClocks(){
  const g = S.game; const wrap = $("#clocks");
  if(!g || !g.time_control || g.opening_completed){ wrap.style.display = "none"; return; }
  wrap.style.display = "flex";
  const topColor = g.player_color === "white" ? "black" : "white";
  const bottomColor = g.player_color;
  const topTime = S.clocks ? S.clocks[topColor] : g.time_control;
  const bottomTime = S.clocks ? S.clocks[bottomColor] : g.time_control;
  $("#clockTop").textContent = fmtTime(topTime);
  $("#clockTop").className = "clock " + (g.turn === topColor ? "active" : "");
  $("#clockBottom").textContent = fmtTime(bottomTime);
  $("#clockBottom").className = "clock " + (g.turn === bottomColor ? "active" : "");
}

function renderBoard(){
  const g = S.game;
  if(!g) return;
  const idx = getViewIndex();
  const fen = g.fens[idx] || g.fen;
  const pieces = fenBoard(fen);
  const flip = g.player_color === "black";
  const boardEl = $("#board");
  boardEl.innerHTML = "";
  const lastMoveData = idx > 0 ? g.moves[idx - 1] : null;
  const last = lastMoveData
    ? {from: lastMoveData.from, to: lastMoveData.to, is_bot: lastMoveData.is_bot, san: lastMoveData.san}
    : null;
  const hint = (!isViewing() && g.hint_uci) ? { from: g.hint_uci.slice(0, 2), to: g.hint_uci.slice(2, 4) } : null;
  const isBotMode = ["bot","coach","opening","blitz"].includes(g.mode);
  for(let dr=0; dr<8; dr++){
    for(let dc=0; dc<8; dc++){
      const r = flip ? 7 - dr : dr;
      const c = flip ? 7 - dc : dc;
      const file = "abcdefgh"[c]; const rank = 8 - r; const sq = file + rank;
      const isLight = (r + c) % 2 === 0;
      const div = document.createElement("div");
      div.className = "square " + (isLight ? "light" : "dark");
      div.dataset.square = sq;
      if(last && (last.from === sq || last.to === sq)){
        div.classList.add("last-move");
        if(isBotMode && last.is_bot){ div.classList.add("last-move-opp"); }
      }
      if(hint && (hint.from === sq || hint.to === sq)){ div.classList.add("hint"); }
      if(dr === 7){
        const lbl = document.createElement("span");
        lbl.className = "coord coord-file"; lbl.textContent = file;
        div.appendChild(lbl);
      }
      if(dc === 0){
        const lbl = document.createElement("span");
        lbl.className = "coord coord-rank"; lbl.textContent = rank;
        div.appendChild(lbl);
      }
      const p = pieces[sq];
      if(p){
        const span = document.createElement("span");
        span.className = "piece " + p.color;
        span.textContent = PIECES[p.type] || "";
        div.appendChild(span);
      }
      if(S.selected && !isViewing()){
        const dests = (S.legalMoves || []).filter(m => m.startsWith(S.selected + sq));
        if(dests.length){
          const dot = document.createElement("div");
          if(p || dests.some(m => m.length > 4)){ dot.className = "capture-ring"; }
          else{ dot.className = "dot"; }
          div.appendChild(dot);
        }
      }
      div.addEventListener("click", () => onSquareClick(sq));
      boardEl.appendChild(div);
    }
  }
  renderClocks();
}

function onSquareClick(sq){
  if(!S.game) return;
  const g = S.game;
  if(isViewing()) return;
  if(g.game_over) return;
  const board = fenBoard(g.fen);
  const turn = g.turn; const piece = board[sq];
  if(S.selected){
    if(sq === S.selected){ S.selected = null; renderBoard(); return; }
    if((S.legalMoves || []).some(m => m.startsWith(S.selected + sq))){ tryMove(S.selected, sq); return; }
  }
  if(piece && piece.color === turn){
    if(["bot","opening","blitz","coach"].includes(g.mode) && turn !== g.player_color) return;
    S.selected = sq; S.legalMoves = g.legal_moves || []; renderBoard();
  }else{
    S.selected = null; renderBoard();
  }
}

function tryMove(from, to){
  const g = S.game;
  if(!g) return;
  const candidates = (g.legal_moves || []).filter(m => m.startsWith(from + to));
  if(!candidates.length) return;
  if(candidates.length > 1 && candidates.some(m => m.length > 4)){
    modal(`<h3>Превращение пешки</h3><div class="promo-buttons">
      <button data-promo="q">Ферзь</button><button data-promo="r">Ладья</button>
      <button data-promo="b">Слон</button><button data-promo="n">Конь</button>
    </div>`);
    $$("#modalRoot [data-promo]").forEach(b => {
      b.onclick = () => { closeModal(); sendMove(from, to, b.dataset.promo); };
    });
  }else{
    const uci = candidates[0];
    sendMove(from, to, uci.length > 4 ? uci.slice(4) : "");
  }
}

function sendMove(from, to, promotion){
  const g = S.game;
  if(!g || S.timeoutSent) return;
  const think = S.lastMoveAt ? (Date.now() - S.lastMoveAt) / 1000 : 0;
  const prevMsgs = (g.coach_messages || []).length;
  const statusBefore = $("#gameStatus").textContent;
  $("#gameStatus").textContent = S.llmActive ? "🧠 Тренер думает..." : "Анализ...";
  api("/api/move", {
    game_id: g.game_id, from: from, to: to,
    promotion: promotion || "", think_time: think,
    profanity: S.settings.profanity,
    use_llm: S.settings.useLlm
  }).then(res => {
    S.game = res; S.viewPly = null; S.selected = null;
    S.legalMoves = res.legal_moves || [];
    S.lastMoveAt = Date.now(); S.nagTurn = null;
    S.llmActive = !!res.llm_active;
    updateLlmBadge();
    renderAll();
    handleLastEvent(res, prevMsgs);
  }).catch(e => {
    $("#gameStatus").textContent = statusBefore;
    addLocalCoach("Ошибка: " + e.message, "angry");
  });
}

function addLocalCoach(text, emotion){
  if(!S.game) return;
  S.game.coach_messages = S.game.coach_messages || [];
  S.game.coach_messages.push({
    text: text, emotion: emotion || "neutral",
    source: "system", is_bot: false, time: Date.now()/1000
  });
  renderChat();
  speakBatch([{text, emotion}]);
}

function handleLastEvent(res, prevMsgs){
  const ev = res.last_event || {};
  if(ev.type === "capture") playSound("capture");
  else if(ev.type === "check") playSound("check");
  else if(ev.type === "mate" || ev.type === "solved" || ev.type === "timeout" || ev.type === "opening_completed") playSound("mate");
  else if(ev.type === "move" || ev.type === "wrong" || ev.type === "undo") playSound("move");
  const msgs = res.coach_messages || [];
  const newMsgs = msgs.slice(prevMsgs);
  const toSpeak = newMsgs.filter(m => {
    if(m.source === 'system') return true;
    if(m.source === 'llm') return true;
    return !S.llmActive;
  });
  if(toSpeak.length) speakBatch(toSpeak);
  if(res.solved){
    $("#btnNextPuzzle").style.display = res.mode === "puzzle" ? "inline-block" : "none";
    $("#btnNextLesson").style.display = res.mode === "lesson" ? "inline-block" : "none";
    if(res.mode === "lesson" && res.lesson_id){
      let p = {};
      try{ p = JSON.parse(localStorage.getItem("lessonProgress") || "{}"); }catch(e){}
      p[res.lesson_id] = true;
      localStorage.setItem("lessonProgress", JSON.stringify(p));
    }
  }else{
    $("#btnNextPuzzle").style.display = "none";
    $("#btnNextLesson").style.display = "none";
  }
  S.clocks = null; initClocks(); renderClocks();
}

function renderOpeningBar(){
  const g = S.game; const bar = $("#openingBar");
  if(!g || g.mode !== "opening"){ bar.style.display = "none"; return; }
  const n = g.opening_index || 0;
  const total = g.opening_total || 0;
  let text = `📖 ${g.opening_name || "Дебют"}`;
  if(total) text += ` — ход ${n} из ${total}`;
  if(g.deviation) text += " — соперник отклонился";
  if(g.opening_completed) text = `✅ ${text} — дебют выполнен!`;
  else if(g.opening_idea) text += `. ${g.opening_idea}`;
  bar.textContent = text;
  bar.style.display = "block";
}

function renderNotation(){
  const g = S.game;
  if(!g) return;
  const el = $("#notation");
  el.innerHTML = "";
  if(!g.moves.length){
    el.innerHTML = '<div class="empty">Пока нет ходов</div>';
    updateViewControls();
    return;
  }
  const curIdx = getViewIndex();
  const activeMoveIdx = curIdx - 1;
  g.moves.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "notation-row";
    if(i === activeMoveIdx) row.classList.add("active");
    if(!isViewing() && i === g.moves.length - 1) row.classList.add("current-live");
    const num = Math.floor(i / 2) + 1;
    const dots = i % 2 === 0 ? "." : "...";
    row.innerHTML = `<span class="move-no">${num}${dots}</span><span class="move-san">${m.san}</span>`;
    row.onclick = () => enterView(i + 1);
    el.appendChild(row);
  });
  updateViewControls();
}

function updateViewControls(){
  if(!S.game) return;
  const cur = getViewIndex(); const max = S.game.fens.length - 1;
  $("#btnPrev").disabled = cur <= 0;
  $("#btnNext").disabled = cur >= max;
  $("#btnLive").style.display = isViewing() ? "inline-block" : "none";
  $("#btnUndo").disabled = isViewing();
  $("#btnHint").disabled = isViewing() || !!S.game.opening_completed;
  if (S.game && (S.game.mode === "puzzle" || S.game.opening_completed)) {
      $("#btnHint").style.display = "none";
  } else {
      $("#btnHint").style.display = "inline-block";
  }
  if(isViewing()){
    $("#viewBanner").style.display = "flex";
    const moveNum = cur === 0 ? "старт" : `после хода ${Math.ceil(cur/2)}`;
    $("#viewBannerText").textContent =
      `📜 Просмотр: ${moveNum} (${cur}/${max}). Кнопка «К игре» — вернуться.`;
  }else{
    $("#viewBanner").style.display = "none";
  }
}

function renderChat(){
  const g = S.game;
  const el = $("#coachChat");
  if(!g){ el.innerHTML = ""; return; }
  el.innerHTML = "";
  (g.coach_messages || []).forEach(m => {
    const d = document.createElement("div");
    d.className = "chat-msg" + (m.is_bot ? " bot" : "");
    if(m.emotion === "angry") d.classList.add("angry");
    else if(m.emotion === "praise") d.classList.add("praise");
    else if(m.emotion === "calm") d.classList.add("calm");
    if(m.source) d.setAttribute("data-source", m.source);
    d.textContent = m.text;
    el.appendChild(d);
  });
  requestAnimationFrame(() => {
    el.scrollTop = el.scrollHeight;
  });
}

function renderStatus(){
  if(!S.game) return;
  const el = $("#gameStatus");
  el.textContent = S.game.status || "";
  el.className = S.game.opening_completed ? "completed" : "";
}

function renderAll(){
  if(!S.game) return;
  renderBoard(); renderNotation(); renderChat(); renderStatus();
  renderOpeningBar(); renderClocks(); updateViewControls();
}

function updateLlmBadge(){
  const b = $("#llmBadge");
  if(!S.game){ b.style.display = "none"; return; }
  b.style.display = "inline-block";
  if(!S.settings.useLlm){
    b.textContent = "🧠 ВЫКЛ"; b.className = "off";
    b.style.background = "#555"; b.style.color = "#ccc";
  } else if(S.game.llm_active){
    b.textContent = "🧠 ИИ"; b.className = "";
    b.style.background = "#2c4a2c"; b.style.color = "#b7e3b7";
  } else {
    b.textContent = "🧠 офлайн"; b.className = "off";
    b.style.background = "#4a2c2c"; b.style.color = "#e3b7b7";
  }
}

function startGameState(res){
  S.game = res; S.viewPly = null; S.selected = null;
  S.legalMoves = res.legal_moves || [];
  S.lastMoveAt = Date.now(); S.clocks = null; S.nagTurn = null;
  S.timeoutSent = false; S.llmActive = !!res.llm_active;
  initClocks();
  showScreen("game");
  $("#title").textContent = "Партия";
  updateLlmBadge();
  try{ history.pushState({screen: "game"}, "", "#game"); }catch(e){}
  renderAll();
  if(res.coach_messages && res.coach_messages.length){ speakBatch(res.coach_messages.slice(-1)); }
  if(res.solved){
    $("#btnNextPuzzle").style.display = res.mode === "puzzle" ? "inline-block" : "none";
    $("#btnNextLesson").style.display = res.mode === "lesson" ? "inline-block" : "none";
  }else{
    $("#btnNextPuzzle").style.display = "none";
    $("#btnNextLesson").style.display = "none";
  }
}

function openBotSetup(coachMode){
  const title = coachMode ? "Учиться с тренером" : "Игра с ботом";
  modal(`
    <h3>${title}</h3>
    <label>Цвет<select id="botColor">
      <option value="white">Белые</option><option value="black">Чёрные</option>
      <option value="random">Случайно</option></select></label>
    <label>Сложность<span><input id="botLevel" type="range" min="1" max="10" value="5"><b id="botLevelVal">5</b></span></label>
    <label>Время<select id="botTime">
      <option value="0">Без времени</option><option value="60">1 мин (пуля)</option>
      <option value="180">3 мин (блиц)</option><option value="300">5 мин (рапид)</option></select></label>
    <div class="modal-actions"><button id="botCancel">Отмена</button><button id="botStart">Начать</button></div>
  `);
  $("#botLevel").oninput = () => $("#botLevelVal").textContent = $("#botLevel").value;
  $("#botCancel").onclick = closeModal;
  $("#botStart").onclick = () => {
    const color = $("#botColor").value;
    const level = parseInt($("#botLevel").value, 10);
    const tc = parseInt($("#botTime").value, 10);
    const mode = coachMode ? "coach" : (tc > 0 ? "blitz" : "bot");
    closeModal();
    api("/api/new_game", {mode, player_color: color, level, time_control: tc, profanity: S.settings.profanity})
      .then(startGameState).catch(e => alert("Ошибка: " + e.message));
  };
}

function openLocalSetup(){
  modal(`
    <h3>Игра на двоих</h3>
    <label>Время<select id="localTime">
      <option value="0">Без времени</option><option value="60">1 мин</option>
      <option value="180">3 мин</option><option value="300">5 мин</option></select></label>
    <div class="modal-actions"><button id="localCancel">Отмена</button><button id="localStart">Начать</button></div>
  `);
  $("#localCancel").onclick = closeModal;
  $("#localStart").onclick = () => {
    const tc = parseInt($("#localTime").value, 10);
    closeModal();
    api("/api/new_game", {mode: "local", player_color: "white", level: 5, time_control: tc, profanity: S.settings.profanity})
      .then(startGameState).catch(e => alert("Ошибка: " + e.message));
  };
}

function openSpeedSetup(){
  modal(`
    <h3>Обучение скорости</h3>
    <p style="color:#ccc;font-size:13px">Тренер будет ругаться за медлительность.</p>
    <label>Контроль<select id="speedTime">
      <option value="60">1 мин — пуля</option><option value="180" selected>3 мин — блиц</option>
      <option value="300">5 мин — рапид</option></select></label>
    <div class="modal-actions"><button id="speedCancel">Отмена</button><button id="speedStart">Начать</button></div>
  `);
  $("#speedCancel").onclick = closeModal;
  $("#speedStart").onclick = () => {
    const tc = parseInt($("#speedTime").value, 10);
    closeModal();
    api("/api/new_game", {mode: "blitz", player_color: "white", level: 5, time_control: tc, profanity: S.settings.profanity})
      .then(startGameState).catch(e => alert("Ошибка: " + e.message));
  };
}

function openSettings(){
  modal(`
    <h3>Настройки</h3>
    <div class="settings-row"><span>Голос тренера</span>
      <input id="setVoiceOn" type="checkbox" ${S.settings.voice ? "checked" : ""}></div>
    <div class="settings-row"><span>Звуки</span>
      <input id="setSounds" type="checkbox" ${S.settings.sounds ? "checked" : ""}></div>
    <div class="settings-row"><span>Мат в комментариях</span>
      <input id="setProfanity" type="checkbox" ${S.settings.profanity ? "checked" : ""}></div>
    <div class="settings-row"><span>Использовать ИИ (Нейросеть)</span>
      <input id="setLlmOn" type="checkbox" ${S.settings.useLlm ? "checked" : ""}></div>
    <label>Голос <select id="setVoice"></select></label>
    <button id="setReloadVoices" style="width:100%;margin:6px 0;background:#555;">🔄 Обновить голоса</button>
    <div class="settings-row" style="border-top:1px solid #444;padding-top:8px;margin-top:12px">
      <span>Нейро-тренер (LLM)</span>
      <span id="llmStatus" style="color:#999">проверка...</span>
    </div>
    <div class="modal-actions"><button id="setClose">Готово</button></div>
  `);
  loadVoices();
  api("/api/llm_status").then(r => {
    const st = $("#llmStatus");
    if(!st) return;
    if(r.available){ st.textContent = `✅ ${r.model || "онлайн"}`; st.style.color = "#8bd08b"; }
    else { st.textContent = "⚠ офлайн"; st.style.color = "#c99"; }
  }).catch(() => {});
  $("#setReloadVoices").onclick = () => {
    speechSynthesis.cancel();
    try { speechSynthesis.getVoices(); } catch(e) {}
    setTimeout(() => {
      loadVoices();
      const n = speechSynthesis.getVoices().length;
      alert("Голосов найдено: " + n + ". Если нужного нет — установите RHVoice как движок TTS в настройках Android.");
    }, 500);
  };
  $("#setClose").onclick = () => {
    S.settings.voice = $("#setVoiceOn").checked;
    S.settings.sounds = $("#setSounds").checked;
    S.settings.profanity = $("#setProfanity").checked;
    S.settings.useLlm = $("#setLlmOn").checked;
    S.settings.voiceName = $("#setVoice").value || "";
    saveSettings();
    updateLlmBadge(); closeModal();
  };
}

async function loadOpenings(){
  showScreen("openings");
  $("#title").textContent = "Дебюты";
  const listEl = $("#openingsList");
  listEl.innerHTML = "Загрузка базы дебютов (Lichess)...";

  let allOpenings = [];
  let dataReady = false;
  let debounceTimer = null;

  function performRenderNow(){
    if(!dataReady) return;
    const inp = document.getElementById("openingsSearch");
    const raw = inp && inp.value ? String(inp.value) : "";
    const f = raw.trim().toLowerCase();
    listEl.innerHTML = "";
    let filtered;
    if(f){
      filtered = allOpenings.filter(o => {
        const en = (o.name || "").toLowerCase();
        const ru = (o.name_ru || "").toLowerCase();
        const eco = (o.eco || "").toLowerCase();
        return en.indexOf(f) !== -1 || ru.indexOf(f) !== -1 || eco.indexOf(f) !== -1;
      });
    }else{
      filtered = allOpenings;
    }
    const limit = f ? 500 : 200;
    filtered.slice(0, limit).forEach(o => {
      const b = document.createElement("button");
      b.className = "list-btn";
      const idea = o.idea ? `<span class="idea">${o.idea}</span>` : "";
      b.innerHTML = `<b>${o.name_ru || o.name}</b><small>${o.name} · ${o.eco} · ${o.length} ходов</small>${idea}`;
      b.onclick = () => startOpening(o.name);
      listEl.appendChild(b);
    });
    if(!listEl.children.length){
      listEl.innerHTML = '<div class="empty">Ничего не найдено</div>';
    }
  }

  function performRenderDebounced(){
    if(debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(performRenderNow, 80);
  }

  // Привязываем обработчики ДО await — поиск работает даже пока база грузится
  const oldInp = document.getElementById("openingsSearch");
  if(oldInp && oldInp.parentNode){
    const fresh = oldInp.cloneNode(true);
    fresh.value = "";
    oldInp.parentNode.replaceChild(fresh, oldInp);
    fresh.addEventListener("input", performRenderDebounced);
    fresh.addEventListener("keyup", performRenderDebounced);
    fresh.addEventListener("search", performRenderDebounced);
    fresh.addEventListener("change", performRenderDebounced);
  }

  try{
    const data = await api("/api/openings");
    allOpenings = (data && data.openings) || [];
    dataReady = true;
    performRenderNow();
  }catch(e){
    listEl.innerHTML = "Ошибка: " + e.message;
  }
}

function startOpening(name){
  api("/api/new_game", {mode: "opening", opening_name: name, player_color: "white", level: 5, profanity: S.settings.profanity})
    .then(startGameState).catch(e => alert("Ошибка: " + e.message));
}

async function loadLessons(){
  showScreen("lessons");
  $("#title").textContent = "Уроки";
  const el = $("#lessonsList");
  el.innerHTML = "Загрузка...";
  try{
    const data = await api("/api/lessons");
    let progress = {};
    try{ progress = JSON.parse(localStorage.getItem("lessonProgress") || "{}"); }catch(e){}
    el.innerHTML = "";
    data.lessons.forEach((l, i) => {
      const locked = i > 0 && !progress[data.lessons[i - 1].id];
      const b = document.createElement("button");
      b.className = "list-btn" + (locked ? " locked" : "");
      b.innerHTML = `<b>${locked ? "🔒 " : ""}${l.id}. ${l.title}</b><small>${l.description}</small>`;
      if(!locked) b.onclick = () => startLesson(l.id);
      el.appendChild(b);
    });
  }catch(e){ el.innerHTML = "Ошибка: " + e.message; }
}

function startLesson(id){
  api("/api/new_game", {mode: "lesson", lesson_id: id, player_color: "white", level: 5, profanity: S.settings.profanity})
    .then(startGameState).catch(e => alert("Ошибка: " + e.message));
}

function startPuzzle(type){
  api("/api/new_game", {mode: "puzzle", puzzle_type: type, player_color: "white", level: 5, profanity: S.settings.profanity})
    .then(startGameState).catch(e => alert("Ошибка: " + e.message));
}

async function loadStats(){
  showScreen("stats");
  $("#title").textContent = "Мой автопортрет";
  const el = $("#statsText");
  el.textContent = "Загрузка...";
  try{
    const data = await api("/api/profile");
    el.textContent = data.autoportrait;
  }catch(e){ el.textContent = "Ошибка: " + e.message; }
}

$$("[data-action]").forEach(btn => {
  btn.onclick = () => {
    const a = btn.dataset.action;
    if(a === "bot") openBotSetup(false);
    else if(a === "coach") openBotSetup(true);
    else if(a === "local") openLocalSetup();
    else if(a === "openings") loadOpenings();
    else if(a === "lessons") loadLessons();
    else if(a === "mate1") startPuzzle("mate1");
    else if(a === "mateN") startPuzzle("mate2");
    else if(a === "speed") openSpeedSetup();
    else if(a === "stats") loadStats();
    else if(a === "settings") openSettings();
  };
});

$("#btnMenu").onclick = goMenu;
$("#btnSettings").onclick = openSettings;

$("#btnUndo").onclick = () => {
  if(!S.game) return;
  const prevMsgs = (S.game.coach_messages || []).length;
  api("/api/undo", {game_id: S.game.game_id})
    .then(res => {
      S.game = res; S.viewPly = null; S.selected = null;
      S.legalMoves = res.legal_moves || [];
      S.lastMoveAt = Date.now(); S.nagTurn = null; S.timeoutSent = false;
      S.llmActive = !!res.llm_active;
      updateLlmBadge(); renderAll(); handleLastEvent(res, prevMsgs);
    })
    .catch(e => addLocalCoach("Ошибка: " + e.message, "angry"));
};

$("#btnHint").onclick = () => {
  if(!S.game) return;
  const prevMsgs = (S.game.coach_messages || []).length;
  api("/api/hint", {game_id: S.game.game_id})
    .then(res => {
      S.game = res; S.legalMoves = res.legal_moves || [];
      renderAll(); handleLastEvent(res, prevMsgs);
    })
    .catch(e => addLocalCoach("Ошибка: " + e.message, "angry"));
};

$("#btnNextPuzzle").onclick = () => {
  const t = S.game && S.game.puzzle_type ? S.game.puzzle_type : "mate1";
  startPuzzle(t);
};
$("#btnNextLesson").onclick = loadLessons;
$("#btnGameMenu").onclick = goMenu;

$("#btnPrev").onclick = () => {
  if(!S.game) return;
  const cur = getViewIndex();
  if(cur <= 0) return;
  enterView(cur - 1);
};
$("#btnNext").onclick = () => {
  if(!S.game) return;
  const cur = getViewIndex(); const max = S.game.fens.length - 1;
  if(cur >= max) return;
  enterView(cur + 1);
};
$("#btnLive").onclick = exitView;
$("#btnExitView").onclick = exitView;

window.addEventListener("popstate", () => {
  if(S.currentScreen !== "menu") goMenu();
});

setInterval(() => {
  const g = S.game;
  if(!g || !g.time_control || g.game_over || g.timed_out || S.timeoutSent || g.opening_completed) return;
  if(!S.clocks) initClocks();
  if(!S.clocks) return;
  const now = Date.now();
  const dt = (now - S.clocks.last) / 1000;
  S.clocks.last = now;
  const active = g.turn;
  if(S.clocks[active] > 0){
    S.clocks[active] = Math.max(0, S.clocks[active] - dt);
    renderClocks();
  }else{
    S.timeoutSent = true;
    api("/api/timeout", {game_id: g.game_id, loser: active})
      .then(res => {
        S.game = res; renderAll();
        const msgs = res.coach_messages || [];
        if(msgs.length) speakBatch(msgs.slice(-1));
        playSound("mate");
      })
      .catch(() => { S.timeoutSent = false; });
    return;
  }
  if((g.mode === "blitz" || g.mode === "coach") && g.turn === g.player_color && !isViewing()){
    const elapsed = (now - (S.lastMoveAt || now)) / 1000;
    const limit = g.time_control <= 60 ? 6 : g.time_control <= 180 ? 10 : 15;
    if(elapsed > limit && S.nagTurn !== g.moves.length){
      S.nagTurn = g.moves.length;
      api("/api/hint", {game_id: g.game_id})
        .then(res => {
          S.game = res; renderAll();
          const msgs = res.coach_messages || [];
          if(msgs.length) speakBatch(msgs.slice(-1));
        })
        .catch(() => {});
    }
  }
}, 250);

showScreen("menu");
try{ history.replaceState({screen: "menu"}, "", "#menu"); }catch(e){}
</script>
</body>
</html>
'''


if __name__ == "__main__":
    print(f"Stockfish: {STOCKFISH_PATH}")
    print(f"LLM: {'включён (' + LLM_CONFIG['model'] + ')' if llm_available() else 'выключен (офлайн-режим)'}")
    print(f"Совет: для нейро-тренера создай {LLM_CONFIG_FILE}")
    app.run(host="127.0.0.1", port=5000, threaded=True)
