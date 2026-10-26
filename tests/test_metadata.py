"""Offline checks for Graph API requests and resumable CSV enrichment."""

import csv
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import process_csv as processor
import utils


TOKEN = "fake-test-token"


def metadata(image_id):
    return {
        "image_id": image_id,
        "lat": 40.4,
        "lon": -3.7,
        "altitude": None,
        "capture_date": "2024-01-01T00:00:00.123+00:00",
        "captured_at": 1704067200123,
    }


class RequestTests(unittest.TestCase):
    def setUp(self):
        # A failed mock must never fall through to a real HTTP request or .env.
        self.network = patch.object(
            requests.sessions.Session, "request", side_effect=AssertionError("Unexpected network")
        )
        self.network.start()
        self.addCleanup(self.network.stop)
        self.dotenv = patch.object(
            utils, "load_dotenv", side_effect=AssertionError("Unexpected .env read")
        )
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)

    def test_token_is_only_in_authorization_header(self):
        request = utils.set_image_request(" 123 ", TOKEN)
        self.assertEqual(request["url"], "https://graph.mapillary.com/123")
        self.assertEqual(request["headers"], {"Authorization": f"OAuth {TOKEN}"})
        self.assertNotIn(TOKEN, request["url"])
        self.assertNotIn(TOKEN, str(request["params"]))
        self.assertIn("captured_at", request["params"]["fields"])

    def test_invalid_image_ids_are_rejected(self):
        for value in (True, 0, -1, 1.5, "", "0", "1.0", "legacy-key", "١٢"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                utils.set_image_request(value, TOKEN)

    def test_empty_and_whitespace_tokens_are_rejected(self):
        for token in ("", " ", "token with-space", "token\ninside"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                utils.set_image_request("123", token)

    def test_http_failure_is_propagated(self):
        session = Mock()
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError("denied")
        with self.assertRaises(requests.HTTPError):
            utils.connect_to_mapillary("123", TOKEN, timeout=7, session=session)
        self.assertEqual(session.get.call_args.kwargs["timeout"], 7)

    def test_coordinates_time_and_optional_altitude(self):
        session = Mock()
        session.get.return_value.json.return_value = {
            "geometry": {"type": "Point", "coordinates": [-3.7, 40.4]},
            "captured_at": 1704067200123,
        }
        result = utils.get_image_metadata("123", TOKEN, session=session)
        self.assertEqual(result, metadata("123"))

    def test_malformed_metadata_is_rejected(self):
        cases = [
            {},
            {"geometry": {"type": "LineString", "coordinates": [1, 2]}, "captured_at": 0},
            {"geometry": {"type": "Point", "coordinates": [181, 2]}, "captured_at": 0},
            {"geometry": {"type": "Point", "coordinates": [1, float("nan")]}, "captured_at": 0},
            {"geometry": {"type": "Point", "coordinates": [True, 2]}, "captured_at": 0},
            {"geometry": {"type": "Point", "coordinates": [1, 2]}, "captured_at": "yesterday"},
            {"geometry": {"type": "Point", "coordinates": [1, 2]}, "captured_at": 0, "altitude": "high"},
        ]
        for payload in cases:
            session = Mock()
            session.get.return_value.json.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                utils.get_image_metadata("123", TOKEN, session=session)


class CsvTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.input = self.directory / "input.csv"
        self.output = self.directory / "enriched.csv"
        self.status = self.directory / "enriched_status.csv"
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.object(
            requests.sessions.Session, "request", side_effect=AssertionError("Unexpected network")
        ))
        contexts.enter_context(patch.object(
            utils, "load_dotenv", side_effect=AssertionError("Unexpected .env read")
        ))
        contexts.enter_context(patch.object(
            processor, "set_image_request",
            side_effect=lambda image_id: utils.set_image_request(image_id, TOKEN),
        ))
        contexts.enter_context(redirect_stdout(StringIO()))

    def write_input(self, rows):
        with self.input.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["image_key", "image_id"])
            writer.writerows(rows)

    def read_csv(self, path):
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_numeric_ids_do_not_round_large_values(self):
        self.assertEqual(processor.numeric_id("9007199254740993.000"), "9007199254740993")
        self.assertEqual(processor.numeric_id(" 000123.0 "), "123")
        for value in (None, "", "0", "-1", "12.5", "1e3", "legacy-key", "١٢"):
            with self.subTest(value=value):
                self.assertIsNone(processor.numeric_id(value))

    def test_prepare_only_preserves_every_row_without_fetching(self):
        self.write_input([
            ("first", "9007199254740993.0"),
            ("duplicate", "9007199254740993"),
            ("123", "legacy-key"),
            ("empty", ""),
            ("fraction", "1.5"),
        ])
        original = self.input.read_bytes()
        with patch.object(processor, "fetch_metadata") as fetch:
            processor.process_csv(self.input, self.output, prepare_only=True)
        fetch.assert_not_called()
        self.assertEqual(self.input.read_bytes(), original)
        rows = self.read_csv(self.output)
        self.assertEqual(list(rows[0]), processor.OUTPUT_FIELDS)
        self.assertEqual([row["image_key"] for row in rows], ["first", "duplicate", "123", "empty", "fraction"])
        self.assertEqual(rows[0]["image_id"], "9007199254740993")
        self.assertTrue(all(row["lat"] == "" for row in rows))
        statuses = self.read_csv(self.status)
        self.assertEqual([row["status"] for row in statuses], ["pending", "pending", "skipped", "skipped", "skipped"])
        self.assertEqual(statuses[3]["reason"], "empty_image_id")

    def test_duplicate_ids_are_fetched_once_and_limit_keeps_pending_rows(self):
        self.write_input([("a", "123.0"), ("b", "123"), ("c", "456")])
        with patch.object(processor, "fetch_metadata", return_value=(metadata("123"), "")) as fetch:
            processor.process_csv(self.input, self.output, limit=1)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["123"])
        rows = self.read_csv(self.output)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["lat"], rows[1]["lat"])
        self.assertEqual(rows[0]["alt"], "")
        self.assertEqual([row["status"] for row in self.read_csv(self.status)], ["ok", "ok", "pending"])

    def test_resume_and_retry_errors_reuse_durable_successes(self):
        self.write_input([("a", "123"), ("b", "456"), ("c", "789")])
        with patch.object(processor, "fetch_metadata", side_effect=[(metadata("123"), ""), (None, "http_404")]):
            processor.process_csv(self.input, self.output, limit=2)
        with patch.object(processor, "fetch_metadata", return_value=(metadata("789"), "")) as fetch:
            processor.process_csv(self.input, self.output)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["789"])
        self.assertEqual([row["status"] for row in self.read_csv(self.status)], ["ok", "error", "ok"])
        with patch.object(processor, "fetch_metadata", return_value=(metadata("456"), "")) as fetch:
            processor.process_csv(self.input, self.output, retry_errors=True)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["456"])
        self.assertEqual([row["status"] for row in self.read_csv(self.status)], ["ok", "ok", "ok"])
        with patch.object(processor, "fetch_metadata") as fetch:
            processor.process_csv(self.input, self.output)
        fetch.assert_not_called()

    def test_interrupt_saves_completed_lookup_for_resume(self):
        self.write_input([("a", "123"), ("b", "456")])
        with patch.object(processor, "fetch_metadata", side_effect=[(metadata("123"), ""), KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                processor.process_csv(self.input, self.output)
        self.assertEqual([row["status"] for row in self.read_csv(self.status)], ["ok", "pending"])
        with patch.object(processor, "fetch_metadata", return_value=(metadata("456"), "")) as fetch:
            processor.process_csv(self.input, self.output)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["456"])

    def test_existing_output_without_checkpoint_is_not_overwritten(self):
        self.write_input([("a", "123")])
        self.output.write_text("keep this", encoding="utf-8")
        with self.assertRaises(ValueError):
            processor.process_csv(self.input, self.output, prepare_only=True)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep this")

    def test_generated_paths_cannot_alias_input(self):
        for filename in ("enriched.csv.tmp", "enriched_status.csv.tmp", "enriched.lock"):
            with self.subTest(filename=filename):
                self.input = self.directory / filename
                self.write_input([("keep-this-key", "123")])
                original = self.input.read_bytes()
                with self.assertRaises(ValueError):
                    processor.process_csv(self.input, self.output, prepare_only=True)
                self.assertEqual(self.input.read_bytes(), original)

    def test_cli_rejects_generated_path_collisions_before_touching_input(self):
        for filename in ("cli.csv.tmp", "cli_status.csv.tmp", "cli.lock"):
            with self.subTest(filename=filename):
                self.input = self.directory / filename
                output = self.directory / "cli.csv"
                self.write_input([("keep-this-key", "123")])
                original = self.input.read_bytes()
                arguments = ["process_csv.py", str(self.input), "--output", str(output), "--prepare-only"]
                with patch("sys.argv", arguments):
                    result = processor.main()
                self.assertNotEqual(result, 0)
                self.assertEqual(self.input.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
