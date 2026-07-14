"""
Discord integration for UNCHAINED â€” notifications via webhook + remote control via slash commands.

Two modes (can run independently):
  - Webhook: simple one-way POST notifications (no bot account needed)
  - Bot: full slash command interface (/status, /stop, /cycle, /brain, /vpn)

Both are optional â€” if config values are empty, nothing runs.
"""

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone


_CREATE_NO_WINDOW = 0x08000000
logger = logging.getLogger('unchained.discord')


class BotBridge:
    """Shared state that both the BotWorker and DiscordBot can access safely."""

    def __init__(self):
        self._lock = threading.Lock()

        self._status = {
            'state': 'idle',
            'cycle': 0,
            'harvested': 0,
            'planted': 0,
            'errors': 0,
            'uptime': 0,
            'vpn': 'disconnected',
            'session_elapsed': '0:00',
            'last_cycle_result': '',
        }

        self._memory = None
        self._start_time = 0.0
        self._vpn_manager = None
        self._vpn_request = None
        self._vpn_result = None
        self._vpn_request_lock = threading.Lock()
        self._llm_engine = None

    def set_memory(self, memory):
        self._memory = memory

    def set_llm_engine(self, llm_engine):
        self._llm_engine = llm_engine

    def set_events(self, one_cycle_event=None, stop_event=None):

        self._one_cycle_event = one_cycle_event
        self._stop_event = stop_event

    def request_one_cycle(self):
        if self._one_cycle_event:

            self._one_cycle_event.set()

    def request_stop(self):
        if self._stop_event:

            self._stop_event.set()

    def update_status(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)
            if 'state' in kwargs and kwargs['state'] == 'bot_running' and self._start_time == 0:
                self._start_time = time.time()

    def reset_uptime(self):
        with self._lock:
            self._start_time = time.time()

    def get_status(self):
        with self._lock:
            s = dict(self._status)

        if self._start_time > 0:
            elapsed = time.time() - self._start_time
            h = int(elapsed // 3600)
            m = int(elapsed % 3600 // 60)
            s['uptime'] = f"{h}h{m}m"
        return s

    def get_brain_summary(self):
        if not self._memory:
            return "Brain not available"
        try:
            s = self._memory.summary()

            lines = [
                f"Sessions: {s.get('total_sessions', 0)}",
                f"Seeds tracked: {s.get('total_seeds', 0)}",
                f"Gardens: {s.get('total_gardens', 0)}",
                f"Detections: {s.get('total_detections', 0)}",
                f"Profiles: {s.get('total_profiles', 0)}",
                f"Last: {s.get('last_session', 'N/A')}",
            ]

            if hasattr(self, '_llm_engine') and self._llm_engine and self._llm_engine.available:
                try:
                    llm_summary = self._llm_engine.summarize_brain(s)
                    if llm_summary:
                        lines.append("")
                        lines.append("AI Analysis:")
                        lines.append(llm_summary)
                except Exception:
                    pass

            return "\n".join(lines)
        except Exception as e:
            return f"Brain error: {e}"

    def set_vpn_manager(self, manager):
        self._vpn_manager = manager

    def request_vpn(self, action, **kwargs):
        """Request a VPN operation and block until the worker processes it."""
        with self._vpn_request_lock:
            self._vpn_request = {'action': action, **kwargs}
            self._vpn_result = None
        event = threading.Event()
        self._vpn_request_event = event
        event.wait(timeout=60)
        with self._vpn_request_lock:
            result = self._vpn_result
            self._vpn_request = None
            self._vpn_result = None
            self._vpn_request_event = None
        return result

    def check_vpn_request(self):
        """Called by BotWorker to check for pending VPN requests. Returns (action, kwargs) or None."""
        with self._vpn_request_lock:
            req = self._vpn_request
            if req is None:
                return None
            return req['action'], {k: v for k, v in req.items() if k != 'action'}

    def complete_vpn_request(self, result):
        """Called by BotWorker after handling a VPN request."""
        with self._vpn_request_lock:
            self._vpn_result = result
            event = getattr(self, '_vpn_request_event', None)
            if event:
                event.set()

    def get_vpn_servers(self):
        if not self._vpn_manager:
            return []
        return self._vpn_manager.get_servers()

    def get_vpn_status(self):
        if not self._vpn_manager:
            return False, None
        return self._vpn_manager.get_state()


class DiscordWebhook:
    """One-way Discord notification via webhook URL.

Usage:
    w = DiscordWebhook("https://discord.com/api/webhooks/...")
    w.send("Hello from UNCHAINED")
    w.notify_cycle(5, harvested=3, planted=1, errors=0)
"""

    def __init__(self, webhook_url=None):
        self._url = webhook_url or ''
        self._session = None

    @property
    def configured(self):

        return bool(self._url)

    def configure(self, webhook_url):

        self._url = webhook_url or ''

    def send(self, message):

        if not self._url:
            return
        try:
            import urllib.request
            import json
            data = json.dumps({'content': message}).encode('utf-8')
            req = urllib.request.Request(
                self._url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    logger.warning(f"Webhook HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Webhook send failed: {e}")

    def send_embed(self, title, description, color=4890367, fields=None):

        if not self._url:
            return
        try:
            import urllib.request
            import json
            embed = {'title': title, 'description': description, 'color': color}
            if fields:
                embed['fields'] = [{'name': k, 'value': v, 'inline': True} for k, v in fields.items()]
            data = json.dumps({'embeds': [embed]}).encode('utf-8')
            req = urllib.request.Request(
                self._url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    logger.warning(f"Webhook HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Webhook embed failed: {e}")

    def notify_cycle(self, cycle, harvested=0, planted=0, errors=0, elapsed=''):

        self.send_embed(
            title=f"Cycle {cycle} Complete",
            description=f"Session elapsed: {elapsed}",
            color=4890367 if errors == 0 else 16739146,
            fields={
                'Harvested': str(harvested),
                'Planted': str(planted),
                'Errors': str(errors),
            }
        )

    def notify_error(self, error_msg):

        self.send_embed(
            title='âš  Error',
            description=f'```{error_msg[:500]}```',
            color=16729156,
        )

    def notify_daily_summary(self, summary_text):

        self.send_embed(
            title='UNCHAINED â€” Daily Summary',
            description=summary_text[:2000],
            color=10181046,
        )

    def close(self):
        pass


class DiscordBot:
    """Full Discord bot with slash commands. Runs in its own thread.

Requires a Bot token. Sends notifications to a specific channel.
Slash commands control the bot via BotBridge.
"""

    def __init__(self, token='', channel_id=0, bridge=None):

        self._token = token
        self._channel_id = channel_id
        self._bridge = bridge or BotBridge()
        self._thread = None
        self._loop = None
        self._bot = None
        self._running = False
        self._ready = threading.Event()

    @property
    def configured(self):

        return bool(self._token) and self._channel_id > 0

    def configure(self, token, channel_id):

        self._token = token
        self._channel_id = channel_id

    def start(self):

        if self._running or not self.configured:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_bot, daemon=True)
        self._thread.start()

    def stop(self):

        self._running = False
        if self._loop and self._bot:
            try:

                asyncio.run_coroutine_threadsafe(self._bot.close(), self._loop)

            except Exception:
                pass

    def _run_bot(self):


        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_start())
        except Exception as e:
            logger.error(f"Discord bot error: {e}")
        finally:
            self._loop.close()
            self._running = False

    async def _async_start(self):

        import discord
        from discord.ext import commands

        intents = discord.Intents.default()

        self._bot = commands.Bot(command_prefix=None, intents=intents)

        bridge = self._bridge

        @self._bot.tree.command(name='status', description='Current bot status')
        async def _status(ctx):
            if not bridge:
                await ctx.response.send_message('Bridge not connected')
                return
            s = bridge.get_status()
            embed = discord.Embed(
                title='UNCHAINED Status',
                color=4890367,
            )
            embed.add_field(name='State', value=s.get('state', '?'), inline=True)
            embed.add_field(name='Cycle', value=str(s.get('cycle', 0)), inline=True)
            embed.add_field(name='Uptime', value=s.get('uptime', '0m'), inline=True)
            embed.add_field(name='Harvested', value=str(s.get('harvested', 0)), inline=True)
            embed.add_field(name='Planted', value=str(s.get('planted', 0)), inline=True)
            embed.add_field(name='Errors', value=str(s.get('errors', 0)), inline=True)
            embed.add_field(name='VPN', value=s.get('vpn', '?'), inline=True)
            embed.add_field(name='Session', value=s.get('session_elapsed', '0:00'), inline=True)
            await ctx.response.send_message(embed=embed)


        @self._bot.tree.command(name='stop', description='Stop the bot immediately')
        async def _stop(ctx):
            if not bridge:
                await ctx.response.send_message('Bridge not connected')
                return
            s = bridge.get_status()
            if s.get('state') in ('bot_running', 'tasks_running'):
                bridge.request_stop()
                await ctx.response.send_message('Stop requested — bot will halt on next cycle boundary')
            else:
                await ctx.response.send_message('Bot is not running')


        @self._bot.tree.command(name='cycle', description='Trigger a one-shot cycle')
        async def _cycle(ctx):
            if not bridge:
                await ctx.response.send_message('Bridge not connected')
                return
            bridge.request_one_cycle()
            await ctx.response.send_message('One-shot cycle triggered')


        @self._bot.tree.command(name='brain', description='Knowledge graph summary')
        async def _brain(ctx):
            if not bridge:
                await ctx.response.send_message('Bridge not connected')
                return
            summary = bridge.get_brain_summary()
            embed = discord.Embed(
                title='UNCHAINED Brain',
                description=f'```{summary}```',
                color=10181046,
            )
            await ctx.response.send_message(embed=embed)


        @self._bot.tree.command(name='vpn', description='VPN operations: status, servers, connect, disconnect, rotate')
        async def _vpn(ctx, action: str = 'status', server: str = ''):
            if not bridge:
                await ctx.response.send_message('Bridge not connected')
                return
            action = action.lower().strip()

            if action == 'status':
                running, name = bridge.get_vpn_status()
                state = 'Connected' if running else 'Disconnected'
                color = 4890367 if running else 16739146
                embed = discord.Embed(title='VPN Status', color=color)
                embed.add_field(name='State', value=state, inline=True)
                embed.add_field(name='Server', value=name or 'N/A', inline=True)
                await ctx.response.send_message(embed=embed)

            elif action == 'servers':
                servers = bridge.get_vpn_servers()
                if not servers:
                    await ctx.response.send_message('No VPN configs found. Place .conf files in the vpn_configs folder.')
                    return
                lines = []
                for i, s in enumerate(servers):
                    tier = 'Free' if s.get('tier') == 0 else 'Paid'
                    lines.append(f"`{i+1}` **{s.get('name', '?')}** ({s.get('country', '??')}) â€” {tier}")
                embed = discord.Embed(
                    title='Available VPN Servers',
                    description='\n'.join(lines[:20]),
                    color=3447003,
                )
                await ctx.response.send_message(embed=embed)

            elif action == 'connect':
                if not server:
                    await ctx.response.send_message('Usage: `/vpn connect <server number or name>`')
                    return
                servers = bridge.get_vpn_servers()
                if not servers:
                    await ctx.response.send_message('No VPN configs found.')
                    return
                target = None
                if server.isdigit():
                    idx = int(server) - 1
                    if 0 <= idx < len(servers):
                        target = servers[idx]
                else:
                    for s in servers:
                        if server.lower() in s.get('name', '').lower():
                            target = s
                            break
                if not target:
                    await ctx.response.send_message(f'Server "{server}" not found. Use `/vpn servers` to list.')
                    return
                await ctx.response.send_message(f'Connecting to **{target.get("name", "?")}**...')
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: bridge.request_vpn('connect', server=target)
                )
                if result and result[0]:
                    await ctx.edit_original_response(content=f'Connected to **{target.get("name", "?")}**')
                else:
                    err = result[1] if result else 'Unknown error'
                    await ctx.edit_original_response(content=f'Connection failed: {err}')

            elif action == 'disconnect':
                await ctx.response.send_message('Disconnecting VPN...')
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: bridge.request_vpn('disconnect')
                )
                if result and result[0]:
                    await ctx.edit_original_response(content='VPN disconnected')
                else:
                    err = result[1] if result else 'Unknown error'
                    await ctx.edit_original_response(content=f'Disconnect failed: {err}')

            elif action == 'rotate':
                servers = bridge.get_vpn_servers()
                if not servers:
                    await ctx.response.send_message('No VPN configs available to rotate to.')
                    return
                import random
                target = random.choice(servers)
                await ctx.response.send_message(f'Rotating to **{target.get("name", "?")}**...')
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: bridge.request_vpn('connect', server=target)
                )
                if result and result[0]:
                    await ctx.edit_original_response(content=f'Rotated to **{target.get("name", "?")}**')
                else:
                    err = result[1] if result else 'Unknown error'
                    await ctx.edit_original_response(content=f'Rotation failed: {err}')

            else:
                await ctx.response.send_message(
                    f'Unknown action `{action}`. Use: `status`, `servers`, `connect`, `disconnect`, `rotate`'
                )


        @self._bot.tree.command(name='changelog', description='Show recent git changelogs')
        async def _changelog(ctx, count: int = 10):
            if not bridge:
                await ctx.response.send_message('Bridge not connected')
                return
            commits = DiscordBot._get_git_log(min(count, 20))
            if not commits:
                await ctx.response.send_message('No changelog available.')
                return
            lines = [f"`{c['hash']}` {c['message']} ({c['date']})" for c in commits]
            import discord as _discord
            embed = _discord.Embed(
                title='UNCHAINED â€” Recent Changelogs',
                description='\n'.join(lines)[:2000],
                color=3447003,
            )
            embed.add_field(name='Total', value=f'{len(commits)} commits', inline=True)
            embed.add_field(name='Repo', value='github.com/shade2363/UNCHAINED', inline=True)
            await ctx.response.send_message(embed=embed)


        @self._bot.event
        async def on_ready():
            logger.info(f'Discord bot logged in as {self._bot.user}')
            self._ready.set()

            try:
                synced = await self._bot.tree.sync()
                logger.info(f'Synced {len(synced)} slash commands')
            except Exception as e:
                logger.warning(f'Command sync failed: {e}')

            channel = self._bot.get_channel(self._channel_id)
            if channel:
                try:
                    await channel.send('UNCHAINED bot online')
                except Exception:
                    pass

        await self._bot.start(self._token)

    def send_message(self, content):

        if not self._running or not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._async_send(content), self._loop)
        except Exception as e:
            logger.warning(f"Discord send failed: {e}")

    def send_embed(self, title, description, color=4890367, fields=None):

        if not self._running or not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._async_send_embed(title, description, color, fields),
                self._loop,
            )
        except Exception as e:
            logger.warning(f"Discord embed failed: {e}")

    async def _async_send(self, content):

        if not self._bot or not self._bot.is_ready():
            return
        channel = self._bot.get_channel(self._channel_id)
        if channel:
            await channel.send(content)

    async def _async_send_embed(self, title, description, color, fields):

        if not self._bot or not self._bot.is_ready():
            return
        channel = self._bot.get_channel(self._channel_id)
        if channel:
            import discord
            embed = discord.Embed(title=title, description=description, color=color)
            if fields:
                for k, v in fields.items():
                    embed.add_field(name=k, value=v, inline=True)
            await channel.send(embed=embed)

    def notify_cycle(self, cycle, harvested=0, planted=0, errors=0, elapsed=''):

        self.send_embed(
            title=f"Cycle {cycle} Complete",
            description=f"Session elapsed: {elapsed}",
            color=4890367 if errors == 0 else 16739146,
            fields={
                'Harvested': str(harvested),
                'Planted': str(planted),
                'Errors': str(errors),
            }
        )

    def notify_error(self, error_msg):

        self.send_embed(
            title='âš  UNCHAINED Error',
            description=f'```{error_msg[:500]}```',
            color=16729156,
        )

    def notify_daily_summary(self, summary_text):

        self.send_embed(
            title='UNCHAINED â€” Daily Summary',
            description=summary_text[:2000],
            color=10181046,
        )

    @staticmethod
    def _get_git_log(count=10):
        import sys as _sys
        if getattr(_sys, 'frozen', False):
            repo_dir = os.path.dirname(_sys.executable)
        else:
            repo_dir = os.path.dirname(__file__)
        try:
            result = subprocess.run(
                ['git', 'log', f'-{count}', '--format=%h|%s|%ai'],
                capture_output=True, text=True, timeout=10,
                cwd=repo_dir, creationflags=_CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                return None
            commits = []
            for line in result.stdout.strip().splitlines():
                parts = line.split('|', 2)
                if len(parts) == 3:
                    commits.append({
                        'hash': parts[0],
                        'message': parts[1],
                        'date': parts[2].split(' ')[0],
                    })
            return commits
        except Exception as e:
            logger.warning(f"Git log failed: {e}")
            return None

    def notify_changelog(self, count=10):
        commits = self._get_git_log(count)
        if not commits:
            self.send_message('No changelog available.')
            return
        lines = [f"`{c['hash']}` {c['message']} ({c['date']})" for c in commits]
        self.send_embed(
            title='UNCHAINED â€” Recent Changelogs',
            description='\n'.join(lines)[:2000],
            color=3447003,
            fields={'Total': f'{len(commits)} commits', 'Repo': 'github.com/shade2363/UNCHAINED'},
        )
