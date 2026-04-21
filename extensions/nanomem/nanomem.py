import json
import os
from pathlib import Path
from loguru import logger

class NanoMem:
    def __init__(self, workspace_path='/root/.nanobot'):
        # 修正路径：在容器内部，这是正确路径
        self.history_file = Path(workspace_path) / 'workspace/memory/history.jsonl'
        self._cache = []
        self._last_mtime = 0

    def _refresh_cache(self):
        try:
            if not self.history_file.exists(): return
            mtime = self.history_file.stat().st_mtime
            if mtime > self._last_mtime:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    self._cache = [json.loads(l) for l in lines[-1000:] if l.strip()]
                self._last_mtime = mtime
        except Exception as e:
            logger.error(f'NanoMem cache fail: {e}')

    def get_related_context(self, query, limit=3):
        if not query or len(query) < 2: return ""
        self._refresh_cache()
        results = []
        q = query.lower()
        query_terms = [t for t in q.split() if len(t) > 1]
        
        for entry in reversed(self._cache):
            content = entry.get('content', '')
            score = sum(2 if term in content.lower() else 0 for term in query_terms)
            if score > 0:
                results.append((score, f"- [{entry.get('timestamp')}] {content}"))
        
        results.sort(key=lambda x: x[0], reverse=True)
        top = [r[1] for r in results[:limit]]
        
        if top:
            return "\n[SYSTEM] 你找到的历史碎片：\n" + "\n".join(top)
        return ""

nano_mem = NanoMem()
