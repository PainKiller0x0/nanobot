"""Session management for conversation history."""

import json
from collections import OrderedDict
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.session.db_store import SessionStore
from nanobot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0
    _hook_session_start_done: bool = field(default=False, repr=False)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        msg = {
            'role': role,
            'content': content,
            'timestamp': datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        declared: set[str] = set()
        start = 0
        for i, msg in enumerate(messages):
            role = msg.get('role')
            if role == 'assistant':
                for tc in msg.get('tool_calls') or []:
                    if isinstance(tc, dict) and tc.get('id'):
                        declared.add(str(tc['id']))
            elif role == 'tool':
                tid = msg.get('tool_call_id')
                if tid and str(tid) not in declared:
                    start = i + 1
                    declared.clear()
                    for prev in messages[start:i + 1]:
                        if prev.get('role') == 'assistant':
                            for tc in prev.get('tool_calls') or []:
                                if isinstance(tc, dict) and tc.get('id'):
                                    declared.add(str(tc['id']))
        return start

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]
        for i, message in enumerate(sliced):
            if message.get('role') == 'user':
                sliced = sliced[i:]
                break
        start = self._find_legal_start(sliced)
        if start:
            sliced = sliced[start:]
        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {'role': message['role'], 'content': message.get('content', '')}
            for key in ('tool_calls', 'tool_call_id', 'name'):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def clear(self) -> None:
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return
        start_idx = max(0, len(self.messages) - max_messages)
        while start_idx > 0 and self.messages[start_idx].get('role') != 'user':
            start_idx -= 1
        retained = self.messages[start_idx:]
        start = self._find_legal_start(retained)
        if start:
            retained = retained[start:]
        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()


class SessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / 'sessions')
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._db = SessionStore(workspace / 'sessions.db')

    def _get_session_path(self, key: str) -> Path:
        safe_key = safe_filename(key.replace(':', '_'))
        return self.sessions_dir / f'{safe_key}.jsonl'

    def _get_legacy_session_path(self, key: str) -> Path:
        safe_key = safe_filename(key.replace(':', '_'))
        return self.legacy_sessions_dir / f'{safe_key}.jsonl'

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        session = self._load(key)
        if session is None:
            session = Session(key=key)
        self._cache[key] = session
        self._cache.move_to_end(key)
        if len(self._cache) > 16:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        return session

    def _load(self, key: str) -> Session | None:
        if self._db.available:
            result = self._db.load_session(key)
            if result is not None:
                meta, messages = result
                created_at = None
                if meta.get('created_at'):
                    try:
                        created_at = datetime.fromisoformat(meta['created_at'])
                    except Exception:
                        pass
                return Session(
                    key=key,
                    messages=messages,
                    created_at=created_at or datetime.now(),
                    metadata=meta.get('metadata', {}),
                    last_consolidated=meta.get('last_consolidated', 0),
                )
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info('Migrated session {} from legacy path', key)
                except Exception:
                    logger.exception('Failed to migrate session {}', key)
        if not path.exists():
            return None
        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get('_type') == 'metadata':
                        metadata = data.get('metadata', {})
                        created_at = datetime.fromisoformat(data['created_at']) if data.get('created_at') else None
                        last_consolidated = data.get('last_consolidated', 0)
                    else:
                        messages.append(data)
            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning('Failed to load session {}: {}', key, e)
            return None

    def save(self, session: Session) -> None:
        metadata_line = {
            '_type': 'metadata',
            'key': session.key,
            'created_at': session.created_at.isoformat(),
            'updated_at': session.updated_at.isoformat(),
            'metadata': session.metadata,
            'last_consolidated': session.last_consolidated
        }
        meta_for_db = {
            'created_at': metadata_line['created_at'],
            'updated_at': metadata_line['updated_at'],
            'metadata': metadata_line['metadata'],
            'last_consolidated': metadata_line['last_consolidated'],
        }
        self._db.save_session(session.key, meta_for_db, session.messages)
        path = self._get_session_path(session.key)
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(metadata_line, ensure_ascii=False) + '\n')
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.warning('JSONL append failed for {}: {}', session.key, e)
        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for key in self._cache:
            session = self._cache[key]
            sessions.append({
                'key': session.key,
                'updated_at': session.updated_at.isoformat(),
                'message_count': len(session.messages),
            })
            seen_keys.add(key)
        for path in self.sessions_dir.glob('*.jsonl'):
            safe_key = path.stem
            if safe_key in seen_keys:
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    first_line = f.readline().strip()
                    if not first_line:
                        continue
                    data = json.loads(first_line)
                    if data.get('_type') == 'metadata':
                        sessions.append({
                            'key': safe_key.replace('_', ':'),
                            'updated_at': data.get('updated_at', ''),
                            'message_count': 0,
                        })
            except Exception:
                continue
        return sorted(sessions, key=lambda x: x.get('updated_at', ''), reverse=True)
