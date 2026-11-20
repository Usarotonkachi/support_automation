#!/usr/bin/env python3
"""Ежедневная сводка для support-отдела: данные CRM + рабочие чаты.

Принципы:
  * Статус и приоритет определяются только по данным CRM и времени.
  * Переписка не подтверждает и не меняет статус. Она может только
    поднять приоритет (задержка, расхождение с CRM, жалоба) и попадает
    в сводку отдельным блоком «известно только из переписки».
  * Ни один случай не пропадает: всё, что правила не поняли, уходит
    в сводку с пометкой «проверить вручную».

Запуск:
  python3 digest.py data/monday.json            # сводка в stdout
  python3 digest.py data/monday.json -o out.md  # сводка в файл
Только стандартная библиотека, Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# --- приоритеты ------------------------------------------------------------

CRITICAL, HIGH, NORMAL, NONE = 0, 1, 2, 3
PRIORITY_LABEL = {
    CRITICAL: "СРОЧНО",
    HIGH: "СЕГОДНЯ",
    NORMAL: "ПОД КОНТРОЛЕМ",
    NONE: "ДЕЙСТВИЙ НЕ НУЖНО",
}
TYPE_TITLE = {
    "onboarding": "Оформление сотрудника",
    "offboarding": "Увольнение сотрудника",
    "card_delivery": "Поставка карт",
}
ROLE_TITLE = {
    "employee": "Сотрудник",
    "manager": "Руководитель",
    "courier": "Курьер",
    "recipient": "Получатель",
}


@dataclass
class Item:
    case_id: str
    title: str
    priority: int
    reason: str  # почему такой приоритет, одной фразой
    deadline: datetime  # для сортировки внутри одного приоритета
    crm: list[str] = field(default_factory=list)  # подтверждено в CRM
    chat: list[str] = field(default_factory=list)  # известно только из переписки
    check: list[str] = field(default_factory=list)  # что проверить перед действием
    actions: list[str] = field(default_factory=list)  # что делать дальше


# --- разбор переписки --------------------------------------------------------
# Простые правила по ключевым словам. На реальных чатах этот шаг заменяется
# вызовом LLM по инструкции из prompt.md, формат результата тот же: набор
# сигналов. Остальная логика от этого не меняется.

SIGNALS = {
    "docs_sent": r"(отправ|скинул|переслал)\w*.{0,40}документ|документ\w*.{0,40}(отправ|скинул|переслал)",
    "private_channel": r"в\s+личк|в\s+лс\b|в\s+личн\w+|whatsapp|ватсап|телеграм",
    "manager_aware": r"руководител\w*.{0,40}(в курсе|обсудил|согласовал|знает)|(в курсе|обсудил|согласовал).{0,40}руководител",
    "delay": r"задерж|опазд|не успева|не приеду|не успею",
    "received": r"получил|приняли|принял[аи]?\b",
    "count_ok": r"количеств\w*\s+(провер|сошл|совпа)",
    "problem": r"не хватает|недостач|повре[жд]|брак|не та\b|не те\b",
}
ETA_RE = re.compile(r"(?:до|раньше|к|после|около|в)\s+(\d{1,2})[:.](\d{2})")


def chat_signals(text: str) -> set[str]:
    low = text.lower()
    return {name for name, pattern in SIGNALS.items() if re.search(pattern, low)}


def case_signals(case: dict) -> set[str]:
    return set().union(*(chat_signals(m["text"]) for m in case.get("chat", [])))


def chat_eta(text: str) -> time | None:
    m = ETA_RE.search(text.lower())
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    return time(h, mnt) if h < 24 and mnt < 60 else None


def quote(msg: dict) -> str:
    role = ROLE_TITLE.get(msg.get("role", ""), msg.get("role") or "Участник чата")
    return f"{role}: «{msg['text'].strip().rstrip('.')}»"


# --- форматирование дат -------------------------------------------------------

def rel_day(d: date, today: date) -> str:
    delta = (d - today).days
    names = {-1: "вчера", 0: "сегодня", 1: "завтра"}
    return f"{d:%d.%m} ({names[delta]})" if delta in names else f"{d:%d.%m}"


def end_of(d: date) -> datetime:
    return datetime.combine(d, time(23, 59))


def by_days_left(days: int) -> int:
    """Чем ближе срок, тем выше приоритет."""
    if days <= 0:
        return CRITICAL
    if days == 1:
        return HIGH
    return NORMAL


# --- правила по типам случаев ------------------------------------------------

def onboarding(case: dict, now: datetime) -> Item:
    crm, today = case["crm"], now.date()
    first_day = date.fromisoformat(crm["first_day"])
    docs_ok = crm.get("documents") == "received"
    days = (first_day - today).days

    it = Item(case["id"], TYPE_TITLE["onboarding"], NONE, "", end_of(first_day - timedelta(days=1)))
    it.crm.append(f"Первый выход: {rel_day(first_day, today)}.")
    it.crm.append("Документы получены." if docs_ok else "Документы не получены (статус «ожидаются»).")

    if docs_ok:
        it.reason = "документы в CRM есть"
        return it

    it.priority = by_days_left(days)
    it.reason = f"выход {rel_day(first_day, today)}, документов в CRM нет"

    sigs = case_signals(case)
    for m in case.get("chat", []):
        it.chat.append(quote(m))
    if "docs_sent" in sigs:
        where = " в личные сообщения, а не в рабочий канал оформления" if "private_channel" in sigs else ""
        it.chat.append(f"По словам сотрудника, документы отправлены руководителю{where}. В CRM их нет.")
        it.check.append("Действительно ли документы у руководителя, полный ли комплект и читаемы ли фото.")
        it.actions.append("Написать руководителю: переслать документы в канал оформления (или загрузить в CRM) сегодня.")
    else:
        it.actions.append("Написать сотруднику: документы нужны до первого выхода, прислать в канал оформления.")
    it.actions.append("После получения проверить комплект и сменить статус документов в CRM.")
    it.actions.append("Если до конца дня документов нет, предупредить руководителя о риске переноса выхода.")
    return it


def offboarding(case: dict, now: datetime) -> Item:
    crm, today = case["crm"], now.date()
    last_day = date.fromisoformat(crm["last_day"])
    confirmed = bool(crm.get("manager_confirmed"))
    days = (last_day - today).days

    it = Item(case["id"], TYPE_TITLE["offboarding"], NONE, "", end_of(last_day))
    it.crm.append(f"Последний рабочий день: {rel_day(last_day, today)}.")
    it.crm.append("Подтверждение руководителя есть." if confirmed else "Подтверждения ответственного руководителя нет.")

    if confirmed:
        it.reason = "подтверждение руководителя в CRM есть"
        return it

    it.priority = by_days_left(days)
    it.reason = f"последний день {rel_day(last_day, today)}, нет подтверждения руководителя"

    sigs = case_signals(case)
    for m in case.get("chat", []):
        it.chat.append(quote(m))
    if "manager_aware" in sigs:
        it.chat.append("Сотрудник говорит, что руководитель в курсе. Это слова сотрудника, а не подтверждение руководителя.")
    it.check.append("Подтверждает ли увольнение сам руководитель: письменно, в рабочем канале или в CRM.")
    it.actions.append("Связаться с руководителем напрямую в первой половине дня и получить подтверждение.")
    it.actions.append("Зафиксировать подтверждение в CRM.")
    it.actions.append("Проверить, что закрывающие шаги по регламенту успеют выполнить сегодня.")
    return it


def card_delivery(case: dict, now: datetime) -> Item:
    crm, today = case["crm"], now.date()
    status = crm.get("status")
    it = Item(case["id"], TYPE_TITLE["card_delivery"], NONE, "", now)

    msgs = case.get("chat", [])
    sigs = case_signals(case)
    for m in msgs:
        it.chat.append(quote(m))

    if status == "accepted":
        at = datetime.fromisoformat(crm["accepted_at"])
        it.deadline = at
        it.crm.append(f"Статус «Принято», приёмка подтверждена {rel_day(at.date(), today)} в {at:%H:%M}.")
        if "problem" in sigs:
            it.priority, it.reason = HIGH, "в CRM принято, но в чате пишут о проблеме с картами"
            it.check.append("Что именно не так с картами и совпадает ли количество с накладной.")
            it.actions.append("Связаться с получателем, зафиксировать проблему, при необходимости открыть претензию.")
        else:
            it.reason = "приёмка подтверждена в CRM"
            if "received" in sigs:
                it.chat.append("Переписка совпадает с CRM.")
        return it

    if status == "in_transit":
        planned = datetime.fromisoformat(crm["planned_by"])
        it.deadline = planned
        it.crm.append(f"Статус «В пути», доставка запланирована {rel_day(planned.date(), today)} до {planned:%H:%M}.")
        overdue = now >= planned
        if "received" in sigs and "delay" not in sigs:
            it.priority, it.reason = HIGH, "в чате пишут, что карты получены, а в CRM статус «В пути»"
            it.check.append("Получены ли карты на самом деле и почему не подтверждена приёмка в CRM.")
            it.actions.append("Попросить получателя подтвердить приёмку в CRM.")
            return it
        if overdue or "delay" in sigs:
            it.priority = HIGH
            eta = next((chat_eta(m["text"]) for m in msgs if chat_eta(m["text"])), None)
            it.reason = "срок доставки истёк" if overdue else "курьер сообщает о задержке"
            if eta:
                it.reason += f", курьер обещает не раньше {eta:%H:%M}"
                it.chat.append(f"Новое время прибытия ({eta:%H:%M}) известно только со слов курьера, в CRM его нет.")
            it.check.append("Знает ли точка о переносе и не мешает ли задержка работе точки.")
            it.actions.append("Предупредить точку о новом времени доставки.")
            it.actions.append("Обновить плановое время или оставить комментарий в CRM.")
            it.actions.append(
                f"Проверить статус в {eta:%H:%M}: если не «Принято», связаться с курьером."
                if eta else "Уточнить у курьера время прибытия и поставить контроль."
            )
            return it
        it.priority, it.reason = NORMAL, f"в пути, срок до {planned:%H:%M} ещё не наступил"
        it.actions.append(f"Проверить статус после {planned:%H:%M}.")
        return it

    return manual(case, f"неизвестный статус доставки в CRM: {status!r}")


def manual(case: dict, why: str) -> Item:
    """Случай, который правила не поняли: не теряем, отдаём человеку."""
    it = Item(case.get("id", "?"), TYPE_TITLE.get(case.get("type"), case.get("type") or "Неизвестный случай"),
              HIGH, f"не удалось разобрать автоматически: {why}", datetime.max)
    it.crm.append(f"Данные CRM: {json.dumps(case.get('crm', {}), ensure_ascii=False)}")
    it.chat.extend(quote(m) for m in case.get("chat", []))
    it.actions.append("Разобрать вручную.")
    return it


HANDLERS = {"onboarding": onboarding, "offboarding": offboarding, "card_delivery": card_delivery}


def process(case: dict, now: datetime) -> Item:
    handler = HANDLERS.get(case.get("type"))
    if handler is None:
        return manual(case, f"неизвестный тип {case.get('type')!r}")
    try:
        return handler(case, now)
    except (KeyError, ValueError, TypeError) as e:
        return manual(case, f"неполные или некорректные данные ({e})")


def build(cases: list[dict], now: datetime) -> list[Item]:
    items = [process(c, now) for c in cases]
    items.sort(key=lambda it: (it.priority, it.deadline))
    return items


# --- вывод -----------------------------------------------------------------------

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def render(items: list[Item], now: datetime) -> str:
    out = [f"# Сводка support-отдела: {WEEKDAYS[now.weekday()]} {now:%d.%m.%Y}, {now:%H:%M}", ""]
    active = [it for it in items if it.priority != NONE]
    if active:
        first = active[0]
        out.append(f"**Начать с {first.case_id}** ({first.title.lower()}): {first.reason}.")
        if len(active) > 1:
            out.append("")
            out.append("Затем: " + ", ".join(it.case_id for it in active[1:]) + ".")
        out.append("")
    else:
        out += ["**Срочных вопросов нет.**", ""]

    out += ["| Приоритет | Случай | Почему |", "|---|---|---|"]
    out += [f"| {PRIORITY_LABEL[it.priority]} | {it.case_id} {it.title} | {it.reason} |" for it in items]
    out.append("")

    for it in items:
        out += [f"## {it.case_id}. {it.title}: {PRIORITY_LABEL[it.priority]}", ""]
        out += _section("Подтверждено в CRM", it.crm)
        out += _section("Известно только из переписки (не подтверждено)", it.chat)
        out += _section("Проверить перед действием", it.check)
        out += _section("Что делать", it.actions, numbered=True)
    return "\n".join(out).rstrip() + "\n"


def _section(title: str, lines: list[str], numbered: bool = False) -> list[str]:
    if not lines:
        return []
    body = [f"{i}. {s}" if numbered else f"- {s}" for i, s in enumerate(lines, 1)]
    return [f"**{title}:**", "", *body, ""]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Сводка support-отдела по данным CRM и переписке")
    p.add_argument("input", help="JSON с полями now (необязательно) и cases")
    p.add_argument("--now", help="момент формирования сводки, ISO 8601 (по умолчанию из файла или текущее время)")
    p.add_argument("-o", "--output", help="куда записать сводку (по умолчанию stdout)")
    args = p.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    now = datetime.fromisoformat(args.now or data.get("now") or datetime.now().isoformat(timespec="minutes"))
    text = render(build(data["cases"], now), now)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
