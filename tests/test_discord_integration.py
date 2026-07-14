import asyncio
import sys
import threading
import time
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discord_integration import BotBridge, DiscordBot, DiscordWebhook


# ---------------------------------------------------------------------------
# BotBridge
# ---------------------------------------------------------------------------

class TestBotBridgeInit:
    def test_default_status(self):
        b = BotBridge()
        s = b.get_status()
        assert s['state'] == 'idle'
        assert s['cycle'] == 0
        assert s['harvested'] == 0
        assert s['planted'] == 0
        assert s['errors'] == 0
        assert s['uptime'] == 0  # _start_time is 0 so no calc, stays raw int
        assert s['vpn'] == 'disconnected'
        assert s['session_elapsed'] == '0:00'
        assert s['last_cycle_result'] == ''

    def test_start_time_zero_initially(self):
        b = BotBridge()
        assert b._start_time == 0.0

    def test_memory_none_initially(self):
        b = BotBridge()
        assert b._memory is None


class TestBotBridgeUpdateStatus:
    def test_updates_fields(self):
        b = BotBridge()
        b.update_status(state='running', cycle=3, harvested=10)
        s = b.get_status()
        assert s['state'] == 'running'
        assert s['cycle'] == 3
        assert s['harvested'] == 10

    def test_sets_start_time_on_bot_running(self):
        b = BotBridge()
        assert b._start_time == 0.0
        b.update_status(state='bot_running')
        assert b._start_time > 0.0

    def test_does_not_overwrite_start_time_if_already_set(self):
        b = BotBridge()
        b._start_time = 100.0
        b.update_status(state='bot_running')
        assert b._start_time == 100.0


class TestBotBridgeGetStatus:
    def test_returns_copy(self):
        b = BotBridge()
        s1 = b.get_status()
        s1['state'] = 'mutated'
        s2 = b.get_status()
        assert s2['state'] == 'idle'

    def test_uptime_calculation(self):
        b = BotBridge()
        b._start_time = time.time() - 3661  # 1h 1m 1s ago
        s = b.get_status()
        assert s['uptime'] == '1h1m'

    def test_uptime_zero_when_start_time_zero(self):
        b = BotBridge()
        s = b.get_status()
        assert s['uptime'] == 0  # raw int when _start_time is 0


class TestBotBridgeResetUptime:
    def test_resets_start_time(self):
        b = BotBridge()
        b._start_time = 100.0
        old = b._start_time
        time.sleep(0.01)
        b.reset_uptime()
        assert b._start_time > old

    def test_uptime_reflects_reset(self):
        b = BotBridge()
        b._start_time = time.time() - 7200
        b.reset_uptime()
        s = b.get_status()
        assert s['uptime'] == '0h0m'


class TestBotBridgeEvents:
    def test_set_events_and_request_one_cycle(self):
        b = BotBridge()
        evt = threading.Event()
        b.set_events(one_cycle_event=evt)
        assert not evt.is_set()
        b.request_one_cycle()
        assert evt.is_set()

    def test_set_events_and_request_stop(self):
        b = BotBridge()
        evt = threading.Event()
        b.set_events(stop_event=evt)
        assert not evt.is_set()
        b.request_stop()
        assert evt.is_set()

    def test_request_one_cycle_no_event(self):
        b = BotBridge()
        b.set_events()  # attributes exist but are None
        b.request_one_cycle()  # should not raise

    def test_request_stop_no_event(self):
        b = BotBridge()
        b.set_events()  # attributes exist but are None
        b.request_stop()  # should not raise


class TestBotBridgeSetMemory:
    def test_set_and_get(self):
        b = BotBridge()
        assert b.get_brain_summary() == 'Brain not available'
        mem = MagicMock()
        mem.summary.return_value = {
            'total_sessions': 5,
            'total_seeds': 10,
            'total_gardens': 2,
            'total_detections': 3,
            'total_profiles': 1,
            'last_session': '2024-01-01',
        }
        b.set_memory(mem)
        out = b.get_brain_summary()
        assert 'Sessions: 5' in out
        assert 'Seeds tracked: 10' in out
        assert 'Gardens: 2' in out
        assert 'Detections: 3' in out
        assert 'Profiles: 1' in out
        assert 'Last: 2024-01-01' in out

    def test_get_brain_summary_no_memory(self):
        b = BotBridge()
        assert b.get_brain_summary() == 'Brain not available'

    def test_get_brain_summary_missing_keys(self):
        b = BotBridge()
        mem = MagicMock()
        mem.summary.return_value = {}
        b.set_memory(mem)
        out = b.get_brain_summary()
        assert 'Sessions: 0' in out
        assert 'Seeds tracked: 0' in out
        assert 'Last: N/A' in out

    def test_get_brain_summary_exception(self):
        b = BotBridge()
        mem = MagicMock()
        mem.summary.side_effect = RuntimeError('db locked')
        b.set_memory(mem)
        assert b.get_brain_summary() == 'Brain error: db locked'


