"""
Локальный файловый агент для La Zarro.
Работает на твоём Windows ПК, слушает задачи из Supabase и выполняет
файловые операции (найти файл, переместить, скопировать, показать содержимое папки)
через Claude API с tool use.

Установка зависимостей:
    pip install anthropic supabase python-dotenv

Файл .env рядом со скриптом:
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY=eyJ...           # service_role key, не anon key
    ANTHROPIC_API_KEY=sk-ant-...
    POLL_INTERVAL_SECONDS=5
    SEARCH_ROOT=C:\\Users\\ТвойUser        # где агенту разрешено искать/двигать файлы
"""

import os
import time
import json
import shutil
import traceback
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client
from anthropic import Anthropic

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
SEARCH_ROOT = Path(os.environ.get("SEARCH_ROOT", str(Path.home())))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client = Anthropic(api_key=ANTHROPIC_API_KEY)

# --- Безопасность: агент может работать только внутри SEARCH_ROOT ---
def _safe_path(p: str) -> Path:
    path = (SEARCH_ROOT / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
    if SEARCH_ROOT.resolve() not in path.parents and path != SEARCH_ROOT.resolve():
        raise PermissionError(f"Путь вне разрешённой зоны: {path}")
    return path


# --- Реальные файловые функции ---
def list_directory(relative_path: str = ".") -> str:
    target = _safe_path(relative_path)
    if not target.exists():
        return f"Папка не найдена: {target}"
    items = []
    for item in target.iterdir():
        kind = "папка" if item.is_dir() else "файл"
        items.append(f"[{kind}] {item.name}")
    return "\n".join(items) if items else "Папка пустая"


def find_file(name_pattern: str) -> str:
    matches = []
    for path in SEARCH_ROOT.rglob(f"*{name_pattern}*"):
        matches.append(str(path))
        if len(matches) >= 20:
            break
    if not matches:
        return f"Ничего не найдено по запросу '{name_pattern}'"
    return "\n".join(matches)


def move_file(source_path: str, destination_folder: str) -> str:
    src = _safe_path(source_path)
    dest_dir = _safe_path(destination_folder)
    if not src.exists():
        return f"Файл не найден: {src}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(str(src), str(dest))
    return f"Перемещено: {src} -> {dest}"


def copy_file(source_path: str, destination_folder: str) -> str:
    src = _safe_path(source_path)
    dest_dir = _safe_path(destination_folder)
    if not src.exists():
        return f"Файл не найден: {src}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(str(src), str(dest))
    return f"Скопировано: {src} -> {dest}"


TOOLS = [
    {
        "name": "list_directory",
        "description": "Показать содержимое папки (файлы и подпапки)",
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Путь относительно домашней папки пользователя, например 'Desktop' или '.'"}
            },
            "required": ["relative_path"],
        },
    },
    {
        "name": "find_file",
        "description": "Найти файл или папку по части имени внутри разрешённой зоны поиска",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_pattern": {"type": "string", "description": "Часть названия файла/папки для поиска, например 'rhino' или 'новая папка'"}
            },
            "required": ["name_pattern"],
        },
    },
    {
        "name": "move_file",
        "description": "Переместить файл или папку в другое место",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Полный путь к файлу, который нужно переместить"},
                "destination_folder": {"type": "string", "description": "Папка назначения, например 'Desktop'"},
            },
            "required": ["source_path", "destination_folder"],
        },
    },
    {
        "name": "copy_file",
        "description": "Скопировать файл или папку в другое место",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "destination_folder": {"type": "string"},
            },
            "required": ["source_path", "destination_folder"],
        },
    },
]

TOOL_FUNCTIONS = {
    "list_directory": list_directory,
    "find_file": find_file,
    "move_file": move_file,
    "copy_file": copy_file,
}

SYSTEM_PROMPT = f"""Ты — локальный файловый ассистент на компьютере пользователя.
Разрешённая зона работы: {SEARCH_ROOT}
Пользователь пишет задачу на русском или узбекском обычным языком, например:
"открой файл с названием новая папка, там есть rhino 7, перемести его на рабочий стол".

Твоя работа:
1. Сначала найди нужный файл/папку через find_file или list_directory, если путь не очевиден.
2. Затем выполни нужное действие (move_file / copy_file).
3. В конце дай короткий итог на русском языке, что именно сделано (или что пошло не так).
Не выполняй ничего, в чём не уверен — сначала посмотри содержимое папок.
"""


def process_task(task_text: str) -> str:
    messages = [{"role": "user", "content": task_text}]

    for _ in range(8):  # ограничение на число шагов агента
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            final_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            return final_text or "Задача обработана."

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            func = TOOL_FUNCTIONS.get(block.name)
            try:
                result = func(**block.input) if func else f"Неизвестный инструмент: {block.name}"
            except Exception as e:
                result = f"Ошибка: {e}"
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": str(result)}
            )

        messages.append({"role": "user", "content": tool_results})

    return "Задача превысила лимит шагов, остановлено для безопасности."


def main_loop():
    print(f"Агент запущен. Зона работы: {SEARCH_ROOT}")
    while True:
        try:
            resp = (
                supabase.table("agent_tasks")
                .select("*")
                .eq("status", "pending")
                .order("created_at")
                .limit(1)
                .execute()
            )
            tasks = resp.data
            if tasks:
                task = tasks[0]
                task_id = task["id"]
                print(f"Новая задача #{task_id}: {task['task_text']}")

                supabase.table("agent_tasks").update(
                    {"status": "in_progress"}
                ).eq("id", task_id).execute()

                try:
                    result = process_task(task["task_text"])
                    supabase.table("agent_tasks").update(
                        {"status": "done", "result": result}
                    ).eq("id", task_id).execute()
                    print(f"Готово #{task_id}: {result}")
                except Exception:
                    err = traceback.format_exc()
                    supabase.table("agent_tasks").update(
                        {"status": "error", "result": err[:2000]}
                    ).eq("id", task_id).execute()
                    print(f"Ошибка #{task_id}:\n{err}")

        except Exception:
            print("Ошибка опроса Supabase:")
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
