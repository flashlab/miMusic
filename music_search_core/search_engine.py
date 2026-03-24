from __future__ import annotations

import random

from music_search_core.models import IndexedSong


class MusicSearchEngine:
    def search_with_count(
        self,
        songs: list[IndexedSong],
        keyword_lower: str,
        limit: int,
        artist_separators: list[str] | None = None,
        album_separators: list[str] | None = None,
    ) -> tuple[int, list[str]]:
        if not keyword_lower:
            return 0, []

        parsed = self._parse_combinatorial(
            keyword_lower,
            artist_separators or [],
            album_separators or [],
        )

        if parsed:
            field_type, field_value, title_keyword = parsed
            match_fn = lambda song: self._is_combinatorial_match(
                song, field_type, field_value, title_keyword
            )
        else:
            match_fn = lambda song: self._is_match(song, keyword_lower)

        if limit <= 0:
            total = sum(1 for song in songs if match_fn(song))
            return total, []
        matched = [song.path for song in songs if match_fn(song)]
        total = len(matched)
        random.shuffle(matched)
        return total, matched[:limit]

    def search(self, songs: list[IndexedSong], keyword_lower: str, limit: int) -> list[str]:
        _, selected = self.search_with_count(songs, keyword_lower, limit)
        return selected

    def random_pick(self, songs: list[IndexedSong], limit: int) -> list[str]:
        if limit <= 0 or not songs:
            return []
        paths = [item.path for item in songs]
        random.shuffle(paths)
        return paths[:limit]

    def _is_match(self, song: IndexedSong, keyword_lower: str) -> bool:
        return (
            keyword_lower in song.name_lower
            or keyword_lower in song.title_lower
            or keyword_lower in song.artist_lower
            or keyword_lower in song.album_lower
            or keyword_lower in song.folders_lower
        )

    @staticmethod
    def _parse_combinatorial(
        keyword_lower: str,
        artist_separators: list[str],
        album_separators: list[str],
    ) -> tuple[str, str, str] | None:
        """Try to parse keyword into (field_type, field_value, title_keyword).

        Returns ("artist", artist_str, title_str) or ("album", album_str, title_str),
        or None if no separator matched with a non-empty pre-string.
        """
        for sep in artist_separators:
            idx = keyword_lower.find(sep)
            if idx > 0:
                pre = keyword_lower[:idx].strip()
                post = keyword_lower[idx + len(sep):].strip()
                if pre:
                    return ("artist", pre, post)
        for sep in album_separators:
            idx = keyword_lower.find(sep)
            if idx > 0:
                pre = keyword_lower[:idx].strip()
                post = keyword_lower[idx + len(sep):].strip()
                if pre:
                    return ("album", pre, post)
        return None

    @staticmethod
    def _is_combinatorial_match(
        song: IndexedSong,
        field_type: str,
        field_value: str,
        title_keyword: str,
    ) -> bool:
        if field_type == "artist":
            if field_value not in song.artist_lower:
                return False
        elif field_type == "album":
            if field_value not in song.album_lower:
                return False
        else:
            return False
        if title_keyword:
            return title_keyword in song.name_lower or title_keyword in song.title_lower
        return True