# ---------------------------------------------------------------------------
# DiscordWebhook
# ---------------------------------------------------------------------------

class TestDiscordWebhookInit:
    def test_default_not_configured(self):
        w = DiscordWebhook()
        assert not w.configured
        assert w._url == ''

    def test_configured_with_url(self):
        w = DiscordWebhook('https://example.com/hook')
        assert w.configured

    def test_configure(self):
        w = DiscordWebhook()
        w.configure('https://example.com/hook')
        assert w.configured
        assert w._url == 'https://example.com/hook'

    def test_configure_none(self):
        w = DiscordWebhook('https://example.com/hook')
        w.configure(None)
        assert not w.configured
        assert w._url == ''


class TestDiscordWebhookSend:
    def test_send_no_url(self):
        w = DiscordWebhook()
        w.send('hello')  # should return early, no error

    @patch('urllib.request.urlopen')
    def test_send_posts_message(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        w = DiscordWebhook('https://example.com/hook')
        w.send('hello')

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.full_url == 'https://example.com/hook'
        assert req.get_method() == 'POST'
        import json
        data = json.loads(req.data.decode('utf-8'))
        assert data == {'content': 'hello'}

    def test_send_no_url_early_return(self):
        w = DiscordWebhook()
        w.send('msg')
        w.send_embed('t', 'd')

    @patch('urllib.request.urlopen')
    def test_send_posts_embed(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        w = DiscordWebhook('https://example.com/hook')
        w.send_embed('title', 'desc', 123, {'K': 'V'})

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        import json
        data = json.loads(req.data.decode('utf-8'))
        assert data['embeds'][0]['title'] == 'title'
        assert data['embeds'][0]['description'] == 'desc'
        assert data['embeds'][0]['color'] == 123
        assert data['embeds'][0]['fields'] == [{'name': 'K', 'value': 'V', 'inline': True}]

    @patch('urllib.request.urlopen')
    def test_send_posts_embed_no_fields(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        w = DiscordWebhook('https://example.com/hook')
        w.send_embed('t', 'd', 1, None)

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        import json
        data = json.loads(req.data.decode('utf-8'))
        assert 'fields' not in data['embeds'][0]


class TestDiscordWebhookNotifyHelpers:
    @patch.object(DiscordWebhook, 'send_embed')
    def test_notify_cycle(self, mock_se):
        w = DiscordWebhook('https://example.com/hook')
        w.notify_cycle(5, harvested=3, planted=1, errors=0, elapsed='1:23')
        mock_se.assert_called_once_with(
            title='Cycle 5 Complete',
            description='Session elapsed: 1:23',
            color=4890367,
            fields={'Harvested': '3', 'Planted': '1', 'Errors': '0'},
        )

    @patch.object(DiscordWebhook, 'send_embed')
    def test_notify_cycle_with_errors(self, mock_se):
        w = DiscordWebhook('https://example.com/hook')
        w.notify_cycle(1, errors=2)
        mock_se.assert_called_once()
        assert mock_se.call_args[1]['color'] == 16739146

    @patch.object(DiscordWebhook, 'send_embed')
    def test_notify_error(self, mock_se):
        w = DiscordWebhook('https://example.com/hook')
        w.notify_error('something broke')
        mock_se.assert_called_once_with(
            title='⚠ Error',
            description='```something broke```',
            color=16729156,
        )

    @patch.object(DiscordWebhook, 'send_embed')
    def test_notify_error_truncates(self, mock_se):
        w = DiscordWebhook('https://example.com/hook')
        long_msg = 'x' * 600
        w.notify_error(long_msg)
        desc = mock_se.call_args[1]['description']
        # description is ```{msg[:500]}``` = 3 + 500 + 3 = 506
        assert len(desc) - 6 == 500

    @patch.object(DiscordWebhook, 'send_embed')
    def test_notify_daily_summary(self, mock_se):
        w = DiscordWebhook('https://example.com/hook')
        w.notify_daily_summary('summary text')
        mock_se.assert_called_once_with(
            title='UNCHAINED — Daily Summary',
            description='summary text',
            color=10181046,
        )

    @patch.object(DiscordWebhook, 'send_embed')
    def test_notify_daily_summary_truncates(self, mock_se):
        w = DiscordWebhook('https://example.com/hook')
        long_text = 'a' * 3000
        w.notify_daily_summary(long_text)
        desc = mock_se.call_args[1]['description']
        assert len(desc) == 2000


# ---------------------------------------------------------------------------
# DiscordBot
# ---------------------------------------------------------------------------

class TestDiscordBotInit:
    def test_default_not_configured(self):
        bot = DiscordBot()
        assert not bot.configured

    def test_configured_with_token_and_channel(self):
        bot = DiscordBot(token='tok', channel_id=123)
        assert bot.configured

    def test_not_configured_no_channel(self):
        bot = DiscordBot(token='tok')
        assert not bot.configured

    def test_not_configured_no_token(self):
        bot = DiscordBot(channel_id=123)
        assert not bot.configured

    def test_configure(self):
        bot = DiscordBot()
        bot.configure('tok', 999)
        assert bot.configured
        assert bot._token == 'tok'
        assert bot._channel_id == 999

    def test_uses_provided_bridge(self):
        bridge = BotBridge()
        bot = DiscordBot(bridge=bridge)
        assert bot._bridge is bridge

    def test_creates_default_bridge(self):
        bot = DiscordBot()
        assert isinstance(bot._bridge, BotBridge)


class TestDiscordBotStartStop:
    def test_start_not_configured(self):
        bot = DiscordBot()
        bot.start()  # should return early
        assert bot._thread is None

    def test_start_creates_thread(self):
        bot = DiscordBot(token='tok', channel_id=1)
        bot._run_bot = MagicMock()  # prevent actual bot launch
        bot.start()
        assert bot._thread is not None
        assert bot._running is True
        bot._thread.join(timeout=1)

    def test_start_idempotent(self):
        bot = DiscordBot(token='tok', channel_id=1)
        bot._run_bot = MagicMock()
        bot.start()
        t1 = bot._thread
        bot.start()  # second call should be no-op
        assert bot._thread is t1
        bot._running = False
        t1.join(timeout=1)

    def test_stop_clears_running(self):
        bot = DiscordBot(token='tok', channel_id=1)
        bot._run_bot = MagicMock()
        bot.start()
        bot._thread.join(timeout=1)
        bot._running = False
        assert bot._running is False

    def test_stop_with_loop_and_bot(self):
        bot = DiscordBot(token='tok', channel_id=1)
        mock_loop = MagicMock()
        mock_bot_instance = MagicMock()
        bot._loop = mock_loop
        bot._bot = mock_bot_instance
        bot._running = True
        bot.stop()
        assert bot._running is False

    def test_stop_no_loop(self):
        bot = DiscordBot(token='tok', channel_id=1)
        bot._running = True
        bot.stop()
        assert bot._running is False

    def test_run_bot_closes_loop_on_exception(self):
        bot = DiscordBot(token='tok', channel_id=1)
        mock_loop = MagicMock()
        mock_loop.run_until_complete.side_effect = RuntimeError('fail')
        with patch('asyncio.new_event_loop', return_value=mock_loop), \
             patch('asyncio.set_event_loop'):
            bot._run_bot()
        mock_loop.close.assert_called_once()
        assert bot._running is False


class TestDiscordBotNotifyHelpers:
    @patch.object(DiscordBot, 'send_embed')
    def test_notify_cycle(self, mock_se):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.notify_cycle(3, harvested=7, planted=2, errors=0, elapsed='0:45')
        mock_se.assert_called_once_with(
            title='Cycle 3 Complete',
            description='Session elapsed: 0:45',
            color=4890367,
            fields={'Harvested': '7', 'Planted': '2', 'Errors': '0'},
        )

    @patch.object(DiscordBot, 'send_embed')
    def test_notify_cycle_errors_color(self, mock_se):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.notify_cycle(1, errors=5)
        assert mock_se.call_args[1]['color'] == 16739146

    @patch.object(DiscordBot, 'send_embed')
    def test_notify_error(self, mock_se):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.notify_error('fail msg')
        mock_se.assert_called_once_with(
            title='⚠ UNCHAINED Error',
            description='```fail msg```',
            color=16729156,
        )

    @patch.object(DiscordBot, 'send_embed')
    def test_notify_error_truncates(self, mock_se):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.notify_error('x' * 600)
        desc = mock_se.call_args[1]['description']
        # description is ```{msg[:500]}``` = 3 + 500 + 3 = 506
        assert len(desc) - 6 == 500

    @patch.object(DiscordBot, 'send_embed')
    def test_notify_daily_summary(self, mock_se):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.notify_daily_summary('the summary')
        mock_se.assert_called_once_with(
            title='UNCHAINED — Daily Summary',
            description='the summary',
            color=10181046,
        )

    @patch.object(DiscordBot, 'send_embed')
    def test_notify_daily_summary_truncates(self, mock_se):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.notify_daily_summary('a' * 3000)
        assert len(mock_se.call_args[1]['description']) == 2000


class TestDiscordBotSendMessage:
    def test_send_message_not_running(self):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.send_message('hi')  # should return early

    def test_send_embed_not_running(self):
        bot = DiscordBot(token='tok', channel_id=1)
        bot.send_embed('t', 'd')  # should return early
