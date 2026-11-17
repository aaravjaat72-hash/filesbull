"""Offline regression suite. No production secrets, database or Telegram writes."""
import copy
import io
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import app as bot


def row(i, title="Lucent GK", **kw):
    return dict(id=i, title=title, file_name=title + ".pdf", caption="", keywords="",
                language="hi", status="indexed", file_size=1000, **kw)


class SearchTests(unittest.TestCase):
    def setUp(self):
        bot.books_changed()
        if hasattr(bot, "_DB_RESTRICTION"):
            bot._DB_RESTRICTION.update(until=0.0, reason="")

    def test_same_title_size_distinct_files_are_not_hidden(self):
        spec = dict(req=[["lucent"]], opt=[], raw="lucent")
        with patch.object(bot, "s2_fetch", return_value=[row(1), row(2)]):
            self.assertEqual(len(bot.s2_search(spec)), 2)

    def test_real_telegram_duplicates_are_hidden(self):
        spec = dict(req=[["lucent"]], opt=[], raw="lucent")
        with patch.object(bot, "s2_fetch", return_value=[row(1, telegram_file_unique_id="same"), row(2, telegram_file_unique_id="same")]):
            self.assertEqual(len(bot.s2_search(spec)), 1)

    def test_legacy_mixed_language(self):
        self.assertEqual(bot.row_lang(dict(language="mixed")), "both")

    def test_phrase_prefilter_accepts_filename_separators(self):
        clause = bot._db_or(["current affairs"])
        self.assertIn("title.ilike.*current*affairs*", clause)
        self.assertTrue(bot._t_match("current affairs", bot.normalize("Current_Affairs.pdf")))

    def test_candidate_cache_avoids_reloading_on_each_page(self):
        calls = []
        def db(*a, **k):
            calls.append(k.get("query"))
            return [row(1)]
        with patch.object(bot, "supabase", side_effect=db):
            first = bot.s2_fetch([["lucent"]])
            first[0]["title"] = "changed by caller"
            self.assertEqual(bot.s2_fetch([["lucent"]])[0]["title"], "Lucent GK")
            self.assertEqual(len(calls), 1)
            bot.books_changed()
            bot.s2_fetch([["lucent"]])
            self.assertEqual(len(calls), 2)

    def test_fuzzy_cache_and_delivery_facets(self):
        sample = row(1, language2="unused")
        sample.pop("language2")
        sample.update(language="en", keywords="kind_notes lang_en", telegram_file_unique_id="unique")
        with patch.object(bot, "supabase", return_value=[sample]) as db:
            self.assertEqual(bot.fuzzy_rows(["lucent"])[0]["language"], "en")
            bot.fuzzy_rows(["lusent"])
            self.assertEqual(db.call_count, 1)
            self.assertNotIn("content_text", db.call_args.kwargs["query"])

    def test_short_words_do_not_match_inside_long_words(self):
        self.assertFalse(bot._t_match("gd", "gdpr handbook"))
        self.assertTrue(bot._t_match("gd", bot.normalize("SSC_GD_Notes")))

    def test_all_required_terms_remain_required(self):
        spec = dict(req=[["lucent"], ["polity"]], opt=[], raw="lucent polity")
        self.assertIsNone(bot.s2_score(row(1, "Lucent Maths"), spec))
        self.assertIsNotNone(bot.s2_score(row(2, "Lucent Polity"), spec))


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        if hasattr(bot, "_DB_RESTRICTION"):
            bot._DB_RESTRICTION.update(until=0.0, reason="")

    def test_quota_failure_has_short_circuit(self):
        with patch.object(bot, "SUPABASE_URL", "https://example.supabase.co"), patch.object(bot, "SUPABASE_KEY", "test"), patch.object(bot, "http_json", side_effect=RuntimeError("HTTP 402: exceed_egress_quota")) as api:
            for _ in range(2):
                with self.assertRaises(bot.DatabaseUnavailable):
                    bot.supabase("books")
            self.assertEqual(api.call_count, 1)

    def test_health_reports_not_ok_and_skips_maintenance_when_restricted(self):
        with patch.object(bot, "SUPABASE_URL", "https://example.supabase.co"), patch.object(bot, "SUPABASE_KEY", "test"), patch.object(bot, "supabase", side_effect=bot.DatabaseUnavailable("quota")), patch.object(bot, "ensure_webhook") as webhook:
            result = bot.app.test_client().get("/")
            self.assertFalse(result.json["ok"])
            self.assertEqual(result.json["status"]["database_state"], "restricted")
            webhook.assert_not_called()

    def test_fuzzy_does_not_disguise_outage_as_empty_results(self):
        with patch.object(bot, "supabase", side_effect=bot.DatabaseUnavailable("quota")):
            with self.assertRaises(bot.DatabaseUnavailable):
                bot.fuzzy_rows(["lucent"])

    def test_webhook_quota_message_is_not_file_not_found(self):
        update = dict(update_id=987654, message=dict(chat=dict(id=123, type="private"), **{"from":dict(id=123)}, text="search lucent"))
        with patch.object(bot, "WEBHOOK_SECRET", "test-secret"), patch.object(bot, "handle_group_file", side_effect=bot.DatabaseUnavailable("quota")), patch.object(bot, "send_text") as sent, patch.object(bot, "flush_request"):
            response = bot.app.test_client().post("/api/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token":"test-secret"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("database", sent.call_args.args[1].lower())
            self.assertNotIn("nahi mili", sent.call_args.args[1])

    def test_unauthorized_webhook_still_rejected(self):
        self.assertEqual(bot.app.test_client().post("/api/webhook", json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
