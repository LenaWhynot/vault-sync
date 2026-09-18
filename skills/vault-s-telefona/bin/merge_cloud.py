#!/usr/bin/env python3
"""
merge_cloud.py — вливает ветки облачных агентов в main, чтобы Лене не нажимать Merge.

Зачем: Claude Code on the web и Codex cloud пушат работу в СВОЮ ветку
(origin/claude/*, origin/codex/*). Мак подтягивает только main, поэтому без
слияния работа с телефона до мака не доезжает и висит месяцами (так повисли
4 ветки с 09.08 по 16.09).

Политика — как у tom-automerge (решение Лены 31.08.2026): мёржим широко,
но НЕ трогаем пути, где чужая среда ломает мак-специфичное:
  .claude/  scripts/  .github/  и любые файлы секретов.
Ветка, которая лезет в эти пути (или конфликтует), остаётся ждать рук. Про неё
приходит баннер — но ОДИН раз на состав, а не каждый тик: см. notify_blocked().

Запуск: launchd com.lena.merge-cloud, каждые 15 мин. Тот же flock, что у
git_auto.py — иначе автокоммит/автопуш и слияние передерутся за индекс.
Флаг --dry-run: показать, что влилось бы, ничего не меняя.
"""
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from datetime import datetime

REPO = str(pathlib.Path(__file__).resolve().parent.parent)
BRANCH = "main"
LOCK = f"{REPO}/.git/git_auto.lock"        # общий с git_auto.py, не свой
# Память уведомлений: какие ветки уже показаны Лене и когда. В .git/ — файл
# не должен уезжать в репозиторий и не должен попадать в автокоммит.
SEEN = f"{REPO}/.git/merge-cloud-seen.json"
REMIND_DAYS = 7  # молчим, пока состав застрявших веток не изменился
PREFIXES = ("claude/", "codex/")            # чьи ветки вливаем
# Пути, которые автомат НЕ вливает. Кроме мак-специфичного (.claude/, scripts/)
# сюда обязательно входит всё ИСПОЛНЯЕМОЕ и всё, что управляет самим git:
#   .githooks/    — core.hooksPath=.githooks, то есть pre-commit ЗАПУСКАЕТСЯ на
#                   маке при каждом автокоммите (≤10 мин). Влить подменённый
#                   хук = дать чужой ветке выполнить код на маке.
#   .gitattributes — задаёт merge=union для журналов, то есть правила слияния.
#   .gitmodules    — сабмодуль тянет чужой код при обновлении.
# Сравниваем КОМПОНЕНТЫ пути, а не префикс строки: иначе
# 06-projects/whynotailab/scripts/deploy.sh проскочит мимо "scripts/".
# И всё в нижнем регистре — macOS регистронезависим, Scripts/ это та же папка.
DENY_DIRS = {".claude", ".github", ".githooks", "scripts"}
DENY_FILES = {".mcp.json", ".gitattributes", ".gitmodules"}


NO_HOOKS = tempfile.mkdtemp(prefix="merge-cloud-nohooks-")  # заведомо пустой


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO, check=check,
                          capture_output=True, text=True)


def log(msg):
    print(f"[{datetime.now():%F %H:%M}] merge_cloud: {msg}", flush=True)


def notify(title, msg):
    script = "on run {m, t}\ndisplay notification m with title t\nend run"
    subprocess.run(["osascript", "-e", script, msg, title],
                   capture_output=True, check=False)


def notify_blocked(blocked):
    """Баннер про застрявшие ветки — только когда есть что сказать НОВОГО.

    Грабля 17.09.2026: launchd будит скрипт каждые 15 минут, и каждый раз он слал
    один и тот же баннер про одни и те же четыре ветки — 96 штук в сутки. Такое
    уведомление перестают читать, и вместе с ним теряется настоящее событие:
    только что застрявшая ветка. Поэтому состав застрявших запоминаем, а
    показываем, когда он изменился (новая ветка или новые коммиты в старой) —
    либо раз в REMIND_DAYS как редкое напоминание.
    """
    cur = {ref: sha for ref, _, _, sha in blocked}
    try:
        seen = json.loads(pathlib.Path(SEEN).read_text())
    except (OSError, ValueError):
        seen = {}
    unchanged = seen.get("branches") == cur
    quiet_left = seen.get("last", 0) + REMIND_DAYS * 86400 - time.time()
    if unchanged and quiet_left > 0:
        log(f"состав застрявших веток не менялся — молчу ещё {quiet_left / 86400:.1f} дн.")
        return
    # дата самого старого коммита — она объясняет срочность лучше имён веток
    dates = [git("log", "-1", "--format=%cs", ref, check=False).stdout.strip()
             for ref, _, _, _ in blocked]
    dates = sorted(d for d in dates if d)
    n = len(blocked)
    word = "ветка" if n == 1 else ("ветки" if n < 5 else "веток")
    tail = f", самая старая с {dates[0]}" if dates else ""
    notify(f"Lena OC: {n} {word} из облака ждут разбора",
           f"{blocked[0][0].replace('origin/', '')} и др.{tail}. "
           f"Скажи Клоду: «разбери зависшие ветки»")
    try:
        pathlib.Path(SEEN).write_text(json.dumps({"branches": cur, "last": time.time()}))
    except OSError as e:
        log(f"не смог запомнить показанное ({type(e).__name__}) — баннер может повториться")


