#!/usr/bin/env python3
"""
git_auto.py — авто-коммит и авто-push vault'а через launchd.

Почему python3, а не bash: launchd-агент, читающий скрипт из ~/Documents,
упирается в macOS TCC ("Operation not permitted") — так умерли mac-auto-commit.sh
и mac-auto-push.sh. У /opt/homebrew/bin/python3 выдан Full Disk Access
(как у journal_sync.py / whoop-sync.py), и его subprocess-дети (git) наследуют
доступ. Поэтому всю git-логику гоняем здесь, а не в bash.

Режимы:
  commit — git add -A → коммит, если есть что (БЕЗ push). launchd каждые 10 мин.
  push   — fetch → rebase поверх origin → push. launchd каждые 30 мин.

Секреты по-прежнему ловит .githooks/pre-commit; git add -A уважает .gitignore.

launchd: com.lena.auto-commit.plist (commit), com.lena.auto-push.plist (push).
"""
import fcntl
import os
import pathlib
import subprocess
import sys
from datetime import datetime

# REPO по умолчанию — vault Lena OC (обратная совместимость со старым launchd
# без второго аргумента). Переопределяется argv[2] (см. main): один скрипт
# обслуживает несколько репозиториев, под каждый — свой launchd-плист.
REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)  # корень vault от __file__, не хардкод
BRANCH = "main"
LOCK = f"{REPO}/.git/git_auto.lock"
FAILS = f"{REPO}/.git/git_auto_push_fails"  # сколько push-тиков подряд не доехали
RESCUE = "auto/unsynced"  # служебная ветка: куда спасаем локальное при неразрешимом конфликте


