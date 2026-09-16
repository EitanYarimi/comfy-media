#!/usr/bin/env python3
"""Day-level filtering inside a month."""

import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_server  # noqa: E402


def ts(year, month, day, hour=12):
    return datetime(year, month, day, hour).timestamp()


def item(name, year, month, day, hour=12):
    return {'name': name, 'path': f'video/{name}', 'size': 1, 'modified': ts(year, month, day, hour)}


class DayKeyTests(unittest.TestCase):
    def test_day_key_matches_month_key_prefix(self):
        stamp = ts(2026, 9, 4)
        self.assertEqual(video_server.item_day_key(stamp), '2026-09-04')
        self.assertTrue(
            video_server.item_day_key(stamp).startswith(video_server.item_month_key(stamp))
        )


class DaySummaryTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            item('a.mp4', 2026, 9, 4),
            item('b.mp4', 2026, 9, 4, hour=23),
            item('c.mp4', 2026, 9, 1),
            item('d.mp4', 2026, 8, 30),
        ]

    def test_summary_is_newest_first(self):
        self.assertEqual(
            video_server.media_day_summary(self.items, '2026-09'),
            [{'day': '2026-09-04', 'count': 2}, {'day': '2026-09-01', 'count': 1}],
        )

    def test_summary_without_month_covers_everything(self):
        days = [row['day'] for row in video_server.media_day_summary(self.items)]
        self.assertEqual(days, ['2026-09-04', '2026-09-01', '2026-08-30'])

    def test_month_prefix_is_not_substring_matched(self):
        # '2026-1' must not pick up 2026-10/11/12 days.
        items = [item('oct.mp4', 2026, 10, 2), item('jan.mp4', 2026, 1, 2)]
        self.assertEqual(
            video_server.media_day_summary(items, '2026-1'),
            [],
        )


class DayFilterTests(unittest.TestCase):
    def setUp(self):
        video_server._media_by_month['videos'] = None
        self.items = [
            item('a.mp4', 2026, 9, 4),
            item('b.mp4', 2026, 9, 4, hour=8),
            item('c.mp4', 2026, 9, 1),
            item('d.mp4', 2026, 8, 30),
        ]

    def tearDown(self):
        video_server._media_by_month['videos'] = None

    def test_filter_by_day_inside_month(self):
        got = video_server.filter_media_items(
            self.items, month='2026-09', day='2026-09-04', kind='videos'
        )
        self.assertEqual(sorted(i['name'] for i in got), ['a.mp4', 'b.mp4'])

    def test_day_without_month(self):
        got = video_server.filter_media_items(self.items, day='2026-08-30', kind='videos')
        self.assertEqual([i['name'] for i in got], ['d.mp4'])

    def test_day_and_search_combine(self):
        got = video_server.filter_media_items(
            self.items, month='2026-09', day='2026-09-04', q='b', kind='videos'
        )
        self.assertEqual([i['name'] for i in got], ['b.mp4'])

    def test_paginate_respects_day(self):
        total, page = video_server.paginate_media(
            self.items, month='2026-09', day='2026-09-04', offset=0, limit=1, kind='videos'
        )
        self.assertEqual(total, 2)
        self.assertEqual(len(page), 1)

    def test_month_index_cache_is_not_polluted_by_day(self):
        video_server.filter_media_items(
            self.items, month='2026-09', day='2026-09-04', kind='videos'
        )
        month_only = video_server.filter_media_items(self.items, month='2026-09', kind='videos')
        self.assertEqual(len(month_only), 3)


class QueryParsingTests(unittest.TestCase):
    def test_day_is_parsed(self):
        parsed = video_server.parse_media_api_query({'month': ['2026-09'], 'day': ['2026-09-04']})
        force, summary, month, q, offset, limit, day = parsed
        self.assertEqual(month, '2026-09')
        self.assertEqual(day, '2026-09-04')

    def test_day_defaults_to_empty(self):
        self.assertEqual(video_server.parse_media_api_query({})[6], '')


if __name__ == '__main__':
    unittest.main()
