"""Uji perbaikan hasil review: rollover tanggal transparan, carry subset tool, kandidat ganda tidak ditebak,
peringkat evaluasi jujur, pemutus error beruntun. Offline."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eval_models as em  # noqa: E402
import eval_scenarios as es  # noqa: E402
from agent import Agent  # noqa: E402
from providers.base import ChatResponse, ServerError, ToolCall  # noqa: E402
from tests.test_fragmen_a import Base  # noqa: E402
from tools.select import select_groups  # noqa: E402

AV = {"tugas", "catatan", "pengetahuan", "kuis"}


class SelectCarryTests(unittest.TestCase):
    def test_carry_only_for_short_unrecognized_replies(self):
        self.assertEqual(select_groups("ya yang pertama", AV, {"tugas"}), frozenset({"tugas"}))
        self.assertIsNone(select_groups("tolong susun lima pertanyaan buat aku belajar dari bab dua yang tadi", AV, {"tugas"}))
        self.assertEqual(select_groups("jelaskan 3NF", AV, {"tugas"}), frozenset({"tugas", "pengetahuan"}))


class TaskDateTests(Base):
    def test_rollover_is_announced_for_add_and_update(self):
        out, err = self.call("add_task", title="Kuis algoritma", due_date="2026-11-01")
        self.assertFalse(err)
        self.assertIn("2027-11-01", out)
        self.assertIn("CATATAN", out)
        out, _ = self.call("update_task", title="kuis algoritma", due_date="2026-11-02")
        self.assertIn("2027-11-02", out)
        self.assertIn("CATATAN", out)

    def test_unrecoverable_past_date_error_does_not_tell_update_to_call_add(self):
        self.call("add_task", title="Laporan", due_date="2026-11-13")
        out, err = self.call("update_task", title="laporan", due_date="2024-01-01")
        self.assertTrue(err)
        self.assertNotIn("add_task", out)


class AmbiguityGuardTests(Base):
    def test_model_cannot_guess_after_candidate_list(self):
        self.call("add_task", title="Kuis 1", due_date="2026-11-12")
        self.call("add_task", title="Kuis 2", due_date="2026-11-13")
        script = [ChatResponse(tool_calls=[ToolCall("complete_task", {"title": "kuis"})]),
                  ChatResponse(tool_calls=[ToolCall("complete_task", {"task_id": 1})]),     # menebak
                  ChatResponse(text="Kuis yang mana yang sudah selesai, Kuis 1 atau Kuis 2?")]
        a = Agent(self.reg, lambda m, s: script.pop(0), today_fn=self.ctx.today)
        ans = a.ask("kuis udah selesai")
        self.assertIn("yang mana", ans)
        self.assertEqual(len(self.ctx.tasks.list("pending")), 2)          # tidak ada yang diselesaikan


def _res(model, statuses):
    return em.ModelReport(model, [em.Result(f"s{i}", s) for i, s in enumerate(statuses)])


class EvalReportTests(unittest.TestCase):
    def test_errors_do_not_inflate_ranking(self):
        flaky = _res("flaky", [em.PASS] * 10 + [em.ERROR] * 12)
        solid = _res("solid", [em.PASS] * 21 + [em.FAIL])
        rows = em.summary_rows([flaky, solid])
        self.assertEqual([r[0] for r in rows], ["solid", "flaky"])
        self.assertIn("(10/22)", rows[1][5])

    def test_streak_halts_model_and_saves_requests(self):
        class Boom:
            def chat(self, messages, tools=None):
                raise ServerError("500 INTERNAL")
        scs = list(es.SCENARIOS)[:6]
        rep = em.eval_model("m", scs, provider_factory=lambda m: (Boom(), Boom()),
                            pacer_factory=lambda m: em.NullPacer())
        self.assertEqual([r.status for r in rep.results], [em.ERROR] * 3 + [em.SKIP] * 3)
        self.assertEqual(rep.total_requests, 3)


if __name__ == "__main__":
    unittest.main()
