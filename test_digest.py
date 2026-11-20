"""Тесты правил сводки. Запуск: python3 -m unittest -v"""

import copy
import json
import unittest
from datetime import datetime
from pathlib import Path

import digest as d

DATA = json.loads((Path(__file__).parent / "data" / "monday.json").read_text(encoding="utf-8"))
NOW = datetime.fromisoformat(DATA["now"])


def case(case_id):
    return copy.deepcopy(next(c for c in DATA["cases"] if c["id"] == case_id))


class FourCases(unittest.TestCase):
    def setUp(self):
        self.items = {it.case_id: it for it in d.build(DATA["cases"], NOW)}

    def test_order_and_priorities(self):
        order = [it.case_id for it in d.build(DATA["cases"], NOW)]
        self.assertEqual(order, ["С-24", "П-08", "С-17", "П-09"])
        self.assertEqual(self.items["С-24"].priority, d.CRITICAL)
        self.assertEqual(self.items["П-08"].priority, d.HIGH)
        self.assertEqual(self.items["С-17"].priority, d.HIGH)
        self.assertEqual(self.items["П-09"].priority, d.NONE)

    def test_chat_never_counts_as_crm_fact(self):
        for it in self.items.values():
            for msg in case(it.case_id)["chat"]:
                text = msg["text"].rstrip(".")
                self.assertFalse(any(text in line for line in it.crm), f"{it.case_id}: чат попал в блок CRM")
                self.assertTrue(any(text in line for line in it.chat), f"{it.case_id}: сообщение потерялось")

    def test_case_details(self):
        self.assertIn("13:00", self.items["П-08"].reason)
        self.assertTrue(any("личные сообщения" in c for c in self.items["С-17"].chat))
        self.assertTrue(any("не подтверждение руководителя" in c for c in self.items["С-24"].chat))
        self.assertFalse(self.items["П-09"].actions)

    def test_render(self):
        text = d.render(d.build(DATA["cases"], NOW), NOW)
        self.assertIn("**Начать с С-24**", text)
        self.assertIn("Затем: П-08, С-17.", text)
        self.assertEqual(text.count("## "), 4)


class Rules(unittest.TestCase):
    def test_chat_does_not_lower_priority(self):
        # Слова сотрудника «руководитель в курсе» не снимают срочность.
        c = case("С-24")
        c["chat"] = []
        self.assertEqual(d.process(c, NOW).priority, d.process(case("С-24"), NOW).priority)

    def test_crm_confirmation_removes_action(self):
        c = case("С-24")
        c["crm"]["manager_confirmed"] = True
        self.assertEqual(d.process(c, NOW).priority, d.NONE)
        c = case("С-17")
        c["crm"]["documents"] = "received"
        self.assertEqual(d.process(c, NOW).priority, d.NONE)

    def test_deadline_distance(self):
        c = case("С-17")
        c["crm"]["first_day"] = "2026-11-23"
        self.assertEqual(d.process(c, NOW).priority, d.CRITICAL)
        c["crm"]["first_day"] = "2026-11-30"
        self.assertEqual(d.process(c, NOW).priority, d.NORMAL)

    def test_delivery_not_yet_due(self):
        c = case("П-08")
        c["chat"] = []
        c["crm"]["planned_by"] = "2026-11-23T15:00"
        self.assertEqual(d.process(c, NOW).priority, d.NORMAL)

    def test_delay_raises_priority_before_deadline(self):
        c = case("П-08")
        c["crm"]["planned_by"] = "2026-11-23T12:00"
        self.assertEqual(d.process(c, NOW).priority, d.HIGH)

    def test_contradiction_chat_vs_crm(self):
        c = case("П-08")
        c["crm"]["planned_by"] = "2026-11-23T15:00"
        c["chat"] = [{"role": "recipient", "text": "Карты получили"}]
        it = d.process(c, NOW)
        self.assertEqual(it.priority, d.HIGH)
        self.assertIn("В пути", it.reason)

    def test_problem_after_acceptance(self):
        c = case("П-09")
        c["chat"] = [{"role": "recipient", "text": "Карты получили, но двух не хватает"}]
        self.assertEqual(d.process(c, NOW).priority, d.HIGH)

    def test_nothing_is_lost(self):
        weird = [
            {"id": "X-1", "type": "unknown", "crm": {}, "chat": []},
            {"id": "X-2", "type": "card_delivery", "crm": {"status": "lost"}, "chat": []},
            {"id": "X-3", "type": "offboarding", "crm": {}, "chat": []},
            {"id": "X-4", "type": "onboarding", "crm": {"first_day": "не дата"}, "chat": []},
        ]
        items = d.build(weird, NOW)
        self.assertEqual({it.case_id for it in items}, {"X-1", "X-2", "X-3", "X-4"})
        for it in items:
            self.assertEqual(it.priority, d.HIGH)
            self.assertIn("вручную", " ".join(it.actions))


class ChatRules(unittest.TestCase):
    def test_signals(self):
        cases = {
            "Вчера отправил фотографии документов руководителю в личку": {"docs_sent", "private_channel"},
            "С руководителем всё обсудили, он в курсе": {"manager_aware"},
            "Задерживаюсь, раньше 13:00 не приеду": {"delay"},
            "Карты получили, количество проверили": {"received", "count_ok"},
        }
        for text, want in cases.items():
            self.assertTrue(want <= d.chat_signals(text), f"{text!r}: {d.chat_signals(text)}")

    def test_eta(self):
        self.assertEqual(str(d.chat_eta("раньше 13:00 не приеду")), "13:00:00")
        self.assertEqual(str(d.chat_eta("буду к 9.30")), "09:30:00")
        self.assertIsNone(d.chat_eta("скоро буду"))


if __name__ == "__main__":
    unittest.main()
