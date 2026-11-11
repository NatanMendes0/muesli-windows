"""Multi-key management, UI masking/unmasking, and round-robin failover rotation."""
import threading
import time


def collect_keys(cfg: dict, singular: str, plural: str) -> list:
    """Return deduplicated non-empty API keys from singular and plural config fields."""
    if not cfg:
        return []
    raw = []
    p = cfg.get(plural)
    if isinstance(p, list):
        raw.extend(p)
    s = cfg.get(singular)
    if isinstance(s, str) and s.strip():
        if s.strip() not in [x.strip() for x in raw if isinstance(x, str)]:
            raw.insert(0, s.strip())
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        k = item.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def mask_key(k: str) -> str:
    return ("*" * 8 + k[-4:]) if k else ""


def mask_keys(keys: list) -> list:
    return [mask_key(k) for k in keys if k]


def resolve_incoming_keys(old_keys: list, incoming_list=None, incoming_single=None) -> tuple:
    """Resolve masked UI values back to stored keys without leaking secrets.

    Returns (primary_key_str, full_keys_list).
    """
    if isinstance(incoming_list, list):
        pool = list(old_keys)
        resolved = []
        seen = set()
        for idx, raw in enumerate(incoming_list):
            val = (raw or "").strip() if isinstance(raw, str) else ""
            if not val:
                continue
            real = ""
            if val.startswith("********"):
                # Prefer positional match if suffix matches, else first unused match
                if idx < len(old_keys) and mask_key(old_keys[idx]) == val and old_keys[idx] in pool:
                    real = old_keys[idx]
                    pool.remove(real)
                else:
                    for cand in pool:
                        if mask_key(cand) == val:
                            real = cand
                            pool.remove(cand)
                            break
            else:
                real = val
            if real and real not in seen:
                seen.add(real)
                resolved.append(real)
        return (resolved[0] if resolved else ""), resolved

    if isinstance(incoming_single, str):
        val = incoming_single.strip()
        if val.startswith("********"):
            return (old_keys[0] if old_keys else ""), list(old_keys)
        if not val:
            rest = list(old_keys[1:]) if len(old_keys) > 1 else []
            return (rest[0] if rest else ""), rest
        rest = [k for k in old_keys[1:] if k != val]
        final = [val] + rest
        return val, final

    return (old_keys[0] if old_keys else ""), list(old_keys)


class KeyRotator:
    """Thread-safe round-robin key ordering with per-key failure cooldowns."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cursors = {}     # pool_name -> next start index
        self._cooldowns = {}   # (pool_name, key) -> unix timestamp when usable again

    def ordered_keys(self, pool_name: str, keys: list) -> list:
        if not keys:
            return []
        if len(keys) == 1:
            return list(keys)
        now = time.time()
        with self._lock:
            n = len(keys)
            start = self._cursors.get(pool_name, 0) % n
            rotated = [keys[(start + i) % n] for i in range(n)]
            healthy = [k for k in rotated if self._cooldowns.get((pool_name, k), 0.0) <= now]
            cooled = [k for k in rotated if self._cooldowns.get((pool_name, k), 0.0) > now]
            cooled.sort(key=lambda k: self._cooldowns.get((pool_name, k), 0.0))
            return healthy + cooled

    def mark_success(self, pool_name: str, key: str, keys=None):
        with self._lock:
            self._cooldowns.pop((pool_name, key), None)
            if keys and len(keys) > 1 and key in keys:
                self._cursors[pool_name] = (keys.index(key) + 1) % len(keys)
            else:
                self._cursors[pool_name] = self._cursors.get(pool_name, 0) + 1

    def mark_failure(self, pool_name: str, key: str, status_code: int = 0, retry_after: float = 0.0):
        if status_code in (401, 403):
            delay = 1800.0
        elif status_code == 402:
            delay = 900.0
        elif status_code == 429:
            delay = max(15.0, min(300.0, float(retry_after or 60.0)))
        else:
            delay = 20.0
        with self._lock:
            self._cooldowns[(pool_name, key)] = time.time() + delay

    def clear(self, pool_name: str = ""):
        with self._lock:
            if not pool_name:
                self._cursors.clear()
                self._cooldowns.clear()
            else:
                self._cursors.pop(pool_name, None)
                for k in [key for key in self._cooldowns if key[0] == pool_name]:
                    self._cooldowns.pop(k, None)


rotator = KeyRotator()