def denied(path):
    """Чем файл запрещён — на ЛЮБОЙ глубине, не только в корне.

    macOS регистронезависим (Scripts/ и .CLAUDE/ — те же каталоги), поэтому
    сравнение в нижнем регистре: иначе запрет обходится сменой регистра.
    """
    parts = [q.lower() for q in pathlib.PurePosixPath(path).parts]
    if not parts:
        return set()
    hits = {f"{q}/" for q in parts[:-1] if q in DENY_DIRS}
    name = parts[-1]
    if name in DENY_FILES or name.startswith(".env"):
        hits.add(name)
    return hits


def cloud_branches():
    out = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").stdout
    return [b for b in out.split()
            if b.startswith("origin/") and b[len("origin/"):].startswith(PREFIXES)]


DRY = "--dry-run" in sys.argv


def main():
    if not pathlib.Path(LOCK).parent.exists():
        sys.exit(f"нет репозитория: {REPO}")
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # автокоммит/автопуш держит замок — следующий тик доделает

        if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != BRANCH:
            return  # не на main — не наше дело, git_auto вернёт ветку сам

        # Дерево на маке почти всегда грязное (правки идут, автокоммит раз в
        # 10 мин), поэтому ждать чистоты нельзя — иначе слияние не случится
        # никогда. Прячем незакоммиченное, как это делает git_auto.do_push.
        stashed = False
        if git("status", "--porcelain").stdout.strip():
            git("stash", "push", "-u", "-q", "-m", "merge-cloud stash")
            stashed = True

        git("fetch", "origin", "--prune", "-q", check=False)

        merged, blocked = [], []
        for ref in cloud_branches():
            ahead = git("rev-list", "--count", f"{BRANCH}..{ref}").stdout.strip()
            if ahead == "0":
                continue  # уже влито

            files = git("diff", "--name-only", f"{BRANCH}...{ref}").stdout.split("\n")
            files = [f for f in files if f]
            # Ветка может «догнать» main содержанием (правки доехали другим путём:
            # тем же коммитом с мака, ручным разбором), а в графе остаться не влитой.
            # Забирать из неё нечего — и напоминать о ней тоже незачем.
            if git("diff", "--quiet", BRANCH, ref, check=False).returncode == 0:
                continue  # состояния совпали — забирать нечего
            sha = git("rev-parse", "--short", ref).stdout.strip()
            hit = sorted({d for f in files for d in denied(f)})
            if hit:
                blocked.append((ref, hit, len(files), sha))
                continue

            # можно ли слить без конфликта — проверяем, ничего не меняя
            probe = git("merge-tree", "--write-tree", BRANCH, ref, check=False)
            if probe.returncode != 0:
                blocked.append((ref, ["КОНФЛИКТ"], len(files), sha))
                continue
            if DRY:
                merged.append((ref, len(files)))
                continue

            # -c core.hooksPath=<пусто>: слияние делает коммит, а коммит
            # дёргает хуки. Автомату чужие хуки исполнять незачем — глушим их
            # на время операции (защита в глубину к DENY выше).
            r = git("-c", f"core.hooksPath={NO_HOOKS}",
                    "merge", "--no-ff", "--no-edit", ref,
                    "-m", f"авто-слияние {ref} → {BRANCH} ({len(files)} ф.)", check=False)
            if r.returncode != 0:
                git("merge", "--abort", check=False)
                blocked.append((ref, ["КОНФЛИКТ"], len(files), sha))
                continue
            merged.append((ref, len(files)))

        if DRY:
            if stashed:
                git("stash", "pop", "-q", check=False)
            for ref, n in merged:
                log(f"[dry-run] влилось бы {ref} ({n} ф.)")
            for ref, why, n, _ in blocked:
                log(f"[dry-run] НЕ влилось бы {ref} ({n} ф.) — {', '.join(why)}")
            return

        if merged:
            p = git("push", "-q", "origin", BRANCH, check=False)
            if p.returncode != 0:
                if stashed:
                    git("stash", "pop", "-q", check=False)
                log(f"слил {len(merged)}, но push не прошёл: {p.stderr.strip()[:150]}")
                notify("Lena OC: слияние не уехало", "push отклонён, см. лог")
                sys.exit(1)
            for ref, n in merged:
                log(f"влито {ref} ({n} ф.)")

        for ref, why, n, _ in blocked:
            log(f"НЕ влито {ref} ({n} ф.) — {', '.join(why)}")
        if stashed:
            git("stash", "pop", "-q", check=False)

        if blocked:
            notify_blocked(blocked)
        else:
            pathlib.Path(SEEN).unlink(missing_ok=True)  # разобрали всё — забываем


if __name__ == "__main__":
    main()
