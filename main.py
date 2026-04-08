from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import shlex
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from typing import Any

from config_loader import load_config


def _config_arg(value: str) -> str:
    resolved_path = os.path.abspath(value)
    if not os.path.isfile(resolved_path):
        raise argparse.ArgumentTypeError(
            f"config file does not exist: {resolved_path}. "
            "Copy config.json.example to a real config file and pass it with --config."
        )
    return resolved_path


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="config.json",
        type=_config_arg,
        help="Path to config.json",
    )
    return parser.parse_args()


CLI_ARGS = parse_cli_args()
MUSIC_CONFIG = load_config(CLI_ARGS.config)
LOG_LEVEL = str((MUSIC_CONFIG.get("logging") or {}).get("level", "INFO")).upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

import open_xiaoai_server
from music_search import MusicSearcher
from music_search import extract_play_keyword
from music_search import is_exact_command
from music_search import is_stop_play_command
from music_search import normalize_exact_keywords
from music_search import normalize_keyword
from music_service import LocalMusicHttpServer
from music_service import build_music_server
from player_control import ask_xiaoai
from player_control import play_music_url
from player_control import speak_text
from player_control import stop_playback


@dataclass
class SongItem:
    index: int
    path: str
    name: str
    url: str
    duration_sec: float


async def on_event(event: str):
    try:
        event_json = json.loads(event)
    except Exception:
        return

    if event_json.get("event") != "instruction":
        return

    raw_line = (event_json.get("data") or {}).get("NewLine")
    if not raw_line:
        return

    try:
        line = json.loads(raw_line)
    except Exception:
        return

    header = line.get("header", {})
    payload = line.get("payload", {})
    App.try_capture_reply_text(header=header, payload=payload, line=line)

    if (
        header.get("namespace") != "SpeechRecognizer"
        or header.get("name") != "RecognizeResult"
    ):
        return
    if not payload.get("is_final"):
        return

    results = payload.get("results") or []
    text = (results[0] or {}).get("text") if results else ""
    if not text:
        return

    logger.info("ASR final text: %s", text)
    is_stop = is_stop_play_command(text, App.stop_keywords)
    is_previous = App._is_previous_song_command(text)
    is_next = App._is_next_song_command(text)
    is_refresh = App._is_refresh_index_command(text)
    is_random = App._is_random_play_command(text)
    is_continue = App._is_continue_command(text)
    keyword = extract_play_keyword(text, App.play_keywords)
    is_new_play_command = bool(keyword) or is_random
    preserve_queue = not is_new_play_command
    await App.handle_user_speech_interrupt(text, preserve_queue=preserve_queue)

    if is_stop:
        App.disarm_reply_interrupt("voice stop command")
        asyncio.create_task(App.stop_music())
        return

    if is_previous:
        App.arm_reply_interrupt("voice previous song")
        asyncio.create_task(App.play_previous_song())
        return

    if is_next:
        App.arm_reply_interrupt("voice next song")
        asyncio.create_task(App.play_next_song())
        return

    if is_refresh:
        App.arm_reply_interrupt("voice refresh index")
        asyncio.create_task(App.refresh_music_index_and_reply("voice refresh index"))
        return

    if is_random:
        App.arm_reply_interrupt("voice random play")
        asyncio.create_task(App.play_random_music())
        return

    if is_continue:
        App.arm_reply_interrupt("voice continue")
        asyncio.create_task(App.resume_music())
        return

    if keyword:
        App.arm_reply_interrupt(f"voice search play:{keyword}")
        asyncio.create_task(App.play_local_music_by_keyword(keyword))


def on_event_callback(event: str):
    asyncio.run_coroutine_threadsafe(on_event(event), App.loop)