def git(*args, check=True):
    """Запустить git в репозитории, вернуть CompletedProcess."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
    )


def log(msg):
    print(f"[{datetime.now():%F %H:%M}] git_auto: {msg}", flush=True)


def has_changes():
    """Есть ли незакоммиченное (рабочее дерево или индекс)."""
    dirty = git("status", "--porcelain").stdout.strip()
    return bool(dirty)


def current_branch():
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def ensure_on_branch():
    """Гарантировать, что авто-синк работает на BRANCH (main).

    Если репозиторий увели на другую ветку (напр. codex/*), main «не держится»:
    rebase в do_push churn'ит чужую ветку и возвращается на неё. Здесь сначала
    фиксируем незакоммиченное НА ТЕКУЩЕЙ ветке (ничего не теряем — её коммиты
    остаются на ней), затем переключаемся на main. Так main держится сам.
    """
    cur = current_branch()
    if cur == BRANCH:
        return
    if has_changes():
        git("add", "-A")
        if git("diff", "--cached", "--name-only").stdout.strip():
            git("commit", "-m",
                f"auto: фиксация на {cur} перед возвратом на {BRANCH} "
                f"{datetime.now():%d.%m %H:%M}", check=False)
    co = git("checkout", BRANCH, check=False)
    if co.returncode != 0:
        log(f"не смог переключиться с {cur} на {BRANCH} — разбор вручную")
        sys.stderr.write(co.stdout + co.stderr)
        sys.exit(1)
    log(f"вернул репозиторий с {cur} на {BRANCH}")


def notify(title, msg):
    """Уведомление macOS. Текст идёт аргументами, а не в тело скрипта —
    не склеиваем строки в AppleScript (имена файлов могут содержать кавычки)."""
    script = "on run {m, t}\ndisplay notification m with title t\nend run"
    subprocess.run(["osascript", "-e", script, msg, title],
                   capture_output=True, check=False)


def conflicted():
    """Файлы, вставшие в конфликт — чтобы лог говорил, ЧТО разбирать."""
    out = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.strip()
    return out.splitlines()


def fail_count(reset=False):
    """Счётчик подряд идущих неудачных push-тиков (в .git/, вне gitа)."""
    if reset:
        try:
            os.remove(FAILS)
        except FileNotFoundError:
            pass
        return 0
    n = 0
    try:
        n = int(pathlib.Path(FAILS).read_text().strip() or 0)
    except (OSError, ValueError):
        n = 0
    n += 1
    pathlib.Path(FAILS).write_text(str(n))
    return n


def rescue_push():
    """Запушить локальный HEAD в служебную ветку, не трогая main.

    Смысл: даже если main заблокирован конфликтом, свежий контекст всё равно
    уезжает в облако — с телефона он доступен, просто в ветке RESCUE.
    force здесь безопасен: ветку создаёт и ведёт только этот скрипт.
    """
    r = git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{RESCUE}", check=False)
    return r.returncode == 0


def do_commit():
    git("add", "-A")
    staged = git("diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        return  # нечего коммитить — тихо выходим
    n = len(staged.splitlines())
    msg = f"auto: локальный коммит {datetime.now():%d.%m %H:%M} ({n} ф.)"
    r = git("commit", "-m", msg, check=False)
    if r.returncode == 0:
        log(f"закоммичено {n} файлов")
    else:
        log("коммит заблокирован (pre-commit/секрет?) — разбор вручную")
        sys.stderr.write(r.stdout + r.stderr)
        sys.exit(1)


def do_push():
    """fetch → свести с origin → push.

    В origin/main пишет не только этот мак, но и VPS Тома (brain-sync), а с
    телефона — облачные агенты. Столкновения неизбежны, поэтому лесенка:
      1) rebase — обычный путь, история линейная;
      2) merge — если rebase уткнулся (он конфликтует покоммитно, merge часто
         проходит там же чисто; вручную это и делалось 14.09);
      3) спасательная ветка — если и merge не свёлся: main ждёт рук, но свежий
         контекст всё равно уезжает в облако и виден с телефона.
    Молча замереть больше нельзя: неудача пишет причину в лог и, со второго
    тика подряд, показывает уведомление.
    """
    # Спрятать незакоммиченное, чтобы rebase/merge шли по чистому дереву
    # (гонка с auto-commit).
    stashed = False
    if has_changes():
        git("stash", "push", "-u", "-q", "-m", "auto-push stash")
        stashed = True

    def unstash():
        if stashed:
            git("stash", "pop", "-q", check=False)

    git("fetch", "origin", BRANCH, "-q")

    rb = git("rebase", f"origin/{BRANCH}", "-q", check=False)
    if rb.returncode != 0:
        files = conflicted()
        git("rebase", "--abort", check=False)

        # Ступень 2: merge.
        mg = git("merge", "--no-edit", f"origin/{BRANCH}",
                 "-m", f"auto: слияние с origin/{BRANCH} {datetime.now():%d.%m %H:%M}",
                 check=False)
        if mg.returncode != 0:
            mfiles = conflicted() or files
            git("merge", "--abort", check=False)

            # Ступень 3: спасаем локальное состояние в служебную ветку.
            saved = rescue_push()
            unstash()

            n = fail_count()
            where = f"локальное спасено в ветку {RESCUE}" if saved else "спасти в ветку НЕ вышло"
            log(f"конфликт не свёлся ({len(mfiles)} ф.: {', '.join(mfiles[:5])}) — "
                f"main ждёт рук, {where}; подряд неудач: {n}")
            if n >= 2:
                notify("Lena OC: синк встал",
                       f"main не синкается {n} тик(ов). Конфликт: "
                       f"{', '.join(mfiles[:3]) or 'см. лог'}")
            sys.exit(1)

        log(f"rebase не прошёл — свёл merge-ом ({len(files)} ф. расходились)")

    unstash()

    ahead = git("log", f"origin/{BRANCH}..HEAD", "--oneline").stdout.strip()
    if ahead:
        pr = git("push", "-q", "origin", BRANCH, check=False)
        if pr.returncode != 0:
            n = fail_count()
            log(f"push отклонён — {pr.stderr.strip()[:200]}; подряд неудач: {n}")
            if n >= 2:
                notify("Lena OC: синк встал", f"push не проходит, тиков подряд: {n}")
            sys.exit(1)
        log("запушено")

    fail_count(reset=True)


def main():
    global REPO, LOCK, FAILS
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("commit", "push"):
        sys.stderr.write("usage: git_auto.py {commit|push} [repo_path]\n")
        sys.exit(2)
    # Необязательный второй аргумент — путь к репозиторию (по умолчанию Lena OC).
    if len(sys.argv) > 2:
        REPO = sys.argv[2]
        LOCK = f"{REPO}/.git/git_auto.lock"
        FAILS = f"{REPO}/.git/git_auto_push_fails"
    # Один общий флаг-замок: commit и push никогда не выполняются одновременно,
    # иначе stash из push выдёргивает файлы из-под add/commit (гонка → пустой
    # коммит). Если параллельный прогон держит замок — тихо уступаем, следующий
    # тик (через 10/30 мин) доделает.
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        ensure_on_branch()  # main должен держаться: не дрейфовать на codex/*
        if mode == "commit":
            do_commit()
        else:
            do_push()


if __name__ == "__main__":
    main()
