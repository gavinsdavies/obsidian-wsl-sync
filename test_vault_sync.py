import importlib.util
import os
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).with_name("vault_sync.py")


def load_module():
    spec = importlib.util.spec_from_file_location("vault_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VaultSyncTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_parse_output_accepts_singular_transferred_summary(self):
        _skipped, _failed, summary = self.mod.parse_output(
            "1 item transferred, 0 skipped, 0 failed",
            "",
        )

        self.assertEqual(summary, ("1", "0", "0"))

    def test_parse_output_accepts_plural_transferred_summary(self):
        _skipped, _failed, summary = self.mod.parse_output(
            "11 items transferred, 2 skipped, 3 failed",
            "",
        )

        self.assertEqual(summary, ("11", "2", "3"))

    def test_sync_count_accepts_singular_and_plural_items(self):
        singular = self.mod._SYNC_COUNT_RE.search("1 item will be synced")
        plural = self.mod._SYNC_COUNT_RE.search("11 items will be synced")

        self.assertEqual(singular.group(1), "1")
        self.assertEqual(plural.group(1), "11")

    def test_progress_bar_clamps_to_total(self):
        self.assertEqual(self.mod._progress_bar(150, 100).count("█"), 30)

    def test_color_disabled_by_no_color(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "FORCE_COLOR": "1"}):
            self.assertEqual(self.mod.color("text", "green"), "text")

    def test_color_forced_by_force_color(self):
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            self.assertEqual(
                self.mod.color("text", "green"),
                "\033[38;2;102;128;11mtext\033[0m",
            )

    def test_classify_skip_frontmatter_only(self):
        import tempfile
        mod = self.mod
        a = "---\ncreated: 2026-01-01\nupdated: 2026-06-01\n---\nsame body\n"
        b = "---\ncreated: 2026-01-01\nupdated: 2026-06-02\n---\nsame body\n"
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d, "a.md"), Path(d, "b.md")
            pa.write_text(a); pb.write_text(b)
            cls, _ = mod.classify_skip(pa, pb)
        self.assertEqual(cls, "frontmatter_only")

    def test_classify_skip_body_diff_is_none(self):
        import tempfile
        mod = self.mod
        a = "---\ncreated: 2026-01-01\n---\nbody one\n"
        b = "---\ncreated: 2026-01-01\n---\nbody two\n"
        with tempfile.TemporaryDirectory() as d:
            pa, pb = Path(d, "a.md"), Path(d, "b.md")
            pa.write_text(a); pb.write_text(b)
            cls, _ = mod.classify_skip(pa, pb)
        self.assertIsNone(cls)


if __name__ == "__main__":
    unittest.main()
