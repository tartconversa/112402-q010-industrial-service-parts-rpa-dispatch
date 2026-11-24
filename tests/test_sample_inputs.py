"""检查合成资料列名和样例关联。"""
import csv
from pathlib import Path
import unittest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
HEADERS = {'inventory': ['stock', 'partner', 'item', 'quantity', 'checked'], 'compatibility': ['part', 'model', 'firmware_from', 'firmware_to', 'replacement', 'revision'], 'tickets': ['ticket', 'asset', 'model', 'part', 'firmware', 'fault', 'warranty', 'reservation', 'dispatch']}
REFERENCES = [('tickets', 'model', 'compatibility', 'model'), ('tickets', 'part', 'inventory', 'item')]


class SampleInputs(unittest.TestCase):
    def read(self, name):
        with (SAMPLES / f"{name}.csv").open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            self.assertEqual(reader.fieldnames, HEADERS[name])
            rows = list(reader)
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn(None, row)
            self.assertNotIn(None, row.values())
        return rows

    def test_csv_files_have_documented_columns(self):
        for name in HEADERS:
            with self.subTest(name=name):
                self.read(name)

    def test_sample_references_resolve(self):
        for source, source_field, target, target_field in REFERENCES:
            with self.subTest(source=source, field=source_field, target=target):
                known = {row[target_field] for row in self.read(target)}
                observed = {row[source_field] for row in self.read(source)}
                self.assertTrue(observed)
                self.assertEqual(observed - known, set())


if __name__ == "__main__":
    unittest.main()