class App:
    loop: asyncio.AbstractEventLoop | None = None
    music_server: LocalMusicHttpServer | None = None
    local_music_lock = asyncio.Lock()
    play_history: list[SongItem] = []
    play_queue: list[SongItem] = []
    current_song: SongItem | None = None
    timer_task: asyncio.Task | None = None
    index_refresh_task: asyncio.Task | None = None
    index_refresh_lock = asyncio.Lock()
    last_reply_text: str = ""
    reply_interrupt_armed = False
    reply_interrupt_armed_at = 0.0
    reply_interrupt_reason = ""
    reply_interrupt_lock = asyncio.Lock()
    reply_interrupt_last_stop_at = 0.0
    whitelist_resume_task: asyncio.Task | None = None
    whitelist_resume_seq = 0

    timer_buffer_sec = float(MUSIC_CONFIG.get("timer_buffer_sec", 1.5))

    search_config = MUSIC_CONFIG.get("search", {}) or {}
    max_results = int(search_config.get("max_results", MUSIC_CONFIG.get("max_results", 50)))
    refresh_interval_sec = float(search_config.get("refresh_interval_sec", 300))
    search_index_file = str(search_config.get("index_file", ".cache/music_index.json"))
    audio_extensions = {
        str(ext).strip().lower()
        for ext in MUSIC_CONFIG.get("supported_audio_extensions", [])
        if str(ext).strip()
    }

    command_config = MUSIC_CONFIG.get("commands", {}) or {}
    play_keywords = list(command_config.get("play_keywords", []))
    stop_keywords = normalize_exact_keywords(command_config.get("stop_keywords", []))
    previous_keywords = normalize_exact_keywords(command_config.get("previous_keywords", []))
    next_keywords = normalize_exact_keywords(command_config.get("next_keywords", []))
    refresh_keywords = normalize_exact_keywords(command_config.get("refresh_keywords", []))
    random_play_keywords = normalize_exact_keywords(command_config.get("random_play_keywords", []))
    continue_keywords = normalize_exact_keywords(command_config.get("continue_keywords", []))
    interrupt_whitelist_keywords = normalize_exact_keywords(
        command_config.get("interrupt_whitelist_keywords", [])
    )
    reply_interrupt_timeout_sec = float(command_config.get("reply_interrupt_timeout_sec", 20))
    reply_interrupt_cooldown_sec = float(command_config.get("reply_interrupt_cooldown_sec", 1.2))
    auto_resume_delay_sec = float(command_config.get("auto_resume_delay_sec", 1.8))
    xiaoai_port = int((MUSIC_CONFIG.get("xiaoai") or {}).get("port", 4399))

    searcher = MusicSearcher(
        music_dirs=MUSIC_CONFIG.get("music_dirs", []) or [],
        max_results=max_results,
        extensions=audio_extensions,
        index_file=search_index_file,
        artist_separators=list(search_config.get("artist_separator", [])),
        album_separators=list(search_config.get("album_separator", [])),
    )
    ffprobe_path = shutil.which("ffprobe")

    @classmethod
    def arm_reply_interrupt(cls, reason: str):
        cls.reply_interrupt_armed = True
        cls.reply_interrupt_armed_at = time.monotonic()
        cls.reply_interrupt_reason = reason
        logger.info("reply interrupt armed: reason=%s", reason)

    @classmethod
    def disarm_reply_interrupt(cls, reason: str):
        if not cls.reply_interrupt_armed:
            return
        cls.reply_interrupt_armed = False
        logger.info(
            "reply interrupt disarmed: previous_reason=%s trigger=%s",
            cls.reply_interrupt_reason,
            reason,
        )
        cls.reply_interrupt_reason = ""

    @classmethod
    def _is_reply_interrupt_armed(cls) -> bool:
        if not cls.reply_interrupt_armed:
            return False
        now = time.monotonic()
        if now - cls.reply_interrupt_armed_at > cls.reply_interrupt_timeout_sec:
            cls.disarm_reply_interrupt("timeout")
            return False
        return True

    @classmethod
    def _is_user_interrupt_whitelisted(cls, text: str) -> bool:
        normalized = normalize_keyword(text).replace(" ", "")
        return cls._matches_any_keyword(normalized, cls.interrupt_whitelist_keywords)

    @classmethod
    def _matches_any_keyword(cls, normalized_text: str, keywords: set[str]) -> bool:
        if not normalized_text:
            return False
        for keyword in keywords:
            if not keyword:
                continue
            if normalized_text == keyword or keyword in normalized_text:
                return True
        return False

    @classmethod
    async def handle_user_speech_interrupt(cls, text: str, preserve_queue: bool = False):
        normalized = normalize_keyword(text).replace(" ", "")
        if cls._is_user_interrupt_whitelisted(text):
            cls.disarm_reply_interrupt("user speech whitelist")
            logger.info("speech interrupt matched whitelist, keep queue: %s", text)
            await cls._schedule_auto_resume_after_whitelist(normalized, text)
            return
        async with cls.local_music_lock:
            await cls._cancel_timer_unlocked()
        if preserve_queue:
            cls.disarm_reply_interrupt("user speech preserve queue")
            logger.info("speech interrupt canceled timer, queue preserved:%s", text)
            return
        cls.disarm_reply_interrupt("user speech interrupt")
        logger.info("speech interrupt canceled timer: text=%s", text)

    @classmethod
    def try_capture_reply_text(
        cls,
        header: dict[str, Any],
        payload: dict[str, Any],
        line: dict[str, Any],
    ):
        namespace = str(header.get("namespace") or "")
        name = str(header.get("name") or "")
        if namespace == "SpeechRecognizer" and name == "RecognizeResult":
            return

        texts: list[str] = []
        for source in (payload, line):
            texts.extend(cls._extract_candidate_texts(source))
        unique_texts = [item for item in dict.fromkeys(texts) if item]
        if not unique_texts:
            return

        namespace_lower = namespace.lower()
        name_lower = name.lower()
        maybe_reply_event = (
            "tts" in namespace_lower
            or "speechsynthesizer" in namespace_lower
            or "nlp" in namespace_lower
            or "dialog" in namespace_lower
            or "assistant" in namespace_lower
            or "reply" in name_lower
            or "respond" in name_lower
            or "speak" in name_lower
        )
        if not maybe_reply_event:
            return

        cls.last_reply_text = unique_texts[0]
        logger.info(
            "captured XiaoAi reply: namespace=%s name=%s text=%s",
            namespace or "-",
            name or "-",
            cls.last_reply_text,
        )
        if cls._is_reply_interrupt_armed():
            is_speak_event = "speechsynthesizer" in namespace_lower and "speak" in name_lower
            if is_speak_event:
                now = time.monotonic()
                if now - cls.reply_interrupt_last_stop_at >= cls.reply_interrupt_cooldown_sec:
                    cls.reply_interrupt_last_stop_at = now
                    asyncio.create_task(cls._interrupt_reply_playback())

    @classmethod
    def _extract_candidate_texts(cls, value: Any) -> list[str]:
        candidates: list[str] = []
        if isinstance(value, str):
            text = value.strip()
            if text:
                candidates.append(text)
            return candidates
        if isinstance(value, list):
            for item in value:
                candidates.extend(cls._extract_candidate_texts(item))
            return candidates
        if isinstance(value, dict):
            direct_keys = {
                "text",
                "reply",
                "answer",
                "content",
                "tts",
                "say",
                "speech",
                "nlp_reply",
                "reply_text",
                "display_text",
            }
            for key, item in value.items():
                key_lower = str(key).lower()
                if key_lower in direct_keys and isinstance(item, str):
                    text = item.strip()
                    if text:
                        candidates.append(text)
                if key_lower in {"payload", "data", "results", "result", "instruction", "directives", "cards"}:
                    candidates.extend(cls._extract_candidate_texts(item))
            return candidates
        return candidates

    @classmethod
    async def _interrupt_reply_playback(cls):
        async with cls.reply_interrupt_lock:
            if not cls._is_reply_interrupt_armed():
                return
            logger.info("reply interrupt hit, stopping current XiaoAi speech")
            await stop_playback()

    @classmethod
    async def _speak_text(cls, text: str):
        cls.disarm_reply_interrupt("sending speak request")
        return await speak_text(text)

    @classmethod
    async def _ask_xiaoai(cls, text: str):
        cls.disarm_reply_interrupt("sending ask request")
        return await ask_xiaoai(text)

    @classmethod
    async def _play_music_url(cls, url: str):
        cls.disarm_reply_interrupt("sending play request")
        return await play_music_url(url)

    @classmethod
    async def _schedule_auto_resume_after_whitelist(cls, normalized_text: str, raw_text: str):
        del normalized_text
        if cls.current_song is None:
            return
        cls.whitelist_resume_seq += 1
        seq = cls.whitelist_resume_seq
        if cls.whitelist_resume_task and not cls.whitelist_resume_task.done():
            cls.whitelist_resume_task.cancel()
        logger.info(
            "scheduled auto resume after whitelist command: text=%s delay=%.1fs",
            raw_text,
            cls.auto_resume_delay_sec,
        )
        cls.whitelist_resume_task = asyncio.create_task(cls._auto_resume_after_whitelist(seq))

    @classmethod
    async def _auto_resume_after_whitelist(cls, seq: int):
        try:
            await asyncio.sleep(max(cls.auto_resume_delay_sec, 0.1))
        except asyncio.CancelledError:
            return
        if seq != cls.whitelist_resume_seq:
            return
        async with cls.local_music_lock:
            if cls.current_song is None:
                return
            song = cls.current_song
            logger.info("auto resume current song after whitelist command: %s", song.name)
            await cls._cancel_timer_unlocked()
            await cls._start_song_unlocked(song, trigger="whitelist auto resume")

    @staticmethod
    def _safe_read_command_line(prompt: str = ">>> ") -> str:
        try:
            return input(prompt)
        except UnicodeDecodeError:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            raw = sys.stdin.buffer.readline()
            if raw == b"":
                raise EOFError
            encoding = sys.stdin.encoding or "utf-8"
            return raw.decode(encoding, errors="replace").rstrip("\r\n")

    @classmethod
    def _probe_wav_duration(cls, file_path: str) -> float | None:
        try:
            with wave.open(file_path, "rb") as wav_file:
                frames = wav_file.getnframes()
                frame_rate = wav_file.getframerate()
                if frame_rate <= 0:
                    return None
                return float(frames) / float(frame_rate)
        except Exception:
            return None

    @classmethod
    def _probe_ffprobe_duration(cls, file_path: str) -> float | None:
        if not cls.ffprobe_path:
            raise RuntimeError("未检测到 ffprobe，无法探测歌曲时长")
        cmd = [
            cls.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if result.returncode != 0:
                return None
            value = float((result.stdout or "").strip())
            if value <= 0:
                return None
            return value
        except Exception:
            return None

    @classmethod
    def _ensure_ffprobe_available(cls):
        if not cls.ffprobe_path:
            raise RuntimeError("未检测到 ffprobe。请先安装 ffmpeg 后再启动")
        logger.info("runtime dependency available: ffprobe=%s", cls.ffprobe_path)

    @classmethod
    def _get_track_duration_sec(cls, file_path: str) -> float | None:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".wav":
            duration = cls._probe_wav_duration(file_path)
            if duration:
                return duration
        duration = cls._probe_ffprobe_duration(file_path)
        if duration:
            return duration
        return None

    @classmethod
    def _build_song_items(
        cls,
        files: list[str],
        music_server: LocalMusicHttpServer,
    ) -> list[SongItem]:
        songs: list[SongItem] = []
        for idx, file_path in enumerate(files, start=1):
            duration = cls._get_track_duration_sec(file_path)
            if duration is None:
                logger.warning("skip song with unknown duration: %s", file_path)
                continue
            songs.append(
                SongItem(
                    index=idx,
                    path=file_path,
                    name=os.path.basename(file_path),
                    url=music_server.create_file_url(file_path),
                    duration_sec=duration,
                )
            )
        return songs

    @classmethod
    async def _cancel_timer_unlocked(cls):
        task = cls.timer_task
        cls.timer_task = None
        if not task or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @classmethod
    async def _clear_queue_unlocked(cls) -> int:
        queued_count = len(cls.play_queue) + (1 if cls.current_song else 0)
        await cls._cancel_timer_unlocked()
        cls.play_history.clear()
        cls.play_queue.clear()
        cls.current_song = None
        await stop_playback()
        return queued_count

    @classmethod
    async def clear_queue(cls) -> int:
        async with cls.local_music_lock:
            return await cls._clear_queue_unlocked()

    @classmethod
    def _log_queue(cls, songs: list[SongItem]):
        logger.info("play queue updated: total=%d", len(songs))
        for song in songs:
            logger.info("queue[%d] %s", song.index, song.name)

    @classmethod
    def _schedule_timer_unlocked(cls, duration_sec: float):
        wait_sec = max(duration_sec, 0.1) + cls.timer_buffer_sec
        cls.timer_task = asyncio.create_task(cls._on_song_timer(wait_sec))

    @classmethod
    async def _start_song_unlocked(cls, song: SongItem, trigger: str):
        cls.current_song = song
        result = await cls._play_music_url(song.url)
        logger.info(
            "start song: trigger=%s index=%d name=%s duration=%.1fs queue_remaining=%d path=%s",
            trigger,
            song.index,
            song.name,
            song.duration_sec,
            len(cls.play_queue),
            song.path,
        )
        logger.debug("play api result: %s", result)
        cls._schedule_timer_unlocked(song.duration_sec)

    @classmethod
    async def _on_song_timer(cls, wait_sec: float):
        try:
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            return

        async with cls.local_music_lock:
            cls.timer_task = None
            if not cls.play_queue:
                cls._push_current_to_history_unlocked()
                cls.current_song = None
                return
            cls._push_current_to_history_unlocked()
            next_song = cls.play_queue.pop(0)
            logger.info(
                "auto next song: index=%d name=%s queue_remaining=%d",
                next_song.index,
                next_song.name,
                len(cls.play_queue),
            )
            await cls._start_song_unlocked(next_song, trigger="auto next")

    @classmethod
    async def refresh_music_index(cls, reason: str):
        async with cls.index_refresh_lock:
            start_time = time.monotonic()
            total = await asyncio.to_thread(cls.searcher.refresh_index)
            cost_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                "music index refreshed: reason=%s total=%d cost=%.1fms",
                reason,
                total,
                cost_ms,
            )
            return total, cost_ms

    @classmethod
    def _is_refresh_index_command(cls, text: str) -> bool:
        return is_exact_command(text, cls.refresh_keywords)

    @classmethod
    def _is_random_play_command(cls, text: str) -> bool:
        return is_exact_command(text, cls.random_play_keywords)

    @classmethod
    def _is_continue_command(cls, text: str) -> bool:
        return is_exact_command(text, cls.continue_keywords)

    @classmethod
    def _is_previous_song_command(cls, text: str) -> bool:
        return is_exact_command(text, cls.previous_keywords)

    @classmethod
    def _is_next_song_command(cls, text: str) -> bool:
        return is_exact_command(text, cls.next_keywords)

    @classmethod
    def _push_current_to_history_unlocked(cls):
        if cls.current_song is not None:
            cls.play_history.append(cls.current_song)

    @classmethod
    async def refresh_music_index_and_reply(cls, reason: str):
        try:
            if cls.index_refresh_lock.locked():
                await cls._speak_text("曲库正在刷新，请稍等")
                return
            await cls._speak_text("正在刷新曲库，请稍等")
            total, cost_ms = await cls.refresh_music_index(reason)
            await cls._speak_text(f"曲库刷新完成，共{total}首，耗时{cost_ms / 1000:.1f}秒")
        except Exception as exc:
            logger.exception("music index refresh failed: reason=%s error=%s", reason, exc)
            await cls._speak_text("曲库刷新失败，请稍后重试")

    @classmethod
    async def run_index_refresh_loop(cls):
        logger.info("scheduled index refresh started: interval=%.1fs", cls.refresh_interval_sec)
        while True:
            try:
                await asyncio.sleep(max(cls.refresh_interval_sec, 1))
                if cls.index_refresh_lock.locked():
                    logger.info("skip scheduled refresh because another refresh is in progress")
                    continue
                await cls.refresh_music_index("scheduled refresh")
            except asyncio.CancelledError:
                logger.info("scheduled index refresh stopped")
                return
            except Exception as exc:
                logger.exception("scheduled index refresh failed: %s", exc)

    @classmethod
    async def play_local_music_by_keyword(cls, keyword: str):
        if not cls.searcher.has_dirs():
            await cls._speak_text("本地音乐目录还没有配置")
            return

        logger.info("received local search request: keyword=%s", keyword)
        files = await asyncio.to_thread(cls.searcher.find, keyword)
        count = len(files)
        if count == 0:
            await cls._speak_text(f"没有找到包含{keyword}的歌曲")
            logger.info("no local songs matched keyword=%s", keyword)
            return

        songs = await asyncio.to_thread(cls._build_song_items, files, cls.music_server)
        if not songs:
            await cls._speak_text("没有可播放的歌曲，无法解析音频时长")
            logger.warning("search results exist but none are playable: keyword=%s", keyword)
            return

        cleared_count = await cls.clear_queue()
        logger.info(
            "replaced queue with search results: keyword=%s matched=%d cleared=%d",
            keyword,
            count,
            cleared_count,
        )
        cls._log_queue(songs)
        await cls._speak_text(f"找到{count}首歌曲")

        async with cls.local_music_lock:
            cls.play_queue = songs
            first_song = cls.play_queue.pop(0)
            logger.info(
                "starting first search result: index=%d name=%s queue_remaining=%d",
                first_song.index,
                first_song.name,
                len(cls.play_queue),
            )
            await cls._start_song_unlocked(first_song, trigger="search play")

    @classmethod
    async def play_random_music(cls):
        if not cls.searcher.has_dirs():
            await cls._speak_text("本地音乐目录还没有配置")
            return

        logger.info("received random play request")
        files = await asyncio.to_thread(cls.searcher.random_pick)
        count = len(files)
        if count == 0:
            await cls._speak_text("曲库为空，无法随机播放")
            logger.info("random play failed because library is empty")
            return

        songs = await asyncio.to_thread(cls._build_song_items, files, cls.music_server)
        if not songs:
            await cls._speak_text("没有可播放的歌曲，无法解析音频时长")
            logger.warning("random results exist but none are playable")
            return

        cleared_count = await cls.clear_queue()
        logger.info("replaced queue with random songs: matched=%d cleared=%d", count, cleared_count)
        cls._log_queue(songs)
        await cls._speak_text(f"好的，随机播放{count}首歌曲")

        async with cls.local_music_lock:
            cls.play_queue = songs
            first_song = cls.play_queue.pop(0)
            logger.info(
                "starting first random result: index=%d name=%s queue_remaining=%d",
                first_song.index,
                first_song.name,
                len(cls.play_queue),
            )
            await cls._start_song_unlocked(first_song, trigger="random play")

    @classmethod
    async def play_previous_song(cls):
        async with cls.local_music_lock:
            if not cls.play_history:
                await cls._speak_text("当前没有上一首")
                return
            await cls._cancel_timer_unlocked()
            if cls.current_song is not None:
                cls.play_queue.insert(0, cls.current_song)
            previous_song = cls.play_history.pop()
            logger.info(
                "manual previous song: index=%d name=%s history=%d queue=%d",
                previous_song.index,
                previous_song.name,
                len(cls.play_history),
                len(cls.play_queue),
            )
            # Wait for XiaoAi's acknowledgement reply to arrive and trigger the
            # reply interrupt before we start the new song, avoiding a race where
            # stop_playback() lands after play_music_url().
            await asyncio.sleep(cls.reply_interrupt_cooldown_sec)
            await cls._start_song_unlocked(previous_song, trigger="manual previous")

    @classmethod
    async def play_next_song(cls):
        async with cls.local_music_lock:
            if not cls.play_queue:
                await cls._speak_text("当前没有下一首")
                return
            await cls._cancel_timer_unlocked()
            cls._push_current_to_history_unlocked()
            next_song = cls.play_queue.pop(0)
            logger.info(
                "manual next song: index=%d name=%s queue=%d history=%d",
                next_song.index,
                next_song.name,
                len(cls.play_queue),
                len(cls.play_history),
            )
            # Same race-condition guard as play_previous_song.
            await asyncio.sleep(cls.reply_interrupt_cooldown_sec)
            await cls._start_song_unlocked(next_song, trigger="manual next")

    @classmethod
    async def _handle_api_command(cls, command: str, params: dict) -> str | dict:
        """Dispatch an HTTP API command to the appropriate App method."""
        if command == "say":
            payload = params.get("payload", "")
            if not payload:
                raise ValueError("missing query param: payload")
            result = await cls._speak_text(payload)
            return str(result)

        if command == "ask":
            payload = params.get("payload", "")
            if not payload:
                raise ValueError("missing query param: payload")
            result = await cls._ask_xiaoai(payload)
            return str(result)

        if command == "music":
            url = params.get("url", "")
            if not url:
                raise ValueError("missing query param: url")
            result = await cls._play_music_url(url)
            return str(result)

        if command == "local":
            keyword = params.get("keyword", "")
            if not keyword:
                raise ValueError("missing query param: keyword")
            await cls.play_local_music_by_keyword(keyword)
            return "ok"

        if command == "prev":
            await cls.play_previous_song()
            return "ok"

        if command == "next":
            await cls.play_next_song()
            return "ok"

        if command == "stop":
            await cls.stop_music()
            return "ok"

        if command == "refresh":
            await cls.refresh_music_index_and_reply("api")
            return "ok"

        if command == "random":
            await cls.play_random_music()
            return "ok"

        if command == "continue":
            await cls.resume_music()
            return "ok"

        if command == "status":
            async with cls.local_music_lock:
                current = cls.current_song
                queue = list(cls.play_queue)
            return {
                "current": {
                    "index": current.index,
                    "name": current.name,
                    "path": current.path,
                    "duration_sec": current.duration_sec,
                } if current else None,
                "queue": [
                    {
                        "index": s.index,
                        "name": s.name,
                        "path": s.path,
                        "duration_sec": s.duration_sec,
                    }
                    for s in queue
                ],
                "queue_length": len(queue),
            }

        raise ValueError(f"unknown command: {command}")

    @classmethod
    async def resume_music(cls):
        async with cls.local_music_lock:
            if cls.current_song is not None:
                logger.info("resume: restarting current song: %s", cls.current_song.name)
                await cls._cancel_timer_unlocked()
                await asyncio.sleep(cls.reply_interrupt_cooldown_sec)
                await cls._start_song_unlocked(cls.current_song, trigger="continue resume")
                return
            if cls.play_queue:
                song = cls.play_queue.pop(0)
                logger.info("resume: starting next queued song: %s", song.name)
                await asyncio.sleep(cls.reply_interrupt_cooldown_sec)
                await cls._start_song_unlocked(song, trigger="continue resume")
                return
        logger.info("resume: queue is empty, nothing to resume")
        await cls._speak_text("当前没有播放列表")

    @classmethod
    async def stop_music(cls):
        count = await cls.clear_queue()
        logger.info("playback stopped and queue cleared: count=%d", count)

    @classmethod
    async def command_loop(cls):
        print(
            "\nCommands:\n"
            "  say <text>   - speak text\n"
            "  ask <text>   - ask XiaoAi\n"
            "  music <url>  - play a remote music url\n"
            "  local <kw>   - search and play local music\n"
            "  prev         - play previous song\n"
            "  next         - play next song\n"
            "  stop         - stop playback\n"
            "  refresh      - refresh music index\n"
            "  quit         - exit\n"
        )

        while True:
            try:
                line = await asyncio.to_thread(cls._safe_read_command_line, ">>> ")
            except EOFError:
                logger.info("stdin closed, exiting command loop")
                break

            args = shlex.split(line.strip())
            if not args:
                continue

            cmd = args[0].lower()
            if cmd in {"quit", "exit"}:
                break

            if cmd == "stop":
                await cls.stop_music()
                continue

            if cmd in {"prev", "previous"}:
                await cls.play_previous_song()
                continue

            if cmd == "next":
                await cls.play_next_song()
                continue

            if cmd == "refresh":
                await cls.refresh_music_index("manual refresh")
                continue

            if len(args) < 2:
                print("missing arguments")
                continue

            content = " ".join(args[1:])
            if cmd == "say":
                logger.info("[say] result=%s", await cls._speak_text(content))
            elif cmd == "ask":
                logger.info("[ask] result=%s", await cls._ask_xiaoai(content))
            elif cmd == "music":
                logger.info("[music] result=%s", await cls._play_music_url(content))
            elif cmd == "local":
                await cls.play_local_music_by_keyword(content)
            else:
                logger.warning("unknown command: %s", cmd)

    @classmethod
    async def start(cls):
        server_task = None
        command_task = None
        cls.loop = asyncio.get_running_loop()
        cls._ensure_ffprobe_available()
        cls.music_server = build_music_server(MUSIC_CONFIG.get("http", {}) or {})
        cls.music_server.event_loop = cls.loop
        cls.music_server.api_handler = cls._handle_api_command
        cls.music_server.start()
        logger.info("music HTTP server started: %s", cls.music_server.base_url)
        logger.info("XiaoAi listener port: %d", cls.xiaoai_port)

        await cls.refresh_music_index("startup refresh")
        if cls.refresh_interval_sec > 0:
            cls.index_refresh_task = asyncio.create_task(cls.run_index_refresh_loop())
        else:
            logger.info("scheduled index refresh disabled: refresh_interval_sec=%.1f", cls.refresh_interval_sec)

        try:
            open_xiaoai_server.register_fn("on_event", on_event_callback)
            server_task = open_xiaoai_server.start_server(cls.xiaoai_port)
            if sys.stdin.isatty():
                command_task = asyncio.create_task(cls.command_loop())
                done, pending = await asyncio.wait(
                    {server_task, command_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            else:
                logger.info("non-interactive mode, command loop disabled")
                await server_task
        finally:
            if server_task:
                server_task.cancel()
            if command_task:
                command_task.cancel()
            if cls.index_refresh_task:
                cls.index_refresh_task.cancel()
                try:
                    await cls.index_refresh_task
                except asyncio.CancelledError:
                    pass
            if cls.music_server is not None:
                cls.music_server.stop()


if __name__ == "__main__":
    asyncio.run(App.start())
